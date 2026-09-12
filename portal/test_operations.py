"""Dedicated operations pages enforce signed intent, CSRF and scoped access."""

import csv
import io
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from care.models import (AuditEvent, MedicationBatch, MedicationProduct, Payment, PharmacyOrder, Shipment,
                         StockMovement)
from care.operations import dispatch_shipment
from care.pharmacy import set_basket_quantity, submit_basket
from care.test_operations import OperationsFixture
from care.test_pharmacy import ADDRESS
from practices.models import CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


class OperationsPortalFixture(OperationsFixture):
    def setUp(self):
        self.login()

    def login(self, actor=None, company=None, client=None):
        client = client or self.client
        client.force_login(actor or self.admin)
        self.select(company or self.company, client)

    def select(self, company, client=None):
        client = client or self.client
        session = client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = company.pk
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = company.pk
        session.save()

    def page(self, name, args=None, client=None, **params):
        response = (client or self.client).get(reverse(f'portal:{name}', args=args), params)
        self.assertEqual(response.status_code, 200)
        return response

    def token(self, name, args=None, **params):
        return self.page(name, args, **params).context['workflow_context']

    def receipt_data(self, **extra):
        return {'product': self.product.pk, 'batch_number': 'PORTAL-BATCH', 'received_on': self.today,
                'expires_on': self.today + timedelta(days=90), 'quantity': 10, 'cold_chain_confirmed': 'on', **extra}

    def product_data(self, **extra):
        return {'name': 'New catalogue product', 'strength': 'Product variant', 'category': 'other',
                'price': '120.00', 'description': '', 'allowance': '', 'allowance_group': '',
                'requires_authorisation': 'on', 'requires_cold_chain': 'on', 'is_active': 'on', **extra}

    def basket(self):
        return set_basket_quantity(company=self.company, patient=self.patient, actor=self.patient_user,
                                   product=self.product, quantity=2)

    def submitted(self):
        order = self.basket()
        return submit_basket(order=order, actor=self.patient_user, delivery_address=ADDRESS,
                             confirm=True, expected_revision=order.revision)


class OperationsPortalTests(OperationsPortalFixture):
    def test_lists_are_separate_scoped_pages_with_read_only_doctor_views(self):
        self.receive()
        self.shipment(quantity=2)
        order = self.submitted()
        self.login(self.doctor)
        for name, template in (('ops-catalogue', 'operations_catalogue'), ('ops-stock', 'operations_stock'),
            ('ops-shipping', 'operations_shipping'), ('ops-history', 'operations_history'), ('ops-orders', 'operations_orders')):
            response = self.page(name)
            self.assertTemplateUsed(response, f'portal/{template}.html')
            self.assertFalse(response.context['can_edit'])
            self.assertIn('no-store', response['Cache-Control'])
            self.assertNotContains(response, self.beta_product.name)
        detail = self.page('ops-order-detail', [order.pk])
        self.assertFalse(detail.context['can_edit'])
        self.assertContains(detail, 'read-only')
        self.assertNotContains(detail, 'name="action"')
        self.assertNotContains(self.page('ops-stock'), 'name="batch_number"')

    def test_anonymous_patient_and_revoked_staff_cannot_open_operations(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('portal:ops-stock')).status_code, 302)
        self.login(self.patient_user)
        self.assertEqual(self.client.get(reverse('portal:ops-stock')).status_code, 403)
        self.login()
        CompanyMembership.objects.filter(company=self.company, user=self.admin).update(is_active=False)
        self.assertEqual(self.client.get(reverse('portal:ops-stock')).status_code, 403)

    def test_doctors_cannot_write_any_operational_endpoint(self):
        batch = self.receive()
        shipment = self.shipment(quantity=2)
        order = self.submitted()
        self.login(self.doctor)
        for name, args in (('ops-product-create', []), ('ops-product-detail', [self.product.pk]),
            ('ops-stock-receive', []), ('ops-batch-detail', [batch.pk]), ('ops-shipping-create', []),
            ('ops-shipment-detail', [shipment.pk]), ('ops-shipping-lock', []), ('ops-order-detail', [order.pk])):
            with self.subTest(name=name):
                self.assertEqual(self.client.post(reverse(f'portal:{name}', args=args), {}).status_code, 403)

    def test_foreign_product_batch_shipment_and_order_urls_are_404(self):
        batch = self.receive(company=self.beta, actor=self.beta_admin, product=self.beta_product)
        shipment = self.shipment(company=self.beta, patient=self.beta_patient, product=self.beta_product, subscription=None)
        order = PharmacyOrder.objects.create(company=self.beta, patient=self.beta_patient, status='submitted')
        for name, pk in (('ops-product-detail', self.beta_product.pk), ('ops-batch-detail', batch.pk),
                         ('ops-shipment-detail', shipment.pk), ('ops-order-detail', order.pk)):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(f'portal:{name}', args=[pk])).status_code, 404)

    def test_catalogue_mutation_is_super_admin_only_and_company_field_is_not_trusted(self):
        self.assertEqual(self.client.get(reverse('portal:ops-product-create')).status_code, 403)
        self.login(self.super_admin)
        token = self.token('ops-product-create')
        response = self.client.post(reverse('portal:ops-product-create'), self.product_data(
            workflow_context=token, company=self.beta.pk))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(MedicationProduct.objects.get(name='New catalogue product').company, self.company)

    def test_duplicate_catalogue_product_is_a_form_error_not_server_error(self):
        self.login(self.super_admin)
        response = self.client.post(reverse('portal:ops-product-create'), self.product_data(
            workflow_context=self.token('ops-product-create'), name=self.product.name, strength=self.product.strength))
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context['form'].non_field_errors())
        self.assertEqual(MedicationProduct.objects.filter(company=self.company, name=self.product.name).count(), 1)

    def test_stock_receipt_history_locks_product_identity_and_safety_rules(self):
        product = MedicationProduct.objects.create(company=self.company, name='Received original product', strength='Original', price=1)
        self.receive(product=product)
        self.login(self.super_admin)
        response = self.client.post(reverse('portal:ops-product-detail', args=[product.pk]), self.product_data(
            workflow_context=self.token('ops-product-detail', [product.pk]), name='Different medicine', strength='Different'))
        self.assertEqual(response.status_code, 400)
        product.refresh_from_db()
        self.assertEqual(product.name, 'Received original product')
        self.assertEqual(product.strength, 'Original')

    def test_receipt_creates_once_and_requires_valid_expiring_signed_context(self):
        name = 'portal:ops-stock-receive'
        token = self.token('ops-stock-receive')
        for invalid in ('', 'tampered'):
            response = self.client.post(reverse(name), self.receipt_data(workflow_context=invalid))
            self.assertEqual(response.status_code, 400)
        with patch('django.core.signing.time.time', return_value=1):
            expired = self.token('ops-stock-receive')
        response = self.client.post(reverse(name), self.receipt_data(workflow_context=expired))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(MedicationBatch.objects.count(), 0)
        first = self.client.post(reverse(name), self.receipt_data(workflow_context=token))
        second = self.client.post(reverse(name), self.receipt_data(workflow_context=token))
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.url, first.url)
        self.assertEqual(MedicationBatch.objects.count(), 1)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_stale_practice_context_never_saves_stock_in_newly_selected_practice(self):
        CompanyMembership.objects.create(company=self.beta, user=self.admin, role='practice_admin')
        token = self.token('ops-stock-receive')
        self.select(self.beta)
        response = self.client.post(reverse('portal:ops-stock-receive'), self.receipt_data(
            workflow_context=token, product=self.beta_product.pk))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['workflow_context'], token)
        self.assertFalse(MedicationBatch.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())

    def test_batch_action_needs_confirmation_and_rejects_stale_version_without_upgrading_token(self):
        batch = self.receive()
        url = reverse('portal:ops-batch-detail', args=[batch.pk])
        token = self.token('ops-batch-detail', [batch.pk])
        response = self.client.post(url, {'workflow_context': token, 'action': 'quarantine', 'reason': 'Check'})
        self.assertEqual(response.status_code, 400)
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'available')
        response = self.client.post(url, {'workflow_context': token, 'action': 'quarantine', 'reason': 'Check', 'confirm': 'on'})
        self.assertEqual(response.status_code, 302)
        response = self.client.post(url, {'workflow_context': token, 'action': 'write_off', 'reason': 'Stale', 'confirm': 'on'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['workflow_context'], token)
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'quarantined')

    def test_manual_shipment_requires_confirmation_creates_only_draft_and_deduplicates(self):
        before_users = get_user_model().objects.count()
        data = {'workflow_context': self.token('ops-shipping-create'), 'patient': self.patient.pk,
                'product': self.product.pk, 'quantity': 2, 'scheduled_for': self.today}
        url = reverse('portal:ops-shipping-create')
        self.assertEqual(self.client.post(url, data).status_code, 400)
        first = self.client.post(url, {**data, 'confirm': 'on'})
        second = self.client.post(url, {**data, 'confirm': 'on'})
        self.assertEqual(first.status_code, 302)
        self.assertEqual(first.url, second.url)
        self.assertEqual(Shipment.objects.get().status, 'draft')
        self.assertFalse(StockMovement.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(get_user_model().objects.count(), before_users)

    def test_manual_shipment_rejects_foreign_form_choices(self):
        response = self.client.post(reverse('portal:ops-shipping-create'), {
            'workflow_context': self.token('ops-shipping-create'), 'patient': self.beta_patient.pk,
            'product': self.beta_product.pk, 'quantity': 2, 'scheduled_for': self.today, 'confirm': 'on'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Shipment.objects.exists())

    def test_prepare_lock_dispatch_and_delivery_are_explicit_separate_actions(self):
        batch = self.receive()
        shipment = self.shipment()
        url = reverse('portal:ops-shipment-detail', args=[shipment.pk])
        def post_action(action, **extra):
            return self.client.post(url, {'workflow_context': self.token('ops-shipment-detail', [shipment.pk]),
                'action': action, 'confirm': 'on', **extra})
        self.assertEqual(post_action('prepare').status_code, 302)
        self.assert_stock(batch, 6)
        self.assertEqual(post_action('dispatch', tracking_number='TRACK-1').status_code, 400)
        shipping = self.page('ops-shipping', week=self.today)
        self.assertEqual(self.client.post(reverse('portal:ops-shipping-lock'), {
            'workflow_context': shipping.context['lock_context'], 'week': self.today}).status_code, 302)
        token = self.token('ops-shipment-detail', [shipment.pk])
        self.assertEqual(self.client.post(url, {'workflow_context': token, 'action': 'dispatch', 'tracking_number': 'TRACK-1'}).status_code, 400)
        self.assertEqual(post_action('dispatch', tracking_number='TRACK-1').status_code, 302)
        self.assert_stock(batch, 6)
        self.assertEqual(post_action('deliver').status_code, 302)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, 'delivered')
        self.assertFalse(Payment.objects.exists())

    def test_supply_acceptance_requires_operations_role_and_confirmation_no_payment(self):
        order = self.submitted()
        url = reverse('portal:ops-order-detail', args=[order.pk])
        token = self.token('ops-order-detail', [order.pk])
        data = {'workflow_context': token, 'action': 'accept', 'scheduled_for': self.today}
        self.assertEqual(self.client.post(url, data).status_code, 400)
        response = self.client.post(url, {**data, 'confirm': 'on'})
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, 'accepted')
        self.assertEqual(order.shipment.status, 'draft')
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(StockMovement.objects.exists())

    def test_stock_csv_is_private_scoped_formula_safe_and_head_does_not_audit(self):
        self.receive(batch_number='  =1+1')
        self.receive(company=self.beta, actor=self.beta_admin, product=self.beta_product, batch_number='FOREIGN-SECRET')
        url = reverse('portal:ops-stock-export')
        count = AuditEvent.objects.count()
        head = self.client.head(url)
        self.assertEqual(head.content, b'')
        self.assertEqual(AuditEvent.objects.count(), count)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertIn('attachment', response['Content-Disposition'])
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertTrue(rows[1][0].startswith("'"))
        self.assertNotContains(response, 'FOREIGN-SECRET')
        self.assertEqual(AuditEvent.objects.count(), count + 1)

    def test_history_export_uses_immutable_snapshot_and_escapes_patient_formula(self):
        self.receive()
        shipment = dispatch_shipment(shipment=self.locked(), actor=self.admin, tracking_number='TRACK', confirm=True)
        shipment.dispatch_snapshot['patient_name'] = ' =HYPERLINK("bad")'
        shipment.save(update_fields=('dispatch_snapshot',))
        old_product_name = self.product.name
        MedicationProduct.objects.filter(pk=self.product.pk).update(name='Changed later')
        response = self.client.get(reverse('portal:ops-history-export'))
        self.assertContains(response, old_product_name)
        self.assertNotContains(response, 'Changed later')
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(rows[1][3][0], "'")
        self.assertEqual(self.client.get(reverse('portal:ops-history-export'), {'start': 'bad'}).status_code, 400)

    def test_invalid_filters_are_validated_and_stock_pagination_reaches_page_two(self):
        for i in range(21):
            self.receive(batch_number=f'PAGE-{i:02d}')
        response = self.page('ops-stock', page=2)
        self.assertEqual(response.context['page_obj'].number, 2)
        self.assertEqual(len(response.context['batches']), 1)
        response = self.page('ops-stock', status='invalid')
        self.assertTrue(response.context['filter_form'].errors)
        self.assertEqual(response.context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(reverse('portal:ops-manifest'), {'week': 'bad'}).status_code, 400)

    def test_mutation_endpoints_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        self.login(client=client)
        response = self.page('ops-stock-receive', client=client)
        self.assertEqual(client.post(reverse('portal:ops-stock-receive'), self.receipt_data(
            workflow_context=response.context['workflow_context'])).status_code, 403)
        self.assertFalse(MedicationBatch.objects.exists())


class PatientPharmacyPortalTests(OperationsPortalFixture):
    def setUp(self):
        self.login(self.patient_user)

    def product_token(self):
        response = self.page('patient-pharmacy')
        return next(p.workflow_context for p in response.context['products'] if p.pk == self.product.pk)

    def add_item(self, **extra):
        return self.client.post(reverse('portal:patient-basket-item', args=[self.product.pk]),
            {'workflow_context': self.product_token(), 'quantity': 2, **extra})

    def test_catalogue_basket_and_order_history_are_distinct_pages_without_implicit_writes(self):
        for name, template in (('patient-pharmacy', 'pharmacy_catalogue'), ('patient-basket', 'pharmacy_basket'),
                               ('patient-orders', 'pharmacy_orders')):
            response = self.page(name)
            self.assertTemplateUsed(response, f'portal/{template}.html')
            self.assertIn('no-store', response['Cache-Control'])
            self.assertNotContains(response, self.beta_product.name)
        self.assertEqual(self.page('patient-orders').context['patient_section'], 'orders')
        self.assertFalse(PharmacyOrder.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())

    def test_staff_only_and_unrelated_patients_cannot_access_patient_orders(self):
        order = self.basket()
        self.login(self.admin)
        for name in ('patient-pharmacy', 'patient-basket', 'patient-orders'):
            self.assertEqual(self.client.get(reverse(f'portal:{name}')).status_code, 403)
        self.login(self.other_user)
        url = reverse('portal:patient-order-detail', args=[order.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {}).status_code, 404)

    def test_patient_add_submit_then_staff_accept_is_local_only(self):
        users_before = get_user_model().objects.count()
        self.assertRedirects(self.add_item(), reverse('portal:patient-basket'))
        order = PharmacyOrder.objects.get()
        basket = self.page('patient-basket')
        response = self.client.post(reverse('portal:patient-order-detail', args=[order.pk]), {
            'workflow_context': basket.context['workflow_context'], 'action': 'submit', 'confirm': 'on',
            **ADDRESS, 'note': 'Keep this private delivery note', 'user': self.admin.pk, 'company': self.beta.pk})
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, 'submitted')
        self.assertEqual(order.patient, self.patient)
        self.assertEqual(order.company, self.company)
        detail = self.page('patient-order-detail', [order.pk])
        self.assertEqual(detail.context['patient_section'], 'orders')
        self.login()
        response = self.client.post(reverse('portal:ops-order-detail', args=[order.pk]), {
            'workflow_context': self.token('ops-order-detail', [order.pk]), 'action': 'accept',
            'confirm': 'on', 'scheduled_for': self.today})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Shipment.objects.get().status, 'draft')
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(StockMovement.objects.exists())
        self.assertEqual(get_user_model().objects.count(), users_before)

    def test_quantity_repeat_is_set_once_and_bad_or_missing_context_never_writes(self):
        url = reverse('portal:patient-basket-item', args=[self.product.pk])
        for token in ('', 'invalid'):
            response = self.client.post(url, {'workflow_context': token, 'quantity': 2})
            self.assertEqual(response.status_code, 400)
        self.assertFalse(PharmacyOrder.objects.exists())
        token = self.product_token()
        for _ in range(2):
            self.assertEqual(self.client.post(url, {'workflow_context': token, 'quantity': 2}).status_code, 302)
        self.assertEqual(PharmacyOrder.objects.count(), 1)
        self.assertEqual(PharmacyOrder.objects.get().items.get().quantity, 2)
        self.assertEqual(AuditEvent.objects.filter(action='basket.updated').count(), 1)

    def test_stale_product_context_rejects_catalogue_changes_without_refreshing_failed_token(self):
        token = self.product_token()
        self.product.price = 150
        self.product.save()
        response = self.client.post(reverse('portal:patient-basket-item', args=[self.product.pk]),
                                    {'workflow_context': token, 'quantity': 2})
        self.assertEqual(response.status_code, 400)
        failed = next(p for p in response.context['products'] if p.pk == self.product.pk)
        self.assertEqual(failed.workflow_context, token)
        self.assertFalse(PharmacyOrder.objects.exists())

    def test_basket_submit_rejects_stale_contents_and_keeps_original_token_until_reload(self):
        order = self.basket()
        token = self.token('patient-basket')
        set_basket_quantity(company=self.company, patient=self.patient, actor=self.patient_user, product=self.product, quantity=3)
        response = self.client.post(reverse('portal:patient-order-detail', args=[order.pk]),
            {'workflow_context': token, 'action': 'submit', 'confirm': 'on', **ADDRESS})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['workflow_context'], token)
        order.refresh_from_db()
        self.assertEqual(order.status, 'draft')
        self.assertEqual(order.items.get().quantity, 3)

    def test_submit_retains_invalid_address_values_and_requires_confirmation(self):
        order = self.basket()
        token = self.token('patient-basket')
        response = self.client.post(reverse('portal:patient-order-detail', args=[order.pk]), {
            'workflow_context': token, 'action': 'submit', 'line1': 'Preserved draft address', 'city': 'Durban'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['form']['line1'].value(), 'Preserved draft address')
        order.refresh_from_db()
        self.assertEqual(order.status, 'draft')

    def test_patient_can_cancel_own_submitted_order_but_cannot_forge_staff_accept(self):
        order = self.submitted()
        url = reverse('portal:patient-order-detail', args=[order.pk])
        token = self.token('patient-order-detail', [order.pk])
        response = self.client.post(url, {'workflow_context': token, 'action': 'accept', 'confirm': 'on'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Shipment.objects.exists())
        self.assertEqual(self.client.post(url, {'workflow_context': token, 'action': 'cancel', 'confirm': 'on'}).status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertFalse(Payment.objects.exists())

    def test_foreign_practice_or_record_context_never_crosses_shared_sso_patient_records(self):
        order = self.basket()
        token = self.token('patient-basket')
        self.select(self.beta)
        url = reverse('portal:patient-order-detail', args=[order.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {'workflow_context': token, 'action': 'submit', **ADDRESS, 'confirm': 'on'}).status_code, 404)
        self.assertFalse(PharmacyOrder.objects.filter(company=self.beta).exists())

    def test_inactive_patient_and_csrf_cannot_write_baskets(self):
        client = Client(enforce_csrf_checks=True)
        self.login(self.patient_user, client=client)
        response = self.page('patient-pharmacy', client=client)
        product = next(p for p in response.context['products'] if p.pk == self.product.pk)
        url = reverse('portal:patient-basket-item', args=[self.product.pk])
        self.assertEqual(client.post(url, {'workflow_context': product.workflow_context, 'quantity': 2}).status_code, 403)
        Patient.objects.filter(pk=self.patient.pk).update(is_active=False)
        # The shared identity falls back to its other active patient practice;
        # the previous practice's product URL must still fail closed.
        self.assertEqual(self.client.post(url, {'workflow_context': product.workflow_context, 'quantity': 2}).status_code, 404)
        Patient.objects.filter(pk=self.beta_patient.pk).update(is_active=False)
        self.assertEqual(self.client.post(url, {'workflow_context': product.workflow_context, 'quantity': 2}).status_code, 403)
        self.assertFalse(PharmacyOrder.objects.exists())
