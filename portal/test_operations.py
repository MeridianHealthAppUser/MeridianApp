"""Dedicated operations pages enforce signed intent, CSRF and scoped access."""

import csv
import io
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from openpyxl import load_workbook

from care.models import AuditEvent, MedicationProduct, Payment, PharmacyOrder, PracticeSettings, Shipment, ShipmentItem
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

    def create_token(self, client=None):
        """The Create shipment dialog's, on the Weekly shipping list."""
        return self.page('ops-shipping', client=client).context['create_context']

    def shipment_data(self, **extra):
        return {'patient': self.patient.pk, 'product': self.product.pk, 'quantity': 2, 'scheduled_for': self.today,
                'confirm': 'on', **extra}

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
    def page(self, name, args=None, client=None, **params):
        # General list tests explicitly request the whole week; stage queues have separate coverage.
        if name == 'ops-shipping':
            params.setdefault('shipping_step', 'all')
        return super().page(name, args=args, client=client, **params)

    def test_lists_are_separate_scoped_pages_and_doctors_cannot_open_them(self):
        shipment = self.shipment(quantity=2)
        order = self.submitted()
        for name, template in (('ops-catalogue', 'operations_catalogue'),
            ('ops-shipping', 'operations_shipping'), ('ops-history', 'operations_history'), ('ops-orders', 'operations_orders')):
            response = self.page(name)
            self.assertTemplateUsed(response, f'portal/{template}.html')
            self.assertIn('no-store', response['Cache-Control'])
            self.assertNotContains(response, self.beta_product.name)
        # Operations is for practice admins and Super Admins; the Clinician role can't read it either.
        self.login(self.doctor)
        for name, args in (('ops-catalogue', []), ('ops-shipping', []), ('ops-history', []), ('ops-orders', []),
            ('ops-product-detail', [self.product.pk]), ('ops-shipment-detail', [shipment.pk]),
            ('ops-order-detail', [order.pk]), ('ops-week-order', []), ('ops-history-export', [])):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(f'portal:{name}', args=args)).status_code, 403)

    def test_anonymous_patient_and_revoked_staff_cannot_open_operations(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('portal:ops-shipping')).status_code, 302)
        self.login(self.patient_user)
        self.assertEqual(self.client.get(reverse('portal:ops-shipping')).status_code, 403)
        self.login()
        CompanyMembership.objects.filter(company=self.company, user=self.admin).update(is_active=False)
        self.assertEqual(self.client.get(reverse('portal:ops-shipping')).status_code, 403)

    def test_doctors_cannot_write_any_operational_endpoint(self):
        shipment = self.shipment(quantity=2)
        order = self.submitted()
        self.login(self.doctor)
        for name, args in (('ops-product-create', []), ('ops-product-detail', [self.product.pk]), ('ops-shipping-create', []),
            ('ops-shipment-detail', [shipment.pk]), ('ops-shipping-lock', []), ('ops-order-detail', [order.pk])):
            with self.subTest(name=name):
                self.assertEqual(self.client.post(reverse(f'portal:{name}', args=args), {}).status_code, 403)

    def test_foreign_product_shipment_and_order_urls_are_404(self):
        shipment = self.shipment(company=self.beta, patient=self.beta_patient, product=self.beta_product, subscription=None)
        order = PharmacyOrder.objects.create(company=self.beta, patient=self.beta_patient, status='submitted')
        for name, pk in (('ops-product-detail', self.beta_product.pk), ('ops-shipment-detail', shipment.pk), ('ops-order-detail', order.pk)):
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

    def test_shipment_history_locks_product_identity_and_safety_rules(self):
        product = MedicationProduct.objects.create(company=self.company, name='Received original product', strength='Original', price=1)
        self.shipment(product=product, quantity=1)
        self.login(self.super_admin)
        response = self.client.post(reverse('portal:ops-product-detail', args=[product.pk]), self.product_data(
            workflow_context=self.token('ops-product-detail', [product.pk]), name='Different medicine', strength='Different'))
        self.assertEqual(response.status_code, 400)
        product.refresh_from_db()
        self.assertEqual(product.name, 'Received original product')
        self.assertEqual(product.strength, 'Original')

    def test_shipment_creation_requires_a_valid_expiring_signed_context(self):
        url = reverse('portal:ops-shipping-create')
        for invalid in ('', 'tampered'):
            self.assertEqual(self.client.post(url, self.shipment_data(workflow_context=invalid)).status_code, 400)
        with patch('django.core.signing.time.time', return_value=1):
            expired = self.create_token()
        self.assertEqual(self.client.post(url, self.shipment_data(workflow_context=expired)).status_code, 400)
        self.assertFalse(Shipment.objects.exists())

    def test_stale_practice_context_never_creates_a_shipment_in_newly_selected_practice(self):
        CompanyMembership.objects.create(company=self.beta, user=self.admin, role='practice_admin')
        token = self.create_token()
        self.select(self.beta)
        response = self.client.post(reverse('portal:ops-shipping-create'), self.shipment_data(
            workflow_context=token, patient=self.beta_patient.pk, product=self.beta_product.pk))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Shipment.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())

    def test_shipment_action_rejects_a_stale_version_without_upgrading_token(self):
        shipment = self.shipment()
        url = reverse('portal:ops-shipment-detail', args=[shipment.pk])
        token = self.token('ops-shipment-detail', [shipment.pk])
        response = self.client.post(url, {'workflow_context': token, 'action': 'hold', 'reason': 'Check'})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(url, {'workflow_context': token, 'action': 'hold', 'reason': 'Check', 'confirm': 'on'})
        self.assertEqual(response.status_code, 302)
        response = self.client.post(url, {'workflow_context': token, 'action': 'cancel', 'reason': 'Stale', 'confirm': 'on'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['workflow_context'], token)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, 'held')

    def test_manual_shipment_requires_confirmation_creates_only_draft_and_deduplicates(self):
        before_users = get_user_model().objects.count()
        data = {'workflow_context': self.create_token(), 'patient': self.patient.pk,
                'product': self.product.pk, 'quantity': 2, 'scheduled_for': self.today}
        url = reverse('portal:ops-shipping-create')
        # Create shipment is a dialog on the Weekly shipping list; a mistake reopens it there.
        self.assertRedirects(self.client.get(url), f"{reverse('portal:ops-shipping')}?create=1", fetch_redirect_response=False)
        mistake = self.client.post(url, data)
        self.assertEqual(mistake.status_code, 400)
        self.assertTemplateUsed(mistake, 'portal/operations_shipping.html')
        self.assertContains(mistake, '<dialog class="app-dialog" id="create-shipment" aria-labelledby="create-shipment-title" open>', status_code=400)
        first = self.client.post(url, {**data, 'confirm': 'on'})
        second = self.client.post(url, {**data, 'confirm': 'on'})
        self.assertEqual(first.status_code, 302)
        self.assertEqual(first.url, second.url)
        self.assertEqual(Shipment.objects.get().status, 'draft')
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(get_user_model().objects.count(), before_users)

    def test_manual_shipment_rejects_foreign_form_choices(self):
        response = self.client.post(reverse('portal:ops-shipping-create'), {
            'workflow_context': self.create_token(), 'patient': self.beta_patient.pk,
            'product': self.beta_product.pk, 'quantity': 2, 'scheduled_for': self.today, 'confirm': 'on'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Shipment.objects.exists())

    def test_confirmation_dispatch_and_delivery_are_explicit_separate_actions(self):
        shipment = self.shipment()
        item = shipment.items.get()
        url = reverse('portal:ops-shipment-detail', args=[shipment.pk])
        def post_action(action, **extra):
            return self.client.post(url, {'workflow_context': self.token('ops-shipment-detail', [shipment.pk]),
                'action': action, 'confirm': 'on', **extra})
        self.assertEqual(post_action('dispatch', tracking_number='TRACK-1').status_code, 400)
        self.assertEqual(post_action('confirm').status_code, 302)
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, Shipment.Status.READY)
        self.assertIsNotNone(shipment.locked_at)
        token = self.token('ops-shipment-detail', [shipment.pk])
        self.assertEqual(self.client.post(url, {'workflow_context': token, 'action': 'dispatch', 'tracking_number': 'TRACK-1'}).status_code, 400)
        # The parcel's batch and expiry are noted with the dispatch.
        self.assertContains(self.page('ops-shipment-detail', [shipment.pk]), f'name="batch_{item.pk}"')
        self.assertEqual(post_action('dispatch', tracking_number='TRACK-1', **{f'batch_{item.pk}': 'SG-2604, expiry 04/2027'}).status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.batch_note, 'SG-2604, expiry 04/2027')
        self.assertContains(self.page('ops-shipment-detail', [shipment.pk]), 'SG-2604, expiry 04/2027')
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

    def test_the_list_confirms_and_locks_selected_pending_parcels(self):
        week = self.today - timedelta(days=self.today.weekday())
        draft = self.shipment(quantity=2, scheduled_for=week)
        page = self.page('ops-shipping', week=week, shipping_step='pending')
        self.assertContains(page, f'{self.product.name} <span class="ops-dose">{self.auth.max_dose}</span>', html=False)
        self.assertContains(page, '<td data-label="Qty"><div>2</div></td>', html=False)
        response = self.client.post(reverse('portal:ops-shipping-confirm'), {
            'workflow_context': page.context['confirm_context'], 'week': week, 'shipment_ids': [draft.pk]})
        self.assertRedirects(response, f"{reverse('portal:ops-shipping')}?week={week}&shipping_step=pending", fetch_redirect_response=False)
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'ready')
        self.assertIsNotNone(draft.locked_at)
        self.assertContains(self.page('ops-shipping', week=week), '1 parcel confirmed and locked.')
        self.login(self.doctor)
        self.assertEqual(self.client.post(reverse('portal:ops-shipping-confirm'), {'week': week}).status_code, 403)

    def test_the_list_opens_on_the_week_of_the_next_shipping_day(self):
        from datetime import date

        PracticeSettings.objects.update_or_create(company=self.company, defaults={'shipping_weekday': 1})  # Tuesday
        # Sunday 25 Oct 2026: the next shipping day is Tuesday 27 Oct, in next week. Tuesday itself is still its week.
        for today, monday, label in ((date(2026, 10, 25), date(2026, 10, 26), 'Next week'),
                                     (date(2026, 10, 27), date(2026, 10, 26), 'This week')):
            with self.subTest(today=today), patch('django.utils.timezone.localdate', return_value=today):
                page = self.page('ops-shipping')
                self.assertEqual(page.context['week_start'], monday)
                self.assertEqual(page.context['filter_form'].cleaned_data['week'], monday)
                other = self.page('ops-shipping', week=monday + timedelta(days=7))
                self.assertEqual((other.context['this_week_url'].endswith(f'week={monday}'), other.context['this_week_label']),
                                 (True, label))

    def test_shipping_list_searches_and_filters_by_several_statuses_from_the_filters_menu(self):
        week = self.today - timedelta(days=self.today.weekday())
        draft = self.shipment(quantity=1, scheduled_for=week)
        ready = self.ready(self.shipment(quantity=1, scheduled_for=week, tracking_number='TRACK-123'))
        page = self.page('ops-shipping', week=week)
        self.assertNotContains(page, 'Show week')
        # The week is chosen with the arrows beside its heading; the Filters menu keeps it.
        self.assertEqual([section['label'] for section in page.context['filter_menu']['sections']], ['Status', 'Product', 'Doctor', 'Payment'])
        self.assertIn(('week', week.isoformat()), page.context['filter_menu']['hidden'])
        self.assertEqual(page.context['search_hidden']['week'], week.isoformat())
        self.assertContains(page, 'data-conversation-search')
        listed = lambda **query: {row.pk for row in self.page('ops-shipping', week=week, **query).context['shipments']}
        self.assertEqual(listed(status=['draft', 'ready']), {draft.pk, ready.pk})
        self.assertEqual(listed(status='ready'), {ready.pk})
        self.assertEqual(listed(q='TRACK-123'), {ready.pk})
        self.assertEqual(listed(q=self.patient.last_name), {draft.pk, ready.pk})
        # Older links that say status=all still mean every status.
        self.assertEqual(listed(status='all'), {draft.pk, ready.pk})

    def test_each_pending_line_has_its_own_bound_confirm_and_lock_control(self):
        week = self.today - timedelta(days=self.today.weekday())
        draft = self.shipment(quantity=1, scheduled_for=week)
        failing = self.shipment(quantity=1, scheduled_for=week)
        failing.items.update(dose='Not the authorised dose')
        locked = self.locked(self.shipment(quantity=1, scheduled_for=week))
        ready = self.ready(self.shipment(quantity=1, scheduled_for=week))
        page = self.page('ops-shipping', week=week, status='all')
        rows = {shipment.pk: shipment for shipment in page.context['shipments']}
        self.assertTrue(rows[draft.pk].confirm_context)
        self.assertTrue(rows[ready.pk].confirm_context)
        self.assertFalse(hasattr(rows[locked.pk], 'confirm_context'))
        self.assertContains(page, reverse('portal:ops-shipping-confirm'))
        self.assertContains(page, 'name="single_shipment"')
        url = reverse('portal:ops-shipping-confirm')
        data = {'workflow_context': rows[draft.pk].confirm_context, 'single_shipment': str(draft.pk),
                'shipment_ids': [draft.pk], 'week': week,
                'list_query': f'week={week}&status=all&next=https://evil.test'}
        response = self.client.post(url, data)
        # Return to Pending in the same week and filters; never follow an unrelated external next value.
        self.assertRedirects(response, f"{reverse('portal:ops-shipping')}?week={week}&status=all&shipping_step=pending", fetch_redirect_response=False)
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'ready')
        self.assertIsNotNone(draft.locked_at)
        # The same record-bound submission becomes stale after the successful confirmation.
        self.client.post(url, data)
        self.assertEqual(AuditEvent.objects.filter(action='shipment.locked', target_id=str(draft.pk)).count(), 1)
        self.client.post(url, {'workflow_context': rows[failing.pk].confirm_context,
                              'single_shipment': str(failing.pk), 'shipment_ids': [failing.pk], 'week': week})
        failing.refresh_from_db()
        self.assertEqual(failing.status, 'draft')
        self.assertIsNone(failing.locked_at)
        self.assertContains(self.page('ops-shipping', week=week), 'The recorded dose does not match')
        self.login(self.doctor)
        self.assertEqual(self.client.post(url, data).status_code, 403)

    def test_a_parcel_is_changed_from_its_page_until_locked(self):
        shipment = self.shipment(quantity=2)
        item = shipment.items.get()
        page = self.page('ops-shipment-detail', [shipment.pk])
        self.assertContains(page, 'Change this parcel')
        url = reverse('portal:ops-shipment-change', args=[shipment.pk])
        data = {'workflow_context': page.context['change_context'], 'scheduled_for': self.today + timedelta(days=2), f'pens_{item.pk}': 1}
        self.assertEqual(self.client.post(url, {**data, f'pens_{item.pk}': 0}).status_code, 400)
        self.assertRedirects(self.client.post(url, data), reverse('portal:ops-shipment-detail', args=[shipment.pk]),
                             fetch_redirect_response=False)
        shipment.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual((shipment.scheduled_for, item.quantity), (self.today + timedelta(days=2), 1))
        # The action form keeps working after a change: each form has its own token.
        self.assertEqual(self.client.post(reverse('portal:ops-shipment-detail', args=[shipment.pk]), {
            'workflow_context': self.token('ops-shipment-detail', [shipment.pk]), 'action': 'confirm', 'confirm': 'on'}).status_code, 302)
        shipment.refresh_from_db()
        self.assertIsNotNone(shipment.locked_at)
        self.assertNotContains(self.page('ops-shipment-detail', [shipment.pk]), 'Change this parcel')

    def test_the_weeks_pens_to_order_are_listed_and_exported_privately_and_formula_safe(self):
        week = self.today - timedelta(days=self.today.weekday())
        MedicationProduct.objects.filter(pk=self.product.pk).update(name='=1+1')
        self.ready(self.shipment(quantity=3, scheduled_for=week))
        self.shipment(quantity=1, scheduled_for=week)
        foreign = self.shipment(company=self.beta, patient=self.beta_patient, product=self.beta_product, subscription=None,
                                scheduled_for=week, status='ready')
        ShipmentItem.objects.filter(shipment=foreign).update(quantity=9)
        # What to order is on Weekly shipping, under the week's parcels (Dispatch stock was its own page until
        # 24 Oct 2026; its address opens this one).
        self.assertRedirects(self.client.get(reverse('portal:ops-stock'), {'week': week}),
                             reverse('portal:inventory'), fetch_redirect_response=False)
        page = self.page('ops-shipping', week=week)
        self.assertEqual([(line['product'].pk, line['pens']) for line in page.context['week_order']['lines']], [(self.product.pk, 3)])
        self.assertNotContains(page, 'Pens this week')
        self.assertNotContains(page, 'Order pens')
        url = reverse('portal:ops-week-order')
        PracticeSettings.objects.update_or_create(company=self.company, defaults={'store_view_log': True})
        count = AuditEvent.objects.count()
        self.assertEqual(self.client.head(url, {'week': week}).content, b'')
        self.assertEqual(AuditEvent.objects.count(), count)
        response = self.client.get(url, {'week': week})
        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('attachment', response['Content-Disposition'])
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(rows, [['Medicine', 'Pen', 'Pens'], ["'=1+1", 'Dose A', '3']])
        self.assertNotContains(response, self.beta_product.name)
        self.assertEqual(AuditEvent.objects.count(), count + 1)
        self.assertEqual(self.client.get(url, {'week': 'bad'}).status_code, 400)

    def test_history_export_uses_immutable_snapshot_and_literal_patient_formula(self):
        shipment = dispatch_shipment(shipment=self.locked(), actor=self.admin, tracking_number='TRACK', confirm=True)
        shipment.dispatch_snapshot['patient_name'] = ' =HYPERLINK("bad")'
        shipment.save(update_fields=('dispatch_snapshot',))
        old_product_name = self.product.name
        MedicationProduct.objects.filter(pk=self.product.pk).update(name='Changed later')
        response = self.client.get(reverse('portal:ops-history-export'))
        self.assertEqual(response['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        content = b''.join(response.streaming_content)
        workbook = load_workbook(io.BytesIO(content))
        self.addCleanup(workbook.close)
        headers = [cell.value for cell in workbook.active[1]]
        row = dict(zip(headers, workbook.active[2]))
        self.assertEqual(row['Product'].value, old_product_name + ' ' + self.product.strength)
        self.assertEqual(row['Patient'].value, ' =HYPERLINK("bad")')
        self.assertEqual(row['Patient'].data_type, 's')
        self.assertEqual(self.client.get(reverse('portal:ops-history-export'), {'start_month': 'bad'}).status_code, 400)

    def test_invalid_filters_are_validated_and_shipping_pagination_reaches_page_two(self):
        week = self.today - timedelta(days=self.today.weekday())
        # A week shows 100 parcels a page.
        for _ in range(101):
            self.shipment(quantity=1, scheduled_for=week)
        response = self.page('ops-shipping', week=week, page=2)
        self.assertEqual(response.context['page_obj'].number, 2)
        self.assertEqual(len(response.context['shipments']), 1)
        response = self.page('ops-shipping', status='invalid')
        self.assertTrue(response.context['filter_form'].errors)
        self.assertEqual(response.context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(reverse('portal:ops-manifest'), {'week': 'bad'}).status_code, 400)

    def test_mutation_endpoints_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        self.login(client=client)
        self.assertEqual(client.post(reverse('portal:ops-shipping-create'), self.shipment_data(
            workflow_context=self.create_token(client))).status_code, 403)
        self.assertFalse(Shipment.objects.exists())


@override_settings(PAYMENT_PROVIDER='sandbox')
class PatientShopPortalTests(OperationsPortalFixture):
    """Once-off items on My Treatment: basket, place and pay by card, then the next shipping day's list."""

    def setUp(self):
        self.login(self.patient_user)

    def product_token(self, product=None):
        product = product or self.open_product
        response = self.page('patient-treatment')
        return next(p.workflow_context for p in response.context['shop_products'] if p.pk == product.pk)

    def add_item(self, quantity=2, **extra):
        return self.client.post(reverse('portal:patient-basket-item', args=[self.open_product.pk]),
                                {'workflow_context': self.product_token(), 'quantity': quantity, **extra})

    def signed(self, kind, record):
        from django.test import RequestFactory
        from .workflow_context import make_workflow_context

        request = RequestFactory().post('/')
        request.user = self.patient_user
        return make_workflow_context(request, self.company, kind, record, patient=self.patient)

    def place(self, **extra):
        basket = self.page('patient-basket')
        return self.client.post(reverse('portal:patient-order-detail', args=[basket.context['order'].pk]),
                                {'workflow_context': basket.context['workflow_context'], 'action': 'submit', **extra})

    def test_my_treatment_lists_once_off_items_below_previously_prescribed_without_writing(self):
        response = self.page('patient-treatment')
        self.assertEqual(response.context['shop_products'], [self.open_product])
        self.assertContains(response, 'Once-off items')
        self.assertContains(response, 'Add to basket')
        self.assertContains(response, 'Basket · 0')
        self.assertNotContains(response, self.beta_product.name)
        self.assertNotContains(response, 'My orders')
        self.assertFalse(PharmacyOrder.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(action__startswith='basket').exists())
        # Inactive or unpriced items aren't offered.
        MedicationProduct.objects.filter(pk=self.open_product.pk).update(price=0)
        self.assertEqual(self.page('patient-treatment').context['shop_products'], [])

    def test_add_place_and_pay_puts_the_order_on_the_next_shipping_list(self):
        from care.billing import parcel_payment
        from care.parcels import shipping_day_from, shipping_weekday

        self.assertRedirects(self.add_item(note='ignored'), reverse('portal:patient-basket'), fetch_redirect_response=False)
        order = PharmacyOrder.objects.get()
        self.assertContains(self.page('patient-treatment'), 'Basket · 2')
        basket = self.page('patient-basket')
        self.assertContains(basket, '1 Test Street')
        self.assertContains(basket, 'Pay R80 by card')
        response = self.place(note='Ring the bell')
        payment = Payment.objects.get(pharmacy_order=order)
        self.assertEqual(response.url, reverse('portal:patient-billing-sandbox-checkout', args=[payment.pk]))
        order.refresh_from_db()
        self.assertEqual((order.status, order.note, order.delivery_address['line1']), ('submitted', 'Ring the bell', '1 Test Street'))
        self.assertFalse(Shipment.objects.exists())
        checkout = self.client.get(response.url)
        response = self.client.post(response.url, {'workflow_context': checkout.context['workflow_context'], 'choice': 'pay'})
        self.assertRedirects(response, reverse('portal:patient-order-detail', args=[order.pk]), fetch_redirect_response=False)
        order.refresh_from_db()
        parcel = order.shipment
        self.assertEqual(order.status, PharmacyOrder.Status.ACCEPTED)
        self.assertEqual((parcel.status, parcel.patient, parcel.scheduled_for),
                         ('draft', self.patient, shipping_day_from(self.today, shipping_weekday(self.company))))
        self.assertEqual([(item.product, item.quantity) for item in parcel.items.all()], [(self.open_product, 2)])
        self.assertEqual(parcel_payment(parcel)[0], 'paid')
        self.assertTrue(AuditEvent.objects.filter(action='pharmacy_order.accepted', metadata__automatic=True).exists())
        detail = self.page('patient-order-detail', [order.pk])
        self.assertContains(detail, f'It goes out with the parcels on {parcel.scheduled_for:%-d %b %Y}')
        self.assertNotContains(detail, 'Cancel order')
        self.assertContains(self.page('patient-treatment'), 'My orders')
        # The practice sees it on that week's shipping list.
        self.login()
        week = parcel.scheduled_for - timedelta(days=parcel.scheduled_for.weekday())
        shipping = self.page('ops-shipping', week=week, shipping_step='all')
        self.assertIn(parcel, shipping.context['shipments'])

    def test_without_a_payment_provider_the_order_waits_for_the_practice(self):
        self.add_item()
        with override_settings(PAYMENT_PROVIDER=''):
            self.assertContains(self.page('patient-basket'), 'Place my order')
            response = self.place()
            order = PharmacyOrder.objects.get()
            self.assertRedirects(response, reverse('portal:patient-order-detail', args=[order.pk]), fetch_redirect_response=False)
            self.assertContains(self.page('patient-order-detail', [order.pk]), 'will tell you how to pay')
        order.refresh_from_db()
        self.assertEqual(order.status, 'submitted')
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(Shipment.objects.exists())

    def test_the_basket_needs_a_delivery_address_and_comes_back_from_it(self):
        from care.test_billing import ADDRESS as DELIVERY

        self.address.delete()
        self.add_item()
        basket = self.page('patient-basket')
        self.assertContains(basket, 'Add my delivery address')
        self.assertContains(basket, reverse('portal:patient-delivery-address') + '?next=basket')
        self.assertNotContains(basket, 'Pay R')
        self.assertEqual(self.place().status_code, 400)
        self.assertEqual(PharmacyOrder.objects.get().status, 'draft')
        url = reverse('portal:patient-delivery-address') + '?next=basket'
        self.assertContains(self.client.get(url), f'action="{url}"')
        self.assertRedirects(self.client.post(url, DELIVERY), reverse('portal:patient-basket'), fetch_redirect_response=False)
        self.assertContains(self.page('patient-basket'), 'Pay R80 by card')

    def test_prescription_medicine_cannot_be_added_and_an_old_basket_must_remove_it(self):
        url = reverse('portal:patient-basket-item', args=[self.product.pk])
        response = self.client.post(url, {'workflow_context': self.signed('basket-product', self.product), 'quantity': 1})
        self.assertRedirects(response, reverse('portal:patient-treatment') + '#once-off-items', fetch_redirect_response=False)
        self.assertFalse(PharmacyOrder.objects.exists())
        # A basket from before the shop closed can still hold one.
        order = self.basket()
        basket = self.page('patient-basket')
        self.assertContains(basket, f'Remove {self.product.name} first')
        self.assertNotContains(basket, 'Pay R')
        response = self.client.post(reverse('portal:patient-order-detail', args=[order.pk]),
                                    {'workflow_context': basket.context['workflow_context'], 'action': 'submit'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(PharmacyOrder.objects.get().status, 'draft')
        response = self.client.post(url, {'workflow_context': basket.context['items'][0].remove_context, 'quantity': 0, 'next': 'basket'})
        self.assertRedirects(response, reverse('portal:patient-basket'), fetch_redirect_response=False)
        self.assertContains(self.page('patient-basket'), 'Your basket is empty')

    def test_quantity_repeat_is_set_once_and_bad_or_stale_context_never_writes(self):
        url = reverse('portal:patient-basket-item', args=[self.open_product.pk])
        for token in ('', 'invalid'):
            self.assertEqual(self.client.post(url, {'workflow_context': token, 'quantity': 2}).status_code, 302)
        self.assertFalse(PharmacyOrder.objects.exists())
        token = self.product_token()
        for _ in range(2):
            self.assertEqual(self.client.post(url, {'workflow_context': token, 'quantity': 2}).status_code, 302)
        self.assertEqual(PharmacyOrder.objects.get().items.get().quantity, 2)
        self.assertEqual(AuditEvent.objects.filter(action='basket.updated').count(), 1)
        # A price change makes the page's form stale.
        self.open_product.price = 50
        self.open_product.save()
        self.client.post(url, {'workflow_context': token, 'quantity': 3})
        self.assertEqual(PharmacyOrder.objects.get().items.get().quantity, 2)

    def test_placing_a_basket_that_changed_since_it_was_shown_is_refused(self):
        self.add_item()
        basket = self.page('patient-basket')
        order = basket.context['order']
        set_basket_quantity(company=self.company, patient=self.patient, actor=self.patient_user, product=self.open_product, quantity=3)
        response = self.client.post(reverse('portal:patient-order-detail', args=[order.pk]),
                                    {'workflow_context': basket.context['workflow_context'], 'action': 'submit'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['workflow_context'], basket.context['workflow_context'])
        order.refresh_from_db()
        self.assertEqual((order.status, order.items.get().quantity), ('draft', 3))
        self.assertFalse(Payment.objects.exists())

    def test_a_draft_opens_as_the_basket_and_the_old_shop_address_opens_once_off_items(self):
        order = self.basket()
        self.assertRedirects(self.client.get(reverse('portal:patient-order-detail', args=[order.pk])),
                             reverse('portal:patient-basket'), fetch_redirect_response=False)
        destination = reverse('portal:patient-treatment') + '#once-off-items'
        url = reverse('portal:patient-pharmacy')
        self.assertRedirects(self.client.get(url), destination, fetch_redirect_response=False)
        self.assertRedirects(self.client.head(url), destination, fetch_redirect_response=False)
        self.assertEqual(self.client.post(url).status_code, 405)
        self.assertEqual(self.client.get(reverse('portal:patient-basket-item', args=[self.open_product.pk])).status_code, 405)

    def test_shop_routes_require_an_authenticated_patient(self):
        urls = [reverse('portal:patient-basket'), reverse('portal:patient-orders'),
                reverse('portal:patient-basket-item', args=[self.open_product.pk])]
        self.client.logout()
        for url in urls:
            with self.subTest(url=url):
                response = self.client.post(url) if 'items' in url else self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith(reverse('accounts:login')))
        self.login(self.admin)
        for url in urls:
            with self.subTest(url=url):
                response = self.client.post(url) if 'items' in url else self.client.get(url)
                self.assertEqual(response.status_code, 403)

    def test_foreign_order_links_and_posts_do_not_disclose_or_modify_the_record(self):
        order = self.submitted()
        before = list(PharmacyOrder.objects.values())
        self.login(self.other_user)
        url = reverse('portal:patient-order-detail', args=[order.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {'action': 'cancel', 'confirm': 'on'}).status_code, 404)
        self.login(self.patient_user, company=self.beta)
        self.assertEqual(self.client.post(url, {'action': 'submit'}).status_code, 404)
        self.assertEqual(list(PharmacyOrder.objects.values()), before)
        self.assertFalse(Payment.objects.exists())

    def test_csrf_and_inactive_patient_guards_apply_to_basket_writes(self):
        client = Client(enforce_csrf_checks=True)
        self.login(self.patient_user, client=client)
        url = reverse('portal:patient-basket-item', args=[self.open_product.pk])
        self.assertEqual(client.post(url, {'quantity': 2}).status_code, 403)
        Patient.objects.filter(pk__in=(self.patient.pk, self.beta_patient.pk)).update(is_active=False)
        self.assertEqual(self.client.post(url, {'quantity': 2}).status_code, 403)
        self.assertFalse(PharmacyOrder.objects.exists())

    def test_an_unpaid_order_can_be_paid_from_its_page_and_found_in_my_orders(self):
        order = self.submitted()
        listing = self.page('patient-orders')
        self.assertContains(listing, reverse('portal:patient-order-detail', args=[order.pk]))
        detail = self.page('patient-order-detail', [order.pk])
        self.assertContains(detail, self.product.name)
        self.assertContains(detail, 'Cancel order')
        self.assertContains(detail, 'Pay R200 by card')
        self.assertContains(self.page('patient-billing'), 'My orders')

    def test_owned_unaccepted_order_can_be_cancelled_only_with_confirmation_and_context(self):
        order = self.submitted()
        url = reverse('portal:patient-order-detail', args=[order.pk])
        token = self.page('patient-order-detail', [order.pk]).context['workflow_context']
        for data in ({'action': 'cancel', 'confirm': 'on'}, {'action': 'cancel', 'workflow_context': token}):
            self.assertEqual(self.client.post(url, data).status_code, 400)
            order.refresh_from_db()
            self.assertEqual(order.status, 'submitted')
        response = self.client.post(url, {'action': 'cancel', 'confirm': 'on', 'workflow_context': token})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertFalse(Shipment.objects.exists())

    def test_accepted_order_cannot_be_cancelled_by_patient(self):
        order = self.submitted()
        order.status = PharmacyOrder.Status.ACCEPTED
        order.save()
        url = reverse('portal:patient-order-detail', args=[order.pk])
        page = self.page('patient-order-detail', [order.pk])
        self.assertNotContains(page, 'Cancel order')
        self.assertContains(page, 'Message your care team')
        response = self.client.post(url, {'action': 'cancel', 'confirm': 'on',
                                          'workflow_context': page.context['workflow_context']})
        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.status, PharmacyOrder.Status.ACCEPTED)
