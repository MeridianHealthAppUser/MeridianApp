from .services import (
    active_membership_for,
    active_patient_for,
    available_companies_for,
    available_patient_companies_for,
    get_active_company,
    get_active_patient_company,
)


def active_company(request):
    if not request.user.is_authenticated:
        return {}
    company = get_active_company(request)
    patient_company = get_active_patient_company(request)
    return {
        'active_company': company,
        'active_membership': active_membership_for(request, company),
        'available_companies': available_companies_for(request.user),
        'active_patient_company': patient_company,
        'active_patient': active_patient_for(request, patient_company),
        'available_patient_companies': available_patient_companies_for(request.user),
    }
