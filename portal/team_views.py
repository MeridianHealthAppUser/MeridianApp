"""The Team tab of Messages: staff-to-staff conversations, optionally about a patient."""

from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.contrib.auth import get_user_model
from django.db.models import Exists, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.models import TeamMessage
from care.services import record_audit
from care.team_messaging import (
    colleagues, manage_team_conversation, mark_team_thread_read, post_team_message, set_team_read_state,
    start_team_conversation, team_threads,
)
from practices.models import CompanyMembership, Patient
from .filter_menus import choice_section, filter_menu, one_ticked
from .inbox import latest_from, message_tab_counts, search_text
from .people_tags import in_join_order, people_tags, person_tag, staff_kinds
from .navigation import messages_title
from .record_messages import patient_notes
from .views import StaffCompanyRequiredMixin
from .workflow_context import make_workflow_context, validate_workflow_context


def _label_with_role(company):
    titles = {membership.user_id: membership.title for membership in CompanyMembership.objects.filter(company=company, is_active=True)}
    return lambda user: f'{user.full_name} · {titles[user.pk]}' if user.pk in titles else user.full_name


class TeamThreadForm(forms.Form):
    members = forms.ModelMultipleChoiceField(label='To', queryset=None, widget=forms.CheckboxSelectMultiple)
    patient = forms.ModelChoiceField(label='About a patient (optional)', queryset=None, required=False, empty_label='No patient',
                                     help_text='The patient never sees team conversations.')
    subject = forms.CharField(label='Subject', max_length=255)
    body = forms.CharField(label='Message', max_length=5000, widget=forms.Textarea(attrs={'rows': 4}))

    def __init__(self, *args, company, actor, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['members'].queryset = colleagues(company).exclude(pk=actor.pk)
        self.fields['members'].label_from_instance = _label_with_role(company)
        self.fields['patient'].queryset = Patient.objects.for_company(company).filter(is_active=True).order_by('last_name', 'first_name')


class TeamMessageForm(forms.Form):
    body = forms.CharField(label='Reply', max_length=5000, widget=forms.Textarea(attrs={'rows': 3}))


class TeamConversationForm(forms.Form):
    """Message action on a team conversation, as on a patient conversation: add a colleague, close or reopen."""

    action = forms.ChoiceField(choices=(('add', 'Add a colleague to this conversation'), ('close', 'Close conversation'),
                                        ('reopen', 'Reopen conversation')))
    user = forms.ModelChoiceField(label='Colleague to add', queryset=None, required=False,
                                  help_text='They can read this whole conversation and reply.')
    confirm = forms.BooleanField(label='Confirm this action. Messages are kept.')

    def __init__(self, *args, company, thread, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['user'].queryset = colleagues(company).exclude(pk__in=thread.member_links.values('user_id'))
        self.fields['user'].label_from_instance = _label_with_role(company)

    def clean(self):
        data = super().clean()
        if data.get('action') == 'add' and not data.get('user'):
            self.add_error('user', 'Choose a colleague to add.')
        return data


# The Filters menu: Open or Closed, and With patient (about a patient) or No patient (a general conversation).
STATUS_CHOICES = (('open', 'Open'), ('closed', 'Closed'))
ABOUT_CHOICES = (('patient', 'With patient'), ('general', 'No patient'))


def team_url(thread=None, *, status=(), page=1, search='', about=()):
    """Team messages on a page of its list, with its filters and search kept and a conversation open if one is given."""
    query = {}
    if statuses := one_ticked(status, STATUS_CHOICES):
        query['status'] = statuses[0]
    if abouts := one_ticked(about, ABOUT_CHOICES):
        query['about'] = abouts[0]
    if search:
        query['q'] = search
    if str(page) != '1':
        query['page'] = page
    if thread is not None:
        query['thread'] = thread.pk
    return f'{reverse("portal:team-inbox")}{"?" + urlencode(query) if query else ""}'


def search_team_threads(threads, search):
    """Team conversations where every word appears: in the subject, a message or its sender, a
    colleague in the conversation, or the name or record number of the patient it is about."""
    people = get_user_model().objects
    for term in search.split()[:8]:
        named = Q(first_name__icontains=term) | Q(last_name__icontains=term)
        threads = threads.filter(
            Q(subject__icontains=term) | Q(patient__first_name__icontains=term) | Q(patient__last_name__icontains=term)
            | Q(patient__medical_record_number__icontains=term)
            | Exists(TeamMessage.objects.filter(
                Q(body__icontains=term) | Q(sender__first_name__icontains=term) | Q(sender__last_name__icontains=term),
                thread_id=OuterRef('pk')))
            | Exists(people.filter(named, team_threads=OuterRef('pk')))
        )
    return threads


def team_inbox_context(request, company, membership, *, selected_id=None, **overrides):
    """Team messages: the conversations on the left and the one being read on the right, as on Patient messages."""
    latest = TeamMessage.objects.filter(thread=OuterRef('pk')).order_by('-created_at', '-pk')
    threads = team_threads(company, request.user).select_related('patient').prefetch_related(in_join_order('members')).annotate(
        latest_body=Subquery(latest.values('body')[:1]),
        latest_sender_id=Subquery(latest.values('sender_id')[:1]),
        latest_sender_first_name=Subquery(latest.values('sender__first_name')[:1]),
        has_others_message=Exists(latest.exclude(sender=request.user)),
        last_activity=Coalesce('last_message_at', 'created_at'),
    ).order_by('-last_message_at', '-pk')
    # Open or Closed, With patient or No patient, then a search, narrow the list; the open conversation stays open
    # even when it doesn't match.
    status = one_ticked(request.POST.getlist('status') or request.GET.getlist('status'), STATUS_CHOICES)
    about = one_ticked(request.POST.getlist('about') or request.GET.getlist('about'), ABOUT_CHOICES)
    filtered = threads
    if status:
        filtered = filtered.filter(is_closed=status[0] == 'closed')
    if about:
        filtered = filtered.filter(patient__isnull=about[0] == 'general')
    search = search_text(request.POST.get('q') or request.GET.get('q'))
    page = Paginator(search_team_threads(filtered, search) if search else filtered, 20).get_page(request.GET.get('page'))
    raw = selected_id if selected_id is not None else request.GET.get('thread')
    selected = None
    if raw is not None:
        if not str(raw).isdecimal() or len(str(raw)) > 19:
            raise Http404
        selected = get_object_or_404(threads, pk=raw)
    listed = list(page.object_list)
    kinds = staff_kinds(company, [member.pk for thread in listed for member in thread.members.all()])
    for thread in listed:
        thread.list_url = team_url(thread, status=status, page=page.number, search=search, about=about)
        thread.is_selected = selected is not None and thread.pk == selected.pk
        others = [member for member in thread.members.all() if member.pk != request.user.pk]
        thread.people_tags = people_tags(others, kinds)
        # A colleague is named before their message only when more than one could have written it.
        thread.latest_from = latest_from(thread, request.user, unnamed={others[0].pk} if len(others) == 1 else ())
        thread.about = person_tag(str(thread.patient), 'patient') if thread.patient_id else None
        thread.read_state_url = reverse('portal:team-thread-read-state', args=[thread.pk]) if thread.has_others_message else ''
    preselected_patient = request.GET.get('patient', '')
    composing = request.GET.get('compose') == '1' or overrides.get('failed_form') == 'thread_form' or (selected is None and not threads.exists())
    context = dict(
        company=company, active_membership=membership, nav_section='messages', page_obj=page, is_paginated=page.has_other_pages(),
        threads=listed, selected_thread=selected, composing=composing and selected is None,
        team_read_state_fields={'status': ','.join(status), 'about': ','.join(about), 'page': page.number, 'q': search},
        filter_status=','.join(status), filter_about=','.join(about), search=search,
        search_hidden={'status': ','.join(status), 'about': ','.join(about)},
        filter_menu=filter_menu(
            action=reverse('portal:team-inbox'), hidden=[('q', search)], clear_url=team_url(search=search),
            label='Filter team messages', sections=[
                choice_section('status', 'Status', STATUS_CHOICES, status),
                choice_section('about', 'Patient', ABOUT_CHOICES, about, icon='person'),
            ]),
        team_search_clear_url=team_url(status=status, about=about),
        team_previous_url=team_url(status=status, page=page.previous_page_number(), search=search, about=about) if page.has_previous() else '',
        team_next_url=team_url(status=status, page=page.next_page_number(), search=search, about=about) if page.has_next() else '',
        thread_form=TeamThreadForm(company=company, actor=request.user,
                                   initial={'patient': preselected_patient} if preselected_patient.isdecimal() else None),
        message_form=TeamMessageForm(auto_id='team_%s'),
        **message_tab_counts(company, request.user), messages_title=messages_title(membership),
    )
    if selected is not None:
        selected.conversation_messages = list(TeamMessage.objects.for_company(company).filter(thread=selected).select_related('sender'))
        selected.member_rows = list(selected.member_links.select_related('user', 'added_by'))
        selected.others = [row.user for row in selected.member_rows if row.user_id != request.user.pk]
        selected.people_tags = people_tags(selected.others, staff_kinds(company, [person.pk for person in selected.others]))
        selected.about = person_tag(str(selected.patient), 'patient') if selected.patient_id else None
        # "You added Lindiwe Mahlangu 8 Oct", under the tags.
        selected.added_rows = [row for row in selected.member_rows if row.added_by_id]
        # Message action opens in a modal over the conversation and posts to its own page, as that page's form does.
        selected.manage_form = TeamConversationForm(company=company, thread=selected, auto_id='team_manage_%s')
        selected.manage_context = make_workflow_context(request, company, 'team-conversation-management', selected)
        # About a patient: their notes under the reply box, as in a patient conversation. Clinical staff only, so a
        # practice admin in the same conversation never sees them.
        context['conversation_notes'] = patient_notes(company, membership, selected.patient, request.user) if selected.patient_id else None
        if request.method == 'GET':
            if selected.patient_id:
                notes = context['conversation_notes']
                record_audit(company=company, actor=request.user, patient=selected.patient, action='team_thread.viewed',
                             target=selected, request=request,
                             metadata={'patient_notes': len(notes['entries'])} if notes else None)
            mark_team_thread_read(selected, request.user)
            for thread in context['threads']:
                if thread.pk == selected.pk:
                    thread.unread_count = 0
            context.update(message_tab_counts(company, request.user))
    context.update(overrides)
    return context


def team_redirect(thread, search='', status='', about=''):
    return redirect(team_url(thread, status=status, search=search, about=about))


@method_decorator(never_cache, name='dispatch')
class TeamInboxView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        return render(request, 'portal/team_inbox.html', team_inbox_context(request, self.company, self.membership))


class TeamThreadCreateView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('post',)

    def post(self, request):
        form = TeamThreadForm(request.POST, company=self.company, actor=request.user)
        if form.is_valid():
            try:
                thread = start_team_conversation(company=self.company, actor=request.user, members=list(form.cleaned_data['members']),
                                                 subject=form.cleaned_data['subject'], body=form.cleaned_data['body'],
                                                 patient=form.cleaned_data['patient'], request=request)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, 'Team conversation started. Only the people in it can read it.')
                return team_redirect(thread)
        return render(request, 'portal/team_inbox.html', team_inbox_context(
            request, self.company, self.membership, thread_form=form, failed_form='thread_form', composing=True), status=400)


class TeamThreadActionView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    http_method_names = ('post',)

    def thread(self, pk):
        return get_object_or_404(team_threads(self.company, self.request.user), pk=pk)

    def invalid(self, request, thread, **overrides):
        return render(request, 'portal/team_inbox.html', team_inbox_context(
            request, self.company, self.membership, selected_id=thread.pk, **overrides), status=400)


class TeamMessageCreateView(TeamThreadActionView):
    def post(self, request, pk):
        thread = self.thread(pk)
        form = TeamMessageForm(request.POST, auto_id='team_%s')
        if form.is_valid():
            try:
                post_team_message(thread=thread, sender=request.user, body=form.cleaned_data['body'], request=request)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                return team_redirect(thread, search_text(request.POST.get('q')), request.POST.get('status', ''), request.POST.get('about', ''))
        return self.invalid(request, thread, message_form=form)


@method_decorator(never_cache, name='dispatch')
class TeamConversationManageView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    """Message action for a team conversation: add a colleague, close or reopen, then back to the conversation."""

    http_method_names = ('get', 'post', 'head', 'options')

    def thread(self, pk):
        return get_object_or_404(team_threads(self.company, self.request.user).select_related('patient'), pk=pk)

    def display(self, request, thread, form=None, status=200):
        form = form if form is not None else TeamConversationForm(company=self.company, thread=thread)
        return render(request, 'portal/team_message_management.html', dict(
            company=self.company, active_membership=self.membership, nav_section='messages',
            messages_title=messages_title(self.membership), thread=thread, form=form, back_url=team_url(thread),
            members=[link.user for link in thread.member_links.select_related('user')],
            workflow_context=request.POST.get('workflow_context', '') if request.method == 'POST'
            else make_workflow_context(request, self.company, 'team-conversation-management', thread)), status=status)

    def get(self, request, pk):
        return self.display(request, self.thread(pk))

    def post(self, request, pk):
        thread = self.thread(pk)
        form = TeamConversationForm(request.POST, company=self.company, thread=thread)
        valid = form.is_valid()
        try:
            validate_workflow_context(request, self.company, 'team-conversation-management', thread)
            if valid:
                colleague = form.cleaned_data['user']
                manage_team_conversation(thread=thread, actor=request.user, action=form.cleaned_data['action'], user=colleague,
                                         request=request)
                if form.cleaned_data['action'] == 'add':
                    messages.success(request, f'{colleague.full_name} can now read and reply in this conversation.')
                else:
                    messages.success(request, 'Conversation status updated. Existing messages are retained.')
                # Done: back to the conversation in Team messages.
                return redirect(team_url(thread))
        except ValidationError as error:
            for message in error.messages:
                form.add_error(None, message)
        return self.display(request, thread, form, 400)


class TeamThreadReadStateView(TeamThreadActionView):
    """Mark a team conversation read, or unread from the latest message someone else sent."""

    def post(self, request, pk):
        thread = self.thread(pk)
        state = request.POST.get('state')
        if state not in ('read', 'unread'):
            return HttpResponseBadRequest('Choose read or unread.')
        set_team_read_state(thread, request.user, read=state == 'read')
        messages.success(request, 'Conversation marked as read.' if state == 'read' else 'Conversation marked as unread.')
        # Back to the list on the same filter, page and search, with no conversation open so it stays unread.
        page = request.POST.get('page', '')
        return redirect(team_url(status=request.POST.get('status', ''), page=page if page.isdecimal() and len(page) < 10 else 1,
                                 search=search_text(request.POST.get('q')), about=request.POST.get('about', '')))
