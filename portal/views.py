from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Prefetch, Q
from django.http import HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.functional import SimpleLazyObject
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.generic import DetailView, TemplateView

from care.forms import (
    ClinicalNoteForm,
    ClinicalTaskForm,
    PatientMessageForm,
    PatientThreadForm,
    WeightEntryForm,
)
from care.messaging import post_staff_reply, staff_threads, start_conversation, unread_for_staff
from care.operations import can_ship
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

from .navigation import uses_flat_menu
from .patient_context import validate_patient_context
from .realtime_context import changed_scope_response, patient_scope, records_conversation_view, staff_scope


class LandingPageView(TemplateView):
    """The public, single-page Meridian Health landing page."""

    template_name = 'portal/landing.html'


class StaffCompanyRequiredMixin:
    """Resolve a staff-selected practice before any tenant query is made."""

    company = None
    membership = None
    # Operations, Insights and Settings pages are for practice admins and Super Admins only.
    admin_only = False
    # Weekly shipping and Dispatch history: doctors too, when the practice lets doctors ship (care.operations.can_ship).
    shipping = False
    # Practice-wide clinical lists the Clinician role's menu leaves out, as they work from each patient's
    # record: opened by URL, a clinician goes back to the Overview with this message as an error toast.
    clinician_redirect_message = ''

    def dispatch(self, request, *args, **kwargs):
        self.company = get_active_company(request)
        self.membership = active_membership_for(request, self.company)
        if self.company is None or self.membership is None:
            raise PermissionDenied('You need an active practice membership to access this workspace.')
        request.realtime_scope = staff_scope(self.company, request.user)
        if response := changed_scope_response(request):
            return response
        if self.admin_only and not self.membership.is_admin and not (self.shipping and can_ship(self.company, self.membership)):
            raise PermissionDenied('This page is for practice administrators.')
        if self.clinician_redirect_message and self.membership.role == CompanyMembership.Role.DOCTOR:
            messages.error(request, self.clinician_redirect_message)
            return redirect('portal:desktop-dashboard')
        from .page_navigation import navigation_response
        if response := navigation_response(self, request, company=self.company, membership=self.membership):
            return response
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
        request.realtime_scope = patient_scope(self.patient_company, self.patient, request.user)
        if response := changed_scope_response(request):
            return response
        from .page_navigation import navigation_response
        if response := navigation_response(self, request, company=self.patient_company, patient=self.patient):
            return response
        return super().dispatch(request, *args, **kwargs)


def staff_dashboard_metrics(company, user, membership):
    """Summary counts can be rendered separately from the working appointment/task lists."""
    now, today = timezone.now(), timezone.localdate()
    patients = Patient.objects.filter(company=company, is_active=True)
    appointments = Appointment.objects.for_company(company).filter(starts_at__gte=now, status=Appointment.Status.BOOKED)
    if membership.role == membership.Role.DOCTOR:
        patients = patients.filter(assigned_doctor=user)
        appointments = appointments.filter(clinician=user)
    tasks = visible_tasks(company, user, membership).filter(status__in=(ClinicalTask.Status.OPEN, ClinicalTask.Status.IN_PROGRESS))
    metrics = {
        'patients': patients.count(), 'appointments': appointments.filter(starts_at__date=today).count(),
        'open_tasks': tasks.count(), 'unread_messages': unread_for_staff(company, user).count(),
    }
    if membership.is_admin:
        metrics['shipments_due'] = Shipment.objects.for_company(company).filter(
            scheduled_for__lte=today + timedelta(days=7),
            status__in=(Shipment.Status.DRAFT, Shipment.Status.HELD, Shipment.Status.READY),
        ).count()
    return metrics


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

        recent_threads = staff_threads(self.company, self.request.user).select_related('patient').order_by('-last_message_at', '-created_at', '-pk')
        patients = Patient.objects.filter(company=self.company, is_active=True)
        # Clinicians' patient records are the patients assigned to them.
        if self.membership.role == self.membership.Role.DOCTOR:
            patients = patients.filter(assigned_doctor=self.request.user)
        from .page_navigation import canonical_url, is_content_request
        # Clinicians and practice administrators get the same simple Overview: appointments and open tasks only.
        simple = uses_flat_menu(self.membership)
        defer_metrics = not simple and is_content_request(self.request)
        if defer_metrics:
            metrics = {}
        elif simple:
            # Keep the context contract for explicit consumers without running
            # four/five counts for cards these overview templates never render.
            metrics = SimpleLazyObject(lambda: staff_dashboard_metrics(self.company, self.request.user, self.membership))
        else:
            metrics = staff_dashboard_metrics(self.company, self.request.user, self.membership)
        context.update(
            company=self.company,
            active_membership=self.membership,
            today=today,
            metrics=metrics,
            defer_dashboard_metrics=defer_metrics,
            dashboard_content_url=canonical_url(self.request),
            simple_overview=simple,
            upcoming_appointments=upcoming_appointments[:8],
            open_tasks=attach_clinical_task_links(list(open_tasks[:8]), self.request.user, self.membership),
        )
        # They find patients in Patient Records and conversations in Messages, so Overview doesn't list them.
        if not simple:
            context.update(patients=patients.select_related('assigned_doctor')[:8], recent_messages=self.tagged(recent_threads[:6]))
        return context

    def tagged(self, threads):
        """Recent conversations with who's in them as tags: the patient, then anyone else besides you."""
        from .people_tags import in_join_order, people_tags, staff_kinds

        threads = list(threads.prefetch_related(in_join_order('participants')))
        kinds = staff_kinds(self.company, [person.pk for thread in threads for person in thread.participants.all()])
        for thread in threads:
            others = [person for person in thread.participants.all() if person.pk not in (self.request.user.pk, thread.patient.user_id)]
            thread.people_tags = people_tags(others, kinds, patient=thread.patient)
        return threads


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
    """Patient messages: the conversations this staff member is in, with the chosen one open beside them."""

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
        for destination in ('portal:clinical-consultations', 'portal:clinical-labs', 'portal:treatment-authorisations', 'portal:compounding-list'):
            if next_path.startswith(reverse(destination)):
                membership = active_membership_for(request, company)
                if membership.has_clinical_access:
                    return redirect(destination)
        for destination in ('portal:management-practices', 'portal:management-users', 'portal:policy-list',
                            'portal:treatment-review-rules', 'portal:activity-statements', 'portal:metrics'):
            if next_path.startswith(reverse(destination)):
                membership = active_membership_for(request, company)
                if membership.role == CompanyMembership.Role.SUPER_ADMIN:
                    return redirect(destination)
        # Administration and Operations pages; the Clinician role is sent to the overview instead.
        for destination in ('portal:staff-leads', 'portal:dropouts', 'portal:staff-data-requests',
                            'portal:ops-catalogue', 'portal:ops-shipping', 'portal:ops-history', 'portal:ops-orders',
                            'portal:treatment-subscriptions'):
            if next_path.startswith(reverse(destination)):
                membership = active_membership_for(request, company)
                if membership.is_admin:
                    return redirect(destination)
        if next_path.startswith(reverse('portal:staff-schedule')):
            return redirect('portal:staff-schedule')
        for destination in (
            'portal:mobile-dashboard', 'portal:staff-inbox', 'portal:staff-tasks',
            'portal:patient-list', 'portal:account-access-history',
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
            'portal:patient-progress', 'portal:patient-profile', 'portal:patient-labs',
            'portal:patient-treatment', 'portal:patient-subscription', 'portal:patient-medical-profile', 'portal:patient-updates',
            'portal:patient-pharmacy', 'portal:patient-orders',
        ):
            if next_path == reverse(destination) or (destination in ('portal:patient-labs', 'portal:patient-appointments', 'portal:patient-subscription', 'portal:patient-pharmacy', 'portal:patient-orders') and next_path.startswith(reverse(destination))):
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


def _conversation_threads(company, patient, *, patient_view, viewer=None, mark_read=False):
    """Load the complete conversation and mark only displayed incoming rows read.

    Staff see only conversations they take part in, and each keeps their own read position: showing them reads
    what the patient and colleagues sent for this staff member only.
    """
    threads = MessageThread.objects.for_company(company).filter(patient=patient)
    if not patient_view:
        threads = threads.filter(participants=viewer)
    threads = list(threads.prefetch_related(
        Prefetch(
            'messages',
            queryset=PatientMessage.objects.for_company(company).select_related('sender').order_by('created_at', 'pk'),
            to_attr='conversation_messages',
        ),
    ))
    if not mark_read:
        return threads
    if patient_view:
        read_ids = [message.pk for thread in threads for message in thread.conversation_messages
                    if message.sender_id != patient.user_id and message.read_at is None]
        if read_ids:
            PatientMessage.objects.for_company(company).filter(pk__in=read_ids, read_at__isnull=True).update(read_at=timezone.now())
    else:
        from care.messaging import mark_read_by_staff

        # Read up to the latest message shown, whoever sent it; colleagues' messages count too.
        for thread in threads:
            if thread.conversation_messages:
                mark_read_by_staff(thread, viewer, [message.pk for message in thread.conversation_messages])
    return threads


def _patient_record_context(request, company, membership, patient, **overrides):
    from video.access import attach_video_join
    from .clinical_tasks import attach_clinical_task_links
    from .record_weight import record_weight_context
    from .video_links import safe_video_link
    from .workflow_context import make_workflow_context

    can_add_note = membership.is_clinician
    can_view_notes = membership.has_clinical_access
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
    attach_video_join(appointments, request.user.pk, allowed_role='doctor' if membership.is_clinician else None)
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
        'message_threads': _conversation_threads(company, patient, patient_view=False, viewer=request.user, mark_read=records_conversation_view(request)),
        'task_form': ClinicalTaskForm(company=company, patient=patient),
        'appointment_form': _staff_booking_form(company, patient=patient),
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
    return context


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

    # Adding a task, booking or conversation changes the record: only the current clinician does. A note
    # can also come from a clinician covering an appointment (PatientNoteCreateView).
    record_write = 'change'

    def get_patient(self):
        from .record_access import require_record_change, require_record_notes, require_record_view

        patient = get_object_or_404(Patient, pk=self.kwargs['patient_pk'], company=self.company, is_active=True)
        if self.request.method in ('GET', 'HEAD', 'OPTIONS'):
            require_record_view(self.membership, self.request.user, patient)
        else:
            (require_record_notes if self.record_write == 'notes' else require_record_change)(
                self.membership, self.request.user, patient)
        return patient

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
        from .appointment_detail_views import staff_booking_form
        form = staff_booking_form(self.company, request.POST, patient=patient)
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
    record_write = 'notes'
    http_method_names = ('post',)

    def post(self, request, *args, **kwargs):
        patient = self.get_patient()
        if not self.membership.is_clinician:
            raise PermissionDenied('Only a clinician in this practice can add a clinical note.')
        if request.POST.get('return_to') == 'overview':
            return self.log_note(request, patient)
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

    def log_note(self, request, patient):
        """Create note on the Overview's log: one text box, then back to the log with the note in it."""
        from care.forms import LogNoteForm

        from .patient_workspace import workspace_tab_context, workspace_url

        form = LogNoteForm(request.POST, company=self.company, patient=patient, author=request.user, auto_id='log_note_%s')
        if not form.is_valid():
            context = workspace_tab_context(request, self.company, self.membership, patient, 'overview',
                                            log_note_form=form, open_dialog='note')
            return render(request, 'portal/patient_workspace.html', context, status=400)
        with transaction.atomic():
            Company.objects.select_for_update().get(pk=self.company.pk)
            note = form.save()
            record_audit(company=self.company, actor=request.user, patient=patient, action='clinical_note.created', target=note, request=request)
        messages.success(request, 'Note added.')
        return redirect(f'{workspace_url(patient)}#log-note-{note.pk}')


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
            messages.success(request, 'Task marked complete. The data request is resolved and the patient can see it.'
                             if getattr(task, 'data_request', None) else 'Task marked complete.')
        return _task_redirect(request, task, self.membership)


class StaffMessageCreateView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('post',)

    def post(self, request, pk):
        from .inbox import staff_inbox_redirect

        thread = get_object_or_404(
            staff_threads(self.company, request.user).select_related('patient'),
            pk=pk,
            is_closed=False,
        )
        # Replies are written on the record's Messages tab ("inbox" is its older name) or on Patient messages.
        return_to = request.POST.get('return_to')
        from .record_access import no_records

        # Practice administrators don't open records, so they always write, and come back, in Patient messages.
        from_record = return_to in ('record', 'inbox') and not no_records(self.membership)
        from_inbox = return_to == 'patient_messages' or no_records(self.membership)
        form = PatientMessageForm(request.POST, auto_id='record_message_%s' if from_record or from_inbox else 'id_%s')
        if form.is_valid():
            try:
                post_staff_reply(thread=thread, sender=request.user, body=form.cleaned_data['body'], request=request)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, 'Secure message sent.')
                if from_inbox:
                    from .inbox import search_text, staff_inbox_url
                    return redirect(staff_inbox_url(thread, status=request.POST.get('status', ''), search=search_text(request.POST.get('q')),
                                                    patients=request.POST.get('patients', '')))
                if from_record:
                    return staff_inbox_redirect(thread)
                from .patient_workspace import workspace_url
                return redirect(workspace_url(thread.patient, 'messages', thread=thread.pk))
        if from_inbox:
            from .inbox import render_staff_inbox

            return render_staff_inbox(request, self.company, self.membership, message_form=form, reply_thread_id=thread.pk)
        if from_record:
            from .record_messages import render_record_messages

            return render_record_messages(request, self.company, self.membership, thread.patient,
                                          record_message_form=form, failed_form='message_form', reply_thread_id=thread.pk)
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
                messages.success(request, 'Your message has been sent.')
                return patient_messages_redirect(thread)
        return _invalid_patient_form(request, self.patient_company, self.patient, 'message_form', form, reply_thread_id=thread.pk)


class PatientThreadCreateView(LoginRequiredMixin, PatientPortalRequiredMixin, View):
    http_method_names = ('post',)

    def post(self, request):
        from .patient_views import patient_messages_redirect

        form = PatientThreadForm(request.POST, auto_id='thread_%s', patient=self.patient)
        try:
            validate_patient_context(request, self.patient_company, self.patient)
        except ValidationError as error:
            form.add_error(None, error)
        if not form.is_valid():
            return _invalid_patient_form(request, self.patient_company, self.patient, 'thread_form', form)
        try:
            thread = start_conversation(patient=self.patient, opened_by=request.user, recipient=form.cleaned_data['recipient'],
                                        subject=form.cleaned_data['subject'], body=form.cleaned_data['body'], request=request)
        except ValidationError as error:
            form.add_error(None, error)
            return _invalid_patient_form(request, self.patient_company, self.patient, 'thread_form', form)
        messages.success(request, f"Your message has been sent to {form.cleaned_data['recipient'].full_name}.")
        return patient_messages_redirect(thread)


class StaffThreadCreateView(StaffPatientActionMixin, View):
    http_method_names = ('post',)

    def post(self, request, *args, **kwargs):
        patient = self.get_patient()
        from_record = request.POST.get('return_to') == 'record'
        data = request.POST.copy()
        if from_record:
            # The record's messages block names a first conversation itself.
            from .record_messages import new_thread_subject

            data['subject'] = new_thread_subject(request.user, self.membership)
        form = PatientThreadForm(data, auto_id='thread_%s')
        thread = None
        if form.is_valid():
            try:
                thread = start_conversation(patient=patient, opened_by=request.user, subject=form.cleaned_data['subject'],
                                            body=form.cleaned_data['body'], request=request)
            except ValidationError as error:
                form.add_error(None, error)
        if thread is None:
            if from_record:
                from .record_messages import render_record_messages

                return render_record_messages(request, self.company, self.membership, patient, record_new_draft=data.get('body', ''),
                                              record_thread_error=' '.join(message for errors in form.errors.values() for message in errors))
            return self.invalid_form(patient, 'thread_form', form)
        messages.success(request, 'Secure conversation started. Only you and the patient can read it.')
        if from_record:
            from .inbox import staff_inbox_redirect
            return staff_inbox_redirect(thread)
        from .patient_workspace import workspace_url
        return redirect(workspace_url(patient, 'messages', thread=thread.pk))


class StaffInboxThreadCreateView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    """New patient message from Patient messages: choose the patient, then write."""

    http_method_names = ('post',)

    def post(self, request):
        from .inbox import NewPatientThreadForm, render_staff_inbox, staff_inbox_url

        form = NewPatientThreadForm(request.POST, company=self.company, user=request.user, membership=self.membership,
                                    auto_id='new_thread_%s')
        if form.is_valid():
            try:
                thread = start_conversation(patient=form.cleaned_data['patient'], opened_by=request.user,
                                            subject=form.cleaned_data['subject'] or form.default_subject,
                                            body=form.cleaned_data['body'], request=request)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, 'Secure conversation started. Only you and the patient can read it.')
                return redirect(staff_inbox_url(thread))
        return render_staff_inbox(request, self.company, self.membership, new_thread_form=form, failed_form='new_thread_form')


def _task_redirect(request, task, membership):
    next_url = request.POST.get('next')
    if next_url and url_has_allowed_host_and_scheme(
        url=next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return HttpResponseRedirect(next_url)
    from .record_access import no_records

    # Practice administrators don't open records: back to My tasks.
    if task.patient_id and not no_records(membership):
        from .patient_workspace import workspace_url
        return redirect(workspace_url(task.patient, 'tasks'))
    return redirect('portal:staff-tasks')


class StaffThreadReadStateView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    """Mark a conversation read (everything in it) or unread (the latest message from the patient or a colleague),
    for this staff member only; the others in the conversation keep their own read state."""

    http_method_names = ('post',)

    def post(self, request, pk):
        from urllib.parse import urlencode

        from care.messaging import set_staff_read_state

        thread = get_object_or_404(staff_threads(self.company, request.user).select_related('patient', 'company'), pk=pk)
        state = request.POST.get('state')
        if state not in ('read', 'unread'):
            return HttpResponseBadRequest('Choose read or unread.')
        set_staff_read_state(thread=thread, user=request.user, read=state == 'read', request=request)
        messages.success(request, 'Conversation marked as read.' if state == 'read' else 'Conversation marked as unread.')
        from .inbox import search_text

        search = search_text(request.POST.get('q'))
        if request.POST.get('return_to') == 'record':
            # Back to the record's Messages tab with no conversation open, so it stays unread.
            from .patient_workspace import workspace_url
            return redirect(workspace_url(thread.patient, 'messages', **({'q': search} if search else {})))
        # Back to Patient messages with no conversation open, keeping its filters, search and page.
        from .inbox import staff_inbox_url
        page = request.POST.get('page', '')
        return redirect(staff_inbox_url(status=request.POST.get('status', ''), page=page if page.isdecimal() and len(page) < 10 else 1,
                                        search=search, patients=request.POST.get('patients', '')))


def _staff_booking_form(company, *args, **kwargs):
    from .appointment_detail_views import staff_booking_form
    return staff_booking_form(company, *args, **kwargs)
