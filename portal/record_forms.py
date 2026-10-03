"""Validated scope and care-purpose choices for staff records."""

from datetime import date

from django import forms
from django.contrib.auth import get_user_model
from django.core.validators import MaxValueValidator, MinValueValidator
from django.utils import timezone

from practices.models import CompanyMembership
from practices.tenancy import multi_practice_enabled, scope_queryset
from .staff_forms import PatientDirectoryFilterForm


RECORD_PURPOSES = (
    ('direct_care', 'Direct care of this patient'),
    ('covering_colleague', 'Covering a treating colleague'),
)
RECORD_CATEGORIES = (
    ('all', 'Everything'), ('clinical', 'Clinical'), ('supply', 'Supply'),
    ('appointments', 'Appointments'), ('system', 'System and consent'),
)


class ClinicalRecordFilterForm(forms.Form):
    snapshot = forms.DateTimeField(required=False, widget=forms.HiddenInput)
    scope = forms.ChoiceField(label='Records to include', choices=(
        ('current', 'This practice'), ('all', 'My permitted clinical practices'),
    ))
    reason = forms.ChoiceField(label='Reason for cross-practice access', required=False,
                              choices=(('', 'Choose a care purpose'), *RECORD_PURPOSES))
    category = forms.ChoiceField(label='Activity', choices=RECORD_CATEGORIES)
    date_from = forms.DateField(label='From date', required=False, widget=forms.DateInput(attrs={
        'type': 'date', 'min': '1900-01-01', 'max': '2100-12-31',
    }), validators=[MinValueValidator(date(1900, 1, 1)), MaxValueValidator(date(2100, 12, 31))])
    date_to = forms.DateField(label='To date', required=False, widget=forms.DateInput(attrs={
        'type': 'date', 'min': '1900-01-01', 'max': '2100-12-31',
    }), validators=[MinValueValidator(date(1900, 1, 1)), MaxValueValidator(date(2100, 12, 31))])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not multi_practice_enabled():
            self.fields['scope'].choices = (('current', 'This practice'),)
            self.fields['scope'].widget = forms.HiddenInput()
            self.fields['reason'].widget = forms.HiddenInput()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('scope') == 'all' and not cleaned.get('reason'):
            self.add_error('reason', 'Choose why you need to view this patient across practices.')
        if cleaned.get('date_from') and cleaned.get('date_to') and cleaned['date_from'] > cleaned['date_to']:
            self.add_error('date_to', 'The end date must be on or after the start date.')
        return cleaned

    def clean_snapshot(self):
        value = self.cleaned_data['snapshot'] or timezone.now()
        if value.year < 1900 or value > timezone.now():
            raise forms.ValidationError('Refresh the record to start a valid history view.')
        return value


class ScopedPatientDirectoryFilterForm(PatientDirectoryFilterForm):
    scope = forms.ChoiceField(label='Practice scope', choices=(
        ('current', 'This practice'), ('all', 'All my active practices'),
    ))

    def __init__(self, *args, company, companies, **kwargs):
        super().__init__(*args, company=company, **kwargs)
        if not multi_practice_enabled():
            self.fields['scope'].choices = (('current', 'This practice'),)
            self.fields['scope'].widget = forms.HiddenInput()
            companies = scope_queryset(CompanyMembership.objects.filter(company__in=companies)).values('company_id')
        self.fields['clinician'].queryset = get_user_model().objects.filter(
            is_active=True, company_memberships__company__in=companies,
            company_memberships__company__is_active=True, company_memberships__is_active=True,
            company_memberships__clinician_type__in=CompanyMembership.CLINICIAN_TYPES,
        ).distinct().order_by('first_name', 'last_name', 'email')
