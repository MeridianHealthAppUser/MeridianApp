"""Working hours and leave constrain new bookings without changing existing ones."""

from django.test import override_settings
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient

from .availability import (
    appointments_requiring_attention, cancel_time_off, create_time_off,
    ensure_working_time, open_slots_for_day, save_working_pattern,
)
from .forms import AppointmentForm
from .messaging import add_participant
from .models import (
    Appointment, AppointmentProposal, AuditEvent, AvailabilitySlot,
    DoctorTimeOff, DoctorWorkingPattern, MessageThread, PatientEvent, PatientMessage,
)
from .scheduling import propose_appointment_time, respond_to_appointment_proposal


@override_settings(MULTI_PRACTICE_ENABLED=True)
class DoctorAvailabilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Alpha Availability', slug='alpha-availability')
        cls.beta = Company.objects.create(name='Beta Availability', slug='beta-availability')
        users = get_user_model().objects
        cls.doctor = users.create_user(email='availability-doctor@example.test')
        cls.colleague = users.create_user(email='availability-colleague@example.test')
        cls.admin = users.create_user(email='availability-admin@example.test')
        cls.super_admin = users.create_user(email='availability-super@example.test')
        cls.patient_user = users.create_user(email='availability-patient@example.test')
        for user, role in (
            (cls.doctor, CompanyMembership.Role.DOCTOR), (cls.colleague, CompanyMembership.Role.DOCTOR),
            (cls.admin, CompanyMembership.Role.PRACTICE_ADMIN), (cls.super_admin, CompanyMembership.Role.SUPER_ADMIN),
        ):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role)
        CompanyMembership.objects.create(company=cls.beta, user=cls.doctor, role=CompanyMembership.Role.DOCTOR)
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Alice', last_name='Patient')
        cls.beta_patient = Patient.objects.create(company=cls.beta, first_name='Beta', last_name='Patient')
        cls.thread = MessageThread.objects.create(company=cls.company, patient=cls.patient, opened_by=cls.patient_user, subject='Appointment changes')
        add_participant(cls.thread, cls.doctor)
        today = timezone.localdate()
        cls.monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        cls.sast = ZoneInfo('Africa/Johannesburg')

    def at(self, hour, minute=0, *, day=None):
        return datetime.combine(day or self.monday, time(hour, minute), tzinfo=self.sast)

    def days(self, *, starts=time(9), ends=time(12)):
        return [
            {'weekday': day, 'is_working': day < 5, 'starts_at': starts if day < 5 else None, 'ends_at': ends if day < 5 else None}
            for day in range(7)
        ]

    def pattern(self, **overrides):
        values = {'company': self.company, 'clinician': self.doctor, 'actor': self.doctor, 'days': self.days()}
        values.update(overrides)
        return save_working_pattern(**values)

    def leave(self, **overrides):
        values = {'company': self.company, 'clinician': self.doctor, 'actor': self.doctor,
                  'starts_at': self.at(10), 'ends_at': self.at(11), 'reason': 'sick'}
        values.update(overrides)
        return create_time_off(**values)

    def appointment(self, **overrides):
        values = {'company': self.company, 'patient': self.patient, 'clinician': self.doctor,
                  'starts_at': self.at(9), 'duration_minutes': 30}
        values.update(overrides)
        return Appointment.objects.create(**values)

    def assert_allowed(self, starts_at, *, company=None, duration=30):
        ensure_working_time(company=company or self.company, clinician=self.doctor, starts_at=starts_at, duration_minutes=duration)

    def test_seven_day_pattern_is_saved_per_practice_and_replaced_without_duplicates(self):
        self.pattern()
        self.assertEqual(DoctorWorkingPattern.objects.filter(company=self.company, clinician=self.doctor).count(), 7)
        self.pattern(days=self.days(starts=time(10)))
        self.assertEqual(DoctorWorkingPattern.objects.filter(company=self.company, clinician=self.doctor).count(), 7)
        self.assertEqual(DoctorWorkingPattern.objects.get(company=self.company, clinician=self.doctor, weekday=0).starts_at, time(10))
        self.pattern(company=self.beta, days=self.days(starts=time(14), ends=time(17)))
        self.assertEqual(DoctorWorkingPattern.objects.count(), 14)
        self.assert_allowed(self.at(10))
        self.assert_allowed(self.at(14), company=self.beta)
        with self.assertRaises(ValidationError):
            self.assert_allowed(self.at(10), company=self.beta)

    def test_only_active_self_doctor_may_change_a_working_pattern(self):
        for actor in (self.colleague, self.admin, self.super_admin, self.patient_user):
            with self.subTest(actor=actor.email):
                with self.assertRaises(PermissionDenied):
                    self.pattern(actor=actor)
        self.assertFalse(DoctorWorkingPattern.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())
        CompanyMembership.objects.filter(company=self.company, user=self.doctor).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.pattern()

    def test_invalid_or_incomplete_week_is_rejected_atomically(self):
        self.pattern()
        before = list(DoctorWorkingPattern.objects.order_by('pk').values())
        examples = [self.days()[:6], self.days() + [self.days()[0]], self.days(starts=time(12), ends=time(9))]
        duplicate = self.days()
        duplicate[6]['weekday'] = 0
        examples.append(duplicate)
        missing_time = self.days()
        missing_time[0]['starts_at'] = None
        examples.append(missing_time)
        for days in examples:
            with self.subTest(days=days):
                with self.assertRaises(ValidationError):
                    self.pattern(days=days)
                self.assertEqual(list(DoctorWorkingPattern.objects.order_by('pk').values()), before)

    def test_hours_include_exact_boundaries_but_exclude_off_days_and_overruns(self):
        self.pattern()
        self.assert_allowed(self.at(9))
        self.assert_allowed(self.at(11, 30))
        for starts_at in (self.at(8, 59), self.at(11, 31), self.at(12), self.at(9, day=self.monday + timedelta(days=5))):
            with self.subTest(starts_at=starts_at):
                with self.assertRaises(ValidationError):
                    self.assert_allowed(starts_at)

    def test_unconfigured_practice_retains_legacy_booking_compatibility(self):
        self.assert_allowed(self.at(6, day=self.monday + timedelta(days=5)))
        manual = AvailabilitySlot.objects.create(
            company=self.company, clinician=self.doctor, starts_at=self.at(9, 10), ends_at=self.at(10, 10),
        )
        slots = open_slots_for_day(company=self.company, clinicians=[self.doctor], day=self.monday)
        self.assertIn(manual.starts_at, [slot.starts_at for slot in slots])

    def test_time_off_is_global_but_other_practice_errors_do_not_reveal_its_reason(self):
        leave = self.leave()
        self.assertEqual(leave.company, self.company)
        self.assertTrue(leave.is_active)
        for company in (self.company, self.beta):
            with self.subTest(company=company.name):
                with self.assertRaises(ValidationError) as error:
                    self.assert_allowed(self.at(10), company=company)
                self.assertNotIn('sick', str(error.exception).lower())
                self.assertNotIn(self.company.name, str(error.exception))

    def test_repeated_identical_time_off_submission_is_idempotent(self):
        first = self.leave()
        repeated = self.leave()
        self.assertEqual(repeated.pk, first.pk)
        self.assertEqual(DoctorTimeOff.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action='availability.time_off_created').count(), 1)

    def test_database_rejects_invalid_weekdays_and_inconsistent_working_hours(self):
        for values in (
            {'weekday': 7, 'is_working': False},
            {'weekday': 0, 'is_working': True, 'starts_at': None, 'ends_at': time(12)},
            {'weekday': 0, 'is_working': False, 'starts_at': time(9), 'ends_at': time(12)},
            {'weekday': 0, 'is_working': True, 'starts_at': time(12), 'ends_at': time(9)},
        ):
            with self.subTest(values=values):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    DoctorWorkingPattern.objects.create(company=self.company, clinician=self.doctor, **values)
        self.assertFalse(DoctorWorkingPattern.objects.exists())

    def test_database_rejects_invalid_time_off_intervals_and_cancellation_state(self):
        base = {'company': self.company, 'clinician': self.doctor, 'starts_at': self.at(10), 'ends_at': self.at(11)}
        for overrides in (
            {'ends_at': self.at(9)},
            {'ends_at': self.at(10) + timedelta(days=367)},
            {'is_active': False, 'cancelled_at': None},
            {'is_active': True, 'cancelled_at': timezone.now()},
            {'is_active': True, 'cancelled_by': self.doctor},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    DoctorTimeOff.objects.create(**{**base, **overrides})
        self.assertFalse(DoctorTimeOff.objects.exists())

    def test_partial_time_off_uses_half_open_intervals(self):
        self.leave()
        self.assert_allowed(self.at(9, 30))
        self.assert_allowed(self.at(11))
        for starts_at in (self.at(9, 45), self.at(10), self.at(10, 45)):
            with self.subTest(starts_at=starts_at):
                with self.assertRaises(ValidationError):
                    self.assert_allowed(starts_at)

    def test_time_off_validation_rejects_reversed_naive_and_unknown_reason(self):
        for overrides in (
            {'ends_at': self.at(9)}, {'ends_at': self.at(10)}, {'starts_at': self.at(10).replace(tzinfo=None)},
            {'reason': 'invented'},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    self.leave(**overrides)
        self.assertFalse(DoctorTimeOff.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())

    def test_only_self_doctor_can_add_or_cancel_leave_and_cancellation_is_soft(self):
        for actor in (self.colleague, self.admin, self.super_admin, self.patient_user):
            with self.subTest(actor=actor.email):
                with self.assertRaises(PermissionDenied):
                    self.leave(actor=actor)
        leave = self.leave()
        for actor in (self.colleague, self.admin, self.super_admin):
            with self.assertRaises(PermissionDenied):
                cancel_time_off(time_off=leave, actor=actor)
        cancelled = cancel_time_off(time_off=leave, actor=self.doctor)
        leave.refresh_from_db()
        self.assertEqual(DoctorTimeOff.objects.count(), 1)
        self.assertFalse(leave.is_active)
        self.assertIsNotNone(leave.cancelled_at)
        self.assertEqual(leave.cancelled_by_id, self.doctor.pk)
        self.assert_allowed(self.at(10), company=self.beta)
        before = AuditEvent.objects.count()
        cancel_time_off(time_off=cancelled or leave, actor=self.doctor)
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_availability_writes_audit_only_the_origin_practice_and_actor(self):
        self.pattern()
        leave = self.leave()
        cancel_time_off(time_off=leave, actor=self.doctor)
        self.assertEqual(AuditEvent.objects.count(), 3)
        for audit in AuditEvent.objects.all():
            self.assertEqual((audit.company_id, audit.actor_id), (self.company.pk, self.doctor.pk))
            self.assertIsNone(audit.patient_id)

    def test_dynamic_slots_are_fifteen_minute_starts_and_do_not_persist_rows(self):
        self.pattern()
        before = (AvailabilitySlot.objects.count(), Appointment.objects.count(), AuditEvent.objects.count())
        slots = list(open_slots_for_day(company=self.company, clinicians=[self.doctor], day=self.monday))
        expected = [self.at(9) + timedelta(minutes=15 * index) for index in range(11)]
        self.assertEqual([slot.starts_at for slot in slots], expected)
        self.assertTrue(all(slot.ends_at - slot.starts_at == timedelta(minutes=30) for slot in slots))
        self.assertEqual((AvailabilitySlot.objects.count(), Appointment.objects.count(), AuditEvent.objects.count()), before)

    def test_dynamic_slots_subtract_cross_practice_bookings_and_partial_time_off(self):
        self.pattern()
        self.appointment(company=self.beta, patient=self.beta_patient, starts_at=self.at(10))
        self.leave(starts_at=self.at(11), ends_at=self.at(11, 30))
        slots = open_slots_for_day(company=self.company, clinicians=[self.doctor], day=self.monday)
        self.assertEqual([slot.starts_at for slot in slots], [self.at(9), self.at(9, 15), self.at(9, 30), self.at(10, 30), self.at(11, 30)])

    def test_cancelled_bookings_do_not_block_slots_and_output_limit_is_respected(self):
        self.pattern()
        self.appointment(starts_at=self.at(9), status=Appointment.Status.CANCELLED)
        slots = list(open_slots_for_day(company=self.company, clinicians=[self.doctor], day=self.monday, limit=2))
        self.assertEqual([slot.starts_at for slot in slots], [self.at(9), self.at(9, 15)])

    def test_existing_appointments_are_preserved_and_attention_is_practice_scoped(self):
        affected = self.appointment(starts_at=self.at(9))
        unaffected = self.appointment(starts_at=self.at(11))
        foreign = self.appointment(company=self.beta, patient=self.beta_patient, starts_at=self.at(9))
        cancelled = self.appointment(starts_at=self.at(9, 30), status=Appointment.Status.CANCELLED)
        past = self.appointment(starts_at=timezone.now() - timedelta(days=1))
        before = list(Appointment.objects.order_by('pk').values())
        self.pattern(days=self.days(starts=time(10)))
        attention = appointments_requiring_attention(company=self.company, clinician=self.doctor)
        self.assertEqual(set(attention.values_list('pk', flat=True)), {affected.pk})
        self.assertEqual(list(Appointment.objects.order_by('pk').values()), before)
        self.leave(starts_at=self.at(11), ends_at=self.at(12))
        self.assertEqual(set(appointments_requiring_attention(company=self.company).values_list('pk', flat=True)), {affected.pk, unaffected.pk})
        self.assertEqual(list(Appointment.objects.order_by('pk').values()), before)

    def test_manual_appointment_form_rejects_outside_hours_and_global_time_off(self):
        self.pattern()
        self.leave(company=self.beta)
        for starts_at in (self.at(8), self.at(10)):
            with self.subTest(starts_at=starts_at):
                form = AppointmentForm({
                    'patient': self.patient.pk, 'clinician': self.doctor.pk, 'appointment_type': 'follow_up',
                    'starts_at': starts_at.isoformat(), 'duration_minutes': 30, 'video_link': '',
                }, company=self.company, patient=self.patient)
                self.assertFalse(form.is_valid())
                self.assertIn('starts_at', form.errors)
        self.assertFalse(Appointment.objects.exists())

    def test_pending_proposal_acceptance_rechecks_new_time_off_without_mutation(self):
        self.pattern()
        appointment = self.appointment()
        proposal = propose_appointment_time(
            appointment=appointment, thread=self.thread, actor=self.doctor, actor_role='doctor', proposed_starts_at=self.at(10),
        )
        self.leave(company=self.beta)
        before = (PatientMessage.objects.count(), PatientEvent.objects.count(), AuditEvent.objects.count())
        with self.assertRaises(ValidationError):
            respond_to_appointment_proposal(proposal=proposal, actor=self.patient_user, actor_role='patient', decision='accept')
        appointment.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual(appointment.starts_at, self.at(9))
        self.assertEqual(proposal.status, AppointmentProposal.Status.PENDING)
        self.assertEqual((PatientMessage.objects.count(), PatientEvent.objects.count(), AuditEvent.objects.count()), before)

    def test_pending_proposal_acceptance_rechecks_a_changed_working_pattern(self):
        self.pattern()
        appointment = self.appointment()
        proposal = propose_appointment_time(
            appointment=appointment, thread=self.thread, actor=self.doctor, actor_role='doctor', proposed_starts_at=self.at(10),
        )
        self.pattern(days=self.days(starts=time(11)))
        with self.assertRaises(ValidationError):
            respond_to_appointment_proposal(proposal=proposal, actor=self.patient_user, actor_role='patient', decision='accept')
        appointment.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual(appointment.starts_at, self.at(9))
        self.assertEqual(proposal.status, AppointmentProposal.Status.PENDING)

    def test_new_proposals_are_rejected_outside_configured_hours(self):
        self.pattern()
        appointment = self.appointment()
        with self.assertRaises(ValidationError):
            propose_appointment_time(
                appointment=appointment, thread=self.thread, actor=self.doctor, actor_role='doctor', proposed_starts_at=self.at(8),
            )
        self.assertFalse(AppointmentProposal.objects.exists())
