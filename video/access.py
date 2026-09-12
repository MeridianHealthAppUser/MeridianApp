"""One authoritative, fresh access policy for HTTP and WebSocket rooms."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from care.models import Appointment
from practices.models import CompanyMembership


class VideoAccessDenied(PermissionDenied):
    code = 4003

    def __init__(self):
        super().__init__('This consultation room is not available.')


# Both spellings refer to the same exception for HTTP/signalling consumers.
RoomAccessDenied = VideoAccessDenied


@dataclass(frozen=True)
class RoomAccess:
    appointment_id: int
    company_id: int
    company_name: str
    clinician_id: int
    patient_user_id: int
    patient_id: int
    user_id: int
    peer_id: int
    peer_name: str
    user_name: str
    role: str
    starts_at: datetime
    ends_at: datetime
    join_opens_at: datetime
    join_closes_at: datetime

    @property
    def practice_name(self):
        return self.company_name

    @property
    def room_url(self):
        return f'/video/appointments/{self.appointment_id}/'


def _identifier(value):
    value = getattr(value, 'pk', value)
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        value = int(value)
    if type(value) is not int or not 0 < value < 2 ** 63:
        raise VideoAccessDenied()
    return value


def _current_time(now):
    now = timezone.now() if now is None else now
    if not isinstance(now, datetime) or timezone.is_naive(now):
        raise VideoAccessDenied()
    return now


def _authorized_appointments(user_id):
    if not getattr(settings, 'VIDEO_ENABLED', True):
        raise VideoAccessDenied()
    active_doctor = CompanyMembership.objects.filter(
        company_id=OuterRef('company_id'), user_id=OuterRef('clinician_id'),
        is_active=True, role=CompanyMembership.Role.DOCTOR,
    )
    # Both identities must still be active, even when only one is connecting.
    # No session-selected practice or staff override grants room access.
    return Appointment.objects.filter(
        status=Appointment.Status.BOOKED, company__is_active=True,
        patient__is_active=True, patient__company_id=F('company_id'),
        clinician__is_active=True, patient__user__is_active=True,
        duration_minutes__gte=5, duration_minutes__lte=120,
    ).filter(Q(clinician_id=user_id) | Q(patient__user_id=user_id)).exclude(
        clinician_id=F('patient__user_id'),
    ).annotate(video_doctor_active=Exists(active_doctor)).filter(
        video_doctor_active=True,
    ).select_related('company', 'clinician', 'patient__user')


def _access_from_appointment(appointment, user_id, *, now, require_window):
    starts_at = appointment.starts_at
    ends_at = starts_at + timedelta(minutes=appointment.duration_minutes)
    opens_at = starts_at - timedelta(minutes=settings.VIDEO_JOIN_EARLY_MINUTES)
    closes_at = ends_at + timedelta(minutes=settings.VIDEO_JOIN_GRACE_MINUTES)
    if require_window and not opens_at <= now <= closes_at:
        raise VideoAccessDenied()
    doctor = appointment.clinician
    patient_user = appointment.patient.user
    role = 'doctor' if user_id == doctor.pk else 'patient'
    user, peer = (doctor, patient_user) if role == 'doctor' else (patient_user, doctor)
    return RoomAccess(
        appointment_id=appointment.pk, company_id=appointment.company_id,
        company_name=appointment.company.name, clinician_id=doctor.pk,
        patient_user_id=patient_user.pk, patient_id=appointment.patient_id,
        user_id=user_id, peer_id=peer.pk, peer_name=peer.full_name,
        user_name=user.full_name, role=role, starts_at=starts_at, ends_at=ends_at,
        join_opens_at=opens_at, join_closes_at=closes_at,
    )


def resolve_room_access(user_id, appointment_id, *, now=None, require_window=True):
    """Return a snapshot only after checking current persisted participants.

    ``require_window=False`` is for explanatory join-window UI only. Every
    room, credential, signal and presence request must use the default True.
    """
    user_id, appointment_id = _identifier(user_id), _identifier(appointment_id)
    now = _current_time(now)
    appointment = _authorized_appointments(user_id).filter(pk=appointment_id).first()
    if appointment is None:
        raise VideoAccessDenied()
    return _access_from_appointment(appointment, user_id, now=now, require_window=require_window)


def attach_video_join(appointments, user_id, *, allowed_role, now=None):
    """Decorate a bounded, already scoped page using one fresh policy query."""
    appointments = list(appointments)
    for appointment in appointments:
        appointment.video_room_url = None
        appointment.video_join_message = ''
        appointment.video_join_opens_at = None
        appointment.video_join_closes_at = None
    if not appointments or allowed_role not in ('doctor', 'patient'):
        return appointments
    try:
        user_id = _identifier(user_id)
        now = _current_time(now)
        authorized = _authorized_appointments(user_id).filter(pk__in=[item.pk for item in appointments])
        access_by_id = {
            item.pk: _access_from_appointment(item, user_id, now=now, require_window=False)
            for item in authorized
        }
    except VideoAccessDenied:
        return appointments
    for appointment in appointments:
        access = access_by_id.get(appointment.pk)
        if access is None or access.role != allowed_role:
            continue
        appointment.video_join_opens_at = access.join_opens_at
        appointment.video_join_closes_at = access.join_closes_at
        if not settings.DEBUG and not getattr(settings, 'VIDEO_REDIS_URL', ''):
            appointment.video_join_message = 'Video consultations are temporarily unavailable.'
        elif now < access.join_opens_at:
            appointment.video_join_message = 'The video room opens shortly before your appointment.'
        elif now > access.join_closes_at:
            appointment.video_join_message = 'The video join window has ended.'
        else:
            appointment.video_room_url = access.room_url
    return appointments
