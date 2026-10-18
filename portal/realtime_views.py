"""Authorized unread totals and acknowledgements of messages displayed in the browser.

Valkey events only prompt a refresh; the read positions and totals always come
from PostgreSQL. A snapshot never acknowledges a message on the user's behalf.
"""

import json

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.messaging import is_participant, mark_patient_messages_read, mark_read_by_staff, staff_threads
from care.models import MessageThread
from care.team_messaging import mark_team_thread_read, team_threads
from .inbox import message_tab_counts
from .patient_views import unread_patient_messages
from .views import PatientPortalRequiredMixin, StaffCompanyRequiredMixin


def displayed_message_ids(request):
    """Accept a bounded batch of IDs, never a client-supplied timestamp or user."""
    try:
        if len(request.body) > 16000:
            raise ValueError
        data = json.loads(request.body)
        values = data.get('message_ids') if isinstance(data, dict) else None
        if not isinstance(values, list) or len(values) > 500:
            raise ValueError
        ids = set()
        for value in values:
            if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
                value = int(value)
            if type(value) is not int or not 0 < value <= 9223372036854775807:
                raise ValueError
            ids.add(value)
        return list(ids)
    except (ValueError, UnicodeError, RecursionError):
        raise ValidationError('Send message_ids as a list of at most 500 positive message IDs.')


class StaffMessageCountsMixin:
    def counts(self):
        counts = message_tab_counts(self.company, self.request.user)
        patients, team = counts['patient_unread_total'], counts['team_unread_total']
        return {'scope': self.request.realtime_scope, 'patient': patients, 'team': team, 'total': patients + team}


class PatientMessageCountsMixin:
    def counts(self):
        count = unread_patient_messages(self.request, self.patient_company, self.patient)
        return {'scope': self.request.realtime_scope, 'patient': count, 'team': 0, 'total': count}


@method_decorator(never_cache, name='dispatch')
class StaffMessageStatusView(LoginRequiredMixin, StaffCompanyRequiredMixin, StaffMessageCountsMixin, View):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        return JsonResponse(self.counts())


@method_decorator(never_cache, name='dispatch')
class PatientMessageStatusView(LoginRequiredMixin, PatientPortalRequiredMixin, PatientMessageCountsMixin, View):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        return JsonResponse(self.counts())


class MessageSeenView(View):
    http_method_names = ('post',)

    def post(self, request, pk):
        thread = self.thread(pk)
        try:
            ids = displayed_message_ids(request)
        except ValidationError as error:
            return JsonResponse({'error': error.messages[0]}, status=400)
        changed = self.mark_seen(thread, ids)
        return JsonResponse({**self.counts(), 'changed': changed})


@method_decorator(never_cache, name='dispatch')
class StaffMessageSeenView(LoginRequiredMixin, StaffCompanyRequiredMixin, StaffMessageCountsMixin, MessageSeenView):
    def thread(self, pk):
        return get_object_or_404(staff_threads(self.company, self.request.user).select_related('patient'), pk=pk)

    def mark_seen(self, thread, ids):
        # Super Admins may inspect a conversation without joining or giving a receipt.
        if not is_participant(thread, self.request.user):
            return 0
        return mark_read_by_staff(thread, self.request.user, ids)


@method_decorator(never_cache, name='dispatch')
class PatientMessageSeenView(LoginRequiredMixin, PatientPortalRequiredMixin, PatientMessageCountsMixin, MessageSeenView):
    def thread(self, pk):
        return get_object_or_404(MessageThread.objects.for_company(self.patient_company), patient=self.patient, pk=pk)

    def mark_seen(self, thread, ids):
        return mark_patient_messages_read(thread, self.request.user, ids)


@method_decorator(never_cache, name='dispatch')
class TeamMessageSeenView(LoginRequiredMixin, StaffCompanyRequiredMixin, StaffMessageCountsMixin, MessageSeenView):
    def thread(self, pk):
        return get_object_or_404(team_threads(self.company, self.request.user), pk=pk)

    def mark_seen(self, thread, ids):
        return mark_team_thread_read(thread, self.request.user, ids)
