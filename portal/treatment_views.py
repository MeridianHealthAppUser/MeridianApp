"""Separate authorisation and local-plan pages; no billing or email actions."""

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.models import AuditEvent, PatientSubscription, PracticeSettings, Shipment, TreatmentAuthorization
from care.treatment import (
    authorization_is_current, change_authorization_status, change_subscription_status,
    create_authorization, enroll_local_subscription, subscription_hold_reason,
)
from practices.models import CompanyMembership

from .clinical_views import StaffClinicalView, _error, _listing
from .patient_views import patient_page_context
from .treatment_forms import AuthorizationForm, ConfirmTreatmentForm, EnrollmentForm, make_treatment_context, validate_treatment_context
from .views import PatientPortalRequiredMixin, StaffCompanyRequiredMixin
from .patient_action_context import patient_action_context, workspace_redirect


def _authorizations(company, patient=None):
    records = TreatmentAuthorization.objects.for_company(company).filter(
        patient__company=company, patient__is_active=True, product__company=company,
    ).select_related('company', 'patient', 'product', 'prescribed_by').order_by('-created_at', '-pk')
    return records.filter(patient=patient) if patient else records


def _plans(company, patient=None):
    records = PatientSubscription.objects.for_company(company).filter(patient__company=company, patient__is_active=True).select_related(
        'company', 'patient', 'authorization__product', 'authorization__prescribed_by', 'authorization__company', 'authorization__patient',
    ).order_by('-created_at', '-pk')
    return records.filter(patient=patient) if patient else records


def _token(request, company, patient, kind, record=None):
    return request.POST.get('treatment_context') or make_treatment_context(request, company, patient, kind, record)


class AuthorizationListView(StaffClinicalView):
    http_method_names = ('get', 'head', 'options')
    nav_section = 'authorisations'
    page_title = 'Treatment authorisations'

    def get(self, request):
        listing = _listing(request, self.company, _authorizations(self.company), TreatmentAuthorization.Status.choices)
        patient = listing['filter_form'].cleaned_data.get('patient') if listing['filter_form'].is_valid() else None
        return render(request, 'portal/treatment_authorisations.html', self.context(**listing, selected_patient=patient))


class AuthorizationEditorView(StaffClinicalView):
    nav_section = 'authorisations'
    page_title = 'Authorise treatment'

    def records(self, patient_pk=None, pk=None):
        self.require_doctor()
        if pk is None:
            return self.patient_for(patient_pk), None
        previous = get_object_or_404(_authorizations(self.company), pk=pk)
        self.require_doctor(previous.prescribed_by_id)
        return previous.patient, previous

    def display(self, request, patient, previous, form, status=200):
        context = self.context(
            patient=patient, previous=previous, form=form,
            treatment_context=_token(request, self.company, patient, 'authorization-renew' if previous else 'authorization-create', previous),
        )
        context.update(patient_action_context(self, patient, 'treatment',
            content_template='portal/includes/authorisation_form_content.html', stylesheets=('css/treatment.css',),
            title='Review and renew' if previous else 'Authorise treatment'))
        return render(request, 'portal/patient_workspace_action.html' if context.get('patient_workspace') else 'portal/treatment_authorisation_form.html', context, status=status)

    def get(self, request, patient_pk=None, pk=None):
        patient, previous = self.records(patient_pk, pk)
        # No preselected medication, dose, expiry or review interval: this is a
        # new, explicit clinical decision, not an automatic prescription extension.
        return self.display(request, patient, previous, AuthorizationForm(company=self.company))

    def post(self, request, patient_pk=None, pk=None):
        patient, previous = self.records(patient_pk, pk)
        form = AuthorizationForm(request.POST, company=self.company)
        valid = form.is_valid()
        try:
            token = validate_treatment_context(request, self.company, patient, 'authorization-renew' if previous else 'authorization-create', previous)
            if valid:
                values = {key: value for key, value in form.cleaned_data.items() if key != 'confirm'}
                authorization = create_authorization(company=self.company, patient=patient, actor=request.user,
                                                     renews=previous, submission_key=token['submission_key'], request=request, **values)
                messages.success(request, 'Treatment authorisation recorded. No payment or dispatch was triggered.')
                return workspace_redirect(request, 'portal:treatment-authorisation-detail', pk=authorization.pk)
        except ValidationError as error:
            _error(form, error)
        return self.display(request, patient, previous, form, status=400)


class AuthorizationDetailView(StaffClinicalView):
    http_method_names = ('get', 'head', 'options')
    nav_section = 'authorisations'
    page_title = 'Treatment authorisation'

    def display(self, request, authorization, form=None, status=200):
        replacement_event = AuditEvent.objects.for_company(self.company).filter(
            action='treatment.authorized', metadata__previous_authorization_id=authorization.pk,
        ).first()
        replacement = None
        if replacement_event:
            replacement = _authorizations(self.company).filter(pk=replacement_event.target_id, patient=authorization.patient).first()
        context = self.context(
            authorization=authorization, patient=authorization.patient, replacement=replacement,
            is_current=authorization_is_current(authorization),
            can_change=self.is_doctor and authorization.prescribed_by_id == request.user.pk,
            form=form if form is not None else ConfirmTreatmentForm(),
            treatment_context=_token(request, self.company, authorization.patient, 'authorization-status', authorization),
        )
        context.update(patient_action_context(self, authorization.patient, 'treatment',
            content_template='portal/includes/authorisation_detail_content.html', stylesheets=('css/treatment.css',)))
        return render(request, 'portal/patient_workspace_action.html' if context.get('patient_workspace') else 'portal/treatment_authorisation_detail.html', context, status=status)

    def get(self, request, pk):
        return self.display(request, get_object_or_404(_authorizations(self.company), pk=pk))


class AuthorizationStatusView(AuthorizationDetailView):
    http_method_names = ('post', 'options')

    def post(self, request, pk):
        authorization = get_object_or_404(_authorizations(self.company), pk=pk)
        self.require_doctor(authorization.prescribed_by_id)
        form = ConfirmTreatmentForm(request.POST)
        valid = form.is_valid()
        try:
            validate_treatment_context(request, self.company, authorization.patient, 'authorization-status', authorization)
            if valid:
                change_authorization_status(authorization=authorization, actor=request.user, action=request.POST.get('action'), request=request)
                messages.success(request, 'Authorisation status updated. Affected undispatched parcels are held.')
                return workspace_redirect(request, 'portal:treatment-authorisation-detail', pk=pk)
        except ValidationError as error:
            _error(form, error)
        return self.display(request, authorization, form, status=400)


@method_decorator(never_cache, name='dispatch')
class SubscriptionListView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        listing = _listing(request, self.company, _plans(self.company), PatientSubscription.Status.choices)
        return render(request, 'portal/treatment_subscriptions.html', dict(
            company=self.company, active_membership=self.membership, nav_section='subscriptions',
            can_read_clinical=self.membership.role in (CompanyMembership.Role.DOCTOR, CompanyMembership.Role.SUPER_ADMIN), **listing,
        ))


def _eligible_authorizations(company, patient):
    today = timezone.localdate()
    # The service repeats all checks against locked records on submission.
    return _authorizations(company, patient).filter(
        status=TreatmentAuthorization.Status.ACTIVE, starts_on__lte=today, expires_on__gte=today,
        product__is_active=True, prescribed_by__is_active=True,
        prescribed_by__company_memberships__company=company,
        prescribed_by__company_memberships__role=CompanyMembership.Role.DOCTOR,
        prescribed_by__company_memberships__is_active=True,
    ).distinct()


def _compose_url(subject):
    return f"{reverse('portal:patient-messages')}?{urlencode({'compose': 1, 'subject': subject})}#patient-conversation"


HISTORY_PAGES = ('page', 'plans_page', 'shipments_page')


def _query_without(request, name):
    query = request.GET.copy()
    query.pop(name, None)
    return query.urlencode()


def patient_treatment_context(request, company, patient, *, enrollment_form=None, action_form=None, action_plan=None):
    """My Treatment: tasks, the record the doctor has on file, and the care plan."""
    from .patient_summary import (
        current_authorization, intake_answers, latest_weight, medical_information, medical_profile,
        next_consult, open_lab_requests, patient_tasks, pending_proposals,
    )

    context = patient_page_context(request, company, patient, 'treatment', 'My Treatment')
    current = _plans(company, patient).exclude(status=PatientSubscription.Status.CANCELLED).first()
    authorization = current_authorization(company, patient, current)
    consult = next_consult(request, company, patient)
    profile = medical_profile(company, patient)
    weight = latest_weight(company, patient)
    authorizations = Paginator(_authorizations(company, patient), 20).get_page(request.GET.get('page'))
    for item in authorizations.object_list:
        item.is_current = authorization_is_current(item)
    context.update(
        next_consult=consult,
        tasks=patient_tasks(
            authorization=authorization, plan=current, consult=consult, weight=weight, profile=profile,
            labs=open_lab_requests(company, patient), proposals=pending_proposals(request, company, patient),
        ),
        medical_rows=medical_information(profile=profile, intake=intake_answers(company, patient), weight=weight),
        medical_profile=profile,
        medical_change_url=_compose_url('Change to my medical information'),
        treatment_help_url=_compose_url('Something is not working with my treatment'),
        current_plan=current, current_authorization=authorization,
        plan_authorization=current.authorization if current and current.authorization_id else authorization,
        can_enroll=current is None and _eligible_authorizations(company, patient).exists(),
        history_open=any(request.GET.get(name) for name in HISTORY_PAGES),
        history_queries={name: _query_without(request, name) for name in HISTORY_PAGES},
        plan_hold_reason=subscription_hold_reason(current) if current else '',
        page_obj=authorizations,
        plans_page=Paginator(_plans(company, patient), 20).get_page(request.GET.get('plans_page')),
        shipments_page=Paginator(Shipment.objects.for_company(company).filter(patient=patient).order_by('-scheduled_for', '-pk'), 20).get_page(request.GET.get('shipments_page')),
        enrollment_form=enrollment_form if enrollment_form is not None else EnrollmentForm(authorizations=_eligible_authorizations(company, patient)),
        action_form=action_form if action_form is not None else ConfirmTreatmentForm(),
        action_plan=action_plan or current,
        enrollment_context=_token(request, company, patient, 'subscription-enroll') if enrollment_form is not None else make_treatment_context(request, company, patient, 'subscription-enroll'),
        action_context=_token(request, company, patient, 'subscription-status', action_plan or current) if action_form is not None else make_treatment_context(request, company, patient, 'subscription-status', current),
        plan_settings=PracticeSettings.objects.for_company(company).first(),
    )
    return context


class PatientTreatmentView(LoginRequiredMixin, PatientPortalRequiredMixin, View):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        return render(request, 'portal/treatment_patient.html', patient_treatment_context(request, self.patient_company, self.patient))


class PatientSubscriptionView(LoginRequiredMixin, PatientPortalRequiredMixin, View):
    """The care plan now lives on My Treatment; this keeps the old address and its forms working."""

    http_method_names = ('get', 'head', 'options')

    def eligible(self):
        return _eligible_authorizations(self.patient_company, self.patient)

    def display(self, request, enrollment_form=None, action_form=None, action_plan=None, status=200):
        context = patient_treatment_context(request, self.patient_company, self.patient, enrollment_form=enrollment_form,
                                            action_form=action_form, action_plan=action_plan)
        return render(request, 'portal/treatment_patient.html', context, status=status)

    def get(self, request):
        return redirect(reverse('portal:patient-treatment') + '#treatment-plan')


class PatientEnrollmentView(PatientSubscriptionView):
    http_method_names = ('post', 'options')

    def post(self, request):
        form = EnrollmentForm(request.POST, authorizations=self.eligible())
        valid = form.is_valid()
        try:
            token = validate_treatment_context(request, self.patient_company, self.patient, 'subscription-enroll')
            if valid:
                enroll_local_subscription(company=self.patient_company, patient=self.patient, actor=request.user,
                                          submission_key=token['submission_key'], request=request, **form.cleaned_data)
                messages.success(request, 'Local care plan enrolled. No payment was collected.')
                return redirect(reverse('portal:patient-treatment') + '#treatment-plan')
        except ValidationError as error:
            _error(form, error)
        return self.display(request, enrollment_form=form, status=400)


class PatientSubscriptionStatusView(PatientSubscriptionView):
    http_method_names = ('post', 'options')

    def post(self, request, pk):
        plan = get_object_or_404(_plans(self.patient_company, self.patient), pk=pk)
        form = ConfirmTreatmentForm(request.POST)
        valid = form.is_valid()
        try:
            validate_treatment_context(request, self.patient_company, self.patient, 'subscription-status', plan)
            if valid:
                change_subscription_status(subscription=plan, actor=request.user, action=request.POST.get('action'),
                                           confirm=form.cleaned_data['confirm'], request=request)
                messages.success(request, 'Local care plan status updated. No payment was processed.')
                return redirect(reverse('portal:patient-treatment') + '#treatment-plan')
        except ValidationError as error:
            _error(form, error)
        return self.display(request, action_form=form, action_plan=plan, status=400)
