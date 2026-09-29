from .services import (
    active_membership_for,
    active_patient_for,
    available_companies_for,
    available_patient_companies_for,
    get_active_company,
    get_active_patient_company,
)
from .tenancy import multi_practice_enabled
from .models import CompanyMembership
from .role_switching import can_switch_practice_role


def active_company(request):
    context = {'multi_practice_enabled': multi_practice_enabled()}
    if not request.user.is_authenticated:
        return context
    company = get_active_company(request)
    membership = active_membership_for(request, company)
    patient_company = get_active_patient_company(request)
    return {
        **context,
        'active_company': company,
        'active_membership': membership,
        'can_switch_practice_role': can_switch_practice_role(request.user, membership, company),
        'practice_role_choices': CompanyMembership.Role.choices,
        'available_companies': available_companies_for(request.user),
        'active_patient_company': patient_company,
        'active_patient': active_patient_for(request, patient_company),
        'available_patient_companies': available_patient_companies_for(request.user),
    }
