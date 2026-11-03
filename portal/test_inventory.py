from django.urls import reverse
from care.models import StockMovement, Shipment
from .test_operations import OperationsPortalFixture


class InventoryPageTests(OperationsPortalFixture):
    def test_inventory_receipt_and_access(self):
        page = self.page('inventory')
        self.assertContains(page, 'On hand')
        response = self.client.post(reverse('portal:inventory'), {
            'workflow_context': page.context['workflow_context'], 'product': self.product.pk,
            'quantity': 7, 'batch': 'TEST-7', 'expires_on': '2030-01-01', 'reference': 'Delivery 7'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(StockMovement.objects.get(batch='TEST-7').quantity, 7)
        self.login(self.patient_user)
        self.assertEqual(self.client.get(reverse('portal:inventory')).status_code, 403)

    def test_customer_and_medication_views_share_parcels_and_selection(self):
        parcel = self.shipment(quantity=2)
        for mode in ('customers', 'medication'):
            page = self.page('ops-shipping', week=self.today, view=mode)
            self.assertContains(page, 'form="shipping-bulk-confirm"')
            self.assertContains(page, f'value="{parcel.pk}"')
            self.assertNotContains(page, 'Pens this week')
            self.assertNotContains(page, 'Order pens')
            html = page.content.decode()
            self.assertLess(html.index('>Due date</th>'), html.index('>Waybill</th>'))
            self.assertNotIn('>Status</th>', html)
            self.assertNotIn('>Shipping status</th>', html)
        page = self.page('ops-shipping', week=self.today, view='medication')
        self.assertEqual(page.context['medication_groups'][0]['quantity'], 2)
        self.assertContains(page, 'Show customers')
        response = self.client.post(reverse('portal:ops-shipping-confirm'), {
            'workflow_context': page.context['confirm_context'], 'week': self.today, 'shipment_ids': [parcel.pk]})
        self.assertEqual(response.status_code, 302)
        parcel.refresh_from_db()
        self.assertEqual(parcel.status, Shipment.Status.READY)
        self.assertIsNotNone(parcel.locked_at)
