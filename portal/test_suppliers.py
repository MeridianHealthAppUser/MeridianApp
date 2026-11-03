"""Supplier maintenance is signed, scoped to Super Admins and separate from stock."""
from datetime import timedelta
from io import BytesIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import Client
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from care.models import AuditEvent, MedicationProduct, PurchaseOrder, Shipment, StockMovement, Supplier, SupplierProduct
from practices.models import CompanyMembership
from .test_operations import OperationsPortalFixture


class SupplierPortalTests(OperationsPortalFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.supplier = Supplier.objects.create(company=cls.company, name='Alpha Medical', contact_name='Pat Buyer',
                                               email='orders@alpha.test', phone='021 555 0001')
        cls.link = SupplierProduct.objects.create(company=cls.company, supplier=cls.supplier,
            product=cls.product, supplier_code='ALPHA-01')
        cls.beta_supplier = Supplier.objects.create(company=cls.beta, name='PRIVATE FOREIGN SUPPLIER')
        SupplierProduct.objects.create(company=cls.beta, supplier=cls.beta_supplier,
            product=cls.beta_product, supplier_code='PRIVATE-CODE')

    def setUp(self):
        super().setUp()
        self.login(self.super_admin)

    def listing(self, **params):
        return self.page('supplier-list', **params)

    def supplier_data(self, *, supplier=None, products=None, token=None, name='New Supplier', client=None):
        products = products if products is not None else [(self.product, 'CODE-1'), (self.open_product, 'CODE-2')]
        if token is None:
            query = {'edit': supplier.pk} if supplier is not None else {'create': '1'}
            token = self.page('supplier-list', client=client, **query).context['supplier_context']
        data = {'workflow_context': token, 'name': name, 'contact_name': 'A Contact', 'email': 'supplier@example.test',
                'phone': '+27 21 555 0011', 'products-TOTAL_FORMS': str(len(products)),
                'products-INITIAL_FORMS': str(supplier.product_links.count()) if supplier else '0'}
        for index, (product, code) in enumerate(products):
            data[f'products-{index}-product'] = str(product.pk) if product else ''
            data[f'products-{index}-supplier_code'] = code
        return data

    def post_supplier(self, data, supplier=None, client=None):
        route = 'supplier-edit' if supplier else 'supplier-create'
        return (client or self.client).post(reverse(f'portal:{route}', args=[supplier.pk] if supplier else None), data)

    def delete_data(self, supplier=None):
        supplier = supplier or self.supplier
        return {'workflow_context': self.listing(delete=supplier.pk).context['supplier_delete_context']}

    def export(self, **params):
        response = self.client.get(reverse('portal:supplier-export'), params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        workbook = load_workbook(BytesIO(b''.join(response.streaming_content)))
        response.close()
        self.addCleanup(workbook.close)
        values = list(workbook.active.values)
        return workbook, [dict(zip(values[0], row)) for row in values[1:]]

    def test_create_multiple_product_codes_is_atomic_idempotent_and_does_not_change_stock(self):
        before = {model: list(model.objects.order_by('pk').values())
                  for model in (StockMovement, Shipment, PurchaseOrder, MedicationProduct)}
        data = self.supplier_data()
        first, replay = self.post_supplier(data), self.post_supplier(data)
        self.assertEqual((first.status_code, replay.status_code), (302, 302))
        saved = Supplier.objects.get(company=self.company, name='New Supplier')
        self.assertEqual(dict(saved.product_links.values_list('product_id', 'supplier_code')),
                         {self.product.pk: 'CODE-1', self.open_product.pk: 'CODE-2'})
        self.assertEqual(AuditEvent.objects.filter(action='supplier.created').count(), 1)
        for model, records in before.items():
            self.assertEqual(list(model.objects.order_by('pk').values()), records)
        self.assertEqual(self.post_supplier({**data, 'phone': 'different'}).status_code, 400)

    def test_create_without_products_and_blank_extra_row_is_allowed(self):
        data = self.supplier_data(products=[(None, '')], name='Contact only')
        self.assertEqual(self.post_supplier(data).status_code, 302)
        self.assertFalse(Supplier.objects.get(name='Contact only').product_links.exists())
        zero = self.supplier_data(products=[], name='No rows')
        self.assertEqual(self.post_supplier(zero).status_code, 302)

    def test_invalid_product_code_duplicate_product_and_foreign_product_preserve_bound_popup(self):
        for products in ([(self.product, '')], [(self.product, 'A'), (self.product, 'B')],
                         [(self.product, 'same'), (self.open_product, 'SAME')], [(self.beta_product, 'PRIVATE')]):
            with self.subTest(products=products):
                data = self.supplier_data(products=products)
                response = self.post_supplier(data)
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.context['supplier_editor_open'])
                self.assertEqual(response.context['supplier_context'], data['workflow_context'])
                self.assertEqual(response.context['supplier_form']['name'].value(), 'New Supplier')
                self.assertTrue(response.context['product_formset'].errors or response.context['product_formset'].non_form_errors())
                self.assertFalse(Supplier.objects.filter(name='New Supplier').exists())

    def test_duplicate_name_and_invalid_email_do_not_save_partial_links(self):
        for fields in ({'name': 'alpha MEDICAL'}, {'email': 'not-an-email'}, {'name': ''}):
            with self.subTest(fields=fields):
                response = self.post_supplier({**self.supplier_data(), **fields})
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.context['supplier_form'].errors)
        self.assertEqual(Supplier.objects.filter(company=self.company).count(), 1)
        self.assertEqual(SupplierProduct.objects.filter(company=self.company).count(), 1)

    def test_edit_replaces_links_accepts_hidden_products_and_can_remove_all_links(self):
        MedicationProduct.objects.filter(pk=self.open_product.pk).update(is_active=False)
        data = self.supplier_data(supplier=self.supplier, products=[(self.open_product, 'HIDDEN-CODE')], name='Alpha Updated')
        first, replay = self.post_supplier(data, self.supplier), self.post_supplier(data, self.supplier)
        self.assertEqual((first.status_code, replay.status_code), (302, 302))
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.name, 'Alpha Updated')
        self.assertEqual(list(self.supplier.product_links.values_list('product_id', 'supplier_code')),
                         [(self.open_product.pk, 'HIDDEN-CODE')])
        remove = self.supplier_data(supplier=self.supplier, products=[(self.open_product, 'HIDDEN-CODE')], name='Alpha Updated')
        remove['products-0-DELETE'] = 'on'
        self.assertEqual(self.post_supplier(remove, self.supplier).status_code, 302)
        self.assertFalse(self.supplier.product_links.exists())

    def test_stale_update_and_delete_are_rejected_with_same_confirmation_context(self):
        update, delete = self.supplier_data(supplier=self.supplier), self.delete_data()
        Supplier.objects.filter(pk=self.supplier.pk).update(updated_at=timezone.now() + timedelta(seconds=2))
        response = self.post_supplier(update, self.supplier)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['supplier_context'], update['workflow_context'])
        delete_response = self.client.post(reverse('portal:supplier-delete', args=[self.supplier.pk]), delete)
        self.assertEqual(delete_response.status_code, 400)
        self.assertTrue(delete_response.context['supplier_delete_error'])
        self.assertEqual(delete_response.context['supplier_delete_context'], delete['workflow_context'])
        self.assertTrue(Supplier.objects.filter(pk=self.supplier.pk).exists())

    def test_browser_replacement_edit_preserves_unchanged_links_and_only_removes_omitted_rows(self):
        SupplierProduct.objects.create(company=self.company, supplier=self.supplier,
            product=self.open_product, supplier_code='KEEP-UNCHANGED')
        data = self.supplier_data(supplier=self.supplier,
            products=[(self.product, 'EDITED-CODE'), (self.open_product, 'KEEP-UNCHANGED')], name=self.supplier.name)
        data['products-INITIAL_FORMS'] = '0'
        response = self.post_supplier(data, self.supplier)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(dict(self.supplier.product_links.values_list('product_id', 'supplier_code')),
                         {self.product.pk: 'EDITED-CODE', self.open_product.pk: 'KEEP-UNCHANGED'})
        # A second save with both rows entirely unchanged must keep both too.
        unchanged = self.supplier_data(supplier=self.supplier,
            products=[(self.product, 'EDITED-CODE'), (self.open_product, 'KEEP-UNCHANGED')], name=self.supplier.name)
        unchanged['products-INITIAL_FORMS'] = '0'
        self.assertEqual(self.post_supplier(unchanged, self.supplier).status_code, 302)
        self.assertEqual(self.supplier.product_links.count(), 2)
        # The browser removes a row from the replacement list when its trash icon is clicked.
        removed = self.supplier_data(supplier=self.supplier,
            products=[(self.open_product, 'KEEP-UNCHANGED')], name=self.supplier.name)
        removed['products-INITIAL_FORMS'] = '0'
        self.assertEqual(self.post_supplier(removed, self.supplier).status_code, 302)
        self.assertEqual(list(self.supplier.product_links.values_list('product_id', 'supplier_code')),
                         [(self.open_product.pk, 'KEEP-UNCHANGED')])

    def test_delete_confirmation_get_is_read_only_and_post_is_replay_safe(self):
        self.page('supplier-delete', [self.supplier.pk])
        self.assertTrue(Supplier.objects.filter(pk=self.supplier.pk).exists())
        before_stock = list(StockMovement.objects.values())
        data = self.delete_data()
        url = reverse('portal:supplier-delete', args=[self.supplier.pk])
        first, replay = self.client.post(url, data), self.client.post(url, data)
        self.assertEqual((first.status_code, replay.status_code), (302, 302))
        self.assertFalse(Supplier.objects.filter(pk=self.supplier.pk).exists())
        self.assertFalse(SupplierProduct.objects.filter(supplier_id=self.supplier.pk).exists())
        self.assertTrue(MedicationProduct.objects.filter(pk=self.product.pk).exists())
        self.assertEqual(list(StockMovement.objects.values()), before_stock)
        self.assertEqual(AuditEvent.objects.filter(action='supplier.deleted').count(), 1)

    def test_signed_forms_reject_missing_tampered_expired_and_wrong_record_intents(self):
        create = self.supplier_data()
        update = self.supplier_data(supplier=self.supplier)
        delete = self.delete_data()
        with patch('django.core.signing.time.time', return_value=1):
            expired = self.listing(create='1').context['supplier_context']
        for token in ('', 'tampered', expired, update['workflow_context'], delete['workflow_context']):
            with self.subTest(token=token):
                self.assertEqual(self.post_supplier({**create, 'workflow_context': token}).status_code, 400)
        self.assertEqual(self.post_supplier({**update, 'workflow_context': create['workflow_context']}, self.supplier).status_code, 400)
        self.assertEqual(self.client.post(reverse('portal:supplier-delete', args=[self.supplier.pk]),
                                         {'workflow_context': create['workflow_context']}).status_code, 400)
        self.assertEqual(Supplier.objects.filter(company=self.company).count(), 1)

    def test_active_company_and_foreign_supplier_or_product_ids_cannot_bypass_scope(self):
        response = self.listing(company=self.beta.pk)
        self.assertEqual([supplier.pk for supplier in response.context['suppliers']], [self.supplier.pk])
        self.assertNotContains(response, self.beta_supplier.name)
        for query in ({'edit': self.beta_supplier.pk}, {'delete': self.beta_supplier.pk}):
            self.assertEqual(self.client.get(reverse('portal:supplier-list'), query).status_code, 404)
        for route in ('supplier-edit', 'supplier-delete'):
            url = reverse(f'portal:{route}', args=[self.beta_supplier.pk])
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertIn(self.client.post(url, self.supplier_data()).status_code, (400, 404))
        data = self.supplier_data()
        CompanyMembership.objects.create(company=self.beta, user=self.super_admin, role='super_admin')
        self.select(self.beta)
        data['products-0-product'] = str(self.beta_product.pk)
        self.assertEqual(self.post_supplier(data).status_code, 400)

    def test_search_product_codes_contact_and_pagination_share_complete_excel_scope(self):
        for number in range(27):
            Supplier.objects.create(company=self.company, name=f'Supply {number:02}', contact_name='Common Contact')
        self.assertEqual([supplier.pk for supplier in self.listing(q='alpha-01').context['suppliers']], [self.supplier.pk])
        self.assertEqual([supplier.pk for supplier in self.listing(q='Dose A').context['suppliers']], [self.supplier.pk])
        self.assertEqual([supplier.pk for supplier in self.listing(q='Pat BUYER').context['suppliers']], [self.supplier.pk])
        second = self.listing(q='Common', page=2)
        self.assertEqual(len(second.context['suppliers']), 2)
        self.assertEqual(second.context['page_obj'].paginator.count, 27)
        self.assertEqual(parse_qs(urlsplit(second.context['table_export_url']).query), {'q': ['Common']})
        _, rows = self.export(q='Common', page=2)
        self.assertEqual(len(rows), 27)
        self.assertTrue(all(row['Supplier'].startswith('Supply ') for row in rows))
        workbook, linked = self.export(q='ALPHA-01')
        self.assertEqual((linked[0]['Supplier'], linked[0]['Product'], linked[0]['Supplier product code']),
                         (self.supplier.name, self.product.name, 'ALPHA-01'))
        self.assertTrue(all(cell.data_type != 'f' for row in workbook.active for cell in row))

    def test_invalid_search_disables_export_and_does_not_return_all_suppliers(self):
        params = {'q': 'x' * 101}
        response = self.client.get(reverse('portal:supplier-list'), params)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['suppliers'], [])
        self.assertTrue(response.context['table_export_disabled'])
        self.assertEqual(self.client.get(reverse('portal:supplier-export'), params).status_code, 400)

    def test_only_super_admins_can_read_write_export_or_open_modals_and_csrf_is_enforced(self):
        data = self.supplier_data()
        routes = [('supplier-list', []), ('supplier-create', []), ('supplier-edit', [self.supplier.pk]),
                  ('supplier-delete', [self.supplier.pk]), ('supplier-export', [])]
        for actor in (self.admin, self.doctor, self.patient_user):
            self.login(actor)
            for route, args in routes:
                with self.subTest(actor=actor.pk, route=route):
                    self.assertEqual(self.client.get(reverse(f'portal:{route}', args=args)).status_code, 403)
                    self.assertEqual(self.client.post(reverse(f'portal:{route}', args=args), data).status_code, 403)
        self.client.logout()
        for route, args in routes:
            self.assertEqual(self.client.get(reverse(f'portal:{route}', args=args)).status_code, 302)
        client = Client(enforce_csrf_checks=True)
        self.login(self.super_admin, client=client)
        csrf_data = self.supplier_data(client=client)
        self.assertEqual(self.post_supplier(csrf_data, client=client).status_code, 403)
