"""Narrow, patient-owned booking, self-reported profile and timeline forms."""

from datetime import date, datetime, time, timedelta

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.utils import timezone

from care.availability import SAST
from care.models import Appointment, PatientEvent
from care.widgets import QUARTER_HOUR_CHOICES
from .staff_forms import DoctorChoiceField, practice_doctors


BOOKING_DAYS = 90
BOOKING_TYPES = (
    (Appointment.Type.REVIEW, 'Review · 15 minutes'),
    (Appointment.Type.FOLLOW_UP, 'Follow-up · 15 minutes'),
    (Appointment.Type.AD_HOC, 'Ad-hoc consultation · 15 minutes'),
)


class PatientBookingForm(forms.Form):
    """Book appointment, as staff book one: a clinician, the type, a date, then a time from that clinician's free
    quarter hours (the Time list fills in once both are chosen; static/js/appointment_booking.js). Without scripts
    the list has every quarter hour, and booking checks the time."""

    clinician = DoctorChoiceField(label='Clinician', queryset=None, empty_label='Choose a clinician')
    appointment_type = forms.ChoiceField(label='Appointment type', choices=BOOKING_TYPES)
    date = forms.DateField(label='Date', widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}))
    time = forms.ChoiceField(label='Time', choices=[('', 'Choose a time'), *QUARTER_HOUR_CHOICES],
                             error_messages={'required': 'Choose a time.'})

    def __init__(self, *args, company, patient, **kwargs):
        kwargs.setdefault('auto_id', 'booking_%s')
        super().__init__(*args, **kwargs)
        doctors = practice_doctors(company)
        self.fields['clinician'].queryset = doctors
        self.fields['clinician'].label_from_instance = lambda person: person.titled_name
        # Their own clinician to start with.
        if not self.is_bound and patient.assigned_doctor_id and doctors.filter(pk=patient.assigned_doctor_id).exists():
            self.initial.setdefault('clinician', patient.assigned_doctor_id)
        today = timezone.localdate()
        self.fields['date'].widget.attrs.update(min=today.isoformat(), max=(today + timedelta(days=BOOKING_DAYS)).isoformat())

    def clean_date(self):
        value = self.cleaned_data['date']
        today = timezone.localdate()
        if value < today or value > today + timedelta(days=BOOKING_DAYS):
            raise ValidationError(f'Choose a date from today to {BOOKING_DAYS} days ahead.')
        return value

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('date') and cleaned.get('time'):
            cleaned['starts_at'] = datetime.combine(cleaned['date'], time.fromisoformat(cleaned['time']), tzinfo=SAST)
        return cleaned


class BookingTimesQuery(forms.Form):
    """What Book appointment's Time list asks for: a practice clinician and a date."""

    clinician = forms.ModelChoiceField(queryset=None)
    date = forms.DateField()

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['clinician'].queryset = practice_doctors(company)

    clean_date = PatientBookingForm.clean_date


PROFILE_FIELDS = (
    ('medications', 'Current medication and supplements', 'Include names and how you currently take them, if known.'),
    ('allergies', 'Allergies and previous reactions', 'Include medicine or other allergies, or say if none are known.'),
    ('medical_history', 'Medical history', 'Conditions you have been diagnosed with and anything you want your doctor to know.'),
    ('surgical_history', 'Previous operations or hospital stays', 'Include approximate dates if you know them.'),
    ('family_history', 'Relevant family history', 'Tell your doctor about conditions that run in your family, if known.'),
    ('smoking', 'Smoking or vaping', 'Describe your current or previous use, if any.'),
    ('alcohol', 'Alcohol', 'Describe your current use, if any.'),
    ('activity', 'Physical activity and daily routine', 'Share what your usual week looks like.'),
    ('nutrition', 'Eating habits and dietary needs', 'Include any dietary restrictions or concerns you want to discuss.'),
    ('goals', 'Your goals and questions', 'What would you like to discuss at your consultation?'),
)


class PatientMedicalProfileForm(forms.Form):
    """The patient's own answers. Those in locked are their clinician's now, so they're shown, not asked."""

    def __init__(self, *args, locked=(), **kwargs):
        super().__init__(*args, **kwargs)
        for name, label, help_text in PROFILE_FIELDS:
            if name not in locked:
                self.fields[name] = forms.CharField(label=label, required=False, max_length=2000,
                                                    help_text=help_text, widget=forms.Textarea(attrs={'rows': 3}))


class PatientUpdatesFilterForm(forms.Form):
    category = forms.ChoiceField(label='Type of update', required=False,
                                  choices=(('', 'All updates'), *PatientEvent.Category.choices))
    date_from = forms.DateField(label='From date', required=False, widget=forms.DateInput(attrs={'type': 'date'}),
                                validators=[MinValueValidator(date(1900, 1, 1)), MaxValueValidator(date(2100, 12, 31))])
    date_to = forms.DateField(label='To date', required=False, widget=forms.DateInput(attrs={'type': 'date'}),
                              validators=[MinValueValidator(date(1900, 1, 1)), MaxValueValidator(date(2100, 12, 31))])
    sort = forms.ChoiceField(label='Order', choices=(('newest', 'Newest first'), ('oldest', 'Oldest first')))

    def clean(self):
        data = super().clean()
        start, end = data.get('date_from'), data.get('date_to')
        if start and end and start > end:
            self.add_error('date_to', 'The end date must be on or after the start date.')
        return data
