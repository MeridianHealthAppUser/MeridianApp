"""The shared staff inbox lists only the selected practice's active patients."""

from django.test import override_settings
from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from care.models import Appointment, AppointmentProposal, AuditEvent, MessageThread, PatientMessage
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

    def login(self, user=None):
        self.client.force_login(user or self.doctor)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def inbox_url(self):
        return reverse('portal:staff-inbox')

    def thread_url(self, thread):
        return f'{self.inbox_url()}?thread={thread.pk}#inbox-conversation'

    def make_appointment(self):
        return Appointment.objects.create(
            company=self.company, patient=self.patient, clinician=self.doctor,
            starts_at=timezone.now() + timedelta(days=7), duration_minutes=30,
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

    def test_all_three_active_staff_roles_can_open_inbox(self):
        for user in (self.doctor, self.administrator, self.super_admin):
            with self.subTest(user=user.email):
                self.login(user)
                response = self.client.get(self.inbox_url())
                self.assertEqual(response.status_code, 200)
                self.assertEqual({thread.pk for thread in response.context['threads']},
                                 {self.recent_thread.pk, self.older_thread.pk})

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

    def test_unread_count_is_patient_origin_only_and_unselected_thread_stays_unread(self):
        self.add_message()
        self.add_message()
        self.add_message(read_at=timezone.now() - timedelta(hours=1))
        self.add_message(sender=self.doctor)
        self.add_message(sender=self.administrator)
        self.add_message(sender=self.second_patient_user)
        self.add_message(thread=self.other_practice_thread)
        before = dict(PatientMessage.objects.values_list('pk', 'read_at'))
        self.login()
        response = self.client.get(self.inbox_url(), {'thread': self.older_thread.pk})
        counts = {thread.pk: thread.unread_count for thread in response.context['threads']}
        self.assertEqual(counts[self.recent_thread.pk], 2)
        self.assertEqual(counts[self.older_thread.pk], 0)
        self.assertEqual(dict(PatientMessage.objects.values_list('pk', 'read_at')), before)

    def test_desktop_and_mobile_link_to_staff_inbox(self):
        self.login()
        for name in ('desktop-dashboard', 'mobile-dashboard'):
            with self.subTest(workspace=name):
                response = self.client.get(reverse(f'portal:{name}'))
                self.assertEqual(response.status_code, 200)
                self.assertIn(self.inbox_url(), self.parse(response).links)

    def test_each_inbox_thread_links_to_its_selected_conversation(self):
        self.login()
        response = self.client.get(self.inbox_url())
        links = self.parse(response).links
        for thread in response.context['threads']:
            with self.subTest(thread=thread.pk):
                matches = [
                    urlsplit(link) for link in links
                    if urlsplit(link).path == self.inbox_url()
                    and parse_qs(urlsplit(link).query).get('thread') == [str(thread.pk)]
                ]
                self.assertTrue(matches)
                self.assertTrue(any(link.fragment == 'inbox-conversation' for link in matches))

    def test_selected_older_thread_opens_instead_of_first_conversation(self):
        self.login()
        response = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]), {
            'thread': self.older_thread.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_thread_id'], self.older_thread.pk)
        self.assertEqual(response.context['workspace_tab'], 'messages')
        self.assertEqual([thread.pk for thread in response.context['message_threads']], [self.older_thread.pk])
        self.assertEqual(response.context['selected_thread'].pk, self.older_thread.pk)
        self.assertContains(response, 'id="patient-conversation"')

    def test_invalid_or_out_of_scope_thread_selection_is_rejected(self):
        other_patient_thread = MessageThread.objects.create(
            company=self.company, patient=self.second_patient, subject='Different patient conversation',
        )
        self.login()
        for selection in ('not-an-id', '99999999', self.other_practice_thread.pk, other_patient_thread.pk):
            with self.subTest(selection=selection):
                response = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]), {'thread': selection})
                self.assertEqual(response.status_code, 404)
                self.assertIsNone(response.context.get('selected_thread_id'))
                self.assertNotContains(response, self.recent_thread.subject, status_code=404)

    def test_inbox_marks_only_selected_thread_patient_messages_read(self):
        recent_message = self.add_message()
        older_message = self.add_message(thread=self.older_thread)
        outgoing_message = self.add_message(sender=self.doctor)
        other_practice_message = self.add_message(thread=self.other_practice_thread)
        self.login()
        response = self.client.get(self.inbox_url(), {'thread': self.older_thread.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_thread'].pk, self.older_thread.pk)
        self.assertEqual({message.pk for message in response.context['selected_thread'].conversation_messages}, {older_message.pk})
        recent_message.refresh_from_db()
        older_message.refresh_from_db()
        outgoing_message.refresh_from_db()
        other_practice_message.refresh_from_db()
        self.assertIsNone(recent_message.read_at)
        self.assertIsNotNone(older_message.read_at)
        self.assertIsNone(outgoing_message.read_at)
        self.assertIsNone(other_practice_message.read_at)

    def test_inbox_opens_with_no_conversation_so_messages_stay_unread(self):
        unread = self.add_message()
        self.login()
        response = self.client.get(self.inbox_url())
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['selected_thread'])
        self.assertContains(response, 'Choose a patient message')
        self.assertNotContains(response, 'action="' + reverse('portal:staff-message-create', args=[self.recent_thread.pk]) + '"')
        counts = {thread.pk: thread.unread_count for thread in response.context['threads']}
        self.assertEqual(counts[self.recent_thread.pk], 1)
        unread.refresh_from_db()
        self.assertIsNone(unread.read_at)
        self.assertFalse(AuditEvent.objects.filter(action='message.thread_viewed').exists())
        # Choosing the conversation is what marks it read.
        self.client.get(self.inbox_url(), {'thread': self.recent_thread.pk})
        unread.refresh_from_db()
        self.assertIsNotNone(unread.read_at)

    def test_patient_record_messages_tab_opens_with_no_conversation(self):
        unread = self.add_message()
        self.login()
        response = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]), {'tab': 'messages'})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['selected_thread'])
        self.assertContains(response, 'New messages from the patient stay unread until you open them.')
        self.assertContains(response, '· 1 unread')
        unread.refresh_from_db()
        self.assertIsNone(unread.read_at)

    def test_explicit_inbox_selection_rejects_malformed_unknown_and_out_of_scope_threads(self):
        self.login()
        for selection in ('bad-id', '99999999', self.other_practice_thread.pk, self.inactive_patient_thread.pk):
            with self.subTest(selection=selection):
                self.assertEqual(self.client.get(self.inbox_url(), {'thread': selection}).status_code, 404)

    def test_reply_filters_use_latest_sender_not_read_receipts(self):
        self.add_message(read_at=timezone.now())
        self.add_message(thread=self.older_thread)
        self.add_message(thread=self.older_thread, sender=self.administrator)
        MessageThread.objects.create(company=self.company, patient=self.patient, subject='No messages yet')
        self.login()
        for status, expected in (
            ('awaiting', {self.recent_thread.pk}),
            ('answered', {self.older_thread.pk}),
        ):
            with self.subTest(status=status):
                response = self.client.get(self.inbox_url(), {'status': status})
                self.assertEqual(response.status_code, 200)
                self.assertEqual({thread.pk for thread in response.context['threads']}, expected)
                self.assertEqual(response.context['metrics']['total'], 3)
                self.assertEqual(response.context['metrics']['awaiting'], 1)
                self.assertEqual(response.context['metrics']['answered'], 1)
                for thread in response.context['threads']:
                    self.assertTrue(thread.has_message)
                    self.assertEqual(thread.is_awaiting_reply, status == 'awaiting')

    def test_inbox_reply_redirects_back_to_the_same_conversation(self):
        self.login()
        response = self.client.post(reverse('portal:staff-message-create', args=[self.older_thread.pk]), {
            'body': 'A reply from the shared inbox.', 'return_to': 'inbox',
        })
        self.assertRedirects(response, self.thread_url(self.older_thread), fetch_redirect_response=False)
        self.assertTrue(self.older_thread.messages.filter(sender=self.doctor, body='A reply from the shared inbox.').exists())

    def test_invalid_inbox_reply_keeps_selected_thread_and_draft(self):
        self.login()
        draft = 'x' * 5001
        response = self.client.post(reverse('portal:staff-message-create', args=[self.older_thread.pk]), {
            'body': draft, 'return_to': 'inbox',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_thread'].pk, self.older_thread.pk)
        self.assertIn('body', response.context['message_form'].errors)
        self.assertEqual(response.context['message_form']['body'].value(), draft)
        self.assertFalse(PatientMessage.objects.exists())

    def test_inbox_proposal_returns_to_conversation_without_changing_booking(self):
        appointment = self.make_appointment()
        original_time = appointment.starts_at
        self.login()
        response = self.client.post(reverse('portal:staff-appointment-propose', args=[self.older_thread.pk]), {
            'appointment': appointment.pk, 'proposed_starts_at': (original_time + timedelta(days=1)).isoformat(),
            'note': 'Would this new time work?', 'return_to': 'inbox',
        })
        self.assertRedirects(response, self.thread_url(self.older_thread), fetch_redirect_response=False)
        proposal = AppointmentProposal.objects.get()
        self.assertEqual((proposal.thread_id, proposal.status), (self.older_thread.pk, 'pending'))
        appointment.refresh_from_db()
        self.assertEqual(appointment.starts_at, original_time)

    def test_invalid_inbox_proposal_keeps_thread_and_form_input(self):
        appointment = self.make_appointment()
        self.login()
        response = self.client.post(reverse('portal:staff-appointment-propose', args=[self.older_thread.pk]), {
            'appointment': appointment.pk, 'proposed_starts_at': 'bad-date',
            'note': 'Retain this proposal explanation.', 'return_to': 'inbox',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_thread'].pk, self.older_thread.pk)
        self.assertEqual(response.context['failed_form'], 'proposal_form')
        form = response.context['selected_thread'].proposal_form
        self.assertIn('proposed_starts_at', form.errors)
        self.assertEqual(form['note'].value(), 'Retain this proposal explanation.')
        self.assertFalse(AppointmentProposal.objects.exists())

    def test_inbox_acceptance_of_patient_proposal_returns_to_same_conversation(self):
        appointment = self.make_appointment()
        new_time = appointment.starts_at + timedelta(days=1)
        self.login(self.patient_user)
        response = self.client.post(reverse('portal:patient-appointment-propose', args=[self.older_thread.pk]), {
            'appointment': appointment.pk, 'proposed_starts_at': new_time.isoformat(),
        })
        self.assertEqual(response.status_code, 302)
        proposal = AppointmentProposal.objects.get()
        self.login()
        response = self.client.post(reverse('portal:staff-appointment-respond', args=[proposal.pk]), {
            'decision': 'accept', 'return_to': 'inbox',
        })
        self.assertRedirects(response, self.thread_url(self.older_thread), fetch_redirect_response=False)
        appointment.refresh_from_db()
        self.assertEqual(appointment.starts_at, new_time)

    def test_conflicting_proposal_acceptance_renders_error_in_selected_inbox_thread(self):
        appointment = self.make_appointment()
        original_time = appointment.starts_at
        new_time = original_time + timedelta(days=1)
        self.login(self.patient_user)
        response = self.client.post(reverse('portal:patient-appointment-propose', args=[self.older_thread.pk]), {
            'appointment': appointment.pk, 'proposed_starts_at': new_time.isoformat(),
        })
        self.assertEqual(response.status_code, 302)
        proposal = AppointmentProposal.objects.get()
        Appointment.objects.create(
            company=self.other_company, patient=self.other_practice_patient,
            clinician=self.doctor, starts_at=new_time, duration_minutes=30,
        )
        self.login()
        response = self.client.post(reverse('portal:staff-appointment-respond', args=[proposal.pk]), {
            'decision': 'accept', 'return_to': 'inbox',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['selected_thread'].pk, self.older_thread.pk)
        self.assertEqual(response.context['failed_proposal_id'], proposal.pk)
        self.assertTrue(response.context['proposal_error'])
        appointment.refresh_from_db()
        self.assertEqual(appointment.starts_at, original_time)
