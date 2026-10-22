"""Administrative follow-ups do not alter clinical screening or create accounts.

Administration > Dropouts also lists what patients and enquirers ask of the practice: a patient's "Something is
wrong, help me" (HelpRequest) until someone confirms it's resolved, and an enquirer's "Please call me" (CallbackRequest)
until someone confirms they called, or the person books.
"""

import uuid
from datetime import date, datetime

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from practices.models import Company, Patient
from .messaging import add_participant, practice_administrators
from .models import AdministrativeFollowUp, CallbackRequest, HelpRequest, Lead, MessageThread, PatientSubscription
from .operations import require_operations_actor
from .services import post_patient_message, record_audit

HELP_SUBJECT = 'Something is not working with my treatment'


@transaction.atomic
def append_follow_up(*, target, actor, note, status, next_contact_on=None, assigned_to=None,
                     stage=None, submission_key, expected_updated, request=None):
    if not isinstance(target, (Lead, PatientSubscription)):
        raise ValidationError('Choose a lead or an ended care plan.')
    company = Company.objects.select_for_update().get(pk=target.company_id)
    require_operations_actor(company, actor)
    target = type(target).objects.select_for_update().for_company(company).get(pk=target.pk)
    if isinstance(target, PatientSubscription) and target.patient.company_id != company.pk:
        raise ValidationError('Choose a care plan belonging to this practice and patient.')
    field = 'lead' if isinstance(target, Lead) else 'subscription'
    try:
        key = uuid.UUID(str(submission_key))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('Reload the follow-up form.') from None
    previous = AdministrativeFollowUp.objects.filter(submission_key=key).first()
    if previous:
        if previous.company_id != company.pk or getattr(previous, field + '_id') != target.pk or previous.author_id != actor.pk:
            raise PermissionDenied('This submission belongs to another record.')
        return previous
    if target.updated_at.isoformat() != expected_updated:
        raise ValidationError('This record changed. Reload before adding a follow-up.')
    note = note.strip() if isinstance(note, str) else ''
    if not note or len(note) > 5000:
        raise ValidationError('Enter a follow-up note of up to 5,000 characters.')
    if status not in AdministrativeFollowUp.Status.values:
        raise ValidationError('Choose a follow-up status.')
    if next_contact_on is not None and (not isinstance(next_contact_on, date) or isinstance(next_contact_on, datetime)
                                      or next_contact_on < timezone.localdate() or status == 'done'):
        raise ValidationError('Choose a future contact date for an open follow-up, or clear it when complete.')
    before = after = ''
    if field == 'lead':
        if target.converted_patient_id or target.stage == Lead.Stage.CONVERTED:
            raise ValidationError('This enquiry is already converted; use its patient record.')
        before, after = target.stage, stage or target.stage
        if after not in (Lead.Stage.QUESTIONNAIRE, Lead.Stage.BOOKING, Lead.Stage.CLOSED):
            raise ValidationError('Use enquiry, booking interest or closed. Follow-ups cannot convert a lead or record a paid consultation.')
        target.stage = after
    elif target.status not in (PatientSubscription.Status.CANCELLED, PatientSubscription.Status.PAUSED):
        raise ValidationError('Dropout follow-ups are for paused or cancelled care plans.')
    record = AdministrativeFollowUp(company=company, author=actor, note=note, status=status,
                                    next_contact_on=next_contact_on, assigned_to=assigned_to,
                                    stage_before=before, stage_after=after, submission_key=key, **{field: target})
    record.full_clean()
    record.save()
    target.save(update_fields=('stage', 'updated_at') if field == 'lead' else ('updated_at',))
    record_audit(company=company, actor=actor, action='follow_up.recorded', target=record,
                 patient=target.patient if field == 'subscription' else None, request=request,
                 metadata={field + '_id': target.pk, 'status': status, 'stage_before': before, 'stage_after': after})
    return record


@transaction.atomic
def request_help(*, patient, actor, reason, request=None):
    """A patient's "Something is wrong, help me". It starts a conversation with their prescribing doctor and the
    practice administrators, and puts them on the Dropouts list. While one is open, asking again adds to the same
    conversation."""
    patient = Patient.objects.select_for_update(of=('self',)).select_related('company', 'assigned_doctor').get(pk=patient.pk)
    if not (actor.is_active and patient.is_active and patient.user_id == actor.pk):
        raise PermissionDenied('This is not your patient record.')
    reason = reason.strip() if isinstance(reason, str) else ''
    if not reason or len(reason) > 5000:
        raise ValidationError('Tell us what isn’t working, in up to 5,000 characters.')
    help_request = HelpRequest.objects.select_for_update(of=('self',)).filter(
        company=patient.company, patient=patient, status=HelpRequest.Status.OPEN, thread__is_closed=False,
    ).select_related('thread').first()
    if help_request is None:
        thread = MessageThread(company=patient.company, patient=patient, subject=HELP_SUBJECT, opened_by=actor)
        thread.full_clean()
        thread.save()
        doctor = patient.assigned_doctor
        for person in [*([doctor] if doctor and doctor.is_active else []), *practice_administrators(patient.company)]:
            add_participant(thread, person)
        plan = PatientSubscription.objects.for_company(patient.company).filter(patient=patient).exclude(
            status=PatientSubscription.Status.CANCELLED).first()
        help_request = HelpRequest(company=patient.company, patient=patient, subscription=plan, thread=thread, reason=reason)
        help_request.full_clean()
        help_request.save()
    else:
        help_request.reason = reason
        help_request.save(update_fields=('reason', 'updated_at'))
    post_patient_message(thread=help_request.thread, sender=actor, body=reason, request=request)
    record_audit(company=patient.company, actor=actor, patient=patient, action='help_request.submitted',
                 target=help_request, request=request, metadata={'thread_id': help_request.thread_id})
    return help_request


@transaction.atomic
def resolve_help_request(*, help_request, actor, note='', request=None):
    """Confirm resolved, from the Dropouts list: it leaves the list, and the note is kept with it."""
    company = Company.objects.select_for_update().get(pk=help_request.company_id)
    require_operations_actor(company, actor)
    help_request = HelpRequest.objects.select_for_update().get(pk=help_request.pk, company=company)
    note = note.strip() if isinstance(note, str) else ''
    if len(note) > 1000:
        raise ValidationError('Keep the note to 1,000 characters or fewer.')
    if help_request.status != HelpRequest.Status.OPEN:
        raise ValidationError('This has already been dealt with. Reload to see the latest list.')
    help_request.status, help_request.resolved_by, help_request.resolved_at = HelpRequest.Status.RESOLVED, actor, timezone.now()
    help_request.resolution_note = note
    help_request.save(update_fields=('status', 'resolved_by', 'resolved_at', 'resolution_note', 'updated_at'))
    record_audit(company=company, actor=actor, patient=help_request.patient, action='help_request.resolved',
                 target=help_request, request=request, metadata={'note': bool(note)})
    return help_request


def close_help_requests(subscription):
    """A patient who cancels leaves the Dropouts list and joins its cancelled subscriptions."""
    return HelpRequest.objects.filter(company_id=subscription.company_id, patient_id=subscription.patient_id,
                                      status=HelpRequest.Status.OPEN).update(
        status=HelpRequest.Status.CANCELLED, subscription=subscription, updated_at=timezone.now())


@transaction.atomic
def request_callback(*, lead, phone, note=''):
    """"Please call me first", from the end of the questionnaire. Asking again while one is waiting updates it."""
    lead = Lead.objects.select_for_update().select_related('company').get(pk=lead.pk)
    phone = ' '.join(phone.split()) if isinstance(phone, str) else ''
    note = note.strip() if isinstance(note, str) else ''
    if not phone or len(phone) > 32:
        raise ValidationError('Enter a phone number we can call, up to 32 characters.')
    if len(note) > 1000:
        raise ValidationError('Keep your question to 1,000 characters or fewer.')
    if lead.converted_patient_id:
        raise ValidationError('You’ve already booked your consultation.')
    callback = CallbackRequest.objects.select_for_update().filter(company=lead.company, lead=lead,
                                                                  status=CallbackRequest.Status.OPEN).first()
    if callback is None:
        callback = CallbackRequest(company=lead.company, lead=lead)
    callback.name, callback.phone, callback.email, callback.note = str(lead), phone, lead.email, note
    callback.full_clean()
    callback.save()
    if not lead.phone:
        lead.phone = phone
        lead.save(update_fields=('phone', 'updated_at'))
    record_audit(company=lead.company, actor=None, action='callback.requested', target=callback,
                 metadata={'lead_id': lead.pk})
    return callback


@transaction.atomic
def confirm_called(*, callback, actor, note='', request=None):
    """Confirm called, from the Please call list: it leaves the list, and the caller's note is kept with it."""
    company = Company.objects.select_for_update().get(pk=callback.company_id)
    require_operations_actor(company, actor)
    callback = CallbackRequest.objects.select_for_update().get(pk=callback.pk, company=company)
    note = note.strip() if isinstance(note, str) else ''
    if len(note) > 1000:
        raise ValidationError('Keep the note to 1,000 characters or fewer.')
    if callback.status != CallbackRequest.Status.OPEN:
        raise ValidationError('This has already been dealt with. Reload to see the latest list.')
    callback.status, callback.called_by, callback.called_at, callback.staff_note = (
        CallbackRequest.Status.CALLED, actor, timezone.now(), note)
    callback.save(update_fields=('status', 'called_by', 'called_at', 'staff_note', 'updated_at'))
    record_audit(company=company, actor=actor, action='callback.called', target=callback, request=request,
                 metadata={'note': bool(note)})
    return callback


def close_callbacks(lead):
    """Someone who books their consultation leaves the Please call list."""
    return CallbackRequest.objects.filter(company_id=lead.company_id, lead=lead, status=CallbackRequest.Status.OPEN).update(
        status=CallbackRequest.Status.BOOKED, updated_at=timezone.now())
