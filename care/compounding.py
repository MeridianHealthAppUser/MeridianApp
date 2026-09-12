"""Doctor-owned manual compounding tracking. Nothing here issues a prescription."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .clinical import _complete_workflow_task, _lock_context, _require_doctor, _submission_key, _text
from .models import ClinicalTask, CompoundingRecord, TreatmentAuthorization
from .services import record_audit
from .treatment import authorization_is_current


def _authorization(company, patient, actor, pk):
    authorization = TreatmentAuthorization.objects.select_for_update().for_company(company).filter(
        pk=pk, patient=patient,
    ).select_related('product', 'prescribed_by', 'patient', 'company').first()
    if authorization is None:
        raise PermissionDenied('The authorisation is not available in this patient’s practice.')
    _require_doctor(company, actor, authorization.prescribed_by_id)
    if not authorization.product.is_compounded or not authorization_is_current(authorization):
        raise ValidationError('A current authorisation for a compounded product is required. Review the treatment separately first.')
    return authorization


def _locked(record, actor):
    company, patient = _lock_context(record.company, record.patient, actor)
    record = CompoundingRecord.objects.select_for_update().for_company(company).get(pk=record.pk, patient=patient)
    _require_doctor(company, actor, record.clinician_id)
    return company, patient, record


def _revision(record, expected_revision):
    if type(expected_revision) is not int or expected_revision != record.revision:
        raise ValidationError('This compounding record changed in another tab. Reload it before making changes.')


def _audit(record, actor, action, request):
    record_audit(company=record.company, patient=record.patient, actor=actor, action=f'compounding.{action}',
                 target=record, request=request, metadata={'authorization_id': record.authorization_id})


@transaction.atomic
def create_compounding_record(*, company, authorization, actor, preparation_note='', submission_key=None, request=None):
    company, patient = _lock_context(company, authorization.patient, actor)
    _require_doctor(company, actor)
    key = _submission_key(submission_key)
    if key is not None:
        existing = CompoundingRecord.objects.select_for_update().filter(submission_key=key).first()
        if existing:
            if (existing.company_id, existing.patient_id, existing.clinician_id, existing.authorization_id) != (company.pk, patient.pk, actor.pk, authorization.pk):
                raise PermissionDenied('The submission reference belongs to another record.')
            return existing
    authorization = _authorization(company, patient, actor, authorization.pk)
    preparation_note = _text(preparation_note, label='Preparation note', required=False)
    snapshot = dict(
        company_name=company.name, patient_name=str(patient), patient_id_number=patient.id_number,
        doctor_name=actor.full_name, authorization_id=authorization.pk,
        product_name=authorization.product.name, strength=authorization.product.strength,
        maximum_dose=authorization.max_dose, quantity_per_cycle=authorization.quantity_per_cycle,
        starts_on=authorization.starts_on.isoformat(), expires_on=authorization.expires_on.isoformat(),
        recorded_on=timezone.localdate().isoformat(),
    )
    task = ClinicalTask(company=company, patient=patient, created_by=actor, assigned_to=actor,
                        title='Review manual compounding workflow', description='', due_at=timezone.now())
    task.full_clean()
    task.save()
    record = CompoundingRecord(company=company, patient=patient, authorization=authorization, clinician=actor,
                                preparation_note=preparation_note, snapshot=snapshot, task=task, submission_key=key)
    record.full_clean()
    record.save()
    _audit(record, actor, 'created', request)
    return record


@transaction.atomic
def update_compounding_draft(*, record, actor, preparation_note, expected_revision, request=None):
    company, patient, record = _locked(record, actor)
    if record.status != CompoundingRecord.Status.DRAFT:
        raise ValidationError('Reviewed or submitted records are immutable. Create a new tracking record for a separate decision.')
    preparation_note = _text(preparation_note, label='Preparation note', required=False)
    if preparation_note == record.preparation_note:
        return record
    _revision(record, expected_revision)
    _authorization(company, patient, actor, record.authorization_id)
    record.preparation_note = preparation_note
    record.revision += 1
    record.full_clean()
    record.save(update_fields=('preparation_note', 'revision', 'updated_at'))
    _audit(record, actor, 'draft_updated', request)
    return record


@transaction.atomic
def review_compounding_record(*, record, actor, confirm=False, expected_revision=None, request=None):
    company, patient, record = _locked(record, actor)
    if confirm is not True:
        raise ValidationError('Confirm that you reviewed the linked authorisation and this manual tracking record.')
    if record.status in (CompoundingRecord.Status.READY, CompoundingRecord.Status.SUBMITTED):
        return record
    _revision(record, expected_revision)
    if record.status != CompoundingRecord.Status.DRAFT:
        raise ValidationError('Only a draft compounding record can be reviewed.')
    _authorization(company, patient, actor, record.authorization_id)
    record.status = CompoundingRecord.Status.READY
    record.reviewed_by, record.reviewed_at = actor, timezone.now()
    record.revision += 1
    record.full_clean()
    record.save(update_fields=('status', 'reviewed_by', 'reviewed_at', 'revision', 'updated_at'))
    _audit(record, actor, 'reviewed', request)
    return record


@transaction.atomic
def mark_compounding_submitted(*, record, actor, external_reference, confirm=False, expected_revision=None, request=None):
    company, patient, record = _locked(record, actor)
    external_reference = _text(external_reference, label='External submission reference', maximum=120).strip()
    if confirm is not True:
        raise ValidationError('Confirm that you have completed the separate manual submission.')
    if record.status == CompoundingRecord.Status.SUBMITTED:
        if record.external_reference == external_reference:
            return record
        raise ValidationError('The submitted record and its external reference are immutable.')
    _revision(record, expected_revision)
    if record.status != CompoundingRecord.Status.READY:
        raise ValidationError('Review this draft before recording manual submission.')
    _authorization(company, patient, actor, record.authorization_id)
    if record.reviewed_by_id != actor.pk:
        raise PermissionDenied('Only the reviewing owner doctor can record submission.')
    record.status, record.submitted_at = CompoundingRecord.Status.SUBMITTED, timezone.now()
    record.external_reference = external_reference
    record.revision += 1
    record.full_clean()
    record.save(update_fields=('status', 'submitted_at', 'external_reference', 'revision', 'updated_at'))
    _complete_workflow_task(record.task, company, patient, actor, request)
    _audit(record, actor, 'submission_recorded', request)
    return record


@transaction.atomic
def cancel_compounding_record(*, record, actor, confirm=False, expected_revision=None, request=None):
    company, patient, record = _locked(record, actor)
    if confirm is not True:
        raise ValidationError('Confirm cancellation of this unsubmitted tracking record.')
    if record.status == CompoundingRecord.Status.CANCELLED:
        return record
    _revision(record, expected_revision)
    if record.status == CompoundingRecord.Status.SUBMITTED:
        raise ValidationError('A submitted tracking record cannot be cancelled or altered here.')
    task = ClinicalTask.objects.select_for_update().get(pk=record.task_id, company=company, patient=patient)
    if task.assigned_to_id != actor.pk:
        raise ValidationError('The workflow task no longer matches the responsible doctor.')
    record.status, record.cancelled_at = CompoundingRecord.Status.CANCELLED, timezone.now()
    record.revision += 1
    record.save(update_fields=('status', 'cancelled_at', 'revision', 'updated_at'))
    task.status, task.completed_at = ClinicalTask.Status.CANCELLED, None
    task.save(update_fields=('status', 'completed_at', 'updated_at'))
    _audit(record, actor, 'cancelled', request)
    return record
