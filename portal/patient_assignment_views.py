from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from care.patient_assignment import active_doctors, assign_doctor, can_assign_doctor
from practices.models import Patient
from .views import StaffCompanyRequiredMixin
from .workflow_context import validate_workflow_context


class AssignedDoctorForm(forms.Form):
    doctor = forms.ModelChoiceField(label='Assigned clinician', queryset=None, required=False, empty_label='Unassigned')

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['doctor'].queryset = active_doctors(company)
        self.fields['doctor'].label_from_instance = lambda user: user.full_name


class PatientDoctorAssignmentView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('post',)

    def post(self, request, patient_pk):
        patient = get_object_or_404(Patient.objects.for_company(self.company).select_related('user', 'assigned_doctor'),
                                    pk=patient_pk, is_active=True)
        if not can_assign_doctor(self.membership):
            raise PermissionDenied('Only an active member of the care team can change the assigned clinician.')
        form = AssignedDoctorForm(request.POST, company=self.company)
        valid = form.is_valid()
        try:
            token = validate_workflow_context(request, self.company, 'patient-doctor-assignment', patient)
            if valid:
                assign_doctor(patient=patient, actor=request.user, doctor=form.cleaned_data['doctor'],
                              expected_updated=token['updated'], request=request)
                messages.success(request, 'Assigned clinician updated.')
                from .patient_workspace import workspace_url
                return redirect(workspace_url(patient))
        except ValidationError as error:
            for message in error.messages:
                form.add_error(None, message)
        from .patient_workspace import workspace_tab_context
        context = workspace_tab_context(request, self.company, self.membership, patient, 'overview',
                                        doctor_form=form, failed_form='doctor_form')
        return render(request, 'portal/patient_workspace.html', context, status=400)
