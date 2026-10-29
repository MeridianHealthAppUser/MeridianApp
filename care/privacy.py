"""Explicit privacy workflow writes; never delete health records or send mail."""

import hashlib
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from practices.models import Company, CompanyMembership, Patient
from practices.tenancy import require_enabled_company
from .models import AuditEvent, ClinicalTask, ConsentDocument, MessageThread, PatientCommunicationPreference, PatientDataRequest, PatientDataRequestReply
from .services import post_patient_message, record_audit


ADMIN_ROLES = (CompanyMembership.Role.PRACTICE_ADMIN, CompanyMembership.Role.SUPER_ADMIN)
# Requests still waiting on the practice: the Data requests menu counts these.
UNRESOLVED = (PatientDataRequest.Status.OPEN, PatientDataRequest.Status.IN_REVIEW)
# Replying with one of these completes the request's task.
CLOSING = (PatientDataRequest.Status.RESOLVED, PatientDataRequest.Status.DECLINED)
TASK_TITLES = {
    PatientDataRequest.Kind.ACCESS: 'Data request: copy of information',
    PatientDataRequest.Kind.CORRECTION: 'Data request: correction',
    PatientDataRequest.Kind.DELETION: 'Data request: deletion review',
    PatientDataRequest.Kind.QUESTION: 'Data request: privacy question',
}


def task_done_reply(actor):
    """What the patient reads when the request is finished from its task rather than with a written reply."""
    return f'{actor.formal_name} has marked your request as complete.'


def unresolved_request_count(company):
    return PatientDataRequest.objects.filter(company=company, patient__company=company, status__in=UNRESOLVED).count()


def request_task_assignee(company, patient):
    """The patient's clinician, so it's in their My tasks; administrators and Super Admins see every task anyway."""
    if not patient.assigned_doctor_id:
        return None
    clinician = CompanyMembership.objects.filter(
        company=company, user_id=patient.assigned_doctor_id, user__is_active=True, is_active=True,
        clinician_type__in=CompanyMembership.CLINICIAN_TYPES,
    ).exists()
    return patient.assigned_doctor if clinician else None


def request_task_note(kind, description):
    lines = ["The patient's request:", description, '']
    if kind == PatientDataRequest.Kind.CORRECTION:
        lines.append("Height, allergies and medical history are changed in Summary data on the record's Overview.")
    lines.append('Completing this task marks the request Resolved and tells the patient you have marked it complete. '
                 'To give a fuller answer, or to decline, an administrator replies from Data requests instead, '
                 'which also completes this task.')
    return '\n'.join(lines)


def raise_request_task(record):
    task = ClinicalTask(
        company=record.company, patient=record.patient, title=TASK_TITLES[record.kind],
        description=request_task_note(record.kind, record.description),
        assigned_to=request_task_assignee(record.company, record.patient),
    )
    task.full_clean()
    task.save()
    record.task = task
    record.save(update_fields=('task', 'updated_at'))
    return task


def require_privacy_admin(actor, company, *, policies=False):
    require_enabled_company(company)
    if not getattr(actor, 'is_active', False) or not company.is_active or not CompanyMembership.objects.filter(
        user=actor, user__is_active=True, company=company, is_active=True,
        role__in=(CompanyMembership.Role.SUPER_ADMIN,) if policies else ADMIN_ROLES,
    ).exists():
        raise PermissionDenied('You do not have permission to manage privacy records in this practice.')


def own_patient(actor, company, patient):
    require_enabled_company(company)
    if not getattr(actor, 'is_active', False) or not company.is_active or not patient.is_active or patient.company_id != company.pk or patient.user_id != actor.pk or not patient.user.is_active:
        raise PermissionDenied('This patient record is not yours in the selected active practice.')


def submission_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('Reload this form before submitting.') from None


@transaction.atomic
def request_details_change(*, actor, company, patient, description, submission_key, request=None):
    """Ask administrators in-app, falling back to Super Admins; never edit details or send email."""
    from .messaging import add_participant, practice_administrators
    from .notifications import paused

    company = Company.objects.select_for_update().get(pk=company.pk)
    patient = Patient.objects.select_for_update().get(pk=patient.pk)
    own_patient(actor, company, patient)
    key = str(submission_uuid(submission_key))
    description = description.strip() if isinstance(description, str) else ''
    if not 1 <= len(description) <= 5000:
        raise ValidationError('Describe the change in 1 to 5,000 characters.')
    digest = hashlib.sha256(description.encode()).hexdigest()
    previous = AuditEvent.objects.filter(company=company, patient=patient, actor=actor,
        action='patient.details_change_requested', metadata__submission_key=key).first()
    if previous:
        if previous.metadata.get('description_digest') != digest:
            raise ValidationError('This request was already sent. Reload before starting a different request.')
        return MessageThread.objects.get(pk=previous.target_id, company=company, patient=patient), False
    administrators = list(practice_administrators(company))
    if not administrators:
        administrators = [membership.user for membership in CompanyMembership.objects.filter(
            company=company, role=CompanyMembership.Role.SUPER_ADMIN, is_active=True, user__is_active=True,
        ).select_related('user').order_by('user_id')]
    if not administrators:
        raise ValidationError('There is no administrator available to receive this request. Please try again later.')
    with paused():
        thread = MessageThread(company=company, patient=patient, opened_by=actor,
                               subject='Request to change my personal details')
        thread.full_clean()
        thread.save()
        for administrator in administrators:
            add_participant(thread, administrator)
        post_patient_message(thread=thread, sender=actor, body=description, request=request)
    record_audit(company=company, actor=actor, patient=patient, target=thread,
        action='patient.details_change_requested', request=request,
        metadata={'submission_key': key, 'description_digest': digest})
    return thread, True


@transaction.atomic
def withdraw_marketing_preference(*, actor, company, patient, request=None):
    """Withdraw an existing marketing preference without changing care emails or consent history."""
    company = Company.objects.select_for_update().get(pk=company.pk)
    patient = Patient.objects.select_for_update().get(pk=patient.pk)
    own_patient(actor, company, patient)
    preference = PatientCommunicationPreference.objects.select_for_update().filter(company=company, patient=patient).first()
    if preference is None or not preference.marketing_enabled:
        return False
    preference.marketing_enabled = False
    preference.save(update_fields=('marketing_enabled', 'updated_at'))
    record_audit(company=company, actor=actor, patient=patient, target=preference,
                 action='privacy.preference_updated', request=request, metadata={'marketing_enabled': False})
    return True


@transaction.atomic
def create_data_request(*, actor, company, patient, kind, description, submission_key, request=None):
    company = Company.objects.select_for_update().get(pk=company.pk)
    patient = Patient.objects.select_for_update().get(pk=patient.pk)
    own_patient(actor, company, patient)
    key = submission_uuid(submission_key)
    if not isinstance(description, str):
        raise ValidationError('Describe your request in text.')
    description = description.strip()
    if kind not in PatientDataRequest.Kind.values or not 1 <= len(description) <= 5000:
        raise ValidationError('Choose a request type and describe your request in 1 to 5,000 characters.')
    existing = PatientDataRequest.objects.filter(submission_key=key).first()
    if existing:
        if existing.company_id != company.pk or existing.patient_id != patient.pk or existing.kind != kind or existing.description != description:
            raise ValidationError('This submission has already been used. Reload before starting a new request.')
        return existing, False
    record = PatientDataRequest(company=company, patient=patient, kind=kind, description=description, submission_key=key)
    record.full_clean()
    record.save()
    task = raise_request_task(record)
    record_audit(company=company, actor=actor, patient=patient, target=record,
                 action='privacy.request_submitted', request=request, metadata={'kind': kind, 'task_id': task.pk})
    return record, True


@transaction.atomic
def respond_to_data_request(*, actor, company, data_request, body, status, submission_key, expected_updated_at, request=None):
    company = Company.objects.select_for_update().get(pk=company.pk)
    require_privacy_admin(actor, company)
    record = PatientDataRequest.objects.select_for_update().filter(
        pk=data_request.pk, company=company, patient__company=company,
    ).first()
    if record is None:
        raise PermissionDenied('This request does not belong to the selected practice.')
    if not isinstance(body, str):
        raise ValidationError('Add a patient-visible text response.')
    body = body.strip()
    if status not in PatientDataRequest.Status.values or not 1 <= len(body) <= 5000:
        raise ValidationError('Add a patient-visible response and choose the updated status.')
    key = submission_uuid(submission_key)
    existing = PatientDataRequestReply.objects.filter(submission_key=key).first()
    if existing:
        if existing.company_id != company.pk or existing.data_request_id != record.pk or existing.author_id != actor.pk or existing.status != status or existing.body != body:
            raise ValidationError('This response has already been submitted. Reload before replying again.')
        return existing, False
    if record.updated_at.isoformat() != expected_updated_at:
        raise ValidationError('Another administrator updated this request. Reload before responding.')
    reply = PatientDataRequestReply(company=company, data_request=record, author=actor, body=body,
                                    status=status, submission_key=key)
    reply.full_clean()
    reply.save()
    record.status = status
    record.save(update_fields=('status', 'updated_at'))
    record_audit(company=company, actor=actor, patient=record.patient, target=record,
                 action='privacy.request_responded', request=request, metadata={'reply_id': reply.pk, 'status': status})
    if status in CLOSING and record.task_id:
        task = ClinicalTask.objects.select_for_update().get(pk=record.task_id, company=company)
        if task.status in (ClinicalTask.Status.OPEN, ClinicalTask.Status.IN_PROGRESS):
            task.mark_done()
            record_audit(company=company, actor=actor, patient=record.patient, action='task.completed', target=task,
                         request=request, metadata={'data_request_id': record.pk})
    return reply, True


def finish_request_with_task(*, task, actor, request=None):
    """Completing a data request's task, from the task list or the task editor, resolves the request for everyone:
    it leaves the Data requests count and the patient sees it Resolved. Called inside the task's transaction."""
    record = PatientDataRequest.objects.select_for_update().filter(task_id=task.pk, company_id=task.company_id).first()
    if record is None or record.status not in UNRESOLVED:
        return None
    reply = PatientDataRequestReply(company_id=record.company_id, data_request=record, author=actor, body=task_done_reply(actor),
                                    status=PatientDataRequest.Status.RESOLVED, submission_key=uuid.uuid4())
    reply.full_clean()
    reply.save()
    record.status = PatientDataRequest.Status.RESOLVED
    record.save(update_fields=('status', 'updated_at'))
    record_audit(company=record.company, actor=actor, patient=record.patient, target=record,
                 action='privacy.request_responded', request=request,
                 metadata={'reply_id': reply.pk, 'status': record.status, 'task_id': task.pk})
    return record


@transaction.atomic
def publish_policy_version(*, actor, company, kind, version, title, body, effective_from, confirmed, request=None):
    company = Company.objects.select_for_update().get(pk=company.pk)
    require_privacy_admin(actor, company, policies=True)
    if confirmed is not True:
        raise ValidationError('Confirm this version is approved for publication.')
    if not all(isinstance(value, str) for value in (body, version, title)):
        raise ValidationError('Supply a version, title and approved document text.')
    body, version, title = body.strip(), version.strip(), title.strip()
    if kind not in ConsentDocument.Kind.values or not version or not title or not body or len(body) > 100000:
        raise ValidationError('Supply a type, unique version, title and approved document text.')
    digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
    existing = ConsentDocument.objects.filter(company=company, kind=kind, version=version).first()
    if existing:
        if existing.body == body and existing.title == title and existing.effective_from == effective_from and existing.is_active:
            return existing, False
        raise ValidationError('This version already exists. Publish a new version; historical documents cannot be overwritten.')
    document = ConsentDocument(company=company, kind=kind, version=version, title=title, body=body,
                               content_hash=digest, effective_from=effective_from, is_active=True)
    document.full_clean()
    document.save()
    record_audit(company=company, actor=actor, target=document, action='privacy.policy_published', request=request,
                 metadata={'kind': kind, 'document_id': document.pk})
    return document, True
