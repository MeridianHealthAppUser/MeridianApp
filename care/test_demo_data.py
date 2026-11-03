"""Managed demo ownership never extends into real accounts or financial history."""
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import override_settings
from django.utils import timezone

from practices.models import CompanyMembership, DemoDataSet, Patient
from .consumption import consumption_report
from .demo_data import clear_demo_data, create_demo_data, demo_status, rebuild_demo_data
from .inventory import stock_rows
from .models import (
    AuditEvent, ClinicalNote, ClinicalTask, DeliveryAddress, EmailDelivery, Invoice, MedicationProduct, Payment,
    PracticeSettings, PurchaseOrder, PurchaseOrderItem, Shipment, ShipmentItem,
    StockMovement, SubscriptionCycle, Supplier, SupplierProduct, RecordTag, TaskTagAssignment,
)
from .operations import confirm_shipments
from .purchasing import create_purchase_order, receive_purchase_order
from .services import record_audit
from .test_purchase_orders import PurchaseOrderFixture


class DemoDataTests(PurchaseOrderFixture):
    def create_demo(self, key=None, actor=None, company=None):
        return create_demo_data(company=company or self.company, actor=actor or self.super_admin,
                                submission_key=key or uuid4())

    def clear_demo(self, dataset, key=None, **kwargs):
        return clear_demo_data(company=self.company, actor=self.super_admin, dataset=dataset,
                               submission_key=key or uuid4(), **kwargs)

    def protected_snapshot(self):
        models = (get_user_model(), CompanyMembership, PracticeSettings, Payment, SubscriptionCycle,
                  Invoice, EmailDelivery)
        return {model._meta.label: list(model.objects.order_by('pk').values()) for model in models}

    def test_bounded_production_dataset_has_owned_scenarios_and_reconciling_consumption(self):
        before = self.protected_snapshot()
        with override_settings(DEBUG=False, EMAIL_ENABLED=True, PAYMENT_PROVIDER='production'), \
                patch('care.notifications.queue') as notify:
            dataset = self.create_demo()
        self.assertEqual(self.protected_snapshot(), before)
        notify.assert_not_called()
        self.assertEqual(demo_status(self.company)['counts'], {
            'patients': 8, 'products': 4, 'suppliers': 2, 'purchase_orders': 3,
            'subscriptions': 7, 'shipments': 47, 'stock_movements': 40,
        })
        self.assertTrue(dataset.summary['prescriber_available'])
        self.assertFalse(Patient.objects.filter(demo_data_set=dataset, user__isnull=False).exists())
        self.assertEqual(set(Shipment.objects.filter(patient__demo_data_set=dataset).values_list('status', flat=True)),
                         {'draft', 'ready', 'dispatched', 'delivered', 'held'})
        rows = [row for row in stock_rows(self.company) if row['product'].demo_data_set_id == dataset.pk]
        self.assertEqual(sorted(row['on_hand'] for row in rows), [1, 2, 8, 20])
        tirz = next(row for row in rows if 'Tirzepatide' in row['product'].name)
        self.assertEqual((tirz['reserved'], tirz['available']), (2, -1))
        report = consumption_report(self.company, query=str(dataset.pk)[:8])
        self.assertEqual(report['product_count'], 4)
        for row in report['rows']:
            self.assertEqual(row['avg_8'], Decimal(row['units_8']) / 8)
            self.assertTrue(all(cell['quantity'] > 0 for cell in row['cells']))
        orders = PurchaseOrder.objects.filter(demo_data_set=dataset)
        self.assertEqual(orders.filter(status='open').count(), 2)
        self.assertEqual(orders.filter(status='received').count(), 1)
        for line in PurchaseOrderItem.objects.filter(purchase_order__demo_data_set=dataset):
            self.assertEqual(line.quantity_received, sum(line.stock_movements.values_list('quantity', flat=True)))

    def test_create_is_idempotent_and_requires_explicit_rebuild(self):
        key = uuid4()
        dataset = self.create_demo(key)
        self.assertEqual(self.create_demo(str(key)).pk, dataset.pk)
        self.assertEqual(DemoDataSet.objects.count(), 1)
        with self.assertRaisesMessage(ValidationError, 'already exists'):
            self.create_demo()
        self.assertEqual(AuditEvent.objects.filter(action='demo_data.created').count(), 1)
        self.clear_demo(dataset)
        self.assertEqual(self.create_demo(key).status, 'cleared')
        self.assertFalse(Patient.objects.filter(demo_data_set=dataset).exists())

    def test_super_admin_permission_is_fresh_and_company_scoped_including_retries(self):
        for actor in (self.admin, self.doctor, self.patient_user, self.beta_admin):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                self.create_demo(actor=actor)
        key = uuid4()
        dataset = self.create_demo(key)
        CompanyMembership.objects.filter(company=self.company, user=self.super_admin).update(is_active=False)
        for operation in (lambda: self.create_demo(key), lambda: self.clear_demo(dataset)):
            with self.assertRaises(PermissionDenied):
                operation()
        self.assertTrue(Patient.objects.filter(demo_data_set=dataset).exists())

    def test_invalid_submission_keys_do_not_write_anything(self):
        for key in (None, '', 1, 'invalid'):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                create_demo_data(company=self.company, actor=self.super_admin, submission_key=key)
        self.assertFalse(DemoDataSet.objects.exists())

    def test_no_prescriber_creates_no_accounts_or_invalid_authorizations(self):
        CompanyMembership.objects.filter(company=self.company, clinician_type='doctor').update(is_active=False)
        before = self.protected_snapshot()
        dataset = self.create_demo()
        self.assertFalse(dataset.summary['prescriber_available'])
        self.assertFalse(Patient.objects.filter(demo_data_set=dataset, assigned_doctor__isnull=False).exists())
        from .models import TreatmentAuthorization
        self.assertFalse(TreatmentAuthorization.objects.filter(patient__demo_data_set=dataset).exists())
        self.assertEqual(self.protected_snapshot(), before)
        self.clear_demo(dataset)

    def test_clear_removes_only_owned_records_and_retains_every_audit_and_existing_demo_named_row(self):
        legacy = Patient.objects.create(company=self.company, first_name='Legacy', last_name='Demo')
        existing_order = self.create_order()
        preserved = {model: list(model.objects.order_by('pk').values()) for model in
                     (Patient, MedicationProduct, Supplier, SupplierProduct, PurchaseOrder, PurchaseOrderItem, StockMovement)}
        before = self.protected_snapshot()
        dataset = self.create_demo()
        patient = Patient.objects.filter(demo_data_set=dataset).first()
        record_audit(company=self.company, actor=self.super_admin, patient=patient,
                     action='demo.test_note', target=patient, metadata={'name': str(patient)})
        retained_audit = AuditEvent.objects.get(action='demo.test_note')
        key = uuid4()
        cleared = self.clear_demo(dataset, key)
        self.assertEqual(cleared.status, 'cleared')
        self.assertEqual(cleared.cleared_by, self.super_admin)
        self.assertFalse(demo_status(self.company)['active'])
        self.assertEqual(demo_status(self.company)['counts'], {})
        self.assertEqual(self.clear_demo(dataset, key).pk, dataset.pk)
        for model, expected in preserved.items():
            self.assertEqual(list(model.objects.order_by('pk').values()), expected, model._meta.label)
        self.assertEqual(self.protected_snapshot(), before)
        retained_audit.refresh_from_db()
        self.assertIsNone(retained_audit.patient_id)
        self.assertEqual(retained_audit.metadata['name'], str(patient))
        self.assertTrue(Patient.objects.filter(pk=legacy.pk).exists())
        self.assertTrue(PurchaseOrder.objects.filter(pk=existing_order.pk).exists())

    def test_new_demo_purchase_and_partial_receipt_are_included_in_cleanup(self):
        dataset = self.create_demo()
        supplier = Supplier.objects.filter(demo_data_set=dataset).first()
        product = supplier.product_links.first().product
        order = create_purchase_order(company=self.company, actor=self.admin, supplier=supplier,
            items=[{'product': product, 'quantity': 4}], submission_key=uuid4())
        self.assertEqual(order.demo_data_set_id, dataset.pk)
        line = order.items.get()
        movements = receive_purchase_order(company=self.company, actor=self.admin, order=order,
            items=[{'item': line, 'quantity': 2, 'batch': 'DEMO-PARTIAL', 'expires_on': self.today}],
            submission_key=uuid4())
        self.assertTrue(movements)
        self.clear_demo(dataset)
        self.assertFalse(PurchaseOrder.objects.filter(pk=order.pk).exists())
        self.assertFalse(StockMovement.objects.filter(pk=movements[0].pk).exists())

    def test_pending_parcels_can_confirm_and_to_order_has_useful_positive_examples(self):
        dataset = self.create_demo()
        today = timezone.localdate()
        from datetime import timedelta
        week = today-timedelta(days=today.weekday())
        pending = list(Shipment.objects.filter(patient__demo_data_set=dataset, status='draft',
            scheduled_for__range=(week, week+timedelta(days=6))))
        before = self.protected_snapshot()
        confirmed = confirm_shipments(company=self.company, actor=self.admin, week_start=week,
                                      shipment_ids=[shipment.pk for shipment in pending])
        self.assertEqual(len(confirmed), 3)
        self.assertTrue(all(shipment.locked_at for shipment in confirmed))
        self.assertEqual(self.protected_snapshot(), before)
        from .purchasing import to_order_rows
        rows = [row for row in to_order_rows(self.company) if row['product'].demo_data_set_id == dataset.pk]
        self.assertEqual(len([row for row in rows if row['order_quantity'] > 0]), 2)
        self.clear_demo(dataset)

    def test_demo_clinical_tasks_and_notes_can_be_removed_without_deleting_shared_tags(self):
        dataset = self.create_demo()
        tag = RecordTag.objects.create(company=self.company, name='Existing shared clinical label')
        task = ClinicalTask.objects.filter(patient__demo_data_set=dataset).first()
        TaskTagAssignment.objects.create(company=self.company, task=task, tag=tag)
        self.assertEqual(ClinicalNote.objects.filter(patient__demo_data_set=dataset).count(), 8)
        self.clear_demo(dataset)
        self.assertTrue(RecordTag.objects.filter(pk=tag.pk).exists())
        self.assertFalse(ClinicalTask.objects.filter(pk=task.pk).exists())

    def test_financial_dependency_blocks_clear_atomically_instead_of_deleting_history(self):
        dataset = self.create_demo()
        patient = Patient.objects.filter(demo_data_set=dataset).first()
        payment = Payment.objects.create(company=self.company, patient=patient, amount=Decimal('12'),
            due_on=self.today, status='paid', method='cash')
        before = self.protected_snapshot()
        counts = demo_status(self.company)['counts']
        with self.assertRaisesMessage(ValidationError, 'Nothing was removed'):
            self.clear_demo(dataset)
        self.assertEqual(demo_status(self.company)['counts'], counts)
        self.assertEqual(self.protected_snapshot(), before)
        self.assertTrue(Payment.objects.filter(pk=payment.pk).exists())

    def test_email_dependency_blocks_clear_without_even_detaching_email_patient(self):
        dataset = self.create_demo()
        patient = Patient.objects.filter(demo_data_set=dataset).first()
        email = EmailDelivery.objects.create(company=self.company, patient=patient,
            recipient_email='real-history@example.test', subject='Preserve me', status='sent')
        with self.assertRaises(ValidationError):
            self.clear_demo(dataset)
        email.refresh_from_db()
        self.assertEqual(email.patient_id, patient.pk)
        self.assertEqual(DemoDataSet.objects.get(pk=dataset.pk).status, 'active')

    def test_cross_links_to_real_or_other_practice_roots_block_cleanup(self):
        dataset = self.create_demo()
        product = MedicationProduct.objects.filter(demo_data_set=dataset).first()
        link = SupplierProduct.objects.create(company=self.company, supplier=self.supplier,
            product=product, supplier_code='REAL-LINK')
        with self.assertRaisesMessage(ValidationError, 'outside this demo dataset'):
            self.clear_demo(dataset)
        link.delete()
        shipment = self.shipment()
        mixed = ShipmentItem.objects.create(company=self.company, shipment=shipment, product=product,
                                             dose='Do not delete real shipment', quantity=1)
        with self.assertRaises(ValidationError):
            self.clear_demo(dataset)
        mixed.delete()
        bad = StockMovement.objects.create(company=self.beta, product=product, quantity=1, actor=self.beta_admin)
        with self.assertRaisesMessage(ValidationError, 'another practice'):
            self.clear_demo(dataset)
        self.assertTrue(StockMovement.objects.filter(pk=bad.pk).exists())
        self.assertTrue(Shipment.objects.filter(pk=shipment.pk).exists())

    def test_patient_linked_to_existing_login_cannot_be_removed(self):
        dataset = self.create_demo()
        patient = Patient.objects.filter(demo_data_set=dataset).first()
        user = get_user_model().objects.create_user('linked-real-account@example.test')
        Patient.objects.filter(pk=patient.pk).update(user=user)
        with self.assertRaisesMessage(ValidationError, 'patient login'):
            self.clear_demo(dataset)
        self.assertTrue(get_user_model().objects.filter(pk=user.pk).exists())
        self.assertTrue(Patient.objects.filter(pk=patient.pk).exists())

    def test_rebuild_changes_only_current_dataset_and_is_idempotent(self):
        first = self.create_demo()
        old_patient_ids = list(Patient.objects.filter(demo_data_set=first).values_list('pk', flat=True))
        before = self.protected_snapshot()
        key = uuid4()
        second = rebuild_demo_data(company=self.company, actor=self.super_admin, dataset=first,
            expected_updated=first.updated_at.isoformat(), submission_key=key)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(DemoDataSet.objects.get(pk=first.pk).status, 'cleared')
        self.assertFalse(Patient.objects.filter(pk__in=old_patient_ids).exists())
        self.assertEqual(self.protected_snapshot(), before)
        self.assertEqual(rebuild_demo_data(company=self.company, actor=self.super_admin, dataset=first,
            expected_updated=first.updated_at.isoformat(), submission_key=key).pk, second.pk)
        with self.assertRaises(ValidationError):
            self.clear_demo(first)
        self.assertEqual(demo_status(self.company)['dataset'].pk, second.pk)

    def test_stale_or_foreign_dataset_is_rejected_and_rebuild_failure_restores_old_records(self):
        dataset = self.create_demo()
        with self.assertRaisesMessage(ValidationError, 'changed'):
            self.clear_demo(dataset, expected_updated='2000-01-01T00:00:00+00:00')
        foreign = DemoDataSet.objects.create(company=self.beta, created_by=self.super_admin)
        with self.assertRaises(ValidationError):
            self.clear_demo(foreign)
        before = {model: list(model.objects.order_by('pk').values()) for model in
                  (Patient, StockMovement, PurchaseOrder, Shipment, DemoDataSet)}
        with patch('care.demo_data._seed', side_effect=RuntimeError('generator failed')):
            with self.assertRaises(RuntimeError):
                rebuild_demo_data(company=self.company, actor=self.super_admin, dataset=dataset, submission_key=uuid4())
        for model, expected in before.items():
            self.assertEqual(list(model.objects.order_by('pk').values()), expected)

    def test_audit_failure_rolls_back_create_and_clear(self):
        with patch('care.demo_data.record_audit', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.create_demo()
        self.assertFalse(DemoDataSet.objects.exists())
        dataset = self.create_demo()
        counts = demo_status(self.company)['counts']
        with patch('care.demo_data.record_audit', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.clear_demo(dataset)
        self.assertEqual(demo_status(self.company)['counts'], counts)
        self.assertEqual(DemoDataSet.objects.get(pk=dataset.pk).status, 'active')
