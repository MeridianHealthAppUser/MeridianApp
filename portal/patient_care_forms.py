"""Narrow, patient-owned booking, self-reported profile and timeline forms."""

from datetime import date, timedelta

from django import forms
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from care.models import Appointment, PatientEvent
from .staff_forms import DoctorChoiceField, practice_doctors


BOOKING_DAYS = 90
BOOKING_TYPES = (
    (Appointment.Type.REVIEW, 'Review · 15 minutes'),
    (Appointment.Type.FOLLOW_UP, 'Follow-up · 15 minutes'),
    (Appointment.Type.AD_HOC, 'Ad-hoc consultation · 15 minutes'),
)
BOOKING_SLOT_SALT = 'portal.patient-booking-slot.v1'
BOOKING_SLOT_MAX_AGE = 20 * 60
SLOT_ERROR = 'This time is no longer available or the selection has expired. Choose a time again.'


def make_booking_slot(request, company, patient, slot, appointment_type):
    return signing.dumps({
        'actor_id': request.user.pk, 'company_id': company.pk, 'patient_id': patient.pk,
        'clinician_id': slot.clinician_id, 'starts_at': slot.starts_at.isoformat(),
        'appointment_type': appointment_type, 'duration_minutes': 15,
    }, salt=BOOKING_SLOT_SALT)


def read_booking_slot(token, request, company, patient):
    if not isinstance(token, str) or not token or len(token) > 4096:
        raise ValidationError(SLOT_ERROR)
    try:
        data = signing.loads(token, salt=BOOKING_SLOT_SALT, max_age=BOOKING_SLOT_MAX_AGE)
    except (signing.BadSignature, ValueError, TypeError):
        raise ValidationError(SLOT_ERROR) from None
    if not isinstance(data, dict) or any(data.get(key) != value for key, value in {
        'actor_id': request.user.pk, 'company_id': company.pk, 'patient_id': patient.pk,
        'duration_minutes': 15,
    }.items()) or data.get('appointment_type') not in dict(BOOKING_TYPES):
        raise ValidationError(SLOT_ERROR)
    if type(data.get('clinician_id')) is not int or not isinstance(data.get('starts_at'), str):
        raise ValidationError(SLOT_ERROR)
    try:
        starts_at = parse_datetime(data['starts_at'])
    except (ValueError, TypeError):
        raise ValidationError(SLOT_ERROR) from None
    if starts_at is None or timezone.is_naive(starts_at):
        raise ValidationError(SLOT_ERROR)
    return data


class BookingFilterForm(forms.Form):
    date = forms.DateField(label='Date (SAST)', widget=forms.DateInput(attrs={'type': 'date'}))
    clinician = DoctorChoiceField(label='Doctor', required=False, queryset=None,
                                  empty_label='Any available doctor')
    appointment_type = forms.ChoiceField(label='Appointment type', choices=BOOKING_TYPES)

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['clinician'].queryset = practice_doctors(company)
        today = timezone.localdate()
        self.fields['date'].widget.attrs.update(min=today.isoformat(), max=(today + timedelta(days=BOOKING_DAYS)).isoformat())

    def clean_date(self):
        value = self.cleaned_data['date']
        today = timezone.localdate()
        if value < today or value > today + timedelta(days=BOOKING_DAYS):
            raise ValidationError(f'Choose a date from today to {BOOKING_DAYS} days ahead.')
        return value


class BookingConfirmationForm(forms.Form):
    slot = forms.CharField(label='Available time', max_length=4096,
                           error_messages={'required': 'Choose an available appointment time.'})
    confirm_booking = forms.BooleanField(label='I confirm this appointment time works for me')


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
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, label, help_text in PROFILE_FIELDS:
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
