"""Rev 3.11: Patient Records & Messages, the messages block on the record, and Request blood tests."""

import importlib
from datetime import timedelta

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from unittest.mock import patch
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from care.clinical import create_lab_request, submit_lab_result
from care.messaging import add_participant
from care.models import (
    ClinicianAssignment, MessageThread, MessageThreadParticipant, PathCareRequestForm, PatientMessage, PharmacyOrder,
)
from care.patient_files import read_patient_file
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY

from .workflow_context import make_workflow_context


@override_settings(MULTI_PRACTICE_ENABLED=True)
class RecordsAndMessagesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.company = Company.objects.create(name='Meridian Health', slug='records-messages')
        cls.doctor = users.create_user(email='rm-doctor@example.test', first_name='Sam', last_name='Marchant',
                                       pathcare_name='MARCHANT,DR S', pathcare_code='MARSA0')
        cls.dietitian = users.create_user(email='rm-dietitian@example.test', first_name='Dee', last_name='Titian')
        cls.admin = users.create_user(email='rm-admin@example.test', first_name='Lindiwe', last_name='Admin')
        for user, role, kind in ((cls.doctor, 'doctor', 'doctor'), (cls.dietitian, 'doctor', 'dietitian'),
                                 (cls.admin, 'practice_admin', '')):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role, clinician_type=kind)
        cls.patient_user = users.create_user(email='rm-nadia@example.test', first_name='Nadia', last_name='Mokoena')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Nadia', last_name='Mokoena',
                                             id_number='9204150123083', assigned_doctor=cls.doctor)
        PharmacyOrder.objects.create(company=cls.company, patient=cls.patient, status='submitted', submitted_at=timezone.now(),
                                     delivery_address={'line1': '14 Kloof Nek Road', 'city': 'Cape Town', 'province': 'WC',
                                                       'postal_code': '8001', 'phone': '082'})
        now = timezone.now()
        cls.answered = MessageThread.objects.create(company=cls.company, patient=cls.patient, subject='Travelling in October',
                                                    opened_by=cls.patient_user, last_message_at=now)
        cls.awaiting = MessageThread.objects.create(company=cls.company, patient=cls.patient, subject='Blood results and my dose',
                                                    opened_by=cls.patient_user, last_message_at=now - timedelta(hours=1))
        for thread in (cls.answered, cls.awaiting):
            add_participant(thread, cls.doctor)
        PatientMessage.objects.create(company=cls.company, thread=cls.answered, sender=cls.patient_user, body='I fly on the 28th.',
                                      staff_read_at=now, staff_read_by=cls.doctor)
        PatientMessage.objects.create(company=cls.company, thread=cls.answered, sender=cls.doctor, body='Thanks, noted.')
        # Sam has read the answered conversation; each staff member keeps their own read position.
        MessageThreadParticipant.objects.filter(thread=cls.answered, user=cls.doctor).update(last_read_at=timezone.now())
        cls.unread = PatientMessage.objects.create(company=cls.company, thread=cls.awaiting, sender=cls.patient_user,
                                                   body='I had the bloods done on Monday.')

    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def record(self, **query):
        return self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]), {'tab': 'messages', **query})

    def token(self, user, kind):
        request = RequestFactory().get('/')
        request.user = user
        return make_workflow_context(request, self.company, kind, patient=self.patient)

    def test_the_record_waits_for_a_conversation_to_be_chosen_then_opens_it_and_marks_it_read(self):
        self.login(self.doctor)
        inbox = self.client.get(reverse('portal:staff-inbox'))
        self.assertEqual(inbox.context['patient_unread_total'], 1)
        # Opened on its own, the record lists the conversations and shows none.
        response = self.record()
        self.assertIsNone(response.context['record_thread'])
        self.assertContains(response, 'Choose a conversation')
        self.assertContains(response, 'Blood results and my dose')
        self.assertContains(response, 'Travelling in October')
        self.assertContains(response, '<strong>You:</strong> Thanks, noted.')
        # Blood tests are requested on the Overview; the button beside the messages goes there.
        self.assertContains(response, f'href="{reverse("portal:patient-detail", args=[self.patient.pk])}?tab=overview#record-bloods"')
        # The Overview itself shows no messages.
        overview = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]))
        self.assertNotContains(overview, 'id="record-messages"')
        self.assertNotContains(overview, 'Blood results and my dose')
        self.unread.refresh_from_db()
        self.assertIsNone(self.unread.staff_read_at)
        # A link to a conversation opens it straight away.
        response = self.record(thread=self.awaiting.pk)
        self.assertEqual(response.context['record_thread'].pk, self.awaiting.pk)
        self.assertContains(response, 'I had the bloods done on Monday.')
        self.assertContains(response, 'placeholder="Reply to Nadia…"')
        self.unread.refresh_from_db()
        self.assertEqual(self.unread.staff_read_by_id, self.doctor.pk)
        # The patient's own unread state for staff messages is untouched.
        self.assertIsNone(PatientMessage.objects.get(sender=self.doctor).read_at)
        self.assertEqual(self.client.get(reverse('portal:staff-inbox')).context['patient_unread_total'], 0)
        # New conversation shows the start form even when there are conversations.
        self.assertContains(self.record(new=1), 'placeholder="Write to Nadia to start a conversation…"')

    def test_the_conversation_list_names_other_clinicians_and_shows_the_time_today_or_the_date(self):
        add_participant(self.answered, self.dietitian)
        older = MessageThread.objects.create(company=self.company, patient=self.patient, subject='Last year',
                                             opened_by=self.patient_user, last_message_at=timezone.now() - timedelta(days=400))
        add_participant(older, self.doctor)
        PatientMessage.objects.create(company=self.company, thread=older, sender=self.patient_user, body='An old question.')
        self.login(self.doctor)
        response = self.record()
        rows = {thread.subject: thread for thread in response.context['record_threads']}
        # Only the other staff are tagged, by role; with none, there's no line of tags. The patient isn't tagged here.
        self.assertEqual([(tag['name'], tag['kind']) for tag in rows['Travelling in October'].people_tags], [('D. Titian', 'dietitian')])
        self.assertEqual(rows['Blood results and my dose'].people_tags, [])
        self.assertContains(response, 'record-inbox-thread__people', count=1)
        self.assertNotContains(response, 'person-tag--patient')
        # A received message shows on its own; the time for today, otherwise the date.
        self.assertContains(response, '<span class="record-inbox-thread__preview">An old question.</span>')
        today = timezone.localtime(rows['Travelling in October'].last_activity)
        if today.date() == timezone.localdate():
            self.assertContains(response, f'>{today:%H:%M}</time>')
        self.assertContains(response, f'>{timezone.localtime(older.last_message_at).strftime("%-d %b %Y")}</time>')

    def test_searching_the_record_s_messages_narrows_the_list_without_opening_or_logging_anything(self):
        from care.models import AuditEvent, PracticeSettings

        PracticeSettings.objects.update_or_create(company=self.company, defaults={'store_view_log': True})
        self.login(self.doctor)
        # On the page: the list narrows, and an open conversation stays open even when it doesn't match.
        response = self.record(q='monday', thread=self.answered.pk)
        self.assertEqual([thread.pk for thread in response.context['record_threads']], [self.awaiting.pk])
        self.assertEqual(response.context['record_thread'].pk, self.answered.pk)
        self.assertContains(response, 'data-search-url="' + reverse('portal:record-conversation-search', args=[self.patient.pk]) + '"')
        # As you type, only the list comes back: nothing opens, nothing is marked read, the record view isn't logged.
        viewed = AuditEvent.objects.filter(action='patient.record_viewed').count()
        search = reverse('portal:record-conversation-search', args=[self.patient.pk])
        listed = self.client.get(search, {'q': 'travelling', 'thread': self.awaiting.pk})
        self.assertEqual([thread.pk for thread in listed.context['record_threads']], [self.answered.pk])
        self.assertIsNone(listed.context['record_thread'])
        self.assertContains(listed, 'data-search-region="list"')
        self.assertNotContains(listed, 'record-messages__pane')
        self.assertContains(self.client.get(search, {'q': 'nothing here'}), 'No conversations match “nothing here”.')
        self.unread.refresh_from_db()
        self.assertIsNone(self.unread.staff_read_at)
        self.assertEqual(AuditEvent.objects.filter(action='patient.record_viewed').count(), viewed)
        # Only conversations you are in, and only patients in this practice. The dietitian saw Nadia before.
        ClinicianAssignment.objects.create(company=self.company, patient=self.patient, clinician=self.dietitian,
                                           ended_at=timezone.now())
        self.login(self.dietitian)
        self.assertEqual(list(self.client.get(search).context['record_threads']), [])

    def test_mark_as_unread_from_the_record_returns_to_it_with_nothing_open(self):
        self.login(self.doctor)
        self.record(thread=self.awaiting.pk)
        response = self.client.post(reverse('portal:staff-thread-read-state', args=[self.awaiting.pk]),
                                    {'state': 'unread', 'return_to': 'record'})
        self.assertRedirects(response, f'{reverse("portal:patient-detail", args=[self.patient.pk])}?tab=messages',
                             fetch_redirect_response=False)
        listed = {thread.pk: thread.unread_count for thread in self.record().context['record_threads']}
        self.assertEqual(listed[self.awaiting.pk], 1)

    def test_a_reply_from_the_record_reaches_the_patient_and_returns_to_the_record(self):
        self.login(self.doctor)
        response = self.client.post(reverse('portal:staff-message-create', args=[self.awaiting.pk]),
                                    {'body': 'Everything is in range.', 'return_to': 'record'})
        record = reverse('portal:patient-detail', args=[self.patient.pk])
        self.assertRedirects(response, f'{record}?tab=messages&thread={self.awaiting.pk}', fetch_redirect_response=False)
        reply = self.awaiting.messages.get(sender=self.doctor)
        self.assertIsNone(reply.read_at)
        self.login(self.patient_user)
        self.assertEqual(self.client.get(reverse('portal:patient-dashboard')).context['patient_unread_messages'], 2)

    def test_the_record_messages_tab_pages_conversations_twenty_at_a_time(self):
        from .patient_workspace import workspace_url

        for number in range(21):  # With the two from setUpTestData, 23 conversations.
            thread = MessageThread.objects.create(company=self.company, patient=self.patient, subject=f'Extra {number}')
            add_participant(thread, self.doctor)
        self.login(self.doctor)
        first = self.client.get(workspace_url(self.patient, 'messages'))
        self.assertEqual(len(first.context['record_threads']), 20)
        self.assertContains(first, '1–20 of 23 conversations')
        self.assertContains(first, f'href="{escape(workspace_url(self.patient, "messages", page=2))}">Older</a>')
        self.assertNotContains(first, '>Newer</a>')
        second = self.client.get(workspace_url(self.patient, 'messages', page=2))
        self.assertEqual(len(second.context['record_threads']), 3)
        self.assertContains(second, '21–23 of 23 conversations')
        self.assertContains(second, f'href="{escape(workspace_url(self.patient, "messages"))}">Newer</a>')
        # A conversation on another page still opens, and the list stays on its page.
        oldest = second.context['record_threads'][-1]
        opened = self.client.get(workspace_url(self.patient, 'messages', thread=oldest.pk))
        self.assertEqual(opened.context['record_thread'].pk, oldest.pk)
        self.assertIn('page=2', second.context['record_threads'][0].list_url)

    def test_overview_wording_matches_what_each_role_sees(self):
        self.login(self.dietitian)
        page = self.client.get(reverse('portal:desktop-dashboard'))
        for text in ('Your upcoming appointments and open tasks.', 'Your booked appointments', 'You have no upcoming appointments.'):
            self.assertContains(page, text)
        # Clinicians find patients in Patient Records and conversations in Messages, so Overview doesn't list them.
        for stale in ('Open inbox', 'in this practice', 'View Patients', 'within Meridian Health', 'id="patients-heading"',
                      'id="messages-heading"', 'Recent conversations', 'View patient messages'):
            self.assertNotContains(page, stale)
        self.assertNotIn('patients', page.context)
        self.assertNotIn('recent_messages', page.context)
        mobile = self.client.get(reverse('portal:mobile-dashboard'))
        for stale in ('id="mobile-patients-heading"', 'id="mobile-messages-heading"'):
            self.assertNotContains(mobile, stale)
        # Practice administrators get the same Overview, with the practice's appointments rather than their own.
        self.login(self.admin)
        page = self.client.get(reverse('portal:desktop-dashboard'))
        self.assertContains(page, 'Your upcoming appointments and open tasks.')
        self.assertContains(page, 'Booked appointments')
        for stale in ('id="patients-heading"', 'Recent conversations', 'aria-label="Practice metrics"'):
            self.assertNotContains(page, stale)
        self.assertNotIn('recent_messages', page.context)
        mobile = self.client.get(reverse('portal:mobile-dashboard'))
        for stale in ('id="mobile-messages-heading"', 'aria-label="Practice metrics"'):
            self.assertNotContains(mobile, stale)
        # Super Admins keep the count cards, Patients and Recent conversations.
        CompanyMembership.objects.filter(user=self.admin).update(role=CompanyMembership.Role.SUPER_ADMIN)
        page = self.client.get(reverse('portal:desktop-dashboard'))
        for text in ('aria-label="Practice metrics"', 'People currently receiving care', 'Recent conversations',
                     'Your latest patient conversations', 'View patient messages'):
            self.assertContains(page, text)
        self.assertNotContains(page, 'Patients assigned to you')

    def test_back_to_conversation_returns_to_where_the_conversation_was_opened(self):
        from .inbox import staff_inbox_url
        from .patient_workspace import workspace_url

        self.login(self.doctor)
        manage = reverse('portal:conversation-manage', args=[self.awaiting.pk])
        record = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]),
                                 {'tab': 'messages', 'thread': self.awaiting.pk})
        self.assertContains(record, f'href="{manage}?from=record" data-modal-open="conversation-manage"')
        # The modal's form goes back to the record too.
        self.assertContains(record, f'<form class="console-form" method="post" action="{manage}?from=record" data-conversation-manage>')
        self.assertContains(self.client.get(f'{manage}?from=record'),
                            f'href="{escape(workspace_url(self.patient, "messages", thread=self.awaiting.pk))}">Back to conversation')
        inbox = self.client.get(reverse('portal:staff-inbox'), {'thread': self.awaiting.pk})
        self.assertContains(inbox, f'href="{manage}"')
        self.assertContains(self.client.get(manage), f'href="{escape(staff_inbox_url(self.awaiting))}">Back to conversation')
        # Once an action is applied, it goes back to the conversation where it was opened.
        token = self.client.get(f'{manage}?from=record').context['workflow_context']
        closed = self.client.post(f'{manage}?from=record', {'workflow_context': token, 'action': 'close', 'confirm': 'on'})
        self.assertRedirects(closed, workspace_url(self.patient, 'messages', thread=self.awaiting.pk), fetch_redirect_response=False)
        token = self.client.get(manage).context['workflow_context']
        reopened = self.client.post(manage, {'workflow_context': token, 'action': 'reopen', 'confirm': 'on'})
        self.assertRedirects(reopened, staff_inbox_url(self.awaiting), fetch_redirect_response=False)

    def test_staff_see_who_added_a_clinician_to_a_patient_conversation(self):
        add_participant(self.awaiting, self.dietitian, added_by=self.doctor)
        ClinicianAssignment.objects.create(company=self.company, patient=self.patient, clinician=self.dietitian,
                                           ended_at=timezone.now())
        record = reverse('portal:patient-detail', args=[self.patient.pk])
        for user, line in ((self.doctor, 'You added Dee Titian'), (self.dietitian, 'Sam Marchant added Dee Titian')):
            with self.subTest(user=user.email):
                self.login(user)
                page = self.client.get(record, {'tab': 'messages', 'thread': self.awaiting.pk})
                if user == self.doctor:
                    self.assertContains(page, line)
                else:
                    # Not Nadia's Prescribing doctor, the dietitian has no Messages tab on her record: the link opens
                    # the conversation in Patient messages.
                    self.assertRedirects(page, f'{reverse("portal:staff-inbox")}?thread={self.awaiting.pk}',
                                         fetch_redirect_response=False)
                inbox = self.client.get(reverse('portal:staff-inbox'), {'thread': self.awaiting.pk})
                self.assertContains(inbox, line)

    def test_with_no_conversation_the_first_message_starts_one_named_for_the_sender(self):
        other_user = get_user_model().objects.create_user(email='rm-beth@example.test')
        beth = Patient.objects.create(company=self.company, user=other_user, first_name='Beth', last_name='Patient')
        self.doctor.title = 'DR'
        self.doctor.save(update_fields=['title'])
        self.dietitian.title = 'MS'
        self.dietitian.save(update_fields=['title'])
        untitled = get_user_model().objects.create_user(email='rm-untitled@example.test', first_name='Ana', last_name='Silva')
        CompanyMembership.objects.create(company=self.company, user=untitled, role='doctor', clinician_type='doctor')
        for user, subject in ((self.doctor, 'Message from Dr Marchant'), (self.dietitian, 'Message from Ms Titian'),
                              (untitled, 'Message from Ana Silva'), (self.admin, 'Message from the practice')):
            with self.subTest(user=user.email):
                # A clinician starts conversations with their own patients only.
                Patient.objects.filter(pk=beth.pk).update(assigned_doctor=None if user == self.admin else user)
                self.login(user)
                page = self.client.get(reverse('portal:patient-detail', args=[beth.pk]), {'tab': 'messages'})
                self.assertContains(page, 'No messages with Beth yet.')
                self.assertContains(page, 'placeholder="Write to Beth to start a conversation…"')
                response = self.client.post(reverse('portal:staff-thread-create', args=[beth.pk]),
                                            {'body': 'Hello Beth', 'return_to': 'record', 'subject': 'ignored'})
                thread = MessageThread.objects.filter(patient=beth, opened_by=user).get()
                self.assertEqual(thread.subject, subject)
                self.assertRedirects(response, f'{reverse("portal:patient-detail", args=[beth.pk])}?tab=messages&thread={thread.pk}',
                                     fetch_redirect_response=False)

    def test_administrators_see_the_messages_block_but_cannot_request_blood_tests(self):
        add_participant(self.awaiting, self.admin)
        self.login(self.admin)
        page = self.record()
        self.assertContains(page, 'id="record-messages"')
        self.assertNotContains(page, '#record-bloods')
        self.assertNotContains(page, 'id="record-bloods"')
        for name in ('patient-blood-tests', 'patient-blood-tests-preview', 'patient-blood-tests-preview-pdf'):
            with self.subTest(name=name):
                response = self.client.post(reverse(f'portal:{name}', args=[self.patient.pk]),
                                            {'workflow_context': self.token(self.admin, 'patient-bloods'), 'tests': 'FBC'})
                self.assertEqual(response.status_code, 403)
        # Even as Nadia's clinician, a dietitian doesn't request blood tests.
        Patient.objects.filter(pk=self.patient.pk).update(assigned_doctor=self.dietitian)
        self.login(self.dietitian)
        self.assertNotContains(self.record(tab='overview'), 'id="record-bloods"')

    def test_a_doctor_previews_sends_views_and_resends_the_form(self):
        self.login(self.doctor)
        page = self.record(tab='overview')
        self.assertContains(page, 'id="record-bloods"')
        self.assertContains(page, 'Email to rm-nadia@example.test')
        summary = {row['label']: row for row in page.context['pathcare_summary']}
        self.assertEqual(summary['PathCare code']['value'], 'MARSA0')
        self.assertTrue(summary['Title']['missing'])
        token = self.token(self.doctor, 'patient-bloods')
        preview = self.client.post(reverse('portal:patient-blood-tests-preview-pdf', args=[self.patient.pk]),
                                   {'workflow_context': token, 'tests': 'FBC, ferritin, TSH'})
        self.assertEqual((preview.status_code, preview['Content-Type']), (200, 'application/pdf'))
        # It is shown inside the preview dialog on the record, so it may be framed by the app itself.
        self.assertEqual(preview['X-Frame-Options'], 'SAMEORIGIN')
        self.assertFalse(PathCareRequestForm.objects.exists())
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('portal:patient-blood-tests', args=[self.patient.pk]),
                                        {'workflow_context': token, 'tests': 'FBC, ferritin, TSH',
                                         'submission_key': '6f0c1d7a-6b3a-4d7e-8f5e-2a1b3c4d5e6f'})
        form = PathCareRequestForm.objects.get()
        self.assertRedirects(response, f'{reverse("portal:patient-detail", args=[self.patient.pk])}?tab=overview&log=clinical#log-blood-{form.lab_request_id}',
                             fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)
        log = self.record(tab='overview', log='clinical')
        self.assertContains(log, 'PathCare form for FBC, ferritin, TSH sent.')
        self.assertContains(log, 'Emailed to rm-nadia@example.test')
        view_url = reverse('portal:patient-blood-test-form', args=[self.patient.pk, form.pk])
        self.assertContains(log, view_url)
        stored = self.client.get(view_url)
        self.assertEqual(b''.join(stored), read_patient_file(form.file))
        with self.captureOnCommitCallbacks(execute=True):
            resent = self.client.post(reverse('portal:patient-blood-test-resend', args=[self.patient.pk, form.pk]),
                                      {'workflow_context': token})
        self.assertEqual(resent.status_code, 302)
        self.assertEqual(len(mail.outbox), 2)
        self.login(self.admin)
        self.assertEqual(self.client.get(view_url).status_code, 403)

    def test_a_doctor_without_pathcare_details_is_told_to_ask_the_practice(self):
        self.doctor.pathcare_code = ''
        self.doctor.save(update_fields=['pathcare_code'])
        self.login(self.doctor)
        page = self.record(tab='overview')
        self.assertTrue(page.context['pathcare_blocked'])
        self.assertContains(page, 'Ask the practice to add them in Users and roles.')
        response = self.client.post(reverse('portal:patient-blood-tests', args=[self.patient.pk]),
                                    {'workflow_context': self.token(self.doctor, 'patient-bloods'), 'tests': 'FBC'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PathCareRequestForm.objects.exists())

    def send_bloods(self, **data):
        # Outside a test's own transaction the email is tried before the view replies, so do the same here.
        with patch('django.db.transaction.on_commit', new=lambda func, using=None, robust=False: func()):
            return self.client.post(reverse('portal:patient-blood-tests', args=[self.patient.pk]),
                                    {'workflow_context': self.token(self.doctor, 'patient-bloods'), 'tests': 'FBC', **data},
                                    follow=True)

    def test_anything_that_stops_the_form_shows_an_error_toast(self):
        self.login(self.doctor)
        # Nothing to send: the patient has no email address.
        get_user_model().objects.filter(pk=self.patient_user.pk).update(email='rm-gone@example.test')
        with patch('portal.pathcare_views.request_blood_tests', side_effect=ValidationError('This patient has no email address on file, so the form can’t be emailed.')):
            response = self.send_bloods()
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'class="flash-message toast error"', status_code=400)
        self.assertContains(response, 'no email address on file', status_code=400)
        self.assertNotContains(response, 'id="record-bloods-dialog" aria-labelledby="record-bloods-title" open', status_code=400)
        # Making the form fails unexpectedly: nothing is stored or sent.
        with patch('care.pathcare.fill_form', side_effect=RuntimeError('pdf broke')), self.assertLogs('portal.pathcare_views', 'ERROR'):
            response = self.send_bloods()
        self.assertContains(response, 'Something went wrong making the blood test form, so nothing was sent.', status_code=500)
        self.assertFalse(PathCareRequestForm.objects.exists())
        # The email can't be sent: the form is kept, marked failed and the toast says so.
        with patch('django.core.mail.EmailMessage.send', side_effect=OSError('mail server down')):
            response = self.send_bloods(submission_key='0b6c9b1e-2f0a-4c55-9d3e-7f1a2b3c4d5e')
        self.assertContains(response, 'couldn’t be sent. Use Resend in the log to try again.')
        self.assertEqual(PathCareRequestForm.objects.get().email_status, 'failed')
        # The task queue can't be reached: the same.
        with patch('care.tasks.send_pathcare_form_email.delay', side_effect=ConnectionError('no broker')), \
                self.assertLogs('care.pathcare', 'ERROR'):
            response = self.send_bloods(submission_key='1c7d0a2f-3a1b-4d66-8e4f-8a2b3c4d5e6f')
        self.assertContains(response, 'couldn’t be sent.')
        self.assertEqual(PathCareRequestForm.objects.order_by('-pk').first().email_status, 'failed')
        # Email isn't set up yet: nothing is sent or printed, the form is kept and the toast says why.
        with override_settings(EMAIL_ENABLED=False), self.assertLogs('care.pathcare', 'WARNING'):
            response = self.send_bloods(submission_key='2d8e1b3a-4b2c-4e77-9f50-9b3c4d5e6f70')
        self.assertContains(response, 'Email isn’t set up yet, so the blood test form wasn’t emailed.')
        self.assertEqual(PathCareRequestForm.objects.order_by('-pk').first().email_status, 'failed')
        self.assertEqual(len(mail.outbox), 0)
        # A preview that can't be made says why, marked for the dialog to show as a toast.
        preview = self.client.post(reverse('portal:patient-blood-tests-preview-pdf', args=[self.patient.pk]),
                                   {'workflow_context': self.token(self.doctor, 'patient-bloods'), 'tests': 'x' * 301})
        self.assertContains(preview, 'data-preview-error', status_code=400)

    def login_patient(self):
        self.client.force_login(self.patient_user)
        session = self.client.session
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def test_the_patient_can_open_and_download_their_form_until_they_upload_results(self):
        self.login(self.doctor)
        self.send_bloods()
        form = PathCareRequestForm.objects.get()
        older = create_lab_request(company=self.company, patient=self.patient, actor=self.doctor, panel_name='Older request')
        self.login_patient()
        form_url = reverse('portal:patient-lab-form', args=[form.lab_request_id])
        listing = self.client.get(reverse('portal:patient-labs'))
        self.assertContains(listing, f'{form_url}?download=1')
        self.assertNotContains(listing, reverse('portal:patient-lab-form', args=[older.pk]))
        detail = self.client.get(reverse('portal:patient-lab-detail', args=[form.lab_request_id]))
        self.assertContains(detail, 'Your PathCare form')
        self.assertContains(detail, f'href="{form_url}" target="_blank"')
        opened = self.client.get(form_url)
        self.assertEqual((opened.status_code, opened['Content-Type']), (200, 'application/pdf'))
        self.assertTrue(opened['Content-Disposition'].startswith('inline;'))
        self.assertEqual(opened.content, read_patient_file(form.file))
        self.assertTrue(self.client.get(form_url, {'download': '1'})['Content-Disposition'].startswith('attachment;'))
        self.assertEqual(self.client.get(reverse('portal:patient-lab-form', args=[older.pk])).status_code, 404)
        # Someone else can't open it.
        stranger = get_user_model().objects.create_user(email='rm-stranger@example.test')
        Patient.objects.create(company=self.company, user=stranger, first_name='Sam', last_name='Stranger')
        self.client.force_login(stranger)
        session = self.client.session
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = self.company.pk
        session.save()
        self.assertEqual(self.client.get(form_url).status_code, 404)
        # Once the results are uploaded, the form is no longer offered.
        submit_lab_result(lab_request=form.lab_request, actor=self.patient_user, filename='results.pdf',
                          content=b'%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF')
        self.login_patient()
        self.assertEqual(self.client.get(form_url).status_code, 404)
        self.assertNotContains(self.client.get(reverse('portal:patient-lab-detail', args=[form.lab_request_id])), 'Your PathCare form')
        self.assertNotContains(self.client.get(reverse('portal:patient-labs')), form_url)

    def test_the_care_team_sets_the_title_printed_on_the_form(self):
        self.login(self.admin)
        response = self.client.post(reverse('portal:patient-title', args=[self.patient.pk]),
                                    {'workflow_context': self.token(self.admin, 'patient-title'), 'title': 'MS'})
        self.assertEqual(response.status_code, 302)
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.title, 'MS')
        self.login(self.patient_user)
        self.assertEqual(self.client.post(reverse('portal:patient-title', args=[self.patient.pk]), {'title': 'DR'}).status_code, 403)

    def test_one_menu_entry_and_three_tabs(self):
        self.login(self.doctor)
        for name in ('staff-inbox', 'patient-list', 'team-inbox'):
            with self.subTest(page=name):
                page = self.client.get(reverse(f'portal:{name}'))
                self.assertContains(page, '<h1>Patient Records &amp; Messages</h1>', html=True)
                for tab in ('Messages', 'All patients', 'Team'):
                    self.assertContains(page, tab)
                self.assertContains(page, 'console-nav__item is-active" aria-current')
        self.assertContains(self.client.get(reverse('portal:patient-list')), 'Mokoena')


class StaffReadStateSeedTests(TestCase):
    def test_answered_or_opened_patient_messages_count_as_read_and_unopened_trailing_ones_stay_unread(self):
        company = Company.objects.create(name='Seed Practice', slug='seed-practice')
        users = get_user_model().objects
        doctor = users.create_user(email='seed-doctor@example.test')
        CompanyMembership.objects.create(company=company, user=doctor, role='doctor')
        patient_user = users.create_user(email='seed-patient@example.test')
        patient = Patient.objects.create(company=company, user=patient_user, first_name='Seed', last_name='Patient')
        thread = MessageThread.objects.create(company=company, patient=patient, subject='Seed')
        first = PatientMessage.objects.create(company=company, thread=thread, sender=patient_user, body='One')
        reply = PatientMessage.objects.create(company=company, thread=thread, sender=doctor, body='Reply')
        opened_at = timezone.now()
        opened = PatientMessage.objects.create(company=company, thread=thread, sender=patient_user, body='Thanks!',
                                               read_at=opened_at)
        trailing = PatientMessage.objects.create(company=company, thread=thread, sender=patient_user, body='Two')
        other = MessageThread.objects.create(company=company, patient=patient, subject='Other')
        patient_read_reply = PatientMessage.objects.create(company=company, thread=other, sender=doctor, body='Later',
                                                           read_at=opened_at)
        migration = importlib.import_module('care.migrations.0016_staff_read_state_and_pathcare_forms')
        migration.seed_staff_read_state(apps, None)
        for message in (first, opened, trailing, patient_read_reply):
            message.refresh_from_db()
        self.assertEqual((first.staff_read_at, first.staff_read_by_id), (reply.created_at, doctor.pk))
        self.assertEqual((opened.staff_read_at, opened.staff_read_by_id), (opened_at, None))
        self.assertIsNone(trailing.staff_read_at)
        self.assertIsNone(patient_read_reply.staff_read_at)  # read_at on staff messages is the patient's.


@override_settings(MULTI_PRACTICE_ENABLED=True)
class PerPersonReadStateTests(TestCase):
    """In a patient conversation with several staff, unread is per person, as in team conversations: anything
    someone else sent (the patient or a colleague) is unread for you until you open it, and your reading changes
    nobody else's."""

    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.company = Company.objects.create(name='Group Practice', slug='group-read-state')
        cls.sam = users.create_user(email='group-sam@example.test', first_name='Sam', last_name='Marchant')
        cls.josh = users.create_user(email='group-josh@example.test', first_name='Josh', last_name='Czech')
        cls.admin = users.create_user(email='group-admin@example.test', first_name='Lindiwe', last_name='Admin')
        for user, role, kind in ((cls.sam, 'doctor', 'doctor'), (cls.josh, 'doctor', 'doctor'),
                                 (cls.admin, 'practice_admin', '')):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role, clinician_type=kind)
        cls.nadia_user = users.create_user(email='group-nadia@example.test', first_name='Nadia', last_name='Mokoena')
        cls.nadia = Patient.objects.create(company=cls.company, user=cls.nadia_user, first_name='Nadia',
                                           last_name='Mokoena', assigned_doctor=cls.sam)

    def setUp(self):
        from care.messaging import start_conversation

        # Nadia writes to Sam, her clinician.
        self.thread = start_conversation(patient=self.nadia, opened_by=self.nadia_user, recipient=self.sam,
                                         subject='Dose question', body='Can I take my dose a day late?')

    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def add_josh(self):
        from care.messaging import hand_over

        hand_over(thread=self.thread, actor=self.sam, clinician=self.josh)

    def unread_for(self, user, thread=None):
        """The Messages tab total and the conversation's count in the list, for one staff member."""
        from .inbox import _inbox_threads, message_tab_counts

        thread = thread or self.thread
        listed = {row.pk: row.unread_count for row in _inbox_threads(self.company, user)}
        return message_tab_counts(self.company, user)['patient_unread_total'], listed[thread.pk]

    def test_a_colleague_s_message_is_unread_for_the_others_but_not_for_the_sender(self):
        from care.messaging import mark_read_by_staff

        self.add_josh()
        add_participant(self.thread, self.admin, added_by=self.sam)
        for user in (self.sam, self.josh, self.admin):
            mark_read_by_staff(self.thread, user)
            self.assertEqual(self.unread_for(user), (0, 0))
        self.login(self.josh)
        self.client.post(reverse('portal:staff-message-create', args=[self.thread.pk]),
                         {'body': 'Agreed, a day late is fine.', 'return_to': 'patient_messages'})
        self.assertEqual(self.unread_for(self.josh), (0, 0))
        self.login(self.sam)
        inbox = self.client.get(reverse('portal:staff-inbox'))
        self.assertEqual(inbox.context['patient_unread_total'], 1)
        self.assertEqual({row.pk: row.unread_count for row in inbox.context['threads']}[self.thread.pk], 1)
        dashboard = self.client.get(reverse('portal:desktop-dashboard'))
        self.assertEqual(dashboard.context['metrics']['unread_messages'], 1)
        # The sidebar's badge, worked out once per request.
        self.assertEqual(dashboard.wsgi_request._staff_message_counts, {'patient_unread_total': 1, 'team_unread_total': 0})
        # An administrator's menu carries it too, on Patient Records & Messages.
        self.login(self.admin)
        self.assertContains(self.client.get(reverse('portal:desktop-dashboard')),
                            '<span class="message-unread-dot" data-live-unread="total" aria-hidden="true">1</span>')

    def test_opening_a_conversation_reads_it_only_for_the_person_who_opened_it(self):
        self.add_josh()
        self.login(self.sam)
        opened = self.client.get(reverse('portal:patient-detail', args=[self.nadia.pk]),
                                 {'tab': 'messages', 'thread': self.thread.pk})
        self.assertEqual(opened.context['record_thread'].pk, self.thread.pk)
        self.assertEqual(self.unread_for(self.sam), (0, 0))
        self.assertEqual(self.unread_for(self.josh), (1, 1))
        # The practice's receipt on Nadia's message is still set, by whoever opened it first.
        message = self.thread.messages.get()
        self.assertEqual(message.staff_read_by_id, self.sam.pk)
        self.login(self.josh)
        opened = self.client.get(reverse('portal:staff-inbox'), {'thread': self.thread.pk})
        self.assertEqual(opened.context['patient_unread_total'], 0)
        self.assertEqual(self.unread_for(self.josh), (0, 0))
        self.assertEqual(self.unread_for(self.sam), (0, 0))
        message.refresh_from_db()
        self.assertEqual(message.staff_read_by_id, self.sam.pk)

    def test_mark_as_unread_is_for_you_alone_and_works_when_a_colleague_wrote_last(self):
        from care.models import AuditEvent
        from care.services import post_patient_message

        self.add_josh()
        post_patient_message(thread=self.thread, sender=self.josh, body='Agreed.')
        url = reverse('portal:staff-thread-read-state', args=[self.thread.pk])
        self.login(self.sam)
        # Marking unread never reads what is already unread: Nadia's and Josh's messages both stay unread.
        self.client.post(url, {'state': 'unread'})
        self.assertEqual(self.unread_for(self.sam), (2, 2))
        opened = self.client.get(reverse('portal:staff-inbox'), {'thread': self.thread.pk})
        self.assertTrue(opened.context['inbox_thread'].has_others_message)
        self.assertEqual(self.unread_for(self.sam), (0, 0))
        josh_position = MessageThreadParticipant.objects.get(thread=self.thread, user=self.josh).last_read_at
        receipt = self.thread.messages.get(sender=self.nadia_user).staff_read_at
        AuditEvent.objects.filter(action='message.marked_unread').delete()
        self.client.post(url, {'state': 'unread'})
        # Josh's message, the latest from someone else, is unread again for Sam; nothing changes for Josh.
        self.assertEqual(self.unread_for(self.sam), (1, 1))
        self.assertEqual(MessageThreadParticipant.objects.get(thread=self.thread, user=self.josh).last_read_at, josh_position)
        self.assertEqual(self.unread_for(self.josh), (0, 0))
        self.assertEqual(self.thread.messages.get(sender=self.nadia_user).staff_read_at, receipt)
        self.assertTrue(AuditEvent.objects.filter(action='message.marked_unread', actor=self.sam).exists())
        self.client.post(url, {'state': 'read'})
        self.assertEqual(self.unread_for(self.sam), (0, 0))

    def test_mark_as_unread_is_offered_once_someone_else_has_written(self):
        from care.messaging import mark_unread_for_staff, start_conversation
        from care.services import post_patient_message

        from .record_messages import read_state_url

        # Only Sam has written: there is nothing someone else sent to mark unread.
        own = start_conversation(patient=self.nadia, opened_by=self.sam, subject='Your results', body='All in range.')
        row = _inbox_threads_row(self.company, self.sam, own)
        self.assertFalse(row.has_others_message)
        self.assertEqual(read_state_url(row), '')
        self.assertEqual(mark_unread_for_staff(own, self.sam), 0)
        # A colleague's reply is enough, even with no message from the patient.
        add_participant(own, self.josh, added_by=self.sam)
        post_patient_message(thread=own, sender=self.josh, body='Seen, thanks.')
        row = _inbox_threads_row(self.company, self.sam, own)
        self.assertFalse(row.has_patient_message)
        self.assertTrue(row.has_others_message)
        self.assertEqual(read_state_url(row), reverse('portal:staff-thread-read-state', args=[own.pk]))
        self.assertEqual(self.unread_for(self.sam, own), (2, 1))  # With Nadia's unread message in the other one.

    def test_a_clinician_added_to_a_conversation_sees_its_history_as_unread(self):
        from care.services import post_patient_message

        post_patient_message(thread=self.thread, sender=self.sam, body='Yes, up to two days late.')
        self.assertEqual(self.unread_for(self.sam), (0, 0))
        self.add_josh()
        self.assertIsNone(MessageThreadParticipant.objects.get(thread=self.thread, user=self.josh).last_read_at)
        self.assertEqual(self.unread_for(self.josh), (2, 2))
        self.assertEqual(self.unread_for(self.sam), (0, 0))

    def test_replying_reads_the_conversation_for_the_person_replying_only(self):
        self.add_josh()
        self.assertEqual(self.unread_for(self.sam), (1, 1))
        self.login(self.sam)
        self.client.post(reverse('portal:staff-message-create', args=[self.thread.pk]),
                         {'body': 'Yes, up to two days late.', 'return_to': 'record'})
        reply = self.thread.messages.get(sender=self.sam)
        self.assertEqual(MessageThreadParticipant.objects.get(thread=self.thread, user=self.sam).last_read_at, reply.created_at)
        self.assertEqual(self.unread_for(self.sam), (0, 0))
        self.assertEqual(self.unread_for(self.josh), (2, 2))

    def test_a_message_from_a_removed_account_counts_as_someone_else_s(self):
        from care.messaging import mark_read_by_staff, mark_unread_for_staff

        mark_read_by_staff(self.thread, self.sam)
        PatientMessage.objects.create(company=self.company, thread=self.thread, sender=None, body='From an old account.')
        self.assertEqual(self.unread_for(self.sam), (1, 1))
        mark_read_by_staff(self.thread, self.sam)
        self.assertEqual(self.unread_for(self.sam), (0, 0))
        self.assertEqual(mark_unread_for_staff(self.thread, self.sam), 1)
        self.assertEqual(self.unread_for(self.sam), (1, 1))


def _inbox_threads_row(company, user, thread):
    from .inbox import _inbox_threads

    return _inbox_threads(company, user).get(pk=thread.pk)


class ParticipantReadPositionSeedTests(TestCase):
    def test_everyone_starts_where_the_practice_s_read_state_left_off(self):
        company = Company.objects.create(name='Position Practice', slug='position-practice')
        users = get_user_model().objects
        sam = users.create_user(email='position-sam@example.test')
        josh = users.create_user(email='position-josh@example.test')
        for user in (sam, josh):
            CompanyMembership.objects.create(company=company, user=user, role='doctor', clinician_type='doctor')
        patient_user = users.create_user(email='position-patient@example.test')
        patient = Patient.objects.create(company=company, user=patient_user, first_name='Position', last_name='Patient')
        now = timezone.now()

        def message(thread, sender, minutes_ago, **fields):
            created = PatientMessage.objects.create(company=company, thread=thread, sender=sender, body='Seed', **fields)
            PatientMessage.objects.filter(pk=created.pk).update(created_at=now - timedelta(minutes=minutes_ago))
            created.refresh_from_db()
            return created

        # A patient message nobody opened: unread from it on, for everyone in the conversation.
        waiting = MessageThread.objects.create(company=company, patient=patient, subject='Waiting')
        message(waiting, patient_user, 50, staff_read_at=now)
        message(waiting, sam, 40)
        first_unread = message(waiting, patient_user, 30)
        message(waiting, patient_user, 20)
        message(waiting, josh, 10)
        # Everything read: read up to the latest message, whoever sent it.
        read = MessageThread.objects.create(company=company, patient=patient, subject='Read')
        message(read, patient_user, 30, staff_read_at=now)
        latest = message(read, sam, 20)
        empty = MessageThread.objects.create(company=company, patient=patient, subject='Empty')
        for thread in (waiting, read, empty):
            add_participant(thread, sam)
            add_participant(thread, josh)
        migration = importlib.import_module('care.migrations.0019_participant_read_position')
        migration.seed_participant_read_positions(apps, None)

        def position(thread, user):
            return MessageThreadParticipant.objects.get(thread=thread, user=user).last_read_at

        for user in (sam, josh):
            self.assertEqual(position(waiting, user), first_unread.created_at - timedelta(microseconds=1))
            self.assertEqual(position(read, user), latest.created_at)
            self.assertIsNone(position(empty, user))
        from .inbox import _inbox_threads, message_tab_counts

        # What each now sees as unread: the waiting messages, and the colleague's after them.
        self.assertEqual({row.subject: row.unread_count for row in _inbox_threads(company, sam)},
                         {'Waiting': 3, 'Read': 0, 'Empty': 0})
        self.assertEqual(message_tab_counts(company, josh)['patient_unread_total'], 2)


class ScreeningTitleTests(TestCase):
    def test_the_screening_title_is_kept_on_the_patient_record_it_creates(self):
        from care.checkout import _create_patient
        from care.models import Lead, ScreeningQuestionnaire

        company = Company.objects.create(name='Title Practice', slug='title-practice')
        user = get_user_model().objects.create_user(email='title-patient@example.test')
        lead = Lead.objects.create(company=company, first_name='Nadia', last_name='Mokoena', email='title-patient@example.test')
        ScreeningQuestionnaire.objects.create(company=company, lead=lead, answers={'title': 'MS'})
        self.assertEqual(_create_patient(company, lead, user, None).title, 'MS')
