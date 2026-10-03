"""The care team chooses the clinician responsible for a patient.

Only the pointer on the patient changes: appointments, notes, consultations, results
and tasks keep their original author or clinician, so nothing is moved or lost.
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient
from practices.tenancy import require_enabled_company
from .models import ClinicianAssignment
from .services import record_audit


ASSIGNING_ROLES = (CompanyMembership.Role.DOCTOR, CompanyMembership.Role.PRACTICE_ADMIN, CompanyMembership.Role.SUPER_ADMIN)


def can_assign_doctor(membership):
    return membership is not None and membership.role in ASSIGNING_ROLES


def active_doctors(company):
    return get_user_model().objects.filter(
        is_active=True, company_memberships__company=company, company_memberships__is_active=True,
        company_memberships__clinician_type__in=CompanyMembership.CLINICIAN_TYPES,
    ).distinct().order_by('first_name', 'last_name', 'email')


def record_clinician_history(patient, clinician):
    """Close the current assignment and open the new one; past clinicians stay reachable by message."""
    now = timezone.now()
    ClinicianAssignment.objects.filter(patient=patient, ended_at__isnull=True).update(ended_at=now, updated_at=now)
    if clinician is not None:
        ClinicianAssignment.objects.create(company_id=patient.company_id, patient=patient, clinician=clinician)
        from .messaging import adopt_unattended_threads
        adopt_unattended_threads(patient, clinician)


@transaction.atomic
def assign_doctor(*, patient, actor, doctor, expected_updated, request=None):
    """Set or clear the assigned clinician; they must be active in the patient's practice."""
    company = Company.objects.select_for_update().get(pk=patient.company_id)
    require_enabled_company(company)
    if not CompanyMembership.objects.filter(company=company, user=actor, user__is_active=True,
                                            is_active=True, role__in=ASSIGNING_ROLES).exists():
        raise PermissionDenied('Only an active member of the care team can change the assigned clinician.')
    patient = Patient.objects.select_for_update().for_company(company).get(pk=patient.pk, is_active=True)
    if patient.updated_at.isoformat() != expected_updated:
        raise ValidationError('This patient record changed in another tab. Reload before changing the assigned clinician.')
    if doctor is not None and not active_doctors(company).filter(pk=doctor.pk).exists():
        raise ValidationError('Select an active clinician in this practice.')
    previous_id = patient.assigned_doctor_id
    if previous_id == (doctor.pk if doctor else None):
        return patient
    patient.assigned_doctor = doctor
    patient.save(update_fields=('assigned_doctor', 'updated_at'))
    record_clinician_history(patient, doctor)
    record_audit(company=company, actor=actor, patient=patient, action='patient.doctor_assigned', target=patient,
                 request=request, metadata={'previous_doctor_id': previous_id, 'doctor_id': patient.assigned_doctor_id})
    return patient
