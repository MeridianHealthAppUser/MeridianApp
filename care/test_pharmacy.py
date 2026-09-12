"""Local baskets reserve allowances, not money or physical inventory."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from practices.models import Patient

from .models import (AuditEvent, MedicationProduct, PatientSubscription, Payment, PharmacyOrder,
                     PharmacyOrderItem, Shipment, ShipmentItem, StockMovement, TreatmentAuthorization)
from .operations import product_allowance
from .pharmacy import accept_order, cancel_order, set_basket_quantity, submit_basket
from .test_operations import OperationsFixture


ADDRESS = {'line1': '1 Test Road', 'line2': '', 'city': 'Cape Town', 'province': 'Western Cape',
           'postal_code': '8000', 'phone': '0820000000'}


class PharmacyRequestTests(OperationsFixture):
    def basket(self, **extra):
        values = dict(company=self.company, patient=self.patient, actor=self.patient_user, product=self.product, quantity=2)
        values.update(extra)
        return set_basket_quantity(**values)

    def submit(self, order=None, **extra):
        order = order or self.basket()
        values = dict(order=order, actor=self.patient_user, delivery_address=ADDRESS, note='Leave with recipient',
                      confirm=True, expected_revision=order.revision)
        values.update(extra)
        return submit_basket(**values)

    def accept(self, order=None, **extra):
        values = dict(order=order or self.submit(), actor=self.admin, scheduled_for=self.today, confirm=True)
        values.update(extra)
        return accept_order(**values)

    def test_quantity_is_set_not_added_and_repeat_does_not_duplicate_order_item_or_audit(self):
        order = self.basket()
        counts = (AuditEvent.objects.count(), order.revision)
        repeated = self.basket()
        self.assertEqual(order.pk, repeated.pk)
        self.assertEqual(repeated.items.get().quantity, 2)
        self.assertEqual((AuditEvent.objects.count(), repeated.revision), counts)
        self.assertEqual(PharmacyOrder.objects.count(), 1)
        self.assertEqual(PharmacyOrderItem.objects.count(), 1)
        self.assertEqual(repeated.subtotal, Decimal('200.00'))
        self.assertFalse(StockMovement.objects.exists())
        self.assertFalse(Payment.objects.exists())

    def test_only_patient_owner_can_modify_basket_and_inactive_database_identity_is_rechecked(self):
        for actor in (self.admin, self.super_admin, self.doctor, self.other_user):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                self.basket(actor=actor)
        get_user_model().objects.filter(pk=self.patient_user.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.basket()
        self.assertFalse(PharmacyOrder.objects.exists())

    def test_foreign_patient_and_foreign_product_are_controlled_rejections(self):
        with self.assertRaises(PermissionDenied):
            self.basket(patient=self.beta_patient)
        with self.assertRaises(ValidationError):
            self.basket(product=self.beta_product)
        self.assertFalse(PharmacyOrder.objects.exists())

    def test_quantity_bounds_zero_removal_and_unauthorized_product(self):
        for quantity in (-1, 101, True, 1.5):
            with self.subTest(quantity=quantity), self.assertRaises(ValidationError):
                self.basket(quantity=quantity)
        order = self.basket()
        result = self.basket(quantity=0)
        self.assertEqual(result.pk, order.pk)
        self.assertFalse(result.items.exists())
        self.assertEqual(result.subtotal, 0)
        unknown = MedicationProduct.objects.create(company=self.company, name='Unprescribed strength', price=200)
        with self.assertRaises(ValidationError):
            self.basket(product=unknown)

    def test_grouped_authorized_products_share_the_lowest_explicit_quantity_cap(self):
        self.product.allowance_group = 'combined'
        self.product.save()
        second = MedicationProduct.objects.create(company=self.company, name='Explicit alternative', price=100, allowance_group='combined')
        TreatmentAuthorization.objects.create(company=self.company, patient=self.patient, product=second,
            prescribed_by=self.doctor, max_dose='Alternative clinician instruction', quantity_per_cycle=3,
            starts_on=self.today, expires_on=self.today + timedelta(days=100))
        self.basket(quantity=2)
        with self.assertRaises(ValidationError):
            self.basket(product=second, quantity=2)
        order = self.basket(product=second, quantity=1)
        self.assertEqual(order.items.count(), 2)

    def test_submission_requires_confirmation_fresh_revision_complete_address_and_no_card_fields(self):
        order = self.basket()
        for extra in ({'confirm': False}, {'expected_revision': -1}, {'delivery_address': {}},
                      {'delivery_address': {**ADDRESS, 'card_number': 'not-allowed'}},
                      {'delivery_address': {**ADDRESS, 'city': 'x' * 201}}, {'note': 'x' * 1001}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                self.submit(order, **extra)
        order.refresh_from_db()
        self.assertEqual(order.status, 'draft')
        self.assertFalse(Payment.objects.exists())

    def test_submit_is_idempotent_reserves_allowance_only_and_preserves_submitted_lines(self):
        batch = self.receive()
        order = self.submit(self.basket(quantity=3))
        lines = list(order.items.values())
        counts = (AuditEvent.objects.count(), PharmacyOrder.objects.count(), StockMovement.objects.count())
        replay = self.submit(order)
        self.assertEqual(replay.pk, order.pk)
        self.assertEqual((AuditEvent.objects.count(), PharmacyOrder.objects.count(), StockMovement.objects.count()), counts)
        self.assertEqual(product_allowance(self.patient, self.product)[1], 1)
        self.assert_stock(batch, 10)
        self.assertFalse(Shipment.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.product.price = 500
        self.product.name = 'Renamed later'
        self.product.save()
        self.basket(product=self.open_product, quantity=1)
        self.assertEqual(list(order.items.values()), lines)
        self.assertEqual(order.subtotal, Decimal('300.00'))

    def test_other_pending_request_prevents_over_allowance_and_cancel_releases_reservation(self):
        first = self.submit(self.basket(quantity=3))
        with self.assertRaises(ValidationError):
            self.basket(quantity=2)
        cancel_order(order=first, actor=self.patient_user, confirm=True)
        self.assertEqual(product_allowance(self.patient, self.product)[1], 4)
        self.assertEqual(self.basket(quantity=4).items.get().quantity, 4)

    def test_submit_rechecks_expired_authorization_and_inactive_product(self):
        order = self.basket()
        TreatmentAuthorization.objects.filter(pk=self.auth.pk).update(status='paused')
        with self.assertRaises(ValidationError):
            self.submit(order)
        TreatmentAuthorization.objects.filter(pk=self.auth.pk).update(status='active')
        MedicationProduct.objects.filter(pk=self.product.pk).update(is_active=False)
        with self.assertRaises(ValidationError):
            self.submit(order)
        order.refresh_from_db()
        self.assertEqual(order.status, 'draft')

    def test_acceptance_is_operations_only_idempotent_and_stock_remains_unallocated(self):
        batch = self.receive()
        order = self.submit()
        for actor in (self.patient_user, self.doctor, self.beta_admin):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                self.accept(order, actor=actor)
        shipment = self.accept(order)
        audit_count = AuditEvent.objects.count()
        repeated = self.accept(order)
        self.assertEqual(shipment.pk, repeated.pk)
        self.assertEqual(AuditEvent.objects.count(), audit_count)
        self.assertEqual(Shipment.objects.count(), 1)
        self.assertEqual(shipment.status, 'draft')
        self.assertIsNone(shipment.items.get().batch_id)
        self.assert_stock(batch, 10)
        self.assertFalse(Payment.objects.exists())

    def test_acceptance_validates_allowance_in_scheduled_cycle_before_creating_any_shipment(self):
        future = self.today + timedelta(days=28)
        self.shipment(quantity=4, scheduled_for=future)
        order = self.submit(self.basket(quantity=2))
        with self.assertRaises(ValidationError):
            self.accept(order, scheduled_for=future)
        order.refresh_from_db()
        self.assertEqual(order.status, 'submitted')
        self.assertIsNone(order.shipment_id)
        self.assertEqual(Shipment.objects.count(), 1)

    def test_acceptance_requires_explicit_confirmation_and_non_past_date(self):
        order = self.submit()
        for extra in ({'confirm': False}, {'scheduled_for': self.today - timedelta(days=1)}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                self.accept(order, **extra)
        self.assertFalse(Shipment.objects.exists())

    def test_cancel_is_owner_or_operations_only_soft_and_idempotent(self):
        order = self.submit()
        lines = list(order.items.values())
        for actor in (self.doctor, self.other_user, self.beta_admin):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                cancel_order(order=order, actor=actor, confirm=True)
        with self.assertRaises(ValidationError):
            cancel_order(order=order, actor=self.patient_user)
        cancelled = cancel_order(order=order, actor=self.admin, confirm=True)
        count = AuditEvent.objects.count()
        cancel_order(order=cancelled, actor=self.patient_user, confirm=True)
        self.assertEqual(AuditEvent.objects.count(), count)
        self.assertEqual(list(order.items.values()), lines)
        self.assertEqual(PharmacyOrder.objects.count(), 1)

    def test_accepted_order_cannot_be_cancelled_or_resubmitted_into_another_shipment(self):
        order = self.submit()
        shipment = self.accept(order)
        for actor in (self.patient_user, self.admin):
            with self.subTest(actor=actor), self.assertRaises(ValidationError):
                cancel_order(order=order, actor=actor, confirm=True)
        replay = self.submit(order)
        self.assertEqual(replay.shipment_id, shipment.pk)
        self.assertEqual(Shipment.objects.count(), 1)

    def test_shared_sso_patient_baskets_remain_separate_by_practice(self):
        order = self.basket()
        beta_open = MedicationProduct.objects.create(company=self.beta, name='Beta open product', price=10, requires_authorisation=False)
        beta_order = self.basket(company=self.beta, patient=self.beta_patient, product=beta_open)
        self.assertNotEqual(order.pk, beta_order.pk)
        self.assertEqual(order.company_id, self.company.pk)
        self.assertEqual(beta_order.company_id, self.beta.pk)
        with self.assertRaises(PermissionDenied):
            accept_order(order=beta_order, actor=self.admin, scheduled_for=self.today, confirm=True)

    def test_numeric_named_allowance_group_does_not_collide_with_unrelated_product_primary_key(self):
        second = MedicationProduct.objects.create(company=self.company, name='Independent cap', price=100,
                                                  allowance_group=str(self.product.pk))
        TreatmentAuthorization.objects.create(company=self.company, patient=self.patient, product=second,
            prescribed_by=self.doctor, max_dose='Independent clinician instruction', quantity_per_cycle=4,
            starts_on=self.today, expires_on=self.today + timedelta(days=100))
        self.basket(quantity=3)
        order = self.basket(product=second, quantity=3)
        self.assertEqual(order.items.count(), 2)

    def test_inactive_patient_blocks_basket_and_inactive_account_cannot_cancel_existing_order(self):
        order = self.submit()
        Patient.objects.filter(pk=self.patient.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.basket()
        Patient.objects.filter(pk=self.patient.pk).update(is_active=True)
        get_user_model().objects.filter(pk=self.patient_user.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            cancel_order(order=order, actor=self.patient_user, confirm=True)
        order.refresh_from_db()
        self.assertEqual(order.status, 'submitted')

    def test_corrupt_order_item_company_is_not_silently_submitted(self):
        order = self.basket()
        order.items.update(company=self.beta)
        with self.assertRaises(ValidationError):
            self.submit(order)
        order.refresh_from_db()
        self.assertEqual(order.status, 'draft')

    def test_setting_existing_quantity_refreshes_price_in_draft_only(self):
        order = self.basket()
        MedicationProduct.objects.filter(pk=self.product.pk).update(price=Decimal('125.00'))
        refreshed = self.basket()
        self.assertEqual(refreshed.pk, order.pk)
        self.assertEqual(refreshed.items.get().unit_price, Decimal('125.00'))
        self.assertEqual(refreshed.subtotal, Decimal('250.00'))
        self.assertEqual(refreshed.revision, order.revision + 1)

    def test_truthy_strings_do_not_replace_explicit_confirmation(self):
        order = self.basket()
        with self.assertRaises(ValidationError):
            self.submit(order, confirm='yes')
        order = self.submit(order)
        with self.assertRaises(ValidationError):
            cancel_order(order=order, actor=self.patient_user, confirm='yes')
        with self.assertRaises(ValidationError):
            self.accept(order, confirm='yes')
