"""Supplier maintenance permissions, linkage, concurrency and retry guarantees."""

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from practices.models import CompanyMembership
from .models import AuditEvent, MedicationProduct, StockMovement, Supplier, SupplierProduct
from .suppliers import delete_supplier, save_supplier
from .test_operations import OperationsFixture


class SupplierTests(OperationsFixture):
    def save(self, *, supplier=None, actor=None, key=None, **values):
        values.setdefault('name', 'Alpha Supplies')
        return save_supplier(company=self.company, actor=actor or self.super_admin, supplier=supplier,
                             submission_key=key or uuid4(), **values)

    def line(self, product=None, code='ALPHA-1'):
        return {'product': product or self.product, 'supplier_code': code}

    def delete(self, supplier, *, actor=None, key=None, **values):
        return delete_supplier(company=self.company, actor=actor or self.super_admin, supplier=supplier,
                               submission_key=key or uuid4(), **values)

    def test_create_retains_contacts_and_supplier_specific_codes_without_changing_stock(self):
        before = list(StockMovement.objects.order_by('pk').values())
        supplier = self.save(name='  Alpha Supplies  ', contact_name=' Ann ', email='ann@example.test',
                             phone=' 0123456789 ', products=[self.line(code=' A-001 ')])

        self.assertEqual((supplier.name, supplier.contact_name, supplier.email, supplier.phone),
                         ('Alpha Supplies', 'Ann', 'ann@example.test', '0123456789'))
        link = supplier.product_links.get()
        self.assertEqual((link.company_id, link.product_id, link.supplier_code),
                         (self.company.pk, self.product.pk, 'A-001'))
        self.assertEqual(self.product.supplier_links.get().supplier_id, supplier.pk)
        self.assertEqual(list(StockMovement.objects.order_by('pk').values()), before)
        audit = AuditEvent.objects.get(action='supplier.created')
        self.assertEqual(audit.metadata['after']['products'], [{'product_id': self.product.pk, 'supplier_code': 'A-001'}])

    def test_supplier_can_start_without_contacts_or_links_and_product_can_have_multiple_suppliers(self):
        blank = self.save()
        self.assertEqual((blank.email, blank.contact_name, blank.phone), ('', '', ''))
        self.assertFalse(blank.product_links.exists())
        self.save(supplier=blank, products=[self.line()])
        other = self.save(name='Second Supplies', products=[self.line(code='SECOND-1')])
        self.assertEqual(set(self.product.supplier_links.values_list('supplier_id', flat=True)), {blank.pk, other.pk})

    def test_update_replaces_complete_mapping_and_allows_codes_to_be_swapped(self):
        supplier = self.save(products=[self.line(), self.line(self.open_product, 'ALPHA-2')])
        updated = self.save(supplier=supplier, name='Renamed Supplies',
                            products=[self.line(code='ALPHA-2'), self.line(self.open_product, 'ALPHA-1')])
        self.assertEqual(updated.pk, supplier.pk)
        self.assertEqual(dict(updated.product_links.values_list('product_id', 'supplier_code')),
                         {self.product.pk: 'ALPHA-2', self.open_product.pk: 'ALPHA-1'})
        updated = self.save(supplier=updated, name='Renamed Supplies', products=[])
        self.assertFalse(updated.product_links.exists())
        self.assertEqual(Supplier.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action='supplier.updated').count(), 2)

    def test_create_and_update_replays_are_once_only_and_ignore_line_order(self):
        key = uuid4()
        supplier = self.save(key=key, products=[self.line(), self.line(self.open_product, 'ALPHA-2')])
        replay = self.save(key=key, products=[self.line(self.open_product, 'ALPHA-2'), self.line()])
        self.assertEqual(replay.pk, supplier.pk)
        self.assertEqual(Supplier.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action='supplier.created').count(), 1)
        update_key = uuid4()
        original_version = supplier.updated_at.isoformat()
        first = self.save(supplier=supplier, key=update_key, name='New name', expected_updated=original_version,
                          products=[self.line(code='UPDATED')])
        second = self.save(supplier=supplier, key=update_key, name='New name', expected_updated=original_version,
                           products=[self.line(code='UPDATED')])
        self.assertEqual(first.updated_at, second.updated_at)
        self.assertEqual(AuditEvent.objects.filter(action='supplier.updated').count(), 1)

    def test_reused_key_cannot_change_payload_or_operation(self):
        key = uuid4()
        supplier = self.save(key=key, products=[self.line()])
        for values in ({'name': 'A different supplier'}, {'products': [self.line(code='CHANGED')]}, {'supplier': supplier}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.save(key=key, **values)
        with self.assertRaises(ValidationError):
            self.delete(supplier, key=key)
        self.assertEqual(Supplier.objects.count(), 1)

    def test_stale_update_and_delete_are_rejected_without_changing_links(self):
        supplier = self.save(products=[self.line()])
        old_version = supplier.updated_at
        Supplier.objects.filter(pk=supplier.pk).update(updated_at=timezone.now() + timedelta(seconds=1))
        for operation in (lambda: self.save(supplier=supplier, name='Changed', products=[]),
                          lambda: self.save(supplier=supplier, expected_updated=old_version.isoformat()),
                          lambda: self.delete(supplier, expected_updated=old_version)):
            with self.assertRaisesMessage(ValidationError, 'changed in another tab'):
                operation()
        supplier.refresh_from_db()
        self.assertEqual(supplier.name, 'Alpha Supplies')
        self.assertEqual(supplier.product_links.get().supplier_code, 'ALPHA-1')

    def test_only_active_super_admin_can_create_update_delete_even_with_a_valid_replay_key(self):
        key = uuid4()
        supplier = self.save(key=key)
        for actor in (self.admin, self.doctor, self.patient_user, self.beta_admin):
            for operation in (lambda: self.save(actor=actor, name='Another'),
                              lambda: self.save(supplier=supplier, actor=actor),
                              lambda: self.delete(supplier, actor=actor),
                              lambda: self.save(actor=actor, key=key)):
                with self.subTest(actor=actor.pk), self.assertRaises(PermissionDenied):
                    operation()
        CompanyMembership.objects.filter(company=self.company, user=self.super_admin).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.save(key=key)
        self.assertEqual(Supplier.objects.count(), 1)

    def test_duplicate_names_are_case_insensitive_per_practice(self):
        supplier = self.save()
        with self.assertRaisesMessage(ValidationError, 'already exists'):
            self.save(name=' alpha SUPPLIES ')
        self.save(supplier=supplier, name='ALPHA SUPPLIES')
        Supplier.objects.create(company=self.beta, name='Alpha Supplies')
        with transaction.atomic(), self.assertRaises(IntegrityError):
            Supplier.objects.create(company=self.company, name='alpha supplies')
        self.assertEqual(Supplier.objects.count(), 2)

    def test_duplicate_products_and_codes_and_missing_codes_are_rejected_atomically(self):
        for products in ([self.line(), self.line(code='OTHER')],
                         [self.line(), self.line(self.open_product, 'alpha-1')],
                         [self.line(code='')], [self.line(code='   ')], [self.line(code='x' * 101)],
                         [self.line(code=None)]):
            with self.subTest(products=products), self.assertRaises(ValidationError):
                self.save(products=products)
        self.assertFalse(Supplier.objects.exists())
        self.assertFalse(SupplierProduct.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(action='supplier.created').exists())

    def test_foreign_supplier_and_product_inputs_cannot_cross_company_scope(self):
        foreign = Supplier.objects.create(company=self.beta, name='Private supplier')
        for operation in (lambda: self.save(supplier=foreign), lambda: self.delete(foreign),
                          lambda: self.save(products=[self.line(self.beta_product)])):
            with self.assertRaises(ValidationError):
                operation()
        # A caller cannot forge the object's company attribute to bypass the persisted product scope.
        self.beta_product.company = self.company
        with self.assertRaises(ValidationError):
            self.save(products=[self.line(self.beta_product)])
        self.assertEqual(Supplier.objects.count(), 1)
        self.assertFalse(SupplierProduct.objects.exists())
        foreign.refresh_from_db()
        self.assertEqual(foreign.name, 'Private supplier')

    def test_inactive_products_can_keep_supplier_mapping_but_unsaved_products_cannot(self):
        self.product.is_active = False
        self.product.save(update_fields=('is_active',))
        self.assertEqual(self.save(products=[self.line()]).product_links.get().product_id, self.product.pk)
        unsaved = MedicationProduct(company=self.company, name='Not saved')
        with self.assertRaises(ValidationError):
            self.save(name='New supplier', products=[self.line(unsaved)])

    def test_invalid_fields_and_submission_key_do_not_create_partial_records(self):
        for values in ({'name': ''}, {'name': '   '}, {'name': 'x' * 256}, {'contact_name': 'x' * 121},
                       {'email': 'not-an-email'}, {'phone': 'x' * 41}, {'name': None},
                       {'products': None}, {'products': [{}] * 101}, {'key': 'invalid'}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.save(**values)
        self.assertFalse(Supplier.objects.exists())

    def test_failure_after_link_replacement_rolls_back_contacts_links_and_audit(self):
        supplier = self.save(products=[self.line()])
        before = list(Supplier.objects.values()), list(SupplierProduct.objects.values()), AuditEvent.objects.count()
        with patch('care.suppliers.record_audit', side_effect=RuntimeError('Audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.save(supplier=supplier, name='Replacement', products=[self.line(self.open_product, 'NEW')])
        self.assertEqual((list(Supplier.objects.values()), list(SupplierProduct.objects.values()), AuditEvent.objects.count()), before)

    def test_delete_removes_only_supplier_links_retains_audit_and_can_be_replayed_using_stub(self):
        supplier = self.save(products=[self.line()])
        other = self.save(name='Another supplier', products=[self.line(code='OTHER')])
        products = list(MedicationProduct.objects.order_by('pk').values())
        stock = list(StockMovement.objects.order_by('pk').values())
        key, pk = uuid4(), supplier.pk
        self.delete(supplier, key=key)
        self.assertFalse(Supplier.objects.filter(pk=pk).exists())
        self.assertEqual(list(SupplierProduct.objects.values_list('supplier_id', flat=True)), [other.pk])
        self.assertEqual(list(MedicationProduct.objects.order_by('pk').values()), products)
        self.assertEqual(list(StockMovement.objects.order_by('pk').values()), stock)
        stub = Supplier(pk=pk, company=self.company)
        self.assertIsNone(self.delete(stub, key=key, expected_updated=supplier.updated_at))
        audit = AuditEvent.objects.get(action='supplier.deleted')
        self.assertEqual(audit.target_id, str(pk))
        self.assertEqual(audit.metadata['before']['name'], 'Alpha Supplies')
        self.assertEqual(audit.metadata['before']['products'], [{'product_id': self.product.pk, 'supplier_code': 'ALPHA-1'}])
        with self.assertRaises(ValidationError):
            self.delete(stub, key=uuid4(), expected_updated=supplier.updated_at)

    def test_deleted_create_cannot_be_replayed_into_a_new_supplier(self):
        key = uuid4()
        supplier = self.save(key=key)
        self.delete(supplier)
        with self.assertRaisesMessage(ValidationError, 'since been deleted'):
            self.save(key=key)
        self.assertFalse(Supplier.objects.exists())

    def test_model_constraints_reject_duplicate_links_codes_and_foreign_relationships(self):
        supplier = self.save(products=[self.line()])
        with transaction.atomic(), self.assertRaises(IntegrityError):
            SupplierProduct.objects.create(company=self.company, supplier=supplier, product=self.product, supplier_code='OTHER')
        with transaction.atomic(), self.assertRaises(IntegrityError):
            SupplierProduct.objects.create(company=self.company, supplier=supplier, product=self.open_product, supplier_code='alpha-1')
        with self.assertRaises(ValidationError):
            SupplierProduct(company=self.company, supplier=supplier, product=self.beta_product, supplier_code='FOREIGN').full_clean()
