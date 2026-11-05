"""An appointment as a calendar file (.ics): Download on My Appointments, and on a doctor's Calendar & Bookings.

Calendars are often synced to other services, so the event names no patient and carries no clinical detail: just
the appointment's time, a short title, the practice and a link back to Meridian, which asks the reader to sign in.
"""

from datetime import timedelta, timezone as datetime_timezone

from django.http import HttpResponse

from care.models import Appointment


def _ical_escape(value):
    # TEXT values are escaped before line folding. Raw CR/LF cannot create new
    # calendar properties, and no clinical notes or user-entered fields appear.
    return str(value).replace('\\', '\\\\').replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,')


def _ical_fold(line):
    parts, current, count = [], '', 0
    for character in line:
        size = len(character.encode('utf-8'))
        if count + size > 75:
            parts.append(current)
            current, count = ' ', 1
        current += character
        count += size
    parts.append(current)
    return '\r\n'.join(parts)


def calendar_file(appointment, *, product, summary, description, url):
    """The appointment as a downloaded .ics file. product names who it's for, e.g. 'Patient appointments'."""
    stamp = lambda value: value.astimezone(datetime_timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    cancelled = appointment.status in (Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW)
    lines = [
        'BEGIN:VCALENDAR', 'VERSION:2.0', f'PRODID:-//Meridian//{product}//EN',
        'CALSCALE:GREGORIAN', 'METHOD:PUBLISH', 'BEGIN:VEVENT',
        f'UID:appointment-{appointment.pk}-practice-{appointment.company_id}@meridian.local',
        f'DTSTAMP:{stamp(appointment.updated_at)}', f'DTSTART:{stamp(appointment.starts_at)}',
        f'DTEND:{stamp(appointment.starts_at + timedelta(minutes=appointment.duration_minutes))}',
        f'SUMMARY:{_ical_escape(summary)}',
        f'DESCRIPTION:{_ical_escape(description)}',
        f'URL:{_ical_escape(url)}',
        f'STATUS:{"CANCELLED" if cancelled else "CONFIRMED"}',
        'CLASS:PRIVATE', 'END:VEVENT', 'END:VCALENDAR',
    ]
    response = HttpResponse('\r\n'.join(_ical_fold(line) for line in lines) + '\r\n', content_type='text/calendar; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="appointment-{appointment.pk}.ics"'
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response
