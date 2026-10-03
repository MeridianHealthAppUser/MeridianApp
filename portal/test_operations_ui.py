"""Smoke coverage for separate supply pages and their scoped form affordances."""

from collections import Counter
from html.parser import HTMLParser
import json
import os
import subprocess
from unittest import skipUnless

from django.urls import reverse
from django.conf import settings

from care.models import MedicationProduct, PharmacyOrder
from care.operations import dispatch_shipment
from care.pharmacy import set_basket_quantity, submit_basket
from care.test_operations import OperationsFixture
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


class InputIds(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.forms = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.append(attrs['id'])
        if tag == 'form':
            self.forms.append(attrs)


class OperationsTemplateTests(OperationsFixture):
    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def basket(self):
        return set_basket_quantity(company=self.company, patient=self.patient, actor=self.patient_user,
                                   product=self.product, quantity=1)

    def submitted_order(self):
        basket = self.basket()
        return submit_basket(order=basket, actor=self.patient_user, confirm=True, expected_revision=basket.revision,
                              delivery_address={'line1': '1 Example Road', 'line2': '', 'city': 'Cape Town',
                                                'province': 'Western Cape', 'postal_code': '8000', 'phone': '0820000000'})

    def assert_page(self, route, heading, args=()):
        response = self.client.get(reverse(f'portal:{route}', args=args))
        self.assertContains(response, heading)
        self.assertNotContains(response, 'Private beta product')
        parser = InputIds()
        parser.feed(response.content.decode())
        duplicates = [key for key, count in Counter(parser.ids).items() if count > 1]
        self.assertEqual(duplicates, [], f'Duplicate IDs on {route}')
        return response

    def test_all_staff_supply_pages_render_as_separate_pages(self):
        batch = self.receive()
        shipment = self.shipment(quantity=1)
        order = self.submitted_order()
        self.login(self.super_admin)
        for route, heading, args in (
            ('ops-catalogue', 'Product catalogue', ()), ('ops-product-create', 'Add a product', ()),
            ('ops-product-detail', 'Explicit prescribed product', (self.product.pk,)),
            ('ops-stock', 'Batches and stock', ()), ('ops-stock-receive', 'Receive stock', ()),
            ('ops-batch-detail', 'Stock movement ledger', (batch.pk,)),
            ('ops-shipping', 'Weekly shipping list', ()), ('ops-shipping-create', 'Create a shipment record', ()),
            ('ops-shipment-detail', 'Shipment record', (shipment.pk,)),
            ('ops-history', 'Dispatch history', ()), ('ops-orders', '<h1>Supply requests</h1>', ()),
            ('ops-order-detail', 'Review this request', (order.pk,)),
        ):
            with self.subTest(route=route):
                self.assert_page(route, heading, args)

    def test_doctor_readonly_views_do_not_render_operational_write_forms(self):
        batch = self.receive()
        shipment = self.shipment(quantity=1)
        order = self.submitted_order()
        self.login(self.doctor)
        for route, pk in (('ops-product-detail', self.product.pk), ('ops-batch-detail', batch.pk),
                          ('ops-shipment-detail', shipment.pk), ('ops-order-detail', order.pk)):
            response = self.client.get(reverse(f'portal:{route}', args=[pk]))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, 'name="workflow_context"')

    def test_patient_catalogue_and_basket_show_no_payment_flow(self):
        self.login(self.patient_user)
        catalogue = self.assert_page('patient-pharmacy', 'My Medications')
        self.assertContains(catalogue, 'Add to basket')
        empty = self.assert_page('patient-basket', 'Your basket is empty')
        self.assertNotContains(empty, 'Submit supply request')
        self.basket()
        populated = self.assert_page('patient-basket', 'Submit supply request')
        self.assertContains(populated, 'name="workflow_context"')
        self.assertContains(populated, 'not a payment screen')

    def test_patient_order_detail_and_history_render_without_staff_actions(self):
        order = self.submitted_order()
        self.login(self.patient_user)
        self.assert_page('patient-orders', 'Your supply requests')
        detail = self.assert_page('patient-order-detail', 'Cancel supply request', (order.pk,))
        self.assertNotContains(detail, 'Accept for preparation')
        self.assertContains(detail, 'name="action" value="cancel"')
        order.status = PharmacyOrder.Status.CANCELLED
        order.save(update_fields=('status',))
        detail = self.client.get(reverse('portal:patient-order-detail', args=[order.pk]))
        self.assertNotContains(detail, 'Cancel supply request')

    def test_dispatch_page_uses_original_snapshot_product_and_patient_names(self):
        self.receive()
        dispatched = dispatch_shipment(shipment=self.locked(), actor=self.admin, tracking_number='TRACK-SNAPSHOT', confirm=True)
        MedicationProduct.objects.filter(pk=self.product.pk).update(name='Changed live name')
        self.login(self.admin)
        response = self.assert_page('ops-shipment-detail', 'Items at dispatch', (dispatched.pk,))
        self.assertContains(response, 'Explicit prescribed product')
        self.assertNotContains(response, 'Changed live name')

    @skipUnless(os.environ.get('MERIDIAN_PLAYWRIGHT_PATH'), 'Optional local browser runner is not configured')
    def test_responsive_supply_layout_with_synthetic_test_data(self):
        batch = self.receive()
        shipment = self.shipment(quantity=1)
        order = self.submitted_order()
        pages = []
        self.login(self.super_admin)
        for route, args in (
            ('ops-catalogue', ()), ('ops-product-create', ()), ('ops-stock', ()),
            ('ops-stock-receive', ()), ('ops-batch-detail', (batch.pk,)), ('ops-shipping', ()),
            ('ops-shipment-detail', (shipment.pk,)), ('ops-orders', ()), ('ops-order-detail', (order.pk,)),
        ):
            response = self.client.get(reverse(f'portal:{route}', args=args))
            self.assertEqual(response.status_code, 200)
            pages.append(dict(name=route, html=response.content.decode()))
        self.login(self.patient_user)
        self.basket()
        for route, args in (('patient-pharmacy', ()), ('patient-basket', ()), ('patient-orders', ()), ('patient-order-detail', (order.pk,))):
            response = self.client.get(reverse(f'portal:{route}', args=args))
            self.assertEqual(response.status_code, 200)
            pages.append(dict(name=route, html=response.content.decode()))
        result = subprocess.run(['node', str(settings.BASE_DIR / 'scripts' / 'operations_layout_smoke.cjs')],
                                input=json.dumps(pages), text=True, capture_output=True, timeout=90, cwd=settings.BASE_DIR)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['checked'], 39)
