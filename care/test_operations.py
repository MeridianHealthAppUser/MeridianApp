"""Clinical gates, the weekly order and tenant boundaries for local dispatch. Nothing waits on stock."""

from django.test import override_settings
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient
from .models import (AuditEvent, MedicationProduct, PatientSubscription, Payment,
                     PracticeSettings, Shipment, ShipmentItem, TreatmentAuthorization)
from .operations import (allowance_window, change_parcel, dispatch_shipment, hold_or_cancel_shipment, lock_shipping_week,
                         mark_delivered, prepare_shipment, prepare_week, product_allowance, shipment_hold_reason, week_order)


@override_settings(MULTI_PRACTICE_ENABLED=True)
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
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Alpha', last_name='Patient',
                                             assigned_doctor=cls.doctor)
        cls.other_patient = Patient.objects.create(company=cls.company, user=cls.other_user, first_name='Other', last_name='Patient',
                                                   assigned_doctor=cls.doctor)
        cls.beta_patient = Patient.objects.create(company=cls.beta, user=cls.patient_user, first_name='Beta', last_name='Patient',
                                                  assigned_doctor=cls.doctor)
        cls.product = MedicationProduct.objects.create(company=cls.company, name='Explicit prescribed product', strength='Dose A', price=Decimal('100.00'))
        cls.open_product = MedicationProduct.objects.create(company=cls.company, name='Open local product', price=Decimal('40.00'), requires_authorisation=False, requires_cold_chain=False)
        cls.beta_product = MedicationProduct.objects.create(company=cls.beta, name='Private beta product', price=Decimal('60.00'))
        cls.auth = TreatmentAuthorization.objects.create(company=cls.company, patient=cls.patient,
            product=cls.product, prescribed_by=cls.doctor, max_dose='Explicit physician dose', quantity_per_cycle=4,
            starts_on=cls.today - timedelta(days=10), expires_on=cls.today + timedelta(days=180))
        cls.plan = PatientSubscription.objects.create(company=cls.company, patient=cls.patient, authorization=cls.auth,
            plan_name='Local non-payment plan', starts_on=cls.today - timedelta(days=5))
        PracticeSettings.objects.create(company=cls.company)

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


class ShipmentOperationsTests(OperationsFixture):
    def test_marking_ready_checks_eligibility_only_and_never_waits_on_stock(self):
        shipment = self.ready()
        self.assertEqual((shipment.status, shipment.hold_reason, shipment.prepared_by), ('ready', '', self.admin))
        self.assertTrue(AuditEvent.objects.filter(action='shipment.prepared', target_id=str(shipment.pk)).exists())
        # Marking it ready again changes nothing but the time it was checked.
        self.assertEqual(self.ready(shipment).status, 'ready')

    def test_cancel_and_hold_keep_the_lines_and_take_no_payment(self):
        shipment = self.ready()
        hold_or_cancel_shipment(shipment=shipment, actor=self.admin, reason='Patient travelling')
        shipment.refresh_from_db()
        self.assertEqual((shipment.status, shipment.locked_at), ('held', None))
        hold_or_cancel_shipment(shipment=shipment, actor=self.admin, cancel=True, reason='Patient cancelled')
        shipment.refresh_from_db()
        self.assertEqual((shipment.status, shipment.items.get().quantity), ('cancelled', 4))
        self.assertFalse(Payment.objects.exists())

    def test_dispatch_requires_ready_lock_and_confirmation(self):
        shipment = self.shipment()
        with self.assertRaises(ValidationError):
            dispatch_shipment(shipment=shipment, actor=self.admin, tracking_number='REF-1', confirm=True)
        shipment = self.ready(shipment)
        with self.assertRaises(ValidationError):
            dispatch_shipment(shipment=shipment, actor=self.admin, tracking_number='REF-1', confirm=True)
        lock_shipping_week(company=self.company, actor=self.admin, week_start=self.today)
        with self.assertRaises(ValidationError):
            dispatch_shipment(shipment=shipment, actor=self.admin, tracking_number='REF-1')
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, 'ready')

    def test_dispatch_notes_each_lines_batch_and_the_snapshot_stays_immutable(self):
        shipment = self.locked()
        item = shipment.items.get()
        with self.assertRaises(ValidationError):
            dispatch_shipment(shipment=shipment, actor=self.admin, tracking_number='REF-1', confirm=True,
                              batch_notes={item.pk: 'x' * 121})
        dispatched = dispatch_shipment(shipment=shipment, actor=self.admin, tracking_number='REF-1', confirm=True,
                                       batch_notes={item.pk: '  SG-2604,  expiry 04/2027 '})
        item.refresh_from_db()
        self.assertEqual(item.batch_note, 'SG-2604, expiry 04/2027')
        snapshot = dispatched.dispatch_snapshot
        self.assertEqual(snapshot['items'], [{'product': str(self.product), 'dose': self.auth.max_dose, 'quantity': 4,
                                              'batch_note': 'SG-2604, expiry 04/2027'}])
        audits = AuditEvent.objects.count()
        repeated = dispatch_shipment(shipment=dispatched, actor=self.admin, tracking_number='REF-1', confirm=True)
        self.assertEqual((repeated.pk, AuditEvent.objects.count()), (dispatched.pk, audits))
        self.product.name = 'Later catalogue name'
        self.product.save()
        delivered = mark_delivered(shipment=shipment, actor=self.admin, confirm=True)
        self.assertEqual((delivered.dispatch_snapshot, delivered.status), (snapshot, 'delivered'))
        self.assertFalse(Payment.objects.exists())

    def test_dispatched_lines_and_tracking_cannot_be_changed(self):
        shipment = dispatch_shipment(shipment=self.locked(), actor=self.admin, tracking_number='REF-1', confirm=True)
        lines = list(shipment.items.values())
        for call in (lambda: prepare_shipment(shipment=shipment, actor=self.admin),
                     lambda: hold_or_cancel_shipment(shipment=shipment, actor=self.admin, cancel=True, reason='Too late'),
                     lambda: dispatch_shipment(shipment=shipment, actor=self.admin, tracking_number='Changed', confirm=True)):
            with self.assertRaises(ValidationError):
                call()
        self.assertEqual(list(shipment.items.values()), lines)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, 'dispatched')

    def test_week_lock_is_atomic_and_scoped_and_replay_does_not_duplicate_audits(self):
        first = self.ready(self.shipment(quantity=2))
        second = self.ready(self.shipment(quantity=2))
        # A parcel that no longer matches the prescription stops the whole week locking.
        second.items.update(dose='Not the authorised dose')
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

    def test_marking_the_weeks_drafts_ready_leaves_any_that_fail_as_drafts(self):
        week = self.today - timedelta(days=self.today.weekday())
        good = self.shipment(quantity=1, scheduled_for=week)
        bad = self.shipment(quantity=1, scheduled_for=week)
        bad.items.update(dose='Not the authorised dose')
        later = self.shipment(quantity=1, scheduled_for=week + timedelta(days=7))
        ready, left = prepare_week(company=self.company, actor=self.admin, week_start=week)
        self.assertEqual((ready, left), ([good], [bad]))
        statuses = dict(Shipment.objects.values_list('pk', 'status'))
        self.assertEqual((statuses[good.pk], statuses[bad.pk], statuses[later.pk]), ('ready', 'draft', 'draft'))
        for actor in (self.doctor, self.beta_admin):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                prepare_week(company=self.company, actor=actor, week_start=week)

    def test_a_parcel_can_be_moved_or_given_other_pens_until_its_week_is_locked(self):
        shipment = self.ready(self.shipment(quantity=2))
        item = shipment.items.get()
        moved = change_parcel(shipment=shipment, actor=self.admin, scheduled_for=self.today + timedelta(days=1),
                              quantities={item.pk: 3})
        item.refresh_from_db()
        # A ready parcel goes back to draft to be checked again.
        self.assertEqual((moved.scheduled_for, moved.status, item.quantity), (self.today + timedelta(days=1), 'draft', 3))
        self.assertEqual(AuditEvent.objects.get(action='shipment.changed').metadata['pens'], {str(item.pk): 3})
        for values in ({'scheduled_for': self.today - timedelta(days=1)}, {'quantities': {item.pk: 0}},
                       {'quantities': {item.pk: True}}, {'quantities': {}}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                change_parcel(shipment=shipment, actor=self.admin, **{'scheduled_for': self.today, 'quantities': {item.pk: 1}, **values})
        locked = self.locked(self.shipment(quantity=1))
        with self.assertRaisesMessage(ValidationError, 'Hold it first'):
            change_parcel(shipment=locked, actor=self.admin, scheduled_for=self.today, quantities={locked.items.get().pk: 1})
        with self.assertRaises(PermissionDenied):
            change_parcel(shipment=shipment, actor=self.doctor, scheduled_for=self.today, quantities={item.pk: 1})

    def test_the_weeks_order_is_the_pens_in_its_ready_parcels(self):
        week = self.today - timedelta(days=self.today.weekday())
        self.ready(self.shipment(quantity=1, scheduled_for=week))
        self.locked(self.shipment(quantity=2, scheduled_for=week))
        self.ready(self.shipment(product=self.open_product, quantity=3, scheduled_for=week))
        self.shipment(quantity=2, scheduled_for=week)
        self.shipment(quantity=2, scheduled_for=week, status='held')
        self.shipment(quantity=2, scheduled_for=week, status='cancelled')
        self.ready(self.shipment(product=self.open_product, quantity=1, scheduled_for=week + timedelta(days=7)))
        order = week_order(self.company, week)
        self.assertEqual([(line['product'], line['pens']) for line in order['lines']], [(self.product, 3), (self.open_product, 3)])
        self.assertEqual((order['parcels'], order['locked'], order['drafts'], order['held']), (3, 2, 1, 1))
        self.assertEqual(week_order(self.beta, week)['lines'], [])

    def test_inactive_patient_or_paused_subscription_or_wrong_dose_blocks_preparation(self):
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
        current = self.shipment(quantity=4, status='dispatched', dispatched_at=timezone.now())
        future = self.locked(self.shipment(quantity=4, scheduled_for=self.today + timedelta(days=28)))
        with self.assertRaisesMessage(ValidationError, 'Cannot dispatch on this date'):
            dispatch_shipment(shipment=future, actor=self.admin, tracking_number='EARLY', confirm=True)
        future.refresh_from_db()
        self.assertEqual(future.status, 'ready')
        self.assertEqual(current.status, 'dispatched')

    def test_early_dispatch_consumes_actual_cycle_and_cannot_be_repeated_via_future_dates(self):
        planned = self.today + timedelta(days=28)
        first = self.locked(self.shipment(quantity=4, scheduled_for=planned))
        dispatch_shipment(shipment=first, actor=self.admin, tracking_number='EARLY-1', confirm=True)
        self.assertEqual(product_allowance(self.patient, self.product)[1], 0)
        self.assertEqual(product_allowance(self.patient, self.product, on_date=planned)[1], 4)
        second = self.locked(self.shipment(quantity=4, scheduled_for=planned))
        with self.assertRaises(ValidationError):
            dispatch_shipment(shipment=second, actor=self.admin, tracking_number='EARLY-2', confirm=True)

    def test_future_dose_cannot_be_dispatched_under_different_current_dose_authorization(self):
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

    def test_zero_quantity_fails_closed(self):
        shipment = self.shipment()
        shipment.items.update(quantity=0)
        with self.assertRaises(ValidationError):
            self.ready(shipment)

    def test_inactive_practice_and_patient_links_never_get_mutation_access(self):
        shipment = self.shipment()
        Company.objects.filter(pk=self.company.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.ready(shipment)

    def test_dispatch_and_delivery_require_boolean_confirmation_not_truthy_strings(self):
        shipment = self.locked()
        with self.assertRaises(ValidationError):
            dispatch_shipment(shipment=shipment, actor=self.admin, tracking_number='REF', confirm='yes')
        shipment = dispatch_shipment(shipment=shipment, actor=self.admin, tracking_number='REF', confirm=True)
        with self.assertRaises(ValidationError):
            mark_delivered(shipment=shipment, actor=self.admin, confirm='yes')


class StockRemovalMigrationTests(TransactionTestCase):
    """Removing stock keeps which batch went into each parcel, as its batch note."""

    before = [('care', '0029_remove_months_and_practice_pricing')]
    after = [('care', '0032_remove_stock')]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        others = [node for node in executor.loader.graph.leaf_nodes() if node[0] != 'care']
        return executor.loader.project_state(targets + others).apps

    def tearDown(self):
        self.migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
        super().tearDown()

    def test_each_parcel_line_keeps_its_batch_and_expiry(self):
        old = self.migrate(self.before)
        company = old.get_model('practices', 'Company').objects.create(name='Migrate', slug='migrate-stock')
        patient = old.get_model('practices', 'Patient').objects.create(company=company, first_name='M', last_name='P')
        product = old.get_model('care', 'MedicationProduct').objects.create(company=company, name='Semaglutide', strength='0.25 mg', price=1)
        batch = old.get_model('care', 'MedicationBatch').objects.create(
            company=company, product=product, batch_number='SG-2604', received_on=date(2026, 9, 1), expires_on=date(2027, 4, 30),
            quantity_received=10, quantity_on_hand=9)
        shipment = old.get_model('care', 'Shipment').objects.create(company=company, patient=patient, scheduled_for=date(2026, 10, 20))
        Item = old.get_model('care', 'ShipmentItem')
        allocated = Item.objects.create(company=company, shipment=shipment, product=product, batch=batch, dose='0.25 mg weekly', quantity=1)
        unallocated = Item.objects.create(company=company, shipment=shipment, product=product, dose='0.25 mg weekly', quantity=1)
        new = self.migrate(self.after)
        Item = new.get_model('care', 'ShipmentItem')
        self.assertEqual(Item.objects.get(pk=allocated.pk).batch_note, 'SG-2604, expiry 04/2027')
        self.assertEqual(Item.objects.get(pk=unallocated.pk).batch_note, '')
