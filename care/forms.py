from decimal import Decimal
from urllib.parse import urlsplit

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.core.validators import MaxLengthValidator
from django.db.models import Q
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient

from .models import Appointment, AppointmentProposal, ClinicalNote, ClinicalTask, WeightEntry
from .tag_forms import TagFieldsMixin


class CompanyBoundModelForm(forms.ModelForm):
    """A form that never lets a browser provide the tenancy boundary."""

    def __init__(self, *args, company, **kwargs):
        self.company = company
        super().__init__(*args, **kwargs)
        self._server_values = {}
        self._bind_instance(company=company)

    def _bind_instance(self, **values):
        self._server_values.update(values)
        for name, value in values.items():
            setattr(self.instance, name, value)

    def _get_validation_exclusions(self):
        # These fields are intentionally absent from the browser form, but must
        # participate in model validation and multi-column uniqueness checks.
        return super()._get_validation_exclusions() - self._server_values.keys()

    def _post_clean(self):
        self._bind_instance(**self._server_values)
        super()._post_clean()

    def _update_errors(self, errors):
        # Model.clean() can identify an invalid server-owned relationship. A
        # field absent from the form must become a visible non-field error,
        # otherwise ModelForm raises ValueError while displaying validation.
        if hasattr(errors, 'error_dict'):
            mapped = {}
            for field, messages in errors.error_dict.items():
                target = field if field in self.fields else NON_FIELD_ERRORS
                mapped.setdefault(target, []).extend(messages)
            errors = ValidationError(mapped)
        super()._update_errors(errors)


class PatientBoundModelForm(CompanyBoundModelForm):
    """Optionally bind a patient from the URL while checking posted values."""

    def __init__(self, *args, company, patient=None, **kwargs):
        self.patient = patient
        super().__init__(*args, company=company, **kwargs)
        self.fields['patient'].queryset = Patient.objects.filter(company=company, is_active=True)
        if patient is not None:
            self._bind_instance(patient=patient)
            self.fields['patient'].widget = forms.HiddenInput()
            self.fields['patient'].required = False
            self.initial['patient'] = patient.pk

    def clean_patient(self):
        patient = self.cleaned_data['patient']
        if self.patient is not None:
            if patient is not None and patient.pk != self.patient.pk:
                raise ValidationError('This form belongs to a different patient. Reload the patient record and try again.')
            if self.patient.company_id != self.company.pk or not self.patient.is_active:
                raise ValidationError('The patient must be active in the current practice.')
            return self.patient
        return patient


class ClinicalTaskForm(TagFieldsMixin, PatientBoundModelForm):
    class Meta:
        model = ClinicalTask
        fields = ('patient', 'title', 'description', 'assigned_to', 'priority', 'due_at')
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
            'due_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        }

    def __init__(self, *args, company, **kwargs):
        kwargs.setdefault('auto_id', 'task_%s')
        super().__init__(*args, company=company, **kwargs)
        self.fields['assigned_to'].queryset = get_user_model().objects.filter(
            is_active=True,
            company_memberships__company=company,
            company_memberships__is_active=True,
        ).distinct().order_by('first_name', 'last_name', 'email')
        self.fields['assigned_to'].required = False
        self.fields['description'].max_length = 10000
        self.fields['description'].validators.append(MaxLengthValidator(10000))
        self.fields['description'].widget.attrs['maxlength'] = 10000
        self.setup_tag_fields(company, self.instance)

    def clean(self):
        data = super().clean()
        # This compact legacy form creates tasks only; posted workflow state
        # cannot silently mark a newly raised patient task complete.
        data['status'] = ClinicalTask.Status.OPEN
        return data


class AppointmentForm(PatientBoundModelForm):
    duration_minutes = forms.IntegerField(
        min_value=5,
        max_value=120,
        initial=15,
        label='Duration (minutes)',
    )

    class Meta:
        model = Appointment
        fields = ('patient', 'clinician', 'appointment_type', 'starts_at', 'duration_minutes', 'video_link')
        widgets = {
            'starts_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
            'video_link': forms.URLInput(attrs={'placeholder': 'https://'}),
        }

    def __init__(self, *args, company, **kwargs):
        kwargs.setdefault('auto_id', 'appointment_%s')
        super().__init__(*args, company=company, **kwargs)
        self.fields['clinician'].queryset = get_user_model().objects.filter(
            is_active=True,
            company_memberships__company=company,
            company_memberships__clinician_type__in=CompanyMembership.CLINICIAN_TYPES,
            company_memberships__is_active=True,
        ).distinct().order_by('first_name', 'last_name', 'email')

    def clean_starts_at(self):
        starts_at = self.cleaned_data['starts_at']
        if starts_at <= timezone.now():
            raise ValidationError('Choose a future appointment time.')
        return starts_at

    def clean_video_link(self):
        link = self.cleaned_data['video_link']
        if link and urlsplit(link).scheme != 'https':
            raise ValidationError('Use an HTTPS link for the video consultation.')
        return link

    def clean(self):
        cleaned = super().clean()
        clinician = cleaned.get('clinician')
        starts_at = cleaned.get('starts_at')
        duration = cleaned.get('duration_minutes')
        if clinician and starts_at and duration:
            from .scheduling import ensure_clinician_available
            try:
                ensure_clinician_available(
                    company=self.company, clinician=clinician, starts_at=starts_at, duration_minutes=duration,
                    exclude_appointment=self.instance if self.instance.pk else None,
                )
            except ValidationError as error:
                if getattr(error, 'code', None) == 'appointment_overlap':
                    self.add_error('starts_at', 'This clinician already has an appointment during that time.')
                else:
                    self.add_error('starts_at', error)
        return cleaned


class ClinicalNoteForm(TagFieldsMixin, CompanyBoundModelForm):
    class Meta:
        model = ClinicalNote
        fields = ('note_type', 'body', 'is_private')
        widgets = {'body': forms.Textarea(attrs={'rows': 5, 'placeholder': 'Add a clinically appropriate note…'})}

    def __init__(self, *args, company, patient, author, **kwargs):
        self.patient = patient
        self.author = author
        super().__init__(*args, company=company, **kwargs)
        self._bind_instance(patient=patient, author=author)
        self.setup_tag_fields(company, self.instance)


class WeightEntryForm(CompanyBoundModelForm):
    weight_kg = forms.DecimalField(
        label='Weight (kg)',
        min_value=20,
        max_value=400,
        max_digits=5,
        decimal_places=2,
        widget=forms.NumberInput(attrs={'step': '0.1', 'min': '20', 'max': '400'}),
    )

    class Meta:
        model = WeightEntry
        fields = ('recorded_on', 'weight_kg', 'note')
        widgets = {
            'recorded_on': forms.DateInput(attrs={'type': 'date'}),
            'weight_kg': forms.NumberInput(attrs={'step': '0.1', 'min': '20', 'max': '400'}),
            'note': forms.TextInput(attrs={'placeholder': 'Optional note'}),
        }

    def __init__(self, *args, company, patient, recorded_by, **kwargs):
        self.patient = patient
        self.recorded_by = recorded_by
        super().__init__(*args, company=company, **kwargs)
        self._bind_instance(patient=patient, recorded_by=recorded_by)

    def clean_recorded_on(self):
        recorded_on = self.cleaned_data['recorded_on']
        if recorded_on > timezone.localdate():
            raise ValidationError('Choose today or an earlier date.')
        existing = WeightEntry.objects.filter(patient=self.patient, recorded_on=recorded_on)
        if self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise ValidationError('A weight has already been recorded for this date. Choose a different date.')
        return recorded_on


class PatientMessageForm(forms.Form):
    body = forms.CharField(
        label='Message',
        max_length=5000,
        widget=forms.Textarea(attrs={'rows': 4, 'placeholder': 'Write a message…'}),
    )


class PatientThreadForm(PatientMessageForm):
    subject = forms.CharField(
        label='Subject',
        max_length=255,
        widget=forms.TextInput(attrs={'placeholder': 'What can we help with?'}),
    )

    def __init__(self, *args, patient=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.has_recipients = True
        if patient is None:
            return  # Staff start conversations with themselves in them.
        # Patients write to their assigned clinician by default, or a past clinician or one they are booked with.
        from .messaging import recipient_choices

        clinicians = recipient_choices(patient)
        self.fields['recipient'] = forms.ModelChoiceField(
            label='To', queryset=get_user_model().objects.filter(pk__in=[clinician.pk for clinician in clinicians]),
            empty_label=None, initial=clinicians[0].pk if clinicians else None, required=False,
        )
        self.default_recipient = clinicians[0] if clinicians else None
        self.fields['recipient'].label_from_instance = (
            lambda user: f'{user.full_name} (your clinician)' if user.pk == patient.assigned_doctor_id else user.full_name)
        self.order_fields(['recipient', 'subject', 'body'])
        self.has_recipients = bool(clinicians)

    def clean_recipient(self):
        # Older forms and links post no recipient: they reach the assigned clinician.
        recipient = self.cleaned_data.get('recipient') or self.default_recipient
        if recipient is None:
            raise ValidationError('You can message a clinician once your practice assigns one to you.')
        return recipient


class ProposalAppointmentChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, appointment):
        start = timezone.localtime(appointment.starts_at).strftime('%d %b %Y, %H:%M')
        return (
            f'{appointment.get_appointment_type_display()} · {start} · '
            f'{appointment.clinician.full_name} ({appointment.get_status_display()})'
        )


class AppointmentProposalForm(forms.Form):
    appointment = ProposalAppointmentChoiceField(queryset=Appointment.objects.none(), label='Appointment')
    proposed_starts_at = forms.DateTimeField(
        label='Suggested date and time (SAST)',
        widget=forms.DateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'type': 'datetime-local'}),
    )
    note = forms.CharField(
        label='Message (optional)', max_length=2000, required=False,
        widget=forms.Textarea(attrs={'rows': 2, 'placeholder': 'Add a note about the suggested time…'}),
    )

    def __init__(self, *args, company, patient, thread, actor, actor_role, **kwargs):
        kwargs.setdefault('auto_id', f'proposal_{thread.pk}_%s')
        super().__init__(*args, **kwargs)
        appointments = Appointment.objects.for_company(company).filter(patient=patient).filter(
            Q(status=Appointment.Status.BOOKED, starts_at__gt=timezone.now())
            | Q(status__in=(Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW)),
        ).exclude(pk__in=AppointmentProposal.objects.for_company(company).filter(
            kind=AppointmentProposal.Kind.REBOOK, status=AppointmentProposal.Status.ACCEPTED,
        ).values('appointment_id')).filter(
            clinician__is_active=True,
            clinician__company_memberships__company=company,
            clinician__company_memberships__is_active=True,
            clinician__company_memberships__clinician_type__in=CompanyMembership.CLINICIAN_TYPES,
        ).select_related('clinician').distinct()
        if actor_role == 'doctor':
            active_doctor = CompanyMembership.objects.filter(
                company=company, user=actor, clinician_type__in=CompanyMembership.CLINICIAN_TYPES, is_active=True,
            ).exists()
            appointments = appointments.filter(clinician=actor) if active_doctor else appointments.none()
        elif actor_role != 'patient' or patient.user_id != actor.pk:
            appointments = appointments.none()
        if (
            thread.company_id != company.pk or thread.patient_id != patient.pk or thread.is_closed
            or not company.is_active or not patient.is_active or not actor.is_active or not patient.user_id
        ):
            appointments = appointments.none()
        # Only appointments whose clinician is in this conversation can be discussed in it.
        appointments = appointments.filter(clinician__message_thread_links__thread=thread)
        self.fields['appointment'].queryset = appointments
        self.has_selectable_appointments = appointments.exists()

    def clean_proposed_starts_at(self):
        starts_at = self.cleaned_data['proposed_starts_at']
        if starts_at <= timezone.now():
            raise ValidationError('Choose a future appointment time.')
        return starts_at

    def clean(self):
        cleaned = super().clean()
        appointment = cleaned.get('appointment')
        starts_at = cleaned.get('proposed_starts_at')
        if appointment and starts_at:
            if appointment.status == Appointment.Status.BOOKED and starts_at == appointment.starts_at:
                self.add_error('proposed_starts_at', 'Choose a different time from the current appointment.')
            else:
                from .scheduling import ensure_clinician_available
                try:
                    ensure_clinician_available(
                        company=appointment.company, clinician=appointment.clinician, starts_at=starts_at,
                        duration_minutes=appointment.duration_minutes, exclude_appointment=appointment,
                    )
                except ValidationError as error:
                    self.add_error('proposed_starts_at', ' '.join(error.messages))
        return cleaned


class EligibilityQuestionnaireForm(forms.Form):
    """A practice-linked enquiry. This form never creates a login or patient."""

    practice = forms.ModelChoiceField(queryset=Company.objects.none(), label='Practice', empty_label='Choose a practice')
    submission_token = forms.CharField(widget=forms.HiddenInput, max_length=8000)
    first_name = forms.CharField(max_length=150, label='First name')
    last_name = forms.CharField(max_length=150, label='Last name')
    email = forms.EmailField(label='Email address')
    phone = forms.CharField(max_length=32, label='Mobile number')
    id_number = forms.CharField(max_length=32, label='South African ID or passport number')
    height_cm = forms.DecimalField(label='Height (cm)', min_value=80, max_value=260, decimal_places=1, max_digits=5)
    weight_kg = forms.DecimalField(label='Weight (kg)', min_value=25, max_value=400, decimal_places=1, max_digits=6)
    adult = forms.ChoiceField(label='Are you 18 or older?', choices=(('yes', 'Yes'), ('no', 'No')), widget=forms.RadioSelect)
    weight_related_condition = forms.ChoiceField(
        label='Do you have a weight-related condition?', choices=(('yes', 'Yes'), ('no', 'No')), widget=forms.RadioSelect,
        help_text='High blood pressure, type 2 diabetes, sleep apnoea, fatty liver, high cholesterol.',
    )
    pregnancy = forms.ChoiceField(
        label='Are you pregnant, breastfeeding, or planning a pregnancy within a year?',
        choices=(('yes', 'Yes'), ('no', 'No')), widget=forms.RadioSelect,
    )
    thyroid_history = forms.ChoiceField(
        label='Any personal or family history of medullary thyroid cancer or MEN2?',
        choices=(('yes', 'Yes'), ('no', 'No'), ('unsure', 'Not sure')), widget=forms.RadioSelect,
    )
    pancreatitis = forms.ChoiceField(label='Have you ever had pancreatitis?', choices=(('yes', 'Yes'), ('no', 'No')), widget=forms.RadioSelect)
    health_context = forms.CharField(
        label='Chronic medication and allergies',
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={'rows': 4}),
        help_text='Include anything else you would like the clinician to know. Your wording is saved as entered.',
        strip=False,
    )
    service_consent = forms.BooleanField(label='I accept the terms of service, privacy notice and telehealth consent for my selected practice.')

    def __init__(self, *args, bound_practice=None, **kwargs):
        from practices.tenancy import enabled_companies, multi_practice_enabled

        self.bound_practice = bound_practice
        super().__init__(*args, **kwargs)
        self.fields['practice'].queryset = enabled_companies().order_by('name')
        if not multi_practice_enabled():
            self.fields['practice'].widget = forms.HiddenInput()
            self.initial.setdefault('practice', self.fields['practice'].queryset.values_list('pk', flat=True).first())
            self.fields['service_consent'].label = 'I accept the practice’s terms of service, privacy notice and telehealth consent.'
        if bound_practice:
            self.initial['practice'] = bound_practice.pk
            self.fields['practice'].queryset = self.fields['practice'].queryset.filter(pk=bound_practice.pk)

    def clean_practice(self):
        practice = self.cleaned_data['practice']
        if self.bound_practice and practice.pk != self.bound_practice.pk:
            raise ValidationError('This enquiry belongs to a different practice. Start a new questionnaire to choose another practice.')
        return practice

    def clean_email(self):
        return self.cleaned_data['email'].lower()

    def cleaned_answers(self):
        data = self.cleaned_data
        height_m = data['height_cm'] / 100
        bmi = (data['weight_kg'] / (height_m * height_m)).quantize(Decimal('0.01'))
        return {
            'adult': data['adult'],
            'weight_related_condition': data['weight_related_condition'],
            'pregnancy': data['pregnancy'],
            'thyroid_history': data['thyroid_history'],
            'pancreatitis': data['pancreatitis'],
            'height_cm': str(data['height_cm']),
            'weight_kg': str(data['weight_kg']),
            'health_context': data['health_context'],
            'service_consent': True,
            'telehealth_consent': True,
            'bmi': str(bmi),
        }
