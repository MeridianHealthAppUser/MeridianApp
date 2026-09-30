"""Create a login with explicit practice access from the technical admin."""

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ValidationError

from practices.models import Company, CompanyMembership
from practices.tenancy import enabled_companies, multi_practice_enabled

from .models import User


PATIENT_ROLE = 'patient'
ROLE_PERMISSIONS = {
    **{role: 'practices.add_companymembership' for role in CompanyMembership.Role.values},
    PATIENT_ROLE: 'practices.add_patient',
}


class PracticeUserCreationForm(UserCreationForm):
    role = forms.ChoiceField(
        label='Role',
        choices=(('', 'Choose a role'), *CompanyMembership.Role.choices, (PATIENT_ROLE, 'Patient')),
        help_text='Staff roles open the practice workspace. Patients open their own care portal.',
    )
    practice = forms.ModelChoiceField(queryset=Company.objects.none())

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('email', 'first_name', 'last_name')

    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.practice = None
        self.fields['first_name'].required = True
        self.fields['last_name'].required = True
        for name in ('password1', 'password2'):
            self.fields[name].widget.attrs['class'] = 'form-control'
        self.fields['role'].choices = [
            (value, label) for value, label in self.fields['role'].choices
            if not value or actor.has_perm(ROLE_PERMISSIONS[value])
        ]
        if multi_practice_enabled():
            self.fields['practice'].queryset = enabled_companies()
        else:
            # A submitted practice ID must never override the deployment boundary.
            self.fields.pop('practice', None)
        if 'is_staff' in self.fields:
            self.fields['is_staff'].label = 'Access to Django administration'
        if 'is_superuser' in self.fields:
            self.fields['is_superuser'].label = 'Full Django administrator permissions'

    def clean_email(self):
        email = User.objects.normalize_email(self.cleaned_data['email'].strip())
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError('A user with this email address already exists.')
        return email

    def clean(self):
        data = super().clean()
        if not self.actor.is_superuser and any(
            self.data.get(field) for field in ('is_staff', 'is_superuser')
        ):
            raise ValidationError('Only a Django superuser can grant Django administration access.')
        if data.get('is_superuser') and not data.get('is_staff'):
            self.add_error('is_staff', 'A Django superuser also needs access to Django administration.')
        if multi_practice_enabled():
            self.practice = data.get('practice')
        else:
            self.practice = enabled_companies().first()
            if self.practice is None:
                raise ValidationError(
                    'The configured practice is missing or inactive. Set up an active practice before adding users.'
                )
        return data
