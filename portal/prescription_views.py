"""Initiate, renew or change a prescription and edit summary data from the patient record Overview."""

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.clinical_summary import FIELDS, edit_summary
from care.models import MedicationDose, StartingPlan, TreatmentAuthorization
from care.prescriptions import (
    DEFAULT_WEEKS, WEEK_CHOICES, change_prescription, current_prescription, plan_steps, prescription_options,
    renew_prescription, weeks_text,
)
from care.starting_plans import initiate_prescription, offered_plans
from practices.models import Patient

from .record_access import require_record_change
from .views import StaffCompanyRequiredMixin
from .workflow_context import validate_workflow_context


WEEKS = [(str(weeks), weeks_text(weeks)) for weeks in WEEK_CHOICES]


class RenewPrescriptionForm(forms.Form):
    current = forms.IntegerField(widget=forms.HiddenInput)
    weeks = forms.TypedChoiceField(choices=WEEKS, coerce=int, initial=str(DEFAULT_WEEKS), widget=forms.RadioSelect)
    acknowledge_flags = forms.BooleanField(required=False)


class ChangePrescriptionForm(forms.Form):
    current = forms.IntegerField(required=False, widget=forms.HiddenInput)
    medicine = forms.CharField(max_length=255)
    dose = forms.ModelChoiceField(queryset=MedicationDose.objects.none(), label='Dose')
    weeks = forms.TypedChoiceField(choices=WEEKS, coerce=int, initial=str(DEFAULT_WEEKS), widget=forms.RadioSelect)
    reason = forms.CharField(required=False, max_length=500, label='Reason')
    acknowledge_flags = forms.BooleanField(required=False)

    def __init__(self, *args, options, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['dose'].queryset = MedicationDose.objects.filter(
            pk__in=[dose.pk for doses in options.values() for dose in doses]).select_related('pen')

    def clean(self):
        data = super().clean()
        if data.get('dose') and data.get('medicine') and data['dose'].medicine != data['medicine']:
            self.add_error('dose', 'Choose a dose of the selected medicine.')
        return data


class InitiatePrescriptionForm(forms.Form):
    """Initiate prescription: a medicine, one of its starting plans, and how long the whole prescription runs."""

    current = forms.IntegerField(required=False, widget=forms.HiddenInput)
    medicine = forms.CharField(max_length=255)
    plan = forms.ModelChoiceField(queryset=StartingPlan.objects.none(), label='Starting plan',
                                  error_messages={'required': 'Choose a starting plan.'})
    weeks = forms.TypedChoiceField(choices=WEEKS, coerce=int, widget=forms.RadioSelect)
    reason = forms.CharField(required=False, max_length=500, label='Reason')
    acknowledge_flags = forms.BooleanField(required=False)

    def __init__(self, *args, plans, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['plan'].queryset = StartingPlan.objects.filter(pk__in=[plan.pk for plan in plans])

    def clean(self):
        data = super().clean()
        if data.get('plan') and data.get('medicine') and data['plan'].medicine != data['medicine']:
            self.add_error('plan', 'Choose a starting plan for the selected medicine.')
        return data


class SummaryEditForm(forms.Form):
    field = forms.ChoiceField(choices=list(FIELDS.items()))
    value = forms.CharField(required=False, max_length=2000, strip=False)


def render_overview(request, view, patient, status=400, **overrides):
    from .patient_workspace import workspace_tab_context

    context = workspace_tab_context(request, view.company, view.membership, patient, 'overview', **overrides)
    return render(request, 'portal/patient_workspace.html', context, status=status)


@method_decorator(never_cache, name='dispatch')
class RecordActionMixin(LoginRequiredMixin, StaffCompanyRequiredMixin):
    http_method_names = ('post',)

    def patient(self, pk):
        patient = get_object_or_404(Patient.objects.for_company(self.company).select_related('user', 'assigned_doctor'),
                                    pk=pk, is_active=True)
        require_record_change(self.membership, self.request.user, patient)
        return patient

    def current(self, patient, value):
        if not value:
            return None
        return TreatmentAuthorization.objects.for_company(self.company).filter(pk=value, patient=patient).first()

    def overview_url(self, patient, anchor):
        from .patient_workspace import workspace_url

        return f'{workspace_url(patient)}#{anchor}'


class PrescriptionRenewView(RecordActionMixin, View):
    def post(self, request, pk):
        if not self.membership.is_prescriber:
            raise PermissionDenied('Only a doctor can renew a prescription.')
        patient = self.patient(pk)
        form = RenewPrescriptionForm(request.POST)
        if form.is_valid():
            try:
                validate_workflow_context(request, self.company, 'patient-prescription', patient=patient)
                renewal = renew_prescription(
                    company=self.company, patient=patient, actor=request.user,
                    current=self.current(patient, form.cleaned_data['current']), weeks=form.cleaned_data['weeks'],
                    acknowledge_flags=form.cleaned_data['acknowledge_flags'], request=request,
                )
                messages.success(request, f'Prescription renewed to {renewal.expires_on:%-d %b %Y}. It is written to the clinical log under your name.')
                return redirect(self.overview_url(patient, 'record-prescription'))
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
        return render_overview(request, self, patient, renew_form=form, open_dialog='renew')


class PrescriptionChangeView(RecordActionMixin, View):
    def post(self, request, pk):
        if not self.membership.is_prescriber:
            raise PermissionDenied('Only a doctor can change or create a prescription.')
        patient = self.patient(pk)
        options = prescription_options(self.company, current_prescription(self.company, patient))
        form = ChangePrescriptionForm(request.POST, options=options)
        if form.is_valid():
            try:
                validate_workflow_context(request, self.company, 'patient-prescription', patient=patient)
                prescription = change_prescription(
                    company=self.company, patient=patient, actor=request.user,
                    current=self.current(patient, form.cleaned_data['current']), dose=form.cleaned_data['dose'],
                    weeks=form.cleaned_data['weeks'], reason=form.cleaned_data['reason'],
                    acknowledge_flags=form.cleaned_data['acknowledge_flags'], request=request,
                )
                messages.success(request, f'Prescription saved: {prescription.product.name} {prescription.max_dose} to '
                                          f'{prescription.expires_on:%-d %b %Y}. It is written to the clinical log under your name.')
                return redirect(self.overview_url(patient, 'record-prescription'))
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
        return render_overview(request, self, patient, change_form=form, open_dialog='change')


class PrescriptionInitiateView(RecordActionMixin, View):
    def post(self, request, pk):
        if not self.membership.is_prescriber:
            raise PermissionDenied('Only a doctor can initiate a prescription.')
        patient = self.patient(pk)
        form = InitiatePrescriptionForm(request.POST, plans=offered_plans(self.company))
        if form.is_valid():
            try:
                validate_workflow_context(request, self.company, 'patient-prescription', patient=patient)
                first = initiate_prescription(
                    company=self.company, patient=patient, actor=request.user,
                    current=self.current(patient, form.cleaned_data['current']), plan=form.cleaned_data['plan'],
                    weeks=form.cleaned_data['weeks'], reason=form.cleaned_data['reason'],
                    acknowledge_flags=form.cleaned_data['acknowledge_flags'], request=request,
                )
                messages.success(request, f'Prescription initiated to {plan_steps(first).last().expires_on:%-d %b %Y}, starting on '
                                          f'{first.product.name} {first.max_dose}. It is written to the clinical log under your name.')
                return redirect(self.overview_url(patient, 'record-prescription'))
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
        return render_overview(request, self, patient, initiate_form=form, open_dialog='initiate')


class SummaryEditView(RecordActionMixin, View):
    def post(self, request, pk):
        if not self.membership.has_clinical_access:
            raise PermissionDenied('Only a clinician or a Super Admin can edit summary data.')
        patient = self.patient(pk)
        form = SummaryEditForm(request.POST)
        if form.is_valid():
            try:
                validate_workflow_context(request, self.company, 'patient-summary', patient=patient)
                edit_summary(company=self.company, patient=patient, actor=request.user,
                             field=form.cleaned_data['field'], value=form.cleaned_data['value'], request=request)
                return redirect(self.overview_url(patient, 'record-summary'))
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
        return render_overview(request, self, patient, summary_error=' '.join(
            message for errors in form.errors.values() for message in errors), summary_error_field=request.POST.get('field', ''))
