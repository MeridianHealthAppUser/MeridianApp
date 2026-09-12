"""Fresh socket authentication and monotonic connection-history snapshots."""

from datetime import datetime, timezone as datetime_timezone
from importlib import import_module

from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY, get_user_model
from django.db import transaction
from django.utils.crypto import constant_time_compare

from care.models import Appointment, AuditEvent
from .models import CallParticipant, CallSession


def authenticated_socket_user(scope):
    """Do not reuse AuthMiddlewareStack's cached user/session after connection."""
    session_key = getattr(scope.get('session'), 'session_key', None)
    if not session_key:
        return None
    store = import_module(settings.SESSION_ENGINE).SessionStore(session_key=session_key)
    session = store.load()
    if session.get(BACKEND_SESSION_KEY) not in settings.AUTHENTICATION_BACKENDS:
        return None
    try:
        user = get_user_model().objects.get(pk=session.get(SESSION_KEY), is_active=True)
    except (get_user_model().DoesNotExist, ValueError, TypeError, OverflowError):
        return None
    saved_hash = session.get(HASH_SESSION_KEY, '')
    if not saved_hash or not any(constant_time_compare(saved_hash, value) for value in
                                (user.get_session_auth_hash(), *user.get_session_auth_fallback_hash())):
        return None
    original_user = scope.get('user')
    if not original_user or not original_user.is_authenticated or original_user.pk != user.pk:
        return None
    return user


def instant(value):
    return datetime.fromtimestamp(value, datetime_timezone.utc)


def connection_audit(access, participant, action):
    AuditEvent.objects.create(company_id=access.company_id, actor_id=participant.user_id,
        patient_id=access.patient_id, action=action, target_type=participant._meta.label_lower,
        target_id=str(participant.pk), metadata={})


@transaction.atomic
def record_presence(access, state, removed=()):
    """Ignore delayed worker updates, so replaced tabs cannot close newer calls."""
    if not state.get('session_key'):
        return None
    appointment = Appointment.objects.select_for_update().get(pk=access.appointment_id, company_id=access.company_id)
    session, created = CallSession.objects.get_or_create(session_key=state['session_key'], defaults={
        'company_id': access.company_id, 'appointment': appointment, 'patient_id': access.patient_id,
        'doctor_id': access.clinician_id, 'started_at': instant(state['started']),
        'last_seen_at': instant(state['now']), 'lease_expires_at': instant(state['now']),
    })
    if session.company_id != access.company_id or session.appointment_id != access.appointment_id:
        raise ValueError('Invalid video session ownership.')
    if state['revision'] <= session.presence_revision:
        return session
    active_keys = set()
    for uid, member in state['users'].items():
        if int(uid) not in (access.clinician_id, access.patient_user_id):
            raise ValueError('Invalid video participant ownership.')
        active_keys.add(member['connection'])
        participant, joined = CallParticipant.objects.get_or_create(connection_key=member['connection'], defaults={
            'company_id': access.company_id, 'session': session, 'user_id': int(uid),
            'joined_at': instant(member['joined']), 'last_seen_at': instant(member['seen']),
            'lease_expires_at': instant(member['expires']),
        })
        if participant.session_id != session.pk or participant.company_id != session.company_id or participant.user_id != int(uid):
            raise ValueError('Invalid connection history ownership.')
        if participant.ended_at is None:
            participant.last_seen_at, participant.lease_expires_at = instant(member['seen']), instant(member['expires'])
            participant.save(update_fields=('last_seen_at', 'lease_expires_at', 'updated_at'))
        if joined:
            connection_audit(access, participant, 'video.signalling_joined')
    reasons = {str(member['connection']): member.get('reason', 'left') for member in removed}
    for participant in session.participants.filter(ended_at__isnull=True).exclude(connection_key__in=active_keys):
        participant.ended_at = max(participant.joined_at, min(instant(state['now']), participant.lease_expires_at))
        participant.end_reason = reasons.get(str(participant.connection_key), 'expired' if participant.lease_expires_at <= instant(state['now']) else 'left')
        participant.save(update_fields=('ended_at', 'end_reason', 'updated_at'))
        connection_audit(access, participant, 'video.signalling_left')
    previous_expiry = session.lease_expires_at
    session.last_seen_at = instant(state['now'])
    session.lease_expires_at = instant(max((member['expires'] for member in state['users'].values()), default=previous_expiry.timestamp()))
    if not state['users']:
        session.ended_at = max(session.started_at, min(instant(state['now']), previous_expiry))
    session.presence_revision = state['revision']
    session.save(update_fields=('last_seen_at', 'lease_expires_at', 'ended_at', 'presence_revision', 'updated_at'))
    return session
