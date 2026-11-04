from io import BytesIO
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from django.urls import reverse
from django.http import QueryDict
from openpyxl import load_workbook

from care.models import AuditEvent, MedicationProduct, PatientSubscription, PharmacyOrder, PracticeSettings, StockMovement
from .export_support import export_url
from .test_operations import OperationsPortalFixture


class OperationsTableExportTests(OperationsPortalFixture):
    def export(self, table, **params):
        return self.client.get(reverse('portal:ops-table-export', kwargs={'table': table}), params)

    def records(self, table, **params):
        response = self.export(table, **params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        content = b''.join(response.streaming_content)
        workbook = load_workbook(BytesIO(content))
        self.addCleanup(workbook.close)
        rows = list(workbook.active.iter_rows(values_only=True))
        return [dict(zip(rows[0], row)) for row in rows[1:]]

    def test_inventory_export_has_all_products_typed_metrics_and_no_foreign_products(self):
        MedicationProduct.objects.filter(pk=self.product.pk).update(safety_stock=0, lead_time_weeks='2.50')
        rows = self.records('inventory', page=8)
        self.assertEqual(len(rows), 2)
        row = next(row for row in rows if row['Product'] == self.product.name)
        self.assertEqual((row['Category'], row['On hand'], row['Reserved']), ('Weight management', 100, 0))
        self.assertEqual(row['Lead time (weeks)'], 2.5)
        self.assertEqual(row['Safety stock'], 0)
        self.assertNotIn(self.beta_product.name, [row['Product'] for row in rows])

    def test_catalogue_filters_and_full_result_export(self):
        MedicationProduct.objects.filter(pk=self.open_product.pk).update(is_active=False)
        for index in range(24):
            MedicationProduct.objects.create(company=self.company, name=f'Find product {index:02d}', price=1)
        rows = self.records('catalogue-products', q='Find', status='active', page=2)
        self.assertEqual(len(rows), 24)
        self.assertTrue(all(row['Visibility'] == 'Published' for row in rows))
        hidden = self.records('catalogue-products', status='hidden')
        self.assertEqual([row['Product'] for row in hidden], [self.open_product.name])
        self.assertEqual(self.export('catalogue-products', status='invalid').status_code, 400)

    def test_shipping_export_uses_active_queue_week_product_doctor_and_payment_filters(self):
        parcel = self.shipment(quantity=2)
        extra = self.shipment(product=self.open_product, quantity=3)
        ready = self.locked(self.shipment(quantity=1))
        pending = self.records('shipping', week=self.today, shipping_step='pending', product=self.product.pk,
                               doctor=self.doctor.pk, payment='paid')
        self.assertEqual({row['Shipment'] for row in pending}, {parcel.pk})
        confirmed = self.records('shipping', week=self.today, shipping_step='confirmed')
        self.assertEqual({row['Shipment'] for row in confirmed}, {ready.pk})
        medication = self.records('shipping', week=self.today, shipping_step='pending', view='medication')
        self.assertEqual({row['Shipment'] for row in medication}, {parcel.pk, extra.pk})
        self.assertIn('Product', medication[0])
        self.assertEqual(self.export('shipping', week='bad').status_code, 400)

    def test_export_detail_records_and_reservations_are_practice_scoped(self):
        parcel = self.locked(self.shipment(quantity=2))
        rows = self.records('inventory-reservations', pk=self.product.pk)
        self.assertEqual([row['Shipment'] for row in rows], [parcel.pk])
        self.assertEqual(self.records('shipment-items', pk=parcel.pk)[0]['Quantity'], 2)
        order = self.submitted()
        self.assertEqual(self.records('request-items', pk=order.pk)[0]['Quantity'], 2)
        self.assertEqual([row['Request'] for row in self.records('requests')], [order.pk])
        foreign_shipment = self.shipment(company=self.beta, patient=self.beta_patient,
                                         subscription=None, product=self.beta_product)
        foreign_order = PharmacyOrder.objects.create(company=self.beta, patient=self.beta_patient, status='submitted')
        foreign_plan = PatientSubscription.objects.create(company=self.beta, patient=self.beta_patient,
                                                          plan_name='Foreign', starts_on=self.today)
        for table, pk in (('shipment-items', foreign_shipment.pk), ('request-items', foreign_order.pk),
                          ('inventory-reservations', self.beta_product.pk), ('billing-cycles', foreign_plan.pk)):
            self.assertEqual(self.export(table, pk=pk).status_code, 404)

    def test_export_permissions_match_admin_and_shipping_permissions(self):
        self.login(self.doctor)
        for table in ('inventory', 'catalogue-products', 'catalogue-doses', 'catalogue-plans',
                      'subscriptions', 'billing', 'requests', 'shipping'):
            self.assertEqual(self.export(table, week=self.today).status_code, 403)
        PracticeSettings.objects.filter(company=self.company).update(doctors_ship=True)
        self.records('shipping', week=self.today)
        self.assertEqual(self.export('inventory').status_code, 403)
        self.login(self.patient_user)
        self.assertEqual(self.export('shipping').status_code, 403)
        self.client.logout()
        self.assertEqual(self.export('inventory').status_code, 302)

    def test_export_links_preserve_multi_filters_and_drop_only_presentation_parameters(self):
        request = self.client.get(reverse('portal:ops-shipping'), {'week': str(self.today), 'page': 2,
            'product': [self.product.pk, self.open_product.pk], 'view': 'medication', 'shipping_step': 'all'}).wsgi_request
        query = parse_qs(urlsplit(export_url('shipping', request)).query)
        self.assertEqual(query['product'], [str(self.product.pk), str(self.open_product.pk)])
        self.assertNotIn('page', query)
        self.assertEqual((query['view'], query['shipping_step']), (['medication'], ['all']))
        page = self.page('ops-history')
        self.assertContains(page, 'data-search-region="table-export"')
        self.assertContains(page, 'title="Download Excel"')
        self.assertNotContains(page, 'data-search-region="history-export"')
        submitted = SimpleNamespace(GET=QueryDict('q=sema&weeks=-1&workflow_context=private&csrfmiddlewaretoken=private'))
        self.assertEqual(parse_qs(urlsplit(export_url('inventory', submitted)).query), {'q': ['sema']})

    def test_exports_audit_get_only_and_do_not_change_inventory(self):
        count = StockMovement.objects.count()
        url = reverse('portal:ops-table-export', kwargs={'table': 'inventory'})
        response = self.client.head(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), b'')
        self.assertFalse(AuditEvent.objects.filter(action='operations.table_exported').exists())
        self.records('inventory')
        self.assertEqual(AuditEvent.objects.filter(action='operations.table_exported').count(), 1)
        self.assertEqual(StockMovement.objects.count(), count)
        self.assertEqual(self.export('unknown').status_code, 404)
        self.assertEqual(self.client.post(url).status_code, 405)
