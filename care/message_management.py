"""Administrative conversation handling and explicit clinician escalation."""

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from practices.models import Company, CompanyMembership
from .models import AppointmentProposal, AuditEvent, ClinicalTask, MessageThread
from .services import record_audit


@transaction.atomic
def manage_conversation(*, thread, actor, action, expected_updated, doctor=None, priority='normal', note='', confirm=False, request=None):
    company = Company.objects.select_for_update().get(pk=thread.company_id)
    actor = get_user_model().objects.get(pk=actor.pk)
    membership = CompanyMembership.objects.filter(company=company, user=actor, user__is_active=True, company__is_active=True, is_active=True).first()
    if membership is None:
        raise PermissionDenied('Only an active member of the care team can manage this conversation.')
    thread = MessageThread.objects.select_for_update().select_related('patient').for_company(company).get(pk=thread.pk)
    if not thread.patient.is_active or thread.patient.company_id != company.pk:
        raise PermissionDenied('This conversation is not available in this practice.')
    if confirm is not True or action not in ('escalate', 'close', 'reopen'):
        raise ValidationError('Choose and confirm a conversation action.')
    if thread.updated_at.isoformat() != expected_updated:
        raise ValidationError('This conversation changed. Reload before continuing.')
    if action in ('close', 'reopen'):
        closed = action == 'close'
        if thread.is_closed == closed:
            return thread
        if closed and AppointmentProposal.objects.for_company(company).filter(thread=thread, status='pending').exists():
            raise ValidationError('Resolve or withdraw pending appointment suggestions before closing this conversation. The appointment is unchanged.')
        thread.is_closed = closed
        thread.save(update_fields=('is_closed', 'updated_at'))
        record_audit(company=company, actor=actor, patient=thread.patient, action=f'message.thread_{action}d' if closed else 'message.thread_reopened', target=thread, request=request)
        return thread
    if thread.is_closed:
        raise ValidationError('Reopen the conversation before routing it to a doctor.')
    if doctor is None or not CompanyMembership.objects.filter(company=company, user=doctor, user__is_active=True, is_active=True, role='doctor').exists():
        raise ValidationError('Select an active doctor in this practice.')
    if priority not in ClinicalTask.Priority.values:
        raise ValidationError('Select a valid task priority.')
    note = note.strip() if isinstance(note, str) else ''
    if len(note) > 2000:
        raise ValidationError('Keep the care-team note within 2,000 characters.')
    existing_ids = AuditEvent.objects.for_company(company).filter(action='message.escalated', target_type='care.clinicaltask', metadata__thread_id=thread.pk).values_list('target_id', flat=True)
    existing = ClinicalTask.objects.for_company(company).filter(pk__in=[int(value) for value in existing_ids if value.isdecimal()], patient=thread.patient, status__in=('open', 'in_progress')).first()
    if existing:
        if existing.assigned_to_id != doctor.pk:
            raise ValidationError('An open task already exists for this conversation. Reassign that task instead.')
        return existing
    task = ClinicalTask(company=company, patient=thread.patient, created_by=actor, assigned_to=doctor, priority=priority,
                        title='Reply to patient message', description=f'Conversation #{thread.pk}: {thread.subject}\n\n{note}'.strip(), due_at=timezone.now())
    task.full_clean()
    task.save()
    record_audit(company=company, actor=actor, patient=thread.patient, action='message.escalated', target=task, request=request, metadata={'thread_id': thread.pk})
    return task
