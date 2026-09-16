"""Constrained forms with expiring, practice-bound write contexts."""

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import password_validators_help_text_html, validate_password
from django.core import signing
from django.core.exceptions import ValidationError

from .management_services import manageable_practices
from .models import Company, CompanyMembership
from .tenancy import multi_practice_enabled, require_multi_practice

MANAGEMENT_CONTEXT_SALT = 'practices.management-context.v1'
MANAGEMENT_CONTEXT_MAX_AGE = 12 * 60 * 60
CONTEXT_ERROR = ('This form expired or the selected practice or account changed. '
                 'No changes were saved. Reload the page before trying again.')


def make_management_context(request, company, kind, record=None):
    return signing.dumps({'actor_id': request.user.pk, 'company_id': company.pk,
        'kind': kind, 'record_id': record.pk if record else None,
        'updated_at': record.updated_at.isoformat() if record else None}, salt=MANAGEMENT_CONTEXT_SALT)


def validate_management_context(request, company, kind, record=None):
    token = request.POST.get('management_context', '')
    if not token or len(token) > 4096:
        raise ValidationError(CONTEXT_ERROR)
    try:
        data = signing.loads(token, salt=MANAGEMENT_CONTEXT_SALT, max_age=MANAGEMENT_CONTEXT_MAX_AGE)
    except (signing.BadSignature, TypeError, ValueError):
        raise ValidationError(CONTEXT_ERROR) from None
    expected = {'actor_id': request.user.pk, 'company_id': company.pk, 'kind': kind,
                'record_id': record.pk if record else None}
    if not isinstance(data, dict) or any(data.get(key) != value for key, value in expected.items()):
        raise ValidationError(CONTEXT_ERROR)
    return data


class StaffUserForm(forms.Form):
    mode = forms.ChoiceField(label='Account action', choices=(('create', 'Create a new staff login'),
                                      ('link', 'Link an existing account')))
    email = forms.EmailField(max_length=254)
    role = forms.ChoiceField(choices=CompanyMembership.Role.choices)
    practices = forms.ModelMultipleChoiceField(queryset=Company.objects.none(),
        widget=forms.CheckboxSelectMultiple, help_text='Only practices where you are a Super Admin appear here.')
    first_name = forms.CharField(max_length=150, required=False, label='First name (new accounts only)')
    last_name = forms.CharField(max_length=150, required=False, label='Last name (new accounts only)')
    password1 = forms.CharField(required=False, strip=False, label='Password (new accounts only)',
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
        help_text=password_validators_help_text_html())
    password2 = forms.CharField(required=False, strip=False, label='Confirm password',
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}))

    def __init__(self, *args, actor, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['practices'].queryset = manageable_practices(actor)
        self.initial.setdefault('practices', [company.pk])
        if not multi_practice_enabled():
            self.fields['practices'].widget = forms.MultipleHiddenInput()
            self.fields['practices'].help_text = ''

    def clean(self):
        data = super().clean()
        data['email'] = data.get('email', '').lower()
        if data.get('mode') == 'create':
            for field in ('first_name', 'last_name', 'password1', 'password2'):
                if not data.get(field):
                    self.add_error(field, 'Required for a new staff account.')
            if data.get('password1') != data.get('password2'):
                self.add_error('password2', 'The passwords do not match.')
            if data.get('password1'):
                user = get_user_model()(email=data.get('email', ''), first_name=data.get('first_name', ''),
                                        last_name=data.get('last_name', ''))
                try:
                    validate_password(data['password1'], user=user)
                except ValidationError as exc:
                    self.add_error('password1', exc)
        elif data.get('mode') == 'link' and any(data.get(key) for key in
                                              ('first_name', 'last_name', 'password1', 'password2')):
            raise ValidationError('Leave name and password fields empty when linking an existing account.')
        return data


class MembershipForm(forms.Form):
    role = forms.ChoiceField(choices=CompanyMembership.Role.choices)
    is_active = forms.BooleanField(required=False, label='Active in this practice',
        help_text='Turning this off removes only this practice’s staff access. The shared login and other practices stay unchanged.')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not multi_practice_enabled():
            self.fields['is_active'].help_text = 'Turning this off removes staff access to this practice without deleting the account.'


class PracticeForm(forms.ModelForm):
    def clean(self):
        require_multi_practice()
        return super().clean()

    class Meta:
        model = Company
        fields = ('name', 'slug')
        labels = {'name': 'Practice name', 'slug': 'Unique practice identifier'}
        help_texts = {'slug': 'Lowercase letters, numbers and hyphens; not a website domain.'}


class UserFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=120, label='Search name or email')
    status = forms.ChoiceField(required=False, choices=(('', 'All memberships'),
                                    ('active', 'Active'), ('inactive', 'Inactive')))
    role = forms.ChoiceField(required=False, choices=(('', 'All staff roles'), *CompanyMembership.Role.choices))
