"""Stock history loads separately, stays scoped, and exports complete Excel data."""
from datetime import date, datetime, timezone as datetime_timezone
from io import BytesIO

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from care.models import StockMovement
from .test_operations import OperationsPortalFixture


class InventoryMovementTests(OperationsPortalFixture):
    def movements(self):
        return StockMovement.objects.filter(company=self.company).order_by('-created_at', '-pk')

    def add_movements(self, count=53):
        created = StockMovement.objects.bulk_create([
            StockMovement(company=self.company, product=self.product, actor=self.admin,
                          quantity=index + 1, reason=f'History receipt {index:03d}')
            for index in range(count)
        ])
        # Equal timestamps must still produce a stable page boundary.
        self.movements().update(created_at=timezone.now())
        return created

    def foreign_movement(self):
        return StockMovement.objects.create(company=self.beta, product=self.beta_product,
            actor=self.beta_admin, quantity=77, reason='PRIVATE BETA RECEIPT')

    def history(self, *, page=1, fragment=True):
        headers = {'X-Inventory-Movements': '1'} if fragment else {}
        return self.client.get(reverse('portal:inventory-movements'), {'page': page}, headers=headers)

    def workbook(self, **params):
        response = self.client.get(reverse('portal:inventory-movements-export'), params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'],
                         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.assertIn('attachment;', response['Content-Disposition'])
        self.assertIn('stock-movements.xlsx', response['Content-Disposition'])
        content = b''.join(response.streaming_content)
        return response, load_workbook(BytesIO(content))

    def assert_private(self, response):
        cache = response['Cache-Control']
        self.assertIn('private', cache)
        self.assertIn('no-store', cache)

    def workbook_cells(self, workbook):
        sheet = workbook['Stock movements']
        headings = [cell.value for cell in sheet[1]]
        return [dict(zip(headings, row)) for row in sheet.iter_rows(min_row=2)]

    def test_inventory_and_movement_tables_use_catalogue_product_and_category(self):
        for response in (self.page('inventory'), self.history()):
            with self.subTest(template=response.templates[0].name):
                self.assertContains(response, '<th scope="col">Product</th>', html=True)
                self.assertContains(response, '<th scope="col">Category</th>', html=True)
                self.assertNotContains(response, '<th scope="col">Medication</th>', html=True)
                self.assertContains(response, self.product.name)
                self.assertContains(response, self.product.strength)
                self.assertContains(response, self.product.get_category_display())

    def test_inventory_does_not_fetch_or_render_history_rows(self):
        self.add_movements()
        with CaptureQueriesContext(connection) as queries:
            response = self.page('inventory')
        self.assertNotIn('movements', response.context)
        self.assertNotContains(response, 'History receipt')
        self.assertContains(response, 'data-inventory-movements')
        self.assertContains(response, reverse('portal:inventory-movements'))
        movement_reads = [query['sql'] for query in queries if 'FROM "care_stockmovement"' in query['sql']]
        self.assertTrue(movement_reads, 'Stock balances must still include movement totals.')
        self.assertTrue(all('"care_stockmovement"."created_at"' not in query for query in movement_reads),
                        'The collapsed landing page must only aggregate stock, never fetch history records.')

    def test_history_has_stable_25_row_pages_without_other_practice_data(self):
        self.add_movements()
        foreign = self.foreign_movement()
        expected = list(self.movements().values_list('pk', flat=True))
        actual = []
        for number, length in ((1, 25), (2, 25), (3, 5)):
            with self.subTest(page=number):
                response = self.history(page=number)
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, 'portal/includes/inventory_movements.html')
                self.assertTemplateNotUsed(response, 'portal/operations_base.html')
                self.assertEqual(response.context['page_obj'].paginator.count, 55)
                self.assertEqual(len(response.context['movements']), length)
                actual.extend(movement.pk for movement in response.context['movements'])
                self.assertNotContains(response, foreign.reason)
                self.assert_private(response)
                self.assertIn('X-Inventory-Movements', response['Vary'])
        self.assertEqual(actual, expected)

    def test_pagination_links_and_full_page_fallback(self):
        self.add_movements()
        response = self.history(page=2, fragment=False)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'portal/inventory_movements.html')
        self.assertTemplateUsed(response, 'portal/operations_base.html')
        self.assertFalse(response.context['history_fragment'])
        base = reverse('portal:inventory-movements')
        self.assertEqual(response.context['previous_url'], f'{base}?page=1')
        self.assertEqual(response.context['next_url'], f'{base}?page=3')
        self.assertEqual(response.context['page_urls'][1],
                         {'number': 2, 'url': f'{base}?page=2', 'current': True, 'ellipsis': False})
        self.assertEqual(response.context['export_url'], reverse('portal:inventory-movements-export'))
        self.assert_private(response)

    def test_invalid_or_stale_pages_have_a_usable_fallback(self):
        self.add_movements()
        for supplied, expected in (('not-a-page', 1), ('9999', 3), ('-1', 3), ('0', 3)):
            with self.subTest(supplied=supplied):
                response = self.history(page=supplied)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['page_obj'].number, expected)

    def test_empty_history_has_one_empty_page_and_excel_headers(self):
        self.movements().delete()
        response = self.history()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No stock movements recorded yet.')
        self.assertEqual(response.context['page_obj'].paginator.count, 0)
        self.assertEqual(response.context['page_obj'].paginator.num_pages, 1)
        self.assertIsNone(response.context['next_url'])
        self.assertIsNone(response.context['previous_url'])
        _, workbook = self.workbook()
        self.assertEqual(workbook.active.max_row, 1)
        self.assertEqual([cell.value for cell in workbook.active[1]],
                         ['Date', 'Product', 'Category', 'Quantity', 'Batch', 'Expires on',
                          'Reference / reason', 'Shipment ID', 'Recorded by', 'Stock override'])
        workbook.close()

    def test_excel_includes_all_pages_in_order_and_only_current_practice(self):
        self.add_movements(count=137)
        self.foreign_movement()
        expected = list(self.movements().values_list('reason', flat=True))
        response, workbook = self.workbook(page=2, company=self.beta.pk)
        self.assert_private(response)
        sheet = workbook['Stock movements']
        self.assertEqual(sheet.max_row, 140)
        self.assertEqual([row['Reference / reason'].value for row in self.workbook_cells(workbook)], expected)
        self.assertNotIn('PRIVATE BETA RECEIPT', expected)
        self.assertEqual(sheet.freeze_panes, 'A2')
        self.assertEqual(sheet.auto_filter.ref, 'A1:J140')
        workbook.close()

    def test_excel_preserves_signed_quantities_dates_metadata_and_literal_text(self):
        parcel = self.shipment()
        movement = StockMovement.objects.create(company=self.company, product=self.product, actor=self.admin,
            shipment=parcel, quantity=-3, batch='=1+1', expires_on=date(2030, 2, 3),
            reason='=HYPERLINK("https://example.test", "test")', is_override=True)
        timestamp = datetime(2026, 11, 2, 22, 45, tzinfo=datetime_timezone.utc)
        StockMovement.objects.filter(pk=movement.pk).update(created_at=timestamp)
        # Export timestamps remain explicitly SAST even when a request's timezone is UTC.
        with timezone.override('UTC'):
            _, workbook = self.workbook()
        row = next(row for row in self.workbook_cells(workbook) if row['Reference / reason'].value == movement.reason)
        self.assertEqual(row['Date'].value, datetime(2026, 11, 3, 0, 45))
        self.assertEqual(row['Date'].data_type, 'd')
        self.assertEqual(row['Product'].value, str(self.product))
        self.assertEqual(row['Category'].value, self.product.get_category_display())
        self.assertEqual(row['Category'].data_type, 's')
        self.assertEqual(row['Quantity'].value, -3)
        self.assertEqual(row['Quantity'].data_type, 'n')
        self.assertEqual(row['Batch'].value, '=1+1')
        self.assertEqual(row['Batch'].data_type, 's')
        self.assertEqual(row['Expires on'].value, date(2030, 2, 3))
        self.assertEqual(row['Expires on'].data_type, 'd')
        self.assertEqual(row['Reference / reason'].data_type, 's')
        self.assertEqual(row['Shipment ID'].value, parcel.pk)
        self.assertEqual(row['Recorded by'].value, str(self.admin))
        self.assertIs(row['Stock override'].value, True)
        self.assertEqual(row['Stock override'].data_type, 'b')
        workbook.close()

    def test_all_excel_text_fields_remain_literal_including_formula_and_error_prefixes(self):
        self.product.name = '=1+1'
        self.product.save(update_fields=['name'])
        for reason in ('=1+1', '+1+1', '-1+1', '@SUM(A1)', '#N/A', '\t=1+1'):
            StockMovement.objects.create(company=self.company, product=self.product, actor=self.admin,
                                         quantity=1, reason=reason)
        _, workbook = self.workbook()
        rows = self.workbook_cells(workbook)
        for row in rows:
            for cell in (row['Product'], row['Category'], row['Batch'], row['Reference / reason'], row['Recorded by']):
                if cell.value is not None:
                    self.assertEqual(cell.data_type, 's')
        for reason in ('=1+1', '+1+1', '-1+1', '@SUM(A1)', '#N/A', '\t=1+1'):
            self.assertIn(reason, [row['Reference / reason'].value for row in rows])
        workbook.close()

    def test_history_and_export_require_login_and_practice_admin_access(self):
        for name in ('inventory-movements', 'inventory-movements-export'):
            url = reverse(f'portal:{name}')
            self.client.logout()
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn('login', response['Location'])
            for actor in (self.doctor, self.patient_user):
                with self.subTest(route=name, actor=actor):
                    self.login(actor)
                    self.assertEqual(self.client.get(url).status_code, 403)
        self.login(self.super_admin)
        self.assertEqual(self.history().status_code, 200)

    def test_switching_practice_scopes_both_history_and_export(self):
        foreign = self.foreign_movement()
        self.login(self.beta_admin, self.beta)
        response = self.history()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, foreign.reason)
        self.assertNotContains(response, self.product.name)
        self.assertEqual([movement.pk for movement in response.context['movements']], [foreign.pk])
        _, workbook = self.workbook()
        self.assertEqual(workbook.active.max_row, 2)
        self.assertEqual(self.workbook_cells(workbook)[0]['Reference / reason'].value, foreign.reason)
        workbook.close()
