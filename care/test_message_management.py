"""Conversation changes never silently answer a clinical question."""

from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError

from practices.models import CompanyMembership

from .message_management import manage_conversation
from .models import AuditEvent, ClinicalTask, MessageThread, PatientMessage
from .services import post_patient_message
from .test_appointment_lifecycle import AppointmentLifecycleFixture


class MessageManagementTests(AppointmentLifecycleFixture):
    def manage(self, **overrides):
        values = dict(thread=self.thread, actor=self.admin, action='escalate',
            expected_updated=self.thread.updated_at.isoformat(), doctor=self.doctor,
            priority='high', note='Please review the patient’s question.', confirm=True)
        values.update(overrides)
        return manage_conversation(**values)

    def test_escalation_is_private_task_not_patient_reply_or_email(self):
        task = self.manage()
        self.assertEqual(task.assigned_to, self.doctor)
        self.assertEqual(task.created_by, self.admin)
        self.assertEqual(task.patient, self.patient)
        self.assertEqual(task.priority, 'high')
        self.assertEqual(task.title, 'Reply to patient message')
        self.assertIn('Please review', task.description)
        audit = AuditEvent.objects.get(action='message.escalated')
        self.assertEqual(audit.metadata, {'thread_id': self.thread.pk})
        self.assertNotIn('Please review', str(audit.metadata))
        self.assertFalse(PatientMessage.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_open_escalation_deduplicates_and_cannot_silently_reassign(self):
        task = self.manage()
        self.assertEqual(self.manage().pk, task.pk)
        task.status = 'in_progress'
        task.save()
        self.assertEqual(self.manage().pk, task.pk)
        with self.assertRaisesMessage(ValidationError, 'Reassign that task instead'):
            self.manage(doctor=self.colleague)
        self.assertEqual(ClinicalTask.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action='message.escalated').count(), 1)

    def test_new_escalation_after_acknowledged_task_creates_new_task(self):
        first = self.manage()
        ClinicalTask.objects.filter(pk=first.pk).update(status='done')
        second = self.manage()
        self.assertNotEqual(first.pk, second.pk)

    def test_fresh_role_active_doctor_and_practice_are_required(self):
        with self.assertRaises(PermissionDenied):
            self.manage(actor=self.patient_user)
        with self.assertRaises(PermissionDenied):
            self.manage(actor=self.super_admin, thread=self.beta_thread)
        CompanyMembership.objects.filter(user=self.admin, company=self.company).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.manage()
        CompanyMembership.objects.filter(user=self.admin, company=self.company).update(is_active=True)
        CompanyMembership.objects.filter(user=self.doctor, company=self.company).update(role='practice_admin')
        with self.assertRaises(ValidationError):
            self.manage()
        self.assertFalse(ClinicalTask.objects.exists())

    def test_inactive_patient_or_actor_denied_despite_cached_instances(self):
        for obj in (self.patient, self.admin, self.company):
            with self.subTest(model=type(obj).__name__):
                type(obj).objects.filter(pk=obj.pk).update(is_active=False)
                with self.assertRaises(PermissionDenied):
                    self.manage()
                type(obj).objects.filter(pk=obj.pk).update(is_active=True)
        self.assertFalse(ClinicalTask.objects.exists())

    def test_invalid_action_confirmation_priority_and_note_write_nothing(self):
        for changes in (dict(action='invented'), dict(confirm=False), dict(confirm='yes'),
                        dict(doctor=None), dict(priority='invented'), dict(note='x' * 2001)):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.manage(**changes)
        self.assertFalse(ClinicalTask.objects.exists())
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
