"""Historical weekly consumption stays separate from the upcoming shipping-demand week."""

from datetime import date, datetime, time, timedelta

from django.utils import timezone

from .models import Shipment, ShipmentItem
from .operations import AVERAGE_WEEKS, week_stock
from .test_operations import OperationsFixture


class InventoryConsumptionTests(OperationsFixture):
    week = date(2026, 11, 2)

    def at(self, day, clock=time(12)):
        return timezone.make_aware(datetime.combine(day, clock), timezone.get_current_timezone())

    def supplied(self, quantity, day, *, clock=time(12), **extra):
        values = dict(subscription=None, status=Shipment.Status.DISPATCHED,
                      scheduled_for=day, dispatched_at=self.at(day, clock))
        values.update(extra)
        return self.shipment(quantity=quantity, **values)

    def row(self, rows, product=None):
        return next(row for row in rows if row['product'].pk == (product or self.product).pk)

    def test_average_uses_all_eight_weeks_with_inclusive_bounds_and_actual_dispatch_dates(self):
        lower = self.week - timedelta(weeks=8)
        upper = self.week - timedelta(days=1)
        self.supplied(9, lower, clock=time.min, scheduled_for=self.week + timedelta(weeks=2))
        self.supplied(7, upper, clock=time(23, 59, 59), status=Shipment.Status.DELIVERED,
                      scheduled_for=self.week + timedelta(weeks=2))
        self.supplied(4, self.week - timedelta(weeks=2), scheduled_for=self.week + timedelta(weeks=2))
        self.supplied(100, lower - timedelta(days=1), clock=time(23, 59, 59), scheduled_for=upper)
        self.supplied(100, self.week, clock=time.min, scheduled_for=upper)
        # A planned date in range cannot replace a missing actual-dispatch timestamp.
        self.shipment(quantity=100, subscription=None, status=Shipment.Status.DISPATCHED, scheduled_for=upper)
        for status in (Shipment.Status.DRAFT, Shipment.Status.READY, Shipment.Status.HELD, Shipment.Status.CANCELLED):
            self.supplied(100, upper, status=status)

        row = self.row(week_stock(self.company, self.week))

        self.assertEqual(AVERAGE_WEEKS, 8)
        self.assertEqual(row['average'], 20 / 8)

    def test_consumption_can_use_completed_calendar_weeks_without_changing_future_shipping_demand(self):
        planning_week = self.week + timedelta(weeks=1)
        self.supplied(8, self.week - timedelta(weeks=8))
        self.supplied(8, self.week - timedelta(days=1))
        self.supplied(16, self.week + timedelta(days=2))
        self.shipment(quantity=5, subscription=None, scheduled_for=planning_week + timedelta(days=1))

        default = self.row(week_stock(self.company, planning_week))
        completed = self.row(week_stock(self.company, planning_week, consumption_week_start=self.week))

        self.assertEqual(default['average'], 3.0)
        self.assertEqual(completed['average'], 2.0)
        self.assertEqual((default['to_ship'], completed['to_ship']), (5, 5))
        self.assertEqual(week_stock(self.company, planning_week),
                         week_stock(self.company, planning_week, consumption_week_start=planning_week))

    def test_zero_history_and_fractional_consumption_are_not_rounded_or_active_weeks_normalised(self):
        empty = self.row(week_stock(self.company, self.week))
        self.assertEqual(empty['average'], 0.0)
        self.supplied(1, self.week - timedelta(days=1))

        consumed = self.row(week_stock(self.company, self.week))

        self.assertEqual(consumed['average'], 0.125)
        self.assertIsInstance(consumed['average'], float)
        self.assertEqual(self.row(week_stock(self.company, self.week), self.open_product)['average'], 0.0)

    def test_inactive_products_keep_their_history_only_when_requested(self):
        self.supplied(8, self.week - timedelta(days=1))
        self.product.is_active = False
        self.product.save(update_fields=('is_active',))

        self.assertNotIn(self.product.pk, {row['product'].pk for row in week_stock(self.company, self.week)})
        included = self.row(week_stock(self.company, self.week, include_inactive=True))
        self.assertEqual(included['average'], 1.0)

    def test_each_company_boundary_is_enforced_for_history_and_scheduled_demand(self):
        day = self.week - timedelta(days=1)
        self.supplied(8, day)
        self.shipment(quantity=3, subscription=None, scheduled_for=self.week + timedelta(days=1))
        self.supplied(80, day, company=self.beta, patient=self.beta_patient, product=self.beta_product)
        self.supplied(80, day, patient=self.beta_patient)
        wrong_item_company = self.supplied(80, day)
        ShipmentItem.objects.filter(shipment=wrong_item_company).update(company=self.beta)
        wrong_shipment_company = self.supplied(80, day, company=self.beta,
                                              scheduled_for=self.week + timedelta(days=1))
        ShipmentItem.objects.filter(shipment=wrong_shipment_company).update(company=self.company)
        self.supplied(80, day, product=self.beta_product)

        rows = week_stock(self.company, self.week, include_inactive=True)

        self.assertEqual((self.row(rows)['average'], self.row(rows)['to_ship']), (1.0, 3))
        self.assertNotIn(self.beta_product.pk, {row['product'].pk for row in rows})
