"""Validated, practice-scoped filters for standalone staff pages."""

from datetime import date

from django import forms
from django.contrib.auth import get_user_model
from django.core.validators import MaxValueValidator, MinValueValidator

from practices.models import CompanyMembership, Patient
from care.models import RecordTag


def practice_doctors(company):
    return get_user_model().objects.filter(
        is_active=True, company_memberships__company=company,
        company_memberships__is_active=True,
        company_memberships__role=CompanyMembership.Role.DOCTOR,
    ).distinct().order_by('first_name', 'last_name', 'email')


class DoctorChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, doctor):
        return doctor.full_name


class PatientDirectoryFilterForm(forms.Form):
    q = forms.CharField(
        label='Search name or ID number', required=False, max_length=200,
        widget=forms.TextInput(attrs={'placeholder': 'Name, ID, email or record number', 'type': 'search'}),
    )
    clinician = DoctorChoiceField(
        label='Doctor', required=False, queryset=get_user_model().objects.none(), empty_label='All doctors',
    )

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['clinician'].queryset = practice_doctors(company)


class TaskFilterForm(forms.Form):
    status = forms.ChoiceField(
        required=False, initial='open',
        choices=(('open', 'Open'), ('done', 'Completed'), ('cancelled', 'Cancelled'), ('all', 'Everything')),
    )
    patient = forms.ModelChoiceField(label='Patient', required=False, queryset=Patient.objects.none(), empty_label='All patients')
    kind = forms.ChoiceField(label='Task type', required=False, choices=(('all', 'All tasks'), ('patient', 'Patient tasks'), ('general', 'General practice tasks')))
    tag = forms.ModelChoiceField(label='Status tag', required=False, queryset=RecordTag.objects.none(), empty_label='All tags')

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['patient'].queryset = Patient.objects.for_company(company).filter(is_active=True)
        self.fields['tag'].queryset = RecordTag.objects.for_company(company)

    def clean_kind(self):
        return self.cleaned_data['kind'] or 'all'

    def clean_status(self):
        return self.cleaned_data['status'] or 'open'


class ScheduleFilterForm(forms.Form):
    date = forms.DateField(
        label='Date (SAST)',
        validators=[MinValueValidator(date(1900, 1, 1)), MaxValueValidator(date(2100, 12, 31))],
        widget=forms.DateInput(attrs={'type': 'date', 'min': '1900-01-01', 'max': '2100-12-31'}),
    )
    clinician = DoctorChoiceField(
        label='Whose diary', required=False, queryset=get_user_model().objects.none(), empty_label='All doctors',
    )

    def __init__(self, *args, company, actor, membership, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.is_doctor = membership.role == CompanyMembership.Role.DOCTOR
        queryset = practice_doctors(company)
        if self.is_doctor:
            queryset = queryset.filter(pk=actor.pk)
            self.fields['clinician'].empty_label = None
            self.fields['clinician'].initial = actor.pk
        self.fields['clinician'].queryset = queryset

    def clean_clinician(self):
        clinician = self.cleaned_data['clinician']
        return self.actor if self.is_doctor else clinician
