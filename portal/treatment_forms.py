"""Explicit treatment decisions and practice-bound local-plan confirmations."""

import uuid

from django import forms
from django.core import signing
from django.core.exceptions import ValidationError

from care.models import MedicationProduct, TreatmentAuthorization


TREATMENT_CONTEXT_SALT = 'portal.treatment-context.v1'
TREATMENT_CONTEXT_MAX_AGE = 12 * 60 * 60


def _scope(request, company, patient, kind, record=None):
    return dict(actor_id=request.user.pk, company_id=company.pk, patient_id=patient.pk,
                kind=kind, record_id=record.pk if record else None)


def make_treatment_context(request, company, patient, kind, record=None):
    return signing.dumps(dict(_scope(request, company, patient, kind, record),
                              submission_key=str(uuid.uuid4())), salt=TREATMENT_CONTEXT_SALT)


def validate_treatment_context(request, company, patient, kind, record=None):
    error = 'This form has expired or its practice or record has changed. No changes were saved. Reload the page and check the selected practice.'
    token = request.POST.get('treatment_context', '')
    if not token or len(token) > 4096:
        raise ValidationError(error)
    try:
        data = signing.loads(token, salt=TREATMENT_CONTEXT_SALT, max_age=TREATMENT_CONTEXT_MAX_AGE)
    except (signing.BadSignature, ValueError, TypeError):
        raise ValidationError(error) from None
    if not isinstance(data, dict) or any(data.get(key) != value for key, value in _scope(request, company, patient, kind, record).items()):
        raise ValidationError(error)
    return data


class AuthorizationForm(forms.Form):
    product = forms.ModelChoiceField(queryset=MedicationProduct.objects.none(), empty_label='Select the prescribed product')
    max_dose = forms.CharField(label='Maximum authorised dose', max_length=80,
                              help_text='Enter the complete dose and frequency you have clinically authorised.')
    quantity_per_cycle = forms.IntegerField(label='Quantity per cycle', min_value=1, max_value=1000)
    starts_on = forms.DateField(label='Authorised from', widget=forms.DateInput(attrs={'type': 'date'}))
    expires_on = forms.DateField(label='Authorisation expires', widget=forms.DateInput(attrs={'type': 'date'}))
    review_interval_days = forms.IntegerField(label='Review interval (days)', min_value=1, max_value=3650)
    instructions = forms.CharField(label='Instructions visible to the patient', required=False, max_length=10000,
                                   strip=False, widget=forms.Textarea(attrs={'rows': 5}))
    confirm = forms.BooleanField(label='I have reviewed this patient and confirm this treatment authorisation.')

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['product'].queryset = MedicationProduct.objects.for_company(company).filter(is_active=True).order_by('name', 'strength', 'pk')


class ConfirmTreatmentForm(forms.Form):
    confirm = forms.BooleanField(label='I confirm this change and understand that undispatched parcels may be held.')


class EnrollmentForm(forms.Form):
    authorization = forms.ModelChoiceField(queryset=TreatmentAuthorization.objects.none(), empty_label='Choose your current authorisation')
    confirm = forms.BooleanField(label='I want to enroll in this local care plan. No payment is collected and no parcel is automatically dispatched.')

    def __init__(self, *args, authorizations, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['authorization'].queryset = authorizations
        self.fields['authorization'].label_from_instance = lambda row: f'{row.product.name} · authorised until {row.expires_on:%d %b %Y}'
