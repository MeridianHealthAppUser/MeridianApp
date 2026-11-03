from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError

from practices.models import CompanyMembership

from .inventory import set_lead_time, stock_rows
from .models import AuditEvent, MedicationProduct, PracticeSettings, ShipmentItem, StockMovement
from .test_operations import OperationsFixture


class LeadTimeTests(OperationsFixture):
    def set_weeks(self, weeks, *, actor=None, product=None):
        return set_lead_time(company=self.company, actor=actor or self.super_admin,
                             product=product or self.product, weeks=weeks)

    def audits(self):
        return AuditEvent.objects.filter(action='inventory.lead_time_updated').order_by('pk')

    def test_unset_default_and_decimal_value_are_persisted_and_audited(self):
        self.assertIsNone(self.product.lead_time_weeks)
        updated = self.set_weeks(Decimal('2.50'))
        self.assertEqual(updated.lead_time_weeks, Decimal('2.50'))
        self.product.refresh_from_db()
        self.assertEqual(self.product.lead_time_weeks, Decimal('2.50'))
        audit = self.audits().get()
        self.assertEqual((audit.company, audit.actor, audit.target_id),
                         (self.company, self.super_admin, str(self.product.pk)))
        self.assertEqual(audit.metadata, {'before': None, 'after': '2.50'})

    def test_zero_and_unset_are_distinct_and_clearing_is_audited(self):
        self.assertEqual(self.set_weeks(0).lead_time_weeks, Decimal('0.00'))
        self.assertIsNone(self.set_weeks(None).lead_time_weeks)
        self.product.refresh_from_db()
        self.assertIsNone(self.product.lead_time_weeks)
        self.assertEqual(list(self.audits().values_list('metadata', flat=True)),
                         [{'before': None, 'after': '0.00'}, {'before': '0.00', 'after': None}])

    def test_setting_and_clearing_leave_stock_reservations_and_safety_stock_unchanged(self):
        self.product.safety_stock = 8
        self.product.save(update_fields=('safety_stock', 'updated_at'))
        shipment = self.locked(self.shipment(quantity=4))
        balances = [(row['product'].pk, row['on_hand'], row['reserved'], row['available'])
                    for row in stock_rows(self.company)]
        movements = list(StockMovement.objects.order_by('pk').values())
        items = list(ShipmentItem.objects.order_by('pk').values())
        shipment_values = (shipment.status, shipment.locked_at, shipment.updated_at)
        self.set_weeks(Decimal('1.25'))
        self.set_weeks(None)
        self.product.refresh_from_db()
        shipment.refresh_from_db()
        self.assertEqual(self.product.safety_stock, 8)
        self.assertEqual((shipment.status, shipment.locked_at, shipment.updated_at), shipment_values)
        self.assertEqual(list(StockMovement.objects.order_by('pk').values()), movements)
        self.assertEqual(list(ShipmentItem.objects.order_by('pk').values()), items)
        self.assertEqual([(row['product'].pk, row['on_hand'], row['reserved'], row['available'])
                          for row in stock_rows(self.company)], balances)

    def test_repeated_equal_values_use_persisted_state_without_saving_or_auditing(self):
        original_updated_at = self.product.updated_at
        self.assertEqual(self.set_weeks(None).updated_at, original_updated_at)
        self.assertFalse(self.audits().exists())
        changed = self.set_weeks(2)
        self.assertIsNone(self.product.lead_time_weeks)
        repeated = self.set_weeks(Decimal('2.00'))
        self.assertEqual((repeated.lead_time_weeks, repeated.updated_at), (Decimal('2.00'), changed.updated_at))
        self.assertEqual(self.audits().count(), 1)

    def test_super_admin_can_configure_inactive_products(self):
        self.product.is_active = False
        self.product.save(update_fields=('is_active', 'updated_at'))
        self.assertEqual(self.set_weeks(1).lead_time_weeks, Decimal('1.00'))
        self.assertEqual(self.set_weeks(Decimal('1.50'), actor=self.super_admin).lead_time_weeks, Decimal('1.50'))
        self.product.refresh_from_db()
        self.assertFalse(self.product.is_active)
        self.assertEqual(list(self.audits().values_list('actor_id', flat=True)),
                         [self.super_admin.pk, self.super_admin.pk])

    def test_foreign_and_missing_products_cannot_be_updated(self):
        for product in (self.beta_product, MedicationProduct(pk=999999)):
            with self.subTest(product=product.pk), self.assertRaises(ValidationError):
                self.set_weeks(2, product=product)
        self.beta_product.refresh_from_db()
        self.product.refresh_from_db()
        self.assertIsNone(self.beta_product.lead_time_weeks)
        self.assertIsNone(self.product.lead_time_weeks)
        self.assertFalse(self.audits().exists())

    def test_only_current_super_administrators_can_configure_lead_time(self):
        PracticeSettings.objects.filter(company=self.company).update(doctors_ship=True)
        for actor in (self.admin, self.doctor, self.patient_user, self.beta_admin):
            with self.subTest(actor=actor.pk), self.assertRaises(PermissionDenied):
                self.set_weeks(2, actor=actor)
        CompanyMembership.objects.filter(company=self.company, user=self.super_admin).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.set_weeks(2)
        self.product.refresh_from_db()
        self.assertIsNone(self.product.lead_time_weeks)
        self.assertFalse(self.audits().exists())

    def test_invalid_values_do_not_write_or_audit(self):
        original_updated_at = self.product.updated_at
        invalid = (True, False, '2.5', '', 2.5, 2.0, [], {}, Decimal('NaN'), Decimal('sNaN'),
                   Decimal('Infinity'), Decimal('-Infinity'), -1, Decimal('-0.01'), 10000,
                   Decimal('10000.00'), Decimal('1.001'), Decimal('1.000'), Decimal('0E-20'))
        for weeks in invalid:
            with self.subTest(weeks=repr(weeks)), self.assertRaises(ValidationError):
                self.set_weeks(weeks)
        self.product.refresh_from_db()
        self.assertEqual((self.product.lead_time_weeks, self.product.updated_at), (None, original_updated_at))
        self.assertFalse(self.audits().exists())

    def test_maximum_and_two_decimal_places_are_accepted(self):
        maximum = Decimal('9999.99')
        self.assertEqual(self.set_weeks(maximum).lead_time_weeks, maximum)
        self.product.refresh_from_db()
        self.assertEqual(self.product.lead_time_weeks, maximum)
        self.assertEqual(self.audits().get().metadata, {'before': None, 'after': '9999.99'})

    def test_model_validation_rejects_negative_lead_time(self):
        self.product.lead_time_weeks = Decimal('-0.01')
        with self.assertRaises(ValidationError) as error:
            self.product.full_clean()
        self.assertIn('lead_time_weeks', error.exception.message_dict)

    def test_audit_failure_rolls_back_the_product_change(self):
        with patch('care.inventory.record_audit', side_effect=RuntimeError('Audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.set_weeks(Decimal('2.25'))
        self.product.refresh_from_db()
        self.assertIsNone(self.product.lead_time_weeks)
        self.assertFalse(self.audits().exists())
