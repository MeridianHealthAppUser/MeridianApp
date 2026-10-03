"""The Team tab of Messages: staff-to-staff conversations, optionally about a patient."""

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.models import TeamMessage
from care.team_messaging import (
    add_team_member, colleagues, mark_team_thread_read, post_team_message, start_team_conversation, team_threads,
)
from practices.models import CompanyMembership, Patient
from .inbox import message_tab_counts
from .views import StaffCompanyRequiredMixin


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


class TeamMemberForm(forms.Form):
    user = forms.ModelChoiceField(label='Add a colleague', queryset=None)

    def __init__(self, *args, company, thread, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['user'].queryset = colleagues(company).exclude(pk__in=thread.member_links.values('user_id'))
        self.fields['user'].label_from_instance = _label_with_role(company)


def team_inbox_context(request, company, membership, *, selected_id=None, **overrides):
    threads = team_threads(company, request.user).select_related('patient').prefetch_related('members').order_by('-last_message_at', '-pk')
    page = Paginator(threads, 20).get_page(request.GET.get('page'))
    raw = selected_id if selected_id is not None else request.GET.get('thread')
    selected = None
    if raw is not None:
        if not str(raw).isdecimal() or len(str(raw)) > 19:
            raise Http404
        selected = get_object_or_404(threads, pk=raw)
    preselected_patient = request.GET.get('patient', '')
    composing = request.GET.get('compose') == '1' or overrides.get('failed_form') == 'thread_form' or (selected is None and not threads.exists())
    context = dict(
        company=company, active_membership=membership, nav_section='messages', page_obj=page, is_paginated=page.has_other_pages(),
        threads=list(page.object_list), selected_thread=selected, composing=composing and selected is None,
        thread_form=TeamThreadForm(company=company, actor=request.user,
                                   initial={'patient': preselected_patient} if preselected_patient.isdecimal() else None),
        message_form=TeamMessageForm(auto_id='team_%s'),
        **message_tab_counts(company, request.user),
    )
    if selected is not None:
        selected.conversation_messages = list(TeamMessage.objects.for_company(company).filter(thread=selected).select_related('sender'))
        selected.member_rows = list(selected.member_links.select_related('user', 'added_by'))
        context['member_form'] = TeamMemberForm(company=company, thread=selected)
        if request.method == 'GET':
            mark_team_thread_read(selected, request.user)
            for thread in context['threads']:
                if thread.pk == selected.pk:
                    thread.unread_count = 0
            context.update(message_tab_counts(company, request.user))
    context.update(overrides)
    return context


def team_redirect(thread):
    return redirect(f'{reverse("portal:team-inbox")}?thread={thread.pk}#team-conversation')


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
                return team_redirect(thread)
        return self.invalid(request, thread, message_form=form)


class TeamMemberAddView(TeamThreadActionView):
    def post(self, request, pk):
        thread = self.thread(pk)
        form = TeamMemberForm(request.POST, company=self.company, thread=thread)
        if form.is_valid():
            try:
                add_team_member(thread=thread, actor=request.user, user=form.cleaned_data['user'], request=request)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, f"{form.cleaned_data['user'].full_name} can now read and reply in this conversation.")
                return team_redirect(thread)
        return self.invalid(request, thread, member_form=form)
