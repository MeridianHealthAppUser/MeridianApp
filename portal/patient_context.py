"""Bind patient forms to the practice and record shown when they were opened."""

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError


PATIENT_CONTEXT_SALT = 'portal.patient-form-context.v1'
PATIENT_CONTEXT_MAX_AGE = 12 * 60 * 60
PATIENT_CONTEXT_ERROR = (
    'The practice for this form has changed or the form has expired. '
    'No changes were saved. Check the selected practice before trying again.'
)


def _payload(request, company, patient):
    # The signature detects stale tabs; it never replaces the view's ownership
    # and active-practice checks or the browser's separate CSRF protection.
    if patient.user_id != request.user.pk or patient.company_id != company.pk:
        raise PermissionDenied('This patient form does not belong to your selected practice.')
    return {'user_id': request.user.pk, 'company_id': company.pk, 'patient_id': patient.pk}


def make_patient_context(request, company, patient):
    return signing.dumps(_payload(request, company, patient), salt=PATIENT_CONTEXT_SALT)


def validate_patient_context(request, company, patient):
    """Reject expired, missing, tampered or no-longer-selected form contexts."""
    token = request.POST.get('patient_context', '')
    if not token or len(token) > 2048:
        raise ValidationError(PATIENT_CONTEXT_ERROR)
    try:
        payload = signing.loads(token, salt=PATIENT_CONTEXT_SALT, max_age=PATIENT_CONTEXT_MAX_AGE)
    except (signing.BadSignature, ValueError, TypeError):
        raise ValidationError(PATIENT_CONTEXT_ERROR) from None
    if payload != _payload(request, company, patient):
        raise ValidationError(PATIENT_CONTEXT_ERROR)
