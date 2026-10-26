"""Smoke coverage for separate supply pages and their scoped form affordances."""

from collections import Counter
from html.parser import HTMLParser
import json
import os
import subprocess
from unittest import skipUnless
from urllib.parse import parse_qs, urlsplit

from django.urls import reverse
from django.conf import settings

from care.models import AuditEvent, MedicationProduct, PharmacyOrder, Shipment
from care.operations import dispatch_shipment
from care.pharmacy import set_basket_quantity, submit_basket
from care.test_operations import OperationsFixture
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


class InputIds(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.forms = []
        self.shipping_tabs = []
        self.shipping_panels = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.append(attrs['id'])
        if tag == 'form':
            self.forms.append(attrs)
        if 'data-shipping-step' in attrs:
            self.shipping_tabs.append(attrs)
        if 'data-shipping-panel' in attrs:
            self.shipping_panels.append(attrs)


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
        shipment = self.shipment(quantity=1)
        order = self.submitted_order()
        self.login(self.super_admin)
        for route, heading, args in (
            ('ops-catalogue', 'Product catalogue', ()), ('ops-product-create', 'Add a product', ()),
            ('ops-product-detail', 'Explicit prescribed product', (self.product.pk,)),
            ('ops-shipping', 'id="create-shipment"', ()),
            ('ops-shipment-detail', 'Shipment record', (shipment.pk,)),
            ('ops-history', 'Dispatch history', ()), ('ops-orders', f'<a href="{reverse("portal:ops-orders")}" class="is-active" aria-current="page">Non-prescription requests</a>', ()),
            ('ops-order-detail', 'Review this request', (order.pk,)),
        ):
            with self.subTest(route=route):
                self.assert_page(route, heading, args)

    def test_doctors_cannot_open_operations_detail_pages(self):
        shipment = self.shipment(quantity=1)
        order = self.submitted_order()
        self.login(self.doctor)
        for route, pk in (('ops-product-detail', self.product.pk), ('ops-shipment-detail', shipment.pk), ('ops-order-detail', order.pk)):
            self.assertEqual(self.client.get(reverse(f'portal:{route}', args=[pk])).status_code, 403)

    def test_patient_catalogue_and_basket_say_how_a_request_is_paid(self):
        self.login(self.patient_user)
        catalogue = self.assert_page('patient-pharmacy', 'My Medications')
        self.assertContains(catalogue, 'Add to basket')
        empty = self.assert_page('patient-basket', 'Your basket is empty')
        self.assertNotContains(empty, 'Submit supply request')
        self.basket()
        populated = self.assert_page('patient-basket', 'Submit supply request')
        self.assertContains(populated, 'name="workflow_context"')
        self.assertContains(populated, 'pay for it by card on the request’s page')

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
        dispatched = dispatch_shipment(shipment=self.locked(), actor=self.admin, tracking_number='TRACK-SNAPSHOT', confirm=True)
        MedicationProduct.objects.filter(pk=self.product.pk).update(name='Changed live name')
        self.login(self.admin)
        response = self.assert_page('ops-shipment-detail', 'Items at dispatch', (dispatched.pk,))
        self.assertContains(response, 'Explicit prescribed product')
        self.assertNotContains(response, 'Changed live name')

    def test_workflow_step_links_select_one_panel_without_changing_shipments(self):
        shipment = self.shipment(quantity=1)
        self.login(self.super_admin)
        audits = AuditEvent.objects.count()
        for step in ('ready', 'lock', 'order', 'courier', 'collection'):
            with self.subTest(step=step):
                response = self.client.get(reverse('portal:ops-shipping'),
                                           {'week': self.today, '__full': '1', 'shipping_step': step})
                self.assertEqual(response.status_code, 200)
                markup = InputIds()
                markup.feed(response.content.decode())
                self.assertEqual(len(markup.shipping_tabs), 5)
                self.assertEqual([tab['data-shipping-step'] for tab in markup.shipping_tabs
                                  if tab['aria-selected'] == 'true'], [step])
                self.assertEqual([panel['data-shipping-panel'] for panel in markup.shipping_panels
                                  if 'hidden' not in panel], [step])
                for action in ('ops-shipping-prepare', 'ops-shipping-lock'):
                    self.assertIn(reverse(f'portal:{action}'), {form.get('action') for form in markup.forms})
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, Shipment.Status.DRAFT)
        self.assertEqual(AuditEvent.objects.count(), audits)

    def test_workflow_native_links_preserve_repeated_filters_and_validate_selection(self):
        self.login(self.super_admin)
        response = self.client.get(reverse('portal:ops-shipping'),
                                   {'week': self.today, 'status': ['draft', 'ready'], 'q': 'Alpha & Patient',
                                    'shipping_step': 'invalid', 'create': '1', '__shell': '1', '__full': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['shipping_step'], 'ready')
        for step in response.context['shipping_steps']:
            address = urlsplit(step['url'])
            query = parse_qs(address.query)
            self.assertEqual(address.path, reverse('portal:ops-shipping'))
            self.assertEqual(query['status'], ['draft', 'ready'])
            self.assertEqual(query['q'], ['Alpha & Patient'])
            self.assertEqual(query['shipping_step'], [step['key']])
            self.assertEqual(query['__full'], ['1'])
            self.assertNotIn('create', query)
            self.assertNotIn('__shell', query)
            self.assertEqual(address.fragment, 'shipping-workflow')

    def test_workflow_badges_distinguish_empty_held_and_ready_weeks(self):
        self.login(self.super_admin)
        url = reverse('portal:ops-shipping')

        def steps():
            return {step['key']: step for step in self.client.get(url, {'week': self.today}).context['shipping_steps']}

        self.assertFalse(any(step['complete'] for step in steps().values()))
        shipment = self.shipment(quantity=1, status=Shipment.Status.HELD)
        self.assertFalse(steps()['ready']['complete'])
        Shipment.objects.filter(pk=shipment.pk).update(status=Shipment.Status.READY)
        ready = steps()
        self.assertTrue(ready['ready']['complete'])
        self.assertTrue(ready['lock']['current'])
        self.assertFalse(ready['order']['complete'])
        self.assertEqual(ready['courier']['summary'], 'Not booked')
        self.assertEqual(ready['collection']['summary'], 'Pending')

    def test_collection_dialog_has_a_native_get_fallback_without_dispatching(self):
        shipment = self.locked(self.shipment(quantity=1))
        Shipment.objects.filter(pk=shipment.pk).update(waybill='TEST-COLLECTION')
        self.login(self.super_admin)
        response = self.client.get(reverse('portal:ops-shipping'), {'week': self.today, 'shipping_step': 'collection'})
        self.assertEqual(response.status_code, 200)
        fallback = self.client.get(response.context['collection_url'])
        self.assertContains(fallback, 'aria-labelledby="record-collection-title" open')
        self.assertEqual(fallback.context['shipping_step'], 'collection')
        self.assertContains(fallback, f'action="{reverse("portal:ops-shipping-collect")}"')
        closed = self.client.get(fallback.context['collection_close_url'])
        self.assertNotContains(closed, 'aria-labelledby="record-collection-title" open')
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, Shipment.Status.READY)

    @skipUnless(os.environ.get('MERIDIAN_PLAYWRIGHT_PATH'), 'Optional local browser runner is not configured')
    def test_responsive_supply_layout_with_synthetic_test_data(self):
        shipment = self.shipment(quantity=1)
        order = self.submitted_order()
        pages = []
        self.login(self.super_admin)
        for route, args in (
            ('ops-catalogue', ()), ('ops-product-create', ()), ('ops-shipping', ()),
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
        self.assertEqual(data['checked'], 30)
