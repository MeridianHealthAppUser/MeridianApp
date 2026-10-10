"""Date-and-time and time pickers that only allow quarter hours: XX:00, XX:15, XX:30 and XX:45.

The browser's own pickers list every minute whatever they're told, so a time is chosen from a Time list of
quarter hours (00:00, 00:15 … 23:45), after a date where there is one. The fields refuse any other minute,
whatever is sent. A time saved before that isn't on a quarter hour shows at the quarter hour before it.
"""

import re
from datetime import datetime, time

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

QUARTER_HOUR_CHOICES = [(f'{hour:02d}:{minute:02d}',) * 2 for hour in range(24) for minute in (0, 15, 30, 45)]
_CLOCK = re.compile(r'(\d{1,2}):(\d{2})(?::(\d{2}))?')
_DATE = re.compile(r'\d{4}-\d{2}-\d{2}')


def on_quarter_hour(value):
    """Whether a time (or a date and time), as a value or as the text a form sent, falls on a quarter hour."""
    if isinstance(value, (datetime, time)):
        return value.minute % 15 == 0 and not value.second and not value.microsecond
    match = _CLOCK.search(str(value or ''))
    if not match:
        return True
    return int(match.group(2)) % 15 == 0 and not int(match.group(3) or 0)


def floor_quarter_hour(value):
    """The quarter hour at or before a time: 11:52 is 11:45."""
    return value.replace(minute=value.minute - value.minute % 15, second=0, microsecond=0)


def validate_quarter_hour(value):
    if value is not None and not on_quarter_hour(value):
        raise ValidationError('Choose a time on the quarter hour: :00, :15, :30 or :45.', code='quarter_hour')


def quarter_hour_text(value):
    """"HH:MM" at the quarter hour at or before a time, as the Time list shows it; blank for no time."""
    if isinstance(value, datetime) and timezone.is_aware(value):
        value = timezone.localtime(value)
    if isinstance(value, (datetime, time)):
        hour, minute = value.hour, value.minute
    else:
        match = _CLOCK.search(str(value or ''))
        if not match:
            return ''
        hour, minute = int(match.group(1)), int(match.group(2))
    return f'{hour:02d}:{minute - minute % 15:02d}'


class QuarterHourTimeWidget(forms.Select):
    """A Time list of quarter hours, sending "HH:MM" as a time input would."""

    def __init__(self, attrs=None, placeholder='Time'):
        super().__init__({'class': 'quarter-hour-time', **(attrs or {})}, choices=[('', placeholder), *QUARTER_HOUR_CHOICES])

    def format_value(self, value):
        return [quarter_hour_text(value)]


class QuarterHourDateTimeWidget(forms.MultiWidget):
    """A date, then a Time list of quarter hours, side by side. Sends one "YYYY-MM-DD HH:MM" to its field."""

    template_name = 'care/widgets/quarter_hour_picker.html'

    def __init__(self, attrs=None):
        date = forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date', 'aria-label': 'Date', 'class': 'quarter-hour-picker__date'})
        super().__init__((date, QuarterHourTimeWidget({'aria-label': 'Time'})), attrs)

    def decompress(self, value):
        if isinstance(value, datetime) and timezone.is_aware(value):
            value = timezone.localtime(value)
        if isinstance(value, datetime):
            day = value.date()
        else:
            match = _DATE.search(str(value or ''))
            day = match.group(0) if match else ''
        return [day, quarter_hour_text(value)]

    def value_from_datadict(self, data, files, name):
        if name in data:  # One value, as a date-and-time input sends it.
            return data.get(name)
        day, at = super().value_from_datadict(data, files, name)
        return f'{day} {at}' if day or at else ''

    def value_omitted_from_data(self, data, files, name):
        return name not in data and super().value_omitted_from_data(data, files, name)

    def id_for_label(self, id_):
        # The field's label goes to its date.
        return f'{id_}_0' if id_ else ''


class QuarterHourDateTimeField(forms.DateTimeField):
    widget = QuarterHourDateTimeWidget
    default_validators = [validate_quarter_hour]


class QuarterHourTimeField(forms.TimeField):
    widget = QuarterHourTimeWidget
    default_validators = [validate_quarter_hour]
