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
    appointments_requiring_attention, attention_kind, cancel_time_off, create_days_off, create_time_off,
    diary_day_states, ensure_working_time, open_slots_for_day, remove_special_day, save_working_pattern,
    set_special_day, undo_diary_change,
)
from .forms import AppointmentForm
from .messaging import add_participant
from .models import (
    Appointment, AppointmentProposal, AuditEvent, AvailabilitySlot, ClinicalTask,
    DoctorSpecialDay, DoctorTimeOff, DoctorWorkingPattern, MessageThread, PatientEvent, PatientMessage,
)
from .scheduling import propose_appointment_time, respond_to_appointment_proposal


class AvailabilityTestData:
    """Two practices, a doctor in both, a colleague, both admin roles and a patient."""

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



@override_settings(MULTI_PRACTICE_ENABLED=True)
class DoctorAvailabilityTests(AvailabilityTestData, TestCase):
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

    def test_only_the_doctor_or_a_practice_admin_can_add_or_cancel_days_off_and_cancellation_is_soft(self):
        beta_admin = get_user_model().objects.create_user(email='availability-beta-admin@example.test')
        CompanyMembership.objects.create(company=self.beta, user=beta_admin, role=CompanyMembership.Role.PRACTICE_ADMIN)
        for actor in (self.colleague, self.patient_user, beta_admin):
            with self.subTest(actor=actor.email):
                with self.assertRaises(PermissionDenied):
                    self.leave(actor=actor)
        leave = self.leave()
        for actor in (self.colleague, beta_admin):
            with self.assertRaises(PermissionDenied):
                cancel_time_off(time_off=leave, actor=actor)
        # Practice administrators manage days off for any doctor in their practice.
        for actor in (self.admin, self.super_admin):
            with self.subTest(actor=actor.email):
                period = self.leave(actor=actor, starts_at=self.at(13), ends_at=self.at(14))
                self.assertEqual((period.company_id, period.clinician_id), (self.company.pk, self.doctor.pk))
                cancel_time_off(time_off=period, actor=actor)
                period.refresh_from_db()
                self.assertEqual((period.is_active, period.cancelled_by_id), (False, actor.pk))
        DoctorTimeOff.objects.filter(starts_at=self.at(13)).delete()
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


@override_settings(MULTI_PRACTICE_ENABLED=True)
class SpecialDayPrecedenceTests(AvailabilityTestData, TestCase):
    """For a doctor on a date: a day off, then a special day, then the working pattern, then not working."""

    def special(self, day=None, starts=time(8), ends=time(10), actor=None):
        return set_special_day(company=self.company, clinician=self.doctor, actor=actor or self.doctor,
                               day=day or self.monday, starts_at=starts, ends_at=ends)

    def slot_starts(self, day=None):
        return [timezone.localtime(slot.starts_at, self.sast).time() for slot in
                open_slots_for_day(company=self.company, clinicians=[self.doctor], day=day or self.monday, limit=1000)]

    def test_special_day_narrows_or_widens_a_working_day_for_that_date_only(self):
        self.pattern()  # Monday to Friday, 09:00 to 12:00
        self.special(starts=time(8), ends=time(10))
        starts = self.slot_starts()
        self.assertEqual((starts[0], starts[-1]), (time(8), time(9, 30)))
        self.assert_allowed(self.at(8))
        with self.assertRaises(ValidationError):
            self.assert_allowed(self.at(10, 30))  # The pattern would allow it; the special day does not.
        tuesday = self.monday + timedelta(days=1)
        self.assertEqual(self.slot_starts(tuesday)[0], time(9))
        self.assert_allowed(self.at(10, 30, day=tuesday))
        # Setting the same date again replaces its hours.
        self.special(starts=time(7), ends=time(15))
        self.assertEqual(DoctorSpecialDay.objects.count(), 1)
        self.assert_allowed(self.at(14))
        self.assertEqual(self.slot_starts()[-1], time(14, 30))

    def test_special_day_on_a_non_working_weekday_makes_it_a_working_day(self):
        self.pattern()
        saturday = self.monday + timedelta(days=5)
        self.assertEqual(self.slot_starts(saturday), [])
        with self.assertRaises(ValidationError):
            self.assert_allowed(self.at(10, day=saturday))
        self.special(day=saturday, starts=time(10), ends=time(12))
        self.assertEqual(self.slot_starts(saturday)[0], time(10))
        self.assert_allowed(self.at(10, day=saturday))
        self.assertEqual(diary_day_states(company=self.company, clinician=self.doctor, first_day=saturday,
                                          last_day=saturday)[saturday]['kind'], 'special')

    def test_special_day_applies_before_any_pattern_is_saved(self):
        self.special(starts=time(8), ends=time(10))
        self.assertEqual(self.slot_starts()[0], time(8))
        with self.assertRaises(ValidationError):
            self.assert_allowed(self.at(11))

    def test_a_day_off_beats_a_special_day(self):
        self.pattern()
        self.special()
        create_days_off(company=self.company, clinician=self.doctor, actor=self.doctor,
                        first_day=self.monday, last_day=self.monday)
        self.assertEqual(self.slot_starts(), [])
        with self.assertRaises(ValidationError):
            self.assert_allowed(self.at(8))
        states = diary_day_states(company=self.company, clinician=self.doctor,
                                  first_day=self.monday, last_day=self.monday + timedelta(days=6))
        self.assertEqual([states[self.monday + timedelta(days=day)]['kind'] for day in range(7)],
                         ['off', 'work', 'work', 'work', 'work', 'not_working', 'not_working'])

    def test_days_off_cover_whole_sast_days_inclusive(self):
        period = create_days_off(company=self.company, clinician=self.doctor, actor=self.doctor,
                                 first_day=self.monday, last_day=self.monday + timedelta(days=2))
        self.assertEqual(period.starts_at, self.at(0))
        self.assertEqual(period.ends_at, self.at(0, day=self.monday + timedelta(days=3)))
        self.assertEqual(period.reason, '')
        with self.assertRaises(ValidationError):
            create_days_off(company=self.company, clinician=self.doctor, actor=self.doctor,
                            first_day=self.monday, last_day=self.monday - timedelta(days=1))

    def test_special_day_validation_and_permissions(self):
        with self.assertRaises(ValidationError):
            self.special(starts=time(10), ends=time(10))
        with self.assertRaises(ValidationError):
            self.special(day=timezone.localdate() - timedelta(days=1))
        for actor in (self.colleague, self.patient_user):
            with self.subTest(actor=actor.email):
                with self.assertRaises(PermissionDenied):
                    self.special(actor=actor)
        self.assertFalse(DoctorSpecialDay.objects.exists())
        special = self.special(actor=self.admin)
        self.assertEqual((special.clinician_id, special.created_by_id), (self.doctor.pk, self.admin.pk))
        with self.assertRaises(PermissionDenied):
            remove_special_day(special_day=special, actor=self.colleague)
        remove_special_day(special_day=special, actor=self.super_admin)
        self.assertFalse(DoctorSpecialDay.objects.exists())
        self.assertEqual(
            list(AuditEvent.objects.filter(action__startswith='availability.special_day').order_by('pk').values_list('action', flat=True)),
            ['availability.special_day_set', 'availability.special_day_removed'],
        )

    def test_bookings_outside_new_hours_are_flagged_not_cancelled_and_the_practice_gets_one_task(self):
        self.pattern()
        booking = self.appointment(starts_at=self.at(11))
        self.special(starts=time(8), ends=time(10))
        flagged = list(appointments_requiring_attention(company=self.company, clinician=self.doctor))
        self.assertEqual([item.pk for item in flagged], [booking.pk])
        self.assertEqual(attention_kind(flagged[0]), 'special_hours')
        booking.refresh_from_db()
        self.assertEqual((booking.status, booking.starts_at), (Appointment.Status.BOOKED, self.at(11)))
        task = ClinicalTask.objects.get()
        self.assertEqual((task.company_id, task.patient_id, task.created_by_id, task.assigned_to_id),
                         (self.company.pk, self.patient.pk, None, None))
        self.assertEqual(task.priority, ClinicalTask.Priority.HIGH)
        self.assertEqual(task.due_at, self.at(11))
        self.assertEqual(task.title, f"Offer new times: 1 booking clashes with {self.doctor.full_name}'s special hours on {self.monday.day} {self.monday:%b %Y}")
        self.assertIn('Alice Patient', task.description)
        # Saving the same hours again doesn't raise a second task.
        self.special(starts=time(8), ends=time(10))
        self.assertEqual(ClinicalTask.objects.count(), 1)

    def test_a_day_off_over_a_booking_flags_it_as_doctor_unavailable_and_raises_a_task(self):
        self.pattern()
        booking = self.appointment(starts_at=self.at(9))
        create_days_off(company=self.company, clinician=self.doctor, actor=self.doctor,
                        first_day=self.monday, last_day=self.monday)
        flagged = list(appointments_requiring_attention(company=self.company, clinician=self.doctor))
        self.assertEqual([attention_kind(item) for item in flagged], ['day_off'])
        booking.refresh_from_db()
        self.assertEqual(booking.status, Appointment.Status.BOOKED)
        self.assertEqual(ClinicalTask.objects.count(), 1)
        self.assertTrue(AuditEvent.objects.filter(action='availability.conflict_task_created').exists())

    def test_an_administrators_change_raises_no_task_and_a_clash_free_change_raises_none(self):
        self.pattern()
        self.appointment(starts_at=self.at(11))
        self.special(starts=time(8), ends=time(10), actor=self.admin)
        self.assertFalse(ClinicalTask.objects.exists())
        self.special(day=self.monday + timedelta(days=1), starts=time(8), ends=time(10))
        self.assertFalse(ClinicalTask.objects.exists())

    def test_undo_removes_that_dates_days_off_and_special_day_only(self):
        self.special()
        later = self.special(day=self.monday + timedelta(days=7))
        create_days_off(company=self.company, clinician=self.doctor, actor=self.doctor,
                        first_day=self.monday, last_day=self.monday + timedelta(days=1))
        other = create_days_off(company=self.company, clinician=self.doctor, actor=self.doctor,
                                first_day=self.monday + timedelta(days=3), last_day=self.monday + timedelta(days=3))
        with self.assertRaises(PermissionDenied):
            undo_diary_change(company=self.company, clinician=self.doctor, actor=self.colleague, day=self.monday)
        self.assertEqual(undo_diary_change(company=self.company, clinician=self.doctor, actor=self.doctor, day=self.monday), 2)
        self.assertEqual(list(DoctorSpecialDay.objects.values_list('pk', flat=True)), [later.pk])
        self.assertEqual(list(DoctorTimeOff.objects.filter(is_active=True).values_list('pk', flat=True)), [other.pk])
