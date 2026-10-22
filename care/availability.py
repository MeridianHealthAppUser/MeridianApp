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
    """Days off and special days: the doctor themselves, or a Super Admin for any doctor here. Practice
    administrators only read them."""
    require_enabled_company(company)
    if not getattr(actor, 'is_active', False):
        raise PermissionDenied('An active practice staff account is required.')
    if actor.pk != clinician.pk and not CompanyMembership.objects.filter(
        company=company, company__is_active=True, user=actor, user__is_active=True, is_active=True,
        role=CompanyMembership.Role.SUPER_ADMIN,
    ).exists():
        raise PermissionDenied('Only the doctor or a Super Admin can change this diary.')
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
    rows = []
    for day in sorted(days, key=lambda item: item['weekday']):
        if not day['is_working']:
            rows.append(DoctorWorkingPattern(company=company, clinician=doctor, weekday=day['weekday'], is_working=False))
            continue
        # A day's periods ("periods"), or its one From and Until.
        periods = sorted(day.get('periods') or [(day.get('starts_at'), day.get('ends_at'))], key=lambda period: period[0] or time.min)
        for (starts_at, ends_at), following in zip(periods, [*periods[1:], None]):
            if following is not None and ends_at and following[0] and following[0] < ends_at:
                raise ValidationError(f'{DoctorWorkingPattern.Weekday(day["weekday"]).label}\'s working periods overlap.')
            rows.append(DoctorWorkingPattern(company=company, clinician=doctor, weekday=day['weekday'], is_working=True,
                                             starts_at=starts_at, ends_at=ends_at))
    for row in rows:
        row.full_clean()
    # The whole week is replaced: one row per period worked, or one for a day not worked.
    DoctorWorkingPattern.objects.select_for_update().for_company(company).filter(clinician=doctor).delete()
    DoctorWorkingPattern.objects.bulk_create(rows)
    record_audit(
        company=company, actor=actor, action='availability.pattern_updated', target=doctor, request=request,
        metadata={'working_days': len({row.weekday for row in rows if row.is_working})},
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

    kind is 'off' (a whole day off), 'special' (special hours), 'work' (regular working hours, with its periods),
    'not_working', or 'unset' when no regular working hours have been saved yet.
    """
    start, _ = day_bounds(first_day)
    _, end = day_bounds(last_day)
    absences = list(DoctorTimeOff.objects.filter(
        clinician=clinician, is_active=True, starts_at__lt=end, ends_at__gt=start,
    ).values_list('starts_at', 'ends_at'))
    specials = {row.date: row for row in DoctorSpecialDay.objects.for_company(company).filter(
        clinician=clinician, date__gte=first_day, date__lte=last_day,
    )}
    patterns = {}
    for row in DoctorWorkingPattern.objects.for_company(company).filter(clinician=clinician):
        patterns.setdefault(row.weekday, []).append(row)
    states = {}
    day = first_day
    while day <= last_day:
        day_start, day_end = day_bounds(day)
        special = specials.get(day)
        periods = [(row.starts_at, row.ends_at) for row in patterns.get(day.weekday(), []) if row.is_working]
        if any(begins <= day_start and finishes >= day_end for begins, finishes in absences):
            state = {'kind': 'off'}
        elif special is not None:
            state = {'kind': 'special', 'starts_at': special.starts_at, 'ends_at': special.ends_at}
        elif not patterns:
            state = {'kind': 'unset'}
        elif periods:
            state = {'kind': 'work', 'starts_at': periods[0][0], 'ends_at': periods[-1][1], 'periods': periods}
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


def open_slots_for_day(*, company, clinicians, day, duration_minutes=30, limit=100, exclude_appointment=None):
    """Read-only slot previews, excluding occupied time across all practices. The appointment being moved, if
    given, doesn't occupy its own time."""
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
        patterns.setdefault(row.clinician_id, {}).setdefault(row.weekday, []).append(row)
    specials = {row.clinician_id: row for row in DoctorSpecialDay.objects.for_company(company).filter(
        clinician_id__in=doctor_ids, date=day,
    )}
    blocked = {doctor.pk: [] for doctor in doctors}
    bookings = Appointment.objects.filter(clinician_id__in=doctor_ids, starts_at__lt=end).exclude(
        status__in=(Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW),
    ).annotate(availability_ends_at=_appointment_end()).filter(availability_ends_at__gt=start)
    if exclude_appointment is not None:
        bookings = bookings.exclude(pk=exclude_appointment.pk)
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
        # Days off are in `blocked`, so they win over everything below. A special day replaces the week's periods.
        special = specials.get(doctor.pk)
        if special is not None:
            periods = [(special.starts_at, special.ends_at)]
        elif doctor.pk not in patterns:
            candidates.extend(slot for slot in manual.get(doctor.pk, []) if slot.ends_at - slot.starts_at >= duration)
            continue
        else:
            periods = [(row.starts_at, row.ends_at) for row in patterns[doctor.pk].get(day.weekday(), [])
                       if row.is_working and row.starts_at is not None and row.ends_at is not None]
        for begins, ends in periods:
            cursor = datetime.combine(day, begins, tzinfo=SAST)
            finishes = datetime.combine(day, ends, tzinfo=SAST)
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


# Available time shows each clinician's day in slots of this length, the shortest booking.
SLOT_MINUTES = 15


def day_diaries(*, company, clinicians, day, exclude_appointment=None):
    """Calendar & Bookings' Available time: each clinician's day, in SLOT_MINUTES slots across their working hours
    (a special day's instead, when it has one). Each slot is free, booked, on leave or past. Bookings in any practice
    take the time, but only this practice's say who. A whole day of leave, a day not worked or no working hours saved
    has no slots; without working hours, any availability recorded by hand for the day is shown instead. An
    appointment being moved (exclude_appointment) doesn't take its own time."""
    start, end = day_bounds(day)
    doctors = list(clinicians)
    ids = [doctor.pk for doctor in doctors]
    patterns, absences, bookings, manual = {}, {}, {}, {}
    for row in DoctorWorkingPattern.objects.for_company(company).filter(clinician_id__in=ids):
        patterns.setdefault(row.clinician_id, []).append(row)
    specials = {row.clinician_id: row for row in DoctorSpecialDay.objects.for_company(company).filter(clinician_id__in=ids, date=day)}
    for row in DoctorTimeOff.objects.filter(clinician_id__in=ids, is_active=True, starts_at__lt=end, ends_at__gt=start):
        absences.setdefault(row.clinician_id, []).append((row.starts_at, row.ends_at))
    for appointment in Appointment.objects.filter(clinician_id__in=ids, starts_at__lt=end).exclude(
        status__in=(Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW),
    ).exclude(pk=getattr(exclude_appointment, 'pk', None)).annotate(availability_ends_at=_appointment_end()).filter(availability_ends_at__gt=start).select_related('patient'):
        bookings.setdefault(appointment.clinician_id, []).append(appointment)
    for slot in AvailabilitySlot.objects.for_company(company).filter(
        clinician_id__in=ids, starts_at__gte=start, starts_at__lt=end,
    ).order_by('starts_at', 'pk'):
        manual.setdefault(slot.clinician_id, []).append((slot.starts_at, slot.ends_at, slot.is_booked or bool(slot.appointment_id)))
    now = timezone.now()

    def slot(begins, finishes, doctor_id, taken=False):
        booking = next((item for item in bookings.get(doctor_id, ())
                        if item.starts_at < finishes and item.availability_ends_at > begins), None)
        if any(off_from < finishes and off_until > begins for off_from, off_until in absences.get(doctor_id, ())):
            state = 'leave'
        elif booking is not None or taken:
            state = 'booked'
        elif begins < now:
            state = 'past'
        else:
            state = 'free'
        return {'starts_at': begins, 'ends_at': finishes, 'state': state,
                # Who it's booked with, only for this practice's bookings.
                'appointment': booking if booking is not None and booking.company_id == company.pk else None}

    diaries = []
    for doctor in doctors:
        rows = patterns.get(doctor.pk, [])
        special = specials.get(doctor.pk)
        if any(off_from <= start and off_until >= end for off_from, off_until in absences.get(doctor.pk, ())):
            kind, hours = 'off', []
        elif special is not None:
            kind, hours = 'special', [(special.starts_at, special.ends_at)]
        elif rows:
            hours = sorted((row.starts_at, row.ends_at) for row in rows
                           if row.weekday == day.weekday() and row.is_working and row.starts_at and row.ends_at)
            kind = 'work' if hours else 'not_working'
        else:
            kind, hours = 'unset', []
        periods = []
        for opens, closes in hours:
            cursor, finishes = datetime.combine(day, opens, tzinfo=SAST), datetime.combine(day, closes, tzinfo=SAST)
            slots = []
            while cursor + timedelta(minutes=SLOT_MINUTES) <= finishes:
                slots.append(slot(cursor, cursor + timedelta(minutes=SLOT_MINUTES), doctor.pk))
                cursor += timedelta(minutes=SLOT_MINUTES)
            periods.append({'starts_at': opens, 'ends_at': closes, 'slots': slots})
        if kind == 'unset' and manual.get(doctor.pk):
            periods = [{'starts_at': None, 'ends_at': None,
                        'slots': [slot(begins, finishes, doctor.pk, taken) for begins, finishes, taken in manual[doctor.pk]]}]
        diaries.append({'clinician': doctor, 'kind': kind, 'hours': hours, 'periods': periods,
                        'free': sum(item['state'] == 'free' for period in periods for item in period['slots'])})
    return diaries


def start_slots(*, company, clinician, day, duration_minutes, free_times, exclude_appointment=None):
    """The booking forms' Time, as a grid of the clinician's day (day_diaries): a slot is free when a booking of this
    length can start there (it's one of free_times), or says why not: booked, leave, past, short (the length doesn't
    fit before the next booking or the end of the hours), clash (free for the clinician, but not for the patient), or
    current (where the appointment being moved starts now). Without working hours, just the free times."""
    free = set(free_times)
    [diary] = day_diaries(company=company, clinicians=[clinician], day=day, exclude_appointment=exclude_appointment)
    moving = timezone.localtime(exclude_appointment.starts_at, SAST) if exclude_appointment is not None else None
    current = f'{moving:%H:%M}' if moving is not None and moving.date() == day else None
    needed = max(1, -(-duration_minutes // SLOT_MINUTES))
    slots = []
    for period in diary['periods']:
        for index, slot in enumerate(period['slots']):
            at = f"{timezone.localtime(slot['starts_at'], SAST):%H:%M}"
            if at == current:
                state = 'current'
            elif at in free:
                state = 'free'
            elif slot['state'] != 'free':
                state = slot['state']
            else:
                run = period['slots'][index:index + needed]
                state = 'short' if len(run) < needed or any(item['state'] != 'free' for item in run) else 'clash'
            slots.append({'time': at, 'state': state})
    return slots or [{'time': at, 'state': 'free'} for at in sorted(free)]


def bookable_start_times(*, company, clinician, day, duration_minutes, patient=None, exclude_appointment=None):
    """The quarter-hour start times a booking of this length could take with a clinician on a day, for the
    booking form's and the suggest-a-new-time form's Time lists: in their hours, not on time off, clear of their
    other bookings in any practice and, when a patient is given, of the patient's. An appointment being moved
    (exclude_appointment) doesn't block times near its own. Saving checks it all again."""
    configured = (DoctorWorkingPattern.objects.for_company(company).filter(clinician=clinician).exists()
                  or DoctorSpecialDay.objects.for_company(company).filter(clinician=clinician, date=day).exists())
    if configured:
        starts = [slot.starts_at for slot in open_slots_for_day(
            company=company, clinicians=[clinician], day=day, duration_minutes=duration_minutes, limit=1000,
            exclude_appointment=exclude_appointment)]
    else:
        # No working hours set up yet: booking allows any free quarter hour, so the list offers them all.
        start, end = datetime.combine(day, time.min, tzinfo=SAST), datetime.combine(day + timedelta(days=1), time.min, tzinfo=SAST)
        duration, now = timedelta(minutes=duration_minutes), timezone.now()
        busy = list(Appointment.objects.filter(clinician=clinician, starts_at__lt=end).exclude(
            status__in=(Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW),
        ).exclude(pk=getattr(exclude_appointment, 'pk', None)).annotate(availability_ends_at=_appointment_end()).filter(
            availability_ends_at__gt=start).values_list('starts_at', 'availability_ends_at'))
        busy += list(DoctorTimeOff.objects.filter(clinician=clinician, is_active=True, starts_at__lt=end, ends_at__gt=start)
                     .values_list('starts_at', 'ends_at'))
        starts = [moment for moment in (start + timedelta(minutes=15 * step) for step in range(96))
                  if moment >= now and moment + duration <= end
                  and not any(moment < finish and moment + duration > begin for begin, finish in busy)]
    if patient is not None and starts:
        theirs = Appointment.objects.filter(starts_at__lt=starts[-1] + timedelta(minutes=duration_minutes)).exclude(
            status__in=(Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW),
        ).exclude(pk=getattr(exclude_appointment, 'pk', None)).filter(
            Q(patient__user_id=patient.user_id) if patient.user_id else Q(patient=patient))
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
