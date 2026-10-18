"""Live refreshes keep tenant authorization and acknowledge only displayed history."""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from care.models import (
    AuditEvent, MessageThread, MessageThreadParticipant, PatientMessage, PracticeSettings,
    TeamMessage, TeamThread, TeamThreadMember,
)
from care.messaging import unread_for_staff
from care.team_messaging import team_threads
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


@override_settings(MULTI_PRACTICE_ENABLED=True)
class RealtimeMessageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.company = Company.objects.create(name='Live Practice', slug='live-practice')
        cls.other = Company.objects.create(name='Other Live Practice', slug='other-live-practice')
        cls.doctor = users.create_user(email='doctor@live.test')
        cls.colleague = users.create_user(email='colleague@live.test')
        cls.admin = users.create_user(email='admin@live.test')
        cls.outsider = users.create_user(email='outsider@live.test')
        cls.patient_user = users.create_user(email='patient@live.test')
        for person in (cls.doctor, cls.colleague, cls.outsider):
            CompanyMembership.objects.create(company=cls.company, user=person, role='doctor')
        CompanyMembership.objects.create(company=cls.company, user=cls.admin, role='super_admin')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Live',
                                             last_name='Patient', assigned_doctor=cls.doctor)
        cls.thread = MessageThread.objects.create(company=cls.company, patient=cls.patient, subject='Live conversation')
        for person in (cls.doctor, cls.colleague):
            MessageThreadParticipant.objects.create(company=cls.company, thread=cls.thread, user=person)
        cls.patient_message = PatientMessage.objects.create(company=cls.company, thread=cls.thread,
                                                            sender=cls.patient_user, body='Patient says hello')
        cls.staff_message = PatientMessage.objects.create(company=cls.company, thread=cls.thread,
                                                          sender=cls.doctor, body='Doctor says hello')
        cls.team = TeamThread.objects.create(company=cls.company, subject='Team conversation',
                                             opened_by=cls.doctor, patient=cls.patient)
        for person in (cls.doctor, cls.colleague):
            TeamThreadMember.objects.create(company=cls.company, thread=cls.team, user=person)
        cls.team_message = TeamMessage.objects.create(company=cls.company, thread=cls.team,
                                                      sender=cls.doctor, body='Team says hello')
        cls.team.last_message_at = cls.team_message.created_at
        cls.team.save(update_fields=['last_message_at'])
        cls.other_patient = Patient.objects.create(company=cls.other, first_name='Other', last_name='Patient')
        cls.other_thread = MessageThread.objects.create(company=cls.other, patient=cls.other_patient, subject='Private elsewhere')
        cls.other_message = PatientMessage.objects.create(company=cls.other, thread=cls.other_thread,
                                                          sender=cls.doctor, body='Other practice')
        PracticeSettings.objects.create(company=cls.company, store_view_log=True)

    def login(self, user, *, patient=False, client=None, company=None):
        client = client or self.client
        client.force_login(user)
        session = client.session
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY if patient else ACTIVE_COMPANY_SESSION_KEY] = (company or self.company).pk
        session.save()

    def seen(self, route, thread, ids, **headers):
        return self.client.post(reverse(f'portal:{route}', args=[thread.pk]), {'message_ids': ids},
                                content_type='application/json', **headers)

    def test_staff_inbox_and_record_snapshots_do_not_mark_read_or_repeat_audits(self):
        self.login(self.doctor)
        for url, query in ((reverse('portal:staff-inbox'), {'thread': self.thread.pk}),
                           (reverse('portal:patient-detail', args=[self.patient.pk]), {'tab': 'messages', 'thread': self.thread.pk})):
            with self.subTest(url=url):
                response = self.client.get(url, query, HTTP_X_MERIDIAN_REALTIME='snapshot')
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Patient says hello')
        self.patient_message.refresh_from_db()
        self.assertIsNone(self.patient_message.staff_read_at)
        self.assertIsNone(self.thread.participant_links.get(user=self.doctor).last_read_at)
        self.assertFalse(AuditEvent.objects.filter(action__in=['message.thread_viewed', 'patient.record_viewed']).exists())
        self.client.get(reverse('portal:staff-inbox'), {'thread': self.thread.pk})
        self.patient_message.refresh_from_db()
        self.assertIsNotNone(self.patient_message.staff_read_at)
        self.assertTrue(AuditEvent.objects.filter(action='message.thread_viewed').exists())

    def test_patient_snapshot_keeps_unread_and_normal_page_keeps_existing_read_behavior(self):
        self.login(self.patient_user, patient=True)
        response = self.client.get(reverse('portal:patient-messages'), {'thread': self.thread.pk},
                                   HTTP_X_MERIDIAN_REALTIME='snapshot')
        self.assertEqual(response.status_code, 200)
        self.staff_message.refresh_from_db()
        self.assertIsNone(self.staff_message.read_at)
        self.assertFalse(AuditEvent.objects.filter(action='message.thread_viewed').exists())
        self.client.get(reverse('portal:patient-messages'), {'thread': self.thread.pk})
        self.staff_message.refresh_from_db()
        self.assertIsNotNone(self.staff_message.read_at)
        self.assertTrue(AuditEvent.objects.filter(action='message.thread_viewed').exists())

    def test_team_snapshot_keeps_unread_and_does_not_log_repeated_record_access(self):
        self.login(self.colleague)
        response = self.client.get(reverse('portal:team-inbox'), {'thread': self.team.pk},
                                   HTTP_X_MERIDIAN_REALTIME='snapshot')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.team.member_links.get(user=self.colleague).last_read_at)
        self.assertFalse(AuditEvent.objects.filter(action='team_thread.viewed').exists())
        self.client.get(reverse('portal:team-inbox'), {'thread': self.team.pk})
        self.assertEqual(self.team.member_links.get(user=self.colleague).last_read_at, self.team_message.created_at)
        self.assertTrue(AuditEvent.objects.filter(action='team_thread.viewed').exists())

    def test_patient_acknowledges_only_own_displayed_incoming_ids(self):
        self.login(self.patient_user, patient=True)
        arriving = PatientMessage.objects.create(company=self.company, thread=self.thread, sender=self.doctor, body='Arrived later')
        response = self.seen('patient-message-seen', self.thread,
                             [self.staff_message.pk, self.patient_message.pk, self.other_message.pk])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['changed'], 1)
        self.assertEqual(response.json()['patient'], 1)
        self.staff_message.refresh_from_db()
        self.assertIsNotNone(self.staff_message.read_at)
        for message in (arriving, self.patient_message, self.other_message):
            message.refresh_from_db()
            self.assertIsNone(message.read_at)
        self.assertEqual(self.seen('patient-message-seen', self.thread, [self.staff_message.pk]).json()['changed'], 0)

    def test_staff_acknowledges_displayed_boundary_not_messages_arriving_after_snapshot(self):
        self.login(self.doctor)
        arriving = PatientMessage.objects.create(company=self.company, thread=self.thread, sender=self.patient_user, body='Later patient message')
        response = self.seen('staff-message-seen', self.thread, [self.patient_message.pk, self.staff_message.pk, self.other_message.pk])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(unread_for_staff(self.company, self.doctor).values_list('pk', flat=True)), [arriving.pk])
        arriving.refresh_from_db()
        self.assertIsNone(arriving.staff_read_at)
        self.assertIsNone(self.thread.participant_links.get(user=self.colleague).last_read_at)

    def test_team_acknowledges_displayed_boundary_without_reading_newer_messages(self):
        self.login(self.colleague)
        arriving = TeamMessage.objects.create(company=self.company, thread=self.team, sender=self.doctor, body='Later team message')
        response = self.seen('team-message-seen', self.team, [self.team_message.pk])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['changed'], 1)
        self.assertEqual(team_threads(self.company, self.colleague).get(pk=self.team.pk).unread_count, 1)
        self.assertLess(self.team.member_links.get(user=self.colleague).last_read_at, arriving.created_at)

    def test_super_admin_read_ack_has_no_participant_or_receipt_side_effect(self):
        self.login(self.admin)
        response = self.seen('staff-message-seen', self.thread, [self.patient_message.pk])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['changed'], 0)
        self.patient_message.refresh_from_db()
        self.assertIsNone(self.patient_message.staff_read_at)
        self.assertFalse(self.thread.participant_links.filter(user=self.admin).exists())
        self.assertEqual(self.seen('team-message-seen', self.team, [self.team_message.pk]).status_code, 404)

    def test_nonparticipants_other_practices_and_other_patient_records_cannot_acknowledge(self):
        self.login(self.outsider)
        self.assertEqual(self.seen('staff-message-seen', self.thread, [self.patient_message.pk]).status_code, 404)
        self.assertEqual(self.seen('team-message-seen', self.team, [self.team_message.pk]).status_code, 404)
        self.login(self.doctor)
        self.assertEqual(self.seen('staff-message-seen', self.other_thread, [self.other_message.pk]).status_code, 404)
        self.login(self.patient_user, patient=True)
        other_patient = Patient.objects.create(company=self.company, first_name='Another', last_name='Person')
        other_thread = MessageThread.objects.create(company=self.company, patient=other_patient, subject='Not yours')
        self.assertEqual(self.seen('patient-message-seen', other_thread, [self.staff_message.pk]).status_code, 404)
        self.assertEqual(self.seen('patient-message-seen', self.other_thread, [self.other_message.pk]).status_code, 404)

    def test_status_requires_correct_access_and_returns_scope_and_durable_counts(self):
        for route in ('staff-message-status', 'patient-message-status'):
            self.assertEqual(self.client.get(reverse(f'portal:{route}')).status_code, 302)
        self.login(self.colleague)
        response = self.client.get(reverse('portal:staff-message-status'))
        self.assertEqual(response.json(), {'scope': f'staff:{self.company.pk}:{self.colleague.pk}',
                                           'patient': 2, 'team': 1, 'total': 3})
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(self.client.get(reverse('portal:patient-message-status')).status_code, 403)
        self.login(self.patient_user, patient=True)
        self.assertEqual(self.client.get(reverse('portal:patient-message-status')).json(),
                         {'scope': f'patient:{self.company.pk}:{self.patient.pk}:{self.patient_user.pk}',
                          'patient': 1, 'team': 0, 'total': 1})
        self.assertEqual(self.client.get(reverse('portal:staff-message-status')).status_code, 403)

    def test_expired_membership_and_changed_scope_stop_live_updates(self):
        self.login(self.doctor)
        self.assertEqual(self.seen('staff-message-seen', self.thread, [self.patient_message.pk],
                                   HTTP_X_MERIDIAN_SCOPE='staff:999:999').status_code, 409)
        self.assertIsNone(self.thread.participant_links.get(user=self.doctor).last_read_at)
        CompanyMembership.objects.filter(company=self.company, user=self.doctor).update(is_active=False)
        self.assertEqual(self.client.get(reverse('portal:staff-message-status')).status_code, 403)
        self.assertEqual(self.seen('staff-message-seen', self.thread, [self.patient_message.pk]).status_code, 403)

    def test_stale_composer_cannot_send_as_another_practice_or_account(self):
        self.login(self.doctor)
        before = PatientMessage.objects.count()
        response = self.client.post(reverse('portal:staff-message-create', args=[self.thread.pk]),
                                     {'body': 'Stale draft'}, HTTP_X_MERIDIAN_SCOPE='staff:999:999')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(PatientMessage.objects.count(), before)
        self.login(self.patient_user, patient=True)
        response = self.client.post(reverse('portal:patient-message-create', args=[self.thread.pk]),
                                     {'body': 'Stale patient draft'}, HTTP_X_MERIDIAN_SCOPE='patient:999:999:999')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(PatientMessage.objects.count(), before)

    def test_ack_requires_post_csrf_and_bounded_valid_ids(self):
        self.login(self.doctor)
        url = reverse('portal:staff-message-seen', args=[self.thread.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        for ids in (None, [True], [-1], ['1.5'], list(range(1, 502))):
            with self.subTest(ids_type=type(ids).__name__):
                self.assertEqual(self.client.post(url, {'message_ids': ids}, content_type='application/json').status_code, 400)
        secured = Client(enforce_csrf_checks=True)
        self.login(self.doctor, client=secured)
        self.assertEqual(secured.post(url, {'message_ids': [self.patient_message.pk]}, content_type='application/json').status_code, 403)
        self.assertIsNone(self.thread.participant_links.get(user=self.doctor).last_read_at)

    def test_patient_older_history_snapshot_preserves_page_and_does_not_read_latest_messages(self):
        self.login(self.patient_user, patient=True)
        PatientMessage.objects.bulk_create([
            PatientMessage(company=self.company, thread=self.thread, sender=self.doctor, body=f'History {number}')
            for number in range(55)
        ])
        response = self.client.get(reverse('portal:patient-messages'), {'thread': self.thread.pk, 'message_page': 2},
                                   HTTP_X_MERIDIAN_REALTIME='snapshot')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['message_page_obj'].number, 2)
        displayed = response.context['selected_thread'].conversation_messages
        self.assertIn(self.staff_message.pk, [message.pk for message in displayed])
        self.assertEqual(PatientMessage.objects.filter(thread=self.thread, read_at__isnull=False).count(), 0)
        response = self.seen('patient-message-seen', self.thread, [message.pk for message in displayed])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['patient'], 50)
