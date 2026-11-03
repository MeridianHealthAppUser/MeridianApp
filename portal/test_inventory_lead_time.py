"""Lead-time editing is scoped, signed, optional and separate from stock receipts."""

from datetime import timedelta
from decimal import Decimal

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from care.models import AuditEvent, MedicationProduct, StockMovement
from practices.models import CompanyMembership
from .test_operations import OperationsPortalFixture


class InventoryLeadTimeTests(OperationsPortalFixture):
    def setUp(self):
        self.login(self.super_admin)

    def row(self, response, product=None):
        return next(row for row in response.context['rows'] if row['product'].pk == (product or self.product).pk)

    def update(self, weeks, *, token=None, product=None, **extra):
        product = product or self.product
        if token is None:
            token = self.row(self.page('inventory'), product)['lead_time_context']
        return self.client.post(reverse('portal:inventory-lead-time', args=[product.pk]),
                                {'weeks': weeks, 'workflow_context': token, **extra})

    def test_lead_time_column_is_between_awc_and_safety_stock_with_a_decimal_editor(self):
        response = self.page('inventory')
        html = response.content.decode()

        self.assertLess(html.index('>AWC</span>'), html.index('>Lead time (weeks)</th>'))
        self.assertLess(html.index('>Lead time (weeks)</th>'), html.index('>Safety stock</th>'))
        self.assertContains(response, f'data-modal-open="inventory-lead-time-{self.product.pk}"')
        self.assertContains(response, 'max="9999.99" step="0.01"')
        self.assertContains(response, 'data-label="Lead time (weeks)"')
        self.assertIsNone(self.row(response)['product'].lead_time_weeks)
        self.assertContains(response, 'table-excel-download')

    def test_super_admin_can_save_fraction_zero_and_clear_without_moving_stock(self):
        before = list(StockMovement.objects.order_by('pk').values())
        for value, expected in (('2.5', Decimal('2.50')), ('0', Decimal('0.00')),
                                ('12.34', Decimal('12.34')), ('', None)):
            with self.subTest(value=value):
                response = self.update(value)
                self.assertRedirects(response, reverse('portal:inventory'), fetch_redirect_response=False)
                self.product.refresh_from_db()
                self.assertEqual(self.product.lead_time_weeks, expected)
                self.assertEqual(self.row(self.page('inventory'))['lead_time_form']['weeks'].value(), expected)
        self.assertEqual(list(StockMovement.objects.order_by('pk').values()), before)
        self.assertEqual(AuditEvent.objects.filter(action='inventory.lead_time_updated').count(), 4)

    def test_invalid_values_reopen_only_the_lead_dialog_and_keep_the_entered_value(self):
        before = StockMovement.objects.count()
        for value in ('-1', '1.234', '10000', 'NaN', 'invalid'):
            with self.subTest(value=value):
                response = self.update(value)
                self.assertEqual(response.status_code, 400)
                row = self.row(response)
                self.assertTrue(row['lead_time_open'])
                self.assertEqual(row['lead_time_form']['weeks'].value(), value)
                self.assertTrue(row['lead_time_form'].errors)
                self.assertEqual(sum(bool(item['lead_time_open']) for item in response.context['rows']), 1)
                self.assertFalse(response.context['receipt_open'])
                self.assertFalse(any(item['safety_stock_open'] or item['reserved_open'] for item in response.context['rows']))
        self.product.refresh_from_db()
        self.assertIsNone(self.product.lead_time_weeks)
        self.assertEqual(StockMovement.objects.count(), before)
        self.assertFalse(AuditEvent.objects.filter(action='inventory.lead_time_updated').exists())

    def test_token_is_required_bound_to_product_and_kind_and_never_upgraded_after_a_stale_post(self):
        page = self.page('inventory')
        other_product = self.row(page, self.open_product)['lead_time_context']
        wrong_kind = self.row(page)['safety_stock_context']
        for token in ('', 'invalid', other_product, wrong_kind):
            with self.subTest(token=token):
                self.assertEqual(self.update('2.5', token=token).status_code, 400)
        token = self.row(page)['lead_time_context']
        MedicationProduct.objects.filter(pk=self.product.pk).update(updated_at=timezone.now() + timedelta(seconds=1))
        response = self.update('2.5', token=token)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'changed in another tab', status_code=400)
        self.assertEqual(self.row(response)['lead_time_context'], token)
        self.assertEqual(self.update('2.5', token=self.row(response)['lead_time_context']).status_code, 400)
        self.product.refresh_from_db()
        self.assertIsNone(self.product.lead_time_weeks)

    def test_endpoint_enforces_admin_company_and_csrf(self):
        url = reverse('portal:inventory-lead-time', args=[self.beta_product.pk])
        self.assertEqual(self.client.post(url, {'weeks': '2.5'}).status_code, 404)
        for actor in (self.admin, self.doctor, self.patient_user):
            self.login(actor)
            self.assertEqual(self.update('2.5', token='invalid').status_code, 403)
        self.client.logout()
        self.assertEqual(self.update('2.5', token='invalid').status_code, 302)
        csrf_client = Client(enforce_csrf_checks=True)
        self.login(self.super_admin, client=csrf_client)
        self.assertEqual(csrf_client.post(reverse('portal:inventory-lead-time', args=[self.product.pk]),
            {'weeks': '2.5', 'workflow_context': 'invalid'}).status_code, 403)

    def test_selected_practice_change_rejects_another_practices_signed_token(self):
        token = self.row(self.page('inventory'))['lead_time_context']
        CompanyMembership.objects.create(company=self.beta, user=self.super_admin, role='super_admin')
        self.select(self.beta)

        response = self.update('2.5', token=token, product=self.beta_product)

        self.assertEqual(response.status_code, 400)
        self.beta_product.refresh_from_db()
        self.assertIsNone(self.beta_product.lead_time_weeks)

    def test_opening_editor_is_read_only_and_get_cannot_save(self):
        before = AuditEvent.objects.count(), StockMovement.objects.count()
        page = self.page('inventory', **{'lead-time': self.product.pk, 'safety-stock': self.open_product.pk})

        self.assertTrue(self.row(page)['lead_time_open'])
        self.assertFalse(self.row(page, self.open_product)['safety_stock_open'])
        self.assertFalse(page.context['receipt_open'])
        self.assertEqual(self.client.get(reverse('portal:inventory-lead-time', args=[self.product.pk])).status_code, 405)
        self.assertEqual((AuditEvent.objects.count(), StockMovement.objects.count()), before)
