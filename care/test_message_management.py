"""Only people in a conversation manage it; adding a clinician never answers the patient for them."""

from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError

from practices.models import CompanyMembership

from .message_management import manage_conversation
from .messaging import add_participant
from .models import AuditEvent, ClinicalTask, MessageThread, MessageThreadParticipant, PatientMessage
from .services import post_patient_message
from .test_appointment_lifecycle import AppointmentLifecycleFixture


class MessageManagementTests(AppointmentLifecycleFixture):
    def manage(self, **overrides):
        values = dict(thread=self.thread, actor=self.doctor, action='handover',
            expected_updated=self.thread.updated_at.isoformat(), doctor=self.colleague, confirm=True)
        values.update(overrides)
        return manage_conversation(**values)

    def test_handover_adds_the_clinician_to_the_whole_conversation(self):
        post_patient_message(thread=self.thread, sender=self.patient_user, body='Earlier question')
        self.thread.refresh_from_db()
        self.manage()
        link = MessageThreadParticipant.objects.get(thread=self.thread, user=self.colleague)
        self.assertEqual(link.added_by, self.doctor)
        self.assertEqual(set(self.thread.participants.all()), {self.doctor, self.colleague})
        # The new clinician can now reply; nothing was sent to the patient on anyone's behalf.
        post_patient_message(thread=self.thread, sender=self.colleague, body='I can help with this.')
        self.assertEqual(AuditEvent.objects.get(action='message.participant_added').metadata, {'user_id': self.colleague.pk})
        self.assertEqual(PatientMessage.objects.count(), 2)
        self.assertFalse(ClinicalTask.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_only_participants_manage_and_administrators_have_no_access(self):
        for actor in (self.admin, self.super_admin, self.colleague, self.patient_user):
            with self.subTest(actor=actor.email), self.assertRaises(PermissionDenied):
                self.manage(actor=actor, doctor=self.colleague if actor != self.colleague else self.doctor)
        for actor in (self.admin, self.super_admin):
            with self.subTest(writer=actor.email), self.assertRaises(PermissionDenied):
                post_patient_message(thread=self.thread, sender=actor, body='Not my conversation')
        self.assertFalse(MessageThreadParticipant.objects.filter(user__in=(self.admin, self.super_admin)).exists())

    def test_only_active_clinicians_can_be_added_and_not_twice(self):
        for clinician in (self.admin, self.patient_user, None):
            with self.subTest(clinician=getattr(clinician, 'email', None)), self.assertRaises(ValidationError):
                self.manage(doctor=clinician)
        with self.assertRaisesMessage(ValidationError, 'already in this conversation'):
            self.manage(doctor=self.doctor)
        CompanyMembership.objects.filter(user=self.colleague, company=self.company).update(is_active=False)
        with self.assertRaises(ValidationError):
            self.manage()
        self.assertEqual(list(self.thread.participants.all()), [self.doctor])

    def test_inactive_patient_or_actor_denied_despite_cached_instances(self):
        for obj in (self.patient, self.doctor, self.company):
            with self.subTest(model=type(obj).__name__):
                type(obj).objects.filter(pk=obj.pk).update(is_active=False)
                with self.assertRaises(PermissionDenied):
                    self.manage()
                type(obj).objects.filter(pk=obj.pk).update(is_active=True)
        self.assertEqual(list(self.thread.participants.all()), [self.doctor])

    def test_invalid_action_or_confirmation_writes_nothing(self):
        for changes in (dict(action='invented'), dict(action='escalate'), dict(confirm=False), dict(confirm='yes')):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.manage(**changes)
        self.assertFalse(AuditEvent.objects.exists())

    def test_close_blocks_pending_proposal_and_preserves_booking(self):
        appointment = self.appointment()
        proposal = self.proposal(appointment)
        self.thread.refresh_from_db()
        with self.assertRaisesMessage(ValidationError, 'pending appointment suggestions'):
            self.manage(action='close')
        self.thread.refresh_from_db()
        proposal.refresh_from_db()
        appointment.refresh_from_db()
        self.assertFalse(self.thread.is_closed)
        self.assertEqual(proposal.status, 'pending')
        self.assertEqual(appointment.status, 'booked')

    def test_close_and_reopen_preserve_messages_and_do_not_change_appointments(self):
        appointment = self.appointment()
        message = post_patient_message(thread=self.thread, sender=self.patient_user, body='Original question')
        self.thread.refresh_from_db()
        closed = self.manage(action='close')
        self.assertTrue(closed.is_closed)
        with self.assertRaises(ValidationError):
            post_patient_message(thread=closed, sender=self.patient_user, body='Must not append')
        with self.assertRaises(ValidationError):
            self.manage(thread=closed, expected_updated=closed.updated_at.isoformat())
        reopened = self.manage(thread=closed, expected_updated=closed.updated_at.isoformat(), action='reopen')
        self.assertFalse(reopened.is_closed)
        self.assertEqual(PatientMessage.objects.get().pk, message.pk)
        appointment.refresh_from_db()
        self.assertEqual(appointment.starts_at, self.starts_at)
        self.assertEqual(appointment.status, 'booked')
        self.assertEqual(AuditEvent.objects.filter(action='message.thread_closed').count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action='message.thread_reopened').count(), 1)

    def test_new_message_makes_old_management_form_stale(self):
        post_patient_message(thread=self.thread, sender=self.patient_user, body='A newer question')
        with self.assertRaisesMessage(ValidationError, 'conversation changed'):
            self.manage(action='close')
        self.assertFalse(MessageThread.objects.get(pk=self.thread.pk).is_closed)

    def test_other_practice_pending_proposal_does_not_block_unrelated_thread(self):
        self.proposal(self.appointment())
        self.assertTrue(self.manage(thread=self.beta_thread, expected_updated=self.beta_thread.updated_at.isoformat(),
                                    action='close').is_closed)

    def test_reassignment_leaves_conversations_with_the_previous_clinician(self):
        from .patient_assignment import assign_doctor

        self.patient.refresh_from_db()
        assign_doctor(patient=self.patient, actor=self.admin, doctor=self.colleague, expected_updated=self.patient.updated_at.isoformat())
        self.assertEqual(list(self.thread.participants.all()), [self.doctor])
        self.assertFalse(MessageThread.objects.get(pk=self.thread.pk).is_closed)

    def test_unattended_conversations_go_to_the_next_assigned_clinician(self):
        from .patient_assignment import assign_doctor

        orphan = MessageThread.objects.create(company=self.company, patient=self.patient, subject='Before participants existed')
        self.patient.refresh_from_db()
        assign_doctor(patient=self.patient, actor=self.admin, doctor=self.colleague, expected_updated=self.patient.updated_at.isoformat())
        self.assertEqual(list(orphan.participants.all()), [self.colleague])
        add_participant(orphan, self.doctor)
        self.assertEqual(orphan.participants.count(), 2)
