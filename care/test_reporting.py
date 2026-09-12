"""Saved-data metrics and activity statements: no inferred outcomes or payouts."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient
from .models import (Appointment, AuditEvent, DoctorActivityStatement, LabRequest, Lead, MedicationBatch,
                     MessageThread, PatientMessage, PatientSubscription, Payment, WeightEntry)
from .reporting import (approve_activity_statement, create_activity_statement, doctor_activity,
                        operational_metrics, period_bounds, refresh_activity_statement, report_companies)
from .test_operations import OperationsFixture

SAST = ZoneInfo('Africa/Johannesburg')
RATES = {'initial': '100.00', 'review': '50.00', 'follow_up': '25.00', 'messages': '2.00'}


class ReportingFixture(OperationsFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.start, cls.end = cls.today - timedelta(days=30), cls.today - timedelta(days=1)
        cls.other_doctor = get_user_model().objects.create_user('reporting-other-doctor@example.test')
        CompanyMembership.objects.create(company=cls.company, user=cls.other_doctor, role='doctor')

    def at(self, day, hour=12):
        return datetime.combine(day, time(hour=hour), tzinfo=SAST)

    def appointment(self, **extra):
        values = dict(company=self.company, patient=self.patient, clinician=self.doctor,
                      appointment_type='initial', status='completed', starts_at=self.at(self.start + timedelta(days=1)))
        values.update(extra)
        return Appointment.objects.create(**values)

    def message(self, *, when=None, company=None, patient=None, sender=None):
        company, patient = company or self.company, patient or self.patient
        thread = MessageThread.objects.create(company=company, patient=patient, subject='Private conversation')
        message = PatientMessage.objects.create(company=company, thread=thread, sender=sender or self.doctor,
                                                body='Private clinical text must never be exported in an aggregate.')
        PatientMessage.objects.filter(pk=message.pk).update(created_at=when or self.at(self.start + timedelta(days=1)))
        message.refresh_from_db()
        return message

    def statement(self, **extra):
        values = dict(company=self.company, actor=self.super_admin, doctor=self.doctor,
                      start=self.start, end=self.end, rates=RATES)
        values.update(extra)
        return create_activity_statement(**values)

    def metrics(self, **extra):
        values = dict(actor=self.admin, company=self.company, scope='current', start=self.start, end=self.end)
        values.update(extra)
        return operational_metrics(**values)


class ReportCalculationTests(ReportingFixture):
    def test_period_is_inclusive_sast_and_rejects_malformed_or_overflowing_dates(self):
        with timezone.override('UTC'):
            lower, upper = period_bounds(self.start, self.end)
        self.assertEqual(lower.utcoffset(), timedelta(hours=2))
        self.assertEqual((lower.date(), upper.date()), (self.start, self.end + timedelta(days=1)))
        for start, end in ((None, self.end), ('bad', 'date'), (self.end, self.start),
                           (date.max, date.max), (date.min, date.min),
                           (self.start, self.start + timedelta(days=367)), (self.at(self.start), self.at(self.end))):
            with self.subTest(start=start, end=end), self.assertRaises(ValidationError):
                period_bounds(start, end)

    def test_all_scope_includes_only_active_memberships_and_leads_only_for_administered_practices(self):
        CompanyMembership.objects.create(company=self.beta, user=self.admin, role='doctor')
        hidden = Company.objects.create(name='Hidden reports', slug='hidden-reports')
        CompanyMembership.objects.create(company=hidden, user=self.admin, role='super_admin', is_active=False)
        for company in (self.company, self.beta, hidden):
            lead = Lead.objects.create(company=company, first_name='Count', last_name='Only', email='count@example.test')
            Lead.objects.filter(pk=lead.pk).update(created_at=self.at(self.start))
        report = self.metrics(scope='all')
        self.assertEqual(set(report['company_ids']), {self.company.pk, self.beta.pk})
        self.assertEqual(dict(report['metrics'])['New enquiries in practices you administer'], 1)
        with self.assertRaises(PermissionDenied):
            report_companies(self.admin, hidden, 'all')
        with self.assertRaises(ValidationError):
            self.metrics(scope='everything')
        self.assertNotIn('New enquiries in practices you administer', dict(self.metrics(actor=self.doctor)['metrics']))

    def test_active_identity_and_membership_are_rechecked_for_reporting(self):
        get_user_model().objects.filter(pk=self.admin.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.metrics()
        with self.assertRaises(PermissionDenied):
            self.metrics(actor=self.patient_user)

    def test_new_patient_record_count_matches_cohorts_even_when_record_is_now_inactive(self):
        Patient.objects.filter(company=self.company).update(created_at=self.at(self.start - timedelta(days=1)))
        active = Patient.objects.create(company=self.company, first_name='New', last_name='Active')
        inactive = Patient.objects.create(company=self.company, first_name='New', last_name='Inactive', is_active=False)
        Patient.objects.filter(pk__in=(active.pk, inactive.pk)).update(created_at=self.at(self.start))
        report = self.metrics()
        self.assertEqual(dict(report['metrics'])['New patient records in period'], 2)
        self.assertEqual(sum(row['records'] for row in report['cohorts']), 2)

    def test_paired_weight_change_is_mean_of_individual_percentages_not_pooled_weight(self):
        for patient, first, last in ((self.patient, '100', '90'), (self.other_patient, '200', '220')):
            WeightEntry.objects.create(company=self.company, patient=patient, recorded_on=self.start, weight_kg=first)
            WeightEntry.objects.create(company=self.company, patient=patient, recorded_on=self.end, weight_kg=last)
        WeightEntry.objects.create(company=self.beta, patient=self.beta_patient, recorded_on=self.start, weight_kg=100)
        WeightEntry.objects.create(company=self.beta, patient=self.beta_patient, recorded_on=self.end, weight_kg=50)
        stats = self.metrics()['weight_stats']
        self.assertEqual(stats['count'], 2)
        self.assertAlmostEqual(stats['mean'], 0.0)

    def test_single_measurement_inactive_record_and_invalid_nonpositive_weights_are_not_paired(self):
        WeightEntry.objects.create(company=self.company, patient=self.patient, recorded_on=self.start, weight_kg=100)
        WeightEntry.objects.create(company=self.company, patient=self.patient, recorded_on=self.end, weight_kg=-5)
        WeightEntry.objects.create(company=self.company, patient=self.other_patient, recorded_on=self.start, weight_kg=100)
        stats = self.metrics()['weight_stats']
        self.assertEqual(stats['count'], 0)
        self.assertIsNone(stats['mean'])
        WeightEntry.objects.filter(patient=self.patient, recorded_on=self.end).update(weight_kg=90)
        Patient.objects.filter(pk=self.patient.pk).update(is_active=False)
        self.assertEqual(self.metrics()['weight_stats']['count'], 0)

    def test_metric_date_edges_include_midnight_start_but_not_next_midnight(self):
        lower, upper = period_bounds(self.start, self.end)
        self.appointment(starts_at=lower)
        self.appointment(starts_at=upper - timedelta(microseconds=1))
        self.appointment(starts_at=lower - timedelta(microseconds=1))
        self.appointment(starts_at=upper)
        self.assertEqual(dict(self.metrics()['metrics'])['Completed appointments in period'], 2)

    def test_available_stock_includes_low_batches_but_not_expired_or_unsafe_batches(self):
        self.receive(quantity=2)
        low = self.receive(quantity=3)
        MedicationBatch.objects.filter(pk=low.pk).update(status='low')
        expired = self.receive(quantity=7)
        MedicationBatch.objects.filter(pk=expired.pk).update(expires_on=self.today)
        unsafe = self.receive(quantity=11)
        MedicationBatch.objects.filter(pk=unsafe.pk).update(cold_chain_confirmed=False)
        self.receive(quantity=13, cold_chain_confirmed=False)
        self.assertEqual(dict(self.metrics()['metrics'])['Unexpired available stock units now'], 5)

    def test_malformed_cross_practice_imports_do_not_enter_counts_or_activity(self):
        self.appointment(patient=self.beta_patient)
        LabRequest.objects.create(company=self.company, patient=self.beta_patient, requested_by=self.doctor, panel_name='Foreign lab')
        LabRequest.objects.filter(patient=self.beta_patient).update(created_at=self.at(self.start))
        PatientSubscription.objects.create(company=self.company, patient=self.beta_patient, plan_name='Wrong tenant', monthly_amount=1)
        self.message(patient=self.beta_patient)
        report = dict(self.metrics()['metrics'])
        self.assertEqual(report['Completed appointments in period'], 0)
        self.assertEqual(report['Lab requests created in period'], 0)
        self.assertEqual(report['Active local plans now'], 1)
        counts, _ = doctor_activity(company=self.company, doctor=self.doctor, start=self.start, end=self.end)
        self.assertEqual(counts['initial'], 0)
        self.assertEqual(counts['messages'], 0)

    def test_metric_query_count_does_not_grow_per_patient(self):
        with CaptureQueriesContext(connection) as small:
            result = self.metrics()
            list(result['practices'])
        for number in range(25):
            patient = Patient.objects.create(company=self.company, first_name='Extra', last_name=str(number))
            WeightEntry.objects.create(company=self.company, patient=patient, recorded_on=self.start, weight_kg=100)
            WeightEntry.objects.create(company=self.company, patient=patient, recorded_on=self.end, weight_kg=95)
        with CaptureQueriesContext(connection) as larger:
            result = self.metrics()
            list(result['practices'])
        self.assertEqual(len(larger), len(small))


class ActivityStatementTests(ReportingFixture):
    def test_counts_only_completed_owned_appointments_and_doctor_messages_with_explicit_decimal_rates(self):
        self.appointment()
        self.appointment(appointment_type='review')
        self.appointment(appointment_type='follow_up')
        self.appointment(appointment_type='ad_hoc')
        for status in ('booked', 'cancelled', 'no_show'):
            self.appointment(status=status)
        self.appointment(clinician=self.other_doctor)
        self.appointment(company=self.beta, patient=self.beta_patient)
        self.message()
        self.message()
        self.message(sender=self.patient_user)
        self.message(company=self.beta, patient=self.beta_patient)
        statement = self.statement()
        self.assertEqual(statement.counts, {'initial': 1, 'review': 1, 'follow_up': 2, 'messages': 2})
        self.assertEqual(statement.amount, Decimal('204.00'))
        self.assertEqual(statement.rates, RATES)
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_statements_are_super_admin_only_and_doctor_must_belong_to_same_practice(self):
        for actor in (self.admin, self.doctor, self.patient_user, self.beta_admin):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                self.statement(actor=actor)
        with self.assertRaises(ValidationError):
            self.statement(doctor=self.beta_admin)
        self.assertFalse(DoctorActivityStatement.objects.exists())

    def test_rates_must_all_be_explicit_finite_nonnegative_and_at_most_two_decimals(self):
        for value in ('-1', '100001', '1.001', 'NaN', 'Infinity', True, None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.statement(rates={**RATES, 'initial': value})
        with self.assertRaises(ValidationError):
            self.statement(rates={'initial': 1})
        with self.assertRaises(ValidationError):
            self.statement(rates=None)
        self.assertFalse(DoctorActivityStatement.objects.exists())

    def test_only_past_nonoverlapping_periods_can_be_prepared_but_adjacent_periods_work(self):
        with self.assertRaises(ValidationError):
            self.statement(end=self.today)
        first = self.statement()
        for start, end in ((self.start, self.end), (self.end, self.end),
                           (self.start - timedelta(days=1), self.start)):
            with self.subTest(start=start), self.assertRaises(ValidationError):
                self.statement(start=start, end=end)
        second = self.statement(start=self.start - timedelta(days=10), end=self.start - timedelta(days=1))
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(DoctorActivityStatement.objects.count(), 2)

    def test_changed_source_requires_refresh_before_approval_and_refresh_keeps_original_rates(self):
        self.appointment()
        statement = self.statement()
        self.appointment(appointment_type='review')
        with self.assertRaises(ValidationError):
            approve_activity_statement(statement=statement, actor=self.super_admin,
                expected_updated=statement.updated_at.isoformat(), confirm=True)
        refreshed = refresh_activity_statement(statement=statement, actor=self.super_admin,
                                                expected_updated=statement.updated_at.isoformat())
        self.assertEqual(refreshed.rates, RATES)
        self.assertEqual(refreshed.amount, Decimal('150.00'))
        approved = approve_activity_statement(statement=refreshed, actor=self.super_admin,
            expected_updated=refreshed.updated_at.isoformat(), confirm=True)
        self.assertIsNotNone(approved.approved_at)

    def test_approved_snapshot_is_immutable_and_replay_adds_no_audit_or_payment(self):
        appointment = self.appointment()
        statement = self.statement()
        approved = approve_activity_statement(statement=statement, actor=self.super_admin,
            expected_updated=statement.updated_at.isoformat(), confirm=True)
        snapshot = (approved.counts.copy(), approved.rates.copy(), approved.source_ids.copy(), approved.amount)
        Appointment.objects.filter(pk=appointment.pk).update(status='cancelled')
        count = AuditEvent.objects.count()
        repeated = approve_activity_statement(statement=approved, actor=self.super_admin,
            expected_updated='old', confirm=True)
        self.assertEqual(repeated.pk, approved.pk)
        self.assertEqual(AuditEvent.objects.count(), count)
        with self.assertRaises(ValidationError):
            refresh_activity_statement(statement=approved, actor=self.super_admin,
                                       expected_updated=approved.updated_at.isoformat())
        approved.refresh_from_db()
        self.assertEqual((approved.counts, approved.rates, approved.source_ids, approved.amount), snapshot)
        self.assertFalse(Payment.objects.exists())

    def test_approval_requires_confirmation_and_stale_version_cannot_refresh(self):
        statement = self.statement()
        for confirm in (False, 'yes'):
            with self.assertRaises(ValidationError):
                approve_activity_statement(statement=statement, actor=self.super_admin,
                    expected_updated=statement.updated_at.isoformat(), confirm=confirm)
        with self.assertRaises(ValidationError):
            refresh_activity_statement(statement=statement, actor=self.super_admin, expected_updated='stale')
        statement.refresh_from_db()
        self.assertIsNone(statement.approved_at)

    def test_malformed_imported_rate_or_mismatched_total_cannot_be_approved(self):
        self.appointment()
        statement = self.statement()
        DoctorActivityStatement.objects.filter(pk=statement.pk).update(amount=Decimal('999.00'))
        statement.refresh_from_db()
        with self.assertRaises(ValidationError):
            approve_activity_statement(statement=statement, actor=self.super_admin,
                expected_updated=statement.updated_at.isoformat(), confirm=True)
        DoctorActivityStatement.objects.filter(pk=statement.pk).update(rates={'initial': 'invalid'})
        statement.refresh_from_db()
        with self.assertRaises(ValidationError):
            refresh_activity_statement(statement=statement, actor=self.super_admin,
                                       expected_updated=statement.updated_at.isoformat())
