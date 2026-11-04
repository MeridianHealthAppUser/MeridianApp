"""Live Inventory search and Excel share the same product/strength/category scope."""

from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import parse_qs, urlsplit

from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from care.models import AuditEvent, MedicationProduct, Shipment, StockMovement
from .test_operations import OperationsPortalFixture


class ListRegion(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.text = []
        self.modal_inside = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get('data-search-region') == 'list':
            self.depth = 1
        elif self.depth and tag not in ('input', 'br', 'hr', 'img', 'meta', 'link', 'path'):
            self.depth += 1
        if self.depth and tag == 'dialog':
            self.modal_inside = True

    def handle_endtag(self, tag):
        if self.depth and tag not in ('input', 'br', 'hr', 'img', 'meta', 'link', 'path'):
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.text.append(data)


class InventorySearchTests(OperationsPortalFixture):
    def setUp(self):
        super().setUp()
        self.blue = MedicationProduct.objects.create(company=self.company, name='Blue Pen', strength='1.0 mg',
                                                     category=MedicationProduct.Category.OTHER, price=15, is_active=False)
        self.green = MedicationProduct.objects.create(company=self.company, name='Green Cream', strength='2 mg',
                                                      category=MedicationProduct.Category.HORMONE, price=20)
        self.url = reverse('portal:ops-table-export', kwargs={'table': 'inventory'})

    def inventory(self, **params):
        return self.page('inventory', **params)

    def ids(self, response):
        return {row['product'].pk for row in response.context['rows']}

    def export_products(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        content = b''.join(response.streaming_content)
        workbook = load_workbook(BytesIO(content))
        self.addCleanup(workbook.close)
        return {row[0] for row in list(workbook.active.values)[1:]}

    def test_case_insensitive_all_word_search_covers_name_strength_and_displayed_category(self):
        for query, expected in (
            (' bLuE   1.0 ', {self.blue.pk}), ('other BLUE', {self.blue.pk}),
            ('hormone therapy', {self.green.pk}), ('mg other', {self.blue.pk}),
            ('blue 2', set()), ('not-present', set()),
            ('weight management', {self.product.pk, self.open_product.pk}),
        ):
            with self.subTest(query=query):
                response = self.inventory(q=query)
                self.assertEqual(self.ids(response), expected)
                self.assertEqual(response.context['count_label'], f'{len(expected)} product' + ('' if len(expected) == 1 else 's'))
        self.assertEqual(self.ids(self.inventory(q='  ')), {self.product.pk, self.open_product.pk, self.blue.pk, self.green.pk})

    def test_excel_matches_the_full_filtered_table_and_preserves_the_search_link(self):
        for index in range(24):
            MedicationProduct.objects.create(company=self.company, name=f'Extra Blue {index:02d}',
                strength='1.0 mg', category=MedicationProduct.Category.OTHER, price=1)
        response = self.inventory(q='BLUE other', page=2)

        self.assertEqual(len(response.context['rows']), 25)
        self.assertEqual(self.export_products(q='BLUE other', page=2),
                         {row['product'].name for row in response.context['rows']})
        link = urlsplit(response.context['table_export_url'])
        self.assertEqual(parse_qs(link.query), {'q': ['BLUE other']})
        self.assertIsNone(response.context['filter_menu'])
        self.assertContains(response, 'placeholder="Product, strength or category"')
        self.assertNotContains(response, 'data-filter-menu')

    def test_invalid_search_shows_no_rows_and_disables_excel_while_export_rejects_it(self):
        query = 'x' * 101
        response = self.inventory(q=query)

        self.assertEqual(response.context['rows'], [])
        self.assertTrue(response.context['filter_form'].errors)
        self.assertTrue(response.context['table_export_disabled'])
        self.assertContains(response, 'Correct the search to view products.')
        self.assertContains(response, 'class="table-excel-download" disabled')
        self.assertEqual(self.client.get(self.url, {'q': query}).status_code, 400)

    def test_search_never_reaches_another_practices_products_or_overrides_admin_access(self):
        MedicationProduct.objects.filter(pk=self.beta_product.pk).update(name='Blue foreign pen', category='other')
        response = self.inventory(q='blue other', company=self.beta.pk)
        self.assertEqual(self.ids(response), {self.blue.pk})
        self.assertEqual(self.export_products(q='blue other', company=self.beta.pk), {self.blue.name})
        self.login(self.doctor)
        self.assertEqual(self.client.get(reverse('portal:inventory'), {'q': 'blue'}).status_code, 403)
        self.assertEqual(self.client.get(self.url, {'q': 'blue'}).status_code, 403)

    def test_all_product_modals_remain_outside_the_refreshed_list_when_initial_search_hides_the_product(self):
        self.login(self.super_admin)
        self.shipment(product=self.open_product, quantity=1, status=Shipment.Status.READY, locked_at=timezone.now())
        response = self.inventory(q='Blue')
        html = response.content.decode()
        region = ListRegion()
        region.feed(html)

        self.assertEqual(self.ids(response), {self.blue.pk})
        self.assertEqual({row['product'].pk for row in response.context['modal_rows']},
                         {self.product.pk, self.open_product.pk, self.blue.pk, self.green.pk})
        self.assertFalse(region.modal_inside)
        self.assertIn(self.blue.name, ' '.join(region.text))
        self.assertNotIn(self.open_product.name, ' '.join(region.text))
        for product in (self.product, self.open_product, self.green):
            self.assertIn(f'id="inventory-lead-time-{product.pk}"', html)
            self.assertIn(f'id="inventory-safety-stock-{product.pk}"', html)
        self.assertIn(f'id="inventory-reservations-{self.open_product.pk}"', html)
        self.assertIn('data-search-region="count"', html)
        self.assertIn('data-search-region="table-export"', html)
        self.assertLess(html.index('data-search-region="count"'), html.index('data-search-region="table-export"'))
        self.assertLess(html.index('data-search-region="table-export"'), html.index('data-conversation-search'))
        self.assertIn('<details class="staff-page-card inventory-section" open', html)

    def test_search_is_preserved_when_saving_metrics_and_receiving_stock(self):
        self.login(self.super_admin)
        page = self.inventory(q='blue other')
        row = next(row for row in page.context['modal_rows'] if row['product'].pk == self.blue.pk)
        before = StockMovement.objects.count()
        for endpoint, value, token_name in (
            ('inventory-lead-time', {'weeks': '2.5'}, 'lead_time_context'),
            ('inventory-safety-stock', {'quantity': '3'}, 'safety_stock_context'),
        ):
            row = next(row for row in self.inventory(q='blue other').context['modal_rows'] if row['product'].pk == self.blue.pk)
            response = self.client.post(reverse(f'portal:{endpoint}', args=[self.blue.pk]),
                                        {'q': 'blue other', 'workflow_context': row[token_name], **value})
            self.assertEqual(parse_qs(urlsplit(response['Location']).query), {'q': ['blue other']})
        page = self.inventory(q='blue other')
        response = self.client.post(reverse('portal:inventory'), {
            'q': 'blue other', 'workflow_context': page.context['workflow_context'], 'reference': 'Search receipt',
            'items-TOTAL_FORMS': '1', 'items-INITIAL_FORMS': '0', 'items-0-product': self.product.pk,
            'items-0-quantity': 2, 'items-0-batch': 'SEARCH-BATCH', 'items-0-expires_on': '2030-01-01',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(parse_qs(urlsplit(response['Location']).query), {'q': ['blue other']})
        self.assertEqual(StockMovement.objects.count(), before + 1)

    def test_search_get_is_read_only_and_invalid_editor_keeps_the_search(self):
        self.login(self.super_admin)
        before = AuditEvent.objects.count(), StockMovement.objects.count()
        page = self.inventory(q='blue other')
        row = next(row for row in page.context['modal_rows'] if row['product'].pk == self.blue.pk)
        response = self.client.post(reverse('portal:inventory-lead-time', args=[self.blue.pk]),
            {'q': 'blue other', 'workflow_context': row['lead_time_context'], 'weeks': '-1'})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.ids(response), {self.blue.pk})
        self.assertEqual(response.context['search'], 'blue other')
        self.assertFalse(response.context['receipt_open'])
        self.assertEqual((AuditEvent.objects.count(), StockMovement.objects.count()), before)
