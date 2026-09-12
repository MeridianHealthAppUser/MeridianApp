"""Patient self-service writes, always revalidated under the relevant locks."""

from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import DateTimeField, DurationField, ExpressionWrapper, F, IntegerField, Value
from django.db.models.functions import Cast
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient
from .availability import SAST, open_slots_for_day
from .models import Appointment, AvailabilitySlot, PatientEvent
from .scheduling import ensure_clinician_available
from .services import record_audit


def _own_active_patient(company, patient, actor):
    if not getattr(actor, 'is_active', False) or not company.is_active or not patient.is_active or patient.user_id != actor.pk or patient.company_id != company.pk:
        raise PermissionDenied('You need an active patient record in the selected practice.')


def patient_busy_intervals(patient, day):
    """The patient's bookings across their own records, never another identity."""
    start = datetime.combine(day, time.min, tzinfo=SAST)
    end = start + timedelta(days=1)
    duration = ExpressionWrapper(Cast('duration_minutes', IntegerField()) * Value(timedelta(minutes=1)), output_field=DurationField())
    return Appointment.objects.filter(patient__user_id=patient.user_id, starts_at__lt=end).exclude(
        status__in=(Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW),
    ).annotate(patient_booking_end=ExpressionWrapper(F('starts_at') + duration, output_field=DateTimeField())).filter(
        patient_booking_end__gt=start,
    ).values_list('starts_at', 'patient_booking_end')


@transaction.atomic
def book_patient_appointment(*, company, patient, actor, clinician_id, starts_at, appointment_type, request=None):
    # All patient self-bookings acquire both identity locks in a stable order;
    # clinician availability edits and existing booking services share the
    # clinician lock. This also serialises one patient's bookings with two doctors.
    if type(clinician_id) is not int or clinician_id <= 0:
        raise ValidationError('Choose an available doctor.')
    locked = {user.pk: user for user in get_user_model().objects.select_for_update().filter(
        pk__in=sorted({actor.pk, clinician_id}),
    ).order_by('pk')}
    actor = locked.get(actor.pk)
    clinician = locked.get(clinician_id)
    company = Company.objects.get(pk=company.pk)
    patient = Patient.objects.select_for_update().get(pk=patient.pk)
    if actor is None:
        raise PermissionDenied('Your account is no longer active.')
    _own_active_patient(company, patient, actor)
    if clinician is None or not clinician.is_active or not CompanyMembership.objects.filter(
        user=clinician, company=company, is_active=True, role=CompanyMembership.Role.DOCTOR,
    ).exists():
        raise ValidationError('This doctor is no longer available in this practice.')
    if appointment_type not in (Appointment.Type.REVIEW, Appointment.Type.FOLLOW_UP, Appointment.Type.AD_HOC):
        raise ValidationError('Choose a review, follow-up or ad-hoc appointment.')
    if not isinstance(starts_at, datetime) or timezone.is_naive(starts_at) or starts_at <= timezone.now():
        raise ValidationError('Choose a future appointment time.')
    if timezone.localdate(starts_at, SAST) > timezone.localdate(timezone.now(), SAST) + timedelta(days=90):
        raise ValidationError('Choose an appointment within the next 90 days.')
    # A duplicate browser submission returns the existing booking without adding
    # another appointment, event or audit row.
    existing = Appointment.objects.for_company(company).filter(
        patient=patient, clinician=clinician, starts_at=starts_at, appointment_type=appointment_type,
        duration_minutes=15, status=Appointment.Status.BOOKED,
    ).first()
    if existing:
        return existing, False
    day = timezone.localdate(starts_at, SAST)
    slots = open_slots_for_day(company=company, clinicians=[clinician], day=day, duration_minutes=15, limit=1000)
    slot = next((slot for slot in slots if slot.starts_at == starts_at), None)
    if slot is None:
        raise ValidationError('This time is no longer available. Choose another time.')
    ensure_clinician_available(company=company, clinician=clinician, starts_at=starts_at, duration_minutes=15)
    ends_at = starts_at + timedelta(minutes=15)
    if any(starts_at < finish and ends_at > begin for begin, finish in patient_busy_intervals(patient, day)):
        raise ValidationError('You already have an appointment that overlaps this time. Choose another time.')
    appointment = Appointment(company=company, patient=patient, clinician=clinician,
                              starts_at=starts_at, duration_minutes=15, appointment_type=appointment_type)
    appointment.full_clean()
    appointment.save()
    if slot.pk is not None:
        AvailabilitySlot.objects.filter(pk=slot.pk, company=company, is_booked=False, appointment__isnull=True).update(
            is_booked=True, appointment=appointment, updated_at=timezone.now(),
        )
    PatientEvent.objects.create(
        company=company, patient=patient, category=PatientEvent.Category.APPOINTMENT,
        title='Appointment booked',
        detail=f'{appointment.get_appointment_type_display()} with {clinician.full_name} on {timezone.localtime(starts_at, SAST):%d %B %Y at %H:%M} SAST.',
        source_type=appointment._meta.label_lower, source_id=str(appointment.pk), is_patient_visible=True,
    )
    record_audit(company=company, actor=actor, patient=patient, action='appointment.patient_booked', target=appointment, request=request)
    return appointment, True


@transaction.atomic
def save_patient_medical_profile(*, company, patient, actor, answers, expected_revision, request=None):
    from .models import PatientMedicalProfile, PatientMedicalProfileRevision

    company = Company.objects.select_for_update().get(pk=company.pk)
    patient = Patient.objects.select_for_update(of=('self',)).select_related('user').get(pk=patient.pk)
    _own_active_patient(company, patient, patient.user)
    if actor.pk != patient.user_id or not actor.is_active:
        raise PermissionDenied('This is not your medical profile.')
    profile = PatientMedicalProfile.objects.select_for_update().for_company(company).filter(patient=patient).first()
    revision = profile.revision if profile else None
    if expected_revision != revision:
        raise ValidationError('Your medical profile changed in another tab. No answers were overwritten. Reload before editing again.')
    if not isinstance(answers, dict) or any(not isinstance(value, str) or len(value) > 2000 for value in answers.values()):
        raise ValidationError('Keep each answer to 2,000 characters or fewer.')
    if (profile and profile.answers == answers) or (profile is None and not any(answers.values())):
        return profile, False
    if profile is None:
        profile = PatientMedicalProfile(company=company, patient=patient)
    profile.answers = answers
    profile.revision += 1
    profile.saved_by = actor
    profile.full_clean()
    profile.save()
    snapshot = PatientMedicalProfileRevision(
        company=company, patient=patient, profile=profile, revision=profile.revision, answers=dict(answers), saved_by=actor,
    )
    snapshot.full_clean()
    snapshot.save()
    record_audit(company=company, actor=actor, patient=patient, action='patient.medical_profile_updated',
                 target=profile, request=request, metadata={'revision': profile.revision, 'snapshot_id': snapshot.pk})
    return profile, True
