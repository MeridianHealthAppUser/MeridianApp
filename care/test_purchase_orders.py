"""Supplier orders are scoped, idempotent and separate from stock until units are received."""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DataError, IntegrityError, transaction
from django.utils import timezone

from practices.models import Company, CompanyMembership

from .inventory import stock_rows
from .models import (AuditEvent, MedicationProduct, PurchaseOrder, PurchaseOrderItem, Shipment,
                     ShipmentItem, StockMovement, Supplier, SupplierProduct)
from .purchasing import create_purchase_order, outstanding_order_quantities, receive_purchase_order, to_order_rows
from .test_operations import OperationsFixture


class PurchaseOrderFixture(OperationsFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.supplier = Supplier.objects.create(company=cls.company, name='Alpha Supplies', email='orders@alpha.test')
        cls.beta_supplier = Supplier.objects.create(company=cls.beta, name='Beta Supplies')
        for product, supplier in ((cls.product, cls.supplier), (cls.open_product, cls.supplier),
                                  (cls.beta_product, cls.beta_supplier)):
            SupplierProduct.objects.create(company=product.company, supplier=supplier, product=product,
                                            supplier_code=f'SKU-{product.pk}')

    def create_order(self, *, items=None, key=None, company=None, actor=None, eta=None, supplier=None):
        company = company or self.company
        supplier = supplier or (self.supplier if company.pk == self.company.pk else self.beta_supplier)
        return create_purchase_order(company=company, actor=actor or self.admin, supplier=supplier,
            items=items if items is not None else [{'product': self.product, 'quantity': 5}],
            eta=eta, submission_key=key or uuid4())

    def receive(self, order, *, items=None, key=None, version=None, actor=None, company=None):
        return receive_purchase_order(company=company or self.company, actor=actor or self.admin, order=order,
            items=items if items is not None else [self.receipt(order.items.get())],
            submission_key=key or uuid4(), expected_updated=version)

    def receipt(self, item, quantity=None, **extra):
        values = dict(item=item, quantity=quantity if quantity is not None else item.quantity-item.quantity_received,
                      batch='BATCH-01', expires_on=self.today+timedelta(days=365))
        values.update(extra)
        return values

    def balances(self):
        return {row['product'].pk: (row['on_hand'], row['reserved'], row['available']) for row in stock_rows(self.company)}


class CreatePurchaseOrderTests(PurchaseOrderFixture):
    def test_creates_numbered_snapshot_lines_and_audit_without_changing_stock_or_shipments(self):
        self.shipment(quantity=2, status=Shipment.Status.READY, locked_at=timezone.now())
        models = (StockMovement, Shipment, ShipmentItem)
        before = {model: list(model.objects.order_by('pk').values()) for model in models}
        eta = self.today+timedelta(days=3)

        order = self.create_order(items=[{'product': self.product, 'quantity': 5},
                                         {'product': self.open_product, 'quantity': 2}], eta=eta)

        self.assertEqual((order.number, order.reference, str(order), order.created_by, order.eta,
                          order.status, order.received_at),
                         (1, 'PO-00001', 'PO-00001', self.admin, eta, PurchaseOrder.Status.OPEN, None))
        lines = list(order.items.order_by('pk'))
        self.assertEqual([(line.product_id, line.quantity, line.quantity_received) for line in lines],
                         [(self.product.pk, 5, 0), (self.open_product.pk, 2, 0)])
        self.assertEqual((lines[0].company, lines[0].product_name, lines[0].strength, lines[0].category),
                         (self.company, self.product.name, self.product.strength, self.product.category))
        audit = AuditEvent.objects.get(action='purchase_order.created')
        self.assertEqual((audit.company, audit.actor, audit.target_id), (self.company, self.admin, str(order.pk)))
        self.assertEqual(audit.metadata['eta'], eta.isoformat())
        self.assertEqual((order.supplier_id, order.supplier_name, order.supplier_email),
                         (self.supplier.pk, self.supplier.name, self.supplier.email))
        self.assertEqual(lines[0].supplier_code, f'SKU-{self.product.pk}')
        self.assertEqual({model: list(model.objects.order_by('pk').values()) for model in models}, before)

    def test_same_key_and_payload_retries_once_even_if_line_order_or_catalogue_changes(self):
        key = uuid4()
        items = [{'product': self.product, 'quantity': 5}, {'product': self.open_product, 'quantity': 2}]
        original_name = self.product.name
        first = self.create_order(key=key, items=items)
        MedicationProduct.objects.filter(pk=self.product.pk).update(name='Changed catalogue name', is_active=False)

        again = self.create_order(key=str(key), items=list(reversed(items)))

        self.assertEqual(again.pk, first.pk)
        self.assertEqual(PurchaseOrder.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action='purchase_order.created').count(), 1)
        self.assertEqual(again.items.get(product=self.product).product_name, original_name)

    def test_retry_cannot_change_quantities_products_or_eta(self):
        key = uuid4()
        order = self.create_order(key=key)
        for kwargs in ({'items': [{'product': self.product, 'quantity': 6}]},
                       {'items': [{'product': self.open_product, 'quantity': 5}]},
                       {'eta': self.today}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                self.create_order(key=key, **kwargs)
        self.assertEqual(list(order.items.values_list('quantity', flat=True)), [5])
        self.assertEqual((PurchaseOrder.objects.count(), AuditEvent.objects.filter(action='purchase_order.created').count()), (1, 1))

    def test_numbering_and_submission_identity_are_scoped_to_each_practice(self):
        key = uuid4()
        first = self.create_order(key=key)
        second = self.create_order(actor=self.super_admin)
        other = self.create_order(company=self.beta, actor=self.beta_admin, key=key,
                                  items=[{'product': self.beta_product, 'quantity': 7}])
        self.assertEqual((first.number, second.number, other.number), (1, 2, 1))
        self.assertNotEqual(first.pk, other.pk)

    def test_all_lines_validate_atomically_and_reject_duplicates_or_noninteger_quantities(self):
        for quantity in (0, -1, 2147483648, True, 1.5, Decimal('2'), '2', None):
            with self.subTest(quantity=quantity), self.assertRaises(ValidationError):
                self.create_order(items=[{'product': self.product, 'quantity': 2},
                                         {'product': self.open_product, 'quantity': quantity}])
        for items in ([], None, 'invalid', [{'product': self.product, 'quantity': 1}]*2,
                      [{'product': self.product, 'quantity': 1}]*101, [{}], [1]):
            # The public helper supplies defaults for None; call the domain directly here.
            with self.subTest(items=items), self.assertRaises(ValidationError):
                create_purchase_order(company=self.company, actor=self.admin, supplier=self.supplier,
                                      items=items, submission_key=uuid4())
        self.assertFalse(PurchaseOrder.objects.exists())
        self.assertFalse(PurchaseOrderItem.objects.exists())

    def test_reloads_active_products_and_rejects_foreign_or_unsaved_selections(self):
        unsaved = MedicationProduct(company=self.company, name='Not saved', price=1)
        MedicationProduct.objects.filter(pk=self.open_product.pk).update(is_active=False)
        for product in (self.beta_product, self.open_product, unsaved):
            with self.subTest(product=product), self.assertRaises(ValidationError):
                self.create_order(items=[{'product': self.product, 'quantity': 1}, {'product': product, 'quantity': 2}])
        self.assertFalse(PurchaseOrder.objects.exists())

    def test_key_eta_and_fresh_administrator_permission_are_required(self):
        for key in (None, '', 'invalid', 1):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                create_purchase_order(company=self.company, actor=self.admin,
                    supplier=self.supplier, items=[{'product': self.product, 'quantity': 1}], submission_key=key)
        for eta in ('2026-11-10', datetime.now(), True):
            with self.subTest(eta=eta), self.assertRaises(ValidationError):
                self.create_order(eta=eta)
        for actor in (self.doctor, self.patient_user, self.beta_admin):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                self.create_order(actor=actor)
        CompanyMembership.objects.filter(company=self.company, user=self.admin).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.create_order()
        self.assertFalse(PurchaseOrder.objects.exists())

    def test_permission_is_rechecked_for_retries_and_inactive_companies(self):
        key = uuid4()
        self.create_order(key=key)
        type(self.admin).objects.filter(pk=self.admin.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.create_order(key=key)
        Company.objects.filter(pk=self.company.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.create_order(actor=self.super_admin)
        self.assertEqual(PurchaseOrder.objects.count(), 1)

    def test_audit_failure_rolls_back_lines_and_number_allocation(self):
        with patch('care.purchasing.record_audit', side_effect=RuntimeError('Audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.create_order()
        self.assertFalse(PurchaseOrder.objects.exists())
        self.assertFalse(PurchaseOrderItem.objects.exists())
        self.assertEqual(self.create_order().number, 1)

    def test_supplier_is_required_scoped_and_must_supply_every_selected_product(self):
        for supplier in (None, self.beta_supplier, Supplier(company=self.company, name='Unsaved')):
            with self.subTest(supplier=supplier), self.assertRaises(ValidationError):
                create_purchase_order(company=self.company, actor=self.admin, supplier=supplier,
                    items=[{'product': self.product, 'quantity': 1}], submission_key=uuid4())
        SupplierProduct.objects.filter(supplier=self.supplier, product=self.open_product).delete()
        with self.assertRaisesMessage(ValidationError, 'Every selected product'):
            self.create_order(items=[{'product': self.product, 'quantity': 1},
                                     {'product': self.open_product, 'quantity': 1}])
        self.assertFalse(PurchaseOrder.objects.exists())
        self.assertFalse(PurchaseOrderItem.objects.exists())

    def test_malformed_supplier_product_links_do_not_authorise_an_order(self):
        SupplierProduct.objects.filter(supplier=self.supplier, product=self.product).update(company=self.beta)
        with self.assertRaises(ValidationError):
            self.create_order()
        SupplierProduct.objects.filter(supplier=self.supplier, product=self.product).update(company=self.company)
        self.beta_supplier.company = self.company
        with self.assertRaises(ValidationError):
            self.create_order(supplier=self.beta_supplier)
        self.assertFalse(PurchaseOrder.objects.exists())

    def test_same_submission_cannot_be_reassigned_to_another_supplier(self):
        other = Supplier.objects.create(company=self.company, name='Second supplier')
        SupplierProduct.objects.create(company=self.company, supplier=other, product=self.product, supplier_code='SECOND')
        key = uuid4()
        order = self.create_order(key=key)
        with self.assertRaises(ValidationError):
            self.create_order(key=key, supplier=other)
        self.assertEqual(PurchaseOrder.objects.count(), 1)
        order.refresh_from_db()
        self.assertEqual(order.supplier_id, self.supplier.pk)

    def test_supplier_and_code_snapshots_survive_supplier_changes_and_deletion(self):
        from .suppliers import delete_supplier, save_supplier

        key = uuid4()
        order = self.create_order(key=key)
        original = (order.supplier_name, order.supplier_email, order.items.get().supplier_code)
        saved = save_supplier(company=self.company, actor=self.super_admin, supplier=self.supplier,
            name='Changed supplier', email='new@example.test',
            products=[{'product': self.product, 'supplier_code': 'NEW-CODE'}], submission_key=uuid4())
        # Retrying an existing submission keeps its original snapshot even if the supplier mapping changes.
        self.assertEqual(self.create_order(key=key).pk, order.pk)
        delete_supplier(company=self.company, actor=self.super_admin, supplier=saved, submission_key=uuid4())
        order.refresh_from_db()
        self.assertIsNone(order.supplier_id)
        self.assertEqual((order.supplier_name, order.supplier_email, order.items.get().supplier_code), original)
        self.receive(order)
        order.refresh_from_db()
        self.assertEqual(order.status, PurchaseOrder.Status.RECEIVED)


class PurchaseOrderReceiptTests(PurchaseOrderFixture):
    def test_partial_receipt_adds_traceable_stock_without_changing_reservations(self):
        order = self.create_order(items=[{'product': self.product, 'quantity': 5},
                                         {'product': self.open_product, 'quantity': 2}])
        first, second = list(order.items.order_by('pk'))
        self.shipment(quantity=4, status=Shipment.Status.READY, locked_at=timezone.now())
        before = self.balances()[self.product.pk]
        version = order.updated_at.isoformat()

        movements = self.receive(order, items=[self.receipt(first, 3)], version=version)

        order.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual((first.quantity_received, second.quantity_received, order.status, order.received_at), (3, 0, 'open', None))
        self.assertNotEqual(order.updated_at.isoformat(), version)
        movement, = movements
        self.assertEqual((movement.company, movement.product, movement.purchase_order_item, movement.quantity,
                          movement.reason, movement.batch, movement.expires_on),
                         (self.company, self.product, first, 3, 'Purchase order PO-00001', 'BATCH-01', self.today+timedelta(days=365)))
        self.assertEqual(self.balances()[self.product.pk], (before[0]+3, before[1], before[2]+3))
        self.assertEqual(outstanding_order_quantities(self.company), {self.product.pk: 2, self.open_product.pk: 2})

    def test_last_receipt_closes_order_and_sets_received_at_once(self):
        order = self.create_order()
        item = order.items.get()
        self.receive(order, items=[self.receipt(item, 2)])
        item.refresh_from_db()
        key = uuid4()
        final = [self.receipt(item, 3)]

        movements = self.receive(order, items=final, key=key)
        order.refresh_from_db()
        completed_at = order.received_at
        again = self.receive(order, items=final, key=key, version='stale')
        order.refresh_from_db()

        self.assertEqual((order.status, order.items.get().quantity_received), ('received', 5))
        self.assertIsNotNone(completed_at)
        self.assertEqual(order.received_at, completed_at)
        self.assertEqual([row.pk for row in again], [row.pk for row in movements])
        self.assertEqual(outstanding_order_quantities(self.company), {})
        self.assertEqual(AuditEvent.objects.filter(action='purchase_order.received').count(), 2)

    def test_replayed_partial_receipt_is_exactly_once_and_changed_payload_is_rejected(self):
        order = self.create_order()
        item = order.items.get()
        key = uuid4()
        payload = [self.receipt(item, 2)]
        version = order.updated_at.isoformat()
        movements = self.receive(order, items=payload, key=key, version=version)
        counts = (StockMovement.objects.count(), AuditEvent.objects.count())
        repeated = self.receive(order, items=payload, key=str(key), version=version)
        self.assertEqual([row.pk for row in repeated], [row.pk for row in movements])
        self.assertEqual((StockMovement.objects.count(), AuditEvent.objects.count()), counts)
        for changed in (self.receipt(item, 3), self.receipt(item, 2, batch='OTHER'),
                        self.receipt(item, 2, expires_on=self.today)):
            with self.subTest(changed=changed), self.assertRaises(ValidationError):
                self.receive(order, items=[changed], key=key)
        self.assertEqual(order.items.get().quantity_received, 2)

    def test_overreceipt_bad_second_line_and_stale_version_leave_everything_unchanged(self):
        order = self.create_order(items=[{'product': self.product, 'quantity': 5},
                                         {'product': self.open_product, 'quantity': 2}])
        first, second = list(order.items.order_by('pk'))
        counts = (StockMovement.objects.count(), AuditEvent.objects.count())
        for items, version in (([self.receipt(first, 2), self.receipt(second, 3)], None),
                                ([self.receipt(first, 6)], None), ([self.receipt(first, 1)], 'old')):
            with self.subTest(items=items, version=version), self.assertRaises(ValidationError):
                self.receive(order, items=items, version=version)
        self.assertEqual((StockMovement.objects.count(), AuditEvent.objects.count()), counts)
        self.assertEqual(list(order.items.values_list('quantity_received', flat=True)), [0, 0])

    def test_receipt_validates_quantity_batch_expiry_duplicates_and_line_scope(self):
        order = self.create_order()
        item = order.items.get()
        other = self.create_order().items.get()
        beta_order = self.create_order(company=self.beta, actor=self.beta_admin,
                                      items=[{'product': self.beta_product, 'quantity': 2}])
        invalid = [self.receipt(item, quantity) for quantity in (0, -1, 1.5, True, '2', 2147483648)]
        invalid += [self.receipt(item, 1, **extra) for extra in (
            {'batch': ''}, {'batch': ' '*3}, {'batch': 'B'*121}, {'batch': None},
            {'expires_on': None}, {'expires_on': '2027-01-01'}, {'expires_on': datetime.now()})]
        invalid += [self.receipt(other, 1), self.receipt(beta_order.items.get(), 1)]
        before = StockMovement.objects.count()
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.receive(order, items=[values])
        for values in ([], [self.receipt(item, 1)]*2, [self.receipt(item, 1)]*101, [None]):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.receive(order, items=values)
        self.assertEqual(StockMovement.objects.count(), before)
        self.assertEqual(order.items.get().quantity_received, 0)

    def test_closed_foreign_and_inactive_product_receipts_are_rejected_and_admin_access_is_required(self):
        order = self.create_order()
        item = order.items.get()
        for actor in (self.doctor, self.patient_user, self.beta_admin):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                self.receive(order, actor=actor)
        with self.assertRaises(ValidationError):
            self.receive(order, company=self.beta, actor=self.beta_admin, items=[self.receipt(item, 1)])
        MedicationProduct.objects.filter(pk=self.product.pk).update(is_active=False)
        with self.assertRaises(ValidationError):
            self.receive(order, items=[self.receipt(item, 1)])
        MedicationProduct.objects.filter(pk=self.product.pk).update(is_active=True)
        for status in (PurchaseOrder.Status.CANCELLED, PurchaseOrder.Status.RECEIVED):
            PurchaseOrder.objects.filter(pk=order.pk).update(status=status)
            with self.subTest(status=status), self.assertRaises(ValidationError):
                self.receive(order, items=[self.receipt(item, 1)])
        self.assertEqual(item.quantity_received, 0)

    def test_receipt_audit_failure_rolls_back_ledger_links_progress_and_status(self):
        order = self.create_order()
        before = list(StockMovement.objects.order_by('pk').values())
        with patch('care.purchasing.record_audit', side_effect=RuntimeError('Audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.receive(order)
        order.refresh_from_db()
        self.assertEqual((order.status, order.received_at, order.items.get().quantity_received), ('open', None, 0))
        self.assertEqual(list(StockMovement.objects.order_by('pk').values()), before)
        self.assertFalse(AuditEvent.objects.filter(action='inventory.received').exists())


class OutstandingPurchaseOrderTests(PurchaseOrderFixture):
    def test_real_open_orders_reduce_recommendations_by_only_the_outstanding_units(self):
        MedicationProduct.objects.filter(pk=self.product.pk).update(starting_awc=4, lead_time_weeks=1, safety_stock=0)
        StockMovement.objects.filter(product=self.product).update(quantity=0)
        order = self.create_order()
        self.receive(order, items=[self.receipt(order.items.get(), 2)])

        row = next(row for row in to_order_rows(self.company) if row['product'].pk == self.product.pk)
        override = next(row for row in to_order_rows(self.company, on_order={}) if row['product'].pk == self.product.pk)

        self.assertEqual((row['on_hand'], row['on_order'], row['order_quantity']), (2, 3, 0))
        self.assertEqual((override['on_order'], override['order_quantity']), (0, 2))

    def test_outstanding_excludes_closed_and_cross_tenant_links_and_keeps_inactive_ordered_products(self):
        valid = self.create_order()
        partial = self.create_order()
        PurchaseOrderItem.objects.filter(purchase_order=partial).update(quantity_received=2)
        for status in ('received', 'cancelled'):
            closed = self.create_order()
            PurchaseOrder.objects.filter(pk=closed.pk).update(status=status)
        foreign = self.create_order(company=self.beta, actor=self.beta_admin,
                                    items=[{'product': self.beta_product, 'quantity': 50}])
        corrupt = self.create_order(items=[{'product': self.open_product, 'quantity': 1}])
        PurchaseOrderItem.objects.create(company=self.beta, purchase_order=corrupt, product=self.product,
            quantity=100, product_name='Wrong line company', category=self.product.category)
        PurchaseOrderItem.objects.create(company=self.company, purchase_order=foreign, product=self.product,
            quantity=100, product_name='Wrong order company', category=self.product.category)
        PurchaseOrderItem.objects.create(company=self.company, purchase_order=corrupt, product=self.beta_product,
            quantity=100, product_name='Wrong product company', category=self.beta_product.category)
        MedicationProduct.objects.filter(pk=self.product.pk).update(is_active=False)

        self.assertEqual(outstanding_order_quantities(self.company), {self.product.pk: 8, self.open_product.pk: 1})
        self.assertEqual(outstanding_order_quantities(self.beta), {self.beta_product.pk: 50})
        self.assertEqual(valid.status, 'open')


class PurchaseOrderModelTests(PurchaseOrderFixture):
    def test_line_and_movement_clean_reject_foreign_or_mismatched_links(self):
        order = self.create_order()
        item = order.items.get()
        for company, product in ((self.beta, self.product), (self.company, self.beta_product)):
            line = PurchaseOrderItem(company=company, purchase_order=order, product=product, quantity=1,
                                     product_name='Snapshot', category=product.category)
            with self.subTest(company=company, product=product), self.assertRaises(ValidationError):
                line.clean()
        for values in ({'company': self.beta}, {'product': self.open_product}, {'quantity': -1},
                       {'shipment': self.shipment(quantity=1)}):
            fields = dict(company=self.company, product=self.product, purchase_order_item=item, quantity=1, actor=self.admin)
            fields.update(values)
            with self.subTest(values=values), self.assertRaises(ValidationError):
                StockMovement(**fields).clean()

    def test_database_enforces_positive_quantities_received_bounds_and_unique_identifiers(self):
        order = self.create_order()
        item = order.items.get()
        # Postgres rejects a number too big for the column as a DataError, before any check constraint.
        for changes in ({'quantity': 0}, {'quantity': -1}, {'quantity': 2147483648},
                        {'quantity_received': -1}, {'quantity_received': 6}):
            with self.subTest(changes=changes), self.assertRaises((IntegrityError, DataError)), transaction.atomic():
                PurchaseOrderItem.objects.filter(pk=item.pk).update(**changes)
        for fields in ({'number': order.number, 'submission_key': uuid4()},
                       {'number': 2, 'submission_key': order.submission_key},
                       {'number': 0, 'submission_key': uuid4()}, {'number': 3, 'submission_key': uuid4(), 'status': 'invalid'}):
            with self.subTest(fields=fields), self.assertRaises(IntegrityError), transaction.atomic():
                PurchaseOrder.objects.create(company=self.company, **fields)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PurchaseOrderItem.objects.create(company=self.company, purchase_order=order, product=self.product,
                quantity=1, product_name='Duplicate', category=self.product.category)
