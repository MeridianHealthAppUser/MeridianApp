"""Conversation handling by the people in it: add a clinician, close or reopen."""

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from practices.models import Company, CompanyMembership
from practices.tenancy import require_enabled_company
from .messaging import hand_over, is_participant
from .models import AppointmentProposal, MessageThread
from .services import record_audit


@transaction.atomic
def manage_conversation(*, thread, actor, action, expected_updated, doctor=None, confirm=False, request=None):
    company = Company.objects.select_for_update().get(pk=thread.company_id)
    require_enabled_company(company)
    actor = get_user_model().objects.get(pk=actor.pk)
    membership = CompanyMembership.objects.filter(company=company, user=actor, user__is_active=True, company__is_active=True, is_active=True).first()
    if membership is None:
        raise PermissionDenied('Only an active member of the care team can manage this conversation.')
    thread = MessageThread.objects.select_for_update().select_related('patient').for_company(company).get(pk=thread.pk)
    if not thread.patient.is_active or thread.patient.company_id != company.pk:
        raise PermissionDenied('This conversation is not available in this practice.')
    if not is_participant(thread, actor):
        raise PermissionDenied('Only people in this conversation can manage it.')
    if confirm is not True or action not in ('handover', 'close', 'reopen'):
        raise ValidationError('Choose and confirm a conversation action.')
    if thread.updated_at.isoformat() != expected_updated:
        raise ValidationError('This conversation changed. Reload before continuing.')
    if action == 'handover':
        hand_over(thread=thread, actor=actor, clinician=doctor, request=request)
        return thread
    closed = action == 'close'
    if thread.is_closed == closed:
        return thread
    if closed and AppointmentProposal.objects.for_company(company).filter(thread=thread, status='pending').exists():
        raise ValidationError('Resolve or withdraw pending appointment suggestions before closing this conversation. The appointment is unchanged.')
    thread.is_closed = closed
    thread.save(update_fields=('is_closed', 'updated_at'))
    record_audit(company=company, actor=actor, patient=thread.patient, action='message.thread_closed' if closed else 'message.thread_reopened', target=thread, request=request)
    return thread
