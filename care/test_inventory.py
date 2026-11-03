from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.utils import timezone

from practices.models import CompanyMembership

from .inventory import receive_stock, set_safety_stock, stock_rows
from .models import AuditEvent, MedicationProduct, PracticeSettings, Shipment, StockMovement
from .operations import dispatch_shipment, hold_or_cancel_shipment, lock_week, prepare_week
from .test_operations import OperationsFixture


class InventoryTests(OperationsFixture):
    def setUp(self):
        StockMovement.objects.all().delete()

    def row(self):
        return next(row for row in stock_rows(self.company) if row['product'].pk == self.product.pk)

    def test_lock_reserves_beyond_on_hand_and_hold_releases(self):
        receive_stock(company=self.company, actor=self.admin, product=self.product, quantity=2)
        parcel = self.locked(self.shipment(quantity=4))
        self.assertEqual((self.row()['on_hand'], self.row()['reserved'], self.row()['available']), (2, 4, -2))
        lock_week(company=self.company, actor=self.admin, week_start=self.today)
        self.assertEqual(self.row()['reserved'], 4)
        hold_or_cancel_shipment(shipment=parcel, actor=self.admin, reason='Waiting for patient')
        self.assertEqual(self.row()['reserved'], 0)

    def test_shortage_requires_explicit_override_and_reason_then_deducts_once(self):
        parcel = self.locked(self.shipment(quantity=4))
        for override, reason in ((False, ''), (True, ''), ('yes', 'Count discrepancy')):
            with self.assertRaises(ValidationError):
                dispatch_shipment(shipment=parcel, actor=self.admin, tracking_number='STOCK1', confirm=True,
                                  stock_override=override, stock_override_reason=reason)
        self.assertFalse(StockMovement.objects.exists())
        dispatch_shipment(shipment=parcel, actor=self.admin, tracking_number='STOCK1', confirm=True,
                          stock_override=True, stock_override_reason='Receipt not recorded yet')
        self.assertEqual((self.row()['on_hand'], self.row()['reserved']), (-4, 0))
        self.assertEqual(AuditEvent.objects.filter(action='inventory.shortage_overridden').count(), 1)
        dispatch_shipment(shipment=parcel, actor=self.admin, tracking_number='STOCK1', confirm=True)
        self.assertEqual(StockMovement.objects.count(), 1)
        receive_stock(company=self.company, actor=self.admin, product=self.product, quantity=10)
        self.assertEqual(self.row()['on_hand'], 6)

    def test_receiving_stock_then_dispatch_needs_no_override(self):
        parcel = self.locked(self.shipment(quantity=4))
        receive_stock(company=self.company, actor=self.admin, product=self.product, quantity=6)
        dispatch_shipment(shipment=parcel, actor=self.admin, tracking_number='STOCK2', confirm=True)
        self.assertEqual((self.row()['on_hand'], self.row()['reserved']), (2, 0))

    def test_receipts_are_scoped_and_admin_only(self):
        with self.assertRaises(ValidationError):
            receive_stock(company=self.company, actor=self.admin, product=self.beta_product, quantity=1)
        with self.assertRaises(PermissionDenied):
            receive_stock(company=self.company, actor=self.patient_user, product=self.product, quantity=1)

    def test_stock_rows_exclude_malformed_cross_practice_reservations_and_balances(self):
        receive_stock(company=self.company, actor=self.admin, product=self.product, quantity=10)
        options = dict(status=Shipment.Status.READY, locked_at=timezone.now(), subscription=None)
        valid = self.shipment(quantity=2, **options)
        # Deliberately bypass model/service validation to model an invalid imported relationship.
        self.shipment(quantity=40, patient=self.beta_patient, **options)
        self.shipment(quantity=40, product=self.beta_product, **options)
        wrong_item = self.shipment(quantity=40, **options)
        wrong_item.items.update(company=self.beta)
        wrong_shipment = self.shipment(quantity=40, company=self.beta, **options)
        wrong_shipment.items.update(company=self.company)
        StockMovement.objects.create(company=self.beta, product=self.product, quantity=90, actor=self.beta_admin)
        StockMovement.objects.create(company=self.company, product=self.beta_product, quantity=90, actor=self.admin)

        rows = stock_rows(self.company)
        row = next(row for row in rows if row['product'].pk == self.product.pk)

        self.assertEqual((row['on_hand'], row['reserved'], row['available']), (10, 2, 8))
        self.assertEqual([item.shipment_id for item in row['reservations']], [valid.pk])
        self.assertNotIn(self.beta_product.pk, [row['product'].pk for row in rows])
        self.assertNotIn(self.beta_patient.pk, [item.shipment.patient_id for row in rows for item in row['reservations']])

    def test_prepare_selection_is_scoped_and_only_selected_parcels_change(self):
        first = self.shipment(quantity=1)
        second = self.shipment(quantity=1)
        ready, left = prepare_week(company=self.company, actor=self.admin, week_start=self.today, shipment_ids=[first.pk])
        self.assertEqual([parcel.pk for parcel in ready], [first.pk])
        second.refresh_from_db()
        self.assertEqual(second.status, Shipment.Status.DRAFT)
        with self.assertRaises(ValidationError):
            prepare_week(company=self.company, actor=self.admin, week_start=self.today, shipment_ids=[999999])


class SafetyStockTests(OperationsFixture):
    def set_quantity(self, quantity, *, actor=None, product=None):
        return set_safety_stock(company=self.company, actor=actor or self.super_admin,
                                product=product or self.product, quantity=quantity)

    def audits(self):
        return AuditEvent.objects.filter(action='inventory.safety_stock_updated').order_by('pk')

    def test_unset_by_default_and_explicit_zero_are_distinct(self):
        self.assertIsNone(self.product.safety_stock)
        product = self.set_quantity(0)
        self.assertEqual(product.safety_stock, 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.safety_stock, 0)
        audit = self.audits().get()
        self.assertEqual((audit.company, audit.actor, audit.target_id),
                         (self.company, self.super_admin, str(self.product.pk)))
        self.assertEqual(audit.metadata, {'before': None, 'after': 0})

    def test_setting_and_clearing_preserves_balances_reservations_and_ledger(self):
        self.locked(self.shipment(quantity=4))
        balances = [(row['product'].pk, row['on_hand'], row['reserved'], row['available'])
                    for row in stock_rows(self.company)]
        movements = list(StockMovement.objects.order_by('pk').values())
        product = self.set_quantity(12)
        self.assertEqual(product.safety_stock, 12)
        self.assertIsNone(self.set_quantity(None).safety_stock)
        self.assertEqual(list(self.audits().values_list('metadata', flat=True)),
                         [{'before': None, 'after': 12}, {'before': 12, 'after': None}])
        self.assertEqual(list(StockMovement.objects.order_by('pk').values()), movements)
        self.assertEqual([(row['product'].pk, row['on_hand'], row['reserved'], row['available'])
                          for row in stock_rows(self.company)], balances)

    def test_repeated_values_are_idempotent_even_with_a_stale_product_instance(self):
        initial_updated_at = self.product.updated_at
        self.assertEqual(self.set_quantity(None).updated_at, initial_updated_at)
        self.assertFalse(self.audits().exists())
        changed = self.set_quantity(6)
        # self.product still has its original None value. The persisted configuration decides whether to save.
        self.assertIsNone(self.product.safety_stock)
        repeated = self.set_quantity(6)
        self.assertEqual((repeated.safety_stock, repeated.updated_at), (6, changed.updated_at))
        self.assertEqual(self.audits().count(), 1)
        zero = self.set_quantity(0)
        self.assertEqual(self.set_quantity(0).updated_at, zero.updated_at)
        self.assertEqual(self.audits().count(), 2)

    def test_super_admin_can_configure_inactive_products(self):
        self.product.is_active = False
        self.product.save(update_fields=('is_active', 'updated_at'))
        self.assertEqual(self.set_quantity(3).safety_stock, 3)
        self.assertEqual(self.set_quantity(5, actor=self.super_admin).safety_stock, 5)
        self.product.refresh_from_db()
        self.assertFalse(self.product.is_active)
        self.assertEqual(list(self.audits().values_list('actor_id', flat=True)),
                         [self.super_admin.pk, self.super_admin.pk])

    def test_foreign_and_missing_products_do_not_change_any_configuration(self):
        for product in (self.beta_product, MedicationProduct(pk=999999)):
            with self.subTest(product=product.pk), self.assertRaises(ValidationError):
                self.set_quantity(8, product=product)
        self.beta_product.refresh_from_db()
        self.product.refresh_from_db()
        self.assertIsNone(self.beta_product.safety_stock)
        self.assertIsNone(self.product.safety_stock)
        self.assertFalse(self.audits().exists())

    def test_only_current_super_administrators_can_set_safety_stock(self):
        PracticeSettings.objects.filter(company=self.company).update(doctors_ship=True)
        for actor in (self.admin, self.doctor, self.patient_user, self.beta_admin):
            with self.subTest(actor=actor.pk), self.assertRaises(PermissionDenied):
                self.set_quantity(8, actor=actor)
        CompanyMembership.objects.filter(company=self.company, user=self.super_admin).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.set_quantity(8)
        self.product.refresh_from_db()
        self.assertIsNone(self.product.safety_stock)
        self.assertFalse(self.audits().exists())

    def test_rejects_non_integral_negative_and_out_of_range_values_without_writes(self):
        maximum = connection.ops.integer_field_range('PositiveIntegerField')[1]
        initial_updated_at = self.product.updated_at
        for quantity in (-1, True, False, '4', '', 4.0, Decimal('4'), [], maximum + 1):
            with self.subTest(quantity=repr(quantity)), self.assertRaises(ValidationError):
                self.set_quantity(quantity)
        self.product.refresh_from_db()
        self.assertEqual((self.product.safety_stock, self.product.updated_at), (None, initial_updated_at))
        self.assertFalse(self.audits().exists())

    def test_accepts_database_positive_integer_maximum(self):
        maximum = connection.ops.integer_field_range('PositiveIntegerField')[1]
        self.assertEqual(self.set_quantity(maximum).safety_stock, maximum)
        self.product.refresh_from_db()
        self.assertEqual(self.product.safety_stock, maximum)
        self.assertEqual(self.audits().get().metadata, {'before': None, 'after': maximum})
