"""Standing hours, special days and private days off; availability is calculated, never booked.

For a doctor on a date, in order: a day off makes them unavailable; otherwise a special day sets the
hours; otherwise the weekly working pattern does; otherwise they are not working.
"""

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

from .models import Appointment, AvailabilitySlot, ClinicalTask, DoctorSpecialDay, DoctorTimeOff, DoctorWorkingPattern
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
        clinician_type__in=CompanyMembership.CLINICIAN_TYPES, is_active=True,
    ).exists():
        raise PermissionDenied('You need an active clinician membership in this practice.')
    return doctor


def _lock_diary(company, clinician, actor):
    """Days off and special days: the doctor themselves, or a practice administrator for any doctor here."""
    require_enabled_company(company)
    if not getattr(actor, 'is_active', False):
        raise PermissionDenied('An active practice staff account is required.')
    if actor.pk != clinician.pk and not CompanyMembership.objects.filter(
        company=company, company__is_active=True, user=actor, user__is_active=True, is_active=True,
        role__in=(CompanyMembership.Role.PRACTICE_ADMIN, CompanyMembership.Role.SUPER_ADMIN),
    ).exists():
        raise PermissionDenied('Only the doctor or a practice administrator can change this diary.')
    doctor = get_user_model().objects.select_for_update().get(pk=clinician.pk)
    if not doctor.is_active or not CompanyMembership.objects.filter(
        company=company, company__is_active=True, user=doctor,
        clinician_type__in=CompanyMembership.CLINICIAN_TYPES, is_active=True,
    ).exists():
        raise PermissionDenied('The doctor needs an active clinician membership in this practice.')
    return doctor


def day_bounds(day):
    """The start of a South Africa day and the start of the next one."""
    return datetime.combine(day, time.min, tzinfo=SAST), datetime.combine(day + timedelta(days=1), time.min, tzinfo=SAST)


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
def create_time_off(*, company, clinician, actor, starts_at, ends_at, reason='', request=None):
    doctor = _lock_diary(company, clinician, actor)
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
    notify_practice_of_conflicts(company=company, clinician=doctor, actor=actor, starts_at=period.starts_at,
                                 ends_at=period.ends_at, change='day_off', request=request)
    return period


def create_days_off(*, company, clinician, actor, first_day, last_day, request=None):
    """Whole South Africa days, from the first date to the last date inclusive."""
    if last_day < first_day:
        raise ValidationError({'ends_on': 'The To date must be on or after the From date.'})
    starts_at, _ = day_bounds(first_day)
    _, ends_at = day_bounds(last_day)
    return create_time_off(company=company, clinician=clinician, actor=actor, starts_at=starts_at, ends_at=ends_at,
                           request=request)


@transaction.atomic
def cancel_time_off(*, time_off, actor, request=None):
    pointer = DoctorTimeOff.objects.select_related('company', 'clinician').get(pk=time_off.pk, company_id=time_off.company_id)
    _lock_diary(pointer.company, pointer.clinician, actor)
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


@transaction.atomic
def set_special_day(*, company, clinician, actor, day, starts_at, ends_at, request=None):
    """One set of hours per doctor and date: setting a date again replaces its hours."""
    doctor = _lock_diary(company, clinician, actor)
    if not isinstance(day, date) or isinstance(day, datetime):
        raise ValidationError({'date': 'Choose a valid date.'})
    if day < timezone.localdate(timezone=SAST):
        raise ValidationError({'date': 'Choose today or a later date.'})
    special = DoctorSpecialDay.objects.select_for_update().for_company(company).filter(clinician=doctor, date=day).first()
    created = special is None
    if created:
        special = DoctorSpecialDay(company=company, clinician=doctor, date=day, created_by=actor)
    elif (special.starts_at, special.ends_at) == (starts_at, ends_at):
        return special  # A repeated submission changes nothing.
    special.starts_at, special.ends_at = starts_at, ends_at
    special.full_clean()
    special.save()
    record_audit(
        company=company, actor=actor, action='availability.special_day_set', target=special, request=request,
        metadata={'date': day.isoformat(), 'starts_at': starts_at.strftime('%H:%M'), 'ends_at': ends_at.strftime('%H:%M'),
                  'replaced': not created},
    )
    notify_practice_of_conflicts(company=company, clinician=doctor, actor=actor, starts_at=day_bounds(day)[0],
                                 ends_at=day_bounds(day)[1], change='special_day', request=request)
    return special


@transaction.atomic
def remove_special_day(*, special_day, actor, request=None):
    pointer = DoctorSpecialDay.objects.select_related('company', 'clinician').get(pk=special_day.pk, company_id=special_day.company_id)
    _lock_diary(pointer.company, pointer.clinician, actor)
    special = DoctorSpecialDay.objects.select_for_update().filter(pk=pointer.pk, company_id=pointer.company_id).first()
    if special is None:
        return None
    record_audit(
        company=special.company, actor=actor, action='availability.special_day_removed', target=special, request=request,
        metadata={'date': special.date.isoformat(), 'starts_at': special.starts_at.strftime('%H:%M'),
                  'ends_at': special.ends_at.strftime('%H:%M')},
    )
    special.delete()
    return None


@transaction.atomic
def undo_diary_change(*, company, clinician, actor, day, request=None):
    """Undo what made a date unavailable: its days off recorded here and its special day."""
    doctor = _lock_diary(company, clinician, actor)
    start, end = day_bounds(day)
    removed = 0
    for period in DoctorTimeOff.objects.select_for_update().for_company(company).filter(
        clinician=doctor, is_active=True, starts_at__lt=end, ends_at__gt=start,
    ):
        cancel_time_off(time_off=period, actor=actor, request=request)
        removed += 1
    special = DoctorSpecialDay.objects.for_company(company).filter(clinician=doctor, date=day).first()
    if special is not None:
        remove_special_day(special_day=special, actor=actor, request=request)
        removed += 1
    return removed


def notify_practice_of_conflicts(*, company, clinician, actor, starts_at, ends_at, change, request=None):
    """A doctor's own change that leaves bookings outside their availability raises a task for the practice.

    Nobody is moved: the practice agrees a new time with each patient. An administrator making
    the change sees the clash on the page, so no task is raised for them.
    """
    if actor.pk != clinician.pk:
        return None
    affected = list(appointments_requiring_attention(company=company, clinician=clinician).filter(
        starts_at__lt=ends_at, availability_ends_at__gt=starts_at,
    ).select_related('patient'))
    if not affected:
        return None
    first_day = timezone.localdate(starts_at, timezone=SAST)
    when = first_day.strftime('%-d %b %Y')
    if change == 'special_day':
        cause = f'special hours on {when}'
    else:
        last_day = timezone.localdate(ends_at - timedelta(microseconds=1), timezone=SAST)
        cause = f'leave on {when}' if last_day == first_day else f'leave from {when} to {last_day.strftime("%-d %b %Y")}'
    count = len(affected)
    clash = '1 booking clashes' if count == 1 else f'{count} bookings clash'
    title = f"Offer new times: {clash} with {clinician.full_name}'s {cause}"[:255]
    if ClinicalTask.objects.for_company(company).filter(
        title=title, status__in=(ClinicalTask.Status.OPEN, ClinicalTask.Status.IN_PROGRESS),
    ).exists():
        return None
    lines = [
        f'{appointment.starts_at.astimezone(SAST):%a %-d %b %Y, %H:%M} SAST · {appointment.patient} · '
        f'{appointment.get_appointment_type_display()}'
        for appointment in affected
    ]
    task = ClinicalTask(
        company=company, patient=affected[0].patient if count == 1 else None,
        title=title, priority=ClinicalTask.Priority.HIGH, due_at=affected[0].starts_at,
        description=(
            f'{clinician.full_name} changed their availability, and these bookings now fall outside it. '
            'Nobody has been moved. Agree a new time with each patient, then cancel and rebook.\n\n'
            + '\n'.join(lines)
        ),
    )
    task.full_clean()
    task.save()
    record_audit(
        company=company, actor=actor, action='availability.conflict_task_created', target=task, request=request,
        metadata={'appointment_ids': [appointment.pk for appointment in affected], 'change': change},
    )
    return task


def diary_day_states(*, company, clinician, first_day, last_day):
    """What each date is for one doctor, in booking precedence.

    kind is 'off' (a whole day off), 'special' (special hours), 'work' (the weekly pattern),
    'not_working', or 'unset' when no weekly pattern has been saved yet.
    """
    start, _ = day_bounds(first_day)
    _, end = day_bounds(last_day)
    absences = list(DoctorTimeOff.objects.filter(
        clinician=clinician, is_active=True, starts_at__lt=end, ends_at__gt=start,
    ).values_list('starts_at', 'ends_at'))
    specials = {row.date: row for row in DoctorSpecialDay.objects.for_company(company).filter(
        clinician=clinician, date__gte=first_day, date__lte=last_day,
    )}
    patterns = {row.weekday: row for row in DoctorWorkingPattern.objects.for_company(company).filter(clinician=clinician)}
    states = {}
    day = first_day
    while day <= last_day:
        day_start, day_end = day_bounds(day)
        special = specials.get(day)
        row = patterns.get(day.weekday())
        if any(begins <= day_start and finishes >= day_end for begins, finishes in absences):
            state = {'kind': 'off'}
        elif special is not None:
            state = {'kind': 'special', 'starts_at': special.starts_at, 'ends_at': special.ends_at}
        elif not patterns:
            state = {'kind': 'unset'}
        elif row is not None and row.is_working:
            state = {'kind': 'work', 'starts_at': row.starts_at, 'ends_at': row.ends_at}
        else:
            state = {'kind': 'not_working'}
        states[day] = state
        day += timedelta(days=1)
    return states


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
    local_start, local_end = timezone.localtime(start, SAST), timezone.localtime(end, SAST)
    special = DoctorSpecialDay.objects.for_company(company).filter(clinician=clinician, date=local_start.date()).first()
    if special is not None:
        # A special day replaces the weekly pattern for its date.
        if (
            local_start.date() != local_end.date()
            or local_start.time() < special.starts_at or local_end.time() > special.ends_at
        ):
            raise ValidationError(UNAVAILABLE, code='clinician_unavailable')
        return
    patterns = DoctorWorkingPattern.objects.for_company(company).filter(clinician=clinician)
    if not patterns.exists():
        return  # Existing manually recorded diaries remain usable until configured.
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
        company_memberships__clinician_type__in=CompanyMembership.CLINICIAN_TYPES,
    ).distinct().order_by('pk'))
    if not doctors:
        return []
    doctor_ids = [doctor.pk for doctor in doctors]
    patterns = {}
    for row in DoctorWorkingPattern.objects.for_company(company).filter(clinician_id__in=doctor_ids):
        patterns.setdefault(row.clinician_id, {})[row.weekday] = row
    specials = {row.clinician_id: row for row in DoctorSpecialDay.objects.for_company(company).filter(
        clinician_id__in=doctor_ids, date=day,
    )}
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
        clinician_id__in=[pk for pk in doctor_ids if pk not in patterns and pk not in specials],
        is_booked=False, appointment__isnull=True,
        starts_at__gte=max(start, now), starts_at__lt=end, ends_at__lte=end,
    ).select_related('clinician').order_by('starts_at', 'pk'):
        manual.setdefault(slot.clinician_id, []).append(slot)
    duration = timedelta(minutes=duration_minutes)
    candidates = []
    for doctor in doctors:
        # Days off are in `blocked`, so they win over everything below.
        hours = specials.get(doctor.pk)
        if hours is None:
            if doctor.pk not in patterns:
                candidates.extend(slot for slot in manual.get(doctor.pk, []) if slot.ends_at - slot.starts_at >= duration)
                continue
            hours = patterns[doctor.pk].get(day.weekday())
            if hours is None or not hours.is_working or hours.starts_at is None or hours.ends_at is None:
                continue
        cursor = datetime.combine(day, hours.starts_at, tzinfo=SAST)
        finishes = datetime.combine(day, hours.ends_at, tzinfo=SAST)
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


def bookable_start_times(*, company, clinician, day, duration_minutes, patient=None):
    """The quarter-hour start times a booking of this length could take with a clinician on a day, for the
    booking form's Time list: in their hours, not on time off, clear of their other bookings in any practice and,
    when a patient is given, of the patient's. Booking checks it all again when saved."""
    configured = (DoctorWorkingPattern.objects.for_company(company).filter(clinician=clinician).exists()
                  or DoctorSpecialDay.objects.for_company(company).filter(clinician=clinician, date=day).exists())
    if configured:
        starts = [slot.starts_at for slot in open_slots_for_day(
            company=company, clinicians=[clinician], day=day, duration_minutes=duration_minutes, limit=1000)]
    else:
        # No working hours set up yet: booking allows any free quarter hour, so the list offers them all.
        start, end = datetime.combine(day, time.min, tzinfo=SAST), datetime.combine(day + timedelta(days=1), time.min, tzinfo=SAST)
        duration, now = timedelta(minutes=duration_minutes), timezone.now()
        busy = list(Appointment.objects.filter(clinician=clinician, starts_at__lt=end).exclude(
            status__in=(Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW),
        ).annotate(availability_ends_at=_appointment_end()).filter(availability_ends_at__gt=start).values_list('starts_at', 'availability_ends_at'))
        busy += list(DoctorTimeOff.objects.filter(clinician=clinician, is_active=True, starts_at__lt=end, ends_at__gt=start)
                     .values_list('starts_at', 'ends_at'))
        starts = [moment for moment in (start + timedelta(minutes=15 * step) for step in range(96))
                  if moment >= now and moment + duration <= end
                  and not any(moment < finish and moment + duration > begin for begin, finish in busy)]
    if patient is not None and starts:
        theirs = Appointment.objects.filter(starts_at__lt=starts[-1] + timedelta(minutes=duration_minutes)).exclude(
            status__in=(Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW),
        ).filter(Q(patient__user_id=patient.user_id) if patient.user_id else Q(patient=patient))
        busy = list(theirs.annotate(availability_ends_at=_appointment_end()).filter(availability_ends_at__gt=starts[0])
                    .values_list('starts_at', 'availability_ends_at'))
        starts = [moment for moment in starts
                  if not any(moment < finish and moment + timedelta(minutes=duration_minutes) > begin for begin, finish in busy)]
    return [timezone.localtime(moment, SAST).strftime('%H:%M') for moment in starts]


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
    specials = DoctorSpecialDay.objects.for_company(company).filter(
        clinician_id=OuterRef('clinician_id'), date=OuterRef('availability_start_date'),
    )
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
        availability_has_special=Exists(specials),
        availability_inside_special=Exists(specials.filter(
            starts_at__lte=OuterRef('availability_local_start'), ends_at__gte=OuterRef('availability_local_end'),
        )),
    )
    crosses_midnight = ~Q(availability_start_date=F('availability_end_date'))
    appointments = appointments.filter(
        Q(availability_has_time_off=True)
        | (Q(availability_has_special=True) & (Q(availability_inside_special=False) | crosses_midnight))
        | (Q(availability_has_special=False) & Q(availability_has_pattern=True) & (
            Q(availability_inside_hours=False) | crosses_midnight
        ))
    )
    return appointments.select_related('patient', 'clinician').order_by('starts_at', 'pk')


def attention_kind(appointment):
    """Why a booking from appointments_requiring_attention needs a new time."""
    if appointment.availability_has_time_off:
        return 'day_off'
    if appointment.availability_has_special:
        return 'special_hours'
    return 'working_hours'
