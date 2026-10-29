"""Separate privacy pages; no email delivery, automatic erasure or legal claims."""

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponseGone
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.models import (
    AuditEvent, ConsentDocument, ConsentRecord, PatientCommunicationPreference, PatientDataRequest,
    PatientDataRequestReply, PracticeSettings,
)
from care.privacy import (
    create_data_request, publish_policy_version, request_details_change, require_privacy_admin, respond_to_data_request,
    withdraw_marketing_preference,
)
from care.services import ACCESS_ACTIONS, record_audit
from .patient_care_views import PatientCarePage, add_error
from .privacy_forms import (
    AccessHistoryFilterForm, DataRequestFilterForm, DataRequestForm, DetailsChangeForm,
    DataRequestReplyForm, PolicyVersionForm, PublicPracticeForm, privacy_context, validate_privacy_context,
)
from .record_views import ACCESS_AUDIT_LABELS, AUDIT_LABELS, staff_memberships
from .views import StaffCompanyRequiredMixin


PRIVACY_AUDIT_LABELS = {
    **AUDIT_LABELS,
    **ACCESS_AUDIT_LABELS,
    'message.sent': 'Secure message sent',
    'privacy.preference_updated': 'Communication preferences updated',
    'privacy.request_submitted': 'Privacy request submitted',
    'privacy.request_viewed': 'Privacy request viewed',
    'privacy.request_responded': 'Privacy request responded to',
    'privacy.policy_published': 'Policy version published',
    'account.password_changed': 'Account password changed',
    'account.password_reset': 'Account password reset',
    'account.sessions_ended': 'Signed out of other devices',
    'staff.details_updated': 'Staff details updated',
    'account.staff_details_updated': 'Own staff details updated',
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
            'pagination_query': urlencode({key: value for key, value in filters.items() if value}, doseq=True)}


def safe_access_rows(queryset):
    return [{'created_at': row['created_at'], 'company_name': row['company__name'],
             'label': PRIVACY_AUDIT_LABELS.get(row['action'], 'Recorded account activity'),
             'target_label': TARGET_LABELS.get(row['target_type'], 'Record')}
            for row in queryset.values('created_at', 'company__name', 'action', 'target_type')]


def filtered_requests(request, queryset):
    """Data requests for staff: a search by patient at the top right of the table, with Status and Type filters."""
    from .filter_menus import list_tools, ticked_sections

    form = DataRequestFilterForm(request.GET, auto_id='request_filter_%s')
    filters = {}
    if form.is_valid():
        filters = form.cleaned_data
        for term in filters['q'].split():
            queryset = queryset.filter(Q(patient__first_name__icontains=term) | Q(patient__last_name__icontains=term)
                                       | Q(patient__medical_record_number__icontains=term))
        for key in ('status', 'kind'):
            if filters[key]:
                queryset = queryset.filter(**{f'{key}__in': filters[key]})
    else:
        queryset = queryset.none()
    context = paginate(request, queryset.order_by('-created_at', '-pk'), **filters)
    count = context['page_obj'].paginator.count
    return dict(context, filter_form=form, count_label=f'{count} request{"" if count == 1 else "s"}', **list_tools(
        action=reverse('portal:staff-data-requests'), data=request.GET, form=form, label='Filter data requests',
        sections=ticked_sections(form, request.GET, ('status', 'kind'), icons={'kind': 'type'}), placeholder='Patient name'))


def patient_data_requests(request, company, patient):
    """My data requests on My profile, newest first, with Filters for Status and Type."""
    from .filter_menus import choice_section, filter_menu, ticked_values

    statuses = [value for value in ticked_values(request.GET.getlist('status')) if value in PatientDataRequest.Status.values]
    kinds = [value for value in ticked_values(request.GET.getlist('kind')) if value in PatientDataRequest.Kind.values]
    queryset = PatientDataRequest.objects.filter(company=company, patient=patient)
    if statuses:
        queryset = queryset.filter(status__in=statuses)
    if kinds:
        queryset = queryset.filter(kind__in=kinds)
    page = Paginator(queryset.order_by('-created_at', '-pk'), 20).get_page(request.GET.get('page'))
    action = f"{reverse('portal:patient-profile')}#profile-requests"
    return {'page_obj': page, 'pagination_query': urlencode({'status': statuses, 'kind': kinds}, doseq=True),
            'filter_menu': filter_menu(action=action, clear_url=action, label='Filter data requests', sections=[
                choice_section('status', 'Status', PatientDataRequest.Status.choices, statuses),
                choice_section('kind', 'Type', PatientDataRequest.Kind.choices, kinds, icon='type'),
            ])}


class PatientProfileView(PatientCarePage):
    """Personal details, account controls, care-email preferences, consents and requests.
    Requests to change personal details go to practice administrators through secure messaging."""

    def preference(self):
        return PatientCommunicationPreference.objects.filter(company=self.patient_company, patient=self.patient).first()

    def consents(self):
        """Recorded consents, each with the text Read opens: the version accepted, or for an older record without
        one, the practice's current published document of that kind."""
        consents = list(ConsentRecord.objects.filter(company=self.patient_company, patient=self.patient)
                        .select_related('document').order_by('-created_at', '-pk'))
        published = {}
        for consent in consents:
            if consent.document is None and consent.consent_type not in published:
                published[consent.consent_type] = ConsentDocument.objects.filter(
                    company=self.patient_company, kind=consent.consent_type, is_active=True,
                    effective_from__lte=timezone.localdate()).order_by('-effective_from', '-created_at', '-pk').first()
            consent.text = consent.document or published[consent.consent_type]
        return consents

    def display(self, request, request_form=None, change_form=None, withdrawal_errors=()):
        """A form that didn't save is shown again with its own signed context; the other gets a fresh one."""
        from care.pathcare import patient_address

        preference = self.preference()
        posted = request.POST.get('privacy_context', '')
        request_token = (posted if request_form is not None else '') or privacy_context(
            request, self.patient_company, 'data-request', record_id=self.patient.pk)
        change_token = (posted if change_form is not None else '') or privacy_context(
            request, self.patient_company, 'details-change', record_id=self.patient.pk)
        context = self.context('account', 'My profile',
            change_form=change_form if change_form is not None else DetailsChangeForm(auto_id='details_%s'),
            change_context=change_token,
            change_dialog_open=change_form is not None or request.GET.get('change') == '1',
            # New request opens its dialog; ?request=1 (the button without scripts) or a mistake opens it with the page.
            # ?kind=correction (from the medical profile) starts the dialog on that type.
            request_form=request_form if request_form is not None else DataRequestForm(initial={
                'kind': request.GET.get('kind') if request.GET.get('kind') in PatientDataRequest.Kind.values else None}),
            request_context=request_token,
            request_dialog_open=request_form is not None or request.GET.get('request') == '1',
            shipping_address=', '.join(part for part in patient_address(self.patient_company, self.patient) if part),
            can_withdraw_marketing=bool(preference and preference.marketing_enabled),
            withdrawal_context=privacy_context(request, self.patient_company, 'marketing-withdrawal', record_id=self.patient.pk),
            withdrawal_errors=withdrawal_errors,
            consents=self.consents(), journey_emails=not (preference and preference.journey_emails_off_at),
            **patient_data_requests(request, self.patient_company, self.patient))
        return render(request, 'portal/patient_profile.html', context)

    def get(self, request):
        return self.display(request)

    def post(self, request):
        section = request.POST.get('section', '')
        if section == 'withdraw-marketing':
            return self.withdraw_marketing(request)
        # The old unchecked preference form carried no checkbox value. Its valid
        # signed context can still withdraw, even if care-email preferences changed.
        if not section and 'privacy_context' in request.POST and 'marketing_enabled' not in request.POST:
            return self.withdraw_marketing(request, legacy=True)
        if 'marketing_enabled' in request.POST:
            return HttpResponseGone('Marketing preferences are no longer available here. Reload My profile to continue.')
        if request.POST.get('section') == 'journeys':
            return self.save_journey_emails(request)
        if request.POST.get('section') == 'details-change':
            return self.send_details_change(request)
        return HttpResponseGone('This form is no longer available. Reload My profile to continue.')

    def withdraw_marketing(self, request, *, legacy=False):
        try:
            validate_privacy_context(request, self.patient_company,
                'preference' if legacy else 'marketing-withdrawal', record_id=self.patient.pk)
            changed = withdraw_marketing_preference(actor=request.user, company=self.patient_company,
                                                   patient=self.patient, request=request)
        except ValidationError as error:
            return self.display(request, withdrawal_errors=error.messages)
        messages.success(request, 'Your marketing consent has been withdrawn. Your care-email preference is unchanged.'
            if changed else 'Marketing messages are already turned off.')
        return redirect(f"{reverse('portal:patient-profile')}#profile-consents")

    def send_details_change(self, request):
        form = DetailsChangeForm(request.POST, auto_id='details_%s')
        valid = form.is_valid()
        try:
            token = validate_privacy_context(request, self.patient_company, 'details-change', record_id=self.patient.pk)
        except ValidationError as error:
            add_error(form, error)
            valid = False
        if valid:
            try:
                thread, created = request_details_change(actor=request.user, company=self.patient_company,
                    patient=self.patient, description=form.cleaned_data['description'],
                    submission_key=token.get('submission_key'), request=request)
            except ValidationError as error:
                add_error(form, error)
            else:
                messages.success(request, 'Your request was sent to your practice administrators. You can follow their reply in My Messages.'
                    if created else 'This request was already sent. You can follow it in My Messages.')
                return redirect(f"{reverse('portal:patient-messages')}?thread={thread.pk}#patient-conversation")
        return self.display(request, change_form=form)


    def save_journey_emails(self, request):
        """Emails about your care (care.journeys): on unless the patient turned them off, here or from a link."""
        from care.models import PatientCommunicationPreference

        wanted = request.POST.get('journey_emails') == 'on'
        preference, _ = PatientCommunicationPreference.objects.get_or_create(company=self.patient_company, patient=self.patient)
        if wanted == (preference.journey_emails_off_at is None):
            messages.success(request, 'Your preferences are already up to date.')
        else:
            preference.journey_emails_off_at = None if wanted else timezone.now()
            preference.save(update_fields=('journey_emails_off_at', 'updated_at'))
            record_audit(company=self.patient_company, actor=request.user, patient=self.patient, target=preference,
                         action='privacy.preference_updated', request=request, metadata={'journey_emails': wanted})
            messages.success(request, 'Your email preferences have been saved.')
        return redirect(f"{reverse('portal:patient-profile')}#profile-journeys")


class PatientDataRequestCreateView(PatientProfileView):
    """A request from the New request dialog on My profile. This address also keeps old links working, opening it."""

    def get(self, request):
        return redirect(f"{reverse('portal:patient-profile')}?request=1#profile-requests")

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
                _, created = create_data_request(actor=request.user, company=self.patient_company, patient=self.patient,
                    kind=form.cleaned_data['kind'], description=form.cleaned_data['description'], submission_key=token.get('submission_key'), request=request)
            except ValidationError as error:
                add_error(form, error)
            else:
                messages.success(request, 'Your request is saved for the practice to review.' if created else 'This request was already submitted.')
                return redirect(f"{reverse('portal:patient-profile')}#profile-requests")
        return self.display(request, request_form=form)


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
        return get_object_or_404(PatientDataRequest.objects.select_related('patient', 'task__assigned_to'), pk=pk,
                                company=self.company, patient__company=self.company)

    def display(self, request, data_request, form=None):
        replies = PatientDataRequestReply.objects.filter(company=self.company, data_request=data_request).select_related('author').order_by('created_at', 'pk')
        context = self.context('Privacy request', data_request=data_request,
            form=form if form is not None else DataRequestReplyForm(),
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
        # Only Super Admins have an access history page; clinicians and practice administrators don't.
        if self.membership.role != 'super_admin':
            raise PermissionDenied('Your access history is available to Super Admins only.')
        memberships = staff_memberships(request.user)
        data = request.GET.copy()
        data.setdefault('scope', 'current')
        form = AccessHistoryFilterForm(data)
        queryset = AuditEvent.objects.none()
        filters = {}
        if form.is_valid():
            filters = form.cleaned_data
            companies = memberships if filters['scope'] == 'all' else [self.company.pk]
            # View, download and export events are never shown, even when the practice stores them.
            queryset = AuditEvent.objects.filter(actor=request.user, company_id__in=companies).exclude(action__in=ACCESS_ACTIONS)
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
