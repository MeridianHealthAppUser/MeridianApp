"""Edit profile on the patient record: a Super Admin corrects the patient's details (care.patient_details)."""

from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.addresses import address_for
from care.patient_assignment import active_doctors
from care.patient_details import FIELDS, ONLY_SUPER_ADMIN, can_edit_details, changed_text, update_patient_details
from practices.models import Patient

from .record_access import RecordAccessDenied, require_record_view
from .views import StaffCompanyRequiredMixin
from .workflow_context import make_workflow_context, validate_workflow_context


class PatientDetailsForm(forms.Form):
    """The details the practice holds for the patient. care.patient_details checks them again when they're saved."""

    title = forms.ChoiceField(label='Title', choices=[('', 'Not recorded'), *Patient.Title.choices], required=False,
                              help_text='Printed on PathCare blood test forms.')
    first_name = forms.CharField(label='First name', max_length=150)
    last_name = forms.CharField(label='Last name', max_length=150)
    date_of_birth = forms.DateField(label='Date of birth', required=False,
                                    widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))
    id_number = forms.CharField(label='ID or passport number', max_length=32, required=False)
    phone = forms.CharField(label='Phone number', max_length=32, required=False,
                            help_text='Parcels use the contact number on the delivery address.')
    city = forms.CharField(label='City or town', max_length=120, required=False, help_text='Shown as Location on the record.')
    assigned_doctor = forms.ModelChoiceField(label='Prescribing doctor', queryset=None, required=False, empty_label='Unassigned')

    def __init__(self, *args, company, patient, **kwargs):
        super().__init__(*args, **kwargs)
        active = {doctor.pk for doctor in active_doctors(company)}
        field = self.fields['assigned_doctor']
        # A doctor no longer active stays listed while they're the patient's, so saving other details keeps them.
        field.queryset = get_user_model().objects.filter(pk__in=[*active, patient.assigned_doctor_id]).order_by(
            'first_name', 'last_name', 'email')
        field.label_from_instance = lambda user: user.full_name if user.pk in active else f'{user.full_name} (no longer active)'
        field.help_text = (f'Active clinicians at {company.name}. A doctor who writes a prescription becomes the '
                           'Prescribing doctor. Changing this keeps every past record, which stays visible to the new clinician.')


@method_decorator(never_cache, name='dispatch')
class PatientDetailsEditView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('get', 'post', 'head', 'options')
    page_title = 'Edit profile'

    def patient(self, pk):
        from .patient_workspace import workspace_url

        patient = get_object_or_404(Patient.objects.for_company(self.company).select_related('user', 'assigned_doctor'),
                                    pk=pk, is_active=True)
        require_record_view(self.membership, self.request.user, patient)
        if not can_edit_details(self.membership):
            raise RecordAccessDenied(ONLY_SUPER_ADMIN, workspace_url(patient))
        return patient

    def display(self, request, patient, form, token=None, status=200):
        from .patient_workspace import patient_workspace_context

        here = reverse('portal:patient-details-edit', args=[patient.pk])
        context = patient_workspace_context(request, self.company, self.membership, patient)
        context.update(
            page_title=self.page_title, form=form, address=address_for(patient),
            workflow_context=token or make_workflow_context(request, self.company, 'patient-details', patient),
            address_url=f'{reverse("portal:ops-delivery-address", args=[patient.pk])}?{urlencode({"next": here})}',
        )
        return render(request, 'portal/patient_details_form.html', context, status=status)

    def get(self, request, pk):
        patient = self.patient(pk)
        initial = {name: getattr(patient, name) for name in FIELDS}
        return self.display(request, patient, PatientDetailsForm(
            initial={**initial, 'assigned_doctor': patient.assigned_doctor_id}, company=self.company, patient=patient))

    def post(self, request, pk):
        from .patient_workspace import workspace_url

        patient = self.patient(pk)
        form = PatientDetailsForm(request.POST, company=self.company, patient=patient)
        if form.is_valid():
            try:
                validate_workflow_context(request, self.company, 'patient-details', patient)
                changed = update_patient_details(company=self.company, patient=patient, actor=request.user,
                                                 data=form.cleaned_data, request=request)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, f'Profile saved: the {changed_text(changed)} changed.' if changed else 'Nothing changed.')
                return redirect(workspace_url(patient))
        # The token from the page they had open, so a change made meanwhile in another tab is still caught.
        return self.display(request, patient, form, token=request.POST.get('workflow_context'), status=400)
