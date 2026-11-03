"""Inventory metrics and manual safety stock preserve stock and tenant boundaries."""
from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from care.models import AuditEvent, MedicationProduct, Shipment, StockMovement
from .test_operations import OperationsPortalFixture


class InventoryMetricsTests(OperationsPortalFixture):
    def setUp(self):
        self.login(self.super_admin)

    def row(self, response, product=None):
        return next(row for row in response.context['rows']
                    if row['product'].pk == (product or self.product).pk)

    def update(self, quantity, *, token=None, product=None):
        product = product or self.product
        token = token if token is not None else self.row(self.page('inventory'), product)['safety_stock_context']
        return self.client.post(reverse('portal:inventory-safety-stock', args=[product.pk]),
                                {'quantity': quantity, 'workflow_context': token})

    def test_awc_uses_completed_weeks_even_when_demand_moves_to_next_week(self):
        monday = self.today - timedelta(days=self.today.weekday())
        parcel = self.shipment(quantity=8, scheduled_for=monday + timedelta(days=14))
        dispatched = timezone.make_aware(datetime.combine(monday - timedelta(days=1), time(12)))
        Shipment.objects.filter(pk=parcel.pk).update(status=Shipment.Status.DELIVERED, dispatched_at=dispatched)
        MedicationProduct.objects.filter(pk=self.product.pk).update(is_active=False)
        with patch('portal.operations_views.this_week', return_value=monday), \
             patch('portal.operations_views.shipping_week', return_value=monday + timedelta(days=7)):
            response = self.page('inventory')
        self.assertEqual(self.row(response)['demand']['average'], 1.0)
        self.assertContains(response, '<span class="inventory-awc-number">1.00</span>', html=True)
        self.assertContains(response, 'Average Weekly Consumption')
        self.assertContains(response, 'previous 8 completed calendar weeks')
        html = response.content.decode()
        self.assertLess(html.index('>AWC</span>'), html.index('>Safety stock</th>'))

    def test_super_admin_can_save_zero_and_clear_safety_stock_without_moving_stock(self):
        movements = StockMovement.objects.count()
        for value, expected in (('12', 12), ('0', 0), ('', None)):
            response = self.update(value)
            self.assertRedirects(response, reverse('portal:inventory'), fetch_redirect_response=False)
            self.product.refresh_from_db()
            self.assertEqual(self.product.safety_stock, expected)
            page = self.page('inventory')
            self.assertEqual(self.row(page)['safety_stock_form']['quantity'].value(), expected)
        self.assertEqual(StockMovement.objects.count(), movements)
        self.assertEqual(AuditEvent.objects.filter(action='inventory.safety_stock_updated').count(), 3)

    def test_invalid_quantity_reopens_only_safety_dialog_and_preserves_input(self):
        movements = StockMovement.objects.count()
        for value in ('-1', '1.5', '2147483648', 'invalid'):
            with self.subTest(value=value):
                response = self.update(value)
                self.assertEqual(response.status_code, 400)
                row = self.row(response)
                self.assertTrue(row['safety_stock_open'])
                self.assertEqual(row['safety_stock_form']['quantity'].value(), value)
                self.assertTrue(row['safety_stock_form'].errors)
                self.assertFalse(response.context['receipt_open'])
                self.assertEqual(sum(bool(item['safety_stock_open']) for item in response.context['rows']), 1)
        self.product.refresh_from_db()
        self.assertIsNone(self.product.safety_stock)
        self.assertEqual(StockMovement.objects.count(), movements)

    def test_workflow_token_is_required_bound_to_product_and_rejects_stale_edits(self):
        page = self.page('inventory')
        other_token = self.row(page, self.open_product)['safety_stock_context']
        for token in ('', 'invalid', other_token):
            self.assertEqual(self.update('4', token=token).status_code, 400)
        token = self.row(page)['safety_stock_context']
        MedicationProduct.objects.filter(pk=self.product.pk).update(updated_at=timezone.now() + timedelta(seconds=1))
        response = self.update('4', token=token)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'changed in another tab', status_code=400)
        self.assertEqual(self.row(response)['safety_stock_context'], token)
        self.assertEqual(self.update('4', token=self.row(response)['safety_stock_context']).status_code, 400)
        self.product.refresh_from_db()
        self.assertIsNone(self.product.safety_stock)

    def test_practice_admin_reads_metrics_but_has_no_editors_or_tokens(self):
        MedicationProduct.objects.filter(pk=self.product.pk).update(lead_time_weeks='0.42', safety_stock=9)
        self.login(self.admin)
        response = self.page('inventory', **{'lead-time': self.product.pk, 'safety-stock': self.product.pk})
        self.assertFalse(response.context['can_edit_metrics'])
        self.assertContains(response, '<td data-label="Lead time (weeks)"><span>0.42</span></td>', html=True)
        self.assertContains(response, '<td data-label="Safety stock"><span>9</span></td>', html=True)
        self.assertNotContains(response, 'data-modal-open="inventory-lead-time-')
        self.assertNotContains(response, 'data-modal-open="inventory-safety-stock-')
        self.assertNotContains(response, 'id="inventory-lead-time-')
        self.assertNotContains(response, 'id="inventory-safety-stock-')
        self.assertContains(response, 'data-modal-open="receive-stock"')
        for row in response.context['modal_rows']:
            self.assertNotIn('lead_time_context', row)
            self.assertNotIn('safety_stock_context', row)
            self.assertFalse(row['lead_time_open'] or row['safety_stock_open'])

    def test_practice_admin_with_correct_signed_intents_cannot_bypass_metric_permissions(self):
        from django.core import signing
        from .workflow_context import SALT

        page = self.page('inventory')
        before = AuditEvent.objects.count(), StockMovement.objects.count()
        for kind, field, value in (('lead-time', 'weeks', '0.42'), ('safety-stock', 'quantity', '9')):
            # Model an old form issued to this administrator before the permission change.
            token_name = 'lead_time_context' if kind == 'lead-time' else 'safety_stock_context'
            data = signing.loads(self.row(page)[token_name], salt=SALT)
            data['actor'] = self.admin.pk
            token = signing.dumps(data, salt=SALT)
            self.login(self.admin)
            response = self.client.post(reverse(f'portal:inventory-{kind}', args=[self.product.pk]),
                                        {field: value, 'workflow_context': token})
            self.assertEqual(response.status_code, 403)
        self.product.refresh_from_db()
        self.assertIsNone(self.product.lead_time_weeks)
        self.assertIsNone(self.product.safety_stock)
        self.assertEqual((AuditEvent.objects.count(), StockMovement.objects.count()), before)

    def test_safety_stock_endpoint_enforces_admin_company_and_csrf(self):
        response = self.client.post(reverse('portal:inventory-safety-stock', args=[self.beta_product.pk]),
                                    {'quantity': '3'})
        self.assertEqual(response.status_code, 404)
        for actor in (self.admin, self.doctor, self.patient_user):
            self.login(actor)
            self.assertEqual(self.update('3', token='invalid').status_code, 403)
        self.client.logout()
        self.assertEqual(self.update('3', token='invalid').status_code, 302)
        csrf_client = Client(enforce_csrf_checks=True)
        self.login(self.super_admin, client=csrf_client)
        response = csrf_client.post(reverse('portal:inventory-safety-stock', args=[self.product.pk]),
                                    {'quantity': '3', 'workflow_context': 'invalid'})
        self.assertEqual(response.status_code, 403)

    def test_reading_and_opening_safety_stock_does_not_change_data(self):
        before = AuditEvent.objects.count(), StockMovement.objects.count()
        page = self.page('inventory', **{'safety-stock': self.product.pk})
        self.assertTrue(self.row(page)['safety_stock_open'])
        self.assertFalse(page.context['receipt_open'])
        self.assertEqual(self.client.get(reverse('portal:inventory-safety-stock', args=[self.product.pk])).status_code, 405)
        self.assertEqual((AuditEvent.objects.count(), StockMovement.objects.count()), before)
