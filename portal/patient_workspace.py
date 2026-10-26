"""One patient workspace; each request renders one permission-scoped section."""

from urllib.parse import urlencode

from django import forms
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Prefetch, Q
from django.db.models.fields.json import KeyTextTransform
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.forms import ClinicalNoteForm, ClinicalTaskForm
from care.models import (
    Appointment, AuditEvent, ClinicalEncounter, ClinicalTask, LabRequest,
    PatientSubscription, Payment, RecordTag, Shipment, TreatmentAuthorization,
)
from care.patient_details import can_edit_details
from care.services import record_audit
from care.task_services import visible_tasks
from practices.models import CompanyMembership, Patient
from .clinical_tasks import attach_clinical_task_links
from .filter_menus import choice_section, filter_menu
from .people_tags import in_join_order
from .record_access import require_record_view
from .realtime_context import is_realtime_snapshot
from .record_weight import record_weight_tab_context
from .views import StaffCompanyRequiredMixin
from .workflow_context import make_workflow_context


TABS = (
    ('overview', 'Overview'), ('history', 'History'), ('appointments', 'Appointments'),
    ('consultations', 'Consultations'), ('blood-tests', 'Blood results'), ('notes', 'Notes'),
    ('weights', 'Weight'), ('messages', 'Messages'), ('tasks', 'Tasks'),
    ('payments', 'Payments'), ('treatment', 'Treatment'), ('deliveries', 'Dispatch history'),
)
CLINICAL_TABS = {'consultations', 'blood-tests', 'notes', 'treatment'}
# Doctors (the Clinician role) work from the Overview: they also see Blood results and, for their own patients, Messages.
CLINICIAN_TABS = {'overview', 'blood-tests', 'messages'}
OPERATIONAL_AUDIT_LABELS = {
    'appointment.created': 'Appointment booked', 'appointment.patient_booked': 'Appointment booked by patient',
    'appointment.status_changed': 'Appointment status changed', 'appointment.proposal.accepted': 'Appointment time changed',
    'appointment.time_changed': 'Appointment time changed',
    'patient.contact_updated': 'Patient contact details updated', 'patient.doctor_assigned': 'Prescribing doctor changed',
    'patient.details_updated': 'Patient details updated',
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
    """A record tab's address. A list value repeats its name, one for each filter ticked."""
    return f'{reverse("portal:patient-detail", args=[patient.pk])}?{urlencode({"tab": tab, **query}, doseq=True)}'


def shows_tab(membership, user, patient, tab):
    """Doctors (the Clinician role) see the Overview, Blood results and, only as the patient's Prescribing doctor,
    Messages: another doctor's conversations with the patient aren't theirs to read. Super Admins see every tab.
    Practice administrators don't open records (portal.record_access)."""
    if tab in CLINICAL_TABS and not membership.has_clinical_access:
        return False
    if membership.role != CompanyMembership.Role.DOCTOR:
        return True
    return tab in CLINICIAN_TABS and (tab != 'messages' or patient.assigned_doctor_id == user.pk)


def patient_workspace_context(request, company, membership, patient, tab='overview'):
    from .record_access import CHANGE, COVER, READ, cover_appointment, record_access

    # A clinician assigned to this patient before reads the record; one covering an appointment also writes notes.
    access = record_access(membership, request.user, patient)
    changeable = access == CHANGE
    # A page under a tab this person doesn't see (a consultation, an appointment) sits under the Overview.
    section = tab if shows_tab(membership, request.user, patient, tab) else 'overview'
    return dict(
        can_change_record=changeable, can_write_notes=access in (CHANGE, COVER),
        record_read_only=access == READ,
        record_cover_appointment=cover_appointment(patient, request.user) if access == COVER else None,
        patient_workspace=True, workspace_base_template='portal/patient_workspace_base.html',
        workspace_patient=patient, workspace_tab=tab, workspace_url=workspace_url(patient, tab),
        workspace_tabs=[dict(name=name, label=label, url=workspace_url(patient, name), active=name == section)
                        for name, label in TABS if shows_tab(membership, request.user, patient, name)],
        workspace_return_url=workspace_url(patient, section), workspace_return_label=f'Back to {dict(TABS)[section].lower()}',
        is_super_admin=membership.role == CompanyMembership.Role.SUPER_ADMIN,
        can_edit_details=can_edit_details(membership),
        patient=patient, company=company, active_membership=membership, nav_section='record',
        is_doctor=membership.is_prescriber, is_clinician=membership.is_clinician, can_view_clinical_notes=membership.has_clinical_access,
        can_add_clinical_note=membership.is_clinician and access in (CHANGE, COVER),
    )


def hidden_tab_destination(request, patient, tab):
    """Where a link to a tab a doctor doesn't see goes: a conversation opens in Patient messages if they're in it,
    anything else on the Overview."""
    thread = request.GET.get('thread', '')
    if tab == 'messages' and thread.isascii() and thread.isdecimal() and len(thread) <= 19:
        return f'{reverse("portal:staff-inbox")}?{urlencode({"thread": thread})}'
    return workspace_url(patient)


def age(born, today=None):
    """Whole years since the date of birth, or None when it isn't recorded."""
    if born is None:
        return None
    today = today or timezone.localdate()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def scoped(model, company, patient):
    return model.objects.for_company(company).filter(patient=patient, patient__company=company, patient__is_active=True)


class WorkspaceFilterForm(forms.Form):
    """The Filters menu over a record table: a section per field, with any number of its choices ticked."""

    def __init__(self, *args, sections=(), **kwargs):
        super().__init__(*args, **kwargs)
        for section in sections:
            self.fields[section.name] = forms.MultipleChoiceField(
                label=section.label, required=False, choices=section.choices, widget=forms.CheckboxSelectMultiple)


class FilterSection:
    """One section of the Filters menu: the query name, its label and icon, the field it filters, and its choices."""

    def __init__(self, name, label, lookup, choices, icon='status'):
        self.name, self.label, self.lookup, self.icon = name, label, lookup, icon
        self.choices = [(str(value), text) for value, text in choices]


def doctor_filter(queryset, field='clinician', label='Doctor'):
    """Everyone who appears in this table for this patient, by name."""
    from django.contrib.auth import get_user_model

    people = get_user_model().objects.filter(pk__in=queryset.order_by().values(field)).order_by('first_name', 'last_name', 'pk')
    return FilterSection('doctor', label, field, [(person.pk, person.full_name) for person in people], icon='person')


def paginated(request, patient, tab, queryset, choices=(), filters=None):
    if filters is None:
        filters = [FilterSection('status', 'Status', 'status', choices)] if choices else []
    data = request.GET.copy()
    for section in filters:
        # "all" is what the old status filter sent for no filter.
        data.setlist(section.name, [value for value in request.GET.getlist(section.name) if value != 'all'])
    form = WorkspaceFilterForm(data, sections=filters)
    selected = []
    if form.is_valid():
        for section in filters:
            values = form.cleaned_data[section.name]
            if values:
                queryset = queryset.filter(**{f'{section.lookup}__in': values})
                selected.append((section.name, values))
    else:
        queryset = queryset.none()
    page = Paginator(queryset, 20).get_page(request.GET.get('page'))
    query = {name: values for name, values in selected}
    menu = filter_menu(
        action=reverse('portal:patient-detail', args=[patient.pk]), hidden=[('tab', tab)], form=form,
        clear_url=workspace_url(patient, tab), label=f'Filter {dict(TABS)[tab].lower()}',
        sections=[choice_section(section.name, section.label, section.choices, data.getlist(section.name),
                                 icon=section.icon, errors=form[section.name].errors) for section in filters],
    ) if filters else None
    return dict(
        workspace_filter=form if filters else None, filter_menu=menu,
        workspace_page=page, page_obj=page,
        rows=list(page.object_list), workspace_row_count=page.paginator.count,
        workspace_previous_url=workspace_url(patient, tab, **query, page=page.previous_page_number()) if page.has_previous() else '',
        workspace_next_url=workspace_url(patient, tab, **query, page=page.next_page_number()) if page.has_next() else '',
    )


def consultations(company, membership, patient, actor):
    visible = Q(status=ClinicalEncounter.Status.SIGNED)
    if membership.is_clinician:
        visible |= Q(clinician=actor)
    return scoped(ClinicalEncounter, company, patient).filter(visible).select_related('clinician').defer('clinical_summary').order_by('-occurred_at', '-pk')


def workspace_messages(request, company, membership, patient, **overrides):
    """The Messages tab: conversations with the patient, then team conversations about them that include you."""
    from care.team_messaging import team_threads

    from .record_messages import record_messages_context

    raw = request.GET.get('thread')
    if raw is not None and not overrides.get('reply_thread_id'):
        # A link to a conversation that isn't well formed, or that you aren't in, isn't found.
        value = str(raw)
        if not value.isascii() or not value.isdecimal() or len(value) > 19 or not 0 < int(value) < 2**63:
            raise Http404
    context = record_messages_context(request, company, membership, patient, **overrides)
    if raw is not None and not overrides.get('reply_thread_id') and context['record_thread'] is None:
        raise Http404
    team = team_threads(company, request.user).filter(patient=patient).prefetch_related(in_join_order('members')).order_by('-last_message_at', '-pk')[:10]
    context['team_threads_about_patient'] = list(team)
    return context


def workspace_tab_context(request, company, membership, patient, tab, **overrides):
    if tab not in dict(TABS):
        raise Http404
    if tab in CLINICAL_TABS and not membership.has_clinical_access:
        raise PermissionDenied('This clinical section is restricted to clinicians and practice Super Admins.')
    if not shows_tab(membership, request.user, patient, tab):
        # A form sent from a tab a doctor doesn't see comes back on the Overview.
        tab = 'overview'
    context = patient_workspace_context(request, company, membership, patient, tab)
    context['page_title'] = dict(TABS)[tab]
    if tab == 'overview' and membership.has_clinical_access:
        # Clinicians and Super Admins get the clinical Overview: log, prescription, weight and summary data.
        from .record_overview import record_overview_context

        dialog = overrides.get('open_dialog') or request.GET.get('prescription', '') or ('note' if request.GET.get('note') == 'new' else '')
        context.update(record_overview_context(request, company, membership, patient, log_filter=request.GET.get('log', 'clinical'),
                                               open_dialog=dialog if dialog in ('initiate', 'renew', 'change', 'note') else ''))
        if context['can_add_clinical_note']:
            from care.forms import LogNoteForm

            context['log_note_form'] = overrides.get('log_note_form') or LogNoteForm(
                company=company, patient=patient, author=request.user, auto_id='log_note_%s')
    if tab == 'overview':
        # Conversations live on the Messages tab, not here.
        # Existing integrations may request this token before their dedicated
        # legacy booking POST. No form or booking list is rendered on Overview.
        context['legacy_appointment_context'] = make_workflow_context(request, company, 'legacy-staff-booking', patient)
        context['patient_age'] = age(patient.date_of_birth)
    elif tab == 'appointments':
        appointments = scoped(Appointment, company, patient).select_related('clinician').order_by('-starts_at', '-pk')
        context.update(paginated(request, patient, tab, appointments, filters=[
            FilterSection('status', 'Status', 'status', Appointment.Status.choices),
            FilterSection('type', 'Appointment type', 'appointment_type', Appointment.Type.choices, icon='type'),
            doctor_filter(appointments)]))
        from video.access import attach_video_join, join_window
        attach_video_join(context['rows'], request.user.pk, allowed_role='doctor' if membership.is_clinician else None)
        for row in context['rows']:
            # The Join window column: when the room opens and closes, for a booking that's still on.
            row.join_window = join_window(row) if row.status == Appointment.Status.BOOKED else None
        from .appointment_detail_views import staff_booking_form
        context.update(appointment_form=staff_booking_form(company, patient=patient),
                       legacy_appointment_context=request.POST.get('workflow_context', '') if request.method == 'POST' else make_workflow_context(request, company, 'legacy-staff-booking', patient))
    elif tab == 'consultations':
        encounters = consultations(company, membership, patient, request.user)
        context.update(paginated(request, patient, tab, encounters, filters=[
            FilterSection('status', 'Status', 'status', ClinicalEncounter.Status.choices), doctor_filter(encounters)]))
    elif tab == 'blood-tests':
        # Results recorded by hand (most are read on the PathCare portal), then the blood test requests.
        from .blood_result_views import blood_results_context

        context.update(blood_results_context(request, company, membership, patient, context, **overrides))
        context.update(paginated(request, patient, tab, scoped(LabRequest, company, patient).select_related('requested_by').defer('result_summary').order_by('-requested_on', '-pk'), LabRequest.Status.choices))
    elif tab == 'notes':
        # Signed consult notes and general notes, newest first: the same list as Patient notes in a conversation.
        from .patient_notes import PatientNotes, note_entry
        notes = PatientNotes(company, membership, patient, request.user)
        context.update(paginated(request, patient, tab, notes))
        from care.tag_forms import ClinicalNoteTagsForm
        failed_note_id = overrides.get('failed_note_tag_id')
        if failed_note_id and not any(note.pk == failed_note_id for note in context['rows']):
            failed_note = notes.notes.filter(pk=failed_note_id, author=request.user).first()
            if failed_note:
                context['rows'].append(note_entry(failed_note))
        for note in context['rows']:
            if note.kind == 'note' and context['can_add_clinical_note'] and note.author_id == request.user.pk:
                note.tags_form = overrides['note_tags_form'] if note.pk == overrides.get('failed_note_tag_id') else ClinicalNoteTagsForm(company=company, instance=note)
        context['notes'] = context['rows']
        context['note_form'] = ClinicalNoteForm(company=company, patient=patient, author=request.user) if context['can_add_clinical_note'] else None
    elif tab == 'weights':
        context.update(record_weight_tab_context(request, company, membership, patient))
        # "Show in log" on a consult note, and setting a goal weight, happen on the Overview.
        context['consult_log_url'] = workspace_url(patient, 'overview')
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
        from care.treatment import other_treatments

        authorizations = scoped(TreatmentAuthorization, company, patient).filter(product__company=company).select_related('product', 'prescribed_by').order_by('-created_at', '-pk')
        context.update(paginated(request, patient, tab, authorizations, TreatmentAuthorization.Status.choices))
        context['other_treatments_offered'] = other_treatments(company).exists()
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
           'note_tags_form': 'notes', 'message_form': 'messages', 'thread_form': 'messages'}[name]
    context = workspace_tab_context(request, company, membership, patient, tab, **{name: form, 'failed_form': name}, **extra)
    return render(request, 'portal/patient_workspace.html', context)


@method_decorator(never_cache, name='dispatch')
class RecordConversationSearchView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    """The record's conversation list for a search typed on its Messages tab, without the rest of the
    record. Only conversations the staff member is in are listed and none is opened, so nothing is
    marked read and the record view isn't logged again for each letter typed."""

    http_method_names = ('get', 'head', 'options')

    def get(self, request, pk):
        from .record_messages import record_messages_context

        patient = get_object_or_404(Patient.objects.for_company(self.company), pk=pk, is_active=True)
        require_record_view(self.membership, request.user, patient)
        context = record_messages_context(request, self.company, self.membership, patient, list_only=True)
        return render(request, 'portal/includes/record_conversation_list.html', context)


@method_decorator(never_cache, name='dispatch')
class PatientWorkspaceView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('get', 'head', 'options')

    def get(self, request, pk):
        patient = get_object_or_404(Patient.objects.for_company(self.company).select_related('user', 'assigned_doctor'), pk=pk, is_active=True)
        require_record_view(self.membership, request.user, patient)
        tab = request.GET['tab'] if 'tab' in request.GET else ('messages' if 'thread' in request.GET else 'weights' if 'weight_page' in request.GET else 'overview')
        if tab in dict(TABS) and not (tab in CLINICAL_TABS and not self.membership.has_clinical_access) \
                and not shows_tab(self.membership, request.user, patient, tab):
            return redirect(hidden_tab_destination(request, patient, tab))
        if tab == 'history' and self.membership.has_clinical_access:
            from .record_views import ClinicalRecordView
            return ClinicalRecordView.as_view()(request, pk=pk)
        context = workspace_tab_context(request, self.company, self.membership, patient, tab)
        if request.method == 'GET' and not (tab == 'messages' and is_realtime_snapshot(request)):
            record_audit(company=self.company, actor=request.user, patient=patient, action='patient.record_viewed', target=patient,
                         request=request, metadata={'section': tab})
        return render(request, 'portal/patient_workspace.html', context)
