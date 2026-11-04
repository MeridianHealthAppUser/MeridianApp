"""The consumption screen, drill-through and XLSX report reconcile and stay scoped."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from care.models import MedicationProduct, Shipment, ShipmentItem, StockMovement
from .test_operations import OperationsPortalFixture


class ConsumptionPortalTests(OperationsPortalFixture):
    today = date(2026, 11, 3)
    current_week = date(2026, 11, 2)
    previous_week = date(2026, 10, 26)

    def setUp(self):
        super().setUp()
        self.clock = patch('django.utils.timezone.now', return_value=self.at(date(2026, 11, 3)))
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def at(self, day, clock=time(12)):
        return timezone.make_aware(datetime.combine(day, clock), timezone.get_current_timezone())

    def supplied(self, quantity=1, day=None, **extra):
        day = day or self.previous_week
        values = dict(subscription=None, scheduled_for=self.current_week + timedelta(days=2),
                      status=Shipment.Status.DISPATCHED, dispatched_at=self.at(day))
        values.update(extra)
        return self.shipment(quantity=quantity, **values)

    def url(self, name='inventory-consumption', product=None, week=None):
        kwargs = None
        if 'detail' in name:
            kwargs = {'pk': (product or self.product).pk,
                      'week': (week or self.previous_week).isoformat()}
        return reverse(f'portal:{name}', kwargs=kwargs)

    def report(self, **params):
        response = self.client.get(self.url(), params)
        self.assertEqual(response.status_code, 200)
        return response

    def row(self, response, product=None):
        pk = (product or self.product).pk
        return next(row for row in response.context['report']['rows'] if row['product'].pk == pk)

    def workbook(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'],
                         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.assertIn('.xlsx', response['Content-Disposition'])
        payload = b''.join(response.streaming_content)
        workbook = load_workbook(BytesIO(payload))
        self.addCleanup(workbook.close)
        return workbook

    def test_default_eight_completed_weeks_reconciles_to_drilldown_and_ignores_starting_awc(self):
        MedicationProduct.objects.filter(pk=self.product.pk).update(starting_awc=Decimal('999.00'))
        first = self.supplied(4, self.previous_week)
        second = self.supplied(6, self.previous_week + timedelta(days=6), status=Shipment.Status.DELIVERED)
        self.supplied(99, self.current_week)
        self.supplied(50, self.previous_week, status=Shipment.Status.READY)
        self.shipment(quantity=70, status=Shipment.Status.DISPATCHED, subscription=None)

        response = self.report()
        row = self.row(response)
        cell = next(cell for cell in row['cells'] if cell['start'] == self.previous_week)
        self.assertEqual((cell['quantity'], row['total'], row['avg_8']), (10, 10, Decimal('1.25')))
        self.assertEqual(len(response.context['report']['weeks']), 8)
        self.assertEqual(response.context['report']['end_week'], self.previous_week)
        self.assertIn('no-store', response['Cache-Control'])

        detail = self.client.get(self.url('inventory-consumption-detail'))
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.context['total'], 10)
        self.assertEqual(detail.context['page_obj'].paginator.count, 2)
        self.assertContains(detail, self.product.name)
        self.assertContains(detail, str(first.pk))
        self.assertContains(detail, str(second.pk))

    def test_search_category_and_inactive_products_match_the_export(self):
        blue = MedicationProduct.objects.create(company=self.company, name='Blue Cream', strength='2 mg',
            category=MedicationProduct.Category.OTHER, price=10, is_active=False)
        self.supplied(3, product=blue)
        response = self.report(q='blue OTHER', category='other')
        self.assertEqual([row['product'].pk for row in response.context['rows']], [blue.pk])
        export = self.workbook(self.client.get(self.url('inventory-consumption-export'),
                                              {'q': 'blue OTHER', 'category': 'other'}))
        cells = [str(cell.value) for sheet in export for row in sheet for cell in row]
        self.assertIn(blue.name, cells)
        self.assertNotIn(self.product.name, cells)
        self.assertNotIn(self.beta_product.name, cells)
        self.assertGreaterEqual(len(export.sheetnames), 3)

    def test_product_pagination_does_not_limit_excel_and_links_preserve_filters(self):
        for number in range(27):
            MedicationProduct.objects.create(company=self.company, name=f'Archive {number:02}',
                strength='2 mg', category=MedicationProduct.Category.OTHER, price=1)
        response = self.report(q='Archive', category='other', weeks='4', end_week='2026-10-27',
                               view='averages', page=2)
        self.assertEqual(response.context['page_obj'].paginator.count, 27)
        self.assertEqual(len(response.context['rows']), 2)
        self.assertEqual(response.context['report']['end_week'], self.previous_week)
        self.assertEqual(len(response.context['report']['weeks']), 4)
        export_query = parse_qs(urlsplit(response.context['table_export_url']).query)
        self.assertEqual(export_query['q'], ['Archive'])
        self.assertNotIn('page', export_query)
        workbook = self.workbook(self.client.get(response.context['table_export_url']))
        names = {cell.value for row in workbook.worksheets[0] for cell in row
                 if isinstance(cell.value, str) and cell.value.startswith('Archive ')}
        self.assertEqual(len(names), 27)

    def test_detail_paginates_and_full_export_includes_every_contributing_line(self):
        for number in range(28):
            self.supplied(number + 1, waybill=f'WB-{number:03}')
        response = self.client.get(self.url('inventory-consumption-detail'), {'page': 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].paginator.count, 28)
        self.assertEqual(len(response.context['page_obj'].object_list), 3)
        self.assertEqual(response.context['total'], sum(range(1, 29)))
        workbook = self.workbook(self.client.get(self.url('inventory-consumption-detail-export'), {'page': 2}))
        waybills = {cell.value for sheet in workbook for row in sheet for cell in row
                    if isinstance(cell.value, str) and cell.value.startswith('WB-')}
        self.assertEqual(waybills, {f'WB-{number:03}' for number in range(28)})
        values = list(workbook['Dispatch detail'].values)
        header_index = next(index for index, row in enumerate(values) if 'Quantity' in row and 'Parcel line' in row)
        quantity_column = values[header_index].index('Quantity')
        quantities = [row[quantity_column] for row in values[header_index + 1:]]
        self.assertTrue(all(isinstance(quantity, int) for quantity in quantities))
        self.assertEqual(sum(quantities), response.context['total'])

    def test_empty_week_is_valid_and_fragment_contains_only_detail_content(self):
        full = self.client.get(self.url('inventory-consumption-detail'))
        fragment = self.client.get(self.url('inventory-consumption-detail'), {'fragment': '1'})
        self.assertEqual((full.status_code, fragment.status_code), (200, 200))
        self.assertEqual(full.context['total'], 0)
        self.assertEqual(fragment.context['total'], 0)
        self.assertNotIn('<html', fragment.content.decode().lower())
        self.assertContains(fragment, self.product.name)

    def test_current_week_is_explicitly_partial_and_future_dispatches_are_excluded(self):
        self.supplied(3, self.current_week)
        self.supplied(60, self.current_week + timedelta(days=2))
        response = self.report(end_week=self.current_week.isoformat(), weeks='4')
        week = response.context['report']['weeks'][-1]
        self.assertTrue(week['is_partial'])
        self.assertEqual(self.row(response)['cells'][-1]['quantity'], 3)
        self.assertEqual(self.row(response)['avg_8'], 0)

    def test_invalid_filters_never_silently_export_all_data(self):
        for params in ({'q': 'x' * 101}, {'category': 'foreign-category'}, {'weeks': '52'},
                       {'end_week': 'not-a-date'}, {'end_week': '2026-11-16'}, {'view': 'unknown'}):
            with self.subTest(params=params):
                response = self.client.get(self.url(), params)
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.context['filter_form'].errors)
                self.assertEqual(response.context['rows'], [])
                self.assertEqual(self.client.get(self.url('inventory-consumption-export'), params).status_code, 400)

    def test_detail_invalid_week_is_rejected_without_leaking_data(self):
        for suffix in ('not-a-week', '2026-11-30'):
            kwargs = {'pk': self.product.pk, 'week': suffix}
            for name in ('inventory-consumption-detail', 'inventory-consumption-detail-export'):
                response = self.client.get(reverse(f'portal:{name}', kwargs=kwargs))
                self.assertIn(response.status_code, (400, 404))

    def test_company_parameter_cannot_change_scope_and_foreign_product_is_404(self):
        self.supplied(500, company=self.beta, patient=self.beta_patient, product=self.beta_product)
        self.supplied(10)
        response = self.report(company=self.beta.pk)
        self.assertEqual(self.row(response)['total'], 10)
        self.assertNotContains(response, self.beta_product.name)
        for name in ('inventory-consumption-detail', 'inventory-consumption-detail-export'):
            self.assertEqual(self.client.get(self.url(name, product=self.beta_product)).status_code, 404)
        workbook = self.workbook(self.client.get(self.url('inventory-consumption-export'), {'company': self.beta.pk}))
        cells = [str(cell.value) for sheet in workbook for row in sheet for cell in row]
        self.assertNotIn(self.beta_product.name, cells)
        self.assertNotIn(str(self.beta_patient), cells)

    def test_malformed_cross_company_lines_never_contribute(self):
        local = self.supplied(3)
        foreign = self.supplied(4, company=self.beta, patient=self.beta_patient, product=self.beta_product)
        ShipmentItem.objects.create(company=self.company, shipment=foreign, product=self.product, quantity=100, dose='bad')
        ShipmentItem.objects.create(company=self.company, shipment=local, product=self.beta_product, quantity=100, dose='bad')
        response = self.report()
        self.assertEqual(self.row(response)['total'], 3)
        detail = self.client.get(self.url('inventory-consumption-detail'))
        self.assertEqual(detail.context['total'], 3)

    def test_admin_and_super_admin_access_but_doctor_patient_and_anonymous_are_denied(self):
        routes = [self.url(), self.url('inventory-consumption-export'),
                  self.url('inventory-consumption-detail'), self.url('inventory-consumption-detail-export')]
        self.login(self.super_admin)
        for url in routes:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(b''.join(response.streaming_content) if response.streaming else response.content)
        for actor in (self.doctor, self.patient_user):
            self.login(actor)
            for url in routes:
                self.assertEqual(self.client.get(url).status_code, 403)
        self.client.logout()
        for url in routes:
            self.assertEqual(self.client.get(url).status_code, 302)

    def test_get_report_and_drilldown_do_not_change_stock_or_shipments(self):
        shipment = self.supplied(4)
        stock_before = list(StockMovement.objects.values())
        shipments_before = list(Shipment.objects.values())
        items_before = list(ShipmentItem.objects.values())
        self.report()
        self.client.get(self.url('inventory-consumption-detail'))
        self.assertEqual(list(StockMovement.objects.values()), stock_before)
        self.assertEqual(list(Shipment.objects.values()), shipments_before)
        self.assertEqual(list(ShipmentItem.objects.values()), items_before)
        self.assertEqual(self.client.post(self.url(), {'shipment': shipment.pk}).status_code, 405)
