"""The staff inbox lists only the selected practice's conversations that the staff member takes part in, or for a
Super Admin every one in the practice."""

from django.test import override_settings
from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from care.messaging import add_participant
from care.models import Appointment, AuditEvent, MessageThread, MessageThreadParticipant, PatientMessage
from care.widgets import floor_quarter_hour
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY


class InboxPageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = set()
        self.conversations = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and attrs.get('href'):
            self.links.add(attrs['href'])
        if tag == 'details' and attrs.get('id', '').startswith('conversation-'):
            self.conversations[attrs['id']] = 'open' in attrs


@override_settings(MULTI_PRACTICE_ENABLED=True)
class StaffInboxTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.company = Company.objects.create(name='Alpha Inbox Practice', slug='alpha-inbox')
        cls.other_company = Company.objects.create(name='Beta Inbox Practice', slug='beta-inbox')
        cls.doctor = users.create_user(email='inbox-doctor@example.test', first_name='Doctor')
        cls.administrator = users.create_user(email='inbox-admin@example.test', first_name='Administrator')
        cls.super_admin = users.create_user(email='inbox-super@example.test', first_name='Super administrator')
        cls.patient_user = users.create_user(email='inbox-patient@example.test', first_name='Alice')
        cls.second_patient_user = users.create_user(email='inbox-second-patient@example.test', first_name='Beth')
        for user, role in (
            (cls.doctor, CompanyMembership.Role.DOCTOR),
            (cls.administrator, CompanyMembership.Role.PRACTICE_ADMIN),
            (cls.super_admin, CompanyMembership.Role.SUPER_ADMIN),
        ):
            CompanyMembership.objects.create(user=user, company=cls.company, role=role)
        CompanyMembership.objects.create(user=cls.doctor, company=cls.other_company, role=CompanyMembership.Role.DOCTOR)
        cls.patient = Patient.objects.create(
            company=cls.company, user=cls.patient_user, first_name='Alice', last_name='Patient',
            assigned_doctor=cls.doctor,
        )
        cls.other_practice_patient = Patient.objects.create(
            company=cls.other_company, user=cls.patient_user, first_name='Alice', last_name='Patient',
        )
        cls.second_patient = Patient.objects.create(
            company=cls.company, user=cls.second_patient_user, first_name='Beth', last_name='Patient',
        )
        cls.inactive_patient = Patient.objects.create(
            company=cls.company, first_name='Archived', last_name='Patient', is_active=False,
        )
        now = timezone.now()
        cls.recent_thread = MessageThread.objects.create(
            company=cls.company, patient=cls.patient, subject='Alpha recent discussion',
            opened_by=cls.patient_user, last_message_at=now,
        )
        cls.older_thread = MessageThread.objects.create(
            company=cls.company, patient=cls.patient, subject='Alpha earlier discussion',
            opened_by=cls.patient_user, last_message_at=now - timedelta(days=1),
        )
        cls.other_practice_thread = MessageThread.objects.create(
            company=cls.other_company, patient=cls.other_practice_patient, subject='Beta private discussion',
            opened_by=cls.patient_user, last_message_at=now,
        )
        cls.inactive_patient_thread = MessageThread.objects.create(
            company=cls.company, patient=cls.inactive_patient, subject='Archived patient discussion',
            last_message_at=now,
        )
        for thread in (cls.recent_thread, cls.older_thread, cls.other_practice_thread, cls.inactive_patient_thread):
            add_participant(thread, cls.doctor)

    def login(self, user=None):
        self.client.force_login(user or self.doctor)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def inbox_url(self):
        return reverse('portal:staff-inbox')

    def thread_url(self, thread):
        # A conversation opens beside the list in Patient messages, from the top of the page.
        return f'{self.inbox_url()}?thread={thread.pk}'

    def record_thread_url(self, thread):
        # Replies sent from the record's Messages tab go back there.
        record = reverse('portal:patient-detail', args=[thread.patient_id])
        return f'{record}?tab=messages&thread={thread.pk}'

    def make_appointment(self):
        return Appointment.objects.create(
            company=self.company, patient=self.patient, clinician=self.doctor,
            starts_at=floor_quarter_hour(timezone.now() + timedelta(days=7)), duration_minutes=30,
        )

    def parse(self, response):
        parser = InboxPageParser()
        parser.feed(response.content.decode())
        return parser

    def add_message(self, thread=None, sender=None, **kwargs):
        thread = thread or self.recent_thread
        return PatientMessage.objects.create(
            company=thread.company, thread=thread, sender=sender or self.patient_user,
            body='Secure inbox test message', **kwargs,
        )

    def test_inbox_requires_login(self):
        url = self.inbox_url()
        self.assertRedirects(
            self.client.get(url), f'{reverse("accounts:login")}?next={url}', fetch_redirect_response=False,
        )

    def test_patient_only_account_cannot_enter_staff_inbox(self):
        self.login(self.patient_user)
        self.assertEqual(self.client.get(self.inbox_url()).status_code, 403)

    def test_staff_see_only_conversations_they_take_part_in_and_super_admins_see_every_one(self):
        self.login(self.doctor)
        response = self.client.get(self.inbox_url())
        self.assertEqual({thread.pk for thread in response.context['threads']}, {self.recent_thread.pk, self.older_thread.pk})
        # Practice administrators have no practice-wide view of patient conversations.
        self.login(self.administrator)
        response = self.client.get(self.inbox_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['threads']), [])
        self.assertNotContains(response, self.recent_thread.subject)
        self.assertEqual(self.client.get(self.inbox_url(), {'thread': self.recent_thread.pk}).status_code, 404)
        self.assertEqual(self.client.post(reverse('portal:staff-message-create', args=[self.recent_thread.pk]),
                                          {'body': 'Not my conversation', 'return_to': 'inbox'}).status_code, 404)
        self.assertFalse(PatientMessage.objects.filter(body='Not my conversation').exists())
        # A Super Admin lists every conversation with this practice's active patients, without being in them.
        self.login(self.super_admin)
        response = self.client.get(self.inbox_url())
        self.assertEqual({thread.pk for thread in response.context['threads']}, {self.recent_thread.pk, self.older_thread.pk})
        self.assertContains(response, self.recent_thread.subject)
        self.assertNotContains(response, self.other_practice_thread.subject)
        self.assertNotContains(response, self.inactive_patient_thread.subject)
        for selection in (self.other_practice_thread.pk, self.inactive_patient_thread.pk):
            self.assertEqual(self.client.get(self.inbox_url(), {'thread': selection}).status_code, 404)

    def test_a_super_admin_reads_a_conversation_they_are_not_in_without_marking_it_and_joins_by_replying(self):
        message = self.add_message()
        doctor_position = MessageThreadParticipant.objects.get(thread=self.recent_thread, user=self.doctor).last_read_at
        self.login(self.super_admin)
        # Conversations they aren't in are never unread for them.
        listed = self.client.get(self.inbox_url())
        self.assertEqual({thread.pk: thread.unread_count for thread in listed.context['threads']},
                         {self.recent_thread.pk: 0, self.older_thread.pk: 0})
        self.assertEqual(listed.context['patient_unread_total'], 0)
        self.assertNotContains(listed, 'is-unread')
        # Opened in Patient messages or on the record, it reads like any other, with no Mark as unread or Message action.
        manage = reverse('portal:conversation-manage', args=[self.recent_thread.pk])
        read_state = reverse('portal:staff-thread-read-state', args=[self.recent_thread.pk])
        record = reverse('portal:patient-detail', args=[self.patient.pk])
        for opened, thread in ((self.client.get(self.inbox_url(), {'thread': self.recent_thread.pk}), 'inbox_thread'),
                               (self.client.get(record, {'tab': 'messages', 'thread': self.recent_thread.pk}), 'record_thread')):
            with self.subTest(thread=thread):
                self.assertEqual(opened.status_code, 200)
                self.assertEqual(opened.context[thread].pk, self.recent_thread.pk)
                self.assertFalse(opened.context[thread].is_member)
                self.assertContains(opened, 'Secure inbox test message')
                self.assertContains(opened, 'You’re not in this conversation. Replying adds you to it.')
                self.assertContains(opened, 'action="' + reverse('portal:staff-message-create', args=[self.recent_thread.pk]) + '"')
                self.assertNotContains(opened, f'action="{read_state}"')
                self.assertNotContains(opened, f'href="{manage}')
                self.assertNotContains(opened, 'id="conversation-manage"')
        # Nothing was marked read: no read position for them, no receipt on the patient's message, nobody else's changed.
        self.assertFalse(MessageThreadParticipant.objects.filter(thread=self.recent_thread, user=self.super_admin).exists())
        message.refresh_from_db()
        self.assertIsNone(message.staff_read_at)
        self.assertIsNone(message.read_at)
        self.assertEqual(MessageThreadParticipant.objects.get(thread=self.recent_thread, user=self.doctor).last_read_at, doctor_position)
        # Replying adds them, which is recorded; from then on it is theirs like any other conversation they're in.
        reply = self.client.post(reverse('portal:staff-message-create', args=[self.recent_thread.pk]),
                                 {'body': 'Joining to help.', 'return_to': 'patient_messages'})
        self.assertRedirects(reply, self.thread_url(self.recent_thread), fetch_redirect_response=False)
        self.assertTrue(self.recent_thread.messages.filter(sender=self.super_admin, body='Joining to help.').exists())
        self.assertEqual(set(self.recent_thread.participants.all()), {self.doctor, self.super_admin})
        joined = AuditEvent.objects.get(action='message.participant_added', actor=self.super_admin)
        self.assertEqual(joined.metadata, {'user_id': self.super_admin.pk, 'joined_by_reply': True})
        opened = self.client.get(self.inbox_url(), {'thread': self.recent_thread.pk})
        self.assertTrue(opened.context['inbox_thread'].is_member)
        self.assertNotContains(opened, 'You’re not in this conversation.')
        self.assertContains(opened, f'action="{read_state}"')
        self.assertContains(opened, f'href="{manage}"')
        self.add_message()
        counts = {thread.pk: thread.unread_count for thread in self.client.get(self.inbox_url()).context['threads']}
        self.assertEqual(counts, {self.recent_thread.pk: 1, self.older_thread.pk: 0})

    def test_revoked_membership_does_not_keep_session_access(self):
        self.login(self.administrator)
        CompanyMembership.objects.filter(user=self.administrator, company=self.company).update(is_active=False)
        self.assertEqual(self.client.get(self.inbox_url()).status_code, 403)

    def test_inbox_is_limited_to_active_practice_and_changes_after_switch(self):
        self.login()
        response = self.client.get(self.inbox_url())
        self.assertEqual({thread.company_id for thread in response.context['threads']}, {self.company.pk})
        self.assertContains(response, self.recent_thread.subject)
        self.assertNotContains(response, self.other_practice_thread.subject)
        response = self.client.post(
            reverse('portal:activate-company', args=[self.other_company.slug]), {'next': self.inbox_url()},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session[ACTIVE_COMPANY_SESSION_KEY], self.other_company.pk)
        response = self.client.get(self.inbox_url())
        self.assertEqual({thread.pk for thread in response.context['threads']}, {self.other_practice_thread.pk})
        self.assertContains(response, self.other_practice_thread.subject)
        self.assertNotContains(response, self.recent_thread.subject)

    def test_inactive_patient_threads_are_excluded(self):
        self.login()
        response = self.client.get(self.inbox_url())
        self.assertNotIn(self.inactive_patient_thread.pk, {thread.pk for thread in response.context['threads']})
        self.assertNotContains(response, self.inactive_patient_thread.subject)

    def test_pagination_has_twenty_threads_and_reaches_remaining_threads(self):
        now = timezone.now()
        extra_threads = MessageThread.objects.bulk_create([
            MessageThread(
                company=self.company, patient=self.patient, subject=f'Additional conversation {number}',
                last_message_at=now - timedelta(minutes=number),
            )
            for number in range(23)
        ])
        for thread in extra_threads:
            add_participant(thread, self.doctor)
        self.login()
        first = self.client.get(self.inbox_url())
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.context['is_paginated'])
        self.assertEqual(first.context['page_obj'].paginator.count, 25)
        first_ids = {thread.pk for thread in first.context['threads']}
        self.assertEqual(len(first_ids), 20)
        self.assertTrue(any('page=2' in link for link in self.parse(first).links))
        second = self.client.get(self.inbox_url(), {'page': 2})
        self.assertEqual(second.status_code, 200)
        second_ids = {thread.pk for thread in second.context['threads']}
        self.assertEqual(len(second_ids), 5)
        self.assertFalse(first_ids & second_ids)
        self.assertEqual(first_ids | second_ids,
                         {self.recent_thread.pk, self.older_thread.pk, *(thread.pk for thread in extra_threads)})

    def test_unread_count_is_what_others_sent_since_you_last_read_and_listing_marks_nothing_read(self):
        now = timezone.now()
        already_read = self.add_message(staff_read_at=now - timedelta(hours=1))
        PatientMessage.objects.filter(pk=already_read.pk).update(created_at=now - timedelta(hours=2))
        MessageThreadParticipant.objects.filter(thread=self.recent_thread, user=self.doctor).update(
            last_read_at=now - timedelta(hours=1))
        self.add_message()
        self.add_message()
        self.add_message(sender=self.doctor)  # Your own messages are never unread for you.
        self.add_message(sender=self.administrator)  # A colleague's are, like the patient's.
        self.add_message(thread=self.other_practice_thread)
        before = dict(PatientMessage.objects.values_list('pk', 'staff_read_at'))
        positions = dict(MessageThreadParticipant.objects.values_list('pk', 'last_read_at'))
        self.login()
        response = self.client.get(self.inbox_url())
        counts = {thread.pk: thread.unread_count for thread in response.context['threads']}
        self.assertEqual(counts[self.recent_thread.pk], 3)
        self.assertEqual(counts[self.older_thread.pk], 0)
        self.assertEqual(response.context['patient_unread_total'], 3)
        self.assertEqual(dict(PatientMessage.objects.values_list('pk', 'staff_read_at')), before)
        self.assertEqual(dict(MessageThreadParticipant.objects.values_list('pk', 'last_read_at')), positions)

    def test_desktop_and_mobile_link_to_staff_inbox(self):
        # A Super Admin's Overview lists recent conversations; clinicians' and practice administrators' have none and
        # open Messages from Patient Records.
        self.login(self.super_admin)
        for name in ('desktop-dashboard', 'mobile-dashboard'):
            with self.subTest(workspace=name):
                response = self.client.get(reverse(f'portal:{name}'))
                self.assertEqual(response.status_code, 200)
                self.assertIn(self.inbox_url(), self.parse(response).links)

    def test_each_inbox_row_opens_that_conversation_beside_the_list(self):
        self.login()
        response = self.client.get(self.inbox_url())
        links = self.parse(response).links
        for thread in response.context['threads']:
            with self.subTest(thread=thread.pk):
                self.assertIn(self.thread_url(thread), links)

    def test_selected_older_thread_opens_instead_of_first_conversation(self):
        self.login()
        response = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]), {
            'thread': self.older_thread.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['workspace_tab'], 'messages')
        self.assertEqual([thread.pk for thread in response.context['message_threads']], [self.older_thread.pk])
        self.assertEqual(response.context['record_thread'].pk, self.older_thread.pk)
        self.assertContains(response, 'id="record-messages"')

    def test_invalid_or_out_of_scope_thread_selection_is_rejected(self):
        other_patient_thread = MessageThread.objects.create(
            company=self.company, patient=self.second_patient, subject='Different patient conversation',
        )
        self.login()
        for selection in ('not-an-id', '99999999', self.other_practice_thread.pk, other_patient_thread.pk):
            with self.subTest(selection=selection):
                response = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]), {'thread': selection})
                self.assertEqual(response.status_code, 404)
                self.assertIsNone(response.context.get('record_thread'))
                self.assertNotContains(response, self.recent_thread.subject, status_code=404)

    def test_showing_a_conversation_on_the_record_marks_only_it_read_for_the_practice(self):
        recent_message = self.add_message()
        older_message = self.add_message(thread=self.older_thread)
        outgoing_message = self.add_message(sender=self.doctor)
        other_practice_message = self.add_message(thread=self.other_practice_thread)
        self.login()
        response = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]),
                                   {'tab': 'messages', 'thread': self.older_thread.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['record_thread'].pk, self.older_thread.pk)
        self.assertEqual({message.pk for message in response.context['record_thread'].conversation_messages}, {older_message.pk})
        for message in (recent_message, older_message, outgoing_message, other_practice_message):
            message.refresh_from_db()
        self.assertIsNone(recent_message.staff_read_at)
        self.assertEqual(older_message.staff_read_by_id, self.doctor.pk)
        self.assertIsNotNone(older_message.staff_read_at)
        self.assertIsNone(outgoing_message.staff_read_at)
        self.assertIsNone(other_practice_message.staff_read_at)
        # Another unread conversation keeps its count on its chip.
        chips = {thread.pk: thread.unread_count for thread in response.context['record_threads']}
        self.assertEqual(chips[self.recent_thread.pk], 1)

    def test_messages_tab_waits_for_a_conversation_then_opens_it_beside_the_list(self):
        unread = self.add_message()
        self.login()
        response = self.client.get(self.inbox_url())
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'action="' + reverse('portal:staff-message-create', args=[self.recent_thread.pk]) + '"')
        self.assertContains(response, '<h1>Patient Records &amp; Messages</h1>', html=True)
        self.assertContains(response, 'Choose a conversation')
        unread.refresh_from_db()
        self.assertIsNone(unread.staff_read_at)
        self.assertFalse(AuditEvent.objects.filter(action='message.thread_viewed').exists())
        # A link to a conversation, including the older ones with an appointment, opens it here and marks it read.
        opened = self.client.get(self.inbox_url(), {'thread': self.recent_thread.pk, 'appointment': '7'})
        self.assertEqual(opened.status_code, 200)
        self.assertEqual(opened.context['inbox_thread'].pk, self.recent_thread.pk)
        self.assertContains(opened, 'action="' + reverse('portal:staff-message-create', args=[self.recent_thread.pk]) + '"')
        self.assertContains(opened, '<input type="hidden" name="return_to" value="patient_messages">')
        record = reverse('portal:patient-detail', args=[self.patient.pk])
        self.assertContains(opened, f'href="{record}?tab=overview">Open record<span class="sr-only"> for ')
        manage = reverse('portal:conversation-manage', args=[self.recent_thread.pk])
        # Message action opens in a modal; its link goes to the action's own page without scripts.
        self.assertContains(opened, f'record-messages__tool" href="{manage}" data-modal-open="conversation-manage" aria-haspopup="dialog">Message action</a>')
        self.assertContains(opened, '<dialog class="app-dialog" id="conversation-manage" aria-labelledby="conversation-manage-title">')
        self.assertContains(opened, f'<form class="console-form" method="post" action="{manage}" data-conversation-manage>')
        self.assertContains(opened, '<div data-shows-for-action="handover"><div class="console-form__field"><label for="manage_doctor">Clinician to add</label>')
        self.assertContains(opened, 'js/dialogs.js')
        self.assertContains(opened, 'js/conversation_management.js')
        unread.refresh_from_db()
        self.assertEqual(unread.staff_read_by_id, self.doctor.pk)
        # Applied from the modal, it goes back to the conversation in Patient messages.
        closed = self.client.post(manage, {'workflow_context': opened.context['inbox_thread'].manage_context, 'action': 'close', 'confirm': 'on'})
        self.assertRedirects(closed, f'{self.inbox_url()}?thread={self.recent_thread.pk}', fetch_redirect_response=False)
        self.recent_thread.refresh_from_db()
        self.assertTrue(self.recent_thread.is_closed)

    def test_search_finds_messages_patients_and_clinicians_only_in_your_conversations(self):
        PatientMessage.objects.create(company=self.company, thread=self.older_thread, sender=self.patient_user,
                                      body='The parcel arrived damaged')
        colleague = get_user_model().objects.create_user(email='inbox-colleague@example.test', first_name='Zola', last_name='Khumalo')
        CompanyMembership.objects.create(user=colleague, company=self.company, role=CompanyMembership.Role.DOCTOR)
        dose = MessageThread.objects.create(company=self.company, patient=self.second_patient, subject='Dose question',
                                            last_message_at=timezone.now())
        add_participant(dose, self.doctor)
        add_participant(dose, colleague)
        not_yours = MessageThread.objects.create(company=self.company, patient=self.second_patient, subject='Parcel for Zola')
        add_participant(not_yours, colleague)
        self.login()

        def found(search, **extra):
            return {thread.pk for thread in self.client.get(self.inbox_url(), {'q': search, **extra}).context['threads']}

        self.assertEqual(found('parcel'), {self.older_thread.pk})  # message text; never a conversation you are not in
        self.assertEqual(found('BETH'), {dose.pk})  # the patient's name, any case
        self.assertEqual(found('khumalo'), {dose.pk})  # a clinician in the conversation
        self.assertEqual(found('alice damaged'), {self.older_thread.pk})  # every word has to match
        self.assertEqual(found('parcel', status='open'), {self.older_thread.pk})
        self.assertEqual(found('parcel', status='closed'), set())
        response = self.client.get(self.inbox_url(), {'q': '  parcel  '})
        self.assertContains(response, 'value="parcel"')
        self.assertContains(response, f'href="{self.inbox_url()}?q=parcel&amp;thread={self.older_thread.pk}"')
        self.assertContains(self.client.get(self.inbox_url(), {'q': 'nothing here'}), 'No conversations match “nothing here”.')

    def test_new_patient_message_starts_a_conversation_with_one_of_your_patients(self):
        self.login()
        page = self.client.get(self.inbox_url())
        self.assertContains(page, f'href="{self.inbox_url()}?compose=1">New patient message</a>', count=2)
        compose = self.client.get(self.inbox_url(), {'compose': '1'})
        # A clinician can write to their own patients who can sign in; Beth isn't Doctor's patient.
        self.assertEqual(list(compose.context['new_thread_form'].fields['patient'].queryset), [self.patient])
        url = reverse('portal:staff-inbox-thread-create')
        refused = self.client.post(url, {'patient': self.second_patient.pk, 'body': 'Hello'})
        self.assertEqual(refused.status_code, 400)
        self.assertTrue(refused.context['inbox_composing'])
        response = self.client.post(url, {'patient': self.patient.pk, 'subject': '', 'body': 'Your results are in.'})
        thread = MessageThread.objects.get(messages__body='Your results are in.')
        self.assertRedirects(response, f'{self.inbox_url()}?thread={thread.pk}', fetch_redirect_response=False)
        self.assertEqual((thread.patient, thread.opened_by, thread.subject), (self.patient, self.doctor, 'Message from the practice'))
        self.assertEqual(list(thread.participants.all()), [self.doctor])
        # An administrator can write to any active patient with a login.
        self.login(self.administrator)
        patients = self.client.get(self.inbox_url(), {'compose': '1'}).context['new_thread_form'].fields['patient'].queryset
        self.assertEqual(set(patients), {self.patient, self.second_patient})

    def test_a_reply_from_patient_messages_comes_back_to_the_same_conversation_and_filter(self):
        self.add_message()
        self.login()
        url = reverse('portal:staff-message-create', args=[self.recent_thread.pk])
        response = self.client.post(url, {'body': 'Thanks, noted.', 'return_to': 'patient_messages', 'status': 'open'})
        self.assertRedirects(response, f'{self.inbox_url()}?status=open&thread={self.recent_thread.pk}',
                             fetch_redirect_response=False)
        self.assertTrue(PatientMessage.objects.filter(thread=self.recent_thread, sender=self.doctor, body='Thanks, noted.').exists())
        # An empty reply stays on Patient messages with the conversation open and says why.
        failed = self.client.post(url, {'body': '', 'return_to': 'patient_messages'})
        self.assertEqual(failed.status_code, 400)
        self.assertEqual(failed.context['inbox_thread'].pk, self.recent_thread.pk)
        self.assertContains(failed, 'class="record-error" role="alert"', status_code=400)

    def test_patient_record_messages_tab_opens_with_no_conversation(self):
        unread = self.add_message()
        self.login()
        response = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]), {'tab': 'messages'})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['record_thread'])
        self.assertContains(response, 'New messages stay unread until you open them.')
        self.assertContains(response, ', 1 unread message')
        unread.refresh_from_db()
        self.assertIsNone(unread.read_at)

    def test_explicit_inbox_selection_rejects_malformed_unknown_and_out_of_scope_threads(self):
        self.login()
        for selection in ('bad-id', '99999999', self.other_practice_thread.pk, self.inactive_patient_thread.pk):
            with self.subTest(selection=selection):
                self.assertEqual(self.client.get(self.inbox_url(), {'thread': selection}).status_code, 404)

    def test_open_and_closed_filters_split_conversations_by_whether_they_are_closed(self):
        MessageThread.objects.filter(pk=self.older_thread.pk).update(is_closed=True)
        empty = MessageThread.objects.create(company=self.company, patient=self.patient, subject='No messages yet')
        add_participant(empty, self.doctor)
        self.login()
        for status, expected in (
            ([], {self.recent_thread.pk, self.older_thread.pk, empty.pk}),
            (['open'], {self.recent_thread.pk, empty.pk}),
            (['closed'], {self.older_thread.pk}),
            (['open', 'closed'], {self.recent_thread.pk, self.older_thread.pk, empty.pk}),
        ):
            with self.subTest(status=status):
                response = self.client.get(self.inbox_url(), {'status': status})
                self.assertEqual({thread.pk for thread in response.context['threads']}, expected)
        self.assertContains(self.client.get(self.inbox_url(), {'status': 'open'}), '<input type="checkbox" name="status" value="open" checked>')
        # An old link to a filter that no longer exists ("all", "awaiting") shows everything.
        for old in ('all', 'awaiting'):
            response = self.client.get(self.inbox_url(), {'status': old})
            self.assertEqual(len(response.context['threads']), 3)
        self.assertNotContains(response, 'Awaiting a reply')

    def test_filters_menu_has_status_and_patient_sections_beside_the_search(self):
        other = Patient.objects.create(company=self.company, first_name='Zanele', last_name='Ndlovu', assigned_doctor=self.doctor)
        theirs = MessageThread.objects.create(company=self.company, patient=other, subject='Zanele question')
        add_participant(theirs, self.doctor)
        self.login()
        inbox = self.inbox_url()
        response = self.client.get(inbox)
        html = response.content.decode()
        # The Filters menu comes just before the search; there are no All, Open and Closed buttons.
        self.assertLess(html.index('<details class="filter-menu filter-menu--compact" data-filter-menu>'), html.index('data-conversation-search'))
        self.assertNotContains(response, 'class="inbox-filters"')
        for label in ('Status', 'Patient'):
            self.assertContains(response, f'<span class="filter-menu__label">{label}</span>')
        # Patients are the ones your conversations are with, by name.
        self.assertContains(response, f'<input type="checkbox" name="patients" value="{other.pk}"><span>Zanele Ndlovu</span>')
        self.assertContains(response, f'<input type="checkbox" name="patients" value="{self.patient.pk}">')
        self.assertNotContains(response, f'name="patients" value="{self.other_practice_patient.pk}"')
        response = self.client.get(inbox, {'patients': other.pk})
        self.assertEqual([thread.pk for thread in response.context['threads']], [theirs.pk])
        self.assertEqual(len(self.client.get(inbox, {'patients': [other.pk, self.patient.pk]}).context['threads']), 3)
        # Opening, replying to or marking a conversation keeps the filters; a new message's ?patient= isn't one.
        self.assertContains(response, f'href="{inbox}?patients={other.pk}&amp;thread={theirs.pk}"')
        reply = self.client.post(reverse('portal:staff-message-create', args=[theirs.pk]),
                                 {'body': 'Noted', 'return_to': 'patient_messages', 'patients': str(other.pk)})
        self.assertRedirects(reply, f'{inbox}?patients={other.pk}&thread={theirs.pk}', fetch_redirect_response=False)
        self.assertEqual(len(self.client.get(inbox, {'compose': 1, 'patient': other.pk}).context['threads']), 3)

    def test_inbox_reply_redirects_back_to_the_same_conversation(self):
        self.login()
        response = self.client.post(reverse('portal:staff-message-create', args=[self.older_thread.pk]), {
            'body': 'A reply from the shared inbox.', 'return_to': 'inbox',
        })
        self.assertRedirects(response, self.record_thread_url(self.older_thread), fetch_redirect_response=False)
        self.assertTrue(self.older_thread.messages.filter(sender=self.doctor, body='A reply from the shared inbox.').exists())

    def test_invalid_inbox_reply_keeps_selected_thread_and_draft(self):
        self.login()
        draft = 'x' * 5001
        response = self.client.post(reverse('portal:staff-message-create', args=[self.older_thread.pk]), {
            'body': draft, 'return_to': 'inbox',
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['record_thread'].pk, self.older_thread.pk)
        self.assertIn('body', response.context['record_message_form'].errors)
        self.assertEqual(response.context['record_message_form']['body'].value(), draft)
        self.assertFalse(PatientMessage.objects.exists())

    def test_the_page_has_no_intro_or_count_cards_and_rows_open_the_patient_record(self):
        self.add_message()
        self.login()
        response = self.client.get(self.inbox_url())
        self.assertNotContains(response, 'inbox-metrics')
        self.assertNotContains(response, 'Your conversations with patients at')
        self.assertNotContains(response, 'Conversations you are not part of are not shown')
        self.assertContains(response, f'<a class="record-inbox-thread is-unread" href="{self.thread_url(self.recent_thread)}"')
        # Both navigation layouts have a live red dot; the tab and row retain numeric unread counts.
        self.assertContains(response, '<span class="message-unread-dot" data-live-unread="total" aria-hidden="true"></span>', count=2)
        self.assertContains(response, '<span class="nav-badge" data-live-unread="patient" aria-hidden="true">1</span>')
        self.assertContains(response, '<span class="nav-badge" aria-hidden="true">1</span><span class="sr-only">, 1 unread message</span>', count=1)
        # Someone else's last message is shown as it is; your own starts "You:". The patient is the first tag.
        self.assertContains(response, '<span class="record-inbox-thread__preview">Secure inbox test message</span>', html=True)
        self.assertContains(response, '<span class="record-inbox-thread__people person-tags"><span class="sr-only">With </span><span class="person-tag person-tag--patient" title="Alice Patient · Patient">')
        self.assertContains(response, 'Alice Patient<span class="sr-only"> (Patient)</span></span>')
        self.add_message(sender=self.doctor)
        self.assertContains(self.client.get(self.inbox_url()), '<strong>You:</strong> Secure inbox test message')

    def test_mark_as_read_reads_everything_and_mark_as_unread_only_the_latest_for_you(self):
        first = self.add_message()
        reply = self.add_message(sender=self.doctor)
        latest = self.add_message()
        self.login()
        page = self.client.get(self.inbox_url())
        url = reverse('portal:staff-thread-read-state', args=[self.recent_thread.pk])
        self.assertContains(page, f'action="{url}"')
        self.assertContains(page, 'name="state" value="read" title="Mark as read"><svg class="read-state-icon"')
        self.assertContains(page, '<span class="sr-only">Mark as read: Alpha recent discussion</span>')
        response = self.client.post(url, {'state': 'read', 'status': 'open', 'page': '1'})
        self.assertRedirects(response, f'{self.inbox_url()}?status=open', fetch_redirect_response=False)
        for message in (first, latest):
            message.refresh_from_db()
            self.assertEqual(message.staff_read_by_id, self.doctor.pk)
        read_page = self.client.get(self.inbox_url())
        self.assertContains(read_page, 'name="state" value="unread" title="Mark as unread"><svg class="read-state-icon"')
        self.assertContains(read_page, 'class="read-state-icon__dot"')
        self.client.post(url, {'state': 'unread'})
        first.refresh_from_db()
        latest.refresh_from_db()
        reply.refresh_from_db()
        # Unread is yours alone now: the practice's receipts on the patient's messages stay as they were.
        self.assertIsNotNone(first.staff_read_at)
        self.assertIsNotNone(latest.staff_read_at)
        self.assertIsNone(reply.staff_read_at)
        counts = {thread.pk: thread.unread_count for thread in self.client.get(self.inbox_url()).context['threads']}
        self.assertEqual(counts[self.recent_thread.pk], 1)
        self.assertEqual(set(AuditEvent.objects.filter(action__startswith='message.marked').values_list('action', flat=True)),
                         {'message.marked_read', 'message.marked_unread'})

    def test_only_people_in_the_conversation_can_change_its_read_state(self):
        message = self.add_message()
        self.login(self.administrator)
        url = reverse('portal:staff-thread-read-state', args=[self.recent_thread.pk])
        self.assertEqual(self.client.post(url, {'state': 'read'}).status_code, 404)
        # A Super Admin can read it, but has no read state in a conversation they aren't in.
        self.login(self.super_admin)
        self.assertEqual(self.client.post(url, {'state': 'read'}).status_code, 403)
        self.login()
        self.assertEqual(self.client.post(url, {'state': 'maybe'}).status_code, 400)
        self.assertEqual(self.client.get(url).status_code, 405)
        message.refresh_from_db()
        self.assertIsNone(message.staff_read_at)
