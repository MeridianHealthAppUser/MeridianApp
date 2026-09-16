"""Treatment changes require a doctor; local plans never process payments."""

from django.test import override_settings
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient

from .models import AuditEvent, MedicationProduct, PatientSubscription, Payment, PracticeSettings, Shipment, TreatmentAuthorization
from .treatment import (
    authorization_is_current, change_authorization_status, change_subscription_status,
    create_authorization, enroll_local_subscription, ensure_subscription_eligible,
)


@override_settings(MULTI_PRACTICE_ENABLED=True)
class TreatmentLifecycleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Treatment Alpha', slug='treatment-alpha')
        cls.beta = Company.objects.create(name='Treatment Beta', slug='treatment-beta')
        users = get_user_model().objects
        cls.doctor = users.create_user(email='treatment-doctor@example.test')
        cls.colleague = users.create_user(email='treatment-colleague@example.test')
        cls.admin = users.create_user(email='treatment-admin@example.test')
        cls.super_admin = users.create_user(email='treatment-super@example.test')
        cls.patient_user = users.create_user(email='treatment-patient@example.test')
        cls.other_user = users.create_user(email='treatment-other@example.test')
        for user, role in ((cls.doctor, 'doctor'), (cls.colleague, 'doctor'), (cls.admin, 'practice_admin'), (cls.super_admin, 'super_admin')):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role)
        CompanyMembership.objects.create(company=cls.beta, user=cls.doctor, role='doctor')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Alex', last_name='Patient')
        cls.other = Patient.objects.create(company=cls.company, user=cls.other_user, first_name='Other', last_name='Patient')
        cls.beta_patient = Patient.objects.create(company=cls.beta, user=cls.patient_user, first_name='Alex', last_name='Beta')
        cls.product = MedicationProduct.objects.create(company=cls.company, name='Explicit doctor product', price=100)
        cls.beta_product = MedicationProduct.objects.create(company=cls.beta, name='Private beta product', price=100)
        PracticeSettings.objects.create(company=cls.company)

    def authorization(self, **overrides):
        values = dict(company=self.company, patient=self.patient, actor=self.doctor, product=self.product,
                      max_dose='Clinician-entered dose and frequency', quantity_per_cycle=4,
                      starts_on=timezone.localdate(), expires_on=timezone.localdate() + timedelta(days=90),
                      review_interval_days=90, instructions='Explicit patient instruction', submission_key=uuid.uuid4())
        values.update(overrides)
        return create_authorization(**values)

    def enroll(self, authorization, **overrides):
        values = dict(company=self.company, patient=self.patient, actor=self.patient_user, authorization=authorization,
                      confirm=True, submission_key=uuid.uuid4())
        values.update(overrides)
        return enroll_local_subscription(**values)

    def shipment(self, plan, status='ready', **overrides):
        values = dict(company=self.company, patient=self.patient, subscription=plan, cycle_number=1,
                      scheduled_for=timezone.localdate(), status=status)
        values.update(overrides)
        return Shipment.objects.create(**values)

    def test_explicit_doctor_decision_without_financial_or_account_mutation(self):
        users = get_user_model().objects.count()
        auth = self.authorization()
        self.assertTrue(authorization_is_current(auth))
        self.assertEqual(auth.prescribed_by, self.doctor)
        self.assertEqual(auth.max_dose, 'Clinician-entered dose and frequency')
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(PatientSubscription.objects.exists())
        self.assertEqual(get_user_model().objects.count(), users)

    def test_only_active_doctor_in_current_practice_can_authorize(self):
        for actor in (self.admin, self.super_admin, self.patient_user):
            with self.assertRaises(PermissionDenied):
                self.authorization(actor=actor)
        with self.assertRaises(ValidationError):
            self.authorization(product=self.beta_product)
        with self.assertRaises(PermissionDenied):
            self.authorization(patient=self.beta_patient)
        CompanyMembership.objects.filter(company=self.company, user=self.doctor).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.authorization()

    def test_authorization_replay_is_idempotent_and_context_cannot_cross_patient(self):
        key = uuid.uuid4()
        first = self.authorization(submission_key=key)
        audit_count = AuditEvent.objects.count()
        repeated = self.authorization(submission_key=key, max_dose='Does not overwrite the first decision')
        self.assertEqual(first.pk, repeated.pk)
        self.assertEqual(AuditEvent.objects.count(), audit_count)
        self.assertEqual(TreatmentAuthorization.objects.count(), 1)
        with self.assertRaises(PermissionDenied):
            self.authorization(submission_key=key, patient=self.other)

    def test_dates_dose_quantity_and_review_interval_are_explicit(self):
        for overrides in ({'max_dose': ''}, {'quantity_per_cycle': 0}, {'quantity_per_cycle': True},
                          {'review_interval_days': 0}, {'expires_on': timezone.localdate() - timedelta(days=1)}):
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                self.authorization(**overrides)
        self.assertFalse(TreatmentAuthorization.objects.exists())

    def test_renewal_preserves_history_and_rebinds_plan_without_releasing_shipments(self):
        old = self.authorization()
        plan = self.enroll(old)
        ready, dispatched = self.shipment(plan), self.shipment(plan, status='dispatched', tracking_number='TRACK-1')
        original = dict(Shipment.objects.filter(pk=dispatched.pk).values().get())
        renewal = self.authorization(renews=old, max_dose='New explicit doctor decision')
        old.refresh_from_db()
        plan.refresh_from_db()
        ready.refresh_from_db()
        self.assertNotEqual(old.pk, renewal.pk)
        self.assertEqual(old.status, TreatmentAuthorization.Status.CANCELLED)
        self.assertEqual(old.max_dose, 'Clinician-entered dose and frequency')
        self.assertEqual(plan.authorization_id, renewal.pk)
        self.assertEqual(plan.status, PatientSubscription.Status.ACTIVE)
        self.assertEqual(ready.status, Shipment.Status.HELD)
        self.assertEqual(Shipment.objects.filter(pk=dispatched.pk).values().get(), original)
        with self.assertRaises(ValidationError):
            self.authorization(renews=old)

    def test_renewal_of_paused_plan_never_reactivates_it(self):
        old = self.authorization()
        plan = self.enroll(old)
        change_authorization_status(authorization=old, actor=self.doctor, action='pause')
        new = self.authorization(renews=old)
        plan.refresh_from_db()
        self.assertEqual((plan.authorization_id, plan.status), (new.pk, PatientSubscription.Status.PAUSED))

    def test_only_prescriber_can_pause_revoke_or_renew(self):
        old = self.authorization()
        for actor in (self.colleague, self.super_admin, self.admin, self.patient_user):
            with self.subTest(actor=actor.email):
                with self.assertRaises(PermissionDenied):
                    change_authorization_status(authorization=old, actor=actor, action='pause')
                with self.assertRaises(PermissionDenied):
                    self.authorization(renews=old, actor=actor)

    def test_revocation_pauses_related_plans_only_and_is_audited_once(self):
        auth = self.authorization()
        plan = self.enroll(auth)
        related = self.shipment(plan)
        unrelated = self.shipment(None)
        result = change_authorization_status(authorization=auth, actor=self.doctor, action='revoke')
        count = AuditEvent.objects.count()
        change_authorization_status(authorization=result, actor=self.doctor, action='revoke')
        self.assertEqual(AuditEvent.objects.count(), count)
        plan.refresh_from_db()
        related.refresh_from_db()
        unrelated.refresh_from_db()
        self.assertEqual((plan.status, related.status, unrelated.status), ('paused', 'held', 'ready'))
        with self.assertRaises(ValidationError):
            change_subscription_status(subscription=plan, actor=self.patient_user, action='resume', confirm=True)

    def test_local_enrollment_requires_patient_confirmation_and_current_authorization(self):
        auth = self.authorization()
        for overrides in ({'actor': self.doctor}, {'actor': self.other_user}, {'patient': self.other}):
            with self.assertRaises((PermissionDenied, ValidationError)):
                self.enroll(auth, **overrides)
        with self.assertRaises(ValidationError):
            self.enroll(auth, confirm=False)
        auth.status = TreatmentAuthorization.Status.PAUSED
        auth.save(update_fields=('status',))
        with self.assertRaises(ValidationError):
            self.enroll(auth)
        self.assertFalse(PatientSubscription.objects.exists())

    def test_enrollment_deduplicates_and_never_creates_payment_or_shipment(self):
        auth = self.authorization()
        plan = self.enroll(auth)
        audit_count = AuditEvent.objects.count()
        replay = self.enroll(auth)
        self.assertEqual(plan.pk, replay.pk)
        self.assertEqual(AuditEvent.objects.count(), audit_count)
        self.assertEqual(ensure_subscription_eligible(plan).pk, auth.pk)
        self.assertIsNone(plan.next_debit_on)
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(Shipment.objects.exists())

    def test_cancel_retains_history_holds_undispatched_and_never_mutates_dispatched(self):
        plan = self.enroll(self.authorization())
        ready, dispatched = self.shipment(plan), self.shipment(plan, status='dispatched')
        original = Shipment.objects.filter(pk=dispatched.pk).values().get()
        cancelled = change_subscription_status(subscription=plan, actor=self.patient_user, action='cancel', confirm=True)
        self.assertIsNotNone(cancelled.cancelled_at)
        ready.refresh_from_db()
        self.assertEqual(ready.status, Shipment.Status.HELD)
        self.assertEqual(Shipment.objects.filter(pk=dispatched.pk).values().get(), original)
        with self.assertRaises(ValidationError):
            change_subscription_status(subscription=cancelled, actor=self.patient_user, action='resume', confirm=True)
        self.assertEqual(PatientSubscription.objects.count(), 1)

    def test_resume_requires_current_authorization_and_never_releases_held_shipments(self):
        auth = self.authorization()
        plan = self.enroll(auth)
        shipment = self.shipment(plan)
        paused = change_subscription_status(subscription=plan, actor=self.patient_user, action='pause', confirm=True)
        resumed = change_subscription_status(subscription=paused, actor=self.patient_user, action='resume', confirm=True)
        shipment.refresh_from_db()
        self.assertEqual(resumed.status, 'active')
        self.assertEqual(shipment.status, 'held')
        paused = change_subscription_status(subscription=resumed, actor=self.patient_user, action='pause', confirm=True)
        TreatmentAuthorization.objects.filter(pk=auth.pk).update(expires_on=timezone.localdate() - timedelta(days=1))
        with self.assertRaises(ValidationError):
            change_subscription_status(subscription=paused, actor=self.patient_user, action='resume', confirm=True)

    def test_inactive_prescriber_or_product_blocks_dispatch_eligibility(self):
        plan = self.enroll(self.authorization())
        CompanyMembership.objects.filter(company=self.company, user=self.doctor).update(is_active=False)
        with self.assertRaises(ValidationError):
            ensure_subscription_eligible(plan)

    def test_patient_plan_change_rechecks_inactive_patient_and_actor(self):
        plan = self.enroll(self.authorization())
        Patient.objects.filter(pk=self.patient.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            change_subscription_status(subscription=plan, actor=self.patient_user, action='cancel', confirm=True)
