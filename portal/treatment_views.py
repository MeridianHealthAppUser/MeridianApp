"""Separate authorisation and local-plan pages; no billing or email actions."""

import hashlib
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.decorators.cache import never_cache

from care.billing import paid_before_pause
from care.prescriptions import ask_to_confirm, start_check
from care.followups import request_help
from care.models import AuditEvent, MedicationProduct, PatientSubscription, Shipment, TreatmentAuthorization
from care.treatment import (
    authorization_is_current, change_authorization_status, change_subscription_status, current_authorizations,
    create_authorization, dose_in_effect, enroll_local_subscription, other_treatments, subscription_hold_reason,
)
from .clinical_views import StaffClinicalView, _date_field, _error
from .filter_menus import TickedChoiceField, choice_section, fields_section, list_tools, ticked_ids, ticked_sections, without_all
from .patient_views import patient_page_context
from .treatment_forms import (
    AuthorizationForm, CancelSubscriptionForm, ConfirmTreatmentForm, EnrollmentForm, HelpRequestForm, make_treatment_context,
    validate_treatment_context,
)
from .navigation import messages_title
from .record_access import limited_by_assignment, no_records, openable_patients
from .views import PatientPortalRequiredMixin, StaffCompanyRequiredMixin
from practices.models import Patient
from .patient_action_context import patient_action_context, workspace_redirect


def _authorizations(company, patient=None):
    records = TreatmentAuthorization.objects.for_company(company).filter(
        patient__company=company, patient__is_active=True, product__company=company,
    ).select_related('company', 'patient', 'product', 'prescribed_by').order_by('-created_at', '-pk')
    return records.filter(patient=patient) if patient else records


def _plans(company, patient=None):
    records = PatientSubscription.objects.for_company(company).filter(patient__company=company, patient__is_active=True).select_related(
        'company', 'patient', 'patient__user', 'authorization__product', 'authorization__dose__pen', 'authorization__prescribed_by',
        'authorization__company', 'authorization__patient',
    ).order_by('-created_at', '-pk')
    return records.filter(patient=patient) if patient else records


def _token(request, company, patient, kind, record=None):
    return request.POST.get('treatment_context') or make_treatment_context(request, company, patient, kind, record)


# The toast after a patient pauses, resumes or cancels their subscription.
STATUS_MESSAGES = {
    'pause': 'Your subscription is paused. Your card isn’t charged while it’s paused, and a parcel you’ve already paid for still goes out.',
    'resume': 'Your subscription is active again.',
    'cancel': 'Your subscription is cancelled and your card won’t be charged for it again.',
}


class AuthorizationListView(StaffClinicalView):
    http_method_names = ('get', 'head', 'options')
    nav_section = 'authorisations'
    page_title = 'Treatment authorisations'
    # Doctors too (James, 26 Nov 2026: a doctor's Authorisation history button led nowhere), with only the patients
    # whose records they can open.

    def get(self, request):
        """The table with the standard tools at its top right (James, 20 Nov 2026): a search for patient, medicine or
        doctor, and Filters for Status, Patient, Doctor, Product, Maximum dose and Valid dates. ?patient= links from a
        patient's page still pick that patient."""
        data = without_all(request.GET, 'status')
        patients = (openable_patients(Patient.objects.for_company(self.company), request.user)
                    if limited_by_assignment(self.membership) else None)
        form, records, ticked = authorisation_listing(data, self.company, patients)
        page = Paginator(records, 20).get_page(request.GET.get('page'))
        states = authorization_states(self.company, page)
        for item in page:
            item.state = states[item.pk]
        query = request.GET.copy()
        query.pop('page', None)
        count = page.paginator.count
        everyone = _authorizations(self.company)
        if patients is not None:
            everyone = everyone.filter(patient__in=patients)
        people = Patient.objects.for_company(self.company).filter(pk__in=everyone.values('patient')).order_by('last_name', 'first_name', 'pk')
        doctors = get_user_model().objects.filter(pk__in=everyone.values('prescribed_by')).order_by('first_name', 'last_name', 'pk')
        products = MedicationProduct.objects.for_company(self.company).filter(pk__in=everyone.values('product')).order_by('name', 'strength', 'pk')
        # Authorise treatment needs one patient, picked in Filters.
        selected = people.filter(pk=ticked['patient'][0]).first() if len(ticked['patient']) == 1 else None
        dated = bool(data.get('valid_from') or data.get('valid_to'))
        return render(request, 'portal/treatment_authorisations.html', self.context(
            filter_form=form, page_obj=page, is_paginated=page.has_other_pages(), pagination_query=query.urlencode(),
            selected_patient=selected, other_treatments_offered=other_treatments(self.company).exists(),
            count_label=f'{count} authorisation{"" if count == 1 else "s"}',
            **list_tools(action=reverse('portal:treatment-authorisations'), data=request.GET, form=form,
                         label='Filter authorisations', placeholder='Patient, medicine or doctor', sections=[
                             *ticked_sections(form, data, ('status',)),
                             choice_section('patient', 'Patient', [(person.pk, str(person)) for person in people],
                                            ticked['patient'], icon='person'),
                             choice_section('doctor', 'Doctor', [(person.pk, person.full_name) for person in doctors],
                                            ticked['doctor'], icon='person'),
                             choice_section('product', 'Product', [(item.pk, f'{item.name} {item.strength}'.strip()) for item in products],
                                            ticked['product'], icon='type'),
                             *ticked_sections(form, data, ('dose',), icons={'dose': 'list'}),
                             fields_section('valid', 'Valid dates', [form['valid_from'], form['valid_to']], count=int(dated)),
                         ])))


def dose_key(label):
    """A maximum dose as a Filters choice: it's free text and may hold a comma, which Filters uses between choices."""
    return hashlib.sha1(label.encode()).hexdigest()[:10]


class AuthorisationFilterForm(forms.Form):
    """Authorisations: a search at the top right of the table, with Filters before it. Patient, Doctor and Product
    are ticked ids; Maximum dose ticks the practice's dose labels."""

    q = forms.CharField(label='Search authorisations', required=False, max_length=100)
    status = TickedChoiceField(label='Status', choices=TreatmentAuthorization.Status.choices)
    dose = TickedChoiceField(label='Maximum dose')
    valid_from = _date_field('From')
    valid_to = _date_field('To', help_text='Authorisations valid on any day from one date to the other.')

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        labels = sorted(set(_authorizations(company).values_list('max_dose', flat=True)), key=str.casefold)
        self.fields['dose'].choices = [(dose_key(label), label) for label in labels]

    def clean(self):
        data = super().clean()
        if data.get('valid_from') and data.get('valid_to') and data['valid_from'] > data['valid_to']:
            self.add_error('valid_to', 'The end date must be on or after the start date.')
        return data


def authorisation_listing(data, company, patients=None):
    """The filtered authorisations, and the ids ticked in Filters' Patient, Doctor and Product (any that aren't one
    are ignored). patients limits them, for a doctor, to the patients whose records they can open."""
    form = AuthorisationFilterForm(data, company=company)
    records = _authorizations(company)
    if patients is not None:
        records = records.filter(patient__in=patients)
    ticked = {name: ticked_ids(data.getlist(name)) for name in ('patient', 'doctor', 'product')}
    if not form.is_valid():
        return form, records.none(), ticked
    for term in form.cleaned_data['q'].split():
        records = records.filter(Q(patient__first_name__icontains=term) | Q(patient__last_name__icontains=term)
                                 | Q(product__name__icontains=term) | Q(prescribed_by__first_name__icontains=term)
                                 | Q(prescribed_by__last_name__icontains=term))
    if form.cleaned_data['status']:
        records = records.filter(status__in=form.cleaned_data['status'])
    if ticked['patient']:
        records = records.filter(patient_id__in=ticked['patient'])
    if ticked['doctor']:
        records = records.filter(prescribed_by_id__in=ticked['doctor'])
    if ticked['product']:
        records = records.filter(product_id__in=ticked['product'])
    if form.cleaned_data['dose']:
        labels = {key: label for key, label in form.fields['dose'].choices}
        records = records.filter(max_dose__in=[labels[key] for key in form.cleaned_data['dose']])
    # Valid on any day in the range: it starts by the range's end and expires on or after its start.
    if form.cleaned_data['valid_from']:
        records = records.filter(expires_on__gte=form.cleaned_data['valid_from'])
    if form.cleaned_data['valid_to']:
        records = records.filter(starts_on__lte=form.cleaned_data['valid_to'])
    return form, records, ticked


class AuthorizationEditorView(StaffClinicalView):
    nav_section = 'authorisations'
    page_title = 'Authorise treatment'

    def records(self, patient_pk=None, pk=None):
        if pk is None:
            self.require_doctor()
            return self.patient_for(patient_pk), None
        previous = get_object_or_404(_authorizations(self.company), pk=pk)
        self.check_record(previous.patient)
        # The prescribing doctor renews it, or the practice's Super Admin, doctor or not (James, 26 Nov 2026).
        if not self.super_admin:
            self.require_doctor(previous.prescribed_by_id)
        return previous.patient, previous

    def overview_instead(self, request, patient, previous):
        """Main treatments are prescribed, renewed and changed from the record's Overview (care.treatment.
        other_treatments), and so is everything when the catalogue has nothing else for this form."""
        offered = other_treatments(self.company)
        if offered.exists() and (previous is None or offered.filter(pk=previous.product_id).exists()):
            return None
        from .patient_workspace import workspace_url

        messages.info(request, 'Prescribe, renew or change a main treatment from the patient’s Overview.')
        return redirect(workspace_url(patient))

    def display(self, request, patient, previous, form, status=200):
        from care.clinical_summary import summary_safety_flags

        context = self.context(
            patient=patient, previous=previous, form=form, safety_flags=summary_safety_flags(self.company, patient),
            treatment_context=_token(request, self.company, patient, 'authorization-renew' if previous else 'authorization-create', previous),
        )
        context.update(patient_action_context(self, patient, 'treatment',
            content_template='portal/includes/authorisation_form_content.html', stylesheets=('css/treatment.css', 'css/record_overview.css'),
            title='Review and renew' if previous else 'Authorise treatment'))
        return render(request, 'portal/patient_workspace_action.html' if context.get('patient_workspace') else 'portal/treatment_authorisation_form.html', context, status=status)

    def get(self, request, patient_pk=None, pk=None):
        patient, previous = self.records(patient_pk, pk)
        if response := self.overview_instead(request, patient, previous):
            return response
        # No preselected medication, dose, expiry or review interval: this is a
        # new, explicit clinical decision, not an automatic prescription extension.
        return self.display(request, patient, previous, AuthorizationForm(company=self.company))

    def post(self, request, patient_pk=None, pk=None):
        patient, previous = self.records(patient_pk, pk)
        if response := self.overview_instead(request, patient, previous):
            return response
        form = AuthorizationForm(request.POST, company=self.company)
        valid = form.is_valid()
        try:
            token = validate_treatment_context(request, self.company, patient, 'authorization-renew' if previous else 'authorization-create', previous)
            if valid:
                values = {key: value for key, value in form.cleaned_data.items() if key != 'confirm'}
                authorization = create_authorization(company=self.company, patient=patient, actor=request.user,
                                                     renews=previous, submission_key=token['submission_key'], request=request, **values)
                messages.success(request, 'Treatment authorisation recorded.')
                return workspace_redirect(request, 'portal:treatment-authorisation-detail', pk=authorization.pk)
        except ValidationError as error:
            _error(form, error)
        return self.display(request, patient, previous, form, status=400)


def _state(authorization, replaced, revoke, today):
    if replaced:
        return {'key': 'replaced', 'label': 'Replaced', 'tone': 'none', 'note': None}
    if authorization.status == TreatmentAuthorization.Status.CANCELLED:
        if revoke is not None:
            by = f' by {revoke.actor.titled_name}' if revoke.actor_id else ''
            return {'key': 'revoked', 'label': 'Revoked', 'tone': 'cancelled', 'warning': True,
                    'note': f'Revoked on {timezone.localdate(revoke.created_at):%-d %b %Y}{by}, so it is no longer valid for dispensing.'}
        return {'key': 'cancelled', 'label': 'Cancelled', 'tone': 'cancelled', 'warning': True,
                'note': 'Cancelled, so it is not valid for dispensing.'}
    if authorization.expires_on < today:
        return {'key': 'expired', 'label': 'Expired', 'tone': 'none', 'note': f'Expired on {authorization.expires_on:%-d %b %Y}.'}
    if authorization.status == TreatmentAuthorization.Status.PAUSED:
        return {'key': 'paused', 'label': 'Paused', 'tone': 'paused', 'warning': True,
                'note': 'Paused, so it is not valid for dispensing and the patient’s parcels are held. Restarting treatment needs a new prescription.'}
    if authorization.starts_on > today:
        step = ', the next step of the starting plan' if authorization.plan_step and authorization.plan_step > 1 else ''
        return {'key': 'scheduled', 'label': f'Starts {authorization.starts_on:%-d %b}', 'tone': '',
                'note': f'Starts on {authorization.starts_on:%-d %b %Y}{step}.'}
    return {'key': 'active', 'label': 'Active', 'tone': 'active', 'note': None}


def authorization_states(company, authorizations, today=None):
    """Where each authorisation stands (James, 26 Nov 2026), by pk: a key, a label and a tone for its pill, and a line
    saying why it is or isn't valid for dispensing (None when it simply is). The stored status alone misleads: a dose
    change or renewal cancels the one it replaces ("Replaced"), a revoke cancels it too ("Revoked"), and an active one
    past its end date has "Expired"."""
    today = today or timezone.localdate()
    records = list(authorizations)
    ids = [authorization.pk for authorization in records]
    replaced = set(TreatmentAuthorization.objects.filter(company=company, replaces_id__in=ids).values_list('replaces_id', flat=True))
    replaced.update(AuditEvent.objects.filter(company=company, action='treatment.authorized',
                                              metadata__previous_authorization_id__in=ids).values_list(
        'metadata__previous_authorization_id', flat=True))
    revokes = {}
    for event in AuditEvent.objects.filter(company=company, action='treatment.revoke', target_type='care.treatmentauthorization',
                                           target_id__in=[str(pk) for pk in ids]).select_related('actor').order_by('created_at'):
        revokes[int(event.target_id)] = event
    return {authorization.pk: _state(authorization, authorization.pk in replaced, revokes.get(authorization.pk), today)
            for authorization in records}


def authorization_state(company, authorization, replacement=None, today=None):
    """authorization_states for one authorisation, whose replacement (if any) the page has already found."""
    state = authorization_states(company, [authorization], today)[authorization.pk]
    return _state(authorization, True, None, today or timezone.localdate()) if replacement is not None else state


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
        if replacement is None:  # Renewed or changed from the Overview (care.prescriptions).
            replacement = _authorizations(self.company).filter(replaces=authorization, patient=authorization.patient).order_by('starts_on', 'pk').first()
        from .patient_workspace import workspace_url

        state = authorization_state(self.company, authorization, replacement)
        # The prescribing doctor manages it, and so does the practice's Super Admin, doctor or not (James, 26 Nov 2026):
        # renewing, pausing and revoking, only while it can still be paused or revoked.
        can_change = self.super_admin or (self.is_doctor and authorization.prescribed_by_id == request.user.pk)
        open_state = state['key'] in ('active', 'scheduled', 'paused')
        # A main treatment is renewed from the Overview (James asked to drop the link to it, 26 Nov 2026); only
        # compounded and other treatments here.
        renewable_here = not replacement and other_treatments(self.company).filter(pk=authorization.product_id).exists()
        context = self.context(
            authorization=authorization, patient=authorization.patient, replacement=replacement, state=state,
            is_current=authorization_is_current(authorization), renewable_here=renewable_here,
            overview_url=workspace_url(authorization.patient), can_change=can_change, manageable=can_change and open_state,
            # Nothing to manage (replaced, revoked, expired): no Manage card, the details take the page.
            show_manage=(can_change and (open_state or renewable_here)) or (not can_change and open_state),
            form=form if form is not None else ConfirmTreatmentForm(),
            treatment_context=_token(request, self.company, authorization.patient, 'authorization-status', authorization),
        )
        context.update(patient_action_context(self, authorization.patient, 'treatment',
            content_template='portal/includes/authorisation_detail_content.html', stylesheets=('css/treatment.css',)))
        return render(request, 'portal/patient_workspace_action.html' if context.get('patient_workspace') else 'portal/treatment_authorisation_detail.html', context, status=status)

    def get(self, request, pk):
        authorization = get_object_or_404(_authorizations(self.company), pk=pk)
        self.check_record(authorization.patient)
        return self.display(request, authorization)


class AuthorizationStatusView(AuthorizationDetailView):
    http_method_names = ('post', 'options')

    def post(self, request, pk):
        authorization = get_object_or_404(_authorizations(self.company), pk=pk)
        self.check_record(authorization.patient)
        if not self.super_admin:
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
    admin_only = True
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        form, plans = subscription_listing(request.GET, self.company)
        page = Paginator(plans, 20).get_page(request.GET.get('page'))
        # Payments moved to Shipping > Billing; each row has its weekly timeline instead (James, 26 Nov 2026).
        page.object_list = list(prepare_subscription_rows(page.object_list, self.company, payments=False))
        query = request.GET.copy()
        query.pop('page', None)
        count = page.paginator.count
        return render(request, 'portal/treatment_subscriptions.html', dict(
            company=self.company, active_membership=self.membership, nav_section='subscriptions',
            messages_title=messages_title(self.membership),
            # A name opens the patient's record for those who can open records (not practice administrators).
            patient_links=not no_records(self.membership),
            can_read_clinical=self.membership.has_clinical_access, filter_form=form, page_obj=page,
            is_paginated=page.has_other_pages(), pagination_query=query.urlencode(),
            count_label=f'{count} care plan{"" if count == 1 else "s"}',
            **list_tools(action=reverse('portal:treatment-subscriptions'), data=request.GET, form=form,
                         label='Filter subscriptions', sections=ticked_sections(form, request.GET, ('status',)),
                         placeholder='Patient or plan'),
        ))


@method_decorator(never_cache, name='dispatch')
class SubscriptionTimelineView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    """A subscription's treatment week by week, as the patient sees it on My Treatment, for the practice (James,
    26 Nov 2026): View timeline on Patient orders > Subscriptions and on the record's Treatment tab. ?fragment=1 is the
    dialog's content alone (static/js/subscription_timeline.js); without scripts, a page with it open. Practice
    administrators and Super Admins open any; a doctor, those of the patients whose records they can open."""

    http_method_names = ('get', 'head', 'options')

    def get(self, request, pk):
        from .patient_summary import treatment_timeline
        from .patient_workspace import workspace_url
        from .record_access import require_record_view

        plan = get_object_or_404(_plans(self.company), pk=pk)
        if not self.membership.is_admin:
            if not self.membership.has_clinical_access:
                raise PermissionDenied('A subscription’s timeline is for the practice team and the patient’s doctors.')
            require_record_view(self.membership, request.user, plan.patient)
        back = request.GET.get('return_to', '')
        if not back.startswith('/') or back.startswith('//') or not url_has_allowed_host_and_scheme(
                back, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
            back = reverse('portal:treatment-subscriptions') if self.membership.is_admin else workspace_url(plan.patient, 'treatment')
        context = dict(company=self.company, active_membership=self.membership, nav_section='subscriptions',
                       timeline_plan=plan, timeline_return=back,
                       timeline=treatment_timeline(self.company, plan.patient, plan.authorization) if plan.authorization_id else None)
        if request.GET.get('fragment') == '1':
            return render(request, 'portal/includes/staff_treatment_timeline.html', context)
        return render(request, 'portal/subscription_timeline.html', context)


class SubscriptionFilterForm(forms.Form):
    """Subscriptions: a search at the top right of the table, with a Status filter before it."""

    q = forms.CharField(label='Search subscriptions', required=False, max_length=100)
    status = TickedChoiceField(label='Status', choices=PatientSubscription.Status.choices)


def subscription_listing(data, company):
    """The unpaginated subscription selection shared by the table and its download."""
    form = SubscriptionFilterForm(without_all(data, 'status'))
    plans = _plans(company)
    if form.is_valid():
        for term in form.cleaned_data['q'].split():
            plans = plans.filter(Q(patient__first_name__icontains=term) | Q(patient__last_name__icontains=term)
                                 | Q(plan_name__icontains=term))
        if form.cleaned_data['status']:
            plans = plans.filter(status__in=form.cleaned_data['status'])
    else:
        plans = plans.none()
    return form, plans


def prepare_subscription_rows(plans, company, *, payments=True):
    """Attach the current course's supply progress without creating or changing parcels, and (payments) the payment
    schedule's progress, which the download has."""
    from care.operations import dose_run

    for plan in plans:
        plan.dose = dose_in_effect(plan)
        if payments:
            from care.subscription_discounts import payment_schedule
            plan.payment_progress = payment_schedule(plan)
        plan.supply_dispatched = plan.supply_remaining = 0
        plan.next_shipment = plan.last_before_pause = None
        if plan.authorization_id:
            course = dose_run(plan.authorization)
            course_filter = Q(authorization_id__in=[item.pk for item in course])
            plan_keys = {item.plan_key for item in course if item.plan_key}
            if plan_keys:
                course_filter |= Q(authorization__plan_key__in=plan_keys)
            parcels = Shipment.objects.for_company(company).filter(
                course_filter, subscription=plan, patient=plan.patient,
            ).exclude(status=Shipment.Status.CANCELLED).order_by('scheduled_for', 'pk')
            for parcel in parcels:
                if parcel.status in (Shipment.Status.DISPATCHED, Shipment.Status.DELIVERED):
                    plan.supply_dispatched += 1
                else:
                    plan.supply_remaining += 1
                    if plan.next_shipment is None:
                        plan.next_shipment = parcel
                    # Paused by the patient: the parcel already paid for still goes out, the last until they resume.
                    if (plan.status == PatientSubscription.Status.PAUSED and plan.last_before_pause is None
                            and paid_before_pause(parcel)):
                        plan.last_before_pause = parcel
        yield plan


def _enrollment_form(company, patient):
    """Start your subscription. With one current authorisation, it starts chosen; the address is the one on file."""
    from care.addresses import address_for

    eligible = _eligible_authorizations(company, patient)
    only = list(eligible[:2])
    return EnrollmentForm(authorizations=eligible, initial={'authorization': only[0].pk} if len(only) == 1 else None,
                          instance_address=address_for(patient), patient=patient, for_patient=True)


def _eligible_authorizations(company, patient):
    # The service repeats all checks against locked records on submission.
    return current_authorizations(_authorizations(company, patient))


def patient_treatment_context(request, company, patient, *, enrollment_form=None, action_form=None, action_plan=None,
                              cancel_form=None, help_form=None):
    """My Treatment: tasks, the record the doctor has on file, the care plan and once-off items. Something is wrong and
    Cancel my subscription each open a dialog, which opens again after a mistake (or from ?help=1 and ?cancel=1
    without scripts)."""
    from .patient_summary import (
        billing_tasks, current_authorization, latest_weight,
        next_consult, patient_tasks, task_lab_requests, prescription_changes, previous_prescriptions, treatment_overview,
        treatment_timeline,
    )
    from care.parcels import first_shipping_day

    from care.billing import patient_summary as billing_summary, prescription_amount

    from .clinical_forms import make_clinical_context

    from .patient_shop import shop_context

    context = patient_page_context(request, company, patient, 'treatment', 'My Treatment')
    current = _plans(company, patient).exclude(status=PatientSubscription.Status.CANCELLED).first()
    authorization = current_authorization(company, patient, current)
    consult = next_consult(request, company, patient)
    weight = latest_weight(company, patient)
    treatment_authorization = current.authorization if current and current.authorization_id else authorization
    prescription_query = request.GET.copy()
    prescription_query.pop('prescription_page', None)
    tasks = billing_tasks(current) + patient_tasks(
        authorization=authorization, plan=current, consult=consult, weight=weight, labs=task_lab_requests(company, patient),
        lab_context=lambda lab: make_clinical_context(request, company, patient, 'lab', lab),
    )
    context.update(
        next_consult=consult,
        tasks=tasks,
        open_task_count=sum(1 for task in tasks if not task['settled']),
        previous_prescriptions=Paginator(previous_prescriptions(company, patient), 12).get_page(request.GET.get('prescription_page')),
        prescription_query=prescription_query.urlencode(),
        today=timezone.localdate(),
        help_form=help_form if help_form is not None else HelpRequestForm(auto_id='help_%s'),
        help_open=help_form is not None or request.GET.get('help') == '1',
        help_context=request.POST.get('treatment_context', '') if help_form is not None else make_treatment_context(request, company, patient, 'help-request'),
        cancel_form=cancel_form if cancel_form is not None else CancelSubscriptionForm(auto_id='cancel_%s'),
        cancel_open=cancel_form is not None or request.GET.get('cancel') == '1',
        current_plan=current, current_authorization=authorization,
        plan_authorization=current.authorization if current and current.authorization_id else authorization,
        can_enroll=current is None and _eligible_authorizations(company, patient).exists(),
        # Written too long ago to start on alone: the doctor confirms it first (care.prescriptions.start_check).
        start_check=start_check(authorization) if current is None and authorization is not None else None,
        ask_context=make_treatment_context(request, company, patient, 'prescription-ask'),
        plan_hold_reason=subscription_hold_reason(current) if current else '',
        enrollment_form=enrollment_form if enrollment_form is not None else _enrollment_form(company, patient),
        action_form=action_form if action_form is not None else ConfirmTreatmentForm(),
        action_plan=action_plan or current,
        enrollment_context=_token(request, company, patient, 'subscription-enroll') if enrollment_form is not None else make_treatment_context(request, company, patient, 'subscription-enroll'),
        action_context=_token(request, company, patient, 'subscription-status', action_plan or current) if action_form is not None or cancel_form is not None else make_treatment_context(request, company, patient, 'subscription-status', current),
        # The plan costs what the dose in effect costs; before enrolling, what the current prescription's dose costs.
        plan_dose=dose_in_effect(current) if current else None,
        enroll_dose=authorization.dose if authorization is not None and authorization.dose_id else None,
        enroll_amount=prescription_amount(authorization) if authorization is not None else None,
        plan_billing=billing_summary(current) if current and current.status != PatientSubscription.Status.CANCELLED else None,
        # The prescription in detail: dose, pens, week of treatment, a starting plan's steps and the next parcel.
        treatment=treatment_overview(company, patient, treatment_authorization),
        # Week by week: the dose, the pens sent and the payments due.
        timeline=treatment_timeline(company, patient, treatment_authorization),
        timeline_open=request.GET.get('timeline') == '1',
        # The doctor's decisions and what they changed, newest first (there is no Updates page in the menu).
        prescription_changes=prescription_changes(company, patient, treatment_authorization),
        changes_open=request.GET.get('changes') == '1',
        changes_selected=request.GET.get('change', '') if request.GET.get('changes') == '1' else '',
        # Paid today, the first pens go out on this day (care.parcels.first_shipping_day: after the order cut-off).
        first_pens_if_paid_today=first_shipping_day(timezone.localdate(), company),
        # Once-off items, below Previously prescribed: what the patient can buy without a prescription.
        **shop_context(request, company, patient),
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

    def display(self, request, enrollment_form=None, action_form=None, action_plan=None, status=200, **forms):
        context = patient_treatment_context(request, self.patient_company, self.patient, enrollment_form=enrollment_form,
                                            action_form=action_form, action_plan=action_plan, **forms)
        return render(request, 'portal/treatment_patient.html', context, status=status)

    def get(self, request):
        return redirect(reverse('portal:patient-treatment') + '#treatment-plan')


class PatientEnrollmentView(PatientSubscriptionView):
    """Start my subscription: the delivery address is saved, the subscription starts, and the patient goes on to
    add their card on the payment provider's page. The first payment is taken once the card is saved."""

    http_method_names = ('post', 'options')

    def post(self, request):
        from care.addresses import save_delivery_address
        from care.billing import start_debit_order
        from care.payment_providers import current_provider

        form = EnrollmentForm(request.POST, authorizations=self.eligible(), patient=self.patient, for_patient=True)
        valid = form.is_valid()
        try:
            token = validate_treatment_context(request, self.patient_company, self.patient, 'subscription-enroll')
            if valid:
                if start_check(form.cleaned_data['authorization']):
                    raise ValidationError('Your prescription needs your doctor to confirm it before you start. Ask them '
                                          'below, and you can start once they do.')
                save_delivery_address(company=self.patient_company, patient=self.patient, actor=request.user,
                                      data=form.address(), request=request)
                plan = enroll_local_subscription(company=self.patient_company, patient=self.patient, actor=request.user,
                                                 authorization=form.cleaned_data['authorization'],
                                                 confirm=form.cleaned_data['confirm'],
                                                 submission_key=token['submission_key'], request=request)
                if current_provider() is None:
                    messages.success(request, 'Your subscription has started. The practice will be in touch about how to pay.')
                    return redirect(reverse('portal:patient-treatment') + '#treatment-plan')
                try:
                    _, url = start_debit_order(subscription=plan, actor=request.user, method=form.cleaned_data['method'],
                                               request=request)
                except ValidationError as error:
                    messages.error(request, f'Your subscription has started, but your card couldn’t be set up: {" ".join(error.messages)}')
                    return redirect('portal:patient-billing')
                return redirect(url)
        except ValidationError as error:
            _error(form, error)
        return self.display(request, enrollment_form=form, status=400)


class PatientAskConfirmView(PatientSubscriptionView):
    """Ask the doctor to confirm a prescription written too long ago to start on alone."""

    http_method_names = ('post', 'options')

    def post(self, request):
        from .patient_summary import current_authorization

        # The prescription the page offered to start on.
        authorization = current_authorization(self.patient_company, self.patient, None)
        try:
            validate_treatment_context(request, self.patient_company, self.patient, 'prescription-ask')
            step = ask_to_confirm(company=self.patient_company, patient=self.patient, actor=request.user,
                                  authorization=authorization, request=request)
            doctor = step.prescribed_by.titled_name if step.prescribed_by_id else 'your doctor'
            messages.success(request, f'We’ve asked {doctor} to confirm your prescription. You can start once they do.')
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
        return redirect(reverse('portal:patient-treatment') + '#treatment-plan')


class PatientSubscriptionStatusView(PatientSubscriptionView):
    http_method_names = ('post', 'options')

    def post(self, request, pk):
        plan = get_object_or_404(_plans(self.patient_company, self.patient), pk=pk)
        # Cancelling asks why, in its own dialog; pausing and resuming only need the tick.
        cancelling = request.POST.get('action') == 'cancel'
        form = CancelSubscriptionForm(request.POST, auto_id='cancel_%s') if cancelling else ConfirmTreatmentForm(request.POST)
        valid = form.is_valid()
        try:
            validate_treatment_context(request, self.patient_company, self.patient, 'subscription-status', plan)
            if valid:
                change_subscription_status(subscription=plan, actor=request.user, action=request.POST.get('action'),
                                           confirm=form.cleaned_data['confirm'], reasons=form.cleaned_data.get('reasons', ()),
                                           note=form.cleaned_data.get('note', ''), request=request)
                messages.success(request, STATUS_MESSAGES[request.POST.get('action')])
                return redirect(reverse('portal:patient-treatment') + '#treatment-plan')
        except ValidationError as error:
            _error(form, error)
        if cancelling:
            return self.display(request, action_plan=plan, cancel_form=form, status=400)
        return self.display(request, action_form=form, action_plan=plan, status=400)


class PatientHelpRequestView(PatientSubscriptionView):
    """Something is wrong, help me: a conversation with the patient's doctor and the practice, and a place on the
    practice's Dropouts list."""

    http_method_names = ('post', 'options')

    def post(self, request):
        form = HelpRequestForm(request.POST, auto_id='help_%s')
        valid = form.is_valid()
        try:
            validate_treatment_context(request, self.patient_company, self.patient, 'help-request')
            if valid:
                help_request = request_help(patient=self.patient, actor=request.user, reason=form.cleaned_data['reason'], request=request)
                messages.success(request, 'Thank you. Your doctor and the practice team have your message and will reply in My Messages.')
                return redirect(f"{reverse('portal:patient-messages')}?{urlencode({'thread': help_request.thread_id})}#patient-conversation")
        except ValidationError as error:
            _error(form, error)
        return self.display(request, help_form=form, status=400)
