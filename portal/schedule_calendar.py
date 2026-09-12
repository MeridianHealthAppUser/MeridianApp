"""Read-only month summaries using the same authorised diary as the day table."""

import calendar
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

from django.db.models import Count, F, Window
from django.db.models.functions import RowNumber, TruncDate
from django.urls import reverse
from django.utils import timezone


MIN_DATE = date(1900, 1, 1)
MAX_DATE = date(2100, 12, 31)


def schedule_url(*, day, clinician=None, view='table'):
    params = {'date': day.isoformat() if isinstance(day, date) else day, 'view': view}
    if clinician:
        params['clinician'] = getattr(clinician, 'pk', clinician)
    return f'{reverse("portal:staff-schedule")}?{urlencode(params)}'


def _shift_month(day, offset):
    month_index = day.year * 12 + day.month - 1 + offset
    year, month = divmod(month_index, 12)
    month += 1
    shifted = date(year, month, min(day.day, calendar.monthrange(year, month)[1]))
    return shifted if MIN_DATE <= shifted <= MAX_DATE else None


def month_context(*, appointments, selected_date, clinician):
    """Fetch at most three previews per day, with uncapped day totals."""
    month_start = selected_date.replace(day=1)
    month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(month_start, time.min), tz)
    end = timezone.make_aware(datetime.combine(month_end, time.min), tz)
    previews = appointments.filter(starts_at__gte=start, starts_at__lt=end).annotate(
        calendar_date=TruncDate('starts_at', tzinfo=tz),
    ).annotate(
        calendar_position=Window(
            expression=RowNumber(), partition_by=[F('calendar_date')],
            order_by=[F('starts_at').asc(), F('pk').asc()],
        ),
        calendar_total=Window(expression=Count('pk'), partition_by=[F('calendar_date')]),
    ).filter(calendar_position__lte=3).order_by('starts_at', 'pk')
    by_date = {}
    for appointment in previews:
        by_date.setdefault(appointment.calendar_date, []).append(appointment)

    today = timezone.localdate()
    weeks = []
    for week in calendar.Calendar(firstweekday=calendar.MONDAY).monthdatescalendar(selected_date.year, selected_date.month):
        days = []
        for day in week:
            events = by_date.get(day, [])
            total = events[0].calendar_total if events else 0
            enabled = MIN_DATE <= day <= MAX_DATE
            days.append({
                'date': day, 'is_current_month': day.month == selected_date.month,
                'is_selected': day == selected_date, 'is_today': day == today,
                'is_enabled': enabled,
                'url': schedule_url(day=day, clinician=clinician, view='calendar') if enabled else None,
                'appointments': events, 'count': total, 'more_count': total - len(events),
            })
        weeks.append(days)
    previous = _shift_month(selected_date, -1)
    following = _shift_month(selected_date, 1)
    return {
        'calendar_month': month_start, 'calendar_weeks': weeks,
        'prev_month_url': schedule_url(day=previous, clinician=clinician, view='calendar') if previous else None,
        'next_month_url': schedule_url(day=following, clinician=clinician, view='calendar') if following else None,
    }
