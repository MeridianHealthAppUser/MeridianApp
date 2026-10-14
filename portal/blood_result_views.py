"""The record's Blood results tab: results a clinician records by hand, and the lab's PDF if one was attached.

Most blood results are read on the PathCare portal. A result from another lab is recorded here, with the
date the blood was taken, the lab, the results as written, and optionally the lab's report.
"""

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.blood_results import record_blood_result
from care.models import BloodResult
from care.patient_files import read_patient_file
from care.services import record_audit
from practices.models import Patient

from .pathcare_views import _pdf_response
from .record_access import require_record_notes, require_record_view
from .views import StaffCompanyRequiredMixin


class BloodResultForm(forms.Form):
    taken_on = forms.DateField(label='Date taken', widget=forms.DateInput(attrs={'type': 'date'}))
    lab_name = forms.CharField(label='Lab', max_length=120, widget=forms.TextInput(attrs={'placeholder': 'e.g. Lancet'}))
    results = forms.CharField(label='Results', max_length=5000, widget=forms.Textarea(
        attrs={'rows': 5, 'placeholder': 'e.g. HbA1c 6.1%, ALT 32 U/L, TSH 2.1 mIU/L'}))
    report = forms.FileField(label='Lab report (optional PDF, up to 5 MB)', required=False,
                             widget=forms.ClearableFileInput(attrs={'accept': '.pdf,application/pdf'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['taken_on'].widget.attrs['max'] = timezone.localdate().isoformat()

    def clean_taken_on(self):
        value = self.cleaned_data['taken_on']
        if value > timezone.localdate():
            raise ValidationError('The date taken can’t be in the future.')
        return value

    def clean_report(self):
        from care.clinical import MAX_LAB_RESULT_BYTES, validate_lab_pdf

        report = self.cleaned_data.get('report')
        if not report:
            return None
        if report.size > MAX_LAB_RESULT_BYTES:
            raise ValidationError('The report must be no larger than 5 MB.')
        filename, content, _ = validate_lab_pdf(report.name, report.read(MAX_LAB_RESULT_BYTES + 1))
        return filename, content


def can_record_blood_result(membership, context):
    """Clinicians who write notes on this record: its clinician, or one covering an appointment."""
    return membership.is_clinician and context['can_write_notes']


def blood_results_context(request, company, membership, patient, context, **overrides):
    recordable = can_record_blood_result(membership, context)
    form = overrides.get('blood_result_form')
    return {
        'blood_results': list(BloodResult.objects.for_company(company).filter(patient=patient).select_related(
            'recorded_by', 'file').defer('file__content')[:100]),
        'can_record_blood_result': recordable,
        'blood_result_form': (form or BloodResultForm(auto_id='blood_result_%s')) if recordable else None,
        'blood_result_dialog_open': form is not None or request.GET.get('update') == 'blood-result',
    }


class BloodResultMixin(LoginRequiredMixin, StaffCompanyRequiredMixin):
    def patient(self, pk):
        if not self.membership.has_clinical_access:
            raise PermissionDenied('Blood results are available to clinicians and Super Admins only.')
        return get_object_or_404(Patient.objects.for_company(self.company), pk=pk, is_active=True)


class BloodResultCreateView(BloodResultMixin, View):
    """Record blood result, from its dialog on the Blood results tab."""

    http_method_names = ('post',)

    def post(self, request, pk):
        from .patient_workspace import workspace_tab_context, workspace_url

        patient = self.patient(pk)
        if not self.membership.is_clinician:
            raise PermissionDenied('Only a clinician can record a blood result.')
        require_record_notes(self.membership, request.user, patient)
        form = BloodResultForm(request.POST, request.FILES, auto_id='blood_result_%s')
        if form.is_valid():
            data = form.cleaned_data
            try:
                record_blood_result(company=self.company, patient=patient, actor=request.user, taken_on=data['taken_on'],
                                    lab_name=data['lab_name'], results=data['results'], report=data['report'], request=request)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, 'Blood result recorded.')
                return redirect(workspace_url(patient, 'blood-tests'))
        context = workspace_tab_context(request, self.company, self.membership, patient, 'blood-tests', blood_result_form=form)
        return render(request, 'portal/patient_workspace.html', context, status=400)


@method_decorator(never_cache, name='dispatch')
class BloodResultReportView(BloodResultMixin, View):
    """The lab's PDF attached to a recorded blood result."""

    http_method_names = ('get', 'head', 'options')

    def get(self, request, pk, result_pk):
        patient = self.patient(pk)
        require_record_view(self.membership, request.user, patient)
        result = get_object_or_404(BloodResult.objects.for_company(self.company).select_related('file'),
                                   pk=result_pk, patient=patient, file__isnull=False)
        content = read_patient_file(result.file)
        record_audit(company=self.company, actor=request.user, patient=patient, action='blood_result.report_viewed',
                     target=result, request=request)
        return _pdf_response(content, 'blood-result.pdf')
