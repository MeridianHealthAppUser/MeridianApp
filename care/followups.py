"""Administrative follow-ups do not alter clinical screening or create accounts."""

import uuid
from datetime import date, datetime

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from practices.models import Company
from .models import AdministrativeFollowUp, Lead, PatientSubscription
from .operations import require_operations_actor
from .services import record_audit


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
