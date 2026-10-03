"""Patients write to their clinicians; conversations stay private to the people in them."""

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

    def test_patient_can_write_to_current_past_and_booked_clinicians_only(self):
        self.assertEqual(recipient_choices(self.patient)[0], self.current)
        self.assertEqual(set(recipient_choices(self.patient)), {self.current, self.past, self.booked})
        self.login_patient()
        page = self.client.get(reverse('portal:patient-messages'), {'compose': 1})
        field = page.context['thread_form'].fields['recipient']
        self.assertEqual(set(field.queryset), {self.current, self.past, self.booked})
        self.assertEqual(page.context['thread_form'].initial.get('recipient', field.initial), self.current.pk)
        self.assertContains(page, 'Cara Current (your clinician)')
        self.assertEqual(self.start(recipient=self.stranger.pk).status_code, 200)
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

    def test_only_participants_see_the_conversation_and_patient_sees_who_joined(self):
        self.login_patient()
        self.start()
        thread = MessageThread.objects.get()
        for user in (self.admin, self.past, self.stranger):
            self.login_staff(user)
            self.assertEqual(self.client.get(reverse('portal:staff-inbox'), {'thread': thread.pk}).status_code, 404)
            overview = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]))
            self.assertEqual([card['count'] for card in overview.context['overview_cards'] if card['label'] == 'Conversations'], [0])
        hand_over(thread=thread, actor=self.current, clinician=self.stranger)
        self.login_staff(self.stranger)
        self.assertEqual(self.client.get(reverse('portal:staff-inbox'), {'thread': thread.pk}).status_code, 200)
        self.login_patient()
        page = self.client.get(reverse('portal:patient-messages'), {'thread': thread.pk})
        self.assertContains(page, 'With Cara Current, Sid Stranger')
        self.assertContains(page, 'Cara Current added Sid Stranger')

    def test_suggesting_a_new_time_opens_a_conversation_with_that_appointments_clinician(self):
        add_participant(MessageThread.objects.create(company=self.company, patient=self.patient, subject='Dose'), self.current)
        self.login_patient()
        appointments = self.client.get(reverse('portal:patient-appointments')).context['appointments']
        booked = next(appointment for appointment in appointments if appointment.clinician_id == self.booked.pk)
        self.assertIn(f'to={self.booked.pk}', booked.proposal_url)
        self.assertEqual(booked.proposal_label, 'Message Bo Booked')

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
