"""To order applies the purchasing formula exactly without changing stock or creating orders."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import AuditEvent, MedicationProduct, Shipment, ShipmentItem, StockMovement
from .purchasing import to_order_rows
from .test_operations import OperationsFixture


class ToOrderTests(OperationsFixture):
    week = date(2026, 11, 2)

    def setUp(self):
        StockMovement.objects.filter(company=self.company).update(quantity=0)

    def configure(self, *, lead='1', safety=0, stock=0, product=None, starting=None):
        product = product or self.product
        MedicationProduct.objects.filter(pk=product.pk).update(
            lead_time_weeks=Decimal(lead) if lead is not None else None, safety_stock=safety, starting_awc=starting)
        StockMovement.objects.filter(company=product.company, product=product).update(quantity=stock)

    def supplied(self, quantity, *, day=None, status=Shipment.Status.DISPATCHED):
        day = day or self.week - timedelta(days=1)
        return self.shipment(quantity=quantity, subscription=None, status=status,
            scheduled_for=self.week + timedelta(weeks=4),
            dispatched_at=timezone.make_aware(datetime.combine(day, time(12))))

    def rows(self, **kwargs):
        return to_order_rows(self.company, week_start=self.week, **kwargs)

    def row(self, rows=None, product=None, **kwargs):
        return next(row for row in (rows if rows is not None else self.rows(**kwargs))
                    if row['product'].pk == (product or self.product).pk)

    def test_fractional_lead_time_uses_ceiling_and_clamps_covered_stock_to_zero(self):
        self.configure(lead='2.5', safety=2, stock=1)
        self.supplied(1)

        row = self.row()

        self.assertEqual((row['awc'], row['lead_time_weeks']), (Decimal('0.125'), Decimal('2.50')))
        self.assertEqual((row['order_quantity'], row['status'], row['missing_fields']), (2, 'needed', ()))
        self.configure(lead='2.5', safety=2, stock=10)
        self.assertEqual((self.row()['order_quantity'], self.row()['status']), (0, 'covered'))

    def test_history_average_is_not_display_rounded_before_calculation(self):
        self.configure(lead='8', safety=0)
        self.supplied(1)

        row = self.row()

        self.assertEqual(row['awc'], Decimal('0.125'))
        self.assertEqual(row['order_quantity'], 1)  # Rounding AWC to 0.13 first would incorrectly order two.
        self.assertEqual(row['awc_source'], 'history')

    def test_starting_awc_overrides_history_including_zero_and_clear_returns_to_history(self):
        self.supplied(8)
        for starting, expected_awc, expected_quantity, source in (
            (Decimal('2.50'), Decimal('2.50'), 5, 'starting'),
            (Decimal('0.00'), Decimal('0.00'), 0, 'starting'),
            (None, Decimal('1.0'), 2, 'history'),
        ):
            with self.subTest(starting=starting):
                self.configure(lead='2', safety=0, starting=starting)
                row = self.row()
                self.assertEqual((row['awc'], row['order_quantity'], row['awc_source']),
                                 (expected_awc, expected_quantity, source))

    def test_missing_settings_are_setup_but_explicit_zeroes_are_valid(self):
        for lead, safety, missing in ((None, None, ('Lead time', 'Safety stock')),
                                      ('0', None, ('Safety stock',)), (None, 0, ('Lead time',))):
            with self.subTest(lead=lead, safety=safety):
                self.configure(lead=lead, safety=safety)
                row = self.row()
                self.assertEqual((row['order_quantity'], row['status'], row['missing_fields']), (None, 'setup', missing))
        self.configure(lead='0', safety=0)
        self.assertEqual((self.row()['order_quantity'], self.row()['status']), (0, 'covered'))

    def test_no_history_uses_zero_and_negative_physical_stock_is_replenished(self):
        self.configure(lead='2.5', safety=4, stock=-3)

        row = self.row()

        self.assertEqual((row['awc'], row['on_hand'], row['on_order']), (Decimal('0.0'), -3, 0))
        self.assertEqual(row['order_quantity'], 7)

    def test_reservations_do_not_replace_on_hand_or_count_as_incoming_supplier_stock(self):
        self.configure(lead='1', safety=0, stock=10)
        self.supplied(8)
        self.shipment(quantity=20, subscription=None, status=Shipment.Status.READY, locked_at=timezone.now())

        row = self.row()

        self.assertEqual((row['on_hand'], row['reserved'], row['on_order']), (10, 20, 0))
        self.assertEqual((row['order_quantity'], row['status']), (0, 'covered'))

    def test_explicit_outstanding_order_quantities_reduce_the_shortfall_once(self):
        self.configure(lead='2.5', safety=2, stock=1)
        self.supplied(1)
        for incoming, expected in ((0, 2), (1, 1), (2, 0), (100, 0)):
            with self.subTest(incoming=incoming):
                row = self.row(on_order={self.product.pk: incoming, self.beta_product.pk: 999, 99999999: 999})
                self.assertEqual((row['on_order'], row['order_quantity']), (incoming, expected))

    def test_on_order_mapping_rejects_negative_fractional_text_and_boolean_quantities(self):
        for value in (-1, 1.5, Decimal('2'), '2', True, None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.rows(on_order={self.product.pk: value})
        for value in ([], 'invalid', {True: 1}, {'1': 1}, {-1: 1}):
            with self.subTest(mapping=value), self.assertRaises(ValidationError):
                self.rows(on_order=value)

    def test_only_active_company_products_are_returned_and_foreign_stock_is_not_used(self):
        self.configure(lead='0', safety=1, stock=0)
        MedicationProduct.objects.filter(pk=self.open_product.pk).update(is_active=False)
        StockMovement.objects.create(company=self.beta, product=self.beta_product, actor=self.beta_admin,
                                     quantity=1000, reason='Other practice stock')

        rows = self.rows(on_order={self.beta_product.pk: 1000})

        self.assertEqual([row['product'].pk for row in rows], [self.product.pk])
        self.assertEqual((rows[0]['on_hand'], rows[0]['on_order'], rows[0]['order_quantity']), (0, 0, 1))

    def test_default_anchor_uses_eight_completed_calendar_weeks(self):
        self.configure(lead='1', safety=0)
        self.supplied(8, day=self.week - timedelta(weeks=8))
        self.supplied(16, day=self.week + timedelta(days=1))
        with patch('care.purchasing.timezone.localdate', return_value=self.week + timedelta(days=2)):
            row = self.row(to_order_rows(self.company))

        self.assertEqual((row['awc'], row['order_quantity']), (Decimal('1.0'), 1))

    def test_reading_recommendations_changes_no_stock_reservations_settings_or_audit(self):
        self.configure(lead='2.5', safety=2, stock=-1)
        self.supplied(8)
        self.shipment(quantity=3, subscription=None, status=Shipment.Status.READY, locked_at=timezone.now())
        models = (MedicationProduct, Shipment, ShipmentItem, StockMovement, AuditEvent)
        before = {model: list(model.objects.order_by('pk').values()) for model in models}

        self.rows(on_order={self.product.pk: 2})

        self.assertEqual({model: list(model.objects.order_by('pk').values()) for model in models}, before)
