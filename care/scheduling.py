"""Whether a clinician and a patient are free for an appointment. The practice moves appointments; patients ask
support for a new time."""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db.models import DateTimeField, DurationField, ExpressionWrapper, F, IntegerField, Value
from django.db.models.functions import Cast

from .models import Appointment


def ensure_clinician_available(*, clinician, starts_at, duration_minutes, exclude_appointment=None, company=None):
    """Check actual occupied intervals across practices without disclosing them.

    Callers that write an appointment must hold the clinician User row lock for
    the duration of their transaction.
    """
    from .availability import ensure_working_time

    ensure_working_time(
        company=company or getattr(exclude_appointment, 'company', None),
        clinician=clinician, starts_at=starts_at, duration_minutes=duration_minutes,
    )
    ends_at = starts_at + timedelta(minutes=duration_minutes)
    duration = ExpressionWrapper(
        Cast('duration_minutes', IntegerField()) * Value(timedelta(minutes=1)),
        output_field=DurationField(),
    )
    occupied = Appointment.objects.filter(clinician=clinician, starts_at__lt=ends_at).exclude(
        status__in=(Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW),
    )
    if exclude_appointment is not None:
        occupied = occupied.exclude(pk=getattr(exclude_appointment, 'pk', exclude_appointment))
    occupied = occupied.annotate(
        ends_at=ExpressionWrapper(F('starts_at') + duration, output_field=DateTimeField()),
    ).filter(ends_at__gt=starts_at)
    if occupied.exists():
        raise ValidationError(
            'The clinician is no longer available at this time. Please choose another time.',
            code='appointment_overlap',
        )


def ensure_patient_available(*, patient, starts_at, duration_minutes, exclude_appointment=None):
    """An identity cannot be booked with two doctors at once, across practices."""
    ends_at = starts_at + timedelta(minutes=duration_minutes)
    duration = ExpressionWrapper(Cast('duration_minutes', IntegerField()) * Value(timedelta(minutes=1)), output_field=DurationField())
    occupied = Appointment.objects.filter(starts_at__lt=ends_at).exclude(status__in=('cancelled', 'no_show'))
    occupied = occupied.filter(patient__user_id=patient.user_id) if patient.user_id else occupied.filter(patient=patient)
    if exclude_appointment is not None:
        occupied = occupied.exclude(pk=getattr(exclude_appointment, 'pk', exclude_appointment))
    if occupied.annotate(ends_at=ExpressionWrapper(F('starts_at') + duration, output_field=DateTimeField())).filter(ends_at__gt=starts_at).exists():
        raise ValidationError('The patient already has an appointment that overlaps this time. Choose another time.')
