"""Explicit privacy workflow writes; never delete health records or send mail."""

import hashlib
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from practices.models import Company, CompanyMembership, Patient
from .models import ConsentDocument, PatientCommunicationPreference, PatientDataRequest, PatientDataRequestReply
from .services import record_audit


ADMIN_ROLES = (CompanyMembership.Role.PRACTICE_ADMIN, CompanyMembership.Role.SUPER_ADMIN)


def require_privacy_admin(actor, company, *, policies=False):
    if not getattr(actor, 'is_active', False) or not company.is_active or not CompanyMembership.objects.filter(
        user=actor, user__is_active=True, company=company, is_active=True,
        role__in=(CompanyMembership.Role.SUPER_ADMIN,) if policies else ADMIN_ROLES,
    ).exists():
        raise PermissionDenied('You do not have permission to manage privacy records in this practice.')


def own_patient(actor, company, patient):
    if not getattr(actor, 'is_active', False) or not company.is_active or not patient.is_active or patient.company_id != company.pk or patient.user_id != actor.pk or not patient.user.is_active:
        raise PermissionDenied('This patient record is not yours in the selected active practice.')


def submission_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('Reload this form before submitting.') from None


@transaction.atomic
def save_communication_preference(*, actor, company, patient, enabled, expected_updated_at, request=None):
    company = Company.objects.select_for_update().get(pk=company.pk)
    patient = Patient.objects.select_for_update().get(pk=patient.pk)
    own_patient(actor, company, patient)
    if type(enabled) is not bool:
        raise ValidationError('Choose whether to receive marketing communications.')
    preference = PatientCommunicationPreference.objects.select_for_update().filter(company=company, patient=patient).first()
    if preference and preference.marketing_enabled == enabled:
        return preference, False
    current_version = preference.updated_at.isoformat() if preference else None
    if current_version != expected_updated_at:
        raise ValidationError('Your preferences changed in another tab. Reload before saving.')
    if preference is None and not enabled:
        return None, False
    if preference is None:
        preference = PatientCommunicationPreference(company=company, patient=patient)
    preference.marketing_enabled = enabled
    preference.full_clean()
    preference.save()
    record_audit(company=company, actor=actor, patient=patient, target=preference,
                 action='privacy.preference_updated', request=request, metadata={'marketing_enabled': enabled})
    return preference, True


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
    record_audit(company=company, actor=actor, patient=patient, target=record,
                 action='privacy.request_submitted', request=request, metadata={'kind': kind})
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
    return reply, True


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
