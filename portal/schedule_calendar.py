"""Read-only week and month summaries using the same authorised diary as the day table."""

import calendar
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

from django.db.models.functions import TruncDate
from django.urls import reverse
from django.utils import timezone

from care.availability import appointments_requiring_attention, attention_kind


MIN_DATE = date(1900, 1, 1)
MAX_DATE = date(2100, 12, 31)


SECTIONS = ('bookings', 'availability')


def schedule_section(value):
    """Calendar & Bookings (the default) or Working Pattern & Availability."""
    return value if value in SECTIONS else 'bookings'


def schedule_url(*, day, clinician=None, view='table', section='bookings'):
    params = {'date': day.isoformat() if isinstance(day, date) else day, 'view': view}
    if clinician:
        params['clinician'] = getattr(clinician, 'pk', clinician)
    if section == 'availability':
        params['section'] = section
    return f'{reverse("portal:staff-schedule")}?{urlencode(params)}'


def _shift_month(day, offset):
    month_index = day.year * 12 + day.month - 1 + offset
    year, month = divmod(month_index, 12)
    month += 1
    shifted = date(year, month, min(day.day, calendar.monthrange(year, month)[1]))
    return shifted if MIN_DATE <= shifted <= MAX_DATE else None


def calendar_range(selected_date):
    """The first and last dates on the Monday-first month grid."""
    weeks = calendar.Calendar(firstweekday=calendar.MONDAY).monthdatescalendar(selected_date.year, selected_date.month)
    return weeks[0][0], weeks[-1][-1]


def month_context(*, appointments, selected_date, clinician, states=None):
    """Every booking per day in time order, cancelled ones included but not counted as booked, and each day's state.

    states maps a date to its diary_day_states entry when one doctor's diary is shown.
    A day links to that date in Day view; each booking links to its appointment details.
    """
    month_start = selected_date.replace(day=1)
    month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(month_start, time.min), tz)
    end = timezone.make_aware(datetime.combine(month_end, time.min), tz)
    bookings = appointments.filter(starts_at__gte=start, starts_at__lt=end).annotate(
        calendar_date=TruncDate('starts_at', tzinfo=tz),
    ).select_related('patient').order_by('starts_at', 'pk')
    by_date = {}
    for appointment in bookings:
        by_date.setdefault(appointment.calendar_date, []).append(appointment)

    today = timezone.localdate()
    weeks = []
    for week in calendar.Calendar(firstweekday=calendar.MONDAY).monthdatescalendar(selected_date.year, selected_date.month):
        days = []
        for day in week:
            events = by_date.get(day, [])
            enabled = MIN_DATE <= day <= MAX_DATE
            days.append({
                'date': day, 'is_current_month': day.month == selected_date.month,
                'is_selected': day == selected_date, 'is_today': day == today,
                'is_enabled': enabled,
                'url': schedule_url(day=day, clinician=clinician, view='table') if enabled else None,
                'appointments': events,
                'count': sum(appointment.status != 'cancelled' for appointment in events),
                'state': (states or {}).get(day),
            })
        weeks.append(days)
    previous = _shift_month(selected_date, -1)
    following = _shift_month(selected_date, 1)
    return {
        'calendar_month': month_start, 'calendar_weeks': weeks,
        'month_booked': sum(day['count'] for week in weeks for day in week if day['is_current_month']),
        'prev_month_url': schedule_url(day=previous, clinician=clinician, view='calendar') if previous else None,
        'next_month_url': schedule_url(day=following, clinician=clinician, view='calendar') if following else None,
    }


def week_range(selected_date):
    """Monday and Sunday of the ISO week the date falls in."""
    monday = selected_date - timedelta(days=selected_date.weekday())
    return monday, monday + timedelta(days=6)


def _shift_week(day, offset):
    """The same weekday a week away, kept inside the supported dates; None when that week is out of range."""
    shifted = day + timedelta(weeks=offset)
    monday, sunday = week_range(shifted)
    if sunday < MIN_DATE or monday > MAX_DATE:
        return None
    return min(max(shifted, MIN_DATE), MAX_DATE)


def week_context(*, appointments, selected_date, clinician, company, states=None):
    """Monday to Sunday of the selected date's ISO week, every booking per day in time order.

    Cancelled and no-show bookings stay in the list (marked as in Day view) but aren't counted as booked.
    One query loads the week; one more marks the bookings that need a new time.
    """
    monday, sunday = week_range(selected_date)
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(monday, time.min), tz)
    end = timezone.make_aware(datetime.combine(sunday + timedelta(days=1), time.min), tz)
    bookings = list(appointments.filter(starts_at__gte=start, starts_at__lt=end).annotate(
        calendar_date=TruncDate('starts_at', tzinfo=tz),
    ).select_related('patient', 'clinician').order_by('starts_at', 'pk'))
    attention = {}
    if bookings:
        attention = {appointment.pk: attention_kind(appointment) for appointment in appointments_requiring_attention(
            company=company, clinician=clinician,
        ).filter(pk__in=[appointment.pk for appointment in bookings])}
    by_date = {}
    for appointment in bookings:
        appointment.attention = attention.get(appointment.pk)
        appointment.needs_attention = appointment.attention is not None
        by_date.setdefault(appointment.calendar_date, []).append(appointment)

    today = timezone.localdate()
    days = []
    for offset in range(7):
        day = monday + timedelta(days=offset)
        events = by_date.get(day, [])
        enabled = MIN_DATE <= day <= MAX_DATE
        days.append({
            'date': day, 'is_selected': day == selected_date, 'is_today': day == today, 'is_enabled': enabled,
            'url': schedule_url(day=day, clinician=clinician, view='table') if enabled else None,
            'appointments': events,
            'count': sum(appointment.status != 'cancelled' for appointment in events),
            'state': (states or {}).get(day),
        })
    previous = _shift_week(selected_date, -1)
    following = _shift_week(selected_date, 1)
    return {
        'week_start': monday, 'week_end': sunday, 'week_number': monday.isocalendar()[1], 'week_days': days,
        'week_booked': sum(day['count'] for day in days), 'week_has_today': monday <= today <= sunday,
        'prev_week_url': schedule_url(day=previous, clinician=clinician, view='week') if previous else None,
        'next_week_url': schedule_url(day=following, clinician=clinician, view='week') if following else None,
    }
