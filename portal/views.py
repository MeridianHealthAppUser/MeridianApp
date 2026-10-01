from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Prefetch, Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.generic import DetailView, TemplateView

from care.forms import (
    AppointmentForm,
    AppointmentProposalForm,
    ClinicalNoteForm,
    ClinicalTaskForm,
    PatientMessageForm,
    PatientThreadForm,
    WeightEntryForm,
)
from care.models import (
    Appointment,
    ClinicalNote,
    ClinicalTask,
    MessageThread,
    PatientEvent,
    PatientMessage,
    PatientSubscription,
    Shipment,
    WeightEntry,
)
from care.services import complete_task, post_patient_message, record_audit
from care.tag_forms import ClinicalNoteTagsForm
from care.task_services import save_task, set_record_tags, visible_tasks
from practices.models import Company, CompanyMembership, Patient
from practices.services import (
    active_membership_for,
    active_patient_for,
    get_active_company,
    get_active_patient_company,
    set_active_company,
    set_active_patient_company,
)

from .patient_context import validate_patient_context


class LandingPageView(TemplateView):
    """The public, single-page Meridian Health landing page."""

    template_name = 'portal/landing.html'


class StaffCompanyRequiredMixin:
    """Resolve a staff-selected practice before any tenant query is made."""

    company = None
    membership = None

    def dispatch(self, request, *args, **kwargs):
        self.company = get_active_company(request)
        self.membership = active_membership_for(request, self.company)
        if self.company is None or self.membership is None:
            raise PermissionDenied('You need an active practice membership to access this workspace.')
        return super().dispatch(request, *args, **kwargs)


class PatientPortalRequiredMixin:
    """Resolve a patient-owned practice record without ever using staff access."""

    patient_company = None
    patient = None

    @method_decorator(never_cache)
    def dispatch(self, request, *args, **kwargs):
        self.patient_company = get_active_patient_company(request)
        self.patient = active_patient_for(request, self.patient_company)
        if self.patient_company is None or self.patient is None:
            raise PermissionDenied('You do not have an active patient record.')
        return super().dispatch(request, *args, **kwargs)


class StaffDashboardContextMixin(StaffCompanyRequiredMixin):
    """Shared, role-aware summary data for the desktop and mobile staff views."""

    def get_context_data(self, **kwargs):
        from .clinical_tasks import attach_clinical_task_links

        context = super().get_context_data(**kwargs)
        now = timezone.now()
        today = timezone.localdate()
        open_tasks = visible_tasks(self.company, self.request.user, self.membership).filter(
            status__in=(ClinicalTask.Status.OPEN, ClinicalTask.Status.IN_PROGRESS),
        ).select_related('patient', 'assigned_to', 'encounter_signing', 'lab_review_request')
        upcoming_appointments = Appointment.objects.for_company(self.company).filter(
            starts_at__gte=now,
            status=Appointment.Status.BOOKED,
        ).select_related('patient', 'clinician')
        if self.membership.role == self.membership.Role.DOCTOR:
            upcoming_appointments = upcoming_appointments.filter(clinician=self.request.user)

        recent_threads = MessageThread.objects.for_company(self.company).filter(
            patient__company=self.company, patient__is_active=True,
        ).select_related('patient').order_by('-last_message_at', '-created_at', '-pk')
        incoming_unread = PatientMessage.objects.for_company(self.company).filter(
            read_at__isnull=True, sender_id=F('thread__patient__user_id'),
        )

        context.update(
            company=self.company,
            active_membership=self.membership,
            today=today,
            metrics={
                'patients': Patient.objects.filter(company=self.company, is_active=True).count(),
                'appointments': upcoming_appointments.filter(starts_at__date=today).count(),
                'open_tasks': open_tasks.count(),
                'unread_messages': incoming_unread.count(),
                'shipments_due': Shipment.objects.for_company(self.company).filter(
                    scheduled_for__lte=today + timedelta(days=7),
                    status__in=(Shipment.Status.DRAFT, Shipment.Status.HELD, Shipment.Status.READY),
                ).count(),
            },
            patients=Patient.objects.filter(company=self.company, is_active=True).select_related('assigned_doctor')[:8],
            upcoming_appointments=upcoming_appointments[:8],
            open_tasks=attach_clinical_task_links(list(open_tasks[:8]), self.request.user, self.membership),
            recent_messages=recent_threads[:6],
        )
        return context


class DesktopDashboardView(LoginRequiredMixin, StaffDashboardContextMixin, TemplateView):
    template_name = 'portal/desktop_dashboard.html'

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        # A patient-only person enters their own care portal, while a shared SSO
        # account with staff access stays in the internal practice workspace.
        if get_active_company(request) is None:
            if get_active_patient_company(request) is not None:
                return redirect('portal:patient-dashboard')
            raise PermissionDenied('Your account is not connected to a practice or patient record.')
        return super().dispatch(request, *args, **kwargs)


class MobileDashboardView(LoginRequiredMixin, StaffDashboardContextMixin, TemplateView):
    template_name = 'portal/mobile_dashboard.html'

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if get_active_company(request) is None:
            if get_active_patient_company(request) is not None:
                return redirect('portal:patient-dashboard')
            raise PermissionDenied('Your account is not connected to a practice or patient record.')
        return super().dispatch(request, *args, **kwargs)


@method_decorator(never_cache, name='dispatch')
class StaffInboxView(LoginRequiredMixin, StaffCompanyRequiredMixin, TemplateView):
    """Open a selected conversation in the active practice's secure inbox."""

    template_name = 'portal/staff_inbox.html'
    http_method_names = ('get', 'head', 'options')

    def get_context_data(self, **kwargs):
        from .inbox import staff_inbox_context

        context = super().get_context_data(**kwargs)
        context.update(staff_inbox_context(self.request, self.company, self.membership))
        return context


class PatientDashboardView(LoginRequiredMixin, PatientPortalRequiredMixin, TemplateView):
    template_name = 'portal/patient_dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(_patient_portal_context(self.request, self.patient_company, self.patient))
        return context


class ActivateCompanyView(LoginRequiredMixin, View):
    http_method_names = ('post',)

    def post(self, request, slug):
        from practices.tenancy import require_multi_practice
        require_multi_practice()
        company = get_object_or_404(Company, slug=slug, is_active=True)
        set_active_company(request, company)
        # Record URLs belong to the previous practice. Switch to an overview so
        # the user can choose a record in the newly selected practice.
        next_path = request.POST.get('next', '').split('?', 1)[0].split('#', 1)[0]
        for destination in ('portal:clinical-consultations', 'portal:clinical-labs', 'portal:treatment-authorisations', 'portal:treatment-review-rules', 'portal:compounding-list', 'portal:activity-statements'):
            if next_path.startswith(reverse(destination)):
                membership = active_membership_for(request, company)
                if membership.role in (CompanyMembership.Role.DOCTOR, CompanyMembership.Role.SUPER_ADMIN):
                    return redirect(destination)
        for destination in ('portal:management-practices', 'portal:management-users', 'portal:policy-list'):
            if next_path.startswith(reverse(destination)):
                membership = active_membership_for(request, company)
                if membership.role == CompanyMembership.Role.SUPER_ADMIN:
                    return redirect(destination)
        for destination in ('portal:staff-leads', 'portal:dropouts', 'portal:staff-data-requests'):
            if next_path.startswith(reverse(destination)):
                membership = active_membership_for(request, company)
                if membership.role in (CompanyMembership.Role.PRACTICE_ADMIN, CompanyMembership.Role.SUPER_ADMIN):
                    return redirect(destination)
        for destination in ('portal:ops-catalogue', 'portal:ops-stock', 'portal:ops-shipping', 'portal:ops-history', 'portal:ops-orders', 'portal:metrics', 'portal:staff-schedule'):
            if next_path.startswith(reverse(destination)):
                return redirect(destination)
        for destination in (
            'portal:mobile-dashboard', 'portal:staff-inbox', 'portal:staff-tasks',
            'portal:patient-list', 'portal:staff-schedule', 'portal:treatment-subscriptions',
            'portal:account-access-history',
        ):
            if next_path == reverse(destination):
                return redirect(destination)
        return redirect('portal:desktop-dashboard')


class ActivatePatientCompanyView(LoginRequiredMixin, View):
    http_method_names = ('post',)

    def post(self, request, slug):
        from practices.tenancy import require_multi_practice
        require_multi_practice()
        company = get_object_or_404(Company, slug=slug, is_active=True)
        set_active_patient_company(request, company)
        # Keep the section, never a record ID or query from the old practice.
        next_path = request.POST.get('next', '').split('?', 1)[0].split('#', 1)[0]
        for destination in (
            'portal:patient-appointments', 'portal:patient-messages',
            'portal:patient-progress', 'portal:patient-account', 'portal:patient-labs',
            'portal:patient-treatment', 'portal:patient-subscription', 'portal:patient-medical-profile', 'portal:patient-updates',
            'portal:patient-pharmacy', 'portal:patient-orders',
            'portal:patient-privacy', 'portal:patient-data-requests',
        ):
            if next_path == reverse(destination) or (destination in ('portal:patient-labs', 'portal:patient-appointments', 'portal:patient-subscription', 'portal:patient-pharmacy', 'portal:patient-orders', 'portal:patient-data-requests') and next_path.startswith(reverse(destination))):
                return redirect(destination)
        return redirect('portal:patient-dashboard')


@method_decorator(never_cache, name='dispatch')
class PatientDetailView(LoginRequiredMixin, StaffCompanyRequiredMixin, DetailView):
    model = Patient
    template_name = 'portal/patient_detail.html'
    context_object_name = 'patient'

    def get_queryset(self):
        return Patient.objects.filter(company=self.company, is_active=True).select_related('assigned_doctor', 'user')

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        if request.method == 'GET':
            record_audit(
                company=self.company,
                actor=request.user,
                patient=self.object,
                action='patient.record_viewed',
                target=self.object,
                request=request,
            )
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(_patient_record_context(self.request, self.company, self.membership, self.object))
        return context


def _conversation_threads(company, patient, *, patient_view, mark_read=False):
    """Load the complete conversation and mark only displayed incoming rows read.

    For staff this is a shared team inbox receipt, not a per-user read receipt.
    """
    threads = list(MessageThread.objects.for_company(company).filter(patient=patient).prefetch_related(
        Prefetch(
            'messages',
            queryset=PatientMessage.objects.for_company(company).select_related('sender').order_by('created_at', 'pk'),
            to_attr='conversation_messages',
        ),
    ))
    read_ids = []
    for thread in threads:
        for message in thread.conversation_messages:
            incoming = message.sender_id != patient.user_id if patient_view else message.sender_id == patient.user_id
            if incoming and message.read_at is None:
                read_ids.append(message.pk)
    if read_ids and mark_read:
        PatientMessage.objects.for_company(company).filter(pk__in=read_ids, read_at__isnull=True).update(read_at=timezone.now())
    return threads


def _attach_appointment_proposals(context, request, company, patient, *, actor_role, allowed):
    from care.models import AppointmentProposal

    threads = context['message_threads']
    open_thread_ids = {thread.pk for thread in threads if not thread.is_closed}
    proposals = AppointmentProposal.objects.for_company(company).filter(
        patient=patient, thread_id__in=[thread.pk for thread in threads],
        thread__company=company, thread__patient=patient,
        appointment__company=company, appointment__patient=patient,
    ).filter(Q(resulting_appointment__isnull=True) | Q(resulting_appointment__company=company, resulting_appointment__patient=patient)).select_related('proposed_by', 'recipient', 'appointment', 'original_clinician', 'resulting_appointment')
    by_thread = {}
    for proposal in proposals:
        pending = proposal.status == AppointmentProposal.Status.PENDING
        thread_open = proposal.thread_id in open_thread_ids
        doctor_matches = actor_role != 'doctor' or proposal.appointment.clinician_id == request.user.pk
        proposal.can_respond = (
            allowed and pending and thread_open and doctor_matches and proposal.recipient_id == request.user.pk
            and proposal.proposed_by_id != request.user.pk and proposal.proposer_role != actor_role
        )
        proposal.can_withdraw = (
            allowed and pending and thread_open and doctor_matches and proposal.proposed_by_id == request.user.pk
            and proposal.proposer_role == actor_role
        )
        by_thread.setdefault(proposal.thread_id, []).append(proposal)
    for thread in threads:
        thread.appointment_proposals_list = by_thread.get(thread.pk, [])
        thread.proposal_form = None
        thread.has_selectable_appointments = False
        if allowed and not thread.is_closed:
            if context.get('failed_form') == 'proposal_form' and context.get('reply_thread_id') == thread.pk:
                form = context['proposal_form']
            else:
                form = AppointmentProposalForm(
                    company=company, patient=patient, thread=thread, actor=request.user, actor_role=actor_role,
                    initial={'appointment': request.GET.get('appointment')},
                )
            thread.proposal_form = form
            thread.has_selectable_appointments = form.has_selectable_appointments
    context['can_propose_appointments'] = allowed
    return context


def _patient_record_context(request, company, membership, patient, **overrides):
    from video.access import attach_video_join
    from .clinical_tasks import attach_clinical_task_links
    from .record_weight import record_weight_context
    from .video_links import safe_video_link
    from .workflow_context import make_workflow_context

    can_add_note = membership.role == CompanyMembership.Role.DOCTOR
    can_view_notes = membership.role in (CompanyMembership.Role.DOCTOR, CompanyMembership.Role.SUPER_ADMIN)
    notes = []
    if can_view_notes:
        note_queryset = ClinicalNote.objects.for_company(company).filter(patient=patient).filter(
            Q(is_private=False) | Q(author=request.user),
        ).select_related('author').prefetch_related('tags')
        notes = list(note_queryset[:8])
        failed_note_id = overrides.get('failed_note_tag_id')
        if failed_note_id and not any(note.pk == failed_note_id for note in notes):
            failed_note = note_queryset.filter(pk=failed_note_id, author=request.user).first()
            if failed_note:
                notes.append(failed_note)
        for note in notes:
            if can_add_note and note.author_id == request.user.pk:
                note.tags_form = overrides['note_tags_form'] if note.pk == failed_note_id else ClinicalNoteTagsForm(company=company, instance=note)
    appointments = list(Appointment.objects.for_company(company).filter(patient=patient).select_related('clinician')[:8])
    for appointment in appointments:
        appointment.video_link = safe_video_link(appointment.video_link)
    attach_video_join(appointments, request.user.pk, allowed_role='doctor' if membership.role == 'doctor' else None)
    context = {
        'patient': patient,
        'company': company,
        'active_membership': membership,
        'can_add_clinical_note': can_add_note,
        'can_view_clinical_notes': can_view_notes,
        'appointments': appointments,
        'tasks': attach_clinical_task_links(list(ClinicalTask.objects.for_company(company).filter(patient=patient).select_related(
            'assigned_to', 'encounter_signing', 'lab_review_request',
        ).prefetch_related('tags')[:8]), request.user, membership),
        'notes': notes,
        'message_threads': _conversation_threads(company, patient, patient_view=False, mark_read=request.method == 'GET'),
        'task_form': ClinicalTaskForm(company=company, patient=patient),
        'appointment_form': AppointmentForm(company=company, patient=patient),
        'legacy_appointment_context': (
            request.POST.get('workflow_context', '') if overrides.get('failed_form') == 'appointment_form'
            else make_workflow_context(request, company, 'legacy-staff-booking', patient)
        ),
        'note_form': ClinicalNoteForm(company=company, patient=patient, author=request.user) if can_add_note else None,
        'message_form': PatientMessageForm(),
        'thread_form': PatientThreadForm(auto_id='thread_%s'),
    }
    context.update(record_weight_context(request, company, patient))
    requested_thread = request.GET.get('thread')
    context['selected_thread_id'] = next(
        (thread.pk for thread in context['message_threads'] if str(thread.pk) == requested_thread),
        None,
    )
    context.update(overrides)
    return _attach_appointment_proposals(context, request, company, patient, actor_role='doctor', allowed=can_add_note)


def _patient_portal_context(request, company, patient, **overrides):
    from .patient_views import patient_overview_context

    return patient_overview_context(request, company, patient, **overrides)


def _invalid_patient_form(request, company, patient, name, form, **extra):
    from .patient_views import patient_messages_context, patient_overview_context, patient_progress_context

    overrides = {name: form, 'failed_form': name, **extra}
    if name == 'weight_form' and extra.get('return_to') == 'home':
        context = patient_overview_context(request, company, patient, **overrides)
        template = 'portal/patient_dashboard.html'
    elif name == 'weight_form':
        context = patient_progress_context(request, company, patient, **overrides)
        template = 'portal/patient_progress.html'
    else:
        context = patient_messages_context(
            request, company, patient, selected_thread_id=extra.get('reply_thread_id'), **overrides,
        )
        template = 'portal/patient_messages.html'
    return render(request, template, context)


def _invalid_staff_form(request, company, membership, patient, name, form, **extra):
    from .patient_workspace import render_workspace_form_error
    return render_workspace_form_error(request, company, membership, patient, name, form, **extra)


class StaffPatientActionMixin(LoginRequiredMixin, StaffCompanyRequiredMixin):
    """Look up the URL patient in the already-authorised active practice."""

    def get_patient(self):
        return get_object_or_404(Patient, pk=self.kwargs['patient_pk'], company=self.company, is_active=True)

    def patient_detail_redirect(self, patient):
        from .patient_workspace import workspace_url
        tab = {'patient-task-create': 'tasks', 'patient-appointment-create': 'appointments',
               'patient-note-create': 'notes', 'staff-thread-create': 'messages'}.get(self.request.resolver_match.url_name, 'overview')
        return redirect(workspace_url(patient, tab))

    def invalid_form(self, patient, name, form):
        return _invalid_staff_form(self.request, self.company, self.membership, patient, name, form)


class PatientTaskCreateView(StaffPatientActionMixin, View):
    http_method_names = ('post',)

    def post(self, request, *args, **kwargs):
        patient = self.get_patient()
        form = ClinicalTaskForm(request.POST, company=self.company, patient=patient)
        if not form.is_valid():
            return self.invalid_form(patient, 'task_form', form)
        try:
            save_task(company=self.company, actor=request.user, form=form, request=request)
        except ValidationError as error:
            form.add_error(None, error)
            return self.invalid_form(patient, 'task_form', form)
        messages.success(request, 'Care task created.')
        return self.patient_detail_redirect(patient)


class PatientAppointmentCreateView(StaffPatientActionMixin, View):
    http_method_names = ('post',)

    def post(self, request, *args, **kwargs):
        patient = self.get_patient()
        form = AppointmentForm(request.POST, company=self.company, patient=patient)
        valid = form.is_valid()
        from care.appointment_lifecycle import book_staff_appointment
        from .workflow_context import validate_workflow_context
        try:
            validate_workflow_context(request, self.company, 'legacy-staff-booking', patient)
            if valid:
                book_staff_appointment(company=self.company, actor=request.user, request=request, **form.cleaned_data)
            else:
                return self.invalid_form(patient, 'appointment_form', form)
        except ValidationError as error:
            for message in error.messages:
                form.add_error(None, message)
            return self.invalid_form(patient, 'appointment_form', form)
        messages.success(request, 'Appointment booked.')
        return self.patient_detail_redirect(patient)


class PatientNoteCreateView(StaffPatientActionMixin, View):
    http_method_names = ('post',)

    def post(self, request, *args, **kwargs):
        patient = self.get_patient()
        if self.membership.role != CompanyMembership.Role.DOCTOR:
            raise PermissionDenied('Only a doctor in this practice can add a clinical note.')
        form = ClinicalNoteForm(request.POST, company=self.company, patient=patient, author=request.user)
        if not form.is_valid():
            return self.invalid_form(patient, 'note_form', form)
        with transaction.atomic():
            Company.objects.select_for_update().get(pk=self.company.pk)
            note = form.save()
            if form.cleaned_data['tags'] or form.cleaned_data['new_tag']:
                set_record_tags(record=note, actor=request.user, tags=form.cleaned_data['tags'], new_tag=form.cleaned_data['new_tag'], request=request)
            record_audit(company=self.company, actor=request.user, patient=patient, action='clinical_note.created', target=note, request=request)
        messages.success(request, 'Clinical note added.')
        return self.patient_detail_redirect(patient)


class TaskCompleteView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('post',)

    def post(self, request, pk):
        queryset = visible_tasks(self.company, request.user, self.membership)
        task = get_object_or_404(queryset, pk=pk)
        try:
            complete_task(task=task, actor=request.user, request=request)
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
        else:
            messages.success(request, 'Task marked complete.')
        return _task_redirect(request, task)


class StaffMessageCreateView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('post',)

    def post(self, request, pk):
        from .inbox import staff_inbox_context, staff_inbox_redirect

        thread = get_object_or_404(
            MessageThread.objects.for_company(self.company).select_related('patient'),
            pk=pk,
            is_closed=False,
            patient__is_active=True,
            patient__company=self.company,
        )
        from_inbox = request.POST.get('return_to') == 'inbox'
        form = PatientMessageForm(request.POST, auto_id='inbox_%s' if from_inbox else 'id_%s')
        if form.is_valid():
            try:
                post_patient_message(thread=thread, sender=request.user, body=form.cleaned_data['body'], request=request)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, 'Secure message sent.')
                if from_inbox:
                    return staff_inbox_redirect(thread)
                from .patient_workspace import workspace_url
                return redirect(workspace_url(thread.patient, 'messages', thread=thread.pk))
        if from_inbox:
            return render(request, 'portal/staff_inbox.html', staff_inbox_context(
                request, self.company, self.membership, selected_thread_id=thread.pk,
                message_form=form, failed_form='message_form', reply_thread_id=thread.pk,
            ))
        return _invalid_staff_form(request, self.company, self.membership, thread.patient, 'message_form', form, reply_thread_id=thread.pk)


class PatientWeightAddView(LoginRequiredMixin, PatientPortalRequiredMixin, View):
    http_method_names = ('post',)

    def post(self, request):
        form = WeightEntryForm(request.POST, company=self.patient_company, patient=self.patient, recorded_by=request.user)
        # Check-ins can be logged from Home or from the weight history page.
        return_to = 'home' if request.POST.get('return_to') == 'home' else ''
        try:
            validate_patient_context(request, self.patient_company, self.patient)
        except ValidationError as error:
            form.add_error(None, error)
        if not form.is_valid():
            return _invalid_patient_form(request, self.patient_company, self.patient, 'weight_form', form, return_to=return_to)
        try:
            with transaction.atomic():
                entry = form.save()
                PatientEvent.objects.create(
                    company=self.patient_company,
                    patient=self.patient,
                    category=PatientEvent.Category.CLINICAL,
                    title='Weight check-in recorded',
                    detail='Your weight check-in was shared with your care team.',
                    source_type=entry._meta.label_lower,
                    source_id=str(entry.pk),
                )
                record_audit(company=self.patient_company, actor=request.user, patient=self.patient, action='weight_entry.created', target=entry, request=request)
        except IntegrityError:
            form.add_error('recorded_on', 'A weight entry already exists for that date.')
            return _invalid_patient_form(request, self.patient_company, self.patient, 'weight_form', form, return_to=return_to)
        messages.success(request, 'Your weight check-in has been saved.')
        if return_to == 'home':
            return redirect(reverse('portal:patient-dashboard') + '#weight-progress')
        return redirect('portal:patient-progress')


class PatientMessageCreateView(LoginRequiredMixin, PatientPortalRequiredMixin, View):
    http_method_names = ('post',)

    def post(self, request, pk):
        from .patient_views import patient_messages_redirect

        thread = get_object_or_404(
            MessageThread.objects.for_company(self.patient_company),
            pk=pk,
            patient=self.patient,
            is_closed=False,
        )
        form = PatientMessageForm(request.POST, auto_id='patient_reply_%s')
        if form.is_valid():
            try:
                post_patient_message(thread=thread, sender=request.user, body=form.cleaned_data['body'], request=request)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, 'Your message has been sent to your care team.')
                return patient_messages_redirect(thread)
        return _invalid_patient_form(request, self.patient_company, self.patient, 'message_form', form, reply_thread_id=thread.pk)


class PatientThreadCreateView(LoginRequiredMixin, PatientPortalRequiredMixin, View):
    http_method_names = ('post',)

    def post(self, request):
        from .patient_views import patient_messages_redirect

        form = PatientThreadForm(request.POST, auto_id='thread_%s')
        try:
            validate_patient_context(request, self.patient_company, self.patient)
        except ValidationError as error:
            form.add_error(None, error)
        if not form.is_valid():
            return _invalid_patient_form(request, self.patient_company, self.patient, 'thread_form', form)
        with transaction.atomic():
            thread = MessageThread(
                company=self.patient_company,
                patient=self.patient,
                subject=form.cleaned_data['subject'],
                opened_by=request.user,
            )
            thread.full_clean()
            thread.save()
            post_patient_message(thread=thread, sender=request.user, body=form.cleaned_data['body'], request=request)
        messages.success(request, 'Your secure conversation has been started.')
        return patient_messages_redirect(thread)


class StaffThreadCreateView(StaffPatientActionMixin, View):
    http_method_names = ('post',)

    def post(self, request, *args, **kwargs):
        patient = self.get_patient()
        form = PatientThreadForm(request.POST, auto_id='thread_%s')
        if not form.is_valid():
            return self.invalid_form(patient, 'thread_form', form)
        with transaction.atomic():
            thread = MessageThread(company=self.company, patient=patient, subject=form.cleaned_data['subject'], opened_by=request.user)
            thread.full_clean()
            thread.save()
            post_patient_message(thread=thread, sender=request.user, body=form.cleaned_data['body'], request=request)
        messages.success(request, 'Secure conversation started.')
        from .patient_workspace import workspace_url
        return redirect(workspace_url(patient, 'messages', thread=thread.pk))


def _task_redirect(request, task):
    next_url = request.POST.get('next')
    if next_url and url_has_allowed_host_and_scheme(
        url=next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return HttpResponseRedirect(next_url)
    if task.patient_id:
        from .patient_workspace import workspace_url
        return redirect(workspace_url(task.patient, 'tasks'))
    return redirect('portal:staff-tasks')
