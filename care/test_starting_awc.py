from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase

from practices.models import CompanyMembership

from .inventory import effective_awc, set_starting_awc, stock_rows
from .models import AuditEvent, MedicationProduct, ShipmentItem, StockMovement
from .test_operations import OperationsFixture


class EffectiveAWCTests(SimpleTestCase):
    def test_unset_starting_value_uses_exact_historical_decimal(self):
        product = SimpleNamespace(starting_awc=None)
        for historical in (Decimal('1.25'), 2, 0.75, '0.50'):
            with self.subTest(historical=historical):
                actual = effective_awc(product, historical)
                self.assertIsInstance(actual, Decimal)
                self.assertEqual(actual, Decimal(str(historical)))

    def test_explicit_zero_overrides_nonzero_history(self):
        self.assertEqual(effective_awc(SimpleNamespace(starting_awc=Decimal('0.00')), Decimal('8.50')), Decimal('0.00'))

    def test_starting_value_takes_precedence_over_history(self):
        self.assertEqual(effective_awc(SimpleNamespace(starting_awc=Decimal('12.50')), Decimal('0.75')), Decimal('12.50'))

    def test_integer_starting_value_still_returns_a_decimal(self):
        value = effective_awc(SimpleNamespace(starting_awc=4), 0)
        self.assertIsInstance(value, Decimal)
        self.assertEqual(value, Decimal('4'))


class StartingAWCTests(OperationsFixture):
    def set_value(self, value, *, actor=None, product=None):
        return set_starting_awc(company=self.company, actor=actor or self.super_admin,
                                product=product or self.product, value=value)

    def audits(self):
        return AuditEvent.objects.filter(action='inventory.starting_awc_updated').order_by('pk')

    def test_unset_default_and_super_admin_change_are_persisted_and_audited(self):
        self.assertIsNone(self.product.starting_awc)
        updated = self.set_value(Decimal('12.50'))
        self.assertEqual(updated.starting_awc, Decimal('12.50'))
        self.product.refresh_from_db()
        self.assertEqual(self.product.starting_awc, Decimal('12.50'))
        audit = self.audits().get()
        self.assertEqual((audit.company, audit.actor, audit.target_id),
                         (self.company, self.super_admin, str(self.product.pk)))
        self.assertEqual(audit.metadata, {'before': None, 'after': '12.50'})

    def test_zero_overrides_history_and_clearing_restores_historical_average(self):
        zero = self.set_value(0)
        self.assertEqual(zero.starting_awc, Decimal('0.00'))
        self.assertEqual(effective_awc(zero, Decimal('0.75')), Decimal('0.00'))
        cleared = self.set_value(None)
        self.assertIsNone(cleared.starting_awc)
        self.assertEqual(effective_awc(cleared, Decimal('0.75')), Decimal('0.75'))
        self.product.refresh_from_db()
        self.assertIsNone(self.product.starting_awc)
        self.assertEqual(list(self.audits().values_list('metadata', flat=True)),
                         [{'before': None, 'after': '0.00'}, {'before': '0.00', 'after': None}])

    def test_practice_admin_and_other_roles_are_denied(self):
        for actor in (self.admin, self.doctor, self.patient_user, self.beta_admin):
            with self.subTest(actor=actor.pk), self.assertRaises(PermissionDenied):
                self.set_value(2, actor=actor)
        self.product.refresh_from_db()
        self.assertIsNone(self.product.starting_awc)
        self.assertFalse(self.audits().exists())

    def test_inactive_or_demoted_membership_and_super_admin_of_another_practice_are_denied(self):
        membership = CompanyMembership.objects.get(company=self.company, user=self.super_admin)
        CompanyMembership.objects.filter(pk=membership.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.set_value(2)
        CompanyMembership.objects.filter(pk=membership.pk).update(is_active=True, role=CompanyMembership.Role.PRACTICE_ADMIN)
        with self.assertRaises(PermissionDenied):
            self.set_value(2)
        CompanyMembership.objects.filter(company=self.beta, user=self.beta_admin).update(role=CompanyMembership.Role.SUPER_ADMIN)
        with self.assertRaises(PermissionDenied):
            self.set_value(2, actor=self.beta_admin)
        self.assertFalse(self.audits().exists())

    def test_inactive_user_is_denied_even_with_a_stale_active_instance(self):
        type(self.super_admin).objects.filter(pk=self.super_admin.pk).update(is_active=False)
        self.assertTrue(self.super_admin.is_active)
        with self.assertRaises(PermissionDenied):
            self.set_value(2)
        self.product.refresh_from_db()
        self.assertIsNone(self.product.starting_awc)
        self.assertFalse(self.audits().exists())

    def test_foreign_and_missing_products_are_not_changed(self):
        for product in (self.beta_product, MedicationProduct(pk=999999)):
            with self.subTest(product=product.pk), self.assertRaises(ValidationError):
                self.set_value(2, product=product)
        self.beta_product.refresh_from_db()
        self.product.refresh_from_db()
        self.assertIsNone(self.beta_product.starting_awc)
        self.assertIsNone(self.product.starting_awc)
        self.assertFalse(self.audits().exists())

    def test_setting_and_clearing_preserve_stock_reservations_and_other_configuration(self):
        self.product.lead_time_weeks = Decimal('2.50')
        self.product.safety_stock = 8
        self.product.save(update_fields=('lead_time_weeks', 'safety_stock', 'updated_at'))
        shipment = self.locked(self.shipment(quantity=4))
        balances = [(row['product'].pk, row['on_hand'], row['reserved'], row['available'])
                    for row in stock_rows(self.company)]
        movements = list(StockMovement.objects.order_by('pk').values())
        items = list(ShipmentItem.objects.order_by('pk').values())
        shipment_values = (shipment.status, shipment.locked_at, shipment.updated_at)
        self.set_value(Decimal('6.25'))
        self.set_value(None)
        self.product.refresh_from_db()
        shipment.refresh_from_db()
        self.assertEqual((self.product.lead_time_weeks, self.product.safety_stock), (Decimal('2.50'), 8))
        self.assertEqual((shipment.status, shipment.locked_at, shipment.updated_at), shipment_values)
        self.assertEqual(list(StockMovement.objects.order_by('pk').values()), movements)
        self.assertEqual(list(ShipmentItem.objects.order_by('pk').values()), items)
        self.assertEqual([(row['product'].pk, row['on_hand'], row['reserved'], row['available'])
                          for row in stock_rows(self.company)], balances)

    def test_equal_repeated_values_are_idempotent_with_stale_product_instances(self):
        original_updated_at = self.product.updated_at
        self.assertEqual(self.set_value(None).updated_at, original_updated_at)
        self.assertFalse(self.audits().exists())
        changed = self.set_value(4)
        self.assertIsNone(self.product.starting_awc)
        repeated = self.set_value(Decimal('4.00'))
        self.assertEqual((repeated.starting_awc, repeated.updated_at), (Decimal('4.00'), changed.updated_at))
        self.assertEqual(self.audits().count(), 1)

    def test_invalid_types_nonfinite_negative_range_and_precision_do_not_write(self):
        original_updated_at = self.product.updated_at
        invalid = (True, False, '2.5', '', 2.5, 2.0, [], {}, Decimal('NaN'), Decimal('sNaN'),
                   Decimal('Infinity'), Decimal('-Infinity'), -1, Decimal('-0.01'), 100000000,
                   Decimal('100000000.00'), Decimal('1.001'), Decimal('1.000'), Decimal('0E-20'))
        for value in invalid:
            with self.subTest(value=repr(value)), self.assertRaises(ValidationError):
                self.set_value(value)
        self.product.refresh_from_db()
        self.assertEqual((self.product.starting_awc, self.product.updated_at), (None, original_updated_at))
        self.assertFalse(self.audits().exists())

    def test_maximum_value_and_inactive_products_can_be_configured(self):
        self.product.is_active = False
        self.product.save(update_fields=('is_active', 'updated_at'))
        maximum = Decimal('99999999.99')
        self.assertEqual(self.set_value(maximum).starting_awc, maximum)
        self.product.refresh_from_db()
        self.assertEqual(self.product.starting_awc, maximum)
        self.assertFalse(self.product.is_active)
        self.assertEqual(self.audits().get().metadata, {'before': None, 'after': '99999999.99'})

    def test_model_validation_rejects_negative_starting_awc(self):
        self.product.starting_awc = Decimal('-0.01')
        with self.assertRaises(ValidationError) as error:
            self.product.full_clean()
        self.assertIn('starting_awc', error.exception.message_dict)

    def test_audit_failure_rolls_back_the_change(self):
        with patch('care.inventory.record_audit', side_effect=RuntimeError('Audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.set_value(Decimal('3.25'))
        self.product.refresh_from_db()
        self.assertIsNone(self.product.starting_awc)
        self.assertFalse(self.audits().exists())
