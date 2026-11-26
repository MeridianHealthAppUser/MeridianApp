"""Standalone clinical pages; private reports never have public media URLs."""

from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

from django import forms
from django.core.validators import MaxValueValidator, MinValueValidator

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Exists, OuterRef, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.clinical import mark_lab_done, review_lab_request, save_consultation, submit_lab_result, undo_lab_done
from care.models import ClinicalEncounter, LabRequest, LabResult, PathCareRequestForm
from care.patient_files import read_patient_file
from care.services import record_audit
from practices.models import CompanyMembership, Patient

from .filter_menus import (TickedChoiceField, choice_section, fields_section, list_tools, ticked_ids, ticked_sections,
                           without_all)
from .clinical_forms import (
    ClinicalFilterForm, ConsultationForm, LabReviewForm, LabUploadForm,
    make_clinical_context, validate_clinical_context,
)
from .patient_views import patient_page_context
from .record_access import require_record_change, require_record_notes, require_record_view
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

    @property
    def super_admin(self):
        return self.membership.role == CompanyMembership.Role.SUPER_ADMIN

    def require_clinician(self, owner_id=None):
        if not self.is_clinician or (owner_id is not None and owner_id != self.request.user.pk):
            raise PermissionDenied('Only the responsible clinician can change this clinical record.')

    def require_doctor(self, owner_id=None):
        if not self.is_doctor or (owner_id is not None and owner_id != self.request.user.pk):
            raise PermissionDenied('Only the responsible doctor can change this clinical record.')

    def context(self, **kwargs):
        return dict(company=self.company, active_membership=self.membership, is_doctor=self.is_doctor, is_clinician=self.is_clinician,
                    nav_section=self.nav_section, page_title=self.page_title, **kwargs)

    # What a save on this page needs: 'change' (the patient's current clinician) or 'notes' (also cover).
    record_write = 'change'

    def check_record(self, patient):
        """A clinician opens only the records of patients assigned to them, now or before, or booked with them,
        and changes only their current patients'. Admins and Super Admins aren't limited."""
        if self.request.method in ('GET', 'HEAD', 'OPTIONS'):
            require_record_view(self.membership, self.request.user, patient)
        elif self.record_write == 'notes':
            require_record_notes(self.membership, self.request.user, patient)
        else:
            require_record_change(self.membership, self.request.user, patient)
        return patient

    def patient_for(self, pk):
        return self.check_record(get_object_or_404(Patient.objects.for_company(self.company), pk=pk, is_active=True))

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
    clinician_redirect_message = 'Consultation notes are on each patient’s record, under Consultations.'

    def get(self, request):
        """The table with the standard tools at its top right (James, 20 Nov 2026): a search, as you type, for patient
        or clinician, and Filters for Status, Patient, Clinician and the consultation's date (one day, or a range).
        ?patient= still picks one."""
        data = without_all(request.GET, 'status')
        form = ConsultationFilterForm(data)
        patients, clinicians = ticked_ids(request.GET.getlist('patient')), ticked_ids(request.GET.getlist('clinician'))
        everyone = self.consultations().order_by('-occurred_at', '-pk')
        records = everyone
        if form.is_valid():
            for term in form.cleaned_data['q'].split():
                records = records.filter(Q(patient__first_name__icontains=term) | Q(patient__last_name__icontains=term)
                                         | Q(clinician__first_name__icontains=term) | Q(clinician__last_name__icontains=term))
            if form.cleaned_data['status']:
                records = records.filter(status__in=form.cleaned_data['status'])
            if patients:
                records = records.filter(patient_id__in=patients)
            if clinicians:
                records = records.filter(clinician_id__in=clinicians)
            first, last = form.cleaned_data['date_from'], form.cleaned_data['date_to']
            if first or last:
                # One date is that day; two are the days from one to the other, in the practice's time.
                first, last = first or last, last or first
                records = records.filter(occurred_at__gte=timezone.make_aware(datetime.combine(first, time.min)),
                                         occurred_at__lt=timezone.make_aware(datetime.combine(last + timedelta(days=1), time.min)))
        else:
            records = records.none()
        page = Paginator(records, 20).get_page(request.GET.get('page'))
        query = request.GET.copy()
        query.pop('page', None)
        people = Patient.objects.for_company(self.company).filter(pk__in=everyone.values('patient')).order_by('last_name', 'first_name', 'pk')
        staff = get_user_model().objects.filter(pk__in=everyone.values('clinician')).order_by('first_name', 'last_name', 'pk')
        rows = list(page.object_list)
        for row in rows:
            row.detail_url = reverse('portal:clinical-consultation-detail', args=[row.pk])
        count = page.paginator.count
        return render(request, 'portal/clinical_consultation_list.html', self.context(
            filter_form=form, page_obj=page, is_paginated=page.has_other_pages(), pagination_query=query.urlencode(),
            consultations=rows, can_create=self.is_clinician,
            # New consultation needs one patient, picked in Filters.
            selected_patient=people.filter(pk=patients[0]).first() if len(patients) == 1 else None,
            count_label=f'{count} record{"" if count == 1 else "s"}',
            **list_tools(action=reverse('portal:clinical-consultations'), data=request.GET, form=form,
                         label='Filter consultations', placeholder='Patient or clinician', sections=[
                             *ticked_sections(form, data, ('status',)),
                             choice_section('patient', 'Patient', [(person.pk, str(person)) for person in people], patients, icon='person'),
                             choice_section('clinician', 'Clinician', [(person.pk, person.full_name) for person in staff], clinicians, icon='person'),
                             fields_section('dates', 'Consultation date', [form['date_from'], form['date_to']],
                                            count=int(bool(data.get('date_from') or data.get('date_to')))),
                         ])))


def _date_field(label, **kwargs):
    return forms.DateField(label=label, required=False, widget=forms.DateInput(attrs={'type': 'date'}),
                           validators=[MinValueValidator(date(1900, 1, 1)), MaxValueValidator(date(2100, 12, 31))], **kwargs)


class ConsultationFilterForm(forms.Form):
    """Consultations: the search, and Filters' Status and date (Patient and Clinician are ticked ids)."""

    q = forms.CharField(label='Search consultations', required=False, max_length=100)
    status = TickedChoiceField(label='Status', choices=ClinicalEncounter.Status.choices)
    date_from = _date_field('From')
    date_to = _date_field('To', help_text='Leave empty for just the one day.')

    def clean(self):
        data = super().clean()
        if data.get('date_from') and data.get('date_to') and data['date_from'] > data['date_to']:
            self.add_error('date_to', 'The end date must be on or after the start date.')
        return data


class ConsultationEditorView(StaffClinicalView):
    nav_section = 'consultations'
    page_title = 'Consultation note'
    record_write = 'notes'  # A clinician covering an appointment writes the consultation note too.

    def record(self, patient_pk=None, pk=None):
        if pk is not None:
            encounter = get_object_or_404(self.consultations(), pk=pk)
            return self.check_record(encounter.patient), encounter
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
        form, raw = None, request.GET.get('appointment', '')
        if encounter is None and raw.isascii() and raw.isdecimal() and len(raw) < 19:
            # From an appointment's Write consultation note: it's chosen when it's yours and has no note yet.
            form = ConsultationForm(company=self.company, patient=patient, actor=request.user, initial={'appointment': int(raw)})
        return self.display(request, patient, encounter, form)

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
    clinician_redirect_message = 'Blood tests are on each patient’s record, under Blood tests.'

    def get(self, request):
        """The table with the standard tools at its top right (James, 20 Nov 2026): a search, as you type, for patient,
        tests or doctor, and Filters for Status, Patient, Doctor and a Required by range. ?patient= still picks one."""
        data = without_all(request.GET, 'status')
        form = LabFilterForm(data)
        patients, doctors = ticked_ids(request.GET.getlist('patient')), ticked_ids(request.GET.getlist('doctor'))
        labs = self.labs().order_by('-requested_on', '-pk')
        everyone = labs
        if form.is_valid():
            for term in form.cleaned_data['q'].split():
                labs = labs.filter(Q(patient__first_name__icontains=term) | Q(patient__last_name__icontains=term)
                                   | Q(panel_name__icontains=term) | Q(requested_by__first_name__icontains=term)
                                   | Q(requested_by__last_name__icontains=term))
            if form.cleaned_data['status']:
                labs = labs.filter(status__in=form.cleaned_data['status'])
            if patients:
                labs = labs.filter(patient_id__in=patients)
            if doctors:
                labs = labs.filter(requested_by_id__in=doctors)
            if form.cleaned_data['due_from']:
                labs = labs.filter(due_on__gte=form.cleaned_data['due_from'])
            if form.cleaned_data['due_to']:
                labs = labs.filter(due_on__lte=form.cleaned_data['due_to'])
        else:
            labs = labs.none()
        page = Paginator(labs, 20).get_page(request.GET.get('page'))
        query = request.GET.copy()
        query.pop('page', None)
        people = Patient.objects.for_company(self.company).filter(pk__in=everyone.values('patient')).order_by('last_name', 'first_name', 'pk')
        clinicians = get_user_model().objects.filter(pk__in=everyone.values('requested_by')).order_by('first_name', 'last_name', 'pk')
        rows = list(page.object_list)
        for row in rows:
            row.detail_url = reverse('portal:clinical-lab-detail', args=[row.pk])
        count = page.paginator.count
        dated = bool(data.get('due_from') or data.get('due_to'))
        return render(request, 'portal/clinical_lab_list.html', self.context(
            filter_form=form, page_obj=page, is_paginated=page.has_other_pages(), pagination_query=query.urlencode(),
            lab_requests=rows, can_create=self.is_doctor,
            # Request blood tests needs one patient, picked in Filters.
            selected_patient=people.filter(pk=patients[0]).first() if len(patients) == 1 else None,
            count_label=f'{count} request{"" if count == 1 else "s"}',
            **list_tools(action=reverse('portal:clinical-labs'), data=request.GET, form=form, label='Filter blood tests',
                         placeholder='Patient, test or doctor', sections=[
                             *ticked_sections(form, data, ('status',)),
                             choice_section('patient', 'Patient', [(person.pk, str(person)) for person in people], patients, icon='person'),
                             choice_section('doctor', 'Doctor', [(person.pk, person.full_name) for person in clinicians], doctors, icon='person'),
                             fields_section('required_by', 'Required by', [form['due_from'], form['due_to']], count=int(dated)),
                         ])))


class LabFilterForm(forms.Form):
    """Blood tests: the search, and Filters' Status and Required by range (Patient and Doctor are ticked ids)."""

    q = forms.CharField(label='Search blood tests', required=False, max_length=100)
    status = TickedChoiceField(label='Status', choices=LabRequest.Status.choices)
    due_from = _date_field('From')
    due_to = _date_field('To')

    def clean(self):
        data = super().clean()
        if data.get('due_from') and data.get('due_to') and data['due_from'] > data['due_to']:
            self.add_error('due_to', 'The end date must be on or after the start date.')
        return data


class LabCreateView(StaffClinicalView):
    """The old Request blood tests page. Blood tests are requested on the record's Overview, with the PathCare form and
    an optional Required by date, so this address takes doctors there."""
    http_method_names = ('get', 'head', 'options')

    def get(self, request, patient_pk):
        from .patient_workspace import workspace_url

        self.require_doctor()
        return redirect(f'{workspace_url(self.patient_for(patient_pk))}#record-bloods')


def _result_metadata(lab_request):
    return LabResult.objects.for_company(lab_request.company).filter(
        lab_request=lab_request, patient=lab_request.patient,
    ).first()


class LabDetailView(StaffClinicalView):
    nav_section = 'labs'
    page_title = 'Blood-test request'

    def display(self, request, lab_request, upload_form=None, review_form=None, status=200):
        from .patient_workspace import patient_workspace_context

        from care.blood_tests import pauses_on

        result = _result_metadata(lab_request)
        owner = self.is_doctor and lab_request.requested_by_id == request.user.pk
        state = lab_request.status
        context = self.context(
            lab_request=lab_request, patient=lab_request.patient, result=result, pauses_on=pauses_on(lab_request),
            can_upload=owner and result is None and state in (LabRequest.Status.REQUESTED, LabRequest.Status.DONE),
            # The doctor may have the results from the PathCare portal before the patient marks the tests Done.
            can_review=owner and (state in (LabRequest.Status.REQUESTED, LabRequest.Status.DONE) or (
                state == LabRequest.Status.UPLOADED and result is not None)),
            # Marked done by mistake: the requesting doctor or a Super Admin sets it back. The patient can't.
            can_undo_done=state == LabRequest.Status.DONE and (owner or self.membership.role == CompanyMembership.Role.SUPER_ADMIN),
            upload_form=upload_form if upload_form is not None else LabUploadForm(),
            review_form=review_form if review_form is not None else LabReviewForm(),
            clinical_context=_form_context(request, self.company, lab_request.patient, 'lab', lab_request),
            report_download_url=reverse('portal:clinical-lab-result-download', args=[lab_request.pk]) if result else '',
        )
        context.update(patient_workspace_context(request, self.company, self.membership, lab_request.patient, 'blood-tests'))
        return render(request, 'portal/clinical_lab_detail.html', context, status=status)

    def get(self, request, pk):
        lab_request = get_object_or_404(self.labs(), pk=pk)
        self.check_record(lab_request.patient)
        return self.display(request, lab_request)

    def post(self, request, pk):
        lab_request = get_object_or_404(self.labs(), pk=pk)
        self.check_record(lab_request.patient)
        action = request.POST.get('action', '')
        if action == 'undo_done':
            return self.undo_done(request, lab_request)
        self.require_doctor(lab_request.requested_by_id)
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

    def undo_done(self, request, lab_request):
        """Mark as not done, for the requesting doctor or a Super Admin (care.clinical.undo_lab_done checks)."""
        try:
            validate_clinical_context(request, self.company, lab_request.patient, 'lab', lab_request)
            undo_lab_done(lab_request=lab_request, actor=request.user, request=request)
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
        else:
            messages.success(request, f'Marked as not done. {lab_request.patient.first_name} is asked to mark them done '
                                      'once they’ve had them.')
        return redirect('portal:clinical-lab-detail', pk=lab_request.pk)


class PatientLabBase(LoginRequiredMixin, PatientPortalRequiredMixin, View):
    http_method_names = ('get', 'post', 'head', 'options')

    def labs(self):
        return LabRequest.objects.for_company(self.patient_company).filter(
            patient=self.patient,
        ).select_related('requested_by').defer('result_summary')

    def context(self, **kwargs):
        return dict(patient_page_context(self.request, self.patient_company, self.patient, 'labs', 'Your blood tests'), **kwargs)

    def pathcare_form(self, lab_request):
        """The PathCare form the doctor sent, until the patient has uploaded their results."""
        if lab_request.status != LabRequest.Status.REQUESTED or _result_metadata(lab_request) is not None:
            return None
        return PathCareRequestForm.objects.for_company(self.patient_company).filter(
            lab_request=lab_request, patient=self.patient,
        ).first()


class PatientLabListView(PatientLabBase):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        labs = self.labs().annotate(
            has_pathcare_form=Exists(PathCareRequestForm.objects.filter(lab_request=OuterRef('pk'))),
            has_result=Exists(LabResult.objects.filter(lab_request=OuterRef('pk'))),
        ).order_by('-requested_on', '-pk')
        context = self.context(**_listing(request, self.patient_company, labs, LabRequest.Status.choices, patient_portal=True))
        rows = list(context['page_obj'].object_list)
        for row in rows:
            row.detail_url = reverse('portal:patient-lab-detail', args=[row.pk])
            available = row.has_pathcare_form and not row.has_result and row.status == LabRequest.Status.REQUESTED
            row.form_url = reverse('portal:patient-lab-form', args=[row.pk]) if available else ''
        context['lab_requests'] = rows
        return render(request, 'portal/patient_lab_list.html', context)


class PatientLabDetailView(PatientLabBase):
    def display(self, request, lab_request, form=None, status=200):
        result = _result_metadata(lab_request)
        pathcare_form = self.pathcare_form(lab_request)
        return render(request, 'portal/patient_lab_detail.html', self.context(
            lab_request=lab_request, result=result, pathcare_form=pathcare_form,
            pathcare_form_url=reverse('portal:patient-lab-form', args=[lab_request.pk]) if pathcare_form else '',
            can_upload=result is None and lab_request.status in (LabRequest.Status.REQUESTED, LabRequest.Status.DONE),
            upload_form=form if form is not None else LabUploadForm(),
            clinical_context=_form_context(request, self.patient_company, self.patient, 'lab', lab_request),
            report_download_url=reverse('portal:patient-lab-result-download', args=[lab_request.pk]) if result else '',
        ), status=status)

    def get(self, request, pk):
        return self.display(request, get_object_or_404(self.labs(), pk=pk))

    def post(self, request, pk):
        lab_request = get_object_or_404(self.labs(), pk=pk)
        if request.POST.get('action') == 'done':
            return self.mark_done(request, lab_request)
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

    def mark_done(self, request, lab_request):
        """Done, from this page or from the task on My Tasks, which it goes back to."""
        try:
            validate_clinical_context(request, self.patient_company, self.patient, 'lab', lab_request)
            mark_lab_done(lab_request=lab_request, actor=request.user, request=request)
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
        else:
            messages.success(request, 'Thank you. Your doctor has been asked to review your blood tests.')
        if request.POST.get('next') == 'tasks':
            return redirect('portal:patient-treatment')
        return redirect('portal:patient-lab-detail', pk=lab_request.pk)


def _download(request, lab_request):
    # Authorise the enclosing request first. Reports are delivered only as
    # attachments, not inline scripts/documents or guessable storage links.
    result = get_object_or_404(LabResult.objects.for_company(lab_request.company),
                               lab_request=lab_request, patient=lab_request.patient)
    content = read_patient_file(result.file) if request.method != 'HEAD' else b''
    if request.method != 'HEAD':
        record_audit(company=lab_request.company, actor=request.user, patient=lab_request.patient,
                     action='lab_result.downloaded', target=result, request=request)
    response = HttpResponse(content, content_type='application/pdf')
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
        lab_request = get_object_or_404(self.labs(), pk=pk)
        self.check_record(lab_request.patient)
        return _download(request, lab_request)


class PatientLabResultDownloadView(PatientLabBase):
    http_method_names = ('get', 'head', 'options')

    def get(self, request, pk):
        return _download(request, get_object_or_404(self.labs(), pk=pk))


class PatientLabFormView(PatientLabBase):
    """The PathCare form exactly as it was emailed, in case the email went missing. It opens in the browser, or
    downloads with ?download=1. Once the results are uploaded it is no longer offered."""

    http_method_names = ('get', 'head', 'options')

    def get(self, request, pk):
        from care.pathcare import FILENAME

        lab_request = get_object_or_404(self.labs(), pk=pk)
        summary = self.pathcare_form(lab_request)
        if summary is None:
            raise Http404('This blood test form is no longer available.')
        form = summary
        content = read_patient_file(form.file) if request.method != 'HEAD' else b''
        if request.method != 'HEAD':
            record_audit(company=self.patient_company, actor=request.user, patient=self.patient,
                         action='pathcare_form.viewed', target=form, request=request)
        response = HttpResponse(content, content_type='application/pdf')
        disposition = 'attachment' if request.GET.get('download') == '1' else 'inline'
        response['Content-Disposition'] = f'{disposition}; filename="{FILENAME}"'
        response['Content-Length'] = str(form.size)
        response['Cache-Control'] = 'private, no-store, max-age=0'
        response['X-Content-Type-Options'] = 'nosniff'
        response['Referrer-Policy'] = 'same-origin'
        return response
