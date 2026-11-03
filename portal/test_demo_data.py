"""Managed demo data is explicit, scoped and reversible without touching prior data."""
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from care.models import (
    AuditEvent, EmailDelivery, Invoice, MedicationProduct, PatientSubscription,
    Payment, PurchaseOrder, Shipment, StockMovement, SubscriptionCycle, Supplier,
)
from practices.models import CompanyMembership, DemoDataSet, Patient
from .page_navigation import HTML_PAGE_ROUTES, NATIVE_GET_ROUTES
from .test_operations import OperationsPortalFixture


@override_settings(DEBUG=False, ALLOWED_HOSTS=['testserver'])
class DemoDataPortalTests(OperationsPortalFixture):
    """Use the real generator and deletion service against Django's isolated test DB."""

    def setUp(self):
        super().setUp()
        self.login(self.super_admin)

    def panel(self, client=None, **params):
        return self.page('demo-data', client=client, **params)

    def action_data(self, action, *, client=None, **params):
        response = self.panel(client=client, **params)
        item = next(item for item in response.context['demo_actions'] if item['key'] == action)
        return {'workflow_context': item['workflow_context'], 'dataset': item['dataset_id'],
                'confirm': 'yes', 'next': response.context['demo_return_url']}

    def post_action(self, action, data, client=None):
        return (client or self.client).post(reverse(f'portal:demo-data-{action}'), data)

    def create(self):
        self.assertEqual(self.post_action('create', self.action_data('create')).status_code, 302)
        return DemoDataSet.objects.get(company=self.company, status='active')

    def snapshot(self, models):
        return {model: list(model.objects.order_by('pk').values()) for model in models}

    def assert_snapshot(self, original):
        for model, rows in original.items():
            with self.subTest(model=model._meta.label):
                self.assertEqual(list(model.objects.order_by('pk').values()), rows)

    def test_get_fragment_and_full_page_are_read_only_and_native(self):
        before = self.snapshot((DemoDataSet, Patient, MedicationProduct, AuditEvent))
        full = self.panel(next='/inventory/?q=Demo')
        self.assertTemplateUsed(full, 'portal/demo_data.html')
        self.assertEqual(full.context['demo_return_url'], '/inventory/?q=Demo')
        self.assertFalse(full.context['demo_status']['active'])
        self.assertEqual([a['key'] for a in full.context['demo_actions']], ['create'])
        fragment = self.panel(fragment='1')
        self.assertTemplateUsed(fragment, 'portal/includes/demo_data_content.html')
        self.assertTemplateNotUsed(fragment, 'portal/demo_data.html')
        self.assertEqual(fragment['X-Demo-Data-Fragment'], '1')
        self.assertIn('no-store', fragment['Cache-Control'])
        self.assertContains(fragment, 'data-demo-data-panel')
        self.assertIn('demo-data', NATIVE_GET_ROUTES)
        self.assertNotIn('demo-data', HTML_PAGE_ROUTES)
        self.assert_snapshot(before)
        for action in ('create', 'clear', 'rebuild'):
            self.assertEqual(self.client.get(reverse(f'portal:demo-data-{action}')).status_code, 405)
        self.assertEqual(self.client.post(reverse('portal:demo-data')).status_code, 405)

    def test_demo_shipping_controls_never_book_or_dispatch_other_parcels_in_same_week(self):
        dataset = self.create()
        week = self.today - timedelta(days=self.today.weekday())
        live_waiting = self.shipment(status='ready', locked_at=timezone.now())
        live_booked = self.shipment(status='ready', locked_at=timezone.now(), waybill='LIVE-COLLECTION')
        original_shipments = list(Shipment.objects.filter(pk__in=[live_waiting.pk, live_booked.pk]).order_by('pk').values())
        original_stock = list(StockMovement.objects.filter(product__demo_data_set__isnull=True).order_by('pk').values())
        confirmed = self.page('ops-shipping', week=week.isoformat(), shipping_step='confirmed')
        self.assertGreater(confirmed.context['to_book_demo'], 0)
        self.assertContains(confirmed, 'Simulate demo courier')
        with patch('care.courier._call') as transport:
            response = self.client.post(reverse('portal:ops-shipping-courier'), {
                'week': week.isoformat(), 'workflow_context': confirmed.context['courier_context'], 'demo_only': '1',
            })
        self.assertEqual(response.status_code, 302)
        transport.assert_not_called()
        tracking = self.page('ops-shipping', week=week.isoformat(), shipping_step='tracking', collect='1', collect_demo='1')
        self.assertTrue(tracking.context['collect_demo_mode'])
        self.assertEqual(tracking.context['collect_count'], tracking.context['to_collect_demo'])
        self.assertTrue(all(product.demo_data_set_id == dataset.pk for product in tracking.context['collect_products']))
        self.assertContains(tracking, 'Simulate collection of these demo parcels only')
        response = self.client.post(reverse('portal:ops-shipping-collect'), {
            'week': week.isoformat(), 'workflow_context': tracking.context['collect_context'], 'demo_only': '1',
            'confirm': 'on', 'stock_override': 'on', 'stock_override_reason': 'Demonstration collection only',
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Shipment.objects.filter(patient__demo_data_set=dataset, scheduled_for__gte=week,
            scheduled_for__lt=week+timedelta(days=7), status='ready').exists())
        self.assertEqual(list(Shipment.objects.filter(pk__in=[live_waiting.pk, live_booked.pk]).order_by('pk').values()), original_shipments)
        self.assertEqual(list(StockMovement.objects.filter(product__demo_data_set__isnull=True).order_by('pk').values()), original_stock)

    def test_create_in_production_is_explicit_and_idempotent_without_accounts_or_financial_records(self):
        protected = self.snapshot((get_user_model(), CompanyMembership, Invoice, Payment,
                                   SubscriptionCycle, EmailDelivery))
        old_patients = list(Patient.objects.values_list('pk', flat=True))
        data = self.action_data('create', next='/inventory/?q=Demo')
        first = self.post_action('create', data)
        self.assertRedirects(first, '/inventory/?q=Demo', fetch_redirect_response=False)
        dataset = DemoDataSet.objects.get(company=self.company, status='active')
        counts = self.panel().context['demo_status']['counts']
        self.assertGreater(counts['patients'], 0)
        self.assertGreater(counts['products'], 0)
        self.assertGreater(counts['purchase_orders'], 0)
        self.assertGreater(counts['shipments'], 0)
        self.assertEqual(Patient.objects.filter(demo_data_set=dataset).count(), counts['patients'])
        self.assertFalse(Patient.objects.filter(demo_data_set=dataset, user__isnull=False).exists())
        self.assertFalse(Patient.objects.filter(pk__in=old_patients, demo_data_set__isnull=False).exists())
        after_first = self.snapshot((DemoDataSet, Patient, MedicationProduct, Supplier, PurchaseOrder,
                                     Shipment, StockMovement, AuditEvent))
        self.assertEqual(self.post_action('create', data).status_code, 302)
        self.assert_snapshot(after_first)
        self.assert_snapshot(protected)
        self.assertEqual(DemoDataSet.objects.filter(company=self.beta).count(), 0)

    def test_delete_only_removes_owned_records_and_retains_audit_and_prior_demo_named_records(self):
        # Names never grant ownership: earlier hand-entered demo records must remain.
        manual_patient = Patient.objects.create(company=self.company, first_name='Manual', last_name='Demo')
        manual_product = MedicationProduct.objects.create(company=self.company, name='Demo old stock', price=1)
        supplier = Supplier.objects.create(company=self.company, name='Demo old supplier')
        PurchaseOrder.objects.create(company=self.company, number=1, created_by=self.super_admin,
                                     supplier=supplier, supplier_name=supplier.name, submission_key=uuid4())
        StockMovement.objects.create(company=self.company, product=manual_product, quantity=7, actor=self.super_admin)
        EmailDelivery.objects.create(company=self.company, patient=manual_patient,
                                     recipient_email='unchanged@example.test', subject='Existing email')
        old = self.snapshot((Patient, MedicationProduct, Supplier, PurchaseOrder, Shipment, StockMovement,
                             PatientSubscription, EmailDelivery, get_user_model()))
        dataset = self.create()
        token = self.action_data('clear')
        self.assertEqual(self.post_action('clear', token).status_code, 302)
        dataset.refresh_from_db()
        self.assertEqual(dataset.status, 'cleared')
        self.assertEqual(dataset.cleared_by, self.super_admin)
        self.assertIsNotNone(dataset.cleared_at)
        self.assert_snapshot(old)
        self.assertFalse(self.panel().context['demo_status']['active'])
        self.assertEqual(AuditEvent.objects.filter(company=self.company, action='demo_data.cleared').count(), 1)
        self.assertEqual(self.post_action('clear', token).status_code, 302)
        self.assert_snapshot(old)
        self.assertEqual(AuditEvent.objects.filter(company=self.company, action='demo_data.cleared').count(), 1)

    def test_rebuild_replaces_only_current_dataset_and_replay_does_not_rebuild_twice(self):
        old = self.create()
        counts = self.panel().context['demo_status']['counts']
        old_clear = self.action_data('clear')
        data = self.action_data('rebuild', next='/purchasing/?tab=on_order')
        self.assertRedirects(self.post_action('rebuild', data), '/purchasing/?tab=on_order', fetch_redirect_response=False)
        active = DemoDataSet.objects.get(company=self.company, status='active')
        old.refresh_from_db()
        self.assertNotEqual(active.pk, old.pk)
        self.assertEqual(old.status, 'cleared')
        self.assertFalse(Patient.objects.filter(demo_data_set=old).exists())
        self.assertEqual(self.panel().context['demo_status']['counts'], counts)
        after = self.snapshot((DemoDataSet, Patient, MedicationProduct, Supplier, Shipment, StockMovement, AuditEvent))
        self.assertEqual(self.post_action('rebuild', data).status_code, 302)
        self.assert_snapshot(after)
        # A different, stale confirmation for the replaced set must not delete the new one.
        self.assertEqual(self.post_action('clear', old_clear).status_code, 400)
        self.assert_snapshot(after)

    def test_delete_and_rebuild_require_confirmation_and_current_signed_version(self):
        dataset = self.create()
        tokens = {action: self.action_data(action) for action in ('clear', 'rebuild')}
        before = self.snapshot((DemoDataSet, Patient, MedicationProduct, StockMovement))
        for action, data in tokens.items():
            for value in ('', 'no', 'on'):
                with self.subTest(action=action, confirm=value):
                    self.assertEqual(self.post_action(action, {**data, 'confirm': value}).status_code, 400)
        self.assert_snapshot(before)
        DemoDataSet.objects.filter(pk=dataset.pk).update(updated_at=timezone.now()+timedelta(seconds=3))
        changed = self.snapshot((DemoDataSet, Patient, MedicationProduct, StockMovement))
        for action, data in tokens.items():
            response = self.post_action(action, data)
            self.assertContains(response, 'changed', status_code=400)
        self.assert_snapshot(changed)

    def test_signatures_expire_and_cannot_change_action_or_actor(self):
        data = self.action_data('create')
        for token in ('', 'tampered'):
            self.assertEqual(self.post_action('create', {**data, 'workflow_context': token}).status_code, 400)
        with patch('django.core.signing.time.time', return_value=1):
            expired = self.action_data('create')
        self.assertEqual(self.post_action('create', expired).status_code, 400)
        CompanyMembership.objects.create(company=self.company, user=self.other_user, role='super_admin')
        self.login(self.other_user)
        self.assertEqual(self.post_action('create', data).status_code, 400)
        self.assertFalse(DemoDataSet.objects.exists())
        self.login(self.super_admin)
        dataset = self.create()
        clear = self.action_data('clear')
        self.assertEqual(self.post_action('rebuild', clear).status_code, 400)
        self.assertTrue(DemoDataSet.objects.filter(pk=dataset.pk, status='active').exists())

    def test_company_switch_and_foreign_dataset_cannot_redirect_mutations(self):
        token = self.action_data('create')
        CompanyMembership.objects.create(company=self.beta, user=self.super_admin, role='super_admin')
        self.select(self.beta)
        self.assertEqual(self.post_action('create', token).status_code, 400)
        self.assertFalse(DemoDataSet.objects.exists())
        foreign = DemoDataSet.objects.create(company=self.beta, created_by=self.super_admin)
        self.select(self.company)
        own = self.create()
        data = self.action_data('clear')
        self.assertEqual(self.post_action('clear', {**data, 'dataset': str(foreign.pk)}).status_code, 404)
        self.assertEqual(self.post_action('clear', {**data, 'dataset': 'not-a-uuid'}).status_code, 400)
        self.assertEqual(DemoDataSet.objects.filter(status='active').count(), 2)
        self.assertEqual(self.panel().context['demo_status']['dataset'], own)

    def test_only_current_super_admins_can_read_or_write_including_native_requests(self):
        token = self.action_data('create')
        url = reverse('portal:demo-data')
        for actor in (self.admin, self.doctor, self.patient_user):
            self.login(actor)
            for params in ({}, {'fragment': '1'}, {'__shell': '1'}):
                with self.subTest(actor=actor.pk, params=params):
                    self.assertEqual(self.client.get(url, params).status_code, 403)
            for action in ('create', 'clear', 'rebuild'):
                self.assertEqual(self.post_action(action, token).status_code, 403)
        self.login(self.super_admin)
        CompanyMembership.objects.filter(company=self.company, user=self.super_admin).update(is_active=False)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.post_action('create', token).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)
        self.assertEqual(self.post_action('create', token).status_code, 302)
        self.assertFalse(DemoDataSet.objects.exists())

    def test_csrf_is_required_even_with_a_valid_signed_intent(self):
        client = Client(enforce_csrf_checks=True)
        self.login(self.super_admin, client=client)
        data = self.action_data('create', client=client)
        self.assertEqual(self.post_action('create', data, client=client).status_code, 403)
        self.assertFalse(DemoDataSet.objects.exists())
        data['csrfmiddlewaretoken'] = client.cookies['csrftoken'].value
        self.assertEqual(self.post_action('create', data, client=client).status_code, 302)
        clear = self.action_data('clear', client=client)
        self.assertEqual(self.post_action('clear', clear, client=client).status_code, 403)
        self.assertTrue(DemoDataSet.objects.filter(status='active').exists())

    def test_return_url_is_same_origin_and_falls_back_for_unsafe_or_recursive_urls(self):
        fallback = reverse('portal:desktop-dashboard')
        for target in ('https://outside.example/inventory/', '//outside.example/inventory/',
                       'javascript:alert(1)', '/demo-data/?create=1', 'relative-path', '/\\outside.example/',
                       'x'*2049):
            with self.subTest(target=target[:80]):
                self.assertEqual(self.panel(next=target).context['demo_return_url'], fallback)
        self.assertEqual(self.panel(next='http://testserver/inventory/?q=Demo#top').context['demo_return_url'],
                         '/inventory/?q=Demo')
        data = self.action_data('create')
        self.assertRedirects(self.post_action('create', {**data, 'next': '//outside.example/'}),
                             fallback, fetch_redirect_response=False)

    def test_unsupported_financial_dependency_is_reported_without_partial_deletion(self):
        dataset = self.create()
        patient = Patient.objects.filter(demo_data_set=dataset).first()
        Invoice.objects.create(company=self.company, patient=patient, invoice_number='PRESERVE-THIS',
                               due_on=self.today, subtotal=10, total=10)
        before = self.snapshot((DemoDataSet, Patient, MedicationProduct, Supplier, PurchaseOrder,
                                Shipment, StockMovement, Invoice, AuditEvent))
        for action in ('clear', 'rebuild'):
            response = self.post_action(action, self.action_data(action))
            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.context['demo_status']['active'])
            self.assertTrue(response.context['demo_error'])
            self.assertContains(response, 'Nothing was removed', status_code=400)
            self.assert_snapshot(before)

    def test_old_create_form_cannot_duplicate_an_active_dataset(self):
        old_create = self.action_data('create')
        self.create()
        before = self.snapshot((DemoDataSet, Patient, MedicationProduct, StockMovement))
        response = self.post_action('create', old_create)
        self.assertContains(response, 'already exists', status_code=400)
        self.assertEqual([a['key'] for a in response.context['demo_actions']], ['clear', 'rebuild'])
        self.assert_snapshot(before)
