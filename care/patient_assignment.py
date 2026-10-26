"""The clinician responsible for a patient: their Prescribing doctor.

A Super Admin chooses it on Edit profile (care.patient_details), and writing a prescription makes the prescriber it
(care.prescriptions). Only the pointer on the patient changes: appointments, notes, consultations, results
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


ASSIGNING_ROLES = (CompanyMembership.Role.PRACTICE_ADMIN, CompanyMembership.Role.SUPER_ADMIN)
NOT_ASSIGNING = 'Only a practice administrator or Super Admin can change the Prescribing doctor.'


def active_doctors(company):
    return get_user_model().objects.filter(
        is_active=True, company_memberships__company=company, company_memberships__is_active=True,
        company_memberships__clinician_type__in=CompanyMembership.CLINICIAN_TYPES,
    ).distinct().order_by('first_name', 'last_name', 'email')


def record_clinician_history(patient, clinician, previous_id=None):
    """Close the current assignment and open the new one. Past clinicians stay reachable by message and can
    still read the record."""
    now = timezone.now()
    ClinicianAssignment.objects.filter(patient=patient, ended_at__isnull=True).update(ended_at=now, updated_at=now)
    if previous_id and not ClinicianAssignment.objects.filter(patient=patient, clinician_id=previous_id).exists():
        # Assigned without a history row (an older or imported record): keep them as a past clinician too.
        ClinicianAssignment.objects.create(company_id=patient.company_id, patient=patient, clinician_id=previous_id,
                                           ended_at=now)
    if clinician is not None:
        ClinicianAssignment.objects.create(company_id=patient.company_id, patient=patient, clinician=clinician)
        from .messaging import adopt_unattended_threads
        adopt_unattended_threads(patient, clinician)


@transaction.atomic
def assign_doctor(*, patient, actor, doctor, expected_updated, request=None):
    """Set or clear the Prescribing doctor; they must be an active clinician in the patient's practice."""
    company = Company.objects.select_for_update().get(pk=patient.company_id)
    require_enabled_company(company)
    if not CompanyMembership.objects.filter(company=company, user=actor, user__is_active=True,
                                            is_active=True, role__in=ASSIGNING_ROLES).exists():
        raise PermissionDenied(NOT_ASSIGNING)
    patient = Patient.objects.select_for_update().for_company(company).get(pk=patient.pk, is_active=True)
    if patient.updated_at.isoformat() != expected_updated:
        raise ValidationError('This patient record changed in another tab. Reload before changing the Prescribing doctor.')
    if doctor is not None and not active_doctors(company).filter(pk=doctor.pk).exists():
        raise ValidationError('Select an active clinician in this practice.')
    previous_id = patient.assigned_doctor_id
    if previous_id == (doctor.pk if doctor else None):
        return patient
    patient.assigned_doctor = doctor
    patient.save(update_fields=('assigned_doctor', 'updated_at'))
    record_clinician_history(patient, doctor, previous_id)
    record_audit(company=company, actor=actor, patient=patient, action='patient.doctor_assigned', target=patient,
                 request=request, metadata={'previous_doctor_id': previous_id, 'doctor_id': patient.assigned_doctor_id})
    return patient


def make_prescribing_doctor(*, company, patient, doctor, request=None):
    """The doctor who wrote the latest prescription becomes the patient's Prescribing doctor. The caller has
    locked the patient (care.prescriptions does, in the prescription's transaction)."""
    previous_id = patient.assigned_doctor_id
    if previous_id == doctor.pk:
        return
    patient.assigned_doctor = doctor
    patient.save(update_fields=('assigned_doctor', 'updated_at'))
    record_clinician_history(patient, doctor, previous_id)
    record_audit(company=company, actor=doctor, patient=patient, action='patient.doctor_assigned', target=patient,
                 request=request, metadata={'previous_doctor_id': previous_id, 'doctor_id': doctor.pk, 'by_prescription': True})
