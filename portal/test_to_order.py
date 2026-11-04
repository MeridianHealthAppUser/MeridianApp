"""To order shows real stock inputs and exports the same filtered purchasing quantities."""
from datetime import datetime, time, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import parse_qs, urlsplit

from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from care.models import (Invoice, MedicationProduct, PatientSubscription, Payment, PharmacyOrder, PracticeSettings,
                         Shipment, ShipmentItem, StockMovement, SubscriptionCycle)
from practices.models import CompanyMembership
from .test_operations import OperationsPortalFixture


class PurchasingMarkup(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.links, self.headers, self._heading = [], [], None
        self.cells, self._cell = [], None
        self.feed(response.content.decode())

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.append(dict(attrs))
        if tag == 'th':
            self._heading = []
        if tag == 'td':
            self._cell = {'label': dict(attrs).get('data-label'), 'parts': []}

    def handle_data(self, text):
        if self._heading is not None:
            self._heading.append(text)
        if self._cell is not None:
            self._cell['parts'].append(text)

    def handle_endtag(self, tag):
        if tag == 'th' and self._heading is not None:
            self.headers.append(' '.join(''.join(self._heading).split()))
            self._heading = None
        if tag == 'td' and self._cell is not None:
            self._cell['text'] = ' '.join(''.join(self._cell.pop('parts')).split())
            self.cells.append(self._cell)
            self._cell = None


class ToOrderPageTests(OperationsPortalFixture):
    def setUp(self):
        super().setUp()
        self.week = self.today - timedelta(days=self.today.weekday())

    def purchasing(self, **params):
        response = self.client.get(reverse('portal:purchasing'), params)
        self.assertEqual(response.status_code, 200)
        return response

    def rows(self, response):
        return list(response.context['page_obj'])

    def row(self, response, product=None):
        pk = (product or self.product).pk
        return next(row for row in self.rows(response) if row['product'].pk == pk)

    def configure(self, product=None, *, lead='2.50', safety=3, on_hand=1, **extra):
        product = product or self.product
        MedicationProduct.objects.filter(pk=product.pk).update(lead_time_weeks=lead, safety_stock=safety, **extra)
        StockMovement.objects.filter(product=product).delete()
        if on_hand:
            StockMovement.objects.create(company=product.company, product=product, actor=self.admin,
                                         quantity=on_hand, reason='Counted test balance')
        product.refresh_from_db()
        return product

    def new_product(self, name, *, category='weight_management', lead='0', safety=0, **extra):
        return MedicationProduct.objects.create(company=self.company, name=name, category=category,
            price='10.00', lead_time_weeks=lead, safety_stock=safety, **extra)

    def supplied(self, quantity, *, product=None, day=None):
        day = day or self.week - timedelta(days=1)
        timestamp = timezone.make_aware(datetime.combine(day, time(12)), timezone.get_default_timezone())
        return self.shipment(product=product or self.product, quantity=quantity, subscription=None,
                             status=Shipment.Status.DISPATCHED, scheduled_for=day, dispatched_at=timestamp)

    def export(self, **params):
        return self.client.get(reverse('portal:ops-table-export', kwargs={'table': 'to-order'}), params)

    def workbook(self, **params):
        response = self.export(**params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.assertIn('to-order.xlsx', response['Content-Disposition'])
        content = b''.join(response.streaming_content)
        workbook = load_workbook(BytesIO(content))
        self.addCleanup(workbook.close)
        return response, workbook

    def records(self, workbook):
        rows = list(workbook.active.iter_rows(values_only=True))
        return [dict(zip(rows[0], row)) for row in rows[1:]]

    def test_default_shows_all_active_own_products_including_zero_and_missing_setup(self):
        self.configure(lead='0', safety=0, on_hand=0)
        needed = self.new_product('Needed stock', safety=4)
        self.new_product('Inactive private product', is_active=False)
        response = self.purchasing(company=self.beta.pk)
        self.assertEqual(response.context['purchasing_view'], 'to-order')
        self.assertEqual({row['product'].pk for row in self.rows(response)},
                         {self.product.pk, self.open_product.pk, needed.pk})
        self.assertEqual(response.context['page_obj'].paginator.per_page, 25)
        headers = PurchasingMarkup(response).headers
        expected = ['Select', 'Product', 'Category', 'AWC', 'Lead time', 'Safety stock', 'On hand', 'On order', 'To order']
        headers = headers[:len(expected)]  # The read-only order preview also has its own table headings.
        self.assertEqual(len(headers), len(expected))
        for header, label in zip(headers, expected):
            self.assertTrue(header.startswith(label), (header, label))
        zero, setup = self.row(response), self.row(response, self.open_product)
        self.assertEqual((zero['order_quantity'], zero['status']), (0, 'covered'))
        self.assertIsNone(setup['order_quantity'])
        self.assertEqual(setup['status'], 'setup')
        self.assertEqual(set(setup['missing_fields']), {'Lead time', 'Safety stock'})
        markup = PurchasingMarkup(response)
        setup_links = [link for link in markup.links if 'purchasing-setup-link' in link.get('class', '').split()]
        self.assertEqual(len(setup_links), 1)
        self.assertIn('Lead time and Safety stock', setup_links[0]['aria-label'])
        self.assertEqual(urlsplit(setup_links[0]['href']).path, reverse('portal:inventory'))
        self.assertEqual(parse_qs(urlsplit(setup_links[0]['href']).query)['lead-time'], [str(self.open_product.pk)])
        lead_cells = [cell['text'] for cell in markup.cells if cell['label'] == 'Lead time (weeks)']
        self.assertEqual(lead_cells.count('0'), 2)
        self.assertEqual(lead_cells.count('—'), 1)
        self.assertNotContains(response, self.beta_product.name)
        self.assertNotContains(response, 'Inactive private product')
        self.assertNotContains(response, 'purchasing-placeholder')

    def test_real_consumption_stock_and_settings_match_formula_and_excel_inputs(self):
        self.configure()
        self.supplied(10)
        self.supplied(800, day=self.week)
        self.supplied(800, day=self.week - timedelta(weeks=8, days=1))
        self.shipment(quantity=8, status=Shipment.Status.READY, locked_at=timezone.now())
        PharmacyOrder.objects.create(company=self.company, patient=self.patient, status='submitted')
        response = self.purchasing(q=self.product.name)
        row = self.row(response)
        self.assertEqual(row['awc'], Decimal('1.25'))
        self.assertEqual(row['lead_time_weeks'], Decimal('2.50'))
        self.assertEqual((row['safety_stock'], row['on_hand'], row['on_order']), (3, 1, 0))
        # Physical stock is 1 despite eight reserved pens: ceil(1.25 * 2.5 + 3 - 1) is 6.
        self.assertEqual((row['order_quantity'], row['status']), (6, 'needed'))
        _, workbook = self.workbook(q=self.product.name)
        exported = self.records(workbook)[0]
        self.assertEqual((exported['AWC'], exported['Lead time (weeks)'], exported['Safety stock']), (1.25, 2.5, 3))
        self.assertEqual((exported['On hand'], exported['On order'], exported['To order']), (1, 0, 6))
        cells = dict(zip([cell.value for cell in workbook.active[1]], workbook.active[2]))
        for heading in ('AWC', 'Lead time (weeks)', 'Safety stock', 'On hand', 'On order', 'To order'):
            self.assertEqual(cells[heading].data_type, 'n')
        self.assertTrue(all(cell.data_type != 'f' for sheet_row in workbook.active for cell in sheet_row))

    def test_starting_awc_overrides_history_including_explicit_zero(self):
        self.configure(lead='1.25', safety=1, on_hand=0, starting_awc='2.50')
        self.supplied(10)
        starting = self.row(self.purchasing(q=self.product.name))
        self.assertEqual((starting['awc'], starting['awc_source'], starting['order_quantity']),
                         (Decimal('2.50'), 'starting', 5))
        MedicationProduct.objects.filter(pk=self.product.pk).update(starting_awc=0)
        zero = self.row(self.purchasing(q=self.product.name))
        self.assertEqual((zero['awc'], zero['awc_source'], zero['order_quantity']), (0, 'starting', 1))
        _, workbook = self.workbook(q=self.product.name)
        exported = self.records(workbook)[0]
        self.assertEqual((exported['AWC'], exported['AWC source'], exported['To order']), (0, 'Starting value', 1))
        MedicationProduct.objects.filter(pk=self.product.pk).update(starting_awc=None)
        history = self.row(self.purchasing(q=self.product.name))
        self.assertEqual((history['awc'], history['awc_source'], history['order_quantity']),
                         (Decimal('1.25'), 'history', 3))

    def test_category_status_and_all_word_search_filters_combine_with_multi_select_or(self):
        MedicationProduct.objects.filter(company=self.company).update(is_active=False)
        needed = self.new_product('Alpha medicine', category='weight_management', safety=3, strength='Dose A')
        covered = self.new_product('Beta treatment', category='hormone', strength='Dose B')
        setup = self.new_product('Setup device', category='other', lead=None, safety=None)
        params = dict(category=['weight_management', 'hormone'], status=['needed', 'covered'])
        combined = self.purchasing(**params)
        self.assertEqual({row['product'].pk for row in self.rows(combined)}, {needed.pk, covered.pk})
        comma = self.purchasing(category='weight_management,hormone', status='needed,covered')
        self.assertEqual([row['product'].pk for row in self.rows(comma)],
                         [row['product'].pk for row in self.rows(combined)])
        self.assertEqual([row['product'].pk for row in self.rows(self.purchasing(q='ALPHA weight DOSE'))], [needed.pk])
        self.assertEqual(self.rows(self.purchasing(q='Alpha hormone')), [])
        selected = self.purchasing(category='other', status='setup')
        self.assertEqual([row['product'].pk for row in self.rows(selected)], [setup.pk])
        self.assertEqual(self.rows(self.purchasing(category='other', status='needed')), [])

    def test_pagination_count_and_export_keep_filters_and_export_all_matching_rows(self):
        for index in range(31):
            self.new_product(f'Paging product {index:02d}')
        self.new_product('Paging hidden product', is_active=False)
        MedicationProduct.objects.create(company=self.beta, name='Paging product private', price=1,
                                          lead_time_weeks=0, safety_stock=0)
        params = dict(q='Paging product', category='weight_management', status='covered')
        first, second = self.purchasing(**params), self.purchasing(**params, page=2)
        self.assertEqual((len(self.rows(first)), len(self.rows(second))), (25, 6))
        self.assertEqual(second.context['page_obj'].paginator.count, 31)
        self.assertEqual(second.context['count_label'], '31 products')
        export_url = second.context['table_export_url']
        self.assertEqual(urlsplit(export_url).path, reverse('portal:ops-table-export', kwargs={'table': 'to-order'}))
        self.assertEqual(parse_qs(urlsplit(export_url).query), {key: [value] for key, value in params.items()})
        self.assertContains(second, 'data-search-region="table-export"')
        numbered = [link for link in PurchasingMarkup(first).links if link.get('aria-label') == 'Page 2']
        self.assertEqual(len(numbered), 1)
        self.assertEqual(parse_qs(urlsplit(numbered[0]['href']).query),
                         {**{key: [value] for key, value in params.items()}, 'page': ['2']})
        _, workbook = self.workbook(**params, page=2)
        rows = self.records(workbook)
        self.assertEqual(len(rows), 31)
        self.assertEqual([row['Product'] for row in rows], [f'Paging product {index:02d}' for index in range(31)])

    def test_invalid_filters_show_no_rows_disable_download_and_reject_export(self):
        for params in ({'category': 'not-a-category'}, {'status': 'not-a-status'}, {'q': 'x' * 101}):
            with self.subTest(params=params):
                response = self.purchasing(**params)
                self.assertTrue(response.context['filter_form'].errors)
                self.assertEqual(self.rows(response), [])
                self.assertFalse([link for link in PurchasingMarkup(response).links
                                  if urlsplit(link.get('href', '')).path ==
                                  reverse('portal:ops-table-export', kwargs={'table': 'to-order'})])
                export = self.export(**params)
                self.assertEqual(export.status_code, 400)
                self.assertNotIn('Content-Disposition', export)

    def test_setup_exports_preserve_missing_values_and_product_text_is_literal(self):
        MedicationProduct.objects.filter(pk=self.open_product.pk).update(name='=1+1', lead_time_weeks=None, safety_stock=None)
        _, workbook = self.workbook(q='=1+1')
        rows = self.records(workbook)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['Product'], '=1+1')
        self.assertEqual((rows[0]['Lead time (weeks)'], rows[0]['Safety stock'], rows[0]['To order']), (None, None, None))
        self.assertEqual(rows[0]['On order'], 0)
        cells = dict(zip([cell.value for cell in workbook.active[1]], workbook.active[2]))
        self.assertEqual(cells['Product'].data_type, 's')

    def test_permissions_and_active_company_scope_match_purchasing(self):
        for actor in (self.admin, self.super_admin):
            self.login(actor)
            self.assertNotIn(self.beta_product.pk, {row['product'].pk for row in self.rows(self.purchasing(company=self.beta.pk))})
        self.login(self.beta_admin, self.beta)
        self.assertEqual([row['product'].pk for row in self.rows(self.purchasing())], [self.beta_product.pk])
        _, workbook = self.workbook()
        self.assertTrue(self.records(workbook)[0]['Product'].startswith(self.beta_product.name))
        PracticeSettings.objects.filter(company=self.company).update(doctors_ship=True)
        for actor in (self.doctor, self.patient_user):
            self.login(actor)
            self.assertEqual(self.client.get(reverse('portal:purchasing')).status_code, 403)
            self.assertEqual(self.export().status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('portal:purchasing')).status_code, 302)
        self.assertEqual(self.export().status_code, 302)
        self.login()
        CompanyMembership.objects.filter(company=self.company, user=self.admin).update(is_active=False)
        self.assertEqual(self.client.get(reverse('portal:purchasing')).status_code, 403)
        self.assertEqual(self.export().status_code, 403)

    def test_get_head_and_download_do_not_create_orders_or_change_stock_billing_or_settings(self):
        self.configure()
        self.supplied(10)
        models = (MedicationProduct, Shipment, ShipmentItem, StockMovement, PharmacyOrder,
                  PatientSubscription, SubscriptionCycle, Payment, Invoice)
        before = {model: list(model.objects.order_by('pk').values()) for model in models}
        self.purchasing()
        self.workbook()
        for url in (reverse('portal:purchasing'), reverse('portal:ops-table-export', kwargs={'table': 'to-order'})):
            response = self.client.head(url)
            self.assertEqual(response.status_code, 200)
            content = b''.join(response.streaming_content) if response.streaming else response.content
            self.assertEqual(content, b'')
        self.assertEqual({model: list(model.objects.order_by('pk').values()) for model in models}, before)
