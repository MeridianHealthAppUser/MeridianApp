"""Audited consultation signing and protected laboratory-result workflows."""

import hashlib
from datetime import date, datetime
from uuid import UUID

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient
from practices.tenancy import require_enabled_company

from .models import Appointment, ClinicalEncounter, ClinicalNote, ClinicalTask, LabRequest, LabResult, PatientEvent
from .services import record_audit


MAX_LAB_RESULT_BYTES = 5 * 1024 * 1024
WORKFLOW_TASK_ERROR = 'Complete this task through its consultation or laboratory review workflow.'


def _active_actor(actor):
    if not getattr(actor, 'is_active', False) or not get_user_model().objects.filter(pk=actor.pk, is_active=True).exists():
        raise PermissionDenied('An active account is required.')


def _lock_context(company, patient, actor):
    require_enabled_company(company)
    # Clinical writes share the company-first lock order used by task/tag writes.
    company = Company.objects.select_for_update().filter(pk=company.pk, is_active=True).first()
    if company is None:
        raise PermissionDenied('This practice is not active.')
    _active_actor(actor)
    patient = Patient.objects.select_for_update().filter(pk=patient.pk, company=company, is_active=True).first()
    if patient is None:
        raise PermissionDenied('This patient is not available in the selected practice.')
    return company, patient


def _require_clinician(company, actor, owner_id=None, *, prescriber=False):
    require_enabled_company(company)
    _active_actor(actor)
    if owner_id is not None and actor.pk != owner_id:
        raise PermissionDenied('Only the clinician responsible for this record can change it.')
    types = CompanyMembership.PRESCRIBER_TYPES if prescriber else CompanyMembership.CLINICIAN_TYPES
    if not CompanyMembership.objects.filter(
        company=company, company__is_active=True, user=actor, is_active=True, clinician_type__in=types,
    ).exists():
        raise PermissionDenied('An active doctor membership in this practice is required.' if prescriber
                               else 'An active clinician membership in this practice is required.')


def _require_doctor(company, actor, owner_id=None):
    # Treatment, compounding and blood tests stay with doctors.
    _require_clinician(company, actor, owner_id, prescriber=True)


def _submission_key(value):
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('The submission reference is invalid. Reload the form and try again.') from None


def _text(value, *, label, maximum=10000, required=True):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValidationError(f'{label} must contain {"between 1 and" if required else "no more than"} {maximum:,} characters.')
    return value


def _owned_duplicate(queryset, key, company, patient, actor, owner_field):
    if key is None:
        return None
    duplicate = queryset.filter(submission_key=key).first()
    if duplicate is not None and (
        duplicate.company_id != company.pk or duplicate.patient_id != patient.pk
        or getattr(duplicate, owner_field) != actor.pk
    ):
        raise PermissionDenied('This submission reference does not belong to this record.')
    if duplicate is not None:
        return queryset.select_for_update().get(pk=duplicate.pk, company=company, patient=patient)
    return None


def _patient_event(record, title, detail):
    PatientEvent.objects.create(
        company=record.company, patient=record.patient, category=PatientEvent.Category.CLINICAL,
        title=title, detail=detail, source_type=record._meta.label_lower,
        source_id=str(record.pk), is_patient_visible=True,
    )


def _new_workflow_task(company, patient, doctor, title, due_at=None):
    task = ClinicalTask(
        company=company, patient=patient, assigned_to=doctor, created_by=doctor,
        title=title, description='', status=ClinicalTask.Status.OPEN, due_at=due_at,
    )
    task.full_clean()
    task.save()
    return task


def _complete_workflow_task(task, company, patient, doctor, request):
    task = ClinicalTask.objects.select_for_update().filter(pk=task.pk, company=company, patient=patient).first()
    if task is None or task.assigned_to_id != doctor.pk or task.created_by_id != doctor.pk:
        raise ValidationError('The linked clinical task no longer matches this record. Contact your practice.')
    if task.status == ClinicalTask.Status.CANCELLED:
        raise ValidationError('The linked clinical task has been cancelled. Contact your practice.')
    if task.status != ClinicalTask.Status.DONE:
        task.mark_done()
        record_audit(company=company, actor=doctor, patient=patient, action='task.completed', target=task, request=request)
    return task


def is_clinical_workflow_task(task):
    if not getattr(task, 'pk', None):
        return False
    return ClinicalTask.objects.filter(pk=task.pk).filter(
        Q(encounter_signing__isnull=False) | Q(lab_review_request__isnull=False) | Q(compounding_record__isnull=False),
    ).exists()


@transaction.atomic
def save_consultation(*, company, patient, actor, summary, occurred_at, appointment=None,
                      encounter=None, expected_revision=None, submission_key=None, sign=False, request=None):
    company, patient = _lock_context(company, patient, actor)
    _require_clinician(company, actor)
    if type(sign) is not bool:
        raise ValidationError('Choose whether to save a draft or explicitly sign the consultation.')
    summary = _text(summary, label='The consultation note', required=sign)
    if not isinstance(occurred_at, datetime) or timezone.is_naive(occurred_at) or occurred_at > timezone.now():
        raise ValidationError({'occurred_at': 'Choose the actual consultation time, not a future time.'})
    key = _submission_key(submission_key)
    created = encounter is None
    if created:
        duplicate = _owned_duplicate(ClinicalEncounter.objects, key, company, patient, actor, 'clinician_id')
        if duplicate is not None:
            return duplicate
        record = ClinicalEncounter(company=company, patient=patient, clinician=actor, submission_key=key)
    else:
        record = ClinicalEncounter.objects.select_for_update().filter(pk=encounter.pk, company=company, patient=patient).first()
        if record is None:
            raise PermissionDenied('This consultation is not available in this patient record.')
        _require_clinician(company, actor, record.clinician_id)
        if appointment is not None and appointment.pk != record.appointment_id:
            raise ValidationError('An existing consultation cannot be moved to another appointment.')
        if record.status == ClinicalEncounter.Status.SIGNED:
            if sign and record.clinical_summary == summary and record.occurred_at == occurred_at:
                return record
            raise ValidationError('A signed consultation is immutable. Record a separate follow-up note for a correction.')
        if type(expected_revision) is not int or expected_revision != record.revision:
            raise ValidationError('This draft changed since you opened it. Reload before saving or signing.')
    appointment_id = appointment.pk if created and appointment is not None else record.appointment_id
    if appointment_id is not None:
        booked = Appointment.objects.select_for_update().filter(
            pk=appointment_id, company=company, patient=patient, clinician=actor,
        ).first()
        if booked is None:
            raise ValidationError({'appointment': 'Choose this clinician’s appointment for this patient and practice.'})
        if sign and (booked.starts_at > timezone.now() or booked.status in (Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW)):
            raise ValidationError('A future, cancelled or missed appointment cannot be signed as a consultation.')
        record.appointment = booked
    record.clinical_summary = summary
    record.occurred_at = occurred_at
    record.status = ClinicalEncounter.Status.DRAFT
    if created:
        record.full_clean()
        record.save()
    if record.signing_task_id is None:
        record.signing_task = _new_workflow_task(company, patient, actor, 'Sign consultation note')
    if sign:
        note = ClinicalNote(
            company=company, patient=patient, author=actor, note_type=ClinicalNote.NoteType.CONSULT,
            body=summary, is_private=False,
        )
        note.full_clean()
        note.save()
        record.signed_note = note
        record.status = ClinicalEncounter.Status.SIGNED
        record.signed_by = actor
        record.signed_at = timezone.now()
        record.signing_task = _complete_workflow_task(record.signing_task, company, patient, actor, request)
    record.revision += 1
    record.full_clean()
    record.save()
    record_audit(
        company=company, actor=actor, patient=patient,
        action='consultation.signed' if sign else ('consultation.created' if created else 'consultation.updated'),
        target=record, request=request,
        metadata={'signing_task_id': record.signing_task_id, 'signed_note_id': record.signed_note_id},
    )
    if sign:
        _patient_event(record, 'Consultation note signed', 'Your clinician has completed a consultation record.')
    return record


@transaction.atomic
def create_lab_request(*, company, patient, actor, panel_name, due_on=None, submission_key=None, request=None):
    company, patient = _lock_context(company, patient, actor)
    _require_doctor(company, actor)
    panel_name = _text(panel_name, label='The requested panel', maximum=255).strip()
    if due_on is not None and (not isinstance(due_on, date) or isinstance(due_on, datetime)):
        raise ValidationError({'due_on': 'Choose a valid due date.'})
    key = _submission_key(submission_key)
    duplicate = _owned_duplicate(LabRequest.objects, key, company, patient, actor, 'requested_by_id')
    if duplicate is not None:
        return duplicate
    lab = LabRequest(company=company, patient=patient, requested_by=actor, panel_name=panel_name, due_on=due_on, submission_key=key)
    lab.full_clean()
    lab.save()
    record_audit(company=company, actor=actor, patient=patient, action='lab_request.created', target=lab, request=request)
    _patient_event(lab, 'Laboratory request created', 'Your clinician has requested laboratory tests. See the request for details.')
    return lab


def validate_lab_pdf(filename, content):
    """Basic PDF framing and size checks only; this is not malware scanning."""
    if not isinstance(filename, str):
        raise ValidationError('Choose a PDF file with a valid filename.')
    filename = filename.replace('\\', '/').rsplit('/', 1)[-1].strip()
    if not filename or len(filename) > 160 or any(ord(char) < 32 or ord(char) == 127 for char in filename) or not filename.lower().endswith('.pdf'):
        raise ValidationError('Use a PDF filename of at most 160 characters without control characters.')
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise ValidationError('Choose a PDF file.')
    content = bytes(content)
    if not content or len(content) > MAX_LAB_RESULT_BYTES:
        raise ValidationError('The PDF must be no larger than 5 MB.')
    if not content.startswith(b'%PDF-') or not content.rstrip().endswith(b'%%EOF'):
        raise ValidationError('The file does not have a valid PDF header and ending. Choose another PDF.')
    return filename, content, hashlib.sha256(content).hexdigest()


def _locked_lab(lab_request, actor):
    company, patient = _lock_context(lab_request.company, lab_request.patient, actor)
    lab = LabRequest.objects.select_for_update().filter(pk=lab_request.pk, company=company, patient=patient).first()
    if lab is None:
        raise PermissionDenied('This laboratory request is not available.')
    return company, patient, lab


@transaction.atomic
def submit_lab_result(*, lab_request, actor, filename, content, request=None):
    company, patient, lab = _locked_lab(lab_request, actor)
    if actor.pk != patient.user_id:
        _require_doctor(company, actor, lab.requested_by_id)
    filename, content, digest = validate_lab_pdf(filename, content)
    result = LabResult.objects.select_for_update().filter(lab_request=lab).first()
    if result is not None:
        if result.company_id != company.pk or result.patient_id != patient.pk:
            raise ValidationError('The saved report does not match this request. Contact your practice.')
        if result.sha256 == digest:
            return result
        raise ValidationError('A report has already been uploaded. The original cannot be overwritten; contact your clinician.')
    if lab.status != LabRequest.Status.REQUESTED:
        raise ValidationError('This request no longer accepts a new report. Contact your clinician.')
    doctor = lab.requested_by
    if not doctor.is_active or not CompanyMembership.objects.filter(
        company=company, user=doctor, is_active=True, clinician_type__in=CompanyMembership.PRESCRIBER_TYPES,
    ).exists():
        raise ValidationError('The requesting clinician is no longer active. Contact your practice before uploading.')
    result = LabResult(
        company=company, patient=patient, lab_request=lab, uploaded_by=actor,
        filename=filename, content=content, size=len(content), sha256=digest,
    )
    result.full_clean()
    result.save()
    lab.review_task = _new_workflow_task(company, patient, doctor, 'Review laboratory results')
    lab.status = LabRequest.Status.UPLOADED
    lab.full_clean()
    lab.save(update_fields=('status', 'review_task', 'updated_at'))
    record_audit(
        company=company, actor=actor, patient=patient, action='lab_result.uploaded', target=result, request=request,
        metadata={'lab_request_id': lab.pk, 'review_task_id': lab.review_task_id},
    )
    _patient_event(lab, 'Laboratory report received', 'Your report is ready for your requesting clinician to review.')
    return result


@transaction.atomic
def review_lab_request(*, lab_request, actor, review_note, request=None):
    company, patient, lab = _locked_lab(lab_request, actor)
    _require_doctor(company, actor, lab.requested_by_id)
    review_note = _text(review_note, label='The review note')
    if lab.status == LabRequest.Status.REVIEWED:
        if lab.reviewed_by_id == actor.pk and lab.result_summary == review_note:
            return lab
        raise ValidationError('This laboratory review is immutable. Record a separate follow-up note for a correction.')
    if lab.status != LabRequest.Status.UPLOADED or not LabResult.objects.filter(lab_request=lab, company=company, patient=patient).exists():
        raise ValidationError('Upload the laboratory report before completing its review.')
    if lab.review_task_id is None:
        raise ValidationError('This request has no linked review task. Contact your practice.')
    lab.result_summary = review_note
    lab.reviewed_by = actor
    lab.reviewed_at = timezone.now()
    lab.status = LabRequest.Status.REVIEWED
    lab.full_clean()
    lab.review_task = _complete_workflow_task(lab.review_task, company, patient, actor, request)
    lab.save(update_fields=('result_summary', 'reviewed_by', 'reviewed_at', 'status', 'updated_at'))
    record_audit(
        company=company, actor=actor, patient=patient, action='lab_request.reviewed', target=lab, request=request,
        metadata={'review_task_id': lab.review_task_id},
    )
    _patient_event(lab, 'Laboratory report reviewed', 'Your requesting clinician has reviewed your report.')
    return lab
