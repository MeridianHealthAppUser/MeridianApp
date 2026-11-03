"""Inventory's reservation popup describes only live, scoped stock commitments."""
from datetime import timedelta
from html.parser import HTMLParser
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone

from care.models import Shipment, ShipmentItem, StockMovement
from care.operations import dispatch_shipment, hold_or_cancel_shipment
from .test_operations import OperationsPortalFixture


class DialogParser(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.dialogs = {}
        self.ids = []
        self.feed(response.content.decode())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.ids.append(attrs['id'])
        if tag == 'dialog':
            self.dialogs[attrs['id']] = attrs


class InventoryReservationPopupTests(OperationsPortalFixture):
    def inventory_row(self, response, product=None):
        product = product or self.product
        return next(row for row in response.context['rows'] if row['product'].pk == product.pk)

    def test_reserved_count_opens_product_popup_with_shipment_details(self):
        parcel = self.locked(self.shipment(quantity=2, authorization=self.auth))
        parcel.waybill = 'RESERVED-WAYBILL'
        parcel.save(update_fields=['waybill'])
        response = self.page('inventory', reserved=self.product.pk)
        row = self.inventory_row(response)
        item = row['reservations'][0]

        self.assertEqual((row['on_hand'], row['reserved'], row['available']), (100, 2, 98))
        self.assertEqual(item.reservation_doctor, self.doctor)
        self.assertEqual((item.reservation_payment_state, item.reservation_payment_text), ('paid', 'Paid'))
        self.assertContains(response, f'data-modal-open="inventory-reservations-{self.product.pk}"')
        self.assertContains(response, f'href="?reserved={self.product.pk}"')
        self.assertNotContains(response, '<summary>2 reserved</summary>')
        self.assertContains(response, str(self.patient))
        self.assertContains(response, self.doctor.titled_name)
        self.assertContains(response, 'RESERVED-WAYBILL')
        self.assertContains(response, parcel.scheduled_for.strftime('%a %-d %b %Y'))
        self.assertContains(response, reverse('portal:ops-shipment-detail', args=[parcel.pk]))
        dialogs = DialogParser(response).dialogs
        self.assertIn('open', dialogs[f'inventory-reservations-{self.product.pk}'])
        self.assertNotIn('open', dialogs['receive-stock'])

    def test_only_locked_undispatched_shipments_are_reserved_across_weeks(self):
        current = self.locked(self.shipment(quantity=1))
        future = self.locked(self.shipment(quantity=2, scheduled_for=self.today + timedelta(days=28)))
        held = self.locked(self.shipment(product=self.open_product, quantity=1))
        hold_or_cancel_shipment(shipment=held, actor=self.admin, reason='Patient away')
        dispatched = self.locked(self.shipment(product=self.open_product, quantity=1))
        dispatch_shipment(shipment=dispatched, actor=self.admin, tracking_number='RES-DISPATCHED', confirm=True)
        self.ready(self.shipment(product=self.open_product, quantity=1))
        self.shipment(product=self.open_product, quantity=1)

        response = self.page('inventory')
        row = self.inventory_row(response)
        self.assertEqual(row['reserved'], 3)
        self.assertEqual({item.shipment_id for item in row['reservations']}, {current.pk, future.pk})
        self.assertEqual(self.inventory_row(response, self.open_product)['reserved'], 0)
        self.assertNotContains(response, f'data-modal-open="inventory-reservations-{self.open_product.pk}"')

    def test_popup_summary_matches_shortage_and_multiple_products_have_unique_ids(self):
        self.locked(self.shipment(quantity=2))
        self.locked(self.shipment(product=self.open_product, quantity=3))
        StockMovement.objects.filter(company=self.company, product=self.product).update(quantity=1)
        response = self.page('inventory', reserved=self.open_product.pk)
        row = self.inventory_row(response)
        self.assertEqual((row['on_hand'], row['reserved'], row['available'], row['shortage']), (1, 2, -1, 1))
        self.assertContains(response, '1 short for these reservations.')
        parsed = DialogParser(response)
        self.assertEqual(len(parsed.ids), len(set(parsed.ids)))
        self.assertNotIn('open', parsed.dialogs[f'inventory-reservations-{self.product.pk}'])
        self.assertIn('open', parsed.dialogs[f'inventory-reservations-{self.open_product.pk}'])

    def test_foreign_missing_and_zero_reservation_ids_do_not_open_or_leak(self):
        self.locked(self.shipment(quantity=1))
        foreign = self.shipment(company=self.beta, patient=self.beta_patient, subscription=None,
                                product=self.beta_product, quantity=3, status=Shipment.Status.READY,
                                locked_at=timezone.now())
        for selected in (self.beta_product.pk, self.open_product.pk, 999999, 'invalid'):
            with self.subTest(selected=selected):
                response = self.page('inventory', reserved=selected)
                self.assertFalse(any(row['reserved_open'] for row in response.context['rows']))
                self.assertFalse(any('open' in attrs for attrs in DialogParser(response).dialogs.values()))
                self.assertNotContains(response, self.beta_product.name)
                self.assertNotContains(response, str(self.beta_patient))
                self.assertNotContains(response, reverse('portal:ops-shipment-detail', args=[foreign.pk]))

    def test_payment_and_doctor_are_shared_for_multiple_lines_of_same_shipment(self):
        parcel = self.locked(self.shipment(quantity=1))
        ShipmentItem.objects.create(company=self.company, shipment=parcel, product=self.open_product, quantity=2)
        with patch('care.billing.parcel_payment', return_value=('failed', 'Debit failed')) as payment:
            response = self.page('inventory')
        payment.assert_called_once()
        self.assertEqual(payment.call_args.args[0].pk, parcel.pk)
        for row in response.context['rows']:
            for item in row['reservations']:
                self.assertEqual(item.reservation_doctor, self.doctor)
                self.assertEqual((item.reservation_payment_state, item.reservation_payment_text), ('failed', 'Debit failed'))
        self.assertContains(response, 'Debit failed', count=2)

    def test_popup_access_retains_inventory_permissions_and_add_stock_fallback(self):
        self.locked(self.shipment(quantity=1))
        response = self.page('inventory', reserved=self.product.pk, receive='1')
        dialogs = DialogParser(response).dialogs
        self.assertIn('open', dialogs['receive-stock'])
        self.assertNotIn('open', dialogs[f'inventory-reservations-{self.product.pk}'])
        for actor in (self.doctor, self.patient_user):
            with self.subTest(actor=actor):
                self.login(actor)
                response = self.client.get(reverse('portal:inventory'), {'reserved': self.product.pk})
                self.assertEqual(response.status_code, 403)
        self.login(self.beta_admin, self.beta)
        response = self.page('inventory', reserved=self.product.pk)
        self.assertNotContains(response, self.product.name)
        self.assertFalse(any(row['reserved_open'] for row in response.context['rows']))
