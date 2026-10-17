"""Patients write to their clinicians; conversations stay private to the people in them and the practice's Super Admins."""

from datetime import timedelta
from importlib import import_module

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.messaging import add_participant, hand_over, recipient_choices
from care.models import (
    Appointment, AuditEvent, ClinicianAssignment, MessageThread, MessageThreadParticipant, PatientMessage,
)
from care.patient_assignment import assign_doctor
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY
from .patient_context import make_patient_context


@override_settings(MULTI_PRACTICE_ENABLED=True)
class PrivateConversationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.company = Company.objects.create(name='Private Messages', slug='private-messages')
        cls.current = User.objects.create_user(email='current@private.test', first_name='Cara', last_name='Current')
        cls.past = User.objects.create_user(email='past@private.test', first_name='Pat', last_name='Past')
        cls.booked = User.objects.create_user(email='booked@private.test', first_name='Bo', last_name='Booked')
        cls.stranger = User.objects.create_user(email='stranger@private.test', first_name='Sid', last_name='Stranger')
        cls.admin = User.objects.create_user(email='admin@private.test', first_name='Ada', last_name='Admin')
        for user in (cls.current, cls.past, cls.booked, cls.stranger):
            CompanyMembership.objects.create(company=cls.company, user=user, role='doctor')
        CompanyMembership.objects.create(company=cls.company, user=cls.admin, role='super_admin')
        cls.patient_user = User.objects.create_user(email='patient@private.test', first_name='Nadia', last_name='Patient')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Nadia', last_name='Patient')
        for clinician in (cls.past, cls.current):
            cls.patient.refresh_from_db()
            assign_doctor(patient=cls.patient, actor=cls.admin, doctor=clinician, expected_updated=cls.patient.updated_at.isoformat())
        cls.patient.refresh_from_db()
        Appointment.objects.create(company=cls.company, patient=cls.patient, clinician=cls.booked,
                                   starts_at=timezone.now() + timedelta(days=3), duration_minutes=15)

    def login_patient(self):
        self.client.force_login(self.patient_user)
        session = self.client.session
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def login_staff(self, user):
        self.client.force_login(user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def patient_token(self):
        request = RequestFactory().get('/patient/')
        request.user = self.patient_user
        return make_patient_context(request, self.company, self.patient)

    def start(self, **data):
        return self.client.post(reverse('portal:patient-thread-create'), {
            'patient_context': self.patient_token(), 'subject': 'A question', 'body': 'Hello', **data})

    def test_history_keeps_every_clinician_and_marks_the_current_one(self):
        rows = ClinicianAssignment.objects.filter(patient=self.patient).order_by('created_at', 'pk')
        self.assertEqual([(row.clinician, row.ended_at is None) for row in rows], [(self.past, False), (self.current, True)])

    def test_patient_can_write_to_current_and_past_clinicians_only(self):
        # Being booked with a clinician doesn't make them a choice; only an assignment, now or before, does.
        self.assertEqual(recipient_choices(self.patient)[0], self.current)
        self.assertEqual(set(recipient_choices(self.patient)), {self.current, self.past})
        self.login_patient()
        page = self.client.get(reverse('portal:patient-messages'), {'compose': 1})
        field = page.context['thread_form'].fields['recipient']
        self.assertEqual(set(field.queryset), {self.current, self.past})
        self.assertEqual(page.context['thread_form'].initial.get('recipient', field.initial), self.current.pk)
        self.assertContains(page, 'Cara Current (your clinician)')
        self.assertNotContains(page, 'Bo Booked')
        self.assertNotContains(page, 'Sid Stranger')
        for clinician in (self.stranger, self.booked):
            self.assertEqual(self.start(recipient=clinician.pk).status_code, 200)
        self.assertFalse(MessageThread.objects.exists())
        self.assertEqual(self.start(recipient=self.past.pk).status_code, 302)
        thread = MessageThread.objects.get()
        self.assertEqual(list(thread.participants.all()), [self.past])
        self.assertEqual(thread.messages.get().body, 'Hello')

    def test_new_message_defaults_to_the_assigned_clinician(self):
        self.login_patient()
        self.assertEqual(self.start().status_code, 302)
        self.assertEqual(list(MessageThread.objects.get().participants.all()), [self.current])

    def test_patient_without_a_clinician_is_told_and_cannot_start(self):
        other = Patient.objects.create(company=self.company, user=get_user_model().objects.create_user(email='new@private.test'),
                                       first_name='New', last_name='Patient')
        self.assertEqual(recipient_choices(other), [])
        self.client.force_login(other.user)
        session = self.client.session
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = self.company.pk
        session.save()
        page = self.client.get(reverse('portal:patient-messages'), {'compose': 1})
        self.assertContains(page, 'once Private Messages assigns one to you')
        self.assertNotContains(page, 'action="%s"' % reverse('portal:patient-thread-create'))

    def test_patient_can_also_write_to_the_practice_administrators(self):
        User = get_user_model()
        practice_admin = User.objects.create_user(email='paula@private.test', first_name='Paula', last_name='Practice')
        CompanyMembership.objects.create(company=self.company, user=practice_admin, role='practice_admin')
        # Not offered: an administrator whose access has ended, or one at another practice.
        former = User.objects.create_user(email='former@private.test', first_name='Fay', last_name='Former')
        CompanyMembership.objects.create(company=self.company, user=former, role='practice_admin', is_active=False)
        elsewhere = Company.objects.create(name='Elsewhere', slug='elsewhere')
        CompanyMembership.objects.create(company=elsewhere, user=User.objects.create_user(email='else@private.test'),
                                         role='practice_admin')
        self.assertEqual(recipient_choices(self.patient), [self.current, self.past, practice_admin])
        self.login_patient()
        page = self.client.get(reverse('portal:patient-messages'), {'compose': 1})
        field = page.context['thread_form'].fields['recipient']
        self.assertEqual(list(field.queryset), [self.current, self.past, practice_admin])
        self.assertContains(page, 'Paula Practice (practice administrator)')
        self.assertNotContains(page, 'Fay Former')
        self.assertEqual(self.start(recipient=practice_admin.pk).status_code, 302)
        thread = MessageThread.objects.get()
        self.assertEqual(list(thread.participants.all()), [practice_admin])
        # The administrator finds it in Patient messages and can reply.
        self.login_staff(practice_admin)
        self.assertContains(self.client.get(reverse('portal:staff-inbox'), {'thread': thread.pk}), 'A question')
        reply = self.client.post(reverse('portal:staff-message-create', args=[thread.pk]),
                                 {'body': 'Happy to help', 'return_to': 'patient_messages'})
        self.assertEqual(reply.status_code, 302)
        self.assertTrue(thread.messages.filter(sender=practice_admin, body='Happy to help').exists())

    def test_super_admins_are_offered_only_as_a_clinician_assigned_to_the_patient(self):
        # self.admin is a Super Admin who isn't a clinician: never offered.
        self.assertNotIn(self.admin, recipient_choices(self.patient))
        doctor_admin = get_user_model().objects.create_user(email='doc-admin@private.test', first_name='Dot', last_name='Admin')
        CompanyMembership.objects.create(company=self.company, user=doctor_admin, role='super_admin', clinician_type='doctor')
        self.assertNotIn(doctor_admin, recipient_choices(self.patient))
        self.patient.refresh_from_db()
        assign_doctor(patient=self.patient, actor=self.admin, doctor=doctor_admin, expected_updated=self.patient.updated_at.isoformat())
        self.patient.refresh_from_db()
        self.assertEqual(recipient_choices(self.patient)[0], doctor_admin)
        self.assertEqual(recipient_choices(self.patient)[0].recipient_note, 'your clinician')

    def test_patient_without_a_clinician_can_write_to_a_practice_administrator(self):
        practice_admin = get_user_model().objects.create_user(email='paula@private.test', first_name='Paula', last_name='Practice')
        CompanyMembership.objects.create(company=self.company, user=practice_admin, role='practice_admin')
        other = Patient.objects.create(company=self.company, user=get_user_model().objects.create_user(email='new@private.test'),
                                       first_name='New', last_name='Patient')
        self.assertEqual(recipient_choices(other), [practice_admin])
        self.client.force_login(other.user)
        session = self.client.session
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = self.company.pk
        session.save()
        page = self.client.get(reverse('portal:patient-messages'), {'compose': 1})
        self.assertContains(page, 'action="%s"' % reverse('portal:patient-thread-create'))
        request = RequestFactory().get('/patient/')
        request.user = other.user
        response = self.client.post(reverse('portal:patient-thread-create'), {
            'patient_context': make_patient_context(request, self.company, other), 'subject': 'Getting started', 'body': 'Hello'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(MessageThread.objects.get().participants.all()), [practice_admin])

    def test_only_participants_and_super_admins_see_the_conversation_and_patient_sees_who_joined(self):
        practice_admin = get_user_model().objects.create_user(email='paula@private.test', first_name='Paula', last_name='Practice')
        CompanyMembership.objects.create(company=self.company, user=practice_admin, role='practice_admin')
        self.login_patient()
        self.start()
        thread = MessageThread.objects.get()
        for user in (practice_admin, self.past, self.stranger):
            self.login_staff(user)
            self.assertEqual(self.client.get(reverse('portal:staff-inbox'), {'thread': thread.pk}).status_code, 404)
            overview = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]))
            if user == self.stranger:
                # Never Nadia's clinician: her record doesn't open for them at all.
                self.assertRedirects(overview, reverse('portal:patient-list'), fetch_redirect_response=False)
                continue
            # No one's Overview lists conversations.
            self.assertNotContains(overview, thread.subject)
        # A Super Admin (self.admin) reads it, in Patient messages and on Nadia's record, without joining it.
        self.login_staff(self.admin)
        self.assertEqual(self.client.get(reverse('portal:staff-inbox'), {'thread': thread.pk}).context['inbox_thread'].pk, thread.pk)
        record = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]), {'tab': 'messages', 'thread': thread.pk})
        self.assertEqual(record.context['record_thread'].pk, thread.pk)
        self.assertContains(record, 'You’re not in this conversation. Replying adds you to it.')
        self.assertEqual(list(thread.participants.all()), [self.current])
        hand_over(thread=thread, actor=self.current, clinician=self.stranger)
        self.login_staff(self.stranger)
        # A conversation link opens it in Patient messages, now that the stranger takes part.
        opened = self.client.get(reverse('portal:staff-inbox'), {'thread': thread.pk})
        self.assertEqual(opened.status_code, 200)
        self.assertEqual(opened.context['inbox_thread'].pk, thread.pk)
        # They read and reply in the conversation, but never Nadia's record or notes: they weren't her clinician.
        self.assertIsNone(opened.context['conversation_notes'])
        self.assertEqual(opened.context['inbox_record_url'], '')
        self.assertNotContains(opened, 'Patient notes')
        self.login_patient()
        page = self.client.get(reverse('portal:patient-messages'), {'thread': thread.pk})
        self.assertContains(page, 'With Cara Current, Sid Stranger')
        self.assertContains(page, 'Cara Current added Sid Stranger')

    def test_appointment_rows_leave_a_new_time_to_the_clinician(self):
        """A patient asks for a different time in Messages; there's no button on My Appointments to suggest one."""
        Appointment.objects.create(company=self.company, patient=self.patient, clinician=self.past,
                                   starts_at=timezone.now() + timedelta(days=4), duration_minutes=15)
        add_participant(MessageThread.objects.create(company=self.company, patient=self.patient, subject='Dose'), self.current)
        self.login_patient()
        page = self.client.get(reverse('portal:patient-appointments'))
        for text in ('Suggest New Time', 'Ask to Rebook', 'Message Pat Past', 'Message Cara Current', 'Contact Care Team',
                     'Need a different time?'):
            self.assertNotContains(page, text)
        self.assertContains(page, 'View Appointment')

    def test_migration_gives_existing_conversations_their_people(self):
        replied = MessageThread.objects.create(company=self.company, patient=self.patient, subject='Replied', opened_by=self.patient_user)
        PatientMessage.objects.create(company=self.company, thread=replied, sender=self.patient_user, body='Question')
        PatientMessage.objects.create(company=self.company, thread=replied, sender=self.past, body='Answer')
        unanswered = MessageThread.objects.create(company=self.company, patient=self.patient, subject='Unanswered', opened_by=self.patient_user)
        staff_opened = MessageThread.objects.create(company=self.company, patient=self.patient, subject='Staff', opened_by=self.admin)
        ClinicianAssignment.objects.all().delete()
        import_module('care.migrations.0011_message_participants_and_clinician_history').backfill(apps, None)
        self.assertEqual(list(replied.participants.all()), [self.past])
        self.assertEqual(list(unanswered.participants.all()), [self.current])
        self.assertEqual(list(staff_opened.participants.all()), [self.admin])
        self.assertEqual(ClinicianAssignment.objects.get(patient=self.patient, ended_at__isnull=True).clinician, self.current)
        self.assertTrue(ClinicianAssignment.objects.filter(patient=self.patient, clinician=self.past, ended_at__isnull=False).exists())
        self.assertEqual(MessageThreadParticipant.objects.filter(thread=replied).count(), 1)
        self.assertFalse(AuditEvent.objects.filter(action='message.participant_added').exists())

    def test_each_conversation_shows_its_unread_count_as_a_badge(self):
        thread = MessageThread.objects.create(company=self.company, patient=self.patient, subject='Dose question')
        add_participant(thread, self.current)
        for number in range(3):
            PatientMessage.objects.create(company=self.company, thread=thread, sender=self.current, body=f'Reply {number}')
        self.login_patient()
        badge = '<span class="patient-inbox-thread__badge" aria-hidden="true">{}</span>'
        self.assertContains(self.client.get(reverse('portal:patient-messages')), badge.format(3), html=True)
        for number in range(8):
            PatientMessage.objects.create(company=self.company, thread=thread, sender=self.current, body=f'More {number}')
        self.assertContains(self.client.get(reverse('portal:patient-messages')), badge.format('9+'), html=True)
        page = self.client.get(reverse('portal:patient-messages'), {'thread': thread.pk})
        self.assertNotContains(page, 'patient-inbox-thread__badge')
