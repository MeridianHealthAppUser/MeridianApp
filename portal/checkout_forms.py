"""Initial-consultation checkout: signed time choices, a test code and a new login."""

import secrets
from datetime import timedelta

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core import signing
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from care.checkout import BOOKING_DAYS, CONSULT_MINUTES, PAYMENT_METHODS


CHECKOUT_SLOT_SALT = 'portal.questionnaire-checkout-slot.v1'
CHECKOUT_SLOT_MAX_AGE = 20 * 60
SLOT_ERROR = 'This time is no longer available or the selection has expired. Choose a time again.'


def make_checkout_slot(lead, owner, slot):
    return signing.dumps({
        'lead_id': lead.pk, 'owner': owner, 'clinician_id': slot.clinician_id,
        'starts_at': slot.starts_at.isoformat(), 'duration_minutes': CONSULT_MINUTES,
    }, salt=CHECKOUT_SLOT_SALT)


def read_checkout_slot(token, lead, owner):
    """Return (clinician_id, starts_at) for this browser's own enquiry only."""
    if not isinstance(token, str) or not token or len(token) > 4096 or not owner:
        raise ValidationError(SLOT_ERROR)
    try:
        data = signing.loads(token, salt=CHECKOUT_SLOT_SALT, max_age=CHECKOUT_SLOT_MAX_AGE)
    except (signing.BadSignature, ValueError, TypeError):
        raise ValidationError(SLOT_ERROR) from None
    if (not isinstance(data, dict) or data.get('lead_id') != lead.pk or data.get('duration_minutes') != CONSULT_MINUTES
            or not isinstance(data.get('owner'), str) or not secrets.compare_digest(data['owner'], owner)):
        raise ValidationError(SLOT_ERROR)
    if type(data.get('clinician_id')) is not int or not isinstance(data.get('starts_at'), str):
        raise ValidationError(SLOT_ERROR)
    try:
        starts_at = parse_datetime(data['starts_at'])
    except (ValueError, TypeError):
        raise ValidationError(SLOT_ERROR) from None
    if starts_at is None or timezone.is_naive(starts_at):
        raise ValidationError(SLOT_ERROR)
    return data['clinician_id'], starts_at


class CheckoutDateForm(forms.Form):
    date = forms.DateField(label='Date (SAST)', widget=forms.DateInput(attrs={'type': 'date', 'form': 'checkout-date-form'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        today = timezone.localdate()
        self.fields['date'].widget.attrs.update(min=today.isoformat(), max=(today + timedelta(days=BOOKING_DAYS)).isoformat())

    def clean_date(self):
        value = self.cleaned_data['date']
        today = timezone.localdate()
        if value < today or value > today + timedelta(days=BOOKING_DAYS):
            raise ValidationError(f'Choose a date from today to {BOOKING_DAYS} days ahead.')
        return value


class CheckoutForm(forms.Form):
    """Card and EFT details are never posted: no payment provider is connected."""

    slot = forms.CharField(max_length=4096, required=False)
    discount_code = forms.CharField(label='Discount code', max_length=40, required=False,
                                    widget=forms.TextInput(attrs={'autocomplete': 'off', 'autocapitalize': 'none', 'spellcheck': 'false'}))
    payment_method = forms.ChoiceField(label='Payment method', choices=PAYMENT_METHODS, initial='card',
                                       required=False, widget=forms.RadioSelect)
    password1 = forms.CharField(label='Password', required=False, strip=False,
                                widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}))
    password2 = forms.CharField(label='Confirm password', required=False, strip=False,
                                widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}))

    def __init__(self, *args, lead, paying=False, needs_password=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.lead = lead
        self.paying = paying
        self.needs_password = needs_password
        if needs_password:
            for name in ('password1', 'password2'):
                self.fields[name].widget.attrs['required'] = True

    def clean(self):
        data = super().clean()
        if not self.paying:
            return data
        if not data.get('slot'):
            self.add_error('slot', 'Choose a consultation time.')
        if self.needs_password:
            first, second = data.get('password1'), data.get('password2')
            if not first:
                self.add_error('password1', 'Choose a password for your new login.')
            elif first != second:
                self.add_error('password2', 'The two passwords do not match.')
            else:
                User = get_user_model()
                candidate = User(email=self.lead.email, first_name=self.lead.first_name, last_name=self.lead.last_name)
                try:
                    validate_password(first, user=candidate)
                except ValidationError as error:
                    self.add_error('password1', error)
        return data
