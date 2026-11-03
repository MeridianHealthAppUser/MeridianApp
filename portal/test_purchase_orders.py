"""Supplier purchases, partial receipts and history keep tenant and stock boundaries intact."""
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from django.db.models import Sum
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from care.models import (AuditEvent, Invoice, MedicationProduct, PatientSubscription, Payment, PharmacyOrder,
                         PracticeSettings, PurchaseOrder, PurchaseOrderItem, Shipment, StockMovement, SubscriptionCycle,
                         Supplier, SupplierProduct)
from care.purchasing import create_purchase_order, receive_purchase_order
from practices.models import CompanyMembership
from .test_operations import OperationsPortalFixture
from .test_to_order import PurchasingMarkup
from .workflow_context import make_workflow_context


class PurchaseOrderPortalTests(OperationsPortalFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.supplier = Supplier.objects.create(company=cls.company, name='Alpha Supplier', email='orders@alpha.test')
        cls.beta_supplier = Supplier.objects.create(company=cls.beta, name='Private Beta Supplier')
        for product in (cls.product, cls.open_product):
            SupplierProduct.objects.create(company=cls.company, supplier=cls.supplier, product=product,
                                           supplier_code=f'ALPHA-{product.pk}')
        SupplierProduct.objects.create(company=cls.beta, supplier=cls.beta_supplier, product=cls.beta_product,
                                       supplier_code='BETA-1')

    def listing(self, view='on-order', **params):
        return self.page('purchasing', view=view, **params)

    def order_ids(self, response):
        return [order.pk for order in response.context['orders']]

    def create_data(self, *, items=None, eta=None, token=None, client=None):
        items = items if items is not None else [(self.product, 5), (self.open_product, 3)]
        if token is None:
            token = self.page('purchasing', client=client).context['purchase_create_context']
        data = {'workflow_context': token, 'supplier': str(self.supplier.pk), 'eta': eta.isoformat() if eta else '', 'view': 'to-order',
                'items-TOTAL_FORMS': str(len(items)), 'items-INITIAL_FORMS': '0'}
        for index, (product, quantity) in enumerate(items):
            data.update({f'items-{index}-product': str(product.pk), f'items-{index}-quantity': str(quantity)})
        return data

    def create(self, data):
        return self.client.post(reverse('portal:purchase-order-create'), data)

    def order(self, *, items=None, eta=None, company=None, actor=None):
        company = company or self.company
        actor = actor or (self.admin if company.pk == self.company.pk else self.beta_admin)
        items = items if items is not None else [(self.product, 5)]
        return create_purchase_order(company=company, actor=actor, eta=eta, submission_key=uuid4(),
                                     supplier=self.supplier if company.pk == self.company.pk else self.beta_supplier,
                                     items=[{'product': product, 'quantity': quantity} for product, quantity in items])

    def complete(self, order):
        order.refresh_from_db()
        receive_purchase_order(company=order.company, actor=self.admin if order.company_id == self.company.pk else self.beta_admin,
            order=order, items=[{'item': item, 'quantity': item.quantity - item.quantity_received,
                                 'batch': f'History {item.pk}', 'expires_on': date(2030, 1, 1)} for item in order.items.all()],
            submission_key=uuid4(), expected_updated=order.updated_at.isoformat())
        order.refresh_from_db()
        return order

    def receipt_data(self, order, *, quantities=None, token=None, client=None):
        items = list(order.items.order_by('pk'))
        if token is None:
            token = self.page('purchasing', client=client, view='on-order', receive=order.pk).context['purchase_receive_context']
        data = {'workflow_context': token, 'view': 'on-order',
                'receipt-TOTAL_FORMS': str(len(items)), 'receipt-INITIAL_FORMS': str(len(items))}
        for index, item in enumerate(items):
            quantity = item.quantity - item.quantity_received if quantities is None else quantities.get(item.pk, 0)
            data.update({f'receipt-{index}-item': str(item.pk), f'receipt-{index}-quantity': str(quantity),
                         f'receipt-{index}-batch': f'BATCH-{item.pk}' if quantity else '',
                         f'receipt-{index}-expires_on': '2030-01-01' if quantity else ''})
        return data

    def receive(self, order, data):
        return self.client.post(reverse('portal:purchase-order-receive', args=[order.pk]), data)

    def balance(self, product=None):
        return StockMovement.objects.filter(product=product or self.product).aggregate(total=Sum('quantity'))['total'] or 0

    def recommendation(self):
        return next(row for row in self.page('purchasing').context['rows'] if row['product'].pk == self.product.pk)

    def workbook(self, table='on-order', **params):
        response = self.client.get(reverse('portal:ops-table-export', kwargs={'table': table}), params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        content = b''.join(response.streaming_content)
        response.close()
        workbook = load_workbook(BytesIO(content))
        self.addCleanup(workbook.close)
        return response, workbook

    def records(self, workbook):
        rows = list(workbook.active.iter_rows(values_only=True))
        return [dict(zip(rows[0], row)) for row in rows[1:]]

    def test_create_multiple_products_and_optional_eta_is_signed_idempotent_and_does_not_receive_stock(self):
        eta = self.today + timedelta(days=7)
        data = self.create_data(eta=eta)
        models = (StockMovement, Shipment, SubscriptionCycle, Payment, Invoice, PharmacyOrder, PatientSubscription)
        before = {model: list(model.objects.order_by('pk').values()) for model in models}
        first, repeated = self.create(data), self.create(data)
        self.assertEqual((first.status_code, repeated.status_code), (302, 302))
        order = PurchaseOrder.objects.get(company=self.company)
        self.assertEqual(first['Location'], repeated['Location'])
        self.assertEqual(urlsplit(first['Location']).path, reverse('portal:purchasing'))
        self.assertEqual(parse_qs(urlsplit(first['Location']).query), {'view': ['on-order'], 'order': [str(order.pk)]})
        self.assertEqual((order.created_by, order.eta, order.status), (self.admin, eta, PurchaseOrder.Status.OPEN))
        self.assertEqual(dict(order.items.values_list('product_id', 'quantity')), {self.product.pk: 5, self.open_product.pk: 3})
        self.assertEqual(AuditEvent.objects.filter(action='purchase_order.created').count(), 1)
        self.assertEqual({model: list(model.objects.order_by('pk').values()) for model in models}, before)
        expanded = self.listing(order=order.pk)
        row = expanded.context['orders'][0]
        self.assertTrue(row.expanded)
        self.assertEqual((row.line_count, row.ordered_units, row.received_units, row.remaining_units, row.progress), (2, 8, 0, 8, 0))
        self.assertContains(expanded, order.reference)
        self.assertContains(expanded, self.product.name)
        self.assertContains(expanded, self.open_product.name)

    def test_creation_without_eta_and_super_admin_access(self):
        self.login(self.super_admin)
        response = self.create(self.create_data(items=[(self.product, 1)]))
        self.assertEqual(response.status_code, 302)
        order = PurchaseOrder.objects.get()
        self.assertEqual(order.created_by, self.super_admin)
        self.assertIsNone(order.eta)

    def test_creation_requires_a_scoped_supplier_that_supplies_every_selected_product(self):
        limited = Supplier.objects.create(company=self.company, name='One product supplier')
        SupplierProduct.objects.create(company=self.company, supplier=limited, product=self.product, supplier_code='LIMITED-1')
        data = self.create_data(items=[(self.product, 2), (self.open_product, 3)])
        for supplier in ('', str(self.beta_supplier.pk), str(limited.pk)):
            with self.subTest(supplier=supplier):
                response = self.create({**data, 'supplier': supplier})
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.context['purchase_create_open'])
                self.assertEqual(response.context['purchase_create_form']['supplier'].value(), supplier)
                self.assertFalse(PurchaseOrder.objects.exists())
        # Removing a link after the review opened must also be caught by the server.
        SupplierProduct.objects.filter(supplier=self.supplier, product=self.open_product).delete()
        self.assertEqual(self.create(data).status_code, 400)
        self.assertFalse(PurchaseOrder.objects.exists())
        self.assertEqual(StockMovement.objects.count(), 2)

    def test_supplier_options_and_product_map_are_scoped_and_available_to_practice_admins(self):
        page = self.page('purchasing')
        choices = page.context['purchase_create_form'].fields['supplier'].queryset
        self.assertEqual(list(choices.values_list('pk', flat=True)), [self.supplier.pk])
        self.assertEqual(page.context['purchase_supplier_products'],
                         {str(self.supplier.pk): [self.product.pk, self.open_product.pk]})
        response = self.create(self.create_data(items=[(self.product, 2)]))
        self.assertEqual(response.status_code, 302)
        order = PurchaseOrder.objects.get()
        self.assertEqual((order.supplier_id, order.supplier_name, order.supplier_email),
                         (self.supplier.pk, self.supplier.name, self.supplier.email))
        self.assertEqual(order.items.get().supplier_code, f'ALPHA-{self.product.pk}')

    def test_invalid_creation_keeps_rows_eta_context_and_popup_open_without_partial_orders(self):
        data = self.create_data(eta=self.today + timedelta(days=3))
        data['q'] = 'Explicit'
        data['items-1-quantity'] = '0'
        response = self.create(data)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context['purchase_create_open'])
        formset = response.context['purchase_formset']
        self.assertTrue(formset.forms[1].errors)
        self.assertEqual(formset.forms[0]['product'].value(), str(self.product.pk))
        self.assertEqual(formset.forms[0]['quantity'].value(), '5')
        self.assertEqual(formset.forms[1]['quantity'].value(), '0')
        self.assertEqual(response.context['purchase_create_form']['eta'].value(), data['eta'])
        self.assertEqual(response.context['purchase_create_context'], data['workflow_context'])
        self.assertEqual(response.context['search'], 'Explicit')
        self.assertContains(response, 'id="purchasing-order-preview"', status_code=400)
        self.assertFalse(PurchaseOrder.objects.exists())
        self.assertFalse(PurchaseOrderItem.objects.exists())
        self.assertEqual(StockMovement.objects.count(), 2)

    def test_creation_rejects_foreign_hidden_duplicate_empty_and_invalid_eta_lines_atomically(self):
        self.open_product.is_active = False
        self.open_product.save(update_fields=('is_active', 'updated_at'))
        cases = [self.create_data(items=[(self.product, 1), (self.beta_product, 1)]),
                 self.create_data(items=[(self.open_product, 1)]),
                 self.create_data(items=[(self.product, 1), (self.product, 2)]), self.create_data(items=[])]
        invalid_eta = self.create_data(items=[(self.product, 1)])
        invalid_eta['eta'] = 'not-a-date'
        cases.append(invalid_eta)
        for data in cases:
            with self.subTest(data=data):
                self.assertEqual(self.create(data).status_code, 400)
        self.assertFalse(PurchaseOrder.objects.exists())
        self.assertFalse(PurchaseOrderItem.objects.exists())

    def test_creation_requires_correct_unexpired_actor_company_kind_and_no_record_context(self):
        page = self.page('purchasing')
        wrong_kind = self.page('inventory').context['workflow_context']
        bound_record = make_workflow_context(page.wsgi_request, self.company, 'purchase-order-create', self.product)
        other_actor = RequestFactory().get('/purchasing/')
        other_actor.user = self.super_admin
        actor_token = make_workflow_context(other_actor, self.company, 'purchase-order-create')
        with patch('django.core.signing.time.time', return_value=1):
            expired = make_workflow_context(page.wsgi_request, self.company, 'purchase-order-create')
        for token in ('', 'tampered', wrong_kind, bound_record, actor_token, expired):
            with self.subTest(token=token):
                self.assertEqual(self.create(self.create_data(token=token)).status_code, 400)
        token = page.context['purchase_create_context']
        CompanyMembership.objects.create(company=self.beta, user=self.admin, role='practice_admin')
        self.select(self.beta)
        self.assertEqual(self.create(self.create_data(items=[(self.beta_product, 1)], token=token)).status_code, 400)
        self.assertFalse(PurchaseOrder.objects.exists())

    def test_open_order_partial_and_full_receipts_update_physical_stock_and_history_once(self):
        StockMovement.objects.filter(product=self.product).delete()
        MedicationProduct.objects.filter(pk=self.product.pk).update(lead_time_weeks=0, safety_stock=10, starting_awc=0)
        self.assertEqual(self.recommendation()['order_quantity'], 10)
        response = self.create(self.create_data(items=[(self.product, 5)]))
        self.assertEqual(response.status_code, 302)
        order = PurchaseOrder.objects.get()
        item = order.items.get()
        self.assertEqual((self.balance(), self.recommendation()['on_order'], self.recommendation()['order_quantity']), (0, 5, 5))
        partial = self.receipt_data(order, quantities={item.pk: 2})
        self.assertEqual(self.receive(order, partial).status_code, 302)
        self.assertEqual(self.receive(order, partial).status_code, 302)
        order.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual((order.status, item.quantity_received, self.balance()), (PurchaseOrder.Status.OPEN, 2, 2))
        self.assertEqual((self.recommendation()['on_order'], self.recommendation()['order_quantity']), (3, 5))
        received = StockMovement.objects.filter(purchase_order_item=item)
        self.assertEqual(list(received.values_list('quantity', 'batch', 'expires_on')), [(2, f'BATCH-{item.pk}', date(2030, 1, 1))])
        modal = self.listing(receive=order.pk)
        self.assertEqual(modal.context['receive_formset'].forms[0]['quantity'].value(), 3)
        remaining = self.receipt_data(order)
        self.assertEqual(self.receive(order, remaining).status_code, 302)
        # A completed order must still accept a byte-for-byte replay of its already saved receipt.
        self.assertEqual(self.receive(order, remaining).status_code, 302)
        order.refresh_from_db()
        self.assertEqual((order.status, self.balance()), (PurchaseOrder.Status.RECEIVED, 5))
        self.assertIsNotNone(order.received_at)
        self.assertEqual((self.recommendation()['on_order'], self.recommendation()['order_quantity']), (0, 5))
        self.assertNotIn(order.pk, self.order_ids(self.listing()))
        self.assertEqual(self.order_ids(self.listing('history')), [order.pk])
        self.assertEqual(StockMovement.objects.filter(purchase_order_item=item).count(), 2)
        self.assertEqual(AuditEvent.objects.filter(action='purchase_order.received').count(), 2)

    def test_receipt_positive_lines_require_batch_and_expiry_but_zero_and_blank_lines_are_skipped(self):
        order = self.order(items=[(self.product, 5), (self.open_product, 3)])
        first, second = list(order.items.order_by('pk'))
        data = self.receipt_data(order, quantities={first.pk: 1})
        data['receipt-0-batch'], data['receipt-0-expires_on'] = '', ''
        invalid = self.receive(order, data)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.context['receive_order'].pk, order.pk)
        self.assertTrue(invalid.context['receive_formset'].forms[0].errors)
        self.assertFalse(invalid.context['receive_formset'].forms[1].errors)
        self.assertEqual(invalid.context['purchase_receive_context'], data['workflow_context'])
        self.assertEqual(StockMovement.objects.count(), 2)
        valid = self.receipt_data(order, quantities={first.pk: 1})
        valid['receipt-1-quantity'] = ''
        self.assertEqual(self.receive(order, valid).status_code, 302)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.quantity_received, second.quantity_received), (1, 0))
        self.assertEqual(StockMovement.objects.filter(purchase_order_item__purchase_order=order).count(), 1)

    def test_receipt_rejects_over_receipt_other_order_lines_empty_receipts_and_stale_context_without_stock_writes(self):
        order, other = self.order(), self.order()
        item, foreign_item = order.items.get(), other.items.get()
        oversized = self.receipt_data(order, quantities={item.pk: 6})
        wrong_line = self.receipt_data(order, quantities={item.pk: 1})
        wrong_line['receipt-0-item'] = str(foreign_item.pk)
        empty = self.receipt_data(order, quantities={})
        stale = self.receipt_data(order, quantities={item.pk: 1})
        for data in (oversized, wrong_line, empty):
            with self.subTest(data=data):
                self.assertEqual(self.receive(order, data).status_code, 400)
        PurchaseOrder.objects.filter(pk=order.pk).update(updated_at=timezone.now() + timedelta(seconds=1))
        stale_response = self.receive(order, stale)
        self.assertEqual(stale_response.status_code, 400)
        self.assertContains(stale_response, 'has changed', status_code=400)
        item.refresh_from_db()
        self.assertEqual(item.quantity_received, 0)
        self.assertEqual(StockMovement.objects.count(), 2)

    def test_receipt_context_is_bound_to_the_order_and_kind_and_rejects_foreign_urls(self):
        order, other = self.order(), self.order()
        data = self.receipt_data(order)
        wrong_order = self.receipt_data(other)['workflow_context']
        create_token = self.page('purchasing').context['purchase_create_context']
        for token in ('', 'tampered', wrong_order, create_token):
            with self.subTest(token=token):
                self.assertEqual(self.receive(order, {**data, 'workflow_context': token}).status_code, 400)
        foreign = self.order(company=self.beta, items=[(self.beta_product, 3)])
        self.assertEqual(self.receive(foreign, data).status_code, 404)
        self.assertEqual(StockMovement.objects.count(), 2)

    def test_create_and_receive_require_admin_login_and_csrf_even_when_doctors_ship(self):
        order = self.order()
        create_data, receipt_data = self.create_data(), self.receipt_data(order)
        PracticeSettings.objects.filter(company=self.company).update(doctors_ship=True)
        for actor in (self.doctor, self.patient_user):
            self.login(actor)
            self.assertEqual(self.create(create_data).status_code, 403)
            self.assertEqual(self.receive(order, receipt_data).status_code, 403)
        self.client.logout()
        self.assertEqual(self.create(create_data).status_code, 302)
        self.assertEqual(self.receive(order, receipt_data).status_code, 302)
        csrf_client = Client(enforce_csrf_checks=True)
        self.login(client=csrf_client)
        self.assertEqual(csrf_client.post(reverse('portal:purchase-order-create'), self.create_data(client=csrf_client)).status_code, 403)
        self.assertEqual(csrf_client.post(reverse('portal:purchase-order-receive', args=[order.pk]),
                                         self.receipt_data(order, client=csrf_client)).status_code, 403)

    def test_reading_orders_history_and_receipt_popup_cannot_receive_stock(self):
        order = self.order()
        completed = self.complete(self.order(items=[(self.open_product, 2)]))
        models = (PurchaseOrder, PurchaseOrderItem, MedicationProduct, StockMovement, Shipment,
                  PharmacyOrder, PatientSubscription, SubscriptionCycle, Payment, Invoice)
        before = {model: list(model.objects.order_by('pk').values()) for model in models}
        self.listing(order=order.pk)
        self.listing(receive=order.pk)
        self.listing('history', order=completed.pk)
        self.listing('history', group='products')
        self.workbook()
        self.workbook('purchase-history')
        self.workbook('purchase-history-products')
        self.assertEqual(self.client.get(reverse('portal:purchase-order-create')).status_code, 405)
        self.assertEqual(self.client.get(reverse('portal:purchase-order-receive', args=[order.pk])).status_code, 405)
        self.assertEqual({model: list(model.objects.order_by('pk').values()) for model in models}, before)

    def test_on_order_is_open_only_with_safe_snapshot_lines_and_partial_progress(self):
        order = self.order(items=[(self.product, 5), (self.open_product, 3)])
        item = order.items.get(product=self.product)
        receive_purchase_order(company=self.company, actor=self.admin, order=order,
            items=[{'item': item, 'quantity': 2, 'batch': 'PARTIAL', 'expires_on': date(2030, 1, 1)}], submission_key=uuid4())
        self.complete(self.order())
        cancelled = self.order()
        PurchaseOrder.objects.filter(pk=cancelled.pk).update(status=PurchaseOrder.Status.CANCELLED)
        self.order(company=self.beta, items=[(self.beta_product, 99)])
        PurchaseOrderItem.objects.create(company=self.beta, purchase_order=order, product=self.beta_product,
            quantity=99, product_name='PRIVATE FOREIGN LINE', strength='PRIVATE', category='other')
        MedicationProduct.objects.filter(pk=self.product.pk).update(name='Later renamed catalogue product')
        response = self.listing(order=order.pk, company=self.beta.pk)
        self.assertEqual(self.order_ids(response), [order.pk])
        displayed = response.context['orders'][0]
        self.assertEqual((displayed.line_count, displayed.ordered_units, displayed.received_units,
                          displayed.remaining_units, displayed.progress), (2, 8, 2, 6, 25))
        self.assertEqual({item.product_id for item in displayed.display_items}, {self.product.pk, self.open_product.pk})
        self.assertContains(response, self.product.name)
        self.assertNotContains(response, 'PRIVATE FOREIGN LINE')
        self.assertNotContains(response, 'Later renamed catalogue product')

    def test_on_order_eta_category_and_all_word_search_share_scoped_or_filters(self):
        self.open_product.category = MedicationProduct.Category.HORMONE
        self.open_product.save(update_fields=('category', 'updated_at'))
        overdue = self.order(eta=self.today - timedelta(days=1))
        upcoming = self.order(items=[(self.open_product, 1)], eta=self.today)
        unset = self.order(items=[(self.product, 1), (self.open_product, 1)])
        self.assertEqual(self.order_ids(self.listing(eta='overdue')), [overdue.pk])
        self.assertEqual(self.order_ids(self.listing(eta='upcoming')), [upcoming.pk])
        self.assertEqual(self.order_ids(self.listing(eta='unset')), [unset.pk])
        params = dict(category=['weight_management', 'hormone'], eta=['upcoming', 'unset'])
        self.assertEqual(self.order_ids(self.listing(**params)), [unset.pk, upcoming.pk])
        self.assertEqual(self.order_ids(self.listing(category='weight_management,hormone', eta='upcoming,unset')),
                         [unset.pk, upcoming.pk])
        self.assertEqual(self.order_ids(self.listing(q='LOCAL therapy')), [unset.pk, upcoming.pk])
        self.assertEqual(self.order_ids(self.listing(q=overdue.reference)), [overdue.pk])
        self.assertEqual(self.order_ids(self.listing(q='Explicit therapy')), [unset.pk])

    def test_on_order_25_row_pagination_and_export_link_preserve_active_filters(self):
        orders = [self.order(items=[(self.product, 1)]) for _ in range(31)]
        self.order(company=self.beta, items=[(self.beta_product, 1)])
        params = dict(q=self.product.name, category='weight_management', eta='unset')
        first, second = self.listing(**params), self.listing(**params, page=2)
        expected = [order.pk for order in reversed(orders)]
        self.assertEqual(self.order_ids(first), expected[:25])
        self.assertEqual(self.order_ids(second), expected[25:])
        self.assertEqual(second.context['page_obj'].paginator.count, 31)
        self.assertEqual(second.context['count_label'], '31 purchase orders')
        numbered = [link for link in PurchasingMarkup(first).links if link.get('aria-label') == 'Page 2']
        self.assertEqual(len(numbered), 1)
        self.assertEqual(parse_qs(urlsplit(numbered[0]['href']).query),
                         {**{key: [value] for key, value in params.items()}, 'view': ['on-order'], 'page': ['2']})
        export_url = second.context['table_export_url']
        self.assertEqual(urlsplit(export_url).path, reverse('portal:ops-table-export', kwargs={'table': 'on-order'}))
        self.assertEqual(parse_qs(urlsplit(export_url).query), {key: [value] for key, value in params.items()})
        _, workbook = self.workbook(**params, page=2)
        rows = self.records(workbook)
        self.assertEqual(len(rows), 31)
        self.assertEqual([row['Purchase order'] for row in rows], [order.reference for order in reversed(orders)])
        self.assertTrue(all((row['Ordered'], row['Received'], row['Outstanding']) == (1, 0, 1) for row in rows))

    def test_invalid_order_and_history_filters_do_not_silently_export_all_records(self):
        self.order()
        self.complete(self.order())
        for view, group, table, params in (
                ('on-order', 'orders', 'on-order', {'category': 'invalid'}),
                ('on-order', 'orders', 'on-order', {'eta': 'invalid'}),
                ('on-order', 'orders', 'on-order', {'q': 'x' * 101}),
                ('history', 'orders', 'purchase-history', {'category': 'invalid'}),
                ('history', 'products', 'purchase-history-products', {'q': 'x' * 101})):
            with self.subTest(view=view, group=group, params=params):
                response = self.listing(view, group=group, **params)
                self.assertTrue(response.context['filter_form'].errors)
                self.assertEqual(response.context['page_obj'].paginator.count, 0)
                exported = self.client.get(reverse('portal:ops-table-export', kwargs={'table': table}), params)
                self.assertEqual(exported.status_code, 400)
                self.assertNotIn('Content-Disposition', exported)

    def test_completed_history_order_and_product_views_agree_and_expansions_keep_the_same_search(self):
        first = self.complete(self.order(items=[(self.product, 5)]))
        second = self.complete(self.order(items=[(self.product, 3), (self.open_product, 2)]))
        opened = self.order(items=[(self.product, 7)])
        cancelled = self.order(items=[(self.product, 8)])
        PurchaseOrder.objects.filter(pk=cancelled.pk).update(status=PurchaseOrder.Status.CANCELLED)
        self.complete(self.order(company=self.beta, items=[(self.beta_product, 99)]))
        by_order = self.listing('history')
        self.assertEqual(self.order_ids(by_order), [second.pk, first.pk])
        self.assertNotIn(opened.pk, self.order_ids(by_order))
        by_product = self.listing('history', group='products')
        self.assertEqual(by_product.context['purchasing_group'], 'products')
        products = {row['product_id']: row for row in by_product.context['product_rows']}
        self.assertEqual(set(products), {self.product.pk, self.open_product.pk})
        self.assertEqual((products[self.product.pk]['ordered_units'], products[self.product.pk]['received_units'],
                          products[self.product.pk]['order_count']), (8, 8, 2))
        selected = self.listing('history', group='products', q=first.reference)
        row = selected.context['product_rows'][0]
        self.assertEqual((row['ordered_units'], row['received_units'], row['order_count']), (5, 5, 1))
        self.assertEqual({item.purchase_order_id for item in row['order_items']}, {first.pk})
        self.assertNotContains(selected, second.reference)
        for table in ('purchase-history', 'purchase-history-products'):
            _, workbook = self.workbook(table)
            rows = self.records(workbook)
            self.assertEqual({row['Purchase order'] for row in rows}, {first.reference, second.reference})
            self.assertEqual((sum(row['Ordered'] for row in rows), sum(row['Received'] for row in rows)), (10, 10))
            self.assertTrue(all(row['Outstanding'] == 0 for row in rows))
            _, filtered = self.workbook(table, q=first.reference)
            self.assertEqual([row['Purchase order'] for row in self.records(filtered)], [first.reference])

    def test_order_and_history_excel_use_literal_snapshots_and_typed_dates_quantities(self):
        self.product.name, self.product.strength, self.product.category = '=1+1', '#N/A', 'hormone'
        self.product.save(update_fields=('name', 'strength', 'category', 'updated_at'))
        eta = self.today + timedelta(days=7)
        order = self.order(items=[(self.product, 4)], eta=eta)
        item = order.items.get()
        receive_purchase_order(company=self.company, actor=self.admin, order=order,
            items=[{'item': item, 'quantity': 1, 'batch': 'PARTIAL', 'expires_on': date(2030, 1, 1)}], submission_key=uuid4())
        MedicationProduct.objects.filter(pk=self.product.pk).update(name='Changed later', strength='Changed strength')
        params = dict(q='=1+1', eta='upcoming', category='hormone')
        self.assertEqual(self.order_ids(self.listing(**params)), [order.pk])
        _, workbook = self.workbook(**params)
        row = self.records(workbook)[0]
        self.assertEqual((row['Purchase order'], row['Product'], row['Strength'], row['Category']),
                         (order.reference, '=1+1', '#N/A', 'Hormone therapy'))
        self.assertEqual((row['Ordered'], row['Received'], row['Outstanding'], row['ETA']), (4, 1, 3, eta))
        self.assertEqual(row['Supplier'], self.supplier.name)
        self.assertIsNone(row['Received on'])
        cells = dict(zip([cell.value for cell in workbook.active[1]], workbook.active[2]))
        for heading in ('Product', 'Strength', 'Category'):
            self.assertEqual(cells[heading].data_type, 's')
        for heading in ('Ordered', 'Received', 'Outstanding'):
            self.assertEqual(cells[heading].data_type, 'n')
        for heading in ('Created', 'ETA'):
            self.assertEqual(cells[heading].data_type, 'd')
        self.assertTrue(all(cell.data_type != 'f' for sheet_row in workbook.active for cell in sheet_row))
        self.complete(order)
        for table in ('purchase-history', 'purchase-history-products'):
            _, history = self.workbook(table, q='=1+1', category='hormone')
            row = self.records(history)[0]
            self.assertEqual((row['Product'], row['Strength'], row['Ordered'], row['Received'], row['Outstanding']),
                             ('=1+1', '#N/A', 4, 4, 0))
            self.assertEqual(row['Status'], 'Received')
            self.assertIsNotNone(row['Received on'])

    def test_order_and_export_reads_follow_the_active_practice_and_receive_links_are_scoped(self):
        alpha = self.order()
        beta = self.order(company=self.beta, items=[(self.beta_product, 9)])
        self.assertEqual(self.order_ids(self.listing(company=self.beta.pk)), [alpha.pk])
        self.assertEqual(self.client.get(reverse('portal:purchasing'), {'view': 'on-order', 'receive': beta.pk}).status_code, 404)
        self.login(self.beta_admin, self.beta)
        response = self.listing(company=self.company.pk)
        self.assertEqual(self.order_ids(response), [beta.pk])
        self.assertNotContains(response, self.product.name)
        _, workbook = self.workbook()
        rows = self.records(workbook)
        self.assertEqual([(row['Purchase order'], row['Product'], row['Ordered']) for row in rows],
                         [(beta.reference, self.beta_product.name, 9)])
        self.assertEqual(self.client.get(reverse('portal:purchasing'), {'view': 'on-order', 'receive': alpha.pk}).status_code, 404)
