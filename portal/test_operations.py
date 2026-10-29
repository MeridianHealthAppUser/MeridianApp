"""Dedicated operations pages enforce signed intent, CSRF and scoped access."""

import csv
import io
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

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

    def test_prepare_lock_dispatch_and_delivery_are_explicit_separate_actions(self):
        shipment = self.shipment()
        item = shipment.items.get()
        url = reverse('portal:ops-shipment-detail', args=[shipment.pk])
        def post_action(action, **extra):
            return self.client.post(url, {'workflow_context': self.token('ops-shipment-detail', [shipment.pk]),
                'action': action, 'confirm': 'on', **extra})
        self.assertEqual(post_action('prepare').status_code, 302)
        self.assertEqual(post_action('dispatch', tracking_number='TRACK-1').status_code, 400)
        shipping = self.page('ops-shipping', week=self.today)
        self.assertEqual(self.client.post(reverse('portal:ops-shipping-lock'), {
            'workflow_context': shipping.context['lock_context'], 'week': self.today}).status_code, 302)
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

    def test_the_list_shows_each_parcel_and_marks_the_weeks_drafts_ready(self):
        week = self.today - timedelta(days=self.today.weekday())
        draft = self.shipment(quantity=2, scheduled_for=week)
        page = self.page('ops-shipping', week=week)
        self.assertContains(page, f'{self.product.name} <span class="ops-dose">{self.auth.max_dose}</span>', html=False)
        self.assertContains(page, '<td data-label="Qty"><div>2</div></td>', html=False)
        response = self.client.post(reverse('portal:ops-shipping-prepare'), {
            'workflow_context': page.context['prepare_context'], 'week': week})
        self.assertRedirects(response, f"{reverse('portal:ops-shipping')}?week={week}", fetch_redirect_response=False)
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'ready')
        self.assertContains(self.page('ops-shipping', week=week), '1 draft parcel marked ready.')
        self.login(self.doctor)
        self.assertEqual(self.client.post(reverse('portal:ops-shipping-prepare'), {'week': week}).status_code, 403)

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
        self.assertEqual([section['label'] for section in page.context['filter_menu']['sections']], ['Status'])
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

    def test_each_draft_line_has_its_own_mark_ready_button(self):
        week = self.today - timedelta(days=self.today.weekday())
        draft = self.shipment(quantity=1, scheduled_for=week)
        failing = self.shipment(quantity=1, scheduled_for=week)
        failing.items.update(dose='Not the authorised dose')
        ready = self.ready(self.shipment(quantity=1, scheduled_for=week))
        page = self.page('ops-shipping', week=week, status='all')
        rows = {shipment.pk: shipment for shipment in page.context['shipments']}
        self.assertTrue(rows[draft.pk].ready_context)
        self.assertFalse(hasattr(rows[ready.pk], 'ready_context'))
        self.assertContains(page, reverse('portal:ops-shipment-ready', args=[draft.pk]))
        self.assertNotContains(page, reverse('portal:ops-shipment-ready', args=[ready.pk]))
        url = reverse('portal:ops-shipment-ready', args=[draft.pk])
        data = {'workflow_context': rows[draft.pk].ready_context, 'list_query': f'week={week}&status=all&next=https://evil.test'}
        response = self.client.post(url, data)
        # Back to the same week and status; nothing else from the query is followed.
        self.assertRedirects(response, f"{reverse('portal:ops-shipping')}?week={week}&status=all", fetch_redirect_response=False)
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'ready')
        # The same click again is stale: the parcel has changed since the page was shown.
        self.client.post(url, data)
        self.assertEqual(AuditEvent.objects.filter(action='shipment.prepared', target_id=str(draft.pk)).count(), 1)
        self.client.post(reverse('portal:ops-shipment-ready', args=[failing.pk]), {'workflow_context': rows[failing.pk].ready_context})
        failing.refresh_from_db()
        self.assertEqual(failing.status, 'draft')
        self.assertContains(self.page('ops-shipping', week=week), 'not marked ready: The recorded dose does not match')
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
            'workflow_context': self.token('ops-shipment-detail', [shipment.pk]), 'action': 'prepare', 'confirm': 'on'}).status_code, 302)
        lock = self.page('ops-shipping', week=shipment.scheduled_for)
        self.client.post(reverse('portal:ops-shipping-lock'), {'workflow_context': lock.context['lock_context'], 'week': shipment.scheduled_for})
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
                             f'{reverse("portal:ops-shipping")}?week={week}#week-pens', fetch_redirect_response=False)
        page = self.page('ops-shipping', week=week)
        self.assertEqual([(line['product'].pk, line['pens']) for line in page.context['week_order']['lines']], [(self.product.pk, 3)])
        self.assertEqual([(row['product'].pk, row['to_ship'], row['to_order']) for row in page.context['stock_rows']],
                         [(self.product.pk, 4, 3), (self.open_product.pk, 0, 0)])
        self.assertContains(page, 'Not in To order yet: 1 draft parcel.')
        self.assertContains(page, f'{reverse("portal:ops-week-order")}?week={week}')
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

    def test_history_export_uses_immutable_snapshot_and_escapes_patient_formula(self):
        shipment = dispatch_shipment(shipment=self.locked(), actor=self.admin, tracking_number='TRACK', confirm=True)
        shipment.dispatch_snapshot['patient_name'] = ' =HYPERLINK("bad")'
        shipment.save(update_fields=('dispatch_snapshot',))
        old_product_name = self.product.name
        MedicationProduct.objects.filter(pk=self.product.pk).update(name='Changed later')
        response = self.client.get(reverse('portal:ops-history-export'))
        self.assertContains(response, old_product_name)
        self.assertNotContains(response, 'Changed later')
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(rows[1][rows[0].index('Patient')][0], "'")
        self.assertEqual(self.client.get(reverse('portal:ops-history-export'), {'start': 'bad'}).status_code, 400)

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


class PatientPharmacyPortalTests(OperationsPortalFixture):
    def setUp(self):
        self.login(self.patient_user)

    def retired_urls(self, pk=123):
        return [reverse(f'portal:{name}', args=args) for name, args in (
            ('patient-pharmacy', ()), ('patient-basket', ()),
            ('patient-basket-item', (self.product.pk,)),
        )]

    def test_old_shop_links_redirect_to_subscription_without_changing_existing_orders(self):
        order = self.basket()
        before = list(PharmacyOrder.objects.values())
        events_before = AuditEvent.objects.count()
        destination = reverse('portal:patient-treatment') + '#treatment-plan'
        for url in self.retired_urls(order.pk):
            with self.subTest(url=url):
                self.assertRedirects(self.client.get(url), destination, fetch_redirect_response=False)
                self.assertRedirects(self.client.head(url), destination, fetch_redirect_response=False)
        self.assertEqual(list(PharmacyOrder.objects.values()), before)
        self.assertEqual(AuditEvent.objects.count(), events_before)
        self.assertFalse(Payment.objects.exists())

    def test_posts_from_old_pages_cannot_create_baskets_or_payments(self):
        for url in self.retired_urls():
            with self.subTest(url=url):
                response = self.client.post(url, {'quantity': 2, 'action': 'submit', 'confirm': 'on', **ADDRESS})
                self.assertEqual(response.status_code, 410)
        self.assertFalse(PharmacyOrder.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())

    def test_stale_signed_forms_cannot_change_or_submit_existing_baskets(self):
        from django.test import RequestFactory
        from .workflow_context import make_workflow_context

        order = self.basket()
        request = RequestFactory().post('/')
        request.user = self.patient_user
        before = list(PharmacyOrder.objects.values())
        items_before = list(order.items.values())
        events_before = AuditEvent.objects.count()
        for name, record, kind, action in (
            ('patient-basket-item', self.product, 'basket-product', ''),
            ('patient-order-detail', order, 'patient-order', 'submit'),
        ):
            token = make_workflow_context(request, self.company, kind, record, patient=self.patient)
            url = reverse(f'portal:{name}', args=[record.pk])
            response = self.client.post(url, {'workflow_context': token, 'quantity': 3, 'action': action,
                                               'confirm': 'on', **ADDRESS})
            self.assertEqual(response.status_code, 410)
        self.assertEqual(list(PharmacyOrder.objects.values()), before)
        self.assertEqual(list(order.items.values()), items_before)
        self.assertEqual(AuditEvent.objects.count(), events_before)
        self.assertFalse(Payment.objects.exists())

    def test_retired_routes_still_require_an_authenticated_patient(self):
        self.client.logout()
        for url in self.retired_urls():
            with self.subTest(url=url):
                for method in (self.client.get, self.client.post):
                    response = method(url)
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response.url.startswith(reverse('accounts:login')))
        self.login(self.admin)
        for url in self.retired_urls():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
                self.assertEqual(self.client.post(url).status_code, 403)

    def test_foreign_order_links_and_posts_do_not_disclose_or_modify_the_record(self):
        order = self.submitted()
        before = list(PharmacyOrder.objects.values())
        self.login(self.other_user)
        url = reverse('portal:patient-order-detail', args=[order.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {'action': 'cancel', 'confirm': 'on'}).status_code, 404)
        self.login(self.patient_user, company=self.beta)
        self.assertEqual(self.client.post(url, {'action': 'submit', 'confirm': 'on', **ADDRESS}).status_code, 404)
        self.assertEqual(list(PharmacyOrder.objects.values()), before)
        self.assertFalse(Payment.objects.exists())

    def test_csrf_and_inactive_patient_guards_still_apply_to_old_write_urls(self):
        client = Client(enforce_csrf_checks=True)
        self.login(self.patient_user, client=client)
        url = reverse('portal:patient-basket-item', args=[self.product.pk])
        self.assertEqual(client.post(url, {'quantity': 2}).status_code, 403)
        Patient.objects.filter(pk__in=(self.patient.pk, self.beta_patient.pk)).update(is_active=False)
        self.assertEqual(self.client.post(url, {'quantity': 2}).status_code, 403)
        self.assertFalse(PharmacyOrder.objects.exists())

    def test_existing_requests_remain_readable_without_new_request_controls(self):
        order = self.submitted()
        before = list(PharmacyOrder.objects.values())
        listing = self.page('patient-orders')
        self.assertContains(listing, reverse('portal:patient-order-detail', args=[order.pk]))
        detail = self.page('patient-order-detail', [order.pk])
        self.assertContains(detail, self.product.name)
        self.assertContains(detail, 'Cancel supply request')
        self.assertContains(detail, 'Pay R200 by card')
        for removed in ('Add to basket', 'Open current basket', 'Submit supply request'):
            self.assertNotContains(listing, removed)
            self.assertNotContains(detail, removed)
        self.assertEqual(list(PharmacyOrder.objects.values()), before)
        billing = self.page('patient-billing')
        self.assertContains(billing, 'Previous supply requests')

    def test_owned_unaccepted_request_can_be_cancelled_only_with_confirmation_and_context(self):
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

    def test_accepted_request_cannot_be_cancelled_by_patient(self):
        order = self.submitted()
        order.status = PharmacyOrder.Status.ACCEPTED
        order.save()
        url = reverse('portal:patient-order-detail', args=[order.pk])
        page = self.page('patient-order-detail', [order.pk])
        self.assertNotContains(page, 'Cancel supply request')
        self.assertContains(page, 'Message your care team')
        response = self.client.post(url, {'action': 'cancel', 'confirm': 'on',
                                          'workflow_context': page.context['workflow_context']})
        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.status, PharmacyOrder.Status.ACCEPTED)

    def test_old_draft_is_readonly_and_cannot_be_submitted(self):
        order = self.basket()
        page = self.page('patient-order-detail', [order.pk])
        self.assertContains(page, 'This draft was never submitted')
        self.assertNotContains(page, 'name="quantity"')
        self.assertNotContains(page, 'name="action" value="submit"')
        self.assertNotContains(page, 'Pay R')
        before = list(order.items.values())
        response = self.client.post(reverse('portal:patient-order-detail', args=[order.pk]), {
            'action': 'submit', 'confirm': 'on', 'workflow_context': page.context['workflow_context'], **ADDRESS})
        self.assertEqual(response.status_code, 410)
        order.refresh_from_db()
        self.assertEqual(order.status, PharmacyOrder.Status.DRAFT)
        self.assertEqual(list(order.items.values()), before)
        self.assertEqual(PharmacyOrder.objects.count(), 1)
        self.assertFalse(Payment.objects.exists())
