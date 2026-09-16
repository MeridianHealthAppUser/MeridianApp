"""Explicit booking and attendance actions; no automatic clinical conclusions."""

from datetime import datetime, timedelta
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient
from practices.tenancy import require_enabled_company
from .models import Appointment, AppointmentProposal, AvailabilitySlot, ClinicalEncounter, PatientEvent
from .scheduling import ensure_clinician_available, ensure_patient_available
from .services import record_audit


def _membership(company, actor):
    require_enabled_company(company)
    return CompanyMembership.objects.filter(company=company, company__is_active=True, user=actor, user__is_active=True, is_active=True).first()


@transaction.atomic
def book_staff_appointment(*, company, patient, actor, clinician, starts_at, duration_minutes, appointment_type, video_link='', request=None):
    patient_user_id = Patient.objects.values_list('user_id', flat=True).get(pk=patient.pk)
    user_ids = {actor.pk, clinician.pk}
    if patient_user_id:
        user_ids.add(patient_user_id)
    locked = {user.pk: user for user in get_user_model().objects.select_for_update().filter(pk__in=user_ids).order_by('pk')}
    actor, clinician = locked.get(actor.pk), locked.get(clinician.pk)
    company = Company.objects.get(pk=company.pk)
    patient = Patient.objects.select_for_update().get(pk=patient.pk)
    if patient.user_id != patient_user_id:
        raise ValidationError('The patient identity changed. Reload before booking.')
    if actor is None or _membership(company, actor) is None:
        raise PermissionDenied('An active staff membership is required.')
    if not patient.is_active or patient.company_id != company.pk:
        raise ValidationError('Choose an active patient record in this practice.')
    if clinician is None or not clinician.is_active or not CompanyMembership.objects.filter(company=company, user=clinician, role='doctor', is_active=True).exists():
        raise ValidationError('Choose an active doctor in this practice.')
    if not isinstance(starts_at, datetime) or timezone.is_naive(starts_at) or starts_at <= timezone.now():
        raise ValidationError('Choose a future appointment time.')
    if type(duration_minutes) is not int or not 5 <= duration_minutes <= 120 or appointment_type not in Appointment.Type.values:
        raise ValidationError('Choose a valid appointment type and duration of 5–120 minutes.')
    if video_link:
        try:
            if not isinstance(video_link, str) or urlsplit(video_link).scheme != 'https':
                raise ValueError
        except ValueError:
            raise ValidationError('Use an HTTPS video link.') from None
    existing = Appointment.objects.for_company(company).filter(patient=patient, clinician=clinician, starts_at=starts_at,
        duration_minutes=duration_minutes, appointment_type=appointment_type, status='booked').first()
    if existing:
        return existing
    ensure_clinician_available(company=company, clinician=clinician, starts_at=starts_at, duration_minutes=duration_minutes)
    ensure_patient_available(patient=patient, starts_at=starts_at, duration_minutes=duration_minutes)
    appointment = Appointment(company=company, patient=patient, clinician=clinician, starts_at=starts_at,
                              duration_minutes=duration_minutes, appointment_type=appointment_type, video_link=video_link)
    appointment.full_clean()
    appointment.save()
    AvailabilitySlot.objects.for_company(company).filter(clinician=clinician, starts_at=starts_at, ends_at=starts_at + timedelta(minutes=duration_minutes), is_booked=False, appointment__isnull=True).update(is_booked=True, appointment=appointment, updated_at=timezone.now())
    PatientEvent.objects.create(company=company, patient=patient, category='appointment', title='Appointment booked',
        detail=f'{appointment.get_appointment_type_display()} booked for {timezone.localtime(starts_at):%d %B %Y at %H:%M} SAST.',
        source_type=appointment._meta.label_lower, source_id=str(appointment.pk))
    record_audit(company=company, actor=actor, patient=patient, action='appointment.created', target=appointment, request=request)
    return appointment


@transaction.atomic
def change_appointment_status(*, appointment, actor, status, expected_updated, confirm=False, reason='', patient_portal=False, request=None):
    # Use the same ordered identity locks as booking/proposal services.
    original = Appointment.objects.select_related('patient').get(pk=appointment.pk, company_id=appointment.company_id)
    user_ids = {actor.pk, original.clinician_id}
    if original.patient.user_id:
        user_ids.add(original.patient.user_id)
    users = {user.pk: user for user in get_user_model().objects.select_for_update().filter(pk__in=user_ids).order_by('pk')}
    actor = users.get(actor.pk)
    appointment = Appointment.objects.select_for_update().select_related('company', 'patient').get(pk=original.pk)
    require_enabled_company(appointment.company)
    if (appointment.clinician_id != original.clinician_id or appointment.patient_id != original.patient_id
            or appointment.patient.user_id != original.patient.user_id):
        raise ValidationError('Appointment participants changed. Reload before updating.')
    if actor is None or not actor.is_active or not appointment.company.is_active or not appointment.patient.is_active or appointment.patient.company_id != appointment.company_id:
        raise PermissionDenied('This appointment is not available in the selected practice.')
    if patient_portal:
        if actor.pk != appointment.patient.user_id or status != 'cancelled':
            raise PermissionDenied('Patients may only cancel their own upcoming booking.')
    else:
        membership = _membership(appointment.company, actor)
        if membership is None:
            raise PermissionDenied('An active practice membership is required.')
        if status in ('completed', 'no_show') and (membership.role != 'doctor' or appointment.clinician_id != actor.pk):
            raise PermissionDenied('Only the booked doctor may record attendance.')
        if membership.role == 'doctor' and appointment.clinician_id != actor.pk:
            raise PermissionDenied('Only the booked doctor can change this appointment.')
    if status not in ('cancelled', 'completed', 'no_show') or confirm is not True:
        raise ValidationError('Choose and confirm an appointment status.')
    if appointment.status == status:
        return appointment
    if appointment.status != 'booked' or appointment.updated_at.isoformat() != expected_updated:
        raise ValidationError('Only an unchanged booked appointment may be updated. Rebooking is a separate agreed proposal.')
    if status in ('completed', 'no_show') and appointment.starts_at + timedelta(minutes=appointment.duration_minutes) > timezone.now():
        raise ValidationError('Attendance can only be recorded after the appointment has ended.')
    if patient_portal and appointment.starts_at <= timezone.now():
        raise ValidationError('Contact the practice about a past appointment.')
    if status != 'completed' and ClinicalEncounter.objects.for_company(appointment.company).filter(appointment=appointment, status='signed').exists():
        raise ValidationError('This appointment has a signed clinical encounter and cannot be cancelled or marked missed.')
    reason = reason.strip() if isinstance(reason, str) else ''
    if len(reason) > 500 or (status == 'cancelled' and not reason):
        raise ValidationError('Give a cancellation reason, up to 500 characters, that the patient can see.')
    appointment.status = status
    appointment.save(update_fields=('status', 'updated_at'))
    AppointmentProposal.objects.for_company(appointment.company).filter(appointment=appointment, status='pending').update(status='superseded', responded_by=actor, responded_at=timezone.now(), updated_at=timezone.now())
    if status in ('cancelled', 'no_show'):
        AvailabilitySlot.objects.for_company(appointment.company).filter(appointment=appointment).update(appointment=None, is_booked=False, updated_at=timezone.now())
    PatientEvent.objects.create(company=appointment.company, patient=appointment.patient, category='appointment',
        title=f'Appointment {appointment.get_status_display().lower()}',
        detail=f'{timezone.localtime(appointment.starts_at):%d %B %Y at %H:%M} SAST.' + (f' {reason}' if reason else ''),
        source_type=appointment._meta.label_lower, source_id=str(appointment.pk))
    record_audit(company=appointment.company, actor=actor, patient=appointment.patient, action='appointment.status_changed', target=appointment, request=request, metadata={'from': 'booked', 'to': status})
    return appointment
