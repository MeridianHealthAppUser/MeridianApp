"""The Messages tab of a patient record: the conversations with this patient and the one being read.

A small list on the left shows the conversations with this patient that the staff member takes part
in, the same scope as Patient messages. Opening the tab shows no conversation until one is chosen;
a link to a conversation (from Patient messages, the list or the calendar) opens it.
Showing a conversation marks it read for the staff member reading it, not for the others in it.
"""

from django.core.paginator import Paginator
from django.shortcuts import render
from django.urls import reverse

from care.forms import PatientMessageForm
from care.messaging import mark_read_by_staff
from care.models import PatientMessage
from care.services import record_audit

from .inbox import _inbox_threads, latest_from, search_text, search_threads
from .people_tags import in_join_order, people_tags, staff_kinds
from .record_access import can_change_record, can_view_record


def new_thread_subject(actor, membership):
    """Named with the clinician's title from their profile, so a dietitian may be "Ms Botha" rather than "Dr"."""
    if membership.is_clinician and actor.last_name:
        if actor.title:
            return f'Message from {actor.get_title_display()} {actor.last_name}'
        return f'Message from {actor.full_name}'
    return 'Message from the practice'


def record_messages_context(request, company, membership, patient, *, selected_id=None, list_only=False, **overrides):
    from .patient_workspace import workspace_url

    conversations = _inbox_threads(company, request.user).filter(patient=patient).prefetch_related(in_join_order('participants'))
    has_threads = conversations.exists()
    # A search narrows the list; the open conversation stays open even when it doesn't match.
    search = search_text(request.GET.get('q'))
    # 20 conversations a page, as on Patient messages.
    page = Paginator(search_threads(conversations, company, search) if search else conversations, 20).get_page(
        request.GET.get('page'))
    threads = list(page.object_list)
    kept = {**({'q': search} if search else {}), **({'page': page.number} if page.number > 1 else {})}
    wanted = str(selected_id or overrides.get('reply_thread_id') or request.GET.get('thread') or '')
    # Only a conversation someone chose opens, even one on another page; otherwise the block waits for one.
    selected = None
    if not list_only and wanted.isdecimal():
        selected = next((thread for thread in threads if str(thread.pk) == wanted), None) or conversations.filter(pk=wanted).first()
    shown = {*threads, *([selected] if selected else [])}
    kinds = staff_kinds(company, [person.pk for thread in shown for person in thread.participants.all()])
    for thread in shown:
        thread.record_url = workspace_url(patient, 'messages', thread=thread.pk)
        thread.list_url = workspace_url(patient, 'messages', thread=thread.pk, **kept)
        thread.is_selected = selected is not None and thread.pk == selected.pk
        # Only the other staff are tagged; the patient is the one whose record this is.
        thread.people_tags = people_tags(clinicians(thread, request.user), kinds)
        thread.latest_from = latest_from(thread, request.user, unnamed={patient.user_id})
        thread.read_state_url = read_state_url(thread)
    # A first message that wasn't sent comes back with what was written and why, from this block or an older form.
    legacy_thread_form = overrides.get('thread_form')
    thread_error = overrides.get('record_thread_error', '')
    if legacy_thread_form is not None and not thread_error:
        thread_error = ' '.join(message for errors in legacy_thread_form.errors.values() for message in errors)
    new_draft = overrides.get('record_new_draft') or (legacy_thread_form['body'].value() if legacy_thread_form is not None else '')
    # A clinician assigned to this patient before, but not now, replies in their conversations but starts none.
    changeable = can_change_record(membership, request.user, patient)
    starting = changeable and (not has_threads or request.GET.get('new') == '1' or bool(thread_error))

    def page_url(number):
        return workspace_url(patient, 'messages', **({'q': search} if search else {}), **({'page': number} if number > 1 else {}))

    context = {
        'record_messages': True, 'record_threads': threads, 'record_thread': selected,
        'record_page_obj': page,
        'record_newer_url': page_url(page.previous_page_number()) if page.has_previous() else '',
        'record_older_url': page_url(page.next_page_number()) if page.has_next() else '',
        'record_has_threads': has_threads, 'record_read_state_fields': {'return_to': 'record', 'q': search},
        'search': search, 'search_hidden': {'tab': 'messages'},
        'search_action': reverse('portal:patient-detail', args=[patient.pk]),
        'search_url': reverse('portal:record-conversation-search', args=[patient.pk]),
        'record_starting': selected is None and starting,
        'record_new_url': workspace_url(patient, 'messages', new=1) if changeable else '',
        # Blood tests are requested from the Overview; the button beside the messages goes there.
        'record_bloods_url': workspace_url(patient) + '#record-bloods' if membership.is_prescriber and changeable else '',
        'record_message_form': overrides.get('record_message_form') or PatientMessageForm(auto_id='record_message_%s'),
        'record_new_subject': new_thread_subject(request.user, membership),
        'record_thread_error': thread_error, 'record_new_draft': new_draft or '',
        'message_threads': [selected] if selected else [],
        'failed_form': overrides.get('failed_form', ''), 'reply_thread_id': overrides.get('reply_thread_id'),
    }
    for key in ('proposal_form', 'proposal_error', 'failed_proposal_id'):
        if key in overrides:
            context[key] = overrides[key]
    if selected is None:
        return context
    return open_conversation(request, company, membership, selected, context)


def clinicians(thread, user):
    """The staff in a conversation besides you; the patient is left out."""
    return [person for person in thread.participants.all() if person.pk not in (user.pk, thread.patient.user_id)]


def read_state_url(thread):
    """Mark as read or unread, for a conversation with a message from someone else: the patient or a colleague."""
    return reverse('portal:staff-thread-read-state', args=[thread.pk]) if thread.has_others_message else ''


NOTES_LIMIT = 100


def patient_notes(company, membership, patient, viewer):
    """The patient's notes under an open conversation, so a clinician can read them while replying: the
    same list as the record's Notes tab. None for staff without clinical access, who never see them, and for
    a clinician in the conversation who may not open this patient's record."""
    if not membership.has_clinical_access or not can_view_record(membership, viewer, patient):
        return None
    from .patient_notes import PatientNotes
    from .patient_workspace import workspace_url

    entries = PatientNotes(company, membership, patient, viewer)[:NOTES_LIMIT + 1]
    return {'entries': entries[:NOTES_LIMIT], 'more': len(entries) > NOTES_LIMIT,
            'notes_url': workspace_url(patient, 'notes'), 'consultations_url': workspace_url(patient, 'consultations')}


def open_conversation(request, company, membership, thread, context):
    """The open conversation: its messages, people and appointment suggestions, Message action's form, and for
    clinical staff the patient's notes. Reading it marks it read for this staff member."""
    from .message_management_views import ConversationManagementForm
    from .views import _attach_appointment_proposals
    from .workflow_context import make_workflow_context

    patient = thread.patient
    thread.conversation_messages = list(PatientMessage.objects.for_company(company).filter(
        thread=thread,
    ).select_related('sender').order_by('created_at', 'pk'))
    for message in thread.conversation_messages:
        message.is_from_patient = message.sender_id is not None and message.sender_id == patient.user_id
    if not hasattr(thread, 'has_others_message'):
        # Mark as unread is offered once the patient or a colleague has written; lists annotate this already.
        thread.has_others_message = any(message.sender_id != request.user.pk for message in thread.conversation_messages)
    thread.participant_rows = list(thread.participant_links.select_related('user', 'added_by'))
    # "Dr Marchant added Ms Botha", as the patient and Team messages show it.
    thread.added_rows = [row for row in thread.participant_rows if row.added_by_id]
    # Message action opens in a modal over the conversation and posts to its own page, as that page's form does.
    thread.manage_form = ConversationManagementForm(company=company, thread=thread, auto_id='manage_%s')
    thread.manage_context = make_workflow_context(request, company, 'conversation-management', thread)
    context['conversation_notes'] = patient_notes(company, membership, patient, request.user)
    if request.method == 'GET':
        # Read up to the latest message shown, for this staff member only.
        if mark_read_by_staff(thread, request.user, [message.pk for message in thread.conversation_messages]):
            # The sidebar and tab counts were not worked out yet on this request; make sure they aren't stale.
            request.__dict__.pop('_staff_message_counts', None)
        thread.unread_count = 0
        record_audit(company=company, actor=request.user, patient=patient, action='message.thread_viewed',
                     target=thread, request=request,
                     metadata={'patient_notes': len(context['conversation_notes']['entries'])} if context['conversation_notes'] else None)
    context['message_threads'] = [thread]
    return _attach_appointment_proposals(context, request, company, patient, actor_role='doctor',
                                         allowed=membership.is_clinician)


def render_record_messages(request, company, membership, patient, *, status=400, **overrides):
    """The record's Messages tab again, with a reply or proposal error shown in it."""
    from .patient_workspace import workspace_tab_context

    context = workspace_tab_context(request, company, membership, patient, 'messages', **overrides)
    return render(request, 'portal/patient_workspace.html', context, status=status)
