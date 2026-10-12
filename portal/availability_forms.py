"""Doctor-owned availability forms and stale-practice protection."""

from django import forms
from django.core import signing
from django.core.exceptions import ValidationError
from django.forms import BaseFormSet, formset_factory

from care.widgets import QuarterHourTimeField


WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
SCHEDULE_CONTEXT_SALT = 'portal.schedule-form-context.v1'
SCHEDULE_CONTEXT_MAX_AGE = 12 * 60 * 60
SCHEDULE_CONTEXT_ERROR = (
    'The practice for this form has changed or the form has expired. '
    'No changes were saved. Check the selected practice before trying again.'
)


def make_schedule_context(request, company, clinician):
    return signing.dumps(
        {'actor_id': request.user.pk, 'company_id': company.pk, 'clinician_id': clinician.pk},
        salt=SCHEDULE_CONTEXT_SALT,
    )


def validate_schedule_context(request, company, clinician):
    token = request.POST.get('schedule_context', '')
    if not token or len(token) > 2048:
        raise ValidationError(SCHEDULE_CONTEXT_ERROR)
    try:
        data = signing.loads(token, salt=SCHEDULE_CONTEXT_SALT, max_age=SCHEDULE_CONTEXT_MAX_AGE)
    except (signing.BadSignature, TypeError, ValueError):
        raise ValidationError(SCHEDULE_CONTEXT_ERROR) from None
    if data != {'actor_id': request.user.pk, 'company_id': company.pk, 'clinician_id': clinician.pk}:
        raise ValidationError(SCHEDULE_CONTEXT_ERROR)


# How many periods a day of regular working hours can have, such as 08:00–11:00 and 13:00–16:00.
WORKING_PERIODS = 4


def period_names(index):
    """A period's From and Until fields: the first keeps the names starts_at and ends_at, as older forms send."""
    suffix = f'_{index + 1}' if index else ''
    return f'starts_at{suffix}', f'ends_at{suffix}'


class WorkingDayForm(forms.Form):
    """One day of regular working hours: Working, then up to WORKING_PERIODS periods, each a From and an Until."""

    is_working = forms.BooleanField(label='Working', required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for index in range(WORKING_PERIODS):
            start, end = period_names(index)
            self.fields[start] = QuarterHourTimeField(label='From', required=False)
            self.fields[end] = QuarterHourTimeField(label='Until', required=False)

    def periods(self):
        """Each period's From and Until fields, for the template."""
        return [(self[start], self[end]) for start, end in map(period_names, range(WORKING_PERIODS))]

    def clean(self):
        data = super().clean()
        data['periods'] = []
        if not data.get('is_working'):
            for name in self.fields:
                if name != 'is_working':
                    data[name] = None
            return data
        periods = []
        for start, end in map(period_names, range(WORKING_PERIODS)):
            if not data.get(start) and not data.get(end):
                continue
            for name in (start, end):
                if not data.get(name) and name not in self.errors:
                    self.add_error(name, 'Enter a time for this working day.')
            if data.get(start) and data.get(end):
                if data[end] <= data[start]:
                    self.add_error(end, 'The end time must be after the start time on the same day.')
                else:
                    periods.append((data[start], data[end]))
        if not periods and not self.errors:
            for name in period_names(0):
                self.add_error(name, 'Enter a time for this working day.')
        periods.sort()
        if any(following[0] < previous[1] for previous, following in zip(periods, periods[1:])):
            self.add_error(None, 'These working periods overlap. Each must end before the next one starts.')
        data['periods'] = periods
        return data


class BaseWorkingPatternFormSet(BaseFormSet):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Days are assigned by the server, never taken from hidden model IDs or
        # a submitted weekday. Exactly seven rows are validated below.
        for index, form in enumerate(self.forms):
            form.day_label = WEEKDAYS[index]
            form.weekday = index

    def days(self):
        return [dict(weekday=index, **form.cleaned_data) for index, form in enumerate(self.forms)]


WorkingPatternFormSet = formset_factory(
    WorkingDayForm, formset=BaseWorkingPatternFormSet, extra=0,
    min_num=7, max_num=7, validate_min=True, validate_max=True, absolute_max=7,
)


class TimeOffForm(forms.Form):
    """Days off are whole South Africa days, from one date to another inclusive."""

    starts_on = forms.DateField(label='From', widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}))
    ends_on = forms.DateField(label='To', widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}))

    def clean(self):
        from django.utils import timezone

        data = super().clean()
        starts_on, ends_on = data.get('starts_on'), data.get('ends_on')
        if starts_on and ends_on:
            if ends_on < starts_on:
                self.add_error('ends_on', 'The To date must be on or after the From date.')
            elif ends_on < timezone.localdate():
                self.add_error('ends_on', 'Leave must include today or a later date.')
            elif (ends_on - starts_on).days + 1 > 366:
                self.add_error('ends_on', 'Add at most one year of leave at a time.')
        return data


class SpecialDayForm(forms.Form):
    """Different hours on one date; they replace the working pattern for that date only."""

    date = forms.DateField(label='Date', widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}))
    starts_at = QuarterHourTimeField(label='From')
    ends_at = QuarterHourTimeField(label='To')

    def clean(self):
        from django.utils import timezone

        data = super().clean()
        if data.get('date') and data['date'] < timezone.localdate():
            self.add_error('date', 'Choose today or a later date.')
        if data.get('starts_at') and data.get('ends_at') and data['ends_at'] <= data['starts_at']:
            self.add_error('ends_at', 'The To time must be after the From time.')
        return data
