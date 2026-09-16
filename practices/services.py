from django.core.exceptions import PermissionDenied

from .models import Company, CompanyMembership, Patient
from .tenancy import company_is_enabled, enabled_companies, require_multi_practice


ACTIVE_COMPANY_SESSION_KEY = 'active_company_id'
ACTIVE_PATIENT_COMPANY_SESSION_KEY = 'active_patient_company_id'


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
    companies = available_companies_for(request.user)
    company_id = request.session.get(ACTIVE_COMPANY_SESSION_KEY)
    company = companies.filter(pk=company_id).first() if company_id else None
    if company is None:
        company = companies.first()
        if company is not None:
            request.session[ACTIVE_COMPANY_SESSION_KEY] = company.pk
        else:
            request.session.pop(ACTIVE_COMPANY_SESSION_KEY, None)
    return company


def set_active_company(request, company):
    """Switch context only when the signed-in user has an active membership."""
    require_multi_practice()
    if not available_companies_for(request.user).filter(pk=company.pk).exists():
        raise PermissionDenied('You do not have access to this company.')
    request.session[ACTIVE_COMPANY_SESSION_KEY] = company.pk
    return company


def active_membership_for(request, company=None):
    company = company or get_active_company(request)
    if not company_is_enabled(company):
        return None
    return CompanyMembership.objects.filter(
        user=request.user,
        company=company,
        is_active=True,
    ).first()


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
    companies = available_patient_companies_for(request.user)
    company_id = request.session.get(ACTIVE_PATIENT_COMPANY_SESSION_KEY)
    company = companies.filter(pk=company_id).first() if company_id else None
    if company is None:
        company = companies.first()
        if company is not None:
            request.session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = company.pk
        else:
            request.session.pop(ACTIVE_PATIENT_COMPANY_SESSION_KEY, None)
    return company


def set_active_patient_company(request, company):
    """Switch only to a practice that owns a patient record for this user."""
    require_multi_practice()
    if not available_patient_companies_for(request.user).filter(pk=company.pk).exists():
        raise PermissionDenied('You do not have a patient record at this practice.')
    request.session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = company.pk
    return company


def active_patient_for(request, company=None):
    company = company or get_active_patient_company(request)
    if not company_is_enabled(company):
        return None
    return Patient.objects.filter(company=company, user=request.user, is_active=True).first()
