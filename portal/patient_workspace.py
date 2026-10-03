"""One patient workspace; each request renders one permission-scoped section."""

from urllib.parse import urlencode

from django import forms
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Prefetch, Q
from django.db.models.fields.json import KeyTextTransform
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.forms import AppointmentForm, ClinicalNoteForm, ClinicalTaskForm, PatientMessageForm, PatientThreadForm
from care.models import (
    Appointment, AuditEvent, ClinicalEncounter, ClinicalNote, ClinicalTask, LabRequest,
    MessageThread, PatientMessage, PatientSubscription, Payment, RecordTag, Shipment, TreatmentAuthorization,
)
from care.patient_assignment import can_assign_doctor
from care.services import record_audit
from care.task_services import visible_tasks
from practices.models import CompanyMembership, Patient
from .clinical_tasks import attach_clinical_task_links
from .patient_assignment_views import AssignedDoctorForm
from .record_weight import record_weight_context
from .views import StaffCompanyRequiredMixin, _attach_appointment_proposals
from .workflow_context import make_workflow_context


TABS = (
    ('overview', 'Overview'), ('history', 'History'), ('appointments', 'Appointments'),
    ('consultations', 'Consultations'), ('blood-tests', 'Blood tests'), ('notes', 'Notes'),
    ('weights', 'Weight'), ('messages', 'Messages'), ('tasks', 'Tasks'),
    ('payments', 'Payments'), ('treatment', 'Treatment'), ('deliveries', 'Deliveries'),
)
CLINICAL_TABS = {'consultations', 'blood-tests', 'notes', 'treatment'}
OPERATIONAL_AUDIT_LABELS = {
    'appointment.created': 'Appointment booked', 'appointment.patient_booked': 'Appointment booked by patient',
    'appointment.status_changed': 'Appointment status changed', 'appointment.proposal.accepted': 'Appointment time changed',
    'patient.contact_updated': 'Patient contact details updated', 'patient.doctor_assigned': 'Assigned clinician changed',
    'shipment.created': 'Delivery prepared',
    'shipment.locked': 'Delivery contents locked', 'shipment.dispatched': 'Delivery dispatched',
    'pharmacy_order.submitted': 'Supply request submitted', 'pharmacy_order.accepted': 'Supply request accepted',
    'pharmacy_order.cancelled': 'Supply request cancelled',
    'patient.account_created': 'Patient login created', 'patient.record_created': 'Patient record created',
    'lead.converted': 'Enquiry converted to patient',
}

# Status changes share one action; the status it recorded picks the label.
APPOINTMENT_STATUS_LABELS = {
    'cancelled': 'Appointment cancelled', 'completed': 'Appointment completion recorded',
    'no_show': 'Appointment non-attendance recorded',
}


def workspace_url(patient, tab='overview', **query):
    return f'{reverse("portal:patient-detail", args=[patient.pk])}?{urlencode({"tab": tab, **query})}'


def patient_workspace_context(request, company, membership, patient, tab='overview'):
    clinical = membership.has_clinical_access
    return dict(
        patient_workspace=True, workspace_base_template='portal/patient_workspace_base.html',
        workspace_patient=patient, workspace_tab=tab, workspace_url=workspace_url(patient, tab),
        workspace_tabs=[dict(name=name, label=label, url=workspace_url(patient, name), active=name == tab)
                        for name, label in TABS if clinical or name not in CLINICAL_TABS],
        patient=patient, company=company, active_membership=membership, nav_section='record',
        is_doctor=membership.is_prescriber, is_clinician=membership.is_clinician, can_view_clinical_notes=clinical,
        can_add_clinical_note=membership.is_clinician,
    )


def scoped(model, company, patient):
    return model.objects.for_company(company).filter(patient=patient, patient__company=company, patient__is_active=True)


class WorkspaceFilterForm(forms.Form):
    status = forms.ChoiceField(required=False)

    def __init__(self, *args, choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['status'].choices = (('all', 'All statuses'), *choices)


def paginated(request, patient, tab, queryset, choices=()):
    data = request.GET.copy()
    data.setdefault('status', 'all')
    form = WorkspaceFilterForm(data, choices=choices)
    status = 'all'
    if form.is_valid():
        status = form.cleaned_data['status'] or 'all'
        if status != 'all':
            queryset = queryset.filter(status=status)
    else:
        queryset = queryset.none()
    page = Paginator(queryset, 20).get_page(request.GET.get('page'))
    return dict(
        workspace_filter=form if choices else None, workspace_page=page, page_obj=page,
        rows=list(page.object_list), workspace_row_count=page.paginator.count,
        workspace_previous_url=workspace_url(patient, tab, status=status, page=page.previous_page_number()) if page.has_previous() else '',
        workspace_next_url=workspace_url(patient, tab, status=status, page=page.next_page_number()) if page.has_next() else '',
    )


def consultations(company, membership, patient, actor):
    visible = Q(status=ClinicalEncounter.Status.SIGNED)
    if membership.is_clinician:
        visible |= Q(clinician=actor)
    return scoped(ClinicalEncounter, company, patient).filter(visible).select_related('clinician').defer('clinical_summary').order_by('-occurred_at', '-pk')


def workspace_messages(request, company, membership, patient, **overrides):
    # Only conversations this staff member takes part in, even on the patient's own record.
    threads = scoped(MessageThread, company, patient).filter(participants=request.user)
    if patient.user_id:
        # Conversations open only when chosen, so the list shows what is still unread.
        threads = threads.annotate(unread_count=Count('messages', filter=Q(
            messages__company=company, messages__sender_id=patient.user_id, messages__read_at__isnull=True,
        )))
    threads = threads.order_by('-last_message_at', '-pk')
    page = Paginator(threads, 20).get_page(request.GET.get('page'))
    raw = overrides.get('reply_thread_id') or request.GET.get('thread')
    selected = None
    if raw is not None:
        value = str(raw)
        if not value.isascii() or not value.isdecimal() or len(value) > 19 or not 0 < int(value) < 2**63:
            raise Http404
        selected = get_object_or_404(threads, pk=value)
    from care.team_messaging import team_threads
    team = team_threads(company, request.user).filter(patient=patient).prefetch_related('members').order_by('-last_message_at', '-pk')[:10]
    context = dict(message_thread_page=page, workspace_message_threads=page.object_list, selected_thread=selected, selected_thread_id=selected.pk if selected else None,
                   team_threads_about_patient=list(team),
                   message_threads=[selected] if selected else [],
                   message_form=PatientMessageForm(), thread_form=PatientThreadForm(auto_id='thread_%s'))
    context.update(overrides)
    if selected:
        messages = PatientMessage.objects.for_company(company).filter(thread=selected).select_related('sender').order_by('-created_at', '-pk')
        message_page = Paginator(messages, 50).get_page(request.GET.get('message_page'))
        selected.participant_rows = list(selected.participant_links.select_related('user', 'added_by'))
        selected.conversation_messages = list(reversed(list(message_page.object_list)))
        context.update(message_page=message_page,
            older_message_url=workspace_url(patient, 'messages', thread=selected.pk, message_page=message_page.next_page_number()) if message_page.has_next() else '',
            newer_message_url=workspace_url(patient, 'messages', thread=selected.pk, message_page=message_page.previous_page_number()) if message_page.has_previous() else '')
        if request.method == 'GET':
            ids = [message.pk for message in selected.conversation_messages if message.sender_id == patient.user_id and message.read_at is None]
            if ids:
                PatientMessage.objects.for_company(company).filter(pk__in=ids, read_at__isnull=True).update(read_at=timezone.now())
    for thread in context['workspace_message_threads']:
        thread.workspace_url = workspace_url(patient, 'messages', thread=thread.pk)
        if selected and thread.pk == selected.pk and request.method == 'GET':
            thread.unread_count = 0
    context.update(
        workspace_previous_url=workspace_url(patient, 'messages', page=page.previous_page_number()) if page.has_previous() else '',
        workspace_next_url=workspace_url(patient, 'messages', page=page.next_page_number()) if page.has_next() else '',
        workspace_page=page,
    )
    return _attach_appointment_proposals(context, request, company, patient, actor_role='doctor', allowed=membership.is_clinician)


def workspace_tab_context(request, company, membership, patient, tab, **overrides):
    if tab not in dict(TABS):
        raise Http404
    if tab in CLINICAL_TABS and not membership.has_clinical_access:
        raise PermissionDenied('This clinical section is restricted to clinicians and practice Super Admins.')
    context = patient_workspace_context(request, company, membership, patient, tab)
    context['page_title'] = dict(TABS)[tab]
    if tab == 'overview':
        context.update(overview_cards=[
            dict(label='Appointments', count=scoped(Appointment, company, patient).count(), url=workspace_url(patient, 'appointments')),
            dict(label='Open tasks', count=scoped(ClinicalTask, company, patient).filter(status__in=('open', 'in_progress')).count(), url=workspace_url(patient, 'tasks')),
            dict(label='Conversations', count=scoped(MessageThread, company, patient).filter(participants=request.user).count(), url=workspace_url(patient, 'messages')),
            dict(label='Deliveries', count=scoped(Shipment, company, patient).count(), url=workspace_url(patient, 'deliveries')),
        ])
        # Existing integrations may request this token before their dedicated
        # legacy booking POST. No form or booking list is rendered on Overview.
        context['legacy_appointment_context'] = make_workflow_context(request, company, 'legacy-staff-booking', patient)
        if can_assign_doctor(membership):
            context.update(doctor_form=AssignedDoctorForm(company=company, initial={'doctor': patient.assigned_doctor_id}),
                           doctor_assignment_context=make_workflow_context(request, company, 'patient-doctor-assignment', patient))
    elif tab == 'appointments':
        context.update(paginated(request, patient, tab, scoped(Appointment, company, patient).select_related('clinician').order_by('-starts_at', '-pk'), Appointment.Status.choices))
        from video.access import attach_video_join
        attach_video_join(context['rows'], request.user.pk, allowed_role='doctor' if membership.is_clinician else None)
        context.update(appointment_form=AppointmentForm(company=company, patient=patient),
                       legacy_appointment_context=request.POST.get('workflow_context', '') if request.method == 'POST' else make_workflow_context(request, company, 'legacy-staff-booking', patient))
        context['appointment_form'].fields['video_link'].widget = forms.HiddenInput()
    elif tab == 'consultations':
        context.update(paginated(request, patient, tab, consultations(company, membership, patient, request.user), ClinicalEncounter.Status.choices))
    elif tab == 'blood-tests':
        context.update(paginated(request, patient, tab, scoped(LabRequest, company, patient).select_related('requested_by').defer('result_summary').order_by('-requested_on', '-pk'), LabRequest.Status.choices))
    elif tab == 'notes':
        visibility = Q(is_private=False)
        if membership.is_clinician:
            visibility |= Q(author=request.user)
        notes = scoped(ClinicalNote, company, patient).filter(visibility, signed_encounter__isnull=True).select_related('author').prefetch_related(Prefetch('tags', queryset=RecordTag.objects.for_company(company))).order_by('-created_at', '-pk')
        context.update(paginated(request, patient, tab, notes))
        from care.tag_forms import ClinicalNoteTagsForm
        failed_note_id = overrides.get('failed_note_tag_id')
        if failed_note_id and not any(note.pk == failed_note_id for note in context['rows']):
            failed_note = notes.filter(pk=failed_note_id, author=request.user).first()
            if failed_note:
                context['rows'].append(failed_note)
        for note in context['rows']:
            if membership.is_clinician and note.author_id == request.user.pk:
                note.tags_form = overrides['note_tags_form'] if note.pk == overrides.get('failed_note_tag_id') else ClinicalNoteTagsForm(company=company, instance=note)
        context['notes'] = context['rows']
        context['note_form'] = ClinicalNoteForm(company=company, patient=patient, author=request.user) if membership.is_clinician else None
    elif tab == 'weights':
        context.update(record_weight_context(request, company, patient))
        for key, number in (('record_weight_previous_url', 'previous_page_number'), ('record_weight_next_url', 'next_page_number')):
            if context[key]:
                context[key] = workspace_url(patient, 'weights', weight_page=getattr(context['record_weight_page'], number)()) + '#weights'
    elif tab == 'messages':
        context.update(workspace_messages(request, company, membership, patient, **overrides))
    elif tab == 'tasks':
        tasks = scoped(ClinicalTask, company, patient).select_related('assigned_to', 'created_by', 'encounter_signing', 'lab_review_request', 'compounding_record', 'authorization_reminder').prefetch_related(Prefetch('tags', queryset=RecordTag.objects.for_company(company))).order_by('-created_at', '-pk')
        context.update(paginated(request, patient, tab, tasks, ClinicalTask.Status.choices))
        context['tasks'] = attach_clinical_task_links(context['rows'], request.user, membership)
        from .patient_action_context import workspace_destination
        for task in context['tasks']:
            task.workspace_can_edit = membership.role != 'doctor' or request.user.pk in (task.assigned_to_id, task.created_by_id)
            task.workspace_workflow_url = workspace_destination(request, task.workflow_url, force=True) if task.workflow_url else ''
        context['task_form'] = ClinicalTaskForm(company=company, patient=patient)
    elif tab == 'payments':
        context.update(paginated(request, patient, tab, scoped(Payment, company, patient).only('pk', 'amount', 'due_on', 'paid_at', 'status').order_by('-due_on', '-pk'), Payment.Status.choices))
    elif tab == 'treatment':
        authorizations = scoped(TreatmentAuthorization, company, patient).filter(product__company=company).select_related('product', 'prescribed_by').order_by('-created_at', '-pk')
        context.update(paginated(request, patient, tab, authorizations, TreatmentAuthorization.Status.choices))
    elif tab == 'deliveries':
        context.update(paginated(request, patient, tab, scoped(Shipment, company, patient).defer('dispatch_snapshot', 'hold_reason').order_by('-scheduled_for', '-pk'), Shipment.Status.choices))
    elif tab == 'history':
        # Administrative users get typed audit labels, never arbitrary clinical
        # events, metadata, IP addresses, note text, reports, or private profiles.
        rows = scoped(AuditEvent, company, patient).filter(action__in=OPERATIONAL_AUDIT_LABELS).annotate(
            new_status=KeyTextTransform('to', 'metadata'),
        ).only('pk', 'created_at', 'action').order_by('-created_at', '-pk')
        context.update(paginated(request, patient, tab, rows))
        for row in context['rows']:
            row.workspace_label = (APPOINTMENT_STATUS_LABELS.get(row.new_status) if row.action == 'appointment.status_changed' else None) \
                or OPERATIONAL_AUDIT_LABELS[row.action]
    context.update(overrides)
    return context


def render_workspace_form_error(request, company, membership, patient, name, form, **extra):
    tab = {'appointment_form': 'appointments', 'task_form': 'tasks', 'note_form': 'notes',
           'note_tags_form': 'notes', 'message_form': 'messages', 'thread_form': 'messages', 'proposal_form': 'messages'}[name]
    context = workspace_tab_context(request, company, membership, patient, tab, **{name: form, 'failed_form': name}, **extra)
    return render(request, 'portal/patient_workspace.html', context)


@method_decorator(never_cache, name='dispatch')
class PatientWorkspaceView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('get', 'head', 'options')

    def get(self, request, pk):
        patient = get_object_or_404(Patient.objects.for_company(self.company).select_related('user', 'assigned_doctor'), pk=pk, is_active=True)
        tab = request.GET['tab'] if 'tab' in request.GET else ('messages' if 'thread' in request.GET else 'weights' if 'weight_page' in request.GET else 'overview')
        if tab == 'history' and self.membership.has_clinical_access:
            from .record_views import ClinicalRecordView
            return ClinicalRecordView.as_view()(request, pk=pk)
        context = workspace_tab_context(request, self.company, self.membership, patient, tab)
        if request.method == 'GET':
            record_audit(company=self.company, actor=request.user, patient=patient, action='patient.record_viewed', target=patient,
                         request=request, metadata={'section': tab})
        return render(request, 'portal/patient_workspace.html', context)
