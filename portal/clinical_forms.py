"""Narrow clinical forms, with user/practice/record-bound write contexts."""

import uuid

from django import forms
from django.core import signing
from django.core.exceptions import ValidationError
from django.utils import timezone

from care.models import Appointment
from practices.models import Patient


CLINICAL_CONTEXT_SALT = 'portal.clinical-form-context.v1'
CLINICAL_CONTEXT_MAX_AGE = 12 * 60 * 60
CONTEXT_ERROR = (
    'This form has expired or its practice or record has changed. '
    'No changes were saved. Reload the page and check the selected practice.'
)


def _context_scope(request, company, patient, kind, record):
    return dict(actor_id=request.user.pk, company_id=company.pk, patient_id=patient.pk,
                kind=kind, record_id=record.pk if record else None)


def make_clinical_context(request, company, patient, kind, record=None):
    data = _context_scope(request, company, patient, kind, record)
    data.update(revision=getattr(record, 'revision', None),
                submission_key=str(uuid.uuid4()) if record is None else None)
    return signing.dumps(data, salt=CLINICAL_CONTEXT_SALT)


def validate_clinical_context(request, company, patient, kind, record=None):
    token = request.POST.get('clinical_context', '')
    if not token or len(token) > 4096:
        raise ValidationError(CONTEXT_ERROR)
    try:
        data = signing.loads(token, salt=CLINICAL_CONTEXT_SALT, max_age=CLINICAL_CONTEXT_MAX_AGE)
    except (signing.BadSignature, TypeError, ValueError):
        raise ValidationError(CONTEXT_ERROR) from None
    scope = _context_scope(request, company, patient, kind, record)
    if not isinstance(data, dict) or any(data.get(key) != value for key, value in scope.items()):
        raise ValidationError(CONTEXT_ERROR)
    # The service compares the signed revision with the locked database row.
    # Never replace it with a fresh revision taken at POST time.
    return data


class ConsultationAppointmentChoice(forms.ModelChoiceField):
    def label_from_instance(self, appointment):
        starts = timezone.localtime(appointment.starts_at).strftime('%d %b %Y, %H:%M')
        return f'{starts} SAST · {appointment.get_appointment_type_display()} · {appointment.get_status_display()}'


class ConsultationForm(forms.Form):
    appointment = ConsultationAppointmentChoice(queryset=Appointment.objects.none(), required=False,
                                         empty_label='General consultation (no appointment link)')
    occurred_at = forms.DateTimeField(
        label='Consultation date and time (SAST)',
        widget=forms.DateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'type': 'datetime-local'}),
    )
    summary = forms.CharField(label='Consultation note', required=False, max_length=10000,
                              strip=False, widget=forms.Textarea(attrs={'rows': 12}))
    confirm_signature = forms.BooleanField(label='I confirm this is the final note', required=False)

    def __init__(self, *args, company, patient, actor, encounter=None, **kwargs):
        super().__init__(*args, **kwargs)
        appointments = Appointment.objects.for_company(company).filter(
            patient=patient, clinician=actor,
        ).order_by('-starts_at', '-pk')
        if encounter:
            # A draft keeps its original appointment association.
            appointments = appointments.filter(pk=encounter.appointment_id)
            self.fields['appointment'].disabled = True
            self.initial.update(appointment=encounter.appointment_id, occurred_at=encounter.occurred_at,
                                summary=encounter.clinical_summary)
        else:
            appointments = appointments.filter(encounter__isnull=True)
            self.initial.setdefault('occurred_at', timezone.localtime().replace(second=0, microsecond=0))
        self.fields['appointment'].queryset = appointments

    def clean_occurred_at(self):
        value = self.cleaned_data['occurred_at']
        if value > timezone.now():
            raise ValidationError('The consultation cannot be dated in the future.')
        return value


class LabRequestForm(forms.Form):
    panel_name = forms.CharField(label='Requested tests / panel', max_length=255)
    due_on = forms.DateField(label='Requested by (optional)', required=False,
                              widget=forms.DateInput(attrs={'type': 'date'}))


class LabUploadForm(forms.Form):
    report = forms.FileField(label='Blood-test report (PDF, up to 5 MB)',
                              widget=forms.ClearableFileInput(attrs={'accept': '.pdf,application/pdf'}))

    def clean_report(self):
        from care.clinical import MAX_LAB_RESULT_BYTES, validate_lab_pdf

        report = self.cleaned_data['report']
        if report.size > MAX_LAB_RESULT_BYTES:
            raise ValidationError('The report must be no larger than 5 MB.')
        filename, content, _ = validate_lab_pdf(report.name, report.read(MAX_LAB_RESULT_BYTES + 1))
        # Keep only validated bytes; do not save uploads under public media URLs.
        return {'filename': filename, 'content': content}


class LabReviewForm(forms.Form):
    review_note = forms.CharField(label='Clinical review note (internal)', max_length=10000,
                                  strip=False, widget=forms.Textarea(attrs={'rows': 6}))

    def clean_review_note(self):
        value = self.cleaned_data['review_note']
        if not value.strip():
            raise ValidationError('Add a clinical review note before completing the review.')
        return value


class ClinicalFilterForm(forms.Form):
    status = forms.ChoiceField()
    patient = forms.ModelChoiceField(queryset=Patient.objects.none(), required=False, empty_label='All patients')

    def __init__(self, *args, company, statuses, patient_portal=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['status'].choices = [('all', 'All statuses'), *statuses]
        if patient_portal:
            self.fields.pop('patient')
        else:
            self.fields['patient'].queryset = Patient.objects.for_company(company).filter(
                is_active=True,
            ).order_by('last_name', 'first_name', 'pk')
