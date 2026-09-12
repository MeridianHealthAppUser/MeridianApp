"""Inventory conservation, clinical gates and tenant boundaries for local dispatch."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Sum
from django.test import TestCase
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient
from .models import (AuditEvent, MedicationBatch, MedicationProduct, PatientSubscription, Payment,
                     PracticeSettings, Shipment, ShipmentItem, StockMovement, TreatmentAuthorization)
from .operations import (allowance_window, change_batch, dispatch_shipment, hold_or_cancel_shipment,
    lock_shipping_week, mark_delivered, prepare_shipment, product_allowance, receive_stock, shipment_hold_reason)


class OperationsFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.company = Company.objects.create(name='Operations Alpha', slug='ops-alpha')
        cls.beta = Company.objects.create(name='Operations Beta', slug='ops-beta')
        users = get_user_model().objects
        cls.admin = users.create_user('ops-admin@example.test')
        cls.super_admin = users.create_user('ops-super@example.test')
        cls.doctor = users.create_user('ops-doctor@example.test')
        cls.patient_user = users.create_user('ops-patient@example.test')
        cls.other_user = users.create_user('ops-other@example.test')
        cls.beta_admin = users.create_user('ops-beta-admin@example.test')
        for actor, role in ((cls.admin, 'practice_admin'), (cls.super_admin, 'super_admin'), (cls.doctor, 'doctor')):
            CompanyMembership.objects.create(company=cls.company, user=actor, role=role)
        CompanyMembership.objects.create(company=cls.beta, user=cls.beta_admin, role='practice_admin')
        CompanyMembership.objects.create(company=cls.beta, user=cls.doctor, role='doctor')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Alpha', last_name='Patient')
        cls.other_patient = Patient.objects.create(company=cls.company, user=cls.other_user, first_name='Other', last_name='Patient')
        cls.beta_patient = Patient.objects.create(company=cls.beta, user=cls.patient_user, first_name='Beta', last_name='Patient')
        cls.product = MedicationProduct.objects.create(company=cls.company, name='Explicit prescribed product', strength='Dose A', price=Decimal('100.00'))
        cls.open_product = MedicationProduct.objects.create(company=cls.company, name='Open local product', price=Decimal('40.00'), requires_authorisation=False, requires_cold_chain=False)
        cls.beta_product = MedicationProduct.objects.create(company=cls.beta, name='Private beta product', price=Decimal('60.00'))
        cls.auth = TreatmentAuthorization.objects.create(company=cls.company, patient=cls.patient,
            product=cls.product, prescribed_by=cls.doctor, max_dose='Explicit physician dose', quantity_per_cycle=4,
            starts_on=cls.today - timedelta(days=10), expires_on=cls.today + timedelta(days=180))
        cls.plan = PatientSubscription.objects.create(company=cls.company, patient=cls.patient, authorization=cls.auth,
            plan_name='Local non-payment plan', monthly_amount=100, starts_on=cls.today - timedelta(days=5))
        PracticeSettings.objects.create(company=cls.company)

    def receive(self, **extra):
        values = dict(company=self.company, actor=self.admin, product=self.product,
                      batch_number=f'BATCH-{MedicationBatch.objects.count() + 1}', received_on=self.today,
                      expires_on=self.today + timedelta(days=90), quantity=10, cold_chain_confirmed=True)
        values.update(extra)
        return receive_stock(**values)

    def shipment(self, *, product=None, quantity=4, **extra):
        values = dict(company=self.company, patient=self.patient, subscription=self.plan,
                      scheduled_for=self.today, status='draft')
        values.update(extra)
        shipment = Shipment.objects.create(**values)
        product = product or self.product
        ShipmentItem.objects.create(company=shipment.company, shipment=shipment, product=product,
            quantity=quantity, dose=self.auth.max_dose if product.pk == self.product.pk else product.strength)
        return shipment

    def ready(self, shipment=None):
        return prepare_shipment(shipment=shipment or self.shipment(), actor=self.admin)

    def locked(self, shipment=None):
        shipment = self.ready(shipment)
        lock_shipping_week(company=self.company, actor=self.admin, week_start=shipment.scheduled_for)
        shipment.refresh_from_db()
        return shipment

    def assert_stock(self, batch, quantity):
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_on_hand, quantity)
        self.assertEqual(batch.movements.aggregate(total=Sum('quantity'))['total'], quantity)


class StockOperationsTests(OperationsFixture):
    def test_receipt_creates_one_scoped_ledger_entry_and_replay_does_not_double_stock(self):
        batch = self.receive(submission_key='receipt-1')
        replay = self.receive(submission_key='receipt-1', quantity=99)
        self.assertEqual(batch.pk, replay.pk)
        self.assertEqual(MedicationBatch.objects.count(), 1)
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action='stock.received').count(), 1)
        self.assert_stock(batch, 10)

    def test_doctor_patient_foreign_admin_and_revoked_admin_cannot_write_stock(self):
        for actor in (self.doctor, self.patient_user, self.beta_admin):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                self.receive(actor=actor)
        CompanyMembership.objects.filter(company=self.company, user=self.admin).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.receive()
        self.assertEqual(MedicationBatch.objects.count(), 0)

    def test_inactive_or_foreign_products_and_bad_dates_or_quantities_are_rejected(self):
        for values in ({'product': self.beta_product}, {'received_on': self.today + timedelta(days=1)},
                       {'expires_on': self.today}, {'quantity': 0}, {'quantity': -1}, {'quantity': True}, {'quantity': 100001}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.receive(**values)
        MedicationProduct.objects.filter(pk=self.product.pk).update(is_active=False)
        with self.assertRaises(ValidationError):
            self.receive()
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_cold_chain_receipt_is_quarantined_until_explicit_safe_release(self):
        batch = self.receive(cold_chain_confirmed=False)
        self.assertEqual(batch.status, 'quarantined')
        with self.assertRaises(ValidationError):
            change_batch(batch=batch, actor=self.admin, action='release', reason='Reviewed')
        batch = change_batch(batch=batch, actor=self.admin, action='release', reason='Reviewed temperature record', cold_chain_confirmed=True)
        self.assertEqual(batch.status, 'available')
        self.assertTrue(batch.cold_chain_confirmed)
        self.assert_stock(batch, 10)

    def test_negative_and_over_received_adjustments_do_not_change_balance_or_history(self):
        batch = self.receive()
        for quantity in (-11, 1, 0, True):
            with self.subTest(quantity=quantity), self.assertRaises(ValidationError):
                change_batch(batch=batch, actor=self.admin, action='adjust', reason='Counted', quantity=quantity)
        self.assert_stock(batch, 10)
        self.assertEqual(batch.movements.count(), 1)
        change_batch(batch=batch, actor=self.admin, action='adjust', reason='Damaged unit', quantity=-1)
        self.assert_stock(batch, 9)

    def test_adjustment_cannot_reintroduce_units_already_allocated_to_an_undispatched_parcel(self):
        batch = self.receive()
        shipment = self.ready()
        with self.assertRaises(ValidationError):
            change_batch(batch=batch, actor=self.admin, action='adjust', reason='Incorrect double count', quantity=4)
        self.assert_stock(batch, 6)
        hold_or_cancel_shipment(shipment=shipment, actor=self.admin, cancel=True, reason='Cancelled')
        self.assert_stock(batch, 10)

    def test_quarantine_holds_only_undispatched_allocations_and_can_be_reprepared_elsewhere(self):
        early = self.receive(expires_on=self.today + timedelta(days=45))
        replacement = self.receive(expires_on=self.today + timedelta(days=90))
        shipment = self.locked()
        change_batch(batch=early, actor=self.admin, action='quarantine', reason='Temperature review')
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, 'held')
        self.assertIsNone(shipment.locked_at)
        prepared = prepare_shipment(shipment=shipment, actor=self.admin)
        self.assertEqual(prepared.items.get().batch, replacement)
        self.assert_stock(early, 10)
        self.assert_stock(replacement, 6)

    def test_writeoff_is_terminal_idempotent_and_allocated_units_never_return_to_stock(self):
        batch = self.receive()
        shipment = self.ready()
        written = change_batch(batch=batch, actor=self.admin, action='write_off', reason='Destroyed')
        self.assertEqual(written.status, 'written_off')
        self.assert_stock(batch, 0)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, 'held')
        self.assertIsNone(shipment.items.get().batch_id)
        count = AuditEvent.objects.count()
        change_batch(batch=batch, actor=self.admin, action='write_off', reason='Replay')
        self.assertEqual(AuditEvent.objects.count(), count)
        for action in ('release', 'adjust', 'quarantine'):
            with self.subTest(action=action), self.assertRaises(ValidationError):
                change_batch(batch=batch, actor=self.admin, action=action, reason='Cannot restore', quantity=1, cold_chain_confirmed=True)
        hold_or_cancel_shipment(shipment=shipment, actor=self.admin, cancel=True, reason='Cancel empty allocation')
        self.assert_stock(batch, 0)

    def test_foreign_actor_cannot_adjust_or_view_batch_by_direct_mutator(self):
        batch = self.receive()
        for action in ('release', 'quarantine', 'adjust', 'write_off'):
            with self.subTest(action=action), self.assertRaises(PermissionDenied):
                change_batch(batch=batch, actor=self.beta_admin, action=action, reason='Foreign', quantity=-1)
        self.assert_stock(batch, 10)

    def test_writeoff_never_mutates_cross_practice_shipment_even_with_corrupt_foreign_batch_link(self):
        batch = self.receive()
        foreign = self.shipment(company=self.beta, patient=self.beta_patient, product=self.beta_product,
                                subscription=None, quantity=1)
        foreign.items.update(batch=batch)
        before = list(foreign.items.values())
        change_batch(batch=batch, actor=self.admin, action='write_off', reason='Destroy local batch')
        self.assertEqual(list(foreign.items.values()), before)
        foreign.refresh_from_db()
        self.assertEqual(foreign.status, 'draft')
        self.assertFalse(AuditEvent.objects.filter(company=self.beta).exists())


class ShipmentOperationsTests(OperationsFixture):
    def test_prepare_allocates_earliest_eligible_expiry_across_batches_skipping_short_lived_stock(self):
        too_early = self.receive(expires_on=self.today + timedelta(days=28), quantity=20)
        early = self.receive(expires_on=self.today + timedelta(days=40), quantity=2)
        late = self.receive(expires_on=self.today + timedelta(days=90), quantity=5)
        cold = self.receive(expires_on=self.today + timedelta(days=35), quantity=20, cold_chain_confirmed=False)
        shipment = self.ready()
        self.assertEqual(set(shipment.items.values_list('batch_id', 'quantity')), {(early.pk, 2), (late.pk, 2)})
        self.assert_stock(too_early, 20)
        self.assert_stock(cold, 20)
        self.assert_stock(early, 0)
        self.assert_stock(late, 3)

    def test_insufficient_second_product_rolls_back_all_prior_allocations_and_line_rebuild(self):
        batch = self.receive()
        shipment = self.shipment()
        second = ShipmentItem.objects.create(company=self.company, shipment=shipment, product=self.open_product, quantity=1, dose='')
        original = list(shipment.items.order_by('pk').values())
        counts = (AuditEvent.objects.count(), StockMovement.objects.count())
        with self.assertRaises(ValidationError):
            self.ready(shipment)
        self.assert_stock(batch, 10)
        self.assertEqual(list(shipment.items.order_by('pk').values()), original)
        self.assertEqual((AuditEvent.objects.count(), StockMovement.objects.count()), counts)
        self.assertTrue(ShipmentItem.objects.filter(pk=second.pk).exists())

    def test_reprepare_and_cancel_preserve_stock_conservation(self):
        batch = self.receive()
        shipment = self.ready()
        self.assert_stock(batch, 6)
        shipment = self.ready(shipment)
        self.assert_stock(batch, 6)
        self.assertEqual(shipment.items.aggregate(total=Sum('quantity'))['total'], 4)
        hold_or_cancel_shipment(shipment=shipment, actor=self.admin, cancel=True, reason='Patient cancelled')
        self.assert_stock(batch, 10)
        self.assertIsNone(shipment.items.get().batch_id)
        self.assertFalse(Payment.objects.exists())

    def test_dispatch_requires_ready_lock_confirmation_and_current_eligible_batch(self):
        batch = self.receive()
        shipment = self.shipment()
        with self.assertRaises(ValidationError):
            dispatch_shipment(shipment=shipment, actor=self.admin, tracking_number='REF-1', confirm=True)
        shipment = self.ready(shipment)
        with self.assertRaises(ValidationError):
            dispatch_shipment(shipment=shipment, actor=self.admin, tracking_number='REF-1', confirm=True)
        lock_shipping_week(company=self.company, actor=self.admin, week_start=self.today)
        with self.assertRaises(ValidationError):
            dispatch_shipment(shipment=shipment, actor=self.admin, tracking_number='REF-1')
        MedicationBatch.objects.filter(pk=batch.pk).update(cold_chain_confirmed=False)
        with self.assertRaises(ValidationError):
            dispatch_shipment(shipment=shipment, actor=self.admin, tracking_number='REF-1', confirm=True)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, 'ready')

    def test_dispatch_replay_does_not_decrement_again_and_snapshot_stays_immutable(self):
        batch = self.receive()
        shipment = self.locked()
        dispatched = dispatch_shipment(shipment=shipment, actor=self.admin, tracking_number='REF-1', confirm=True)
        snapshot = dispatched.dispatch_snapshot
        movements, audits = StockMovement.objects.count(), AuditEvent.objects.count()
        repeated = dispatch_shipment(shipment=dispatched, actor=self.admin, tracking_number='REF-1', confirm=True)
        self.assertEqual(repeated.pk, dispatched.pk)
        self.assertEqual((StockMovement.objects.count(), AuditEvent.objects.count()), (movements, audits))
        self.assert_stock(batch, 6)
        self.product.name = 'Later catalogue name'
        self.product.save()
        delivered = mark_delivered(shipment=shipment, actor=self.admin, confirm=True)
        self.assertEqual(delivered.dispatch_snapshot, snapshot)
        self.assertEqual(delivered.status, 'delivered')
        self.assertFalse(Payment.objects.exists())

    def test_dispatched_lines_and_tracking_cannot_be_changed_and_batch_writeoff_preserves_history(self):
        batch = self.receive()
        shipment = dispatch_shipment(shipment=self.locked(), actor=self.admin, tracking_number='REF-1', confirm=True)
        lines = list(shipment.items.values())
        for call in (lambda: prepare_shipment(shipment=shipment, actor=self.admin),
                     lambda: hold_or_cancel_shipment(shipment=shipment, actor=self.admin, cancel=True, reason='Too late'),
                     lambda: dispatch_shipment(shipment=shipment, actor=self.admin, tracking_number='Changed', confirm=True)):
            with self.assertRaises(ValidationError):
                call()
        change_batch(batch=batch, actor=self.admin, action='write_off', reason='Remaining unused stock destroyed')
        self.assertEqual(list(shipment.items.values()), lines)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, 'dispatched')

    def test_week_lock_is_atomic_and_scoped_and_replay_does_not_duplicate_audits(self):
        self.receive(quantity=20)
        first = self.ready(self.shipment(quantity=2))
        second = self.ready(self.shipment(quantity=2))
        second.items.update(batch=None)
        with self.assertRaises(ValidationError):
            lock_shipping_week(company=self.company, actor=self.admin, week_start=self.today)
        first.refresh_from_db()
        self.assertIsNone(first.locked_at)
        second.status = 'held'
        second.save()
        lock_shipping_week(company=self.company, actor=self.admin, week_start=self.today)
        count = AuditEvent.objects.filter(action='shipment.locked').count()
        lock_shipping_week(company=self.company, actor=self.admin, week_start=self.today)
        self.assertEqual(AuditEvent.objects.filter(action='shipment.locked').count(), count)

    def test_inactive_patient_or_paused_subscription_or_wrong_dose_blocks_preparation(self):
        self.receive()
        shipment = self.shipment()
        ShipmentItem.objects.filter(shipment=shipment).update(dose='Not the authorized dose')
        with self.assertRaises(ValidationError):
            self.ready(shipment)
        shipment.items.update(dose=self.auth.max_dose)
        PatientSubscription.objects.filter(pk=self.plan.pk).update(status='paused')
        with self.assertRaises(ValidationError):
            self.ready(shipment)
        PatientSubscription.objects.filter(pk=self.plan.pk).update(status='active')
        Patient.objects.filter(pk=self.patient.pk).update(is_active=False)
        with self.assertRaises(ValidationError):
            self.ready(shipment)

    def test_explicit_product_auth_and_shared_group_cap_never_infer_permission_for_other_strength(self):
        self.product.allowance_group = 'shared-clinical-cap'
        self.product.save()
        second = MedicationProduct.objects.create(company=self.company, name='Second strength', strength='Dose B',
                                                  price=100, allowance_group='shared-clinical-cap')
        self.assertEqual(product_allowance(self.patient, second)[1], 0)
        TreatmentAuthorization.objects.create(company=self.company, patient=self.patient, product=second,
            prescribed_by=self.doctor, max_dose='Second explicit dose', quantity_per_cycle=3,
            starts_on=self.today, expires_on=self.today + timedelta(days=90))
        self.shipment(quantity=2)
        self.assertEqual(product_allowance(self.patient, second)[1], 1)
        self.assertEqual(product_allowance(self.patient, self.product)[1], 1)

    def test_allowance_windows_anchor_to_plan_and_cancelled_or_foreign_shipments_do_not_consume(self):
        start, end = allowance_window(self.patient, self.auth, self.today)
        self.assertEqual((start, end), (self.plan.starts_on, self.plan.starts_on + timedelta(days=27)))
        self.shipment(quantity=4, status='cancelled')
        self.shipment(quantity=4, scheduled_for=end + timedelta(days=1))
        self.assertEqual(product_allowance(self.patient, self.product)[1], 4)
        self.shipment(quantity=1, scheduled_for=start)
        self.assertEqual(product_allowance(self.patient, self.product)[1], 3)

    def test_doctor_and_foreign_admin_cannot_prepare_hold_lock_dispatch_or_deliver(self):
        self.receive()
        shipment = self.shipment()
        for actor in (self.doctor, self.beta_admin, self.patient_user):
            for call in (lambda: prepare_shipment(shipment=shipment, actor=actor),
                         lambda: hold_or_cancel_shipment(shipment=shipment, actor=actor, reason='Forbidden'),
                         lambda: lock_shipping_week(company=self.company, actor=actor, week_start=self.today),
                         lambda: dispatch_shipment(shipment=shipment, actor=actor, tracking_number='NO', confirm=True),
                         lambda: mark_delivered(shipment=shipment, actor=actor, confirm=True)):
                with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                    call()

    def test_dispatch_rechecks_actual_day_quantity_not_only_future_scheduled_allowance(self):
        self.receive(quantity=20)
        current = self.shipment(quantity=4, status='dispatched', dispatched_at=timezone.now())
        future = self.locked(self.shipment(quantity=4, scheduled_for=self.today + timedelta(days=28)))
        with self.assertRaisesMessage(ValidationError, 'Cannot dispatch on this date'):
            dispatch_shipment(shipment=future, actor=self.admin, tracking_number='EARLY', confirm=True)
        future.refresh_from_db()
        self.assertEqual(future.status, 'ready')
        self.assertEqual(current.status, 'dispatched')

    def test_early_dispatch_consumes_actual_cycle_and_cannot_be_repeated_via_future_dates(self):
        self.receive(quantity=20)
        planned = self.today + timedelta(days=28)
        first = self.locked(self.shipment(quantity=4, scheduled_for=planned))
        dispatch_shipment(shipment=first, actor=self.admin, tracking_number='EARLY-1', confirm=True)
        self.assertEqual(product_allowance(self.patient, self.product)[1], 0)
        self.assertEqual(product_allowance(self.patient, self.product, on_date=planned)[1], 4)
        second = self.locked(self.shipment(quantity=4, scheduled_for=planned))
        with self.assertRaises(ValidationError):
            dispatch_shipment(shipment=second, actor=self.admin, tracking_number='EARLY-2', confirm=True)

    def test_future_dose_cannot_be_dispatched_under_different_current_dose_authorization(self):
        self.receive(quantity=10)
        future_day = self.today + timedelta(days=2)
        new_auth = TreatmentAuthorization.objects.create(company=self.company, patient=self.patient,
            product=self.product, prescribed_by=self.doctor, max_dose='Future physician dose', quantity_per_cycle=4,
            starts_on=future_day, expires_on=self.today + timedelta(days=200))
        shipment = self.shipment(scheduled_for=future_day)
        shipment.items.update(dose=new_auth.max_dose)
        shipment = self.locked(shipment)
        with self.assertRaisesMessage(ValidationError, 'recorded dose does not match'):
            dispatch_shipment(shipment=shipment, actor=self.admin, tracking_number='TOO-EARLY-DOSE', confirm=True)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, 'ready')

    def test_zero_quantity_and_corrupt_cross_product_allocation_fail_closed(self):
        batch = self.receive(product=self.open_product)
        change_batch(batch=batch, actor=self.admin, action='adjust', reason='Count correction', quantity=-4)
        shipment = self.shipment()
        shipment.items.update(quantity=0)
        with self.assertRaises(ValidationError):
            self.ready(shipment)
        shipment.items.update(quantity=4, batch=batch)
        count = StockMovement.objects.count()
        with self.assertRaises(ValidationError):
            hold_or_cancel_shipment(shipment=shipment, actor=self.admin, cancel=True, reason='Unsafe allocation')
        self.assert_stock(batch, 6)
        self.assertEqual(StockMovement.objects.count(), count)

    def test_inactive_practice_and_patient_links_never_get_mutation_access(self):
        self.receive()
        shipment = self.shipment()
        Company.objects.filter(pk=self.company.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.ready(shipment)
        with self.assertRaises(PermissionDenied):
            self.receive(batch_number='AFTER-DEACTIVATION')

    def test_dispatch_and_delivery_require_boolean_confirmation_not_truthy_strings(self):
        self.receive()
        shipment = self.locked()
        with self.assertRaises(ValidationError):
            dispatch_shipment(shipment=shipment, actor=self.admin, tracking_number='REF', confirm='yes')
        shipment = dispatch_shipment(shipment=shipment, actor=self.admin, tracking_number='REF', confirm=True)
        with self.assertRaises(ValidationError):
            mark_delivered(shipment=shipment, actor=self.admin, confirm='yes')
