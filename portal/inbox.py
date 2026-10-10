"""Practice-scoped staff inbox presentation and safe return destinations."""

from django import forms
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db.models import Count, Exists, F, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from care.forms import PatientMessageForm
from care.messaging import staff_threads, unread_for_staff
from care.models import MessageThread, MessageThreadParticipant, PatientMessage
from care.team_messaging import unread_team_messages
from practices.models import CompanyMembership, Patient
from .navigation import messages_title, uses_flat_menu
from .record_access import can_change_record, can_view_record


def message_tab_counts(company, user):
    """Unread totals for the Patients and Team tabs of Messages: what others sent since this member last read."""
    patients = unread_for_staff(company, user).count()
    return {'patient_unread_total': patients, 'team_unread_total': unread_team_messages(company, user)}


def latest_from(thread, user, *, unnamed=()):
    """Who wrote the latest message, before it in a list of conversations: "You", nobody for the one person
    the conversation is with (the patient, or the only other member), otherwise the sender's first name."""
    sender = thread.latest_sender_id
    if sender is None or sender in unnamed:
        return ''
    return 'You' if sender == user.pk else (thread.latest_sender_first_name or '')


def record_thread_url(thread, **query):
    """The patient record's Messages tab with this conversation open, from the top of the page."""
    from urllib.parse import urlencode

    params = urlencode({'tab': 'messages', 'thread': thread.pk, **query})
    return f'{reverse("portal:patient-detail", args=[thread.patient_id])}?{params}'


def staff_inbox_redirect(thread):
    return redirect(record_thread_url(thread))


def _inbox_threads(company, user):
    # Only conversations this staff member takes part in; there is no practice-wide inbox.
    latest = PatientMessage.objects.for_company(company).filter(thread_id=OuterRef('pk')).order_by('-created_at', '-pk')
    last_read = MessageThreadParticipant.objects.filter(thread_id=OuterRef('pk'), user_id=user.pk).values('last_read_at')[:1]
    return staff_threads(company, user).select_related('patient__user', 'patient__assigned_doctor').annotate(
        latest_sender_id=Subquery(latest.values('sender_id')[:1]),
        latest_body=Subquery(latest.values('body')[:1]),
        latest_sender_first_name=Subquery(latest.values('sender__first_name')[:1]),
        has_patient_message=Exists(latest.filter(sender_id=OuterRef('patient__user_id'))),
        # Someone else, the patient or a colleague, has written: there is something to mark unread.
        has_others_message=Exists(latest.exclude(sender_id=user.pk)),
        last_activity=Coalesce('last_message_at', 'created_at'),
        my_last_read=Subquery(last_read),
    ).annotate(
        # Unread for this member only, as for unread_for_staff: what others sent since they last read it.
        unread_count=Count('messages', filter=Q(messages__company=company) & (
            Q(messages__sender__isnull=True) | ~Q(messages__sender_id=user.pk)
        ) & (Q(my_last_read__isnull=True) | Q(messages__created_at__gt=F('my_last_read'))), distinct=True),
    ).order_by('-last_activity', '-pk')


# The Filters menu: Open or Closed, and any of the patients your conversations are with.
STATUS_CHOICES = (('open', 'Open'), ('closed', 'Closed'))
SEARCH_LENGTH = 100


def messageable_patients(company, user, membership):
    """Patients a staff member can start a conversation with: active, able to sign in to read it, and
    for a clinician only their own patients, as in All patients."""
    patients = Patient.objects.for_company(company).filter(is_active=True, user__isnull=False).order_by('last_name', 'first_name', 'pk')
    if uses_flat_menu(membership):
        patients = patients.filter(assigned_doctor=user)
    return patients


class NewPatientThreadForm(PatientMessageForm):
    """New patient message: choose the patient, then write. A blank subject is named for the sender."""

    patient = forms.ModelChoiceField(label='To', queryset=None, empty_label='Choose a patient')
    subject = forms.CharField(label='Subject (optional)', max_length=255, required=False)
    field_order = ('patient', 'subject', 'body')

    def __init__(self, *args, company, user, membership, **kwargs):
        from .record_messages import new_thread_subject

        super().__init__(*args, **kwargs)
        self.fields['patient'].queryset = messageable_patients(company, user, membership)
        self.fields['patient'].label_from_instance = (
            lambda patient: f'{patient} · {patient.medical_record_number}' if patient.medical_record_number else str(patient))
        self.default_subject = new_thread_subject(user, membership)
        self.fields['subject'].widget.attrs['placeholder'] = self.default_subject


def search_text(value):
    """A search as typed, with spacing tidied and a sensible length."""
    return ' '.join((value or '').split())[:SEARCH_LENGTH]


def search_threads(threads, company, search):
    """Conversations where every word appears: in the subject, a message or its sender, the patient's
    name or record number, or the name of a clinician in the conversation."""
    people = get_user_model().objects
    for term in search.split()[:8]:
        named = Q(first_name__icontains=term) | Q(last_name__icontains=term)
        threads = threads.filter(
            Q(subject__icontains=term) | Q(patient__first_name__icontains=term) | Q(patient__last_name__icontains=term)
            | Q(patient__medical_record_number__icontains=term)
            | Exists(PatientMessage.objects.for_company(company).filter(
                Q(body__icontains=term) | Q(sender__first_name__icontains=term) | Q(sender__last_name__icontains=term),
                thread_id=OuterRef('pk')))
            | Exists(people.filter(named, message_threads=OuterRef('pk')))
        )
    return threads


def staff_inbox_url(thread=None, *, status=(), page=1, search='', patients=()):
    """Patient messages with its filters, search and page kept, and a conversation open if one is given."""
    from urllib.parse import urlencode

    from .filter_menus import one_ticked, ticked_ids

    query = {}
    if statuses := one_ticked(status, STATUS_CHOICES):
        query['status'] = statuses[0]
    if patients := ticked_ids(patients):
        query['patients'] = patients
    if search:
        query['q'] = search
    if str(page) != '1':
        query['page'] = page
    if thread is not None:
        query['thread'] = thread.pk
    return f'{reverse("portal:staff-inbox")}{"?" + urlencode(query, doseq=True) if query else ""}'


def staff_inbox_context(request, company, membership, *, selected_id=None, **overrides):
    """Patient messages: the conversations on the left and the one being read on the right, as on the record."""
    from .patient_workspace import workspace_url
    from .people_tags import people_tags, staff_kinds
    from .record_messages import open_conversation, read_state_url

    from .filter_menus import choice_section, filter_menu, one_ticked, ticked_ids

    all_threads = _inbox_threads(company, request.user).prefetch_related('participants')
    # Open or Closed, and which patients; a reply or suggestion that wasn't sent comes back to the same filters.
    status = one_ticked(request.POST.getlist('status') or request.GET.getlist('status'), STATUS_CHOICES)
    patients = ticked_ids(request.POST.getlist('patients') or request.GET.getlist('patients'))
    filtered = all_threads
    if status:
        filtered = filtered.filter(is_closed=status[0] == 'closed')
    if patients:
        filtered = filtered.filter(patient_id__in=patients)
    search = search_text(request.POST.get('q') or request.GET.get('q'))
    if search:
        filtered = search_threads(filtered, company, search)
    paginator = Paginator(filtered, 20)
    page = paginator.get_page(request.GET.get('page'))
    threads = list(page.object_list)
    # Only a conversation someone chose opens; one outside this page or filter still opens.
    raw = selected_id or overrides.get('reply_thread_id') or request.GET.get('thread')
    selected = None
    if raw:
        wanted = inbox_thread(company, request.user, raw).pk
        selected = next((thread for thread in threads if thread.pk == wanted), None) or all_threads.get(pk=wanted)
    shown = {*threads, *([selected] if selected else [])}
    kinds = staff_kinds(company, [person.pk for thread in shown for person in thread.participants.all()])
    for thread in shown:
        thread.record_url = record_thread_url(thread)
        thread.list_url = staff_inbox_url(thread, status=status, page=page.number, search=search, patients=patients)
        thread.is_selected = selected is not None and thread.pk == selected.pk
        # Across patients, the patient is first, then anyone else in it besides you, each as a tag.
        others = [person for person in thread.participants.all() if person.pk not in (request.user.pk, thread.patient.user_id)]
        thread.people_tags = people_tags(others, kinds, patient=thread.patient)
        thread.latest_from = latest_from(thread, request.user, unnamed={thread.patient.user_id})
        thread.read_state_url = read_state_url(thread)
    kept = {'status': ','.join(status), 'patients': ','.join(map(str, patients))}
    read_state_fields = {'return_to': 'patient_messages', **kept, 'q': search, 'page': page.number}
    # Only the patients your conversations are with are offered, by name.
    people = Patient.objects.filter(pk__in=all_threads.order_by().values('patient_id')).order_by('first_name', 'last_name', 'pk')
    composing = selected is None and (request.GET.get('compose') == '1' or overrides.get('failed_form') == 'new_thread_form')
    preselected = request.GET.get('patient', '')
    context = {
        'company': company,
        'active_membership': membership,
        'is_staff_inbox': True,
        'is_doctor': membership.role == CompanyMembership.Role.DOCTOR,
        'threads': threads,
        'paginator': paginator,
        'page_obj': page,
        'is_paginated': page.has_other_pages(),
        'search': search, 'search_hidden': kept,
        'filter_menu': filter_menu(
            action=reverse('portal:staff-inbox'), hidden=[('q', search)], clear_url=staff_inbox_url(search=search),
            label='Filter patient messages', sections=[
                choice_section('status', 'Status', STATUS_CHOICES, status),
                choice_section('patients', 'Patient', [(person.pk, str(person)) for person in people], patients, icon='person'),
            ]),
        'inbox_search_clear_url': staff_inbox_url(status=status, patients=patients),
        'inbox_previous_url': staff_inbox_url(status=status, page=page.previous_page_number(), search=search, patients=patients) if page.has_previous() else '',
        'inbox_next_url': staff_inbox_url(status=status, page=page.next_page_number(), search=search, patients=patients) if page.has_next() else '',
        'messages_title': messages_title(membership),
        'inbox_thread': selected,
        'inbox_read_state_fields': read_state_fields,
        'inbox_composing': composing,
        'new_thread_form': overrides.get('new_thread_form') or NewPatientThreadForm(
            company=company, user=request.user, membership=membership, auto_id='new_thread_%s',
            initial={'patient': preselected} if preselected.isdecimal() else None),
        'message_threads': [],
        'failed_form': overrides.get('failed_form', ''), 'reply_thread_id': overrides.get('reply_thread_id'),
    }
    for key in ('proposal_form', 'proposal_error', 'failed_proposal_id'):
        if key in overrides:
            context[key] = overrides[key]
    if selected is not None:
        context.update(
            inbox_patient=selected.patient,
            # A clinician in the conversation may not be one who can open, or change, this patient's record.
            inbox_record_url=workspace_url(selected.patient) if can_view_record(membership, request.user, selected.patient) else '',
            inbox_new_url=workspace_url(selected.patient, 'messages', new=1)
            if can_change_record(membership, request.user, selected.patient) else '',
            inbox_message_form=overrides.get('message_form') or PatientMessageForm(auto_id='record_message_%s'),
        )
        context = open_conversation(request, company, membership, selected, context)
    # Worked out after the open conversation is marked read, so the tab counts include it.
    context.update(message_tab_counts(company, request.user))
    return context


def render_staff_inbox(request, company, membership, *, status=400, **overrides):
    """Patient messages again, with a reply or suggestion error shown in the open conversation."""
    return render(request, 'portal/staff_inbox.html', staff_inbox_context(request, company, membership, **overrides), status=status)


def inbox_thread(company, user, raw_id):
    """A conversation from a ?thread= link, only if this staff member takes part in it."""
    raw_id = str(raw_id)
    if not raw_id.isascii() or not raw_id.isdecimal() or len(raw_id) > 19 or not 0 < int(raw_id) <= 9223372036854775807:
        raise Http404('Conversation not found.')
    return get_object_or_404(staff_threads(company, user), pk=int(raw_id))
