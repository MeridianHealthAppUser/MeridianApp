"""Only Super Admins can edit starting AWC; all stock readers see the effective value."""
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from care.models import AuditEvent, MedicationProduct, Payment, Shipment, StockMovement, SubscriptionCycle
from practices.models import CompanyMembership
from .test_operations import OperationsPortalFixture
from .workflow_context import make_workflow_context


class InventoryStartingAWCTests(OperationsPortalFixture):
    def setUp(self):
        super().setUp()
        self.login(self.super_admin)

    def row(self, response, product=None):
        pk = (product or self.product).pk
        return next(row for row in response.context['modal_rows'] if row['product'].pk == pk)

    def update(self, value, *, token=None, product=None, q=''):
        product = product or self.product
        if token is None:
            token = self.row(self.page('inventory', q=q), product)['awc_context']
        return self.client.post(reverse('portal:inventory-starting-awc', args=[product.pk]),
            {'starting_awc': value, 'workflow_context': token, 'q': q})

    def history(self):
        week = self.today - timedelta(days=self.today.weekday())
        day = week - timedelta(days=1)
        timestamp = timezone.make_aware(datetime.combine(day, time(12)), timezone.get_default_timezone())
        self.shipment(quantity=8, subscription=None, status=Shipment.Status.DISPATCHED,
                      scheduled_for=day, dispatched_at=timestamp)

    def test_super_admin_has_editor_while_practice_admin_reads_the_same_effective_awc(self):
        self.history()
        MedicationProduct.objects.filter(pk=self.product.pk).update(starting_awc='2.75')
        super_page = self.page('inventory')
        self.assertTrue(super_page.context['can_edit_awc'])
        self.assertContains(super_page, f'data-modal-open="inventory-starting-awc-{self.product.pk}"')
        self.assertContains(super_page, 'name="starting_awc" min="0" max="99999999.99" step="0.01"')
        self.assertEqual(self.row(super_page)['awc_form']['starting_awc'].value(), Decimal('2.75'))
        self.login()
        page = self.page('inventory', awc=self.product.pk)
        row = self.row(page)
        self.assertFalse(page.context['can_edit_awc'])
        self.assertEqual((row['historical_awc'], row['awc'], row['awc_source']),
                         (Decimal('1.0'), Decimal('2.75'), 'starting'))
        self.assertContains(page, '>2.75</span>')
        self.assertNotContains(page, f'data-modal-open="inventory-starting-awc-{self.product.pk}"')
        self.assertNotContains(page, f'id="inventory-starting-awc-{self.product.pk}"')
        self.assertNotIn('awc_context', row)
        self.assertNotIn('awc_form', row)

    def test_save_fraction_explicit_zero_and_clear_preserves_search_and_stock(self):
        self.history()
        models = (StockMovement, Shipment, Payment, SubscriptionCycle)
        before = {model: list(model.objects.order_by('pk').values()) for model in models}
        query = 'Explicit prescribed'
        for value, stored, effective, source in (('2.75', Decimal('2.75'), Decimal('2.75'), 'starting'),
                                                  ('0', Decimal('0.00'), Decimal('0.00'), 'starting'),
                                                  ('', None, Decimal('1.0'), 'history')):
            with self.subTest(value=value):
                response = self.update(value, q=query)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(urlsplit(response['Location']).path, reverse('portal:inventory'))
                self.assertEqual(parse_qs(urlsplit(response['Location']).query), {'q': [query]})
                self.product.refresh_from_db()
                self.assertEqual(self.product.starting_awc, stored)
                row = self.row(self.page('inventory', q=query))
                self.assertEqual((row['awc'], row['awc_source']), (effective, source))
                self.assertEqual(row['awc_form']['starting_awc'].value(), stored)
        self.assertEqual({model: list(model.objects.order_by('pk').values()) for model in models}, before)
        self.assertEqual(list(AuditEvent.objects.filter(action='inventory.starting_awc_updated')
            .order_by('pk').values_list('metadata', flat=True)),
            [{'before': None, 'after': '2.75'}, {'before': '2.75', 'after': '0.00'}, {'before': '0.00', 'after': None}])

    def test_unchanged_value_is_idempotent_and_inactive_own_products_can_be_configured(self):
        self.product.is_active = False
        self.product.save(update_fields=('is_active', 'updated_at'))
        self.assertEqual(self.update('1.25').status_code, 302)
        self.product.refresh_from_db()
        saved_at = self.product.updated_at
        self.assertEqual(self.update('1.25').status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual((self.product.starting_awc, self.product.updated_at, self.product.is_active),
                         (Decimal('1.25'), saved_at, False))
        self.assertEqual(AuditEvent.objects.filter(action='inventory.starting_awc_updated').count(), 1)

    def test_invalid_values_reopen_only_awc_preserving_search_input_and_submitted_token(self):
        for value in ('-1', '1.234', '100000000', 'NaN', 'Infinity', 'invalid'):
            with self.subTest(value=value):
                query = 'no matching stock'
                token = self.row(self.page('inventory', q=query))['awc_context']
                response = self.update(value, token=token, q=query)
                self.assertEqual(response.status_code, 400)
                row = self.row(response)
                self.assertTrue(row['awc_open'])
                self.assertTrue(row['awc_form'].errors)
                self.assertEqual(row['awc_form']['starting_awc'].value(), value)
                self.assertEqual(row['awc_context'], token)
                self.assertEqual(response.context['search'], query)
                self.assertEqual(response.context['rows'], [])
                self.assertEqual(parse_qs(urlsplit(response.context['inventory_return_url']).query), {'q': [query]})
                self.assertEqual(sum(bool(item.get('awc_open')) for item in response.context['modal_rows']), 1)
                self.assertFalse(response.context['receipt_open'])
                self.assertFalse(any(item['lead_time_open'] or item['safety_stock_open'] or item['reserved_open']
                                     for item in response.context['modal_rows']))
                self.assertContains(response, f'id="inventory-starting-awc-{self.product.pk}"', status_code=400)
        self.product.refresh_from_db()
        self.assertIsNone(self.product.starting_awc)
        self.assertFalse(AuditEvent.objects.filter(action='inventory.starting_awc_updated').exists())

    def test_context_is_bound_to_product_kind_and_actor_and_expired_context_is_rejected(self):
        page = self.page('inventory')
        other_product = self.row(page, self.open_product)['awc_context']
        wrong_kind = self.row(page)['lead_time_context']
        other_actor_request = RequestFactory().get('/inventory/')
        other_actor_request.user = self.admin
        other_actor = make_workflow_context(other_actor_request, self.company, 'inventory-starting-awc', self.product)
        with patch('django.core.signing.time.time', return_value=1):
            expired = make_workflow_context(page.wsgi_request, self.company, 'inventory-starting-awc', self.product)
        for token in ('', 'tampered', other_product, wrong_kind, other_actor, expired):
            with self.subTest(token=token):
                response = self.update('3.5', token=token)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.row(response)['awc_context'], token)
        self.product.refresh_from_db()
        self.assertIsNone(self.product.starting_awc)

    def test_stale_post_keeps_its_old_context_instead_of_upgrading_it(self):
        token = self.row(self.page('inventory'))['awc_context']
        MedicationProduct.objects.filter(pk=self.product.pk).update(updated_at=timezone.now() + timedelta(seconds=1))
        response = self.update('3.5', token=token, q='Explicit')
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'changed in another tab', status_code=400)
        self.assertEqual(self.row(response)['awc_context'], token)
        self.assertEqual(response.context['search'], 'Explicit')
        self.assertEqual(self.update('3.5', token=self.row(response)['awc_context']).status_code, 400)
        self.product.refresh_from_db()
        self.assertIsNone(self.product.starting_awc)

    def test_endpoint_enforces_super_admin_company_login_and_csrf(self):
        token = self.row(self.page('inventory'))['awc_context']
        self.assertEqual(self.update('3.5', token=token, product=self.beta_product).status_code, 404)
        for actor in (self.admin, self.doctor, self.patient_user):
            self.login(actor)
            self.assertEqual(self.update('3.5', token=token).status_code, 403)
        self.client.logout()
        self.assertEqual(self.update('3.5', token=token).status_code, 302)
        csrf_client = Client(enforce_csrf_checks=True)
        self.login(self.super_admin, client=csrf_client)
        page = self.page('inventory', client=csrf_client)
        self.assertEqual(csrf_client.post(reverse('portal:inventory-starting-awc', args=[self.product.pk]),
            {'starting_awc': '3.5', 'workflow_context': self.row(page)['awc_context']}).status_code, 403)

    def test_switching_selected_practice_rejects_the_original_practices_context(self):
        token = self.row(self.page('inventory'))['awc_context']
        CompanyMembership.objects.create(company=self.beta, user=self.super_admin, role='super_admin')
        self.select(self.beta)
        response = self.update('3.5', token=token, product=self.beta_product)
        self.assertEqual(response.status_code, 400)
        self.beta_product.refresh_from_db()
        self.assertIsNone(self.beta_product.starting_awc)

    def test_modal_get_is_read_only_even_when_the_search_hides_the_selected_product(self):
        before = list(MedicationProduct.objects.order_by('pk').values()), StockMovement.objects.count(), AuditEvent.objects.count()
        page = self.page('inventory', awc=self.product.pk, q='no matching stock',
                         **{'lead-time': self.open_product.pk, 'safety-stock': self.open_product.pk})
        self.assertEqual(page.context['rows'], [])
        self.assertTrue(self.row(page)['awc_open'])
        self.assertFalse(page.context['receipt_open'])
        self.assertFalse(any(row['lead_time_open'] or row['safety_stock_open'] or row['reserved_open']
                             for row in page.context['modal_rows']))
        self.assertEqual(self.client.get(reverse('portal:inventory-starting-awc', args=[self.product.pk])).status_code, 405)
        foreign = self.page('inventory', awc=self.beta_product.pk)
        self.assertFalse(any(row.get('awc_open') for row in foreign.context['modal_rows']))
        self.assertNotContains(foreign, f'id="inventory-starting-awc-{self.beta_product.pk}"')
        self.assertEqual((list(MedicationProduct.objects.order_by('pk').values()),
                          StockMovement.objects.count(), AuditEvent.objects.count()), before)

    def test_to_order_uses_effective_awc_and_exposes_no_editor_even_to_super_admin(self):
        MedicationProduct.objects.filter(pk=self.product.pk).update(starting_awc='2.75')
        page = self.page('purchasing')
        row = next(row for row in page.context['rows'] if row['product'].pk == self.product.pk)
        self.assertEqual((row['awc'], row['awc_source']), (Decimal('2.75'), 'starting'))
        self.assertNotContains(page, 'data-modal-open="inventory-starting-awc-')
        self.assertNotContains(page, 'name="starting_awc"')
        self.assertEqual(self.client.post(reverse('portal:purchasing'), {'starting_awc': '5'}).status_code, 405)
        self.product.refresh_from_db()
        self.assertEqual(self.product.starting_awc, Decimal('2.75'))
