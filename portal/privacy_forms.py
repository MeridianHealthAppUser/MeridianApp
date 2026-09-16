"""Narrow privacy forms, with actor/practice/record-bound write tokens."""

import uuid
from datetime import date

from django import forms
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator

from care.models import ConsentDocument, PatientDataRequest
from practices.models import Company
from practices.tenancy import multi_practice_enabled


CONTEXT_SALT = 'portal.privacy-context.v1'
CONTEXT_ERROR = 'This form expired or its practice changed. Nothing was saved. Reload and check the selected practice.'


def privacy_context(request, company, kind, *, record_id=None, version=None):
    return signing.dumps({'actor_id': request.user.pk, 'company_id': company.pk, 'kind': kind,
                          'record_id': record_id, 'version': version, 'submission_key': str(uuid.uuid4())}, salt=CONTEXT_SALT)


def validate_privacy_context(request, company, kind, *, record_id=None):
    token = request.POST.get('privacy_context', '')
    if not token or len(token) > 4096:
        raise ValidationError(CONTEXT_ERROR)
    try:
        data = signing.loads(token, salt=CONTEXT_SALT, max_age=12 * 60 * 60)
    except (signing.BadSignature, ValueError, TypeError):
        raise ValidationError(CONTEXT_ERROR) from None
    expected = {'actor_id': request.user.pk, 'company_id': company.pk, 'kind': kind, 'record_id': record_id}
    if not isinstance(data, dict) or any(data.get(key) != value for key, value in expected.items()):
        raise ValidationError(CONTEXT_ERROR)
    return data


class CommunicationPreferenceForm(forms.Form):
    marketing_enabled = forms.BooleanField(required=False,
        label='I would like to receive optional marketing communications from this practice',
        help_text='You can turn this off here at any time. This does not change your recorded treatment consent.')


class DataRequestForm(forms.Form):
    kind = forms.ChoiceField(label='What would you like help with?', choices=PatientDataRequest.Kind.choices)
    description = forms.CharField(label='Your request', max_length=5000,
        help_text='Explain what you need. Do not include passwords or payment-card details.',
        widget=forms.Textarea(attrs={'rows': 6}))


class DataRequestReplyForm(forms.Form):
    status = forms.ChoiceField(label='Updated status', choices=PatientDataRequest.Status.choices)
    body = forms.CharField(label='Response visible to the patient', max_length=5000,
                          widget=forms.Textarea(attrs={'rows': 6}))


class DataRequestFilterForm(forms.Form):
    status = forms.ChoiceField(label='Status', required=False, choices=(('', 'All statuses'), *PatientDataRequest.Status.choices))
    kind = forms.ChoiceField(label='Request type', required=False, choices=(('', 'All types'), *PatientDataRequest.Kind.choices))


class AccessHistoryFilterForm(forms.Form):
    scope = forms.ChoiceField(label='Practice scope', choices=(('current', 'This practice'), ('all', 'All my active practices')))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not multi_practice_enabled():
            self.fields['scope'].choices = (('current', 'This practice'),)
            self.fields['scope'].widget = forms.HiddenInput()


class PublicPracticeForm(forms.Form):
    practice = forms.ModelChoiceField(label='Practice', queryset=Company.objects.none(), empty_label=None)

    def __init__(self, *args, **kwargs):
        from practices.tenancy import enabled_companies, multi_practice_enabled

        super().__init__(*args, **kwargs)
        self.fields['practice'].queryset = enabled_companies()
        if not multi_practice_enabled():
            self.fields['practice'].widget = forms.HiddenInput()


class PolicyVersionForm(forms.Form):
    kind = forms.ChoiceField(label='Document type', choices=ConsentDocument.Kind.choices)
    version = forms.CharField(label='New version identifier', max_length=64)
    title = forms.CharField(label='Document title', max_length=255)
    body = forms.CharField(label='Approved document text', max_length=100000,
                           widget=forms.Textarea(attrs={'rows': 16}), help_text='Plain text. No template legal wording is supplied.')
    effective_from = forms.DateField(label='Effective from', widget=forms.DateInput(attrs={
        'type': 'date', 'min': '1900-01-01', 'max': '2100-12-31',
    }), validators=[MinValueValidator(date(1900, 1, 1)), MaxValueValidator(date(2100, 12, 31))])
    confirm_publication = forms.BooleanField(label='I confirm this version is approved for publication')
