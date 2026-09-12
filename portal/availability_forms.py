"""Doctor-owned availability forms and stale-practice protection."""

from django import forms
from django.core import signing
from django.core.exceptions import ValidationError
from django.forms import BaseFormSet, formset_factory


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


class WorkingDayForm(forms.Form):
    is_working = forms.BooleanField(label='Working', required=False)
    starts_at = forms.TimeField(label='From', required=False, widget=forms.TimeInput(format='%H:%M', attrs={'type': 'time'}))
    ends_at = forms.TimeField(label='To', required=False, widget=forms.TimeInput(format='%H:%M', attrs={'type': 'time'}))

    def clean(self):
        data = super().clean()
        if not data.get('is_working'):
            data['starts_at'] = data['ends_at'] = None
            return data
        for name in ('starts_at', 'ends_at'):
            if not data.get(name):
                self.add_error(name, 'Enter a time for this working day.')
        if data.get('starts_at') and data.get('ends_at') and data['ends_at'] <= data['starts_at']:
            self.add_error('ends_at', 'The end time must be after the start time on the same day.')
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
    reason = forms.ChoiceField(choices=(('leave', 'Leave'), ('sick', 'Sick leave'), ('other', 'Other time off')))
    starts_at = forms.DateTimeField(
        label='From (SAST)',
        widget=forms.DateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'type': 'datetime-local'}),
    )
    ends_at = forms.DateTimeField(
        label='Until (SAST)',
        widget=forms.DateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'type': 'datetime-local'}),
    )

    def clean(self):
        from datetime import timedelta
        from django.utils import timezone

        data = super().clean()
        starts_at, ends_at = data.get('starts_at'), data.get('ends_at')
        if starts_at and ends_at:
            if ends_at <= starts_at:
                self.add_error('ends_at', 'The end must be after the start.')
            elif ends_at <= timezone.now():
                self.add_error('ends_at', 'Time off must end in the future.')
            elif ends_at - starts_at > timedelta(days=366):
                self.add_error('ends_at', 'Add at most one year of time off at a time.')
        return data
