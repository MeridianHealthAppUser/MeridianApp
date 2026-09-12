"""Small write services that preserve tenant checks and an audit trail."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from practices.models import CompanyMembership

from .models import AuditEvent, ClinicalTask, MessageThread, PatientEvent, PatientMessage


def _has_staff_access(user, company_id):
    return bool(getattr(user, 'is_active', False)) and CompanyMembership.objects.filter(
        user=user, company_id=company_id, company__is_active=True, is_active=True,
    ).exists()


def request_ip_address(request):
    if request is None:
        return None
    return request.META.get('REMOTE_ADDR') or None


def record_audit(*, company, actor, action, target=None, patient=None, request=None, metadata=None):
    """Log a non-sensitive operational event without serialising clinical content."""
    return AuditEvent.objects.create(
        company=company,
        actor=actor if getattr(actor, 'is_authenticated', False) else None,
        patient=patient,
        action=action,
        target_type=target._meta.label_lower if target is not None else '',
        target_id=str(target.pk) if target is not None else '',
        metadata=metadata or {},
        ip_address=request_ip_address(request),
    )


@transaction.atomic
def post_patient_message(*, thread, sender, body, request=None):
    """Append a message and update only metadata on the thread's timeline."""
    thread = MessageThread.objects.select_for_update().get(pk=thread.pk, company_id=thread.company_id)
    owns_record = (
        sender.is_active and thread.patient.is_active and thread.company.is_active
        and thread.patient.user_id == sender.pk
    )
    if not owns_record and not _has_staff_access(sender, thread.company_id):
        raise PermissionDenied('You do not have access to this conversation.')
    if thread.is_closed:
        raise ValidationError('This conversation is closed. Please start a new one.')
    if not body.strip() or len(body.strip()) > 5000:
        raise ValidationError('Enter a message between 1 and 5,000 characters.')
    message = PatientMessage(company=thread.company, thread=thread, sender=sender, body=body.strip())
    message.full_clean()
    message.save()
    thread.last_message_at = timezone.now()
    thread.save(update_fields=('last_message_at', 'updated_at'))
    event = PatientEvent.objects.create(
        company=thread.company,
        patient=thread.patient,
        category=PatientEvent.Category.MESSAGE,
        title='New secure message',
        detail='A message was added to the secure conversation.',
        occurred_at=message.created_at,
        source_type=message._meta.label_lower,
        source_id=str(message.pk),
        is_patient_visible=True,
    )
    record_audit(
        company=thread.company,
        actor=sender,
        patient=thread.patient,
        action='message.sent',
        target=message,
        request=request,
        metadata={'thread_id': thread.pk, 'event_id': event.pk},
    )
    return message


@transaction.atomic
def complete_task(*, task: ClinicalTask, actor, request=None):
    from practices.models import Company
    from .task_services import _staff_membership, _validate_record_actor

    company = Company.objects.select_for_update().get(pk=task.company_id)
    membership = _staff_membership(company, actor)
    task = ClinicalTask.objects.select_for_update().get(pk=task.pk, company_id=task.company_id)
    _validate_record_actor(task, actor, membership)
    if task.status == ClinicalTask.Status.DONE:
        return task
    if task.status == ClinicalTask.Status.CANCELLED:
        raise ValidationError('A cancelled task cannot be completed.')
    task.mark_done()
    record_audit(
        company=task.company,
        actor=actor,
        patient=task.patient,
        action='task.completed',
        target=task,
        request=request,
    )
    return task
