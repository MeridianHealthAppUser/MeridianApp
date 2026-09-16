"""Separate privacy pages; no email delivery, automatic erasure or legal claims."""

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import F
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.models import (
    AuditEvent, ConsentDocument, ConsentRecord, PatientCommunicationPreference, PatientDataRequest,
    PatientDataRequestReply, PracticeSettings,
)
from care.privacy import (
    create_data_request, publish_policy_version, require_privacy_admin, respond_to_data_request,
    save_communication_preference,
)
from care.services import record_audit
from practices.models import Company
from .patient_care_views import PatientCarePage, add_error
from .privacy_forms import (
    AccessHistoryFilterForm, CommunicationPreferenceForm, DataRequestFilterForm, DataRequestForm,
    DataRequestReplyForm, PolicyVersionForm, PublicPracticeForm, privacy_context, validate_privacy_context,
)
from .record_views import AUDIT_LABELS, staff_memberships
from .views import StaffCompanyRequiredMixin


PRIVACY_AUDIT_LABELS = {
    **AUDIT_LABELS,
    'privacy.preference_updated': 'Communication preferences updated',
    'privacy.request_submitted': 'Privacy request submitted',
    'privacy.request_viewed': 'Privacy request viewed',
    'privacy.request_responded': 'Privacy request responded to',
    'privacy.policy_published': 'Policy version published',
    'account.password_changed': 'Account password changed',
}
TARGET_LABELS = {
    'practices.patient': 'Patient record', 'care.clinicalencounter': 'Consultation',
    'care.clinicalnote': 'Clinical note', 'care.labrequest': 'Blood test request',
    'care.labresult': 'Blood test report', 'care.patientdatarequest': 'Privacy request',
    'care.patientcommunicationpreference': 'Communication preference',
    'care.consentdocument': 'Policy document', 'care.appointment': 'Appointment',
    'care.messagethread': 'Conversation', 'care.patientmessage': 'Message',
}


def paginate(request, queryset, **filters):
    page = Paginator(queryset, 20).get_page(request.GET.get('page'))
    return {'page_obj': page, 'is_paginated': page.has_other_pages(),
            'pagination_query': urlencode({key: value for key, value in filters.items() if value})}


def safe_access_rows(queryset):
    return [{'created_at': row['created_at'], 'company_name': row['company__name'],
             'label': PRIVACY_AUDIT_LABELS.get(row['action'], 'Recorded account activity'),
             'target_label': TARGET_LABELS.get(row['target_type'], 'Record')}
            for row in queryset.values('created_at', 'company__name', 'action', 'target_type')]


def filtered_requests(request, queryset):
    form = DataRequestFilterForm(request.GET, auto_id='request_filter_%s')
    filters = {}
    if form.is_valid():
        filters = form.cleaned_data
        for key in ('status', 'kind'):
            if filters[key]:
                queryset = queryset.filter(**{key: filters[key]})
    else:
        queryset = queryset.none()
    return dict(paginate(request, queryset.order_by('-created_at', '-pk'), **filters), filter_form=form)


class PatientPrivacyView(PatientCarePage):
    def preference(self):
        return PatientCommunicationPreference.objects.filter(company=self.patient_company, patient=self.patient).first()

    def display(self, request, form=None):
        preference = self.preference()
        form = form if form is not None else CommunicationPreferenceForm(initial={
            'marketing_enabled': preference.marketing_enabled if preference else False,
        })
        token = request.POST.get('privacy_context') or privacy_context(
            request, self.patient_company, 'preference', record_id=self.patient.pk,
            version=preference.updated_at.isoformat() if preference else None,
        )
        context = self.context('account', 'Privacy and preferences', form=form, privacy_context=token,
            consents=ConsentRecord.objects.filter(company=self.patient_company, patient=self.patient).order_by('-created_at', '-pk'),
            access_rows=safe_access_rows(AuditEvent.objects.filter(company=self.patient_company, patient=self.patient,
                actor=request.user).order_by('-created_at', '-pk')[:10]))
        return render(request, 'portal/privacy_patient_preferences.html', context)

    def get(self, request):
        return self.display(request)

    def post(self, request):
        form = CommunicationPreferenceForm(request.POST)
        valid = form.is_valid()
        try:
            token = validate_privacy_context(request, self.patient_company, 'preference', record_id=self.patient.pk)
        except ValidationError as error:
            add_error(form, error)
            valid = False
        if valid:
            try:
                _, changed = save_communication_preference(actor=request.user, company=self.patient_company,
                    patient=self.patient, enabled=form.cleaned_data['marketing_enabled'], expected_updated_at=token.get('version'), request=request)
            except ValidationError as error:
                add_error(form, error)
            else:
                messages.success(request, 'Your preferences have been saved. Recorded consent is unchanged.' if changed else 'Your preferences are already up to date.')
                return redirect('portal:patient-privacy')
        return self.display(request, form)


class PatientDataRequestListView(PatientCarePage):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        context = self.context('account', 'Your privacy requests', **filtered_requests(request,
            PatientDataRequest.objects.filter(company=self.patient_company, patient=self.patient)))
        return render(request, 'portal/privacy_patient_requests.html', context)


class PatientDataRequestCreateView(PatientCarePage):
    def display(self, request, form=None):
        return render(request, 'portal/privacy_patient_request_form.html', self.context('account', 'New privacy request',
            form=form if form is not None else DataRequestForm(),
            privacy_context=request.POST.get('privacy_context') or privacy_context(request, self.patient_company, 'data-request', record_id=self.patient.pk)))

    def get(self, request):
        return self.display(request)

    def post(self, request):
        form = DataRequestForm(request.POST)
        valid = form.is_valid()
        try:
            token = validate_privacy_context(request, self.patient_company, 'data-request', record_id=self.patient.pk)
        except ValidationError as error:
            add_error(form, error)
            valid = False
        if valid:
            try:
                data_request, created = create_data_request(actor=request.user, company=self.patient_company, patient=self.patient,
                    kind=form.cleaned_data['kind'], description=form.cleaned_data['description'], submission_key=token.get('submission_key'), request=request)
            except ValidationError as error:
                add_error(form, error)
            else:
                messages.success(request, 'Your request is saved for the practice to review.' if created else 'This request was already submitted.')
                return redirect('portal:patient-data-request-detail', pk=data_request.pk)
        return self.display(request, form)


class PatientDataRequestDetailView(PatientCarePage):
    http_method_names = ('get', 'head', 'options')

    def get(self, request, pk):
        data_request = get_object_or_404(PatientDataRequest, pk=pk, company=self.patient_company, patient=self.patient)
        replies = PatientDataRequestReply.objects.filter(company=self.patient_company, data_request=data_request).select_related('author').order_by('created_at', 'pk')
        return render(request, 'portal/privacy_patient_request_detail.html', self.context('account', 'Your privacy request',
            data_request=data_request, **paginate(request, replies)))


@method_decorator(never_cache, name='dispatch')
class PrivacyStaffPage(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('get', 'post', 'head', 'options')

    def context(self, title, section='privacy_requests', **extra):
        return dict(company=self.company, active_membership=self.membership, page_title=title, nav_section=section, **extra)


class StaffDataRequestListView(PrivacyStaffPage):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        require_privacy_admin(request.user, self.company)
        context = self.context('Privacy requests', **filtered_requests(request,
            PatientDataRequest.objects.filter(company=self.company, patient__company=self.company).select_related('patient')))
        return render(request, 'portal/privacy_staff_requests.html', context)


class StaffDataRequestDetailView(PrivacyStaffPage):
    def record(self, pk):
        require_privacy_admin(self.request.user, self.company)
        return get_object_or_404(PatientDataRequest.objects.select_related('patient'), pk=pk,
                                company=self.company, patient__company=self.company)

    def display(self, request, data_request, form=None):
        replies = PatientDataRequestReply.objects.filter(company=self.company, data_request=data_request).select_related('author').order_by('created_at', 'pk')
        context = self.context('Privacy request', data_request=data_request,
            form=form if form is not None else DataRequestReplyForm(initial={'status': data_request.status}),
            privacy_context=request.POST.get('privacy_context') or privacy_context(request, self.company, 'data-response',
                record_id=data_request.pk, version=data_request.updated_at.isoformat()), **paginate(request, replies))
        return render(request, 'portal/privacy_staff_request_detail.html', context)

    def get(self, request, pk):
        data_request = self.record(pk)
        response = self.display(request, data_request)
        if request.method == 'GET':
            record_audit(company=self.company, actor=request.user, patient=data_request.patient, target=data_request,
                         action='privacy.request_viewed', request=request)
        return response

    def post(self, request, pk):
        data_request = self.record(pk)
        form = DataRequestReplyForm(request.POST)
        valid = form.is_valid()
        try:
            token = validate_privacy_context(request, self.company, 'data-response', record_id=data_request.pk)
        except ValidationError as error:
            add_error(form, error)
            valid = False
        if valid:
            try:
                _, created = respond_to_data_request(actor=request.user, company=self.company, data_request=data_request,
                    body=form.cleaned_data['body'], status=form.cleaned_data['status'], submission_key=token.get('submission_key'),
                    expected_updated_at=token.get('version'), request=request)
            except ValidationError as error:
                add_error(form, error)
            else:
                messages.success(request, 'Response saved and visible to the patient. No records were deleted or corrected automatically.' if created else 'This response was already submitted.')
                return redirect('portal:staff-data-request-detail', pk=data_request.pk)
        return self.display(request, data_request, form)


class AccountAccessHistoryView(PrivacyStaffPage):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        memberships = staff_memberships(request.user)
        data = request.GET.copy()
        data.setdefault('scope', 'current')
        form = AccessHistoryFilterForm(data)
        queryset = AuditEvent.objects.none()
        filters = {}
        if form.is_valid():
            filters = form.cleaned_data
            companies = memberships if filters['scope'] == 'all' else [self.company.pk]
            queryset = AuditEvent.objects.filter(actor=request.user, company_id__in=companies)
        context = self.context('My access history', section='access_history', filter_form=form,
            **paginate(request, queryset.order_by('-created_at', '-pk'), **filters))
        context['access_rows'] = safe_access_rows(context['page_obj'].object_list)
        return render(request, 'portal/privacy_access_history.html', context)


class PolicyListView(PrivacyStaffPage):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        require_privacy_admin(request.user, self.company, policies=True)
        docs = ConsentDocument.objects.filter(company=self.company).order_by('-effective_from', '-created_at', '-pk')
        context = self.context('Policy versions', section='policies', today=timezone.localdate(),
                               **paginate(request, docs))
        return render(request, 'portal/privacy_policy_list.html', context)


class PolicyCreateView(PrivacyStaffPage):
    def display(self, request, form=None):
        return render(request, 'portal/privacy_policy_form.html', self.context('Publish a policy version', section='policies',
            form=form if form is not None else PolicyVersionForm(initial={'effective_from': timezone.localdate()}),
            privacy_context=request.POST.get('privacy_context') or privacy_context(request, self.company, 'policy')))

    def get(self, request):
        require_privacy_admin(request.user, self.company, policies=True)
        return self.display(request)

    def post(self, request):
        require_privacy_admin(request.user, self.company, policies=True)
        form = PolicyVersionForm(request.POST)
        valid = form.is_valid()
        try:
            validate_privacy_context(request, self.company, 'policy')
        except ValidationError as error:
            add_error(form, error)
            valid = False
        if valid:
            values = form.cleaned_data.copy()
            values['confirmed'] = values.pop('confirm_publication')
            try:
                _, created = publish_policy_version(actor=request.user, company=self.company, request=request, **values)
            except ValidationError as error:
                add_error(form, error)
            else:
                messages.success(request, 'Policy version published. It becomes current on its effective date.' if created else 'This exact policy version is already published.')
                return redirect('portal:policy-list')
        return self.display(request, form)


@method_decorator(never_cache, name='dispatch')
class PublicPolicyView(View):
    http_method_names = ('get', 'head', 'options')
    page_kind = 'terms'

    def get(self, request):
        from practices.tenancy import enabled_companies

        data = request.GET.copy()
        if 'practice' not in data:
            first = enabled_companies().first()
            if first:
                data['practice'] = str(first.pk)
        form = PublicPracticeForm(data if data else None)
        company = document = practice_settings = None
        documents = []
        if form.is_valid():
            company = form.cleaned_data['practice']
            if self.page_kind == 'contact':
                practice_settings = PracticeSettings.objects.filter(company=company).first()
            else:
                document = ConsentDocument.objects.filter(company=company, kind=ConsentDocument.Kind.SERVICE,
                    is_active=True, effective_from__lte=timezone.localdate()).order_by('-effective_from', '-created_at', '-pk').first()
                # Treatment and marketing documents are also available as their
                # actual published text, without rewriting the combined notice.
                for kind in (ConsentDocument.Kind.TELEHEALTH, ConsentDocument.Kind.MARKETING):
                    extra = ConsentDocument.objects.filter(company=company, kind=kind, is_active=True,
                        effective_from__lte=timezone.localdate()).order_by('-effective_from', '-created_at', '-pk').first()
                    if extra:
                        documents.append(extra)
        return render(request, 'portal/privacy_public.html', {
            'practice_form': form, 'policy_company': company, 'document': document,
            'additional_documents': documents, 'practice_settings': practice_settings, 'public_page_kind': self.page_kind,
            'public_page_title': {'terms': 'Terms of service', 'privacy': 'Privacy notice', 'contact': 'Contact the practice'}[self.page_kind],
        })
