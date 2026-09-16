from django import forms
from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import ValidationError


class AccountProfileForm(forms.ModelForm):
    """Only display names are editable here; identity and access are not fields."""

    class Meta:
        model = get_user_model()
        fields = ('first_name', 'last_name')
        widgets = {
            'first_name': forms.TextInput(attrs={'autocomplete': 'given-name'}),
            'last_name': forms.TextInput(attrs={'autocomplete': 'family-name'}),
        }


class EmailAuthenticationForm(forms.Form):
    """Authentication form with an email-first, patient-friendly interface."""

    email = forms.EmailField(
        label='Email address',
        widget=forms.EmailInput(
            attrs={
                'autocomplete': 'username',
                'autofocus': True,
                'placeholder': 'you@example.com',
            }
        ),
    )
    password = forms.CharField(
        label='Password',
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                'autocomplete': 'current-password',
                'placeholder': 'Enter your password',
            }
        ),
    )
    remember_me = forms.BooleanField(
        label='Keep me signed in on this device',
        required=False,
        initial=True,
    )

    error_messages = {
        'invalid_login': 'Please enter a correct email address and password.',
        'inactive': 'This account is inactive.',
    }

    def __init__(self, request=None, *args, **kwargs):
        self.request = request
        self.user_cache = None
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        email = cleaned_data.get('email')
        password = cleaned_data.get('password')

        if email and password:
            email = get_user_model().objects.normalize_email(email)
            cleaned_data['email'] = email
            self.user_cache = authenticate(self.request, email=email, password=password)
            if self.user_cache is None:
                raise ValidationError(
                    self.error_messages['invalid_login'],
                    code='invalid_login',
                )
            self.confirm_login_allowed(self.user_cache)

        return cleaned_data

    def confirm_login_allowed(self, user):
        if not user.is_active:
            raise ValidationError(self.error_messages['inactive'], code='inactive')
        from practices.services import available_companies_for, available_patient_companies_for
        from practices.tenancy import multi_practice_enabled

        if not multi_practice_enabled() and not (
            available_companies_for(user).exists() or available_patient_companies_for(user).exists()
        ):
            raise ValidationError(self.error_messages['invalid_login'], code='invalid_login')

    def get_user(self):
        return self.user_cache
