"""Shipping filters use the displayed product, doctor and payment state and survive navigation."""
from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

from django.contrib.auth import get_user_model
from django.urls import reverse

from care.billing import record_manual_payment
from care.models import MedicationProduct, PatientSubscription, Shipment, ShipmentItem, SubscriptionCycle
from practices.models import CompanyMembership, Patient
from .filter_menus import ticked_values
from .test_operations import OperationsPortalFixture


class ShippingFilterTests(OperationsPortalFixture):
    def page(self, name, args=None, client=None, **params):
        if name == 'ops-shipping':
            params.setdefault('shipping_step', 'all')
        return super().page(name, args=args, client=client, **params)

    def ids(self, response):
        return {parcel.pk for parcel in response.context['page_obj']}

    def doctor_two(self):
        user = get_user_model().objects.create_user('shipping-filter-second-doctor@example.test', first_name='Second', last_name='Doctor')
        CompanyMembership.objects.create(company=self.company, user=user, role='doctor', clinician_type='doctor')
        return user

    def assert_query_filters(self, address, expected):
        query = parse_qs(urlsplit(address).query)
        for key, values in expected.items():
            self.assertEqual(set(ticked_values(query.get(key, []))), {str(value) for value in values}, (key, address))

    def test_product_multi_select_matches_any_item_without_duplicate_parcels(self):
        mixed = self.shipment(quantity=1)
        ShipmentItem.objects.create(company=self.company, shipment=mixed, product=self.open_product, quantity=1, dose='')
        open_only = self.shipment(product=self.open_product, quantity=1)
        extra = MedicationProduct.objects.create(company=self.company, name='Unselected product', price=10)
        excluded = self.shipment(product=extra, quantity=1)
        for values in ([self.product.pk, self.open_product.pk], f'{self.product.pk},{self.open_product.pk}'):
            response = self.page('ops-shipping', week=self.today, product=values)
            self.assertEqual(self.ids(response), {mixed.pk, open_only.pk})
            self.assertEqual(response.context['page_obj'].paginator.count, 2)
            self.assertNotIn(excluded.pk, self.ids(response))

    def test_doctor_filter_uses_prescriber_before_assigned_doctor_and_falls_back_for_other_parcels(self):
        second = self.doctor_two()
        Patient.objects.filter(pk=self.patient.pk).update(assigned_doctor=second)
        prescribed = self.shipment(quantity=1, authorization=self.auth)
        assigned = self.shipment(product=self.open_product, quantity=1, authorization=None)
        self.assertEqual(self.ids(self.page('ops-shipping', week=self.today, doctor=self.doctor.pk)), {prescribed.pk})
        self.assertEqual(self.ids(self.page('ops-shipping', week=self.today, doctor=second.pk)), {assigned.pk})
        response = self.page('ops-shipping', week=self.today, doctor=[self.doctor.pk, second.pk])
        self.assertEqual(self.ids(response), {prescribed.pk, assigned.pk})

    def test_payment_filter_uses_direct_payments_and_includes_all_outstanding_states(self):
        paid = self.shipment(quantity=1)
        direct = self.shipment(quantity=1, subscription=None)
        record_manual_payment(company=self.company, actor=self.admin, amount=100, method='eft', shipment=direct,
                              reference='DIRECT-SHIPPING-FILTER')
        outstanding = {self.shipment(quantity=1, subscription=None).pk}
        for state, attempts in (('pending', 0), ('pending', 1), ('failed', 2)):
            plan = PatientSubscription.objects.create(company=self.company, patient=self.other_patient,
                plan_name=f'Filter {state} {attempts}', starts_on=self.today)
            SubscriptionCycle.objects.create(company=self.company, patient=self.other_patient, subscription=plan,
                cycle_number=1, starts_on=self.today, ends_on=self.today + timedelta(days=27), due_on=self.today,
                amount=100, status=state, attempts=attempts)
            outstanding.add(self.shipment(quantity=1, patient=self.other_patient, subscription=plan).pk)
        self.assertEqual(self.ids(self.page('ops-shipping', week=self.today, payment='paid')), {paid.pk, direct.pk})
        self.assertEqual(self.ids(self.page('ops-shipping', week=self.today, payment='unpaid')), outstanding)
        self.assertEqual(self.ids(self.page('ops-shipping', week=self.today, payment=['paid', 'unpaid'])),
                         outstanding | {paid.pk, direct.pk})

    def test_product_doctor_status_and_payment_filters_combine_in_both_views(self):
        second = self.doctor_two()
        chosen = self.shipment(quantity=1, authorization=self.auth, status=Shipment.Status.READY)
        self.shipment(product=self.open_product, quantity=1, authorization=self.auth, status=Shipment.Status.READY)
        self.shipment(quantity=1, authorization=self.auth, status=Shipment.Status.DRAFT)
        other = Patient.objects.create(company=self.company, first_name='Second', last_name='Patient', assigned_doctor=second)
        self.shipment(quantity=1, patient=other, subscription=None, status=Shipment.Status.READY)
        for mode in ('customers', 'medication'):
            response = self.page('ops-shipping', week=self.today, view=mode, product=self.product.pk,
                                 doctor=self.doctor.pk, status='ready', payment='paid')
            self.assertEqual(self.ids(response), {chosen.pk})
            self.assertEqual([section['name'] for section in response.context['filter_menu']['sections']],
                             ['status', 'product', 'doctor', 'payment'])
            self.assertEqual(response.context['filter_menu']['count'], 4)

    def test_invalid_and_foreign_filter_choices_show_no_parcels(self):
        self.shipment(quantity=1)
        for field, value in (('product', self.beta_product.pk), ('doctor', self.beta_admin.pk),
                             ('product', 'not-an-id'), ('payment', 'anything')):
            with self.subTest(field=field, value=value):
                response = self.page('ops-shipping', week=self.today, **{field: value})
                self.assertIn(field, response.context['filter_form'].errors)
                self.assertEqual(self.ids(response), set())
        response = self.page('ops-shipping', week=self.today)
        form = response.context['filter_form']
        self.assertNotIn(str(self.beta_product.pk), {str(value) for value, _ in form.fields['product'].choices})
        self.assertNotIn(str(self.beta_admin.pk), {str(value) for value, _ in form.fields['doctor'].choices})

    def test_multi_select_filters_survive_view_week_workflow_and_search_navigation(self):
        second = self.doctor_two()
        self.shipment(quantity=1, authorization=self.auth)
        filters = dict(product=[self.product.pk, self.open_product.pk], doctor=[self.doctor.pk, second.pk],
                       payment=['paid', 'unpaid'], status=['draft', 'ready'])
        response = self.page('ops-shipping', week=self.today, view='medication', q='Alpha', **filters)
        for address in [response.context['customers_view_url'], response.context['medication_view_url'],
                        response.context['prev_week_url'], response.context['next_week_url'],
                        response.context['search_clear_url'],
                        *[step['url'] for step in response.context['shipping_steps']]]:
            self.assert_query_filters(address, filters)
        hidden = response.context['search_hidden']
        for field, values in filters.items():
            self.assertEqual(set(ticked_values(hidden[field])), {str(value) for value in values})
        self.assertEqual(hidden['view'], 'medication')
        self.assert_query_filters(response.context['filter_menu']['clear_url'], {'view': ['medication']})
        clear_query = parse_qs(urlsplit(response.context['filter_menu']['clear_url']).query)
        for field in filters:
            self.assertNotIn(field, clear_query)

    def test_note_return_url_keeps_repeated_filters(self):
        second = self.doctor_two()
        parcel = self.shipment(quantity=1, authorization=self.auth)
        filters = dict(product=[self.product.pk, self.open_product.pk], doctor=[self.doctor.pk, second.pk],
                       payment=['paid', 'unpaid'], status=['draft', 'ready'])
        data = dict(week=self.today.isoformat(), view='medication', **filters)
        page = self.page('ops-shipping', **data)
        token = next(shipment.note_context for shipment in page.context['shipments'] if shipment.pk == parcel.pk)
        response = self.client.post(reverse('portal:ops-shipment-note', args=[parcel.pk]),
            {'workflow_context': token, 'note': 'Filter navigation preserved', 'list_query': urlencode(data, doseq=True)})
        self.assertEqual(response.status_code, 302)
        self.assert_query_filters(response['Location'], filters)
