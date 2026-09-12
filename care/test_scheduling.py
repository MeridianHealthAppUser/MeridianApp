from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient

from .models import Appointment, AppointmentProposal, AuditEvent, AvailabilitySlot, MessageThread, PatientEvent, PatientMessage
from .scheduling import propose_appointment_time, respond_to_appointment_proposal


class AppointmentProposalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Meridian', slug='meridian-proposals')
        cls.other_company = Company.objects.create(name='Orion', slug='orion-proposals')
        users = get_user_model().objects
        cls.doctor = users.create_user(email='proposal-doctor@example.com')
        cls.other_doctor = users.create_user(email='proposal-other-doctor@example.com')
        cls.patient_user = users.create_user(email='proposal-patient@example.com')
        cls.other_user = users.create_user(email='proposal-other-patient@example.com')
        cls.admin = users.create_user(email='proposal-admin@example.com')
        for company, user, role in (
            (cls.company, cls.doctor, CompanyMembership.Role.DOCTOR),
            (cls.other_company, cls.doctor, CompanyMembership.Role.DOCTOR),
            (cls.company, cls.other_doctor, CompanyMembership.Role.DOCTOR),
            (cls.company, cls.admin, CompanyMembership.Role.PRACTICE_ADMIN),
        ):
            CompanyMembership.objects.create(company=company, user=user, role=role)
        cls.patient = Patient.objects.create(
            company=cls.company, user=cls.patient_user, first_name='Nadia', last_name='Mokoena',
        )
        cls.other_patient = Patient.objects.create(
            company=cls.other_company, user=cls.other_user, first_name='Other', last_name='Patient',
        )
        cls.thread = MessageThread.objects.create(
            company=cls.company, patient=cls.patient, opened_by=cls.patient_user, subject='Appointment time',
        )
        cls.other_thread = MessageThread.objects.create(
            company=cls.other_company, patient=cls.other_patient, opened_by=cls.other_user, subject='Other time',
        )

    def setUp(self):
        self.original_time = timezone.now().replace(microsecond=0) + timedelta(days=3)
        self.proposed_time = self.original_time + timedelta(days=1)
        self.appointment = Appointment.objects.create(
            company=self.company, patient=self.patient, clinician=self.doctor,
            starts_at=self.original_time, duration_minutes=30,
            video_link='https://example.com/original-call', outcome_notes='Original historical note',
        )

    def propose(self, **overrides):
        kwargs = {
            'appointment': self.appointment, 'thread': self.thread,
            'actor': self.doctor, 'actor_role': 'doctor',
            'proposed_starts_at': self.proposed_time,
        }
        kwargs.update(overrides)
        return propose_appointment_time(**kwargs)

    def respond(self, proposal, **overrides):
        kwargs = {'proposal': proposal, 'actor': self.patient_user, 'actor_role': 'patient', 'decision': 'accept'}
        kwargs.update(overrides)
        return respond_to_appointment_proposal(**kwargs)

    def assert_unchanged(self):
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.starts_at, self.original_time)

    def test_proposal_captures_snapshots_and_messages_without_moving_appointment(self):
        proposal = self.propose(note='Would the afternoon suit you?')
        self.assertEqual(proposal.original_starts_at, self.original_time)
        self.assertEqual(proposal.original_status, Appointment.Status.BOOKED)
        self.assertEqual(proposal.original_clinician, self.doctor)
        self.assertEqual(proposal.original_duration_minutes, 30)
        self.assertEqual(proposal.recipient, self.patient_user)
        self.assertEqual(proposal.status, AppointmentProposal.Status.PENDING)
        self.assert_unchanged()
        self.assertEqual(Appointment.objects.count(), 1)
        message = PatientMessage.objects.get()
        self.assertIn('does not reserve a time slot', message.body)
        self.assertIn('Would the afternoon suit you?', message.body)
        self.assertEqual(message.sender, self.doctor)
        self.assertTrue(PatientEvent.objects.filter(category='appointment', source_id=str(proposal.pk)).exists())
        self.assertTrue(AuditEvent.objects.filter(action='appointment.proposal.created', target_id=str(proposal.pk)).exists())

    def test_only_appointment_doctor_or_owning_patient_can_propose(self):
        for actor, role in ((self.other_doctor, 'doctor'), (self.admin, 'doctor'), (self.other_user, 'patient'), (self.doctor, 'admin')):
            with self.subTest(actor=actor.email, role=role):
                with self.assertRaises(PermissionDenied):
                    self.propose(actor=actor, actor_role=role)
        self.assertFalse(AppointmentProposal.objects.exists())
        self.assertFalse(PatientMessage.objects.exists())

    def test_patient_can_propose_to_their_appointment_doctor(self):
        proposal = self.propose(actor=self.patient_user, actor_role='patient')
        self.assertEqual(proposal.recipient, self.doctor)
        self.assertEqual(proposal.proposer_role, AppointmentProposal.ProposerRole.PATIENT)
        accepted = self.respond(proposal, actor=self.doctor, actor_role='doctor')
        self.assertEqual(accepted.status, AppointmentProposal.Status.ACCEPTED)
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.starts_at, self.proposed_time)

    def test_proposal_requires_matching_open_thread(self):
        with self.assertRaises(ValidationError):
            self.propose(thread=self.other_thread)
        self.thread.is_closed = True
        self.thread.save(update_fields=('is_closed',))
        with self.assertRaises(ValidationError):
            self.propose()
        self.assertFalse(AppointmentProposal.objects.exists())

    def test_creation_rejects_completed_or_past_booked_appointment(self):
        for status, starts_at in (
            (Appointment.Status.COMPLETED, self.original_time),
            (Appointment.Status.BOOKED, timezone.now() - timedelta(days=1)),
        ):
            with self.subTest(status=status):
                Appointment.objects.filter(pk=self.appointment.pk).update(status=status, starts_at=starts_at)
                with self.assertRaises(ValidationError):
                    self.propose()
        self.assertFalse(AppointmentProposal.objects.exists())

    def test_proposal_time_must_be_future_different_and_timezone_aware(self):
        for starts_at in (timezone.now() - timedelta(minutes=1), self.original_time, self.proposed_time.replace(tzinfo=None)):
            with self.subTest(starts_at=starts_at):
                with self.assertRaises(ValidationError):
                    self.propose(proposed_starts_at=starts_at)

    def test_long_note_is_rejected_without_side_effects(self):
        with self.assertRaises(ValidationError):
            self.propose(note='x' * 2001)
        self.assertFalse(AppointmentProposal.objects.exists())
        self.assertFalse(PatientMessage.objects.exists())

    def test_inactive_company_patient_or_clinician_blocks_new_proposals(self):
        for obj in (self.company, self.patient, self.doctor):
            with self.subTest(model=type(obj).__name__):
                obj.is_active = False
                obj.save(update_fields=('is_active',))
                with self.assertRaises((PermissionDenied, ValidationError)):
                    self.propose(actor=self.patient_user, actor_role='patient')
                obj.is_active = True
                obj.save(update_fields=('is_active',))

    def test_recipient_acceptance_reschedules_once_with_audit_and_message(self):
        proposal = self.propose()
        accepted = self.respond(proposal)
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.starts_at, self.proposed_time)
        self.assertEqual(self.appointment.status, Appointment.Status.BOOKED)
        self.assertEqual(self.appointment.video_link, 'https://example.com/original-call')
        self.assertEqual(accepted.responded_by, self.patient_user)
        self.assertIsNotNone(accepted.responded_at)
        self.assertIsNone(accepted.resulting_appointment)
        self.assertEqual(PatientMessage.objects.count(), 2)
        self.assertEqual(Appointment.objects.count(), 1)
        self.assertTrue(AuditEvent.objects.filter(action='appointment.proposal.accepted').exists())
        repeated = self.respond(proposal)
        self.assertEqual(repeated.pk, accepted.pk)
        self.assertEqual(PatientMessage.objects.count(), 2)
        self.assertEqual(AuditEvent.objects.filter(action='appointment.proposal.accepted').count(), 1)

    def test_proposer_foreign_patient_and_other_doctor_cannot_accept(self):
        proposal = self.propose()
        for actor, role in ((self.doctor, 'doctor'), (self.other_doctor, 'doctor'), (self.other_user, 'patient')):
            with self.subTest(actor=actor.email):
                with self.assertRaises(PermissionDenied):
                    self.respond(proposal, actor=actor, actor_role=role)
        self.assert_unchanged()
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, AppointmentProposal.Status.PENDING)

    def test_dual_role_proposer_cannot_accept_their_own_proposal(self):
        proposal = self.propose()
        Patient.objects.filter(pk=self.patient.pk).update(user=self.doctor)
        with self.assertRaises(PermissionDenied):
            self.respond(proposal, actor=self.doctor, actor_role='patient')
        self.assert_unchanged()

    def test_actor_must_respond_through_opposite_role(self):
        proposal = self.propose()
        CompanyMembership.objects.create(company=self.company, user=self.patient_user, role=CompanyMembership.Role.DOCTOR)
        with self.assertRaises(PermissionDenied):
            self.respond(proposal, actor_role='doctor')
        with self.assertRaises(PermissionDenied):
            self.respond(proposal, decision='withdraw')

    def test_decline_and_withdraw_do_not_move_the_appointment(self):
        proposal = self.propose()
        declined = self.respond(proposal, decision='decline')
        self.assertEqual(declined.status, AppointmentProposal.Status.DECLINED)
        self.assert_unchanged()
        replacement = self.propose()
        withdrawn = self.respond(replacement, actor=self.doctor, actor_role='doctor', decision='withdraw')
        self.assertEqual(withdrawn.status, AppointmentProposal.Status.WITHDRAWN)
        self.assert_unchanged()
        with self.assertRaises(ValidationError):
            self.respond(withdrawn)

    def test_counterproposal_preserves_and_supersedes_old_proposal(self):
        first = self.propose()
        counter = self.propose(
            actor=self.patient_user, actor_role='patient', proposed_starts_at=self.proposed_time + timedelta(hours=1),
        )
        first.refresh_from_db()
        self.assertEqual(first.status, AppointmentProposal.Status.SUPERSEDED)
        self.assertEqual(first.responded_by, self.patient_user)
        self.assertEqual(AppointmentProposal.objects.filter(status='pending').count(), 1)
        self.assertEqual(AppointmentProposal.objects.count(), 2)
        self.assertEqual(counter.recipient, self.doctor)
        self.assert_unchanged()
        with self.assertRaises(ValidationError):
            self.respond(first)
        self.respond(counter, actor=self.doctor, actor_role='doctor')
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.starts_at, self.proposed_time + timedelta(hours=1))

    def test_failed_counterproposal_does_not_supersede_existing_proposal(self):
        first = self.propose()
        with patch('care.scheduling.post_patient_message', side_effect=ValidationError('Message unavailable')):
            with self.assertRaises(ValidationError):
                self.propose(proposed_starts_at=self.proposed_time + timedelta(hours=1))
        first.refresh_from_db()
        self.assertEqual(first.status, AppointmentProposal.Status.PENDING)
        self.assertEqual(AppointmentProposal.objects.count(), 1)
        self.assertEqual(PatientMessage.objects.count(), 1)

    def test_acceptance_rechecks_conflicts_across_practices_and_leaves_pending(self):
        proposal = self.propose()
        Appointment.objects.create(
            company=self.other_company, patient=self.other_patient, clinician=self.doctor,
            starts_at=self.proposed_time - timedelta(minutes=20), duration_minutes=40,
        )
        with self.assertRaisesMessage(ValidationError, 'no longer available'):
            self.respond(proposal)
        self.assert_unchanged()
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, AppointmentProposal.Status.PENDING)
        self.assertEqual(PatientMessage.objects.count(), 1)
        self.assertFalse(AuditEvent.objects.filter(action='appointment.proposal.accepted').exists())

    def test_conflicting_counterproposal_keeps_the_previous_pending_proposal(self):
        first = self.propose()
        conflict_time = self.proposed_time + timedelta(hours=2)
        Appointment.objects.create(
            company=self.other_company, patient=self.other_patient, clinician=self.doctor,
            starts_at=conflict_time - timedelta(minutes=10), duration_minutes=30,
        )
        with self.assertRaisesMessage(ValidationError, 'no longer available'):
            self.propose(proposed_starts_at=conflict_time)
        first.refresh_from_db()
        self.assertEqual(first.status, AppointmentProposal.Status.PENDING)
        self.assertEqual(AppointmentProposal.objects.count(), 1)
        self.assertEqual(PatientMessage.objects.count(), 1)

    def test_all_original_snapshot_fields_are_rechecked_before_acceptance(self):
        original_values = {
            'starts_at': self.original_time, 'status': Appointment.Status.BOOKED,
            'clinician_id': self.doctor.pk, 'duration_minutes': 30,
        }
        proposal = self.propose()
        for field, value in (
            ('starts_at', self.original_time + timedelta(hours=1)),
            ('status', Appointment.Status.CANCELLED),
            ('clinician_id', self.other_doctor.pk),
            ('duration_minutes', 45),
        ):
            with self.subTest(field=field):
                Appointment.objects.filter(pk=self.appointment.pk).update(**{field: value})
                with self.assertRaisesMessage(ValidationError, 'changed since this time was proposed'):
                    self.respond(proposal)
                proposal.refresh_from_db()
                self.assertEqual(proposal.status, AppointmentProposal.Status.PENDING)
                Appointment.objects.filter(pk=self.appointment.pk).update(**original_values)

    def test_acceptance_rechecks_proposed_time_has_not_passed(self):
        proposal = self.propose(proposed_starts_at=timezone.now() + timedelta(hours=1))
        with patch('care.scheduling.timezone.now', return_value=timezone.now() + timedelta(hours=2)):
            with self.assertRaisesMessage(ValidationError, 'future appointment time'):
                self.respond(proposal)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, AppointmentProposal.Status.PENDING)
        self.assert_unchanged()

    def test_rebooking_creates_new_booking_and_preserves_cancelled_or_missed_history(self):
        for original_status in (Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW):
            with self.subTest(status=original_status):
                original = Appointment.objects.create(
                    company=self.company, patient=self.patient, clinician=self.doctor,
                    starts_at=timezone.now() - timedelta(days=2), duration_minutes=15,
                    status=original_status, outcome_notes='Historical outcome',
                )
                new_time = self.proposed_time + timedelta(days=1 if original_status == 'cancelled' else 2)
                proposal = self.propose(appointment=original, proposed_starts_at=new_time)
                self.assertEqual(proposal.kind, AppointmentProposal.Kind.REBOOK)
                accepted = self.respond(proposal)
                original.refresh_from_db()
                self.assertEqual(original.status, original_status)
                self.assertEqual(original.outcome_notes, 'Historical outcome')
                replacement = accepted.resulting_appointment
                self.assertNotEqual(replacement.pk, original.pk)
                self.assertEqual(replacement.status, Appointment.Status.BOOKED)
                self.assertEqual(replacement.starts_at, new_time)
                self.assertEqual(replacement.company, self.company)
                self.assertEqual(replacement.patient, self.patient)
                self.assertEqual(replacement.clinician, self.doctor)
                self.assertEqual(replacement.outcome_notes, '')
                count = Appointment.objects.count()
                self.respond(proposal)
                self.assertEqual(Appointment.objects.count(), count)
                with self.assertRaisesMessage(ValidationError, 'already been rebooked'):
                    self.propose(appointment=original, proposed_starts_at=new_time + timedelta(hours=2))
                self.assertEqual(Appointment.objects.count(), count)

    def test_reschedule_releases_the_original_availability_slot(self):
        slot = AvailabilitySlot.objects.create(
            company=self.company, clinician=self.doctor,
            starts_at=self.original_time, ends_at=self.original_time + timedelta(minutes=30),
            is_booked=True, appointment=self.appointment,
        )
        proposal = self.propose()
        slot.refresh_from_db()
        self.assertTrue(slot.is_booked)
        self.respond(proposal)
        slot.refresh_from_db()
        self.assertFalse(slot.is_booked)
        self.assertIsNone(slot.appointment_id)
        self.assertEqual(slot.starts_at, self.original_time)

    def test_pending_proposals_do_not_reserve_slots(self):
        proposal = self.propose()
        other_appointment = Appointment.objects.create(
            company=self.other_company, patient=self.other_patient, clinician=self.doctor,
            starts_at=self.original_time + timedelta(hours=2), duration_minutes=30,
        )
        other_proposal = self.propose(appointment=other_appointment, thread=self.other_thread)
        self.assertEqual(AppointmentProposal.objects.filter(status='pending').count(), 2)
        self.respond(proposal)
        with self.assertRaisesMessage(ValidationError, 'no longer available'):
            self.respond(other_proposal, actor=self.other_user)

    def test_message_failure_rolls_back_an_accepted_change(self):
        proposal = self.propose()
        with patch('care.scheduling.post_patient_message', side_effect=ValidationError('Message unavailable')):
            with self.assertRaises(ValidationError):
                self.respond(proposal)
        self.assert_unchanged()
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, AppointmentProposal.Status.PENDING)

    def test_model_rejects_cross_practice_and_wrong_participant_links(self):
        proposal = self.propose()
        for field, value in (
            ('thread', self.other_thread),
            ('appointment', Appointment.objects.create(
                company=self.other_company, patient=self.other_patient, clinician=self.doctor,
                starts_at=self.original_time + timedelta(days=5), duration_minutes=30,
            )),
            ('proposed_by', self.other_doctor),
            ('recipient', self.other_user),
        ):
            with self.subTest(field=field):
                proposal.refresh_from_db()
                setattr(proposal, field, value)
                with self.assertRaises(ValidationError) as caught:
                    proposal.full_clean()
                self.assertIn(field, caught.exception.message_dict)

    def test_database_enforces_only_one_pending_proposal_per_appointment(self):
        proposal = self.propose()
        proposal.pk = None
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                proposal.save()
        self.assertEqual(AppointmentProposal.objects.count(), 1)
