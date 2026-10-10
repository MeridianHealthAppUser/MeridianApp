"""Staff-to-staff conversations, optionally about a patient.

Any active staff member can message colleagues in the same practice. Only the
members of a conversation can see it; patients never can, even when linked.
Each member keeps their own read position, so a group shows unread per person.
Any member can close a conversation, which keeps every message but takes no
replies or new colleagues until someone in it reopens it.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, F, OuterRef, Q, Subquery
from django.utils import timezone

from practices.models import CompanyMembership, Patient
from practices.tenancy import require_enabled_company
from .models import TeamMessage, TeamThread, TeamThreadMember
from .services import record_audit


def colleagues(company):
    """Active staff members of the practice, in any role."""
    return get_user_model().objects.filter(
        is_active=True, company_memberships__company=company, company_memberships__is_active=True,
    ).distinct().order_by('first_name', 'last_name', 'email')


def team_threads(company, user):
    """The member's team conversations with their own unread count."""
    last_read = TeamThreadMember.objects.filter(thread=OuterRef('pk'), user=user).values('last_read_at')[:1]
    return TeamThread.objects.for_company(company).filter(members=user).annotate(
        my_last_read=Subquery(last_read),
    ).annotate(unread_count=Count('messages', filter=~Q(messages__sender=user) & (
        Q(my_last_read__isnull=True) | Q(messages__created_at__gt=F('my_last_read'))
    ), distinct=True))


def unread_team_messages(company, user):
    return sum(thread.unread_count for thread in team_threads(company, user).filter(last_message_at__isnull=False))


def _require_staff(company, user):
    require_enabled_company(company)
    if not (getattr(user, 'is_active', False) and company.is_active and CompanyMembership.objects.filter(
            company=company, user=user, is_active=True).exists()):
        raise PermissionDenied('An active practice membership is required.')


def _require_member(thread, user):
    _require_staff(thread.company, user)
    if not TeamThreadMember.objects.filter(thread=thread, user=user).exists():
        raise PermissionDenied('You are not part of this conversation.')


def _body(body):
    body = (body or '').strip()
    if not body or len(body) > 5000:
        raise ValidationError('Enter a message between 1 and 5,000 characters.')
    return body


@transaction.atomic
def start_team_conversation(*, company, actor, members, subject, body, patient=None, request=None):
    _require_staff(company, actor)
    members = [member for member in members if member.pk != actor.pk]
    if not members:
        raise ValidationError('Choose at least one colleague.')
    allowed = set(colleagues(company).filter(pk__in=[member.pk for member in members]).values_list('pk', flat=True))
    if len(allowed) != len({member.pk for member in members}):
        raise ValidationError('Choose active colleagues in this practice.')
    if patient is not None and not Patient.objects.for_company(company).filter(pk=patient.pk, is_active=True).exists():
        raise ValidationError('Choose an active patient in this practice.')
    subject = (subject or '').strip()
    if not subject:
        raise ValidationError('Enter a subject.')
    thread = TeamThread(company=company, subject=subject[:255], patient=patient, opened_by=actor)
    thread.full_clean()
    thread.save()
    for member in [actor, *members]:
        TeamThreadMember.objects.create(company=company, thread=thread, user=member)
    post_team_message(thread=thread, sender=actor, body=body, request=request)
    return thread


@transaction.atomic
def post_team_message(*, thread, sender, body, request=None):
    thread = TeamThread.objects.select_for_update().select_related('company').get(pk=thread.pk)
    _require_member(thread, sender)
    if thread.is_closed:
        raise ValidationError('This conversation is closed. Reopen it to reply.')
    message = TeamMessage.objects.create(company=thread.company, thread=thread, sender=sender, body=_body(body))
    thread.last_message_at = message.created_at
    thread.save(update_fields=('last_message_at', 'updated_at'))
    TeamThreadMember.objects.filter(thread=thread, user=sender).update(last_read_at=message.created_at)
    # Internal discussion stays out of the patient-facing access history; the link is kept for the practice audit.
    record_audit(company=thread.company, actor=sender, action='team_message.sent', target=message, request=request,
                 metadata={'thread_id': thread.pk, 'patient_id': thread.patient_id})
    return message


@transaction.atomic
def add_team_member(*, thread, actor, user, request=None):
    thread = TeamThread.objects.select_for_update().select_related('company').get(pk=thread.pk)
    _require_member(thread, actor)
    if thread.is_closed:
        raise ValidationError('Reopen the conversation before adding a colleague.')
    if user is None or not colleagues(thread.company).filter(pk=user.pk).exists():
        raise ValidationError('Choose an active colleague in this practice.')
    if TeamThreadMember.objects.filter(thread=thread, user=user).exists():
        raise ValidationError(f'{user.full_name} is already in this conversation.')
    link = TeamThreadMember.objects.create(company=thread.company, thread=thread, user=user, added_by=actor)
    record_audit(company=thread.company, actor=actor, action='team_thread.member_added', target=thread, request=request,
                 metadata={'user_id': user.pk, 'patient_id': thread.patient_id})
    return link


@transaction.atomic
def manage_team_conversation(*, thread, actor, action, user=None, request=None):
    """Message action, for anyone in the conversation: add a colleague, close it or reopen it."""
    if action == 'add':
        return add_team_member(thread=thread, actor=actor, user=user, request=request)
    if action not in ('close', 'reopen'):
        raise ValidationError('Choose a conversation action.')
    thread = TeamThread.objects.select_for_update().select_related('company').get(pk=thread.pk)
    _require_member(thread, actor)
    closed = action == 'close'
    if thread.is_closed != closed:
        thread.is_closed = closed
        thread.save(update_fields=('is_closed', 'updated_at'))
        record_audit(company=thread.company, actor=actor, action='team_thread.closed' if closed else 'team_thread.reopened',
                     target=thread, request=request, metadata={'patient_id': thread.patient_id})
    return thread


def mark_team_thread_read(thread, user):
    TeamThreadMember.objects.filter(thread=thread, user=user).update(last_read_at=timezone.now())


def set_team_read_state(thread, user, *, read):
    """Mark a conversation read, or unread from the latest message someone else sent."""
    if read:
        mark_team_thread_read(thread, user)
        return
    latest = TeamMessage.objects.filter(thread=thread).exclude(sender=user).order_by('-created_at', '-pk').values_list(
        'created_at', flat=True).first()
    if latest is not None:
        TeamThreadMember.objects.filter(thread=thread, user=user).update(last_read_at=latest - timedelta(microseconds=1))
