"""Explicit booking/status rules, including shared-identity collision checks."""

from django.test import override_settings
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient

from .appointment_lifecycle import book_staff_appointment, change_appointment_status
from .clinical import save_consultation
from .models import Appointment, AuditEvent, AvailabilitySlot, ClinicalNote, MessageThread, PatientEvent
from .scheduling import propose_appointment_time, respond_to_appointment_proposal


@override_settings(MULTI_PRACTICE_ENABLED=True)
class AppointmentLifecycleFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Appointment Alpha', slug='appointment-alpha')
        cls.beta = Company.objects.create(name='Appointment Beta', slug='appointment-beta')
        users = get_user_model().objects
        cls.doctor = users.create_user(email='lifecycle-doctor@example.test', first_name='Doctor')
        cls.colleague = users.create_user(email='lifecycle-colleague@example.test')
        cls.admin = users.create_user(email='lifecycle-admin@example.test')
        cls.super_admin = users.create_user(email='lifecycle-super@example.test')
        cls.patient_user = users.create_user(email='lifecycle-patient@example.test')
        cls.other_user = users.create_user(email='lifecycle-other@example.test')
        for company, actor, role in (
            (cls.company, cls.doctor, 'doctor'), (cls.beta, cls.doctor, 'doctor'),
            (cls.company, cls.colleague, 'doctor'), (cls.beta, cls.colleague, 'doctor'),
            (cls.company, cls.admin, 'practice_admin'), (cls.beta, cls.admin, 'practice_admin'),
            (cls.company, cls.super_admin, 'super_admin'),
        ):
            CompanyMembership.objects.create(company=company, user=actor, role=role)
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user,
            first_name='Alpha', last_name='Patient', assigned_doctor=cls.doctor)
        cls.beta_patient = Patient.objects.create(company=cls.beta, user=cls.patient_user,
            first_name='Beta', last_name='Patient', assigned_doctor=cls.colleague)
        cls.other_patient = Patient.objects.create(company=cls.company, user=cls.other_user,
            first_name='Other', last_name='Patient')
        cls.thread = MessageThread.objects.create(company=cls.company, patient=cls.patient,
            opened_by=cls.patient_user, subject='Appointment discussion')
        cls.beta_thread = MessageThread.objects.create(company=cls.beta, patient=cls.beta_patient,
            opened_by=cls.patient_user, subject='Other practice discussion')

    def setUp(self):
        self.starts_at = timezone.now().replace(microsecond=0) + timedelta(days=3)

    def appointment(self, **overrides):
        values = dict(company=self.company, patient=self.patient, clinician=self.doctor,
                      starts_at=self.starts_at, duration_minutes=30)
        values.update(overrides)
        return Appointment.objects.create(**values)

    def book(self, **overrides):
        values = dict(company=self.company, patient=self.patient, actor=self.admin,
                      clinician=self.doctor, starts_at=self.starts_at, duration_minutes=30,
                      appointment_type='initial')
        values.update(overrides)
        return book_staff_appointment(**values)

    def change(self, appointment, **overrides):
        values = dict(appointment=appointment, actor=self.doctor, status='cancelled',
                      expected_updated=appointment.updated_at.isoformat(), confirm=True,
                      reason='Patient asked to cancel.')
        values.update(overrides)
        return change_appointment_status(**values)

    def proposal(self, appointment, **overrides):
        values = dict(appointment=appointment, thread=self.thread, actor=self.doctor,
                      actor_role='doctor', proposed_starts_at=self.starts_at + timedelta(days=1))
        values.update(overrides)
        return propose_appointment_time(**values)


class AppointmentLifecycleTests(AppointmentLifecycleFixture):
    def test_staff_booking_claims_exact_slot_and_replay_has_no_duplicate_events(self):
        slot = AvailabilitySlot.objects.create(company=self.company, clinician=self.doctor,
            starts_at=self.starts_at, ends_at=self.starts_at + timedelta(minutes=30))
        booking = self.book(video_link='https://example.test/video')
        slot.refresh_from_db()
        self.assertTrue(slot.is_booked)
        self.assertEqual(slot.appointment_id, booking.pk)
        self.assertEqual(self.book().pk, booking.pk)
        self.assertEqual(Appointment.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action='appointment.created').count(), 1)
        self.assertEqual(PatientEvent.objects.count(), 1)
        self.assertFalse(ClinicalNote.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_booking_rechecks_roles_active_company_patient_and_clinician(self):
        with self.assertRaises(PermissionDenied):
            self.book(actor=self.patient_user)
        with self.assertRaises(ValidationError):
            self.book(patient=self.beta_patient)
        for obj, exception in ((self.company, PermissionDenied), (self.patient, ValidationError),
                               (self.doctor, ValidationError), (self.admin, PermissionDenied)):
            with self.subTest(model=type(obj).__name__, pk=obj.pk):
                type(obj).objects.filter(pk=obj.pk).update(is_active=False)
                with self.assertRaises(exception):
                    self.book()
                type(obj).objects.filter(pk=obj.pk).update(is_active=True)
        CompanyMembership.objects.filter(company=self.company, user=self.doctor).update(is_active=False)
        with self.assertRaises(ValidationError):
            self.book()
        self.assertFalse(Appointment.objects.exists())

    def test_invalid_dates_duration_and_video_links_are_validation_errors(self):
        cases = [dict(starts_at=timezone.now() - timedelta(seconds=1)),
                 dict(starts_at=self.starts_at.replace(tzinfo=None)), dict(starts_at='tomorrow'),
                 dict(duration_minutes=True), dict(duration_minutes=0), dict(duration_minutes=121),
                 dict(appointment_type='invented'), dict(video_link='javascript:alert(1)'),
                 dict(video_link='https://['), dict(video_link=123), dict(video_link='https:missing-host')]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.book(**values)
        self.assertFalse(Appointment.objects.exists())

    def test_doctor_owns_attendance_and_must_wait_until_appointment_end(self):
        appointment = self.appointment(starts_at=timezone.now() - timedelta(minutes=15))
        with self.assertRaises(ValidationError):
            self.change(appointment, status='completed')
        Appointment.objects.filter(pk=appointment.pk).update(starts_at=timezone.now() - timedelta(hours=1))
        appointment.refresh_from_db()
        for actor in (self.colleague, self.admin, self.super_admin, self.patient_user):
            with self.subTest(actor=actor.pk), self.assertRaises(PermissionDenied):
                self.change(appointment, actor=actor, status='completed')
        completed = self.change(appointment, status='completed')
        self.assertEqual(completed.status, 'completed')
        self.assertFalse(ClinicalNote.objects.exists())

    def test_admin_and_super_can_cancel_but_doctor_cannot_cancel_colleague(self):
        appointment = self.appointment()
        with self.assertRaises(PermissionDenied):
            self.change(appointment, actor=self.colleague)
        self.assertEqual(self.change(appointment, actor=self.admin).status, 'cancelled')
        second = self.appointment(starts_at=self.starts_at + timedelta(hours=1))
        self.assertEqual(self.change(second, actor=self.super_admin).status, 'cancelled')

    def test_patient_may_only_cancel_own_future_booking(self):
        appointment = self.appointment()
        for actor, status in ((self.other_user, 'cancelled'), (self.patient_user, 'completed'),
                              (self.doctor, 'cancelled')):
            with self.subTest(actor=actor.pk, status=status), self.assertRaises(PermissionDenied):
                self.change(appointment, actor=actor, status=status, patient_portal=True)
        past = self.appointment(starts_at=timezone.now() - timedelta(days=1))
        with self.assertRaises(ValidationError):
            self.change(past, actor=self.patient_user, patient_portal=True)
        self.assertEqual(self.change(appointment, actor=self.patient_user, patient_portal=True).status, 'cancelled')

    def test_signed_consultation_blocks_cancellation_and_no_show_but_allows_completed(self):
        appointment = self.appointment(starts_at=timezone.now() - timedelta(hours=1))
        encounter = save_consultation(company=self.company, patient=self.patient, actor=self.doctor,
            summary='Signed original.', occurred_at=appointment.starts_at, appointment=appointment, sign=True)
        for status in ('cancelled', 'no_show'):
            with self.subTest(status=status), self.assertRaisesMessage(ValidationError, 'signed clinical encounter'):
                self.change(appointment, status=status)
        self.assertEqual(self.change(appointment, status='completed').status, 'completed')
        encounter.refresh_from_db()
        self.assertEqual(encounter.signed_note.body, 'Signed original.')

    def test_stale_version_terminal_status_and_confirmation_fail_without_audits(self):
        appointment = self.appointment()
        for changes in (dict(expected_updated='old-version'), dict(confirm=False),
                        dict(confirm='yes'), dict(reason=''), dict(reason='x' * 501), dict(status='booked')):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.change(appointment, **changes)
        Appointment.objects.filter(pk=appointment.pk).update(status='completed')
        with self.assertRaises(ValidationError):
            self.change(appointment)
        self.assertFalse(AuditEvent.objects.exists())

    def test_cancellation_supersedes_pending_proposal_releases_slot_and_is_idempotent(self):
        appointment = self.appointment()
        slot = AvailabilitySlot.objects.create(company=self.company, clinician=self.doctor,
            starts_at=self.starts_at, ends_at=self.starts_at + timedelta(minutes=30), is_booked=True, appointment=appointment)
        proposal = self.proposal(appointment)
        cancelled = self.change(appointment)
        proposal.refresh_from_db()
        slot.refresh_from_db()
        self.assertEqual(proposal.status, 'superseded')
        self.assertIsNotNone(proposal.responded_at)
        self.assertEqual(proposal.responded_by, self.doctor)
        self.assertFalse(slot.is_booked)
        self.assertIsNone(slot.appointment_id)
        before = AuditEvent.objects.count()
        self.assertEqual(self.change(appointment).pk, cancelled.pk)
        self.assertEqual(AuditEvent.objects.count(), before)
        with self.assertRaises(ValidationError):
            respond_to_appointment_proposal(proposal=proposal, actor=self.patient_user, actor_role='patient', decision='accept')
        appointment.refresh_from_db()
        self.assertEqual(appointment.starts_at, self.starts_at)

    def test_no_show_releases_slot_without_creating_clinical_note(self):
        appointment = self.appointment(starts_at=timezone.now() - timedelta(hours=1))
        slot = AvailabilitySlot.objects.create(company=self.company, clinician=self.doctor,
            starts_at=appointment.starts_at, ends_at=appointment.starts_at + timedelta(minutes=30),
            is_booked=True, appointment=appointment)
        self.change(appointment, status='no_show', reason='')
        slot.refresh_from_db()
        self.assertFalse(slot.is_booked)
        self.assertFalse(ClinicalNote.objects.exists())

    def test_patient_identity_collision_crosses_practices_without_exposing_other_record(self):
        self.appointment(company=self.beta, patient=self.beta_patient, clinician=self.colleague)
        with self.assertRaises(ValidationError) as caught:
            self.book()
        self.assertIn('patient already has an appointment', str(caught.exception))
        self.assertNotIn(self.beta.name, str(caught.exception))
        self.assertNotIn(str(self.beta_patient.pk), str(caught.exception))
        # Touching interval boundaries do not overlap.
        self.book(starts_at=self.starts_at + timedelta(minutes=30))
        self.book(starts_at=self.starts_at - timedelta(minutes=30))
        self.assertEqual(Appointment.objects.count(), 3)

    def test_unlinked_patient_collision_uses_record_not_all_null_user_records(self):
        first = Patient.objects.create(company=self.company, first_name='Unlinked', last_name='One')
        second = Patient.objects.create(company=self.company, first_name='Unlinked', last_name='Two')
        self.appointment(patient=first, clinician=self.colleague)
        with self.assertRaises(ValidationError):
            self.book(patient=first)
        self.assertEqual(self.book(patient=second).patient_id, second.pk)

    def test_proposal_rechecks_patient_availability_after_intervening_booking(self):
        appointment = self.appointment()
        proposed_at = self.starts_at + timedelta(days=1)
        proposal = self.proposal(appointment, proposed_starts_at=proposed_at)
        self.book(company=self.beta, patient=self.beta_patient, clinician=self.colleague, starts_at=proposed_at)
        with self.assertRaisesMessage(ValidationError, 'patient already has an appointment'):
            respond_to_appointment_proposal(proposal=proposal, actor=self.patient_user, actor_role='patient', decision='accept')
        proposal.refresh_from_db()
        appointment.refresh_from_db()
        self.assertEqual(proposal.status, 'pending')
        self.assertEqual(appointment.starts_at, self.starts_at)

    def test_booking_rechecks_patient_after_intervening_proposal_acceptance(self):
        appointment = self.appointment()
        proposed_at = self.starts_at + timedelta(days=1)
        proposal = self.proposal(appointment, proposed_starts_at=proposed_at)
        respond_to_appointment_proposal(proposal=proposal, actor=self.patient_user, actor_role='patient', decision='accept')
        with self.assertRaisesMessage(ValidationError, 'patient already has an appointment'):
            self.book(company=self.beta, patient=self.beta_patient, clinician=self.colleague, starts_at=proposed_at)

    def test_patient_clash_prevents_creating_proposal(self):
        appointment = self.appointment()
        proposed_at = self.starts_at + timedelta(days=1)
        self.appointment(company=self.beta, patient=self.beta_patient, clinician=self.colleague, starts_at=proposed_at)
        with self.assertRaisesMessage(ValidationError, 'patient already has an appointment'):
            self.proposal(appointment, proposed_starts_at=proposed_at)

    def test_booking_locks_fresh_patient_identity_and_rejects_changed_key(self):
        manager = get_user_model().objects
        original = manager.select_for_update

        def changed_identity(*args, **kwargs):
            Patient.objects.filter(pk=self.patient.pk).update(user=None)
            return original(*args, **kwargs)

        with patch.object(manager, 'select_for_update', side_effect=changed_identity):
            with self.assertRaisesMessage(ValidationError, 'patient identity changed'):
                self.book()
        self.assertFalse(Appointment.objects.exists())

    def test_proposal_rejects_patient_identity_changed_while_waiting_for_lock(self):
        appointment = self.appointment()
        manager = get_user_model().objects
        original = manager.select_for_update

        def changed_identity(*args, **kwargs):
            Patient.objects.filter(pk=self.patient.pk).update(user=None)
            return original(*args, **kwargs)

        with patch.object(manager, 'select_for_update', side_effect=changed_identity):
            with self.assertRaisesMessage(ValidationError, 'appointment patient changed'):
                self.proposal(appointment)

    def test_status_rejects_patient_identity_changed_while_waiting_for_lock(self):
        appointment = self.appointment()
        manager = get_user_model().objects
        original = manager.select_for_update

        def changed_identity(*args, **kwargs):
            Patient.objects.filter(pk=self.patient.pk).update(user=None)
            return original(*args, **kwargs)

        with patch.object(manager, 'select_for_update', side_effect=changed_identity):
            with self.assertRaisesMessage(ValidationError, 'Appointment participants changed'):
                self.change(appointment)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, 'booked')

    def test_booking_identity_query_is_ordered_and_precedes_collision_reads(self):
        with CaptureQueriesContext(connection) as queries:
            self.book()
        user_table = get_user_model()._meta.db_table
        statements = [entry['sql'] for entry in queries]
        identity_index = next(i for i, sql in enumerate(statements) if f'FROM "{user_table}"' in sql and 'ORDER BY' in sql)
        self.assertIn('ASC', statements[identity_index])
        self.assertIn(str(self.patient_user.pk), statements[identity_index])
        collision_index = next(i for i, sql in enumerate(statements) if 'AS "a" FROM "care_appointment"' in sql)
        self.assertLess(identity_index, collision_index)
