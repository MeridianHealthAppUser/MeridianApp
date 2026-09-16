"""Standing hours and private time off; availability is calculated, never booked."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import DateTimeField, DurationField, Exists, ExpressionWrapper, F, IntegerField, OuterRef, Q, Value
from django.db.models.functions import Cast, ExtractIsoWeekDay, TruncDate, TruncTime
from django.utils import timezone

from practices.models import CompanyMembership
from practices.tenancy import require_enabled_company

from .models import Appointment, AvailabilitySlot, DoctorTimeOff, DoctorWorkingPattern
from .services import record_audit


SAST = ZoneInfo('Africa/Johannesburg')
UNAVAILABLE = 'The clinician is unavailable at this time. Please choose another time.'


@dataclass(frozen=True)
class OpenTimeSlot:
    clinician: object
    starts_at: datetime
    ends_at: datetime
    pk: None = None

    @property
    def clinician_id(self):
        return self.clinician.pk


def _lock_own_doctor(company, clinician, actor):
    require_enabled_company(company)
    # This is the same first lock acquired by booking/proposal acceptance.
    if not getattr(actor, 'is_active', False) or actor.pk != clinician.pk:
        raise PermissionDenied('Only the doctor can change their own availability.')
    doctor = get_user_model().objects.select_for_update().get(pk=clinician.pk)
    if not doctor.is_active or not CompanyMembership.objects.filter(
        company=company, company__is_active=True, user=doctor,
        role=CompanyMembership.Role.DOCTOR, is_active=True,
    ).exists():
        raise PermissionDenied('You need an active doctor membership in this practice.')
    return doctor


@transaction.atomic
def save_working_pattern(*, company, clinician, actor, days, request=None):
    doctor = _lock_own_doctor(company, clinician, actor)
    if (
        not isinstance(days, (list, tuple)) or len(days) != 7
        or any(not isinstance(day, dict) or type(day.get('weekday')) is not int for day in days)
        or {day['weekday'] for day in days} != set(range(7))
        or any(type(day.get('is_working')) is not bool for day in days)
    ):
        raise ValidationError('Submit all seven days of the working week exactly once.')
    existing = {row.weekday: row for row in DoctorWorkingPattern.objects.select_for_update().for_company(company).filter(clinician=doctor)}
    rows = []
    for day in sorted(days, key=lambda item: item['weekday']):
        row = existing.get(day['weekday']) or DoctorWorkingPattern(company=company, clinician=doctor, weekday=day['weekday'])
        row.is_working = day['is_working']
        row.starts_at = day.get('starts_at') if row.is_working else None
        row.ends_at = day.get('ends_at') if row.is_working else None
        row.full_clean()
        rows.append(row)
    for row in rows:
        row.save()
    record_audit(
        company=company, actor=actor, action='availability.pattern_updated', target=doctor, request=request,
        metadata={'working_days': sum(row.is_working for row in rows)},
    )
    return rows


@transaction.atomic
def create_time_off(*, company, clinician, actor, starts_at, ends_at, reason, request=None):
    doctor = _lock_own_doctor(company, clinician, actor)
    period = DoctorTimeOff(company=company, clinician=doctor, starts_at=starts_at, ends_at=ends_at, reason=reason)
    period.full_clean()
    if period.ends_at <= timezone.now():
        raise ValidationError({'ends_at': 'Time off must end in the future.'})
    # Browser retries/double-clicks must not leave duplicate active blocks that
    # would each need cancellation. The doctor lock serialises both submissions.
    existing = DoctorTimeOff.objects.select_for_update().for_company(company).filter(
        clinician=doctor, starts_at=period.starts_at, ends_at=period.ends_at,
        reason=period.reason, is_active=True,
    ).order_by('pk').first()
    if existing is not None:
        return existing
    period.save()
    record_audit(company=company, actor=actor, action='availability.time_off_created', target=period, request=request)
    return period


@transaction.atomic
def cancel_time_off(*, time_off, actor, request=None):
    pointer = DoctorTimeOff.objects.select_related('company', 'clinician').get(pk=time_off.pk, company_id=time_off.company_id)
    _lock_own_doctor(pointer.company, pointer.clinician, actor)
    period = DoctorTimeOff.objects.select_for_update().get(pk=pointer.pk, company_id=pointer.company_id)
    if period.clinician_id != pointer.clinician_id:
        raise ValidationError('The availability record changed. Reload this page and try again.')
    if not period.is_active:
        return period
    period.is_active = False
    period.cancelled_at = timezone.now()
    period.cancelled_by = actor
    period.full_clean()
    period.save(update_fields=('is_active', 'cancelled_at', 'cancelled_by', 'updated_at'))
    record_audit(company=period.company, actor=actor, action='availability.time_off_cancelled', target=period, request=request)
    return period


def _interval(starts_at, duration_minutes):
    if not isinstance(starts_at, datetime) or timezone.is_naive(starts_at):
        raise ValidationError('Choose a valid appointment time with a timezone.')
    try:
        duration = timedelta(minutes=duration_minutes)
    except (TypeError, ValueError, OverflowError):
        raise ValidationError('Choose a valid appointment duration.') from None
    if duration <= timedelta(0) or duration > timedelta(days=1):
        raise ValidationError('Choose an appointment duration between one minute and one day.')
    try:
        return starts_at, starts_at + duration
    except OverflowError:
        raise ValidationError('Choose a valid appointment date.') from None


def ensure_working_time(*, company, clinician, starts_at, duration_minutes):
    """Enforce current-practice hours and global absence without exposing reasons."""
    start, end = _interval(starts_at, duration_minutes)
    if DoctorTimeOff.objects.filter(clinician=clinician, is_active=True, starts_at__lt=end, ends_at__gt=start).exists():
        raise ValidationError(UNAVAILABLE, code='clinician_unavailable')
    if company is None:
        return  # Older collision-only callers still respect global time off.
    patterns = DoctorWorkingPattern.objects.for_company(company).filter(clinician=clinician)
    if not patterns.exists():
        return  # Existing manually recorded diaries remain usable until configured.
    local_start, local_end = timezone.localtime(start, SAST), timezone.localtime(end, SAST)
    if local_start.date() != local_end.date() or not patterns.filter(
        weekday=local_start.weekday(), is_working=True,
        starts_at__lte=local_start.time(), ends_at__gte=local_end.time(),
    ).exists():
        raise ValidationError(UNAVAILABLE, code='clinician_unavailable')


def _appointment_end():
    duration = ExpressionWrapper(
        Cast('duration_minutes', IntegerField()) * Value(timedelta(minutes=1)), output_field=DurationField(),
    )
    return ExpressionWrapper(F('starts_at') + duration, output_field=DateTimeField())


def open_slots_for_day(*, company, clinicians, day, duration_minutes=30, limit=100):
    """Read-only slot previews, excluding occupied time across all practices."""
    if not isinstance(day, date) or isinstance(day, datetime):
        raise ValidationError('Choose a valid diary date.')
    if not isinstance(limit, int) or limit <= 0:
        return []
    start = datetime.combine(day, time.min, tzinfo=SAST)
    start, _ = _interval(start, duration_minutes)
    try:
        end = start + timedelta(days=1)
    except OverflowError:
        return []
    now = timezone.now()
    if end <= now:
        return []
    ids = [getattr(clinician, 'pk', clinician) for clinician in clinicians]
    doctors = list(get_user_model().objects.filter(
        pk__in=ids, is_active=True, company_memberships__company=company,
        company_memberships__company__is_active=True, company_memberships__is_active=True,
        company_memberships__role=CompanyMembership.Role.DOCTOR,
    ).distinct().order_by('pk'))
    if not doctors:
        return []
    doctor_ids = [doctor.pk for doctor in doctors]
    patterns = {}
    for row in DoctorWorkingPattern.objects.for_company(company).filter(clinician_id__in=doctor_ids):
        patterns.setdefault(row.clinician_id, {})[row.weekday] = row
    blocked = {doctor.pk: [] for doctor in doctors}
    bookings = Appointment.objects.filter(clinician_id__in=doctor_ids, starts_at__lt=end).exclude(
        status__in=(Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW),
    ).annotate(availability_ends_at=_appointment_end()).filter(availability_ends_at__gt=start)
    for doctor_id, begins, finishes in bookings.values_list('clinician_id', 'starts_at', 'availability_ends_at'):
        blocked[doctor_id].append((begins, finishes))
    absences = DoctorTimeOff.objects.filter(clinician_id__in=doctor_ids, is_active=True, starts_at__lt=end, ends_at__gt=start)
    for doctor_id, begins, finishes in absences.values_list('clinician_id', 'starts_at', 'ends_at'):
        blocked[doctor_id].append((begins, finishes))
    manual = {}
    for slot in AvailabilitySlot.objects.for_company(company).filter(
        clinician_id__in=[pk for pk in doctor_ids if pk not in patterns], is_booked=False, appointment__isnull=True,
        starts_at__gte=max(start, now), starts_at__lt=end, ends_at__lte=end,
    ).select_related('clinician').order_by('starts_at', 'pk'):
        manual.setdefault(slot.clinician_id, []).append(slot)
    duration = timedelta(minutes=duration_minutes)
    candidates = []
    for doctor in doctors:
        if doctor.pk not in patterns:
            candidates.extend(slot for slot in manual.get(doctor.pk, []) if slot.ends_at - slot.starts_at >= duration)
            continue
        row = patterns[doctor.pk].get(day.weekday())
        if row is None or not row.is_working or row.starts_at is None or row.ends_at is None:
            continue
        cursor = datetime.combine(day, row.starts_at, tzinfo=SAST)
        finishes = datetime.combine(day, row.ends_at, tzinfo=SAST)
        while cursor + duration <= finishes:
            if cursor >= now:
                candidates.append(OpenTimeSlot(clinician=doctor, starts_at=cursor, ends_at=cursor + duration))
            cursor += timedelta(minutes=15)
    available = []
    for slot in sorted(candidates, key=lambda item: (item.starts_at, item.clinician_id)):
        if any(slot.starts_at < finish and slot.ends_at > begin for begin, finish in blocked[slot.clinician_id]):
            continue
        available.append(slot)
        if len(available) >= min(limit, 1000):
            break
    return available


def appointments_requiring_attention(*, company, clinician=None):
    """Only this practice's future bookings; never expose another practice's leave."""
    appointments = Appointment.objects.for_company(company).filter(
        starts_at__gt=timezone.now(), status=Appointment.Status.BOOKED,
        patient__company=company, patient__is_active=True,
    )
    if clinician is not None:
        appointments = appointments.filter(clinician=clinician)
    appointments = appointments.annotate(
        availability_ends_at=_appointment_end(),
        availability_weekday=ExtractIsoWeekDay('starts_at', tzinfo=SAST) - Value(1),
        availability_local_start=TruncTime('starts_at', tzinfo=SAST),
        availability_local_end=TruncTime('availability_ends_at', tzinfo=SAST),
        availability_start_date=TruncDate('starts_at', tzinfo=SAST),
        availability_end_date=TruncDate('availability_ends_at', tzinfo=SAST),
    )
    patterns = DoctorWorkingPattern.objects.for_company(company).filter(clinician_id=OuterRef('clinician_id'))
    absence = DoctorTimeOff.objects.filter(
        clinician_id=OuterRef('clinician_id'), is_active=True,
        starts_at__lt=OuterRef('availability_ends_at'), ends_at__gt=OuterRef('starts_at'),
    )
    appointments = appointments.annotate(
        availability_has_pattern=Exists(patterns),
        availability_inside_hours=Exists(patterns.filter(
            weekday=OuterRef('availability_weekday'), is_working=True,
            starts_at__lte=OuterRef('availability_local_start'), ends_at__gte=OuterRef('availability_local_end'),
        )),
        availability_has_time_off=Exists(absence),
    ).filter(
        Q(availability_has_time_off=True)
        | (Q(availability_has_pattern=True) & (
            Q(availability_inside_hours=False) | ~Q(availability_start_date=F('availability_end_date'))
        ))
    )
    return appointments.select_related('patient', 'clinician').order_by('starts_at', 'pk')
