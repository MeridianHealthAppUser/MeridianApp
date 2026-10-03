"""Standalone clinical pages; private reports never have public media URLs."""

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.clinical import create_lab_request, review_lab_request, save_consultation, submit_lab_result
from care.models import ClinicalEncounter, LabRequest, LabResult
from care.services import record_audit
from practices.models import CompanyMembership, Patient

from .clinical_forms import (
    ClinicalFilterForm, ConsultationForm, LabRequestForm, LabReviewForm, LabUploadForm,
    make_clinical_context, validate_clinical_context,
)
from .patient_views import patient_page_context
from .views import PatientPortalRequiredMixin, StaffCompanyRequiredMixin


class ClinicalAccessMixin:
    def dispatch(self, request, *args, **kwargs):
        if not self.membership.has_clinical_access:
            raise PermissionDenied('Clinical records are available to clinicians and practice Super Admins only.')
        return super().dispatch(request, *args, **kwargs)


@method_decorator(never_cache, name='dispatch')
class StaffClinicalView(LoginRequiredMixin, StaffCompanyRequiredMixin, ClinicalAccessMixin, View):
    http_method_names = ('get', 'post', 'head', 'options')
    nav_section = ''
    page_title = ''

    @property
    def is_clinician(self):
        return self.membership.is_clinician

    @property
    def is_doctor(self):
        # Blood tests, treatment and compounding stay with doctors.
        return self.membership.is_prescriber

    def require_clinician(self, owner_id=None):
        if not self.is_clinician or (owner_id is not None and owner_id != self.request.user.pk):
            raise PermissionDenied('Only the responsible clinician can change this clinical record.')

    def require_doctor(self, owner_id=None):
        if not self.is_doctor or (owner_id is not None and owner_id != self.request.user.pk):
            raise PermissionDenied('Only the responsible doctor can change this clinical record.')

    def context(self, **kwargs):
        return dict(company=self.company, active_membership=self.membership, is_doctor=self.is_doctor, is_clinician=self.is_clinician,
                    nav_section=self.nav_section, page_title=self.page_title, **kwargs)

    def patient_for(self, pk):
        return get_object_or_404(Patient.objects.for_company(self.company), pk=pk, is_active=True)

    def consultations(self):
        queryset = ClinicalEncounter.objects.for_company(self.company).filter(
            patient__company=self.company, patient__is_active=True,
        ).select_related('patient', 'clinician', 'signed_by', 'appointment')
        visible = Q(status=ClinicalEncounter.Status.SIGNED)
        if self.is_clinician:
            visible |= Q(clinician=self.request.user)
        return queryset.filter(visible)

    def labs(self):
        return LabRequest.objects.for_company(self.company).filter(
            patient__company=self.company, patient__is_active=True,
        ).select_related('patient', 'requested_by', 'reviewed_by')


def _listing(request, company, queryset, statuses, *, patient_portal=False):
    data = request.GET.copy()
    data.setdefault('status', 'all')
    form = ClinicalFilterForm(data, company=company, statuses=statuses,
                              patient_portal=patient_portal, auto_id='filter_%s')
    filters = {}
    if form.is_valid():
        status, patient = form.cleaned_data['status'], form.cleaned_data.get('patient')
        if status != 'all':
            queryset = queryset.filter(status=status)
        if patient:
            queryset = queryset.filter(patient=patient)
        filters = dict(status=status, patient=patient.pk if patient else '')
    else:
        queryset = queryset.none()
    page = Paginator(queryset, 20).get_page(request.GET.get('page'))
    return dict(filter_form=form, page_obj=page, is_paginated=page.has_other_pages(),
                pagination_query=urlencode({key: value for key, value in filters.items() if value != ''}))


def _error(form, error):
    for message in error.messages:
        form.add_error(None, message)


def _form_context(request, company, patient, kind, record=None):
    # Retain the signed create UUID/revision on validation failures. A stale
    # revision deliberately needs a reload, not a silent last-writer-wins save.
    return request.POST.get('clinical_context') or make_clinical_context(request, company, patient, kind, record)


class ConsultationListView(StaffClinicalView):
    http_method_names = ('get', 'head', 'options')
    nav_section = 'consultations'
    page_title = 'Consultation notes'

    def get(self, request):
        context = self.context(**_listing(request, self.company, self.consultations().order_by('-occurred_at', '-pk'),
                                          ClinicalEncounter.Status.choices), can_create=self.is_clinician)
        rows = list(context['page_obj'].object_list)
        for row in rows:
            row.detail_url = reverse('portal:clinical-consultation-detail', args=[row.pk])
        context['consultations'] = rows
        return render(request, 'portal/clinical_consultation_list.html', context)


class ConsultationEditorView(StaffClinicalView):
    nav_section = 'consultations'
    page_title = 'Consultation note'

    def record(self, patient_pk=None, pk=None):
        if pk is not None:
            encounter = get_object_or_404(self.consultations(), pk=pk)
            return encounter.patient, encounter
        self.require_clinician()
        return self.patient_for(patient_pk), None

    def display(self, request, patient, encounter, form=None, status=200):
        from .patient_workspace import patient_workspace_context

        can_edit = self.is_clinician and (encounter is None or (
            encounter.clinician_id == request.user.pk and encounter.status != ClinicalEncounter.Status.SIGNED
        ))
        if form is None:
            form = ConsultationForm(company=self.company, patient=patient, actor=request.user, encounter=encounter)
        context = self.context(patient=patient, encounter=encounter, form=form, can_edit=can_edit, can_sign=can_edit,
                               clinical_context=_form_context(request, self.company, patient, 'consultation', encounter))
        context.update(patient_workspace_context(request, self.company, self.membership, patient, 'consultations'))
        return render(request, 'portal/clinical_consultation_form.html', context, status=status)

    def get(self, request, patient_pk=None, pk=None):
        patient, encounter = self.record(patient_pk, pk)
        return self.display(request, patient, encounter)

    def post(self, request, patient_pk=None, pk=None):
        patient, encounter = self.record(patient_pk, pk)
        self.require_clinician(encounter.clinician_id if encounter else None)
        form = ConsultationForm(request.POST, company=self.company, patient=patient, actor=request.user, encounter=encounter)
        valid = form.is_valid()
        try:
            context = validate_clinical_context(request, self.company, patient, 'consultation', encounter)
            action = request.POST.get('action', '')
            if action not in ('save', 'sign'):
                raise ValidationError('Choose Save draft or Sign note.')
            if action == 'sign' and not form.cleaned_data.get('confirm_signature'):
                form.add_error('confirm_signature', 'Confirm that this is the final note before signing.')
                valid = False
            if valid:
                saved = save_consultation(
                    company=self.company, patient=patient, actor=request.user,
                    summary=form.cleaned_data['summary'], occurred_at=form.cleaned_data['occurred_at'],
                    appointment=form.cleaned_data['appointment'], encounter=encounter,
                    expected_revision=context.get('revision'), submission_key=context.get('submission_key'),
                    sign=action == 'sign', request=request,
                )
                messages.success(request, 'Consultation note signed and locked.' if saved.status == ClinicalEncounter.Status.SIGNED
                                 else 'Consultation draft saved.')
                return redirect('portal:clinical-consultation-detail', pk=saved.pk)
        except ValidationError as error:
            _error(form, error)
        return self.display(request, patient, encounter, form, status=400)


class LabListView(StaffClinicalView):
    http_method_names = ('get', 'head', 'options')
    nav_section = 'labs'
    page_title = 'Blood tests'

    def get(self, request):
        context = self.context(**_listing(request, self.company, self.labs().order_by('-requested_on', '-pk'),
                                          LabRequest.Status.choices), can_create=self.is_doctor)
        rows = list(context['page_obj'].object_list)
        for row in rows:
            row.detail_url = reverse('portal:clinical-lab-detail', args=[row.pk])
        context['lab_requests'] = rows
        return render(request, 'portal/clinical_lab_list.html', context)


class LabCreateView(StaffClinicalView):
    nav_section = 'labs'
    page_title = 'Request blood tests'

    def display(self, request, patient, form, status=200):
        from .patient_workspace import patient_workspace_context

        context = self.context(
            patient=patient, form=form, clinical_context=_form_context(request, self.company, patient, 'lab'),
        )
        context.update(patient_workspace_context(request, self.company, self.membership, patient, 'blood-tests'))
        return render(request, 'portal/clinical_lab_form.html', context, status=status)

    def get(self, request, patient_pk):
        self.require_doctor()
        return self.display(request, self.patient_for(patient_pk), LabRequestForm())

    def post(self, request, patient_pk):
        self.require_doctor()
        patient, form = self.patient_for(patient_pk), LabRequestForm(request.POST)
        valid = form.is_valid()
        try:
            context = validate_clinical_context(request, self.company, patient, 'lab')
            if valid:
                lab_request = create_lab_request(company=self.company, patient=patient, actor=request.user,
                                                 submission_key=context['submission_key'], request=request,
                                                 **form.cleaned_data)
                messages.success(request, 'Blood-test request saved. It is available in the patient portal.')
                return redirect('portal:clinical-lab-detail', pk=lab_request.pk)
        except ValidationError as error:
            _error(form, error)
        return self.display(request, patient, form, status=400)


def _result_metadata(lab_request):
    return LabResult.objects.for_company(lab_request.company).filter(
        lab_request=lab_request, patient=lab_request.patient,
    ).defer('content').first()


class LabDetailView(StaffClinicalView):
    nav_section = 'labs'
    page_title = 'Blood-test request'

    def display(self, request, lab_request, upload_form=None, review_form=None, status=200):
        from .patient_workspace import patient_workspace_context

        result = _result_metadata(lab_request)
        owner = self.is_doctor and lab_request.requested_by_id == request.user.pk
        context = self.context(
            lab_request=lab_request, patient=lab_request.patient, result=result,
            can_upload=owner and result is None and lab_request.status == LabRequest.Status.REQUESTED,
            can_review=owner and result is not None and lab_request.status == LabRequest.Status.UPLOADED,
            upload_form=upload_form if upload_form is not None else LabUploadForm(),
            review_form=review_form if review_form is not None else LabReviewForm(),
            clinical_context=_form_context(request, self.company, lab_request.patient, 'lab', lab_request),
            report_download_url=reverse('portal:clinical-lab-result-download', args=[lab_request.pk]) if result else '',
        )
        context.update(patient_workspace_context(request, self.company, self.membership, lab_request.patient, 'blood-tests'))
        return render(request, 'portal/clinical_lab_detail.html', context, status=status)

    def get(self, request, pk):
        return self.display(request, get_object_or_404(self.labs(), pk=pk))

    def post(self, request, pk):
        lab_request = get_object_or_404(self.labs(), pk=pk)
        self.require_doctor(lab_request.requested_by_id)
        action = request.POST.get('action', '')
        form = LabUploadForm(request.POST, request.FILES) if action == 'upload' else LabReviewForm(request.POST)
        valid = form.is_valid()
        try:
            validate_clinical_context(request, self.company, lab_request.patient, 'lab', lab_request)
            if action not in ('upload', 'review'):
                raise ValidationError('Choose Upload report or Complete clinical review.')
            if valid:
                if action == 'upload':
                    submit_lab_result(lab_request=lab_request, actor=request.user, request=request, **form.cleaned_data['report'])
                    messages.success(request, 'Report uploaded. A clinical review task is assigned to the requesting doctor.')
                else:
                    review_lab_request(lab_request=lab_request, actor=request.user, request=request, **form.cleaned_data)
                    messages.success(request, 'Clinical review recorded and the review task completed.')
                return redirect('portal:clinical-lab-detail', pk=lab_request.pk)
        except ValidationError as error:
            _error(form, error)
        return self.display(request, lab_request, status=400,
                            **{'upload_form' if action == 'upload' else 'review_form': form})


class PatientLabBase(LoginRequiredMixin, PatientPortalRequiredMixin, View):
    http_method_names = ('get', 'post', 'head', 'options')

    def labs(self):
        return LabRequest.objects.for_company(self.patient_company).filter(
            patient=self.patient,
        ).select_related('requested_by').defer('result_summary')

    def context(self, **kwargs):
        return dict(patient_page_context(self.request, self.patient_company, self.patient, 'labs', 'Your blood tests'), **kwargs)


class PatientLabListView(PatientLabBase):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        context = self.context(**_listing(request, self.patient_company, self.labs().order_by('-requested_on', '-pk'),
                                          LabRequest.Status.choices, patient_portal=True))
        rows = list(context['page_obj'].object_list)
        for row in rows:
            row.detail_url = reverse('portal:patient-lab-detail', args=[row.pk])
        context['lab_requests'] = rows
        return render(request, 'portal/patient_lab_list.html', context)


class PatientLabDetailView(PatientLabBase):
    def display(self, request, lab_request, form=None, status=200):
        result = _result_metadata(lab_request)
        return render(request, 'portal/patient_lab_detail.html', self.context(
            lab_request=lab_request, result=result,
            can_upload=result is None and lab_request.status == LabRequest.Status.REQUESTED,
            upload_form=form if form is not None else LabUploadForm(),
            clinical_context=_form_context(request, self.patient_company, self.patient, 'lab', lab_request),
            report_download_url=reverse('portal:patient-lab-result-download', args=[lab_request.pk]) if result else '',
        ), status=status)

    def get(self, request, pk):
        return self.display(request, get_object_or_404(self.labs(), pk=pk))

    def post(self, request, pk):
        lab_request = get_object_or_404(self.labs(), pk=pk)
        form = LabUploadForm(request.POST, request.FILES)
        valid = form.is_valid()
        try:
            validate_clinical_context(request, self.patient_company, self.patient, 'lab', lab_request)
            if request.POST.get('action', 'upload') != 'upload':
                raise PermissionDenied('Only your doctor can complete the clinical review.')
            if valid:
                submit_lab_result(lab_request=lab_request, actor=request.user, request=request, **form.cleaned_data['report'])
                messages.success(request, 'Your report was uploaded and sent to your doctor’s review queue.')
                return redirect('portal:patient-lab-detail', pk=lab_request.pk)
        except ValidationError as error:
            _error(form, error)
        return self.display(request, lab_request, form, status=400)


def _download(request, lab_request):
    # Authorise the enclosing request first. Reports are delivered only as
    # attachments, not inline scripts/documents or guessable storage links.
    result = get_object_or_404(LabResult.objects.for_company(lab_request.company),
                               lab_request=lab_request, patient=lab_request.patient)
    if request.method != 'HEAD':
        record_audit(company=lab_request.company, actor=request.user, patient=lab_request.patient,
                     action='lab_result.downloaded', target=result, request=request)
    response = HttpResponse(bytes(result.content) if request.method != 'HEAD' else b'', content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="lab-result-{lab_request.pk}.pdf"'
    response['Content-Length'] = str(result.size)
    response['Cache-Control'] = 'private, no-store, max-age=0'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Content-Security-Policy'] = "default-src 'none'; sandbox"
    response['Referrer-Policy'] = 'same-origin'
    return response


class LabResultDownloadView(StaffClinicalView):
    http_method_names = ('get', 'head', 'options')

    def get(self, request, pk):
        return _download(request, get_object_or_404(self.labs(), pk=pk))


class PatientLabResultDownloadView(PatientLabBase):
    http_method_names = ('get', 'head', 'options')

    def get(self, request, pk):
        return _download(request, get_object_or_404(self.labs(), pk=pk))
