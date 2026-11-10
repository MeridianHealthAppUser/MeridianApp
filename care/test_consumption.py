"""Reconciliation, calendar boundaries and isolation for consumption analytics."""

from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.utils import timezone

from .consumption import consumption_items, consumption_report, period_items, weekly_items
from .models import Shipment, ShipmentItem
from .test_operations import OperationsFixture


class ConsumptionReportTests(OperationsFixture):
    as_of = date(2026, 11, 4)
    monday = date(2026, 11, 3)

    def at(self, day, clock=time(12)):
        return timezone.make_aware(datetime.combine(day, clock), timezone.get_current_timezone())

    def dispatched(self, day, quantity, **kwargs):
        values = dict(subscription=None, scheduled_for=day, dispatched_at=self.at(day), status=Shipment.Status.DISPATCHED)
        values.update(kwargs)
        return self.shipment(quantity=quantity, **values)

    def report(self, **kwargs):
        return consumption_report(self.company, as_of=self.as_of, **kwargs)

    def row(self, report, product=None):
        return next(row for row in report['rows'] if row['product'].pk == (product or self.product).pk)

    def test_weekly_totals_reconcile_to_exact_lines_including_multiple_lines_and_delivered_shipments(self):
        start = self.monday - timedelta(weeks=1)
        one = self.dispatched(start, 4)
        ShipmentItem.objects.create(company=self.company, shipment=one, product=self.product, dose='Another dose', quantity=2)
        two = self.dispatched(start + timedelta(days=6), 4, status=Shipment.Status.DELIVERED)
        self.dispatched(start + timedelta(days=4), 3, product=self.open_product)

        report = self.report()
        row = self.row(report)
        cell = row['cells'][-1]
        lines = weekly_items(self.company, self.product, start, as_of=report['as_of_at'])

        self.assertEqual(cell['quantity'], 10)
        self.assertEqual(lines.aggregate(total=Sum('quantity'))['total'], 10)
        self.assertEqual(set(lines.values_list('shipment_id', flat=True)), {one.pk, two.pk})
        self.assertEqual(row['total'], 10)
        self.assertEqual((report['total_units'], report['products_with_consumption']), (13, 2))
        self.assertEqual(report['totals_by_week'][-1]['quantity'], 13)

    def test_only_actual_dispatch_statuses_and_dates_contribute(self):
        day = self.monday - timedelta(days=1)
        valid = self.dispatched(day, 8, scheduled_for=self.monday + timedelta(weeks=2))
        for status in (Shipment.Status.DRAFT, Shipment.Status.HELD, Shipment.Status.READY, Shipment.Status.CANCELLED):
            self.dispatched(day, 100, status=status)
        self.dispatched(day, 100, dispatched_at=None)
        self.dispatched(self.monday + timedelta(weeks=2), 100, scheduled_for=day)

        row = self.row(self.report())

        self.assertEqual((row['total'], row['avg_8'], row['units_all']), (8, Decimal('1'), 8))
        self.assertEqual(list(consumption_items(self.company, as_of=self.as_of).values_list('shipment_id', flat=True)), [valid.pk])

    def test_eight_week_average_includes_zero_weeks_and_uses_exact_calendar_bounds(self):
        lower = self.monday - timedelta(weeks=8)
        self.dispatched(lower, 9, dispatched_at=self.at(lower, time.min))
        last = self.monday - timedelta(days=1)
        self.dispatched(last, 7, dispatched_at=self.at(last, time(23, 59, 59)))
        self.dispatched(lower - timedelta(days=1), 80)
        self.dispatched(self.monday, 80, dispatched_at=self.at(self.monday, time.min))
        self.product.starting_awc = Decimal('99')
        self.product.save(update_fields=('starting_awc',))

        row = self.row(self.report())

        self.assertEqual((row['total'], row['units_8'], row['avg_8'], row['weeks_8']), (16, 16, Decimal('2'), Decimal('8')))
        self.assertEqual(sum(cell['quantity'] == 0 for cell in row['cells']), 6)
        self.assertEqual(self.row(self.report(), self.open_product)['avg_8'], Decimal('0'))

    def test_average_periods_are_current_and_do_not_change_with_selected_grid(self):
        self.dispatched(date(2026, 1, 2), 7)
        self.dispatched(date(2026, 10, 28), 8)
        current = self.row(self.report())
        historical = self.row(self.report(end_week=date(2026, 1, 6), weeks=4))

        self.assertEqual(current['total'], 8)
        self.assertEqual(historical['total'], 7)
        for key in ('avg_8', 'avg_year', 'avg_all', 'units_year', 'weeks_year', 'first_week'):
            self.assertEqual(current[key], historical[key], key)

    def test_year_average_includes_partial_first_week_and_zero_days(self):
        self.dispatched(date(2026, 1, 1), 7)
        self.dispatched(date(2026, 1, 4), 1)
        self.dispatched(date(2025, 12, 31), 40)
        self.dispatched(date(2026, 1, 6), 50)

        report = consumption_report(self.company, as_of=date(2026, 1, 7))
        row = self.row(report)

        self.assertEqual(row['units_year'], 8)
        self.assertEqual(row['weeks_year'], Decimal(5) / Decimal(7))
        self.assertAlmostEqual(row['avg_year'], Decimal('11.2'))
        self.assertEqual(report['average_end'], date(2026, 1, 5))

    def test_all_time_starts_with_product_first_dispatch_week_and_counts_quiet_weeks(self):
        first = self.monday - timedelta(weeks=5)
        self.dispatched(first + timedelta(days=3), 10)
        self.dispatched(self.monday - timedelta(days=1), 5)
        self.dispatched(self.monday - timedelta(days=1), 3, product=self.open_product)

        report = self.report()
        first_row, second_row = self.row(report), self.row(report, self.open_product)

        self.assertEqual((first_row['first_week'], first_row['weeks_all'], first_row['avg_all']), (first, Decimal(5), Decimal(3)))
        self.assertEqual((second_row['weeks_all'], second_row['avg_all']), (Decimal(1), Decimal(3)))

    def test_products_with_no_completed_history_have_no_all_time_average(self):
        self.dispatched(self.monday, 8)
        report = self.report(end_week=self.monday)
        row = self.row(report)
        self.assertEqual(row['total'], 8)
        self.assertEqual((row['avg_8'], row['units_all'], row['weeks_all']), (Decimal(0), 0, Decimal(0)))
        self.assertIsNone(row['avg_all'])
        self.assertIsNone(row['first_week'])
        first_days = consumption_report(self.company, as_of=date(2026, 1, 2))
        self.assertIsNone(self.row(first_days)['avg_year'])

    def test_timezone_boundary_is_local_tuesday_not_utc_monday(self):
        # Monday 22:30 UTC is Tuesday 00:30 in Johannesburg.
        local_tuesday = datetime(2026, 10, 26, 22, 30, tzinfo=datetime_timezone.utc)
        local_monday = datetime(2026, 10, 26, 21, 59, tzinfo=datetime_timezone.utc)
        self.dispatched(date(2026, 10, 27), 9, dispatched_at=local_tuesday)
        self.dispatched(date(2026, 10, 26), 4, dispatched_at=local_monday)

        report = self.report()
        row = self.row(report)
        self.assertEqual([cell['quantity'] for cell in row['cells'][-2:]], [4, 9])
        lines = weekly_items(self.company, self.product, date(2026, 10, 27), as_of=self.as_of)
        self.assertEqual(lines.aggregate(total=Sum('quantity'))['total'], 9)

    def test_current_week_is_partial_and_never_counts_future_timestamps(self):
        cutoff = self.at(self.as_of, time(10))
        self.dispatched(self.monday, 2)
        self.dispatched(self.as_of, 7, dispatched_at=self.at(self.as_of, time(11)))
        self.dispatched(self.as_of + timedelta(days=1), 12)
        report = consumption_report(self.company, end_week=self.monday, as_of=cutoff)
        row = self.row(report)

        self.assertTrue(report['weeks'][-1]['is_partial'])
        self.assertEqual(row['total'], 2)
        self.assertEqual(weekly_items(self.company, self.product, self.monday, as_of=report['as_of_at']).aggregate(total=Sum('quantity'))['total'], 2)
        self.assertEqual(self.row(self.report(end_week=self.monday))['total'], 9)

    def test_product_filters_match_category_labels_and_include_inactive_history(self):
        self.product.is_active = False
        self.product.save(update_fields=('is_active',))
        self.dispatched(self.monday - timedelta(days=1), 8)
        report = self.report(query='prescribed dose')
        self.assertEqual([row['product'].pk for row in report['rows']], [self.product.pk])
        self.assertEqual(report['rows'][0]['total'], 8)
        self.assertEqual(self.report(query='NO SUCH PRODUCT')['rows'], [])
        report = self.report(query=self.product.get_category_display(), categories=[self.product.category])
        self.assertIn(self.product.pk, [row['product'].pk for row in report['rows']])

    def test_tenant_boundaries_hold_for_products_items_shipments_and_patients(self):
        day = self.monday - timedelta(days=1)
        self.dispatched(day, 8)
        self.dispatched(day, 80, company=self.beta, patient=self.beta_patient, product=self.beta_product)
        self.dispatched(day, 80, patient=self.beta_patient)
        wrong_item = self.dispatched(day, 80)
        wrong_item.items.update(company=self.beta)
        wrong_shipment = self.dispatched(day, 80, company=self.beta)
        wrong_shipment.items.update(company=self.company)
        self.dispatched(day, 80, product=self.beta_product)

        report = self.report()
        self.assertEqual(self.row(report)['total'], 8)
        self.assertNotIn(self.beta_product.pk, [row['product'].pk for row in report['rows']])
        self.assertEqual(period_items(self.company, report['period_start'], report['period_end'], as_of=report['as_of_at']).aggregate(total=Sum('quantity'))['total'], 8)
        self.assertFalse(weekly_items(self.company, self.beta_product, day, as_of=self.as_of).exists())

    def test_report_uses_fixed_query_count_and_validates_windows(self):
        self.dispatched(self.monday - timedelta(days=1), 8)
        with self.assertNumQueries(3):
            self.report()
        for weeks in (0, 5, 1000, True, '8'):
            with self.subTest(weeks=weeks), self.assertRaises(ValidationError):
                self.report(weeks=weeks)
        with self.assertRaises(ValidationError):
            self.report(end_week=self.monday + timedelta(days=7))
        report = self.report(end_week=self.monday - timedelta(days=1), weeks=4)
        self.assertEqual(report['end_week'], self.monday - timedelta(days=7))
        self.assertEqual(len(report['weeks']), 4)
