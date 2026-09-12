"""Exercise real portal writes and practice/role boundaries without demo seeds."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from care.models import (
    Appointment,
    AuditEvent,
    ClinicalNote,
    ClinicalTask,
    MessageThread,
    PatientMessage,
)
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


class PortalWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.company = Company.objects.create(name='Alpha Practice', slug='alpha-practice')
        cls.other_company = Company.objects.create(name='Beta Practice', slug='beta-practice')
        cls.doctor = users.create_user(email='doctor@example.test', first_name='Doctor')
        cls.second_doctor = users.create_user(email='second-doctor@example.test', first_name='Second')
        cls.administrator = users.create_user(email='administrator@example.test', first_name='Administrator')
        cls.super_admin = users.create_user(email='super-admin@example.test', first_name='Super')
        cls.patient_user = users.create_user(email='patient@example.test', first_name='Patient')
        cls.other_patient_user = users.create_user(email='other-patient@example.test', first_name='Other')
        for user, role in (
            (cls.doctor, CompanyMembership.Role.DOCTOR),
            (cls.second_doctor, CompanyMembership.Role.DOCTOR),
            (cls.administrator, CompanyMembership.Role.PRACTICE_ADMIN),
            (cls.super_admin, CompanyMembership.Role.SUPER_ADMIN),
        ):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role)
        CompanyMembership.objects.create(
            company=cls.other_company, user=cls.doctor, role=CompanyMembership.Role.DOCTOR,
        )
        cls.patient = Patient.objects.create(
            company=cls.company, user=cls.patient_user,
            first_name='Alice', last_name='Patient', assigned_doctor=cls.doctor,
        )
        cls.other_practice_patient = Patient.objects.create(
            company=cls.other_company, user=cls.patient_user,
            first_name='Alice', last_name='Patient', assigned_doctor=cls.doctor,
        )
        cls.other_patient = Patient.objects.create(
            company=cls.company, user=cls.other_patient_user, first_name='Beth', last_name='Patient',
        )
        cls.thread = MessageThread.objects.create(
            company=cls.company, patient=cls.patient, subject='Care question', opened_by=cls.patient_user,
        )
        cls.other_practice_thread = MessageThread.objects.create(
            company=cls.other_company, patient=cls.other_practice_patient,
            subject='Beta care question', opened_by=cls.patient_user,
        )
        cls.other_patient_thread = MessageThread.objects.create(
            company=cls.company, patient=cls.other_patient,
            subject='Another patient question', opened_by=cls.other_patient_user,
        )
        cls.task = ClinicalTask.objects.create(
            company=cls.company, patient=cls.patient, title='Contact patient', assigned_to=cls.administrator,
        )
        cls.other_practice_task = ClinicalTask.objects.create(
            company=cls.other_company, patient=cls.other_practice_patient, title='Beta task',
        )

    def login(self, user, *, company=None, patient_company=None):
        self.client.force_login(user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = (company or self.company).pk
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = (patient_company or self.company).pk
        session.save()

    def patient_url(self, patient=None):
        return reverse('portal:patient-detail', args=[(patient or self.patient).pk])

    def patient_token(self):
        from .patient_context import make_patient_context

        request = RequestFactory().get('/patient/')
        request.user = self.patient_user
        return make_patient_context(request, self.company, self.patient)

    def add_message(self, *, thread=None, sender=None, body='Message body'):
        thread = thread or self.thread
        return PatientMessage.objects.create(
            company=thread.company, thread=thread, sender=sender or self.patient_user, body=body,
        )

    def test_anonymous_workspaces_redirect_to_login(self):
        for name in ('desktop-dashboard', 'mobile-dashboard', 'patient-dashboard'):
            with self.subTest(workspace=name):
                path = reverse(f'portal:{name}')
                response = self.client.get(path)
                self.assertRedirects(
                    response, f'{reverse("accounts:login")}?next={path}', fetch_redirect_response=False,
                )

    def test_patient_only_account_enters_patient_portal_from_staff_landing(self):
        self.login(self.patient_user)
        for name in ('desktop-dashboard', 'mobile-dashboard'):
            with self.subTest(workspace=name):
                self.assertRedirects(
                    self.client.get(reverse(f'portal:{name}')),
                    reverse('portal:patient-dashboard'), fetch_redirect_response=False,
                )

    def test_patient_cannot_use_staff_record_or_write_endpoints(self):
        self.login(self.patient_user)
        self.assertEqual(self.client.get(self.patient_url()).status_code, 403)
        for route, pk in (
            ('patient-task-create', self.patient.pk),
            ('patient-appointment-create', self.patient.pk),
            ('patient-note-create', self.patient.pk),
            ('task-complete', self.task.pk),
            ('staff-message-create', self.thread.pk),
        ):
            with self.subTest(route=route):
                self.assertEqual(self.client.post(reverse(f'portal:{route}', args=[pk]), {}).status_code, 403)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, ClinicalTask.Status.OPEN)
        self.assertFalse(ClinicalNote.objects.exists())
        self.assertFalse(PatientMessage.objects.exists())

    def test_staff_cannot_address_records_in_non_active_practice(self):
        # This doctor has memberships in both practices; the selected one still bounds every URL.
        self.login(self.doctor)
        self.assertEqual(self.client.get(self.patient_url(self.other_practice_patient)).status_code, 404)
        for route, pk in (
            ('patient-task-create', self.other_practice_patient.pk),
            ('patient-appointment-create', self.other_practice_patient.pk),
            ('patient-note-create', self.other_practice_patient.pk),
            ('task-complete', self.other_practice_task.pk),
            ('staff-message-create', self.other_practice_thread.pk),
        ):
            with self.subTest(route=route):
                self.assertEqual(self.client.post(reverse(f'portal:{route}', args=[pk]), {}).status_code, 404)
        self.assertFalse(AuditEvent.objects.exists())

    def test_record_view_is_audited_without_clinical_content(self):
        self.login(self.doctor)
        self.assertEqual(self.client.get(self.patient_url()).status_code, 200)
        event = AuditEvent.objects.get(action='patient.record_viewed')
        self.assertEqual((event.company_id, event.patient_id, event.actor_id),
                         (self.company.pk, self.patient.pk, self.doctor.pk))
        self.assertEqual(event.target_id, str(self.patient.pk))
        self.assertEqual(event.metadata, {'section': 'overview'})

    def test_only_active_practice_doctor_can_create_clinical_notes(self):
        url = reverse('portal:patient-note-create', args=[self.patient.pk])
        data = {'note_type': ClinicalNote.NoteType.CONSULT, 'body': 'Clinical assessment preserved.'}
        for user in (self.administrator, self.super_admin):
            with self.subTest(user=user.email):
                self.login(user)
                self.assertEqual(self.client.post(url, data).status_code, 403)
        self.assertFalse(ClinicalNote.objects.exists())
        self.login(self.doctor)
        self.assertRedirects(self.client.post(url, data), self.patient_url() + '?tab=notes', fetch_redirect_response=False)
        note = ClinicalNote.objects.get()
        self.assertEqual((note.company_id, note.patient_id, note.author_id),
                         (self.company.pk, self.patient.pk, self.doctor.pk))
        self.assertTrue(AuditEvent.objects.filter(action='clinical_note.created', target_id=str(note.pk)).exists())

    def test_private_notes_are_author_only_and_administrators_do_not_see_note_bodies(self):
        private_body = 'Private clinical observation for the author only.'
        shared_body = 'Shared clinical assessment for permitted clinical readers.'
        ClinicalNote.objects.create(
            company=self.company, patient=self.patient, author=self.doctor, body=private_body, is_private=True,
        )
        ClinicalNote.objects.create(
            company=self.company, patient=self.patient, author=self.doctor, body=shared_body,
        )
        for user in (self.doctor, self.second_doctor, self.super_admin, self.administrator):
            with self.subTest(user=user.email):
                self.login(user)
                response = self.client.get(self.patient_url(), {'tab': 'overview' if user == self.administrator else 'notes'})
                self.assertEqual(response.status_code, 200)
                if user == self.doctor:
                    self.assertContains(response, private_body)
                else:
                    self.assertNotContains(response, private_body)
                if user == self.administrator:
                    self.assertNotContains(response, shared_body)
                    self.assertEqual(self.client.get(self.patient_url(), {'tab': 'notes'}).status_code, 403)
                else:
                    self.assertContains(response, shared_body)

    def test_administrator_can_create_task_in_active_practice(self):
        self.login(self.administrator)
        response = self.client.post(reverse('portal:patient-task-create', args=[self.patient.pk]), {
            'patient': self.patient.pk, 'company': self.other_company.pk,
            'title': 'Arrange follow-up', 'description': 'Contact about appointment availability.',
            'assigned_to': self.administrator.pk, 'priority': ClinicalTask.Priority.NORMAL,
        })
        self.assertRedirects(response, self.patient_url() + '?tab=tasks', fetch_redirect_response=False)
        task = ClinicalTask.objects.get(title='Arrange follow-up')
        self.assertEqual((task.company_id, task.patient_id), (self.company.pk, self.patient.pk))

    def test_task_form_rejects_different_patient_and_preserves_input(self):
        self.login(self.administrator)
        count = ClinicalTask.objects.count()
        response = self.client.post(reverse('portal:patient-task-create', args=[self.patient.pk]), {
            'patient': self.other_patient.pk, 'title': 'Do not silently change the patient',
            'description': 'Retained description', 'priority': ClinicalTask.Priority.NORMAL,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ClinicalTask.objects.count(), count)
        self.assertTrue(response.context['task_form'].errors)
        self.assertContains(response, 'Retained description')

    def test_invalid_task_form_preserves_errors_and_entered_description(self):
        self.login(self.administrator)
        response = self.client.post(reverse('portal:patient-task-create', args=[self.patient.pk]), {
            'patient': self.patient.pk, 'title': '', 'description': 'Keep this task draft.',
            'priority': ClinicalTask.Priority.HIGH,
        })
        self.assertEqual(response.status_code, 200)
        form = response.context['task_form']
        self.assertIn('title', form.errors)
        self.assertEqual(form['priority'].value(), ClinicalTask.Priority.HIGH)
        self.assertContains(response, 'Keep this task draft.')

    def test_administrator_can_book_appointment_with_practice_doctor(self):
        self.login(self.administrator)
        token = self.client.get(self.patient_url()).context['legacy_appointment_context']
        response = self.client.post(reverse('portal:patient-appointment-create', args=[self.patient.pk]), {
            'workflow_context': token,
            'patient': self.patient.pk, 'clinician': self.doctor.pk,
            'appointment_type': Appointment.Type.FOLLOW_UP,
            'starts_at': (timezone.now() + timedelta(days=3)).isoformat(), 'duration_minutes': 30,
            'video_link': 'https://example.test/consult',
        })
        self.assertRedirects(response, self.patient_url() + '?tab=appointments', fetch_redirect_response=False)
        appointment = Appointment.objects.get()
        self.assertEqual((appointment.company_id, appointment.patient_id, appointment.clinician_id),
                         (self.company.pk, self.patient.pk, self.doctor.pk))

    def test_invalid_appointment_form_preserves_input_and_rejects_non_doctor(self):
        self.login(self.administrator)
        token = self.client.get(self.patient_url()).context['legacy_appointment_context']
        response = self.client.post(reverse('portal:patient-appointment-create', args=[self.patient.pk]), {
            'workflow_context': token,
            'patient': self.patient.pk, 'clinician': self.administrator.pk,
            'appointment_type': Appointment.Type.FOLLOW_UP,
            'starts_at': (timezone.now() + timedelta(days=3)).isoformat(), 'duration_minutes': 30,
            'video_link': 'https://example.test/keep-this-draft',
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('clinician', response.context['appointment_form'].errors)
        self.assertContains(response, 'https://example.test/keep-this-draft')
        self.assertFalse(Appointment.objects.exists())

    def test_invalid_clinical_note_preserves_draft(self):
        self.login(self.doctor)
        response = self.client.post(reverse('portal:patient-note-create', args=[self.patient.pk]), {
            'note_type': 'unknown-type', 'body': 'Do not lose this clinical draft.', 'is_private': 'on',
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('note_type', response.context['note_form'].errors)
        self.assertContains(response, 'Do not lose this clinical draft.')
        self.assertFalse(ClinicalNote.objects.exists())

    def test_task_completion_is_idempotent(self):
        self.login(self.administrator)
        url = reverse('portal:task-complete', args=[self.task.pk])
        self.assertEqual(self.client.post(url).status_code, 302)
        self.task.refresh_from_db()
        completed_at = self.task.completed_at
        self.assertEqual(self.task.status, ClinicalTask.Status.DONE)
        self.assertIsNotNone(completed_at)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.completed_at, completed_at)
        self.assertEqual(AuditEvent.objects.filter(action='task.completed', target_id=str(self.task.pk)).count(), 1)

    def test_cancelled_task_cannot_be_completed(self):
        self.task.status = ClinicalTask.Status.CANCELLED
        self.task.save(update_fields=['status'])
        self.login(self.administrator)
        response = self.client.post(reverse('portal:task-complete', args=[self.task.pk]), follow=True)
        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, ClinicalTask.Status.CANCELLED)
        self.assertIsNone(self.task.completed_at)
        self.assertFalse(AuditEvent.objects.filter(action='task.completed', target_id=str(self.task.pk)).exists())
        self.assertTrue(any(message.level_tag == 'error' for message in response.context['messages']))

    def test_patient_can_start_additional_thread_and_reply_to_owned_thread(self):
        self.login(self.patient_user)
        response = self.client.post(reverse('portal:patient-thread-create'), {
            'patient_context': self.patient_token(),
            'subject': 'An additional question', 'body': 'My new question.',
            'company': self.other_company.pk, 'patient': self.other_patient.pk,
        })
        self.assertEqual(response.status_code, 302)
        new_thread = MessageThread.objects.get(subject='An additional question')
        self.assertEqual((new_thread.company_id, new_thread.patient_id, new_thread.opened_by_id),
                         (self.company.pk, self.patient.pk, self.patient_user.pk))
        self.assertEqual(new_thread.messages.get().body, 'My new question.')
        self.assertEqual(self.client.post(reverse('portal:patient-message-create', args=[self.thread.pk]), {
            'body': 'A reply to my earlier conversation.',
        }).status_code, 302)
        self.assertTrue(self.thread.messages.filter(body='A reply to my earlier conversation.').exists())

    def test_patient_cannot_reply_outside_owned_active_practice_or_to_closed_thread(self):
        self.login(self.patient_user)
        closed_thread = MessageThread.objects.create(
            company=self.company, patient=self.patient, subject='Closed conversation', is_closed=True,
        )
        for thread in (self.other_practice_thread, self.other_patient_thread, closed_thread):
            with self.subTest(thread=thread.subject):
                response = self.client.post(reverse('portal:patient-message-create', args=[thread.pk]), {'body': 'No access'})
                self.assertEqual(response.status_code, 404)
        self.assertFalse(PatientMessage.objects.exists())

    def test_patient_inbox_shows_full_conversation_and_new_thread_form(self):
        long_body = 'This secure message is deliberately longer than a card preview. ' * 5 + 'Important final sentence.'
        self.add_message(sender=self.doctor, body=long_body)
        for number in range(7):
            self.add_message(body=f'Later message {number}')
        self.login(self.patient_user)
        response = self.client.get(reverse('portal:patient-messages'), {'thread': self.thread.pk})
        self.assertContains(response, long_body)
        self.assertContains(response, 'Later message 6')
        self.assertContains(response, f'action="{reverse("portal:patient-thread-create")}"')
        self.assertContains(response, f'action="{reverse("portal:patient-message-create", args=[self.thread.pk])}"')

    def test_invalid_patient_thread_preserves_body_and_field_errors(self):
        self.login(self.patient_user)
        count = MessageThread.objects.count()
        response = self.client.post(reverse('portal:patient-thread-create'), {
            'patient_context': self.patient_token(),
            'subject': '', 'body': 'Please keep this unsent question.',
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('subject', response.context['thread_form'].errors)
        self.assertContains(response, 'Please keep this unsent question.')
        self.assertEqual(MessageThread.objects.count(), count)
        self.assertFalse(PatientMessage.objects.exists())

    def test_invalid_patient_reply_preserves_input(self):
        self.login(self.patient_user)
        draft = 'x' * 5001
        response = self.client.post(reverse('portal:patient-message-create', args=[self.thread.pk]), {'body': draft})
        self.assertEqual(response.status_code, 200)
        self.assertIn('body', response.context['message_form'].errors)
        self.assertEqual(response.context['message_form']['body'].value(), draft)
        self.assertFalse(PatientMessage.objects.exists())

    def test_administrator_can_send_message_and_invalid_draft_is_preserved(self):
        self.login(self.administrator)
        url = reverse('portal:staff-message-create', args=[self.thread.pk])
        self.assertRedirects(
            self.client.post(url, {'body': 'Your appointment details are ready.'}),
            self.patient_url() + f'?tab=messages&thread={self.thread.pk}', fetch_redirect_response=False,
        )
        message = PatientMessage.objects.get()
        self.assertEqual((message.company_id, message.sender_id), (self.company.pk, self.administrator.pk))
        draft = 'y' * 5001
        response = self.client.post(url, {'body': draft})
        self.assertEqual(response.status_code, 200)
        self.assertIn('body', response.context['message_form'].errors)
        self.assertEqual(response.context['message_form']['body'].value(), draft)
        self.assertEqual(PatientMessage.objects.count(), 1)

    def test_shared_staff_inbox_counts_only_unread_patient_origin_messages(self):
        incoming = self.add_message(body='Patient question to team')
        self.add_message(sender=self.administrator, body='Administrative reply')
        other_practice = self.add_message(thread=self.other_practice_thread, body='Beta message')
        for user in (self.doctor, self.second_doctor):
            with self.subTest(user=user.email):
                self.login(user)
                response = self.client.get(reverse('portal:desktop-dashboard'))
                self.assertEqual(response.context['metrics']['unread_messages'], 1)
        self.client.get(self.patient_url(), {'tab': 'messages', 'thread': self.thread.pk})
        incoming.refresh_from_db()
        other_practice.refresh_from_db()
        self.assertIsNotNone(incoming.read_at)
        self.assertIsNone(other_practice.read_at)
        self.login(self.doctor)
        response = self.client.get(reverse('portal:desktop-dashboard'))
        self.assertEqual(response.context['metrics']['unread_messages'], 0)

    def test_staff_read_markers_touch_only_incoming_messages_in_viewed_record(self):
        incoming = self.add_message()
        outbound = self.add_message(sender=self.doctor)
        another_patient = self.add_message(thread=self.other_patient_thread, sender=self.other_patient_user)
        self.login(self.doctor)
        self.client.get(self.patient_url(), {'tab': 'messages', 'thread': self.thread.pk})
        for message in (incoming, outbound, another_patient):
            message.refresh_from_db()
        self.assertIsNotNone(incoming.read_at)
        self.assertIsNone(outbound.read_at)
        self.assertIsNone(another_patient.read_at)

    def test_patient_read_markers_touch_only_team_messages_in_active_practice(self):
        incoming = self.add_message(sender=self.doctor)
        own = self.add_message()
        other_practice = self.add_message(thread=self.other_practice_thread, sender=self.doctor)
        self.login(self.patient_user)
        self.client.get(reverse('portal:patient-messages'), {'thread': self.thread.pk})
        for message in (incoming, own, other_practice):
            message.refresh_from_db()
        self.assertIsNotNone(incoming.read_at)
        self.assertIsNone(own.read_at)
        self.assertIsNone(other_practice.read_at)

    def test_mixed_staff_patient_account_uses_patient_practice_switcher_on_patient_page(self):
        CompanyMembership.objects.create(
            company=self.company, user=self.patient_user, role=CompanyMembership.Role.DOCTOR,
        )
        self.login(self.patient_user, patient_company=self.other_company)
        response = self.client.get(reverse('portal:patient-dashboard'))
        self.assertContains(response, 'id="patient-company-select"')
        self.assertNotContains(response, 'id="company-select"')
        self.assertContains(response, reverse('portal:activate-patient-company', args=[self.other_company.slug]))

    def test_switch_practice_from_patient_record_returns_to_overview(self):
        self.login(self.doctor)
        response = self.client.post(
            reverse('portal:activate-company', args=[self.other_company.slug]),
            {'next': self.patient_url()},
        )
        self.assertRedirects(response, reverse('portal:desktop-dashboard'), fetch_redirect_response=False)
        self.assertEqual(self.client.session[ACTIVE_COMPANY_SESSION_KEY], self.other_company.pk)
