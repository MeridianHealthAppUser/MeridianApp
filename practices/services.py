from django.conf import settings
from django.core.exceptions import PermissionDenied

from .models import Company, CompanyMembership, Patient
from .tenancy import company_is_enabled, enabled_companies, require_multi_practice


ACTIVE_COMPANY_SESSION_KEY = 'active_company_id'
ACTIVE_PATIENT_COMPANY_SESSION_KEY = 'active_patient_company_id'
_UNRESOLVED = object()


def _read_context(request):
    """Reuse access resolution within one read, never across requests or writes.

    Page rendering resolves the same identity in its access mixin and template
    context. Mutating requests must reload permissions after any role changes.
    Session selections remain part of individual keys so a changed selection
    cannot reuse the previous practice's resolution.
    """
    if request.method not in ('GET', 'HEAD'):
        return {}
    identity = (request.user.pk, settings.MULTI_PRACTICE_ENABLED, settings.SINGLE_PRACTICE_SLUG)
    context = getattr(request, '_practice_read_context', None)
    if context is None or context[0] != identity:
        context = request._practice_read_context = (identity, {})
    return context[1]


def _clear_read_context(request):
    request.__dict__.pop('_practice_read_context', None)


def available_companies_for(user):
    """Return companies the user can currently enter, including their role."""
    if not user.is_authenticated:
        return Company.objects.none()
    return enabled_companies().filter(
        is_active=True,
        memberships__user=user,
        memberships__is_active=True,
    ).distinct().order_by('name')


def get_active_company(request):
    """Resolve a safe active company from the session, falling back to the first one."""
    context = _read_context(request)
    company_id = request.session.get(ACTIVE_COMPANY_SESSION_KEY)
    cached = context.get(('company', company_id), _UNRESOLVED)
    if cached is not _UNRESOLVED:
        return cached
    companies = available_companies_for(request.user)
    company = companies.filter(pk=company_id).first() if company_id else None
    if company is None:
        company = companies.first()
        if company is not None:
            request.session[ACTIVE_COMPANY_SESSION_KEY] = company.pk
        else:
            request.session.pop(ACTIVE_COMPANY_SESSION_KEY, None)
    context[('company', request.session.get(ACTIVE_COMPANY_SESSION_KEY))] = company
    return company


def set_active_company(request, company):
    """Switch context only when the signed-in user has an active membership."""
    require_multi_practice()
    if not available_companies_for(request.user).filter(pk=company.pk).exists():
        raise PermissionDenied('You do not have access to this company.')
    request.session[ACTIVE_COMPANY_SESSION_KEY] = company.pk
    _clear_read_context(request)
    return company


def active_membership_for(request, company=None):
    company = company or get_active_company(request)
    context = _read_context(request)
    key = ('membership', company.pk if company is not None else None)
    if key in context:
        return context[key]
    if not company_is_enabled(company):
        return None
    membership = CompanyMembership.objects.filter(
        user=request.user,
        company=company,
        is_active=True,
    ).first()
    context[key] = membership
    return membership


def available_patient_companies_for(user):
    """Practices where this person has a patient record, separate from staff access."""
    if not user.is_authenticated:
        return Company.objects.none()
    return enabled_companies().filter(
        is_active=True,
        practices_patient_records__user=user,
        practices_patient_records__is_active=True,
    ).distinct().order_by('name')


def get_active_patient_company(request):
    """Resolve an owned patient-practice context without granting staff access."""
    context = _read_context(request)
    company_id = request.session.get(ACTIVE_PATIENT_COMPANY_SESSION_KEY)
    cached = context.get(('patient_company', company_id), _UNRESOLVED)
    if cached is not _UNRESOLVED:
        return cached
    companies = available_patient_companies_for(request.user)
    company = companies.filter(pk=company_id).first() if company_id else None
    if company is None:
        company = companies.first()
        if company is not None:
            request.session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = company.pk
        else:
            request.session.pop(ACTIVE_PATIENT_COMPANY_SESSION_KEY, None)
    context[('patient_company', request.session.get(ACTIVE_PATIENT_COMPANY_SESSION_KEY))] = company
    return company


def set_active_patient_company(request, company):
    """Switch only to a practice that owns a patient record for this user."""
    require_multi_practice()
    if not available_patient_companies_for(request.user).filter(pk=company.pk).exists():
        raise PermissionDenied('You do not have a patient record at this practice.')
    request.session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = company.pk
    _clear_read_context(request)
    return company


def active_patient_for(request, company=None):
    company = company or get_active_patient_company(request)
    context = _read_context(request)
    key = ('patient', company.pk if company is not None else None)
    if key in context:
        return context[key]
    if not company_is_enabled(company):
        return None
    patient = Patient.objects.filter(company=company, user=request.user, is_active=True).first()
    context[key] = patient
    return patient
