"""Billing pages: the patient's Billing and payments, the sandbox approval pages, Start my subscription with an address
and debit order, Operations > Billing, delivery addresses, and Weekly shipping's payment and courier steps."""

from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from care import courier
from care.addresses import save_delivery_address
from care.billing import approve_mandate, start_debit_order
from care.models import (
    AuditEvent, DebitMandate, DeliveryAddress, PatientSubscription, Payment, PharmacyOrder, PracticeSettings, Shipment,
    SubscriptionCycle,
)
from care.operations import prepare_shipment
from care.test_billing import ADDRESS, BillingFixture
from practices.models import CompanyMembership
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


class BillingPageFixture(BillingFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from django.contrib.auth import get_user_model

        cls.super_admin = get_user_model().objects.create_user(email='billing-super@example.test')
        CompanyMembership.objects.create(company=cls.company, user=cls.super_admin, role='super_admin')

    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def approved(self, *, fail=False):
        mandate, _ = start_debit_order(subscription=self.plan, actor=self.patient_user)
        return approve_mandate(mandate=mandate, fail_debits=fail)


class PatientBillingPageTests(BillingPageFixture):
    def test_the_sandbox_approval_page_approves_the_debit_order_and_collects_the_first_payment(self):
        self.login(self.patient_user)
        page = self.client.get(reverse('portal:patient-billing'))
        self.assertContains(page, 'No debit order yet')
        response = self.client.post(reverse('portal:patient-debit-order'), {
            'workflow_context': page.context['debit_context'], 'method': 'debicheck'})
        mandate = DebitMandate.objects.get()
        self.assertRedirects(response, reverse('portal:patient-billing-sandbox-mandate', args=[mandate.pk]),
                             fetch_redirect_response=False)
        approval = self.client.get(response['Location'])
        self.assertContains(approval, 'R3 295.00')
        self.client.post(response['Location'], {'workflow_context': approval.context['workflow_context'], 'choice': 'approve'})
        mandate.refresh_from_db()
        self.assertEqual(mandate.status, 'active')
        self.assertEqual(SubscriptionCycle.objects.get().status, 'paid')
        page = self.client.get(reverse('portal:patient-billing'))
        self.assertContains(page, 'Paid by DebiCheck debit order')
        invoice = SubscriptionCycle.objects.get().invoice
        self.assertContains(self.client.get(reverse('portal:patient-invoice', args=[invoice.pk])), invoice.invoice_number)

    def test_a_failed_payment_can_be_paid_now_by_card_and_another_patient_cant_see_it(self):
        self.approved(fail=True)
        self.login(self.patient_user)
        page = self.client.get(reverse('portal:patient-billing'))
        self.assertContains(page, 'Payment needed')
        cycle = page.context['due'][0]
        response = self.client.post(reverse('portal:patient-cycle-pay', args=[cycle.pk]), {'workflow_context': cycle.pay_context})
        payment = Payment.objects.get(method='card')
        checkout = self.client.get(response['Location'])
        self.client.post(response['Location'], {'workflow_context': checkout.context['workflow_context'], 'choice': 'pay'})
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, 'paid')
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'paid')
        # Only the patient's own.
        from django.contrib.auth import get_user_model
        from practices.models import Patient

        other = get_user_model().objects.create_user(email='billing-other@example.test')
        Patient.objects.create(company=self.company, user=other, first_name='Other', last_name='Patient')
        self.login(other)
        self.assertEqual(self.client.get(reverse('portal:patient-invoice', args=[cycle.invoice.pk])).status_code, 404)

    def test_the_sandbox_pages_only_exist_with_the_sandbox_provider(self):
        mandate, _ = start_debit_order(subscription=self.plan, actor=self.patient_user)
        self.login(self.patient_user)
        with override_settings(PAYMENT_PROVIDER='peach'):
            self.assertEqual(self.client.get(reverse('portal:patient-billing-sandbox-mandate', args=[mandate.pk])).status_code, 404)

    def test_the_patient_keeps_their_delivery_address(self):
        self.login(self.patient_user)
        page = self.client.get(reverse('portal:patient-delivery-address'))
        self.assertContains(page, 'Leave blank to use your own name.')
        self.assertEqual(self.client.post(reverse('portal:patient-delivery-address'), {**ADDRESS, 'postal_code': '1'}).status_code, 400)
        self.assertRedirects(self.client.post(reverse('portal:patient-delivery-address'), ADDRESS),
                             f'{reverse("portal:patient-profile")}#profile-delivery', fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse('portal:patient-profile')), '12 Kloof Street')

    def test_existing_submitted_request_can_still_be_paid_from_its_page(self):
        order = PharmacyOrder.objects.create(company=self.company, patient=self.patient, status='submitted', subtotal='120.00')
        self.login(self.patient_user)
        detail = self.client.get(reverse('portal:patient-order-detail', args=[order.pk]))
        self.assertContains(detail, 'Pay R120 by card')
        response = self.client.post(reverse('portal:patient-order-pay', args=[order.pk]),
                                    {'workflow_context': detail.context['pay_context']})
        payment = Payment.objects.get(pharmacy_order=order)
        checkout = self.client.get(response.url)
        response = self.client.post(response.url, {'workflow_context': checkout.context['workflow_context'], 'choice': 'pay'})
        self.assertRedirects(response, reverse('portal:patient-order-detail', args=[order.pk]), fetch_redirect_response=False)
        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertIsNotNone(order.paid_at)
        self.assertEqual(payment.status, Payment.Status.PAID)
        self.assertEqual(PharmacyOrder.objects.count(), 1)
        self.assertContains(self.client.get(reverse('portal:patient-order-detail', args=[order.pk])), 'Paid on')

    def test_draft_cancelled_and_paid_requests_cannot_start_new_payments(self):
        from django.test import RequestFactory
        from .workflow_context import make_workflow_context

        self.login(self.patient_user)
        request = RequestFactory().post('/')
        request.user = self.patient_user
        for status, paid_at in (('draft', None), ('cancelled', None), ('submitted', timezone.now())):
            order = PharmacyOrder.objects.create(company=self.company, patient=self.patient, status=status,
                                                 subtotal='120.00', paid_at=paid_at)
            token = make_workflow_context(request, self.company, 'order-pay', order, patient=self.patient)
            with patch('care.payment_providers.SandboxProvider.start_checkout') as checkout:
                self.client.post(reverse('portal:patient-order-pay', args=[order.pk]), {'workflow_context': token})
                checkout.assert_not_called()
            self.assertFalse(Payment.objects.filter(pharmacy_order=order).exists())

    def test_pending_existing_order_card_form_can_prepare_its_checkout(self):
        order = PharmacyOrder.objects.create(company=self.company, patient=self.patient, status='submitted', subtotal='120.00')
        payment = Payment.objects.create(company=self.company, patient=self.patient, pharmacy_order=order,
                                         kind=Payment.Kind.ONCE_OFF, method=Payment.Method.CARD, provider='peach',
                                         amount='120.00', due_on=self.today, checkout=True)
        self.login(self.patient_user)
        url = reverse('portal:patient-card-form', args=['payment', payment.pk])
        with patch('care.peach.PeachProvider.card_form', return_value={'checkout_id': 'existing-checkout',
                   'script_url': 'https://sandbox.invalid/payment.js', 'action': '/existing-return/'}) as card_form:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            card_form.assert_called_once()
        self.assertContains(response, 'for your request')
        order.status = PharmacyOrder.Status.CANCELLED
        order.save()
        with patch('care.peach.PeachProvider.card_form') as card_form:
            self.assertRedirects(self.client.get(url), reverse('portal:patient-order-detail', args=[order.pk]),
                                 fetch_redirect_response=False)
            card_form.assert_not_called()

    def test_paid_request_cancellation_preserves_payment_and_explains_refund_contact(self):
        order = PharmacyOrder.objects.create(company=self.company, patient=self.patient, status='submitted',
                                             subtotal='120.00', paid_at=timezone.now())
        payment = Payment.objects.create(company=self.company, patient=self.patient, pharmacy_order=order,
                                         kind=Payment.Kind.ONCE_OFF, method=Payment.Method.CARD, amount='120.00',
                                         due_on=self.today, status=Payment.Status.PAID, paid_at=order.paid_at)
        self.login(self.patient_user)
        url = reverse('portal:patient-order-detail', args=[order.pk])
        page = self.client.get(url)
        self.assertContains(page, 'contact your care team about any refund')
        self.client.post(url, {'workflow_context': page.context['workflow_context'], 'action': 'cancel', 'confirm': 'on'})
        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(order.status, PharmacyOrder.Status.CANCELLED)
        self.assertIsNotNone(order.paid_at)
        self.assertEqual(payment.status, Payment.Status.PAID)
        self.assertContains(self.client.get(url), 'contact your care team about any refund')

    def test_cancelled_order_pending_sandbox_checkout_cannot_collect(self):
        from care.billing import start_order_payment

        order = PharmacyOrder.objects.create(company=self.company, patient=self.patient, status='submitted', subtotal='120.00')
        payment, url = start_order_payment(order=order, actor=self.patient_user)
        order.status = PharmacyOrder.Status.CANCELLED
        order.save()
        self.login(self.patient_user)
        for method in (self.client.get, self.client.post):
            with patch('portal.billing_views.complete_card_payment') as collect:
                self.assertRedirects(method(url), reverse('portal:patient-order-detail', args=[order.pk]),
                                     fetch_redirect_response=False)
                collect.assert_not_called()
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)


class StartSubscriptionTests(BillingPageFixture):
    def setUp(self):
        super().setUp()
        PatientSubscription.objects.all().delete()

    def test_starting_saves_the_address_and_goes_on_to_approve_the_debit_order(self):
        self.login(self.patient_user)
        page = self.client.get(reverse('portal:patient-treatment'))
        response = self.client.post(reverse('portal:patient-subscription-enroll'), {
            **ADDRESS, 'authorization': self.auth.pk, 'method': 'card', 'confirm': 'on',
            'treatment_context': page.context['enrollment_context']})
        mandate = DebitMandate.objects.get()
        self.assertEqual(mandate.method, 'card')
        self.assertRedirects(response, reverse('portal:patient-billing-sandbox-mandate', args=[mandate.pk]),
                             fetch_redirect_response=False)
        self.assertTrue(DeliveryAddress.objects.filter(patient=self.patient, suburb='Gardens').exists())


class StaffBillingTests(BillingPageFixture):
    def test_practice_admins_see_billing_and_record_a_payment_doctors_cant(self):
        self.approved(fail=True)
        self.login(self.doctor)
        self.assertEqual(self.client.get(reverse('portal:ops-billing')).status_code, 403)
        self.login(self.admin)
        page = self.client.get(reverse('portal:ops-billing'))
        self.assertContains(page, 'Retrying')
        self.assertFalse(page.context['show_totals'])
        detail = self.client.get(reverse('portal:ops-billing-detail', args=[self.plan.pk]), {'record': SubscriptionCycle.objects.get().pk})
        cycle = detail.context['cycles'][0]
        response = self.client.post(reverse('portal:ops-billing-detail', args=[self.plan.pk]), {
            'action': 'record', 'cycle': cycle.pk, 'workflow_context': cycle.record_context, 'amount': '3295', 'method': 'eft',
            'reference': 'EFT-1'})
        self.assertEqual(response.status_code, 302)
        cycle.refresh_from_db()
        self.assertEqual(cycle.status, 'paid')
        # Filtering by state.
        self.assertEqual(len(self.client.get(reverse('portal:ops-billing'), {'state': 'failed'}).context['page_obj']), 0)

    def test_only_super_admins_run_the_debits_and_see_totals(self):
        self.approved()
        self.login(self.admin)
        self.assertEqual(self.client.post(reverse('portal:ops-billing-run'), {}).status_code, 403)
        self.login(self.super_admin)
        page = self.client.get(reverse('portal:ops-billing'))
        self.assertTrue(page.context['show_totals'])
        self.assertRedirects(self.client.post(reverse('portal:ops-billing-run'), {'workflow_context': page.context['run_context']}),
                             reverse('portal:ops-billing'), fetch_redirect_response=False)

    def test_the_next_debit_moves_and_a_new_debit_order_is_asked_for(self):
        self.approved()
        self.login(self.admin)
        detail = self.client.get(reverse('portal:ops-billing-detail', args=[self.plan.pk]))
        day = self.today + timedelta(days=12)
        self.client.post(reverse('portal:ops-billing-detail', args=[self.plan.pk]), {
            'action': 'next_debit', 'workflow_context': detail.context['next_context'], 'day': day})
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.next_debit_on, day)
        self.client.post(reverse('portal:ops-billing-detail', args=[self.plan.pk]), {
            'action': 'ask', 'workflow_context': detail.context['ask_context']})
        self.assertTrue(AuditEvent.objects.filter(action='billing.new_debit_order_requested').exists())
        self.assertEqual(DebitMandate.objects.get().status, 'update_needed')

    def test_practice_settings_hold_the_billing_and_courier_details(self):
        self.login(self.super_admin)
        page = self.client.get(reverse('portal:practice-settings'))
        self.assertContains(page, 'Initial consultation fee (R)')
        self.assertContains(page, 'Whenever the prescription or its price changes')
        self.client.post(reverse('portal:practice-settings'), {'section': 'billing', 'initial_consult_fee': '700',
                                                                'require_payment': 'on', 'payment_retry_days': '7, 3',
                                                                'new_debit_order': 'higher'})
        settings_row = PracticeSettings.objects.get(company=self.company)
        self.assertEqual((settings_row.initial_consult_fee, settings_row.payment_retry_days, settings_row.new_debit_order),
                         (700, '3,7', 'higher'))
        self.assertEqual(self.client.post(reverse('portal:practice-settings'), {
            'section': 'billing', 'initial_consult_fee': '700', 'payment_retry_days': 'soon'}).status_code, 400)


class WeeklyShippingPaymentTests(BillingPageFixture):
    def parcel(self):
        shipment = Shipment.objects.create(company=self.company, patient=self.patient, subscription=self.plan,
                                           authorization=self.auth, scheduled_for=self.today)
        shipment.items.create(company=self.company, product=self.dose.pen, dose=self.auth.max_dose, quantity=1)
        return prepare_shipment(shipment=shipment, actor=self.admin)

    def test_the_list_shows_where_each_parcel_goes_and_whether_its_paid(self):
        shipment = self.parcel()
        self.login(self.admin)
        page = self.client.get(reverse('portal:ops-shipping'), {'week': self.today})
        row = page.context['shipments'][0]
        self.assertEqual(row.payment_state, 'unpaid')
        self.assertContains(page, 'No address')
        self.assertEqual(page.context['lock_blocked'], 1)
        # The practice adds the address for the patient, and records a payment.
        self.client.post(row.address_url.split('?')[0], {**ADDRESS, 'next': reverse('portal:ops-shipping')})
        self.assertTrue(DeliveryAddress.objects.filter(patient=self.patient, updated_by=self.admin).exists())
        self.client.post(reverse('portal:ops-parcel-payment', args=[shipment.pk]), {'amount': '3295', 'method': 'eft'})
        page = self.client.get(reverse('portal:ops-shipping'), {'week': self.today})
        self.assertEqual(page.context['shipments'][0].payment_state, 'paid')
        self.assertEqual(page.context['lock_blocked'], 0)
        response = self.client.post(reverse('portal:ops-shipping-lock'), {'workflow_context': page.context['lock_context'],
                                                                          'week': page.context['week_start']})
        shipment.refresh_from_db()
        self.assertIsNotNone(shipment.locked_at)
        self.assertEqual(response.status_code, 302)

    def test_hold_and_lock_the_rest_from_the_list(self):
        self.parcel()
        self.login(self.admin)
        page = self.client.get(reverse('portal:ops-shipping'), {'week': self.today})
        self.assertContains(page, 'Hold 1 and lock the rest')
        self.client.post(reverse('portal:ops-shipping-lock'), {'workflow_context': page.context['lock_context'],
                                                               'week': page.context['week_start'], 'hold_failing': '1'})
        self.assertEqual(Shipment.objects.get().status, 'held')

    @override_settings(TCG_API_KEY='key')
    def test_waybills_are_booked_and_the_collection_recorded_from_the_list(self):
        PracticeSettings.objects.filter(company=self.company).update(
            collection_street='1 Practice Road', collection_suburb='Claremont', collection_city='Cape Town',
            collection_province='WC', collection_postal_code='7708', collection_contact='Dispensary', collection_phone='021 555 0100')
        save_delivery_address(company=self.company, patient=self.patient, actor=self.patient_user, data=ADDRESS)
        self.approved()
        shipment = self.parcel()
        self.login(self.admin)
        page = self.client.get(reverse('portal:ops-shipping'), {'week': self.today})
        self.client.post(reverse('portal:ops-shipping-lock'), {'workflow_context': page.context['lock_context'], 'week': page.context['week_start']})
        page = self.client.get(reverse('portal:ops-shipping'), {'week': self.today})
        self.assertEqual(page.context['to_book'], 1)
        with patch.object(courier, '_call', return_value={'id': 5, 'short_tracking_reference': 'TCG5'}):
            self.client.post(reverse('portal:ops-shipping-courier'), {'workflow_context': page.context['courier_context'],
                                                                      'week': page.context['week_start']})
        page = self.client.get(reverse('portal:ops-shipping'), {'week': self.today})
        self.assertEqual((page.context['to_book'], page.context['to_collect']), (0, 1))
        self.assertContains(page, 'Waybill TCG5')
        self.client.post(reverse('portal:ops-shipping-collect'), {
            'workflow_context': page.context['collect_context'], 'week': page.context['week_start'], 'confirm': 'on',
            f'batch_{self.dose.pen_id}': 'SG-2612, expiry 04/2027'})
        shipment.refresh_from_db()
        self.assertEqual((shipment.status, shipment.tracking_number), ('dispatched', 'TCG5'))
        self.assertEqual(shipment.items.get().batch_note, 'SG-2612, expiry 04/2027')


@override_settings(PAYMENT_PROVIDER='peach', PEACH_API_URL='https://peach.test', PEACH_ENTITY_ID='entity',
                   PEACH_ACCESS_TOKEN='token', SITE_URL='https://meridian.test')
class HostedProviderPageTests(BillingPageFixture):
    """The pages a hosted provider uses: Peach's card form on the app's page, the return page and the webhook."""

    def test_the_card_page_shows_peachs_form_and_the_return_page_asks_peach_how_it_went(self):
        from care.test_payment_providers import FakePeach

        self.login(self.patient_user)
        page = self.client.get(reverse('portal:patient-billing'))
        self.assertEqual([value for value, _ in page.context['debit_form'].fields['method'].choices], ['card'])
        response = self.client.post(reverse('portal:patient-debit-order'), {'workflow_context': page.context['debit_context'],
                                                                            'method': 'card'})
        mandate = DebitMandate.objects.get()
        self.assertRedirects(response, reverse('portal:patient-card-form', args=['mandate', mandate.pk]), fetch_redirect_response=False)
        checkout = FakePeach(checkouts=(200, {'id': 'CHK5', 'integrity': 'sha384-xyz', 'result': {'code': '000.200.100'}}))
        with patch('care.peach.http_call', checkout):
            form_page = self.client.get(reverse('portal:patient-card-form', args=['mandate', mandate.pk]))
        self.assertContains(form_page, '<script src="https://peach.test/v1/paymentWidgets.js?checkoutId=CHK5/registration" '
                                       'integrity="sha384-xyz" crossorigin="anonymous"></script>', html=False)
        self.assertContains(form_page, f'action="https://meridian.test/patient/billing/return/mandate/{mandate.pk}/"')
        approved = FakePeach(checkouts_CHK5_registration=(200, {'id': 'reg-5', 'paymentBrand': 'MASTER', 'card': {'last4Digits': '0005'},
                                                                'result': {'code': '000.100.110'}}))
        with patch('care.peach.http_call', approved), self.captureOnCommitCallbacks(execute=True):
            # What the patient's browser brings back isn't trusted: Peach is asked.
            back = self.client.get(reverse('portal:patient-payment-return', args=['mandate', mandate.pk]),
                                   {'id': 'CHK5', 'resourcePath': '/v1/checkouts/forged/registration'})
        self.assertRedirects(back, reverse('portal:patient-billing'), fetch_redirect_response=False)
        mandate.refresh_from_db()
        self.assertEqual((mandate.status, mandate.account_label, mandate.provider_reference), ('active', 'Mastercard ···0005', 'reg-5'))
        self.assertTrue(all('forged' not in call['url'] for call in approved.calls))
        self.assertEqual(SubscriptionCycle.objects.get().status, 'paid')
        # Only the patient: staff aren't patients here (403), and the debit order isn't open any more (404).
        self.assertEqual(self.client.get(reverse('portal:patient-card-form', args=['mandate', mandate.pk])).status_code, 404)
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse('portal:patient-payment-return', args=['mandate', mandate.pk])).status_code, 403)

    def test_each_hosted_provider_has_a_webhook_that_refuses_what_it_cant_verify(self):
        # Stitch's address answers too (it may hold debit orders from before a move), but without its secret it refuses.
        self.assertEqual(self.client.post(reverse('portal:payment-webhook', args=['stitch']), 'x', content_type='text/plain').status_code, 400)
        self.assertEqual(self.client.post(reverse('portal:payment-webhook', args=['other']), 'x', content_type='text/plain').status_code, 404)
        with override_settings(PAYMENT_WEBHOOK_SECRET='00' * 32):
            refused = self.client.post(reverse('portal:payment-webhook', args=['peach']), 'abcd', content_type='text/plain',
                                       HTTP_X_INITIALIZATION_VECTOR='00' * 12, HTTP_X_AUTHENTICATION_TAG='00' * 16)
        self.assertEqual(refused.status_code, 400)
        with override_settings(PAYMENT_PROVIDER='sandbox'):
            self.assertEqual(self.client.post(reverse('portal:payment-webhook', args=['sandbox']), '{}', content_type='application/json').status_code, 404)


@override_settings(PAYMENT_PROVIDER='stitch', STITCH_CLIENT_ID='test-client', STITCH_CLIENT_SECRET='secret')
class DebiCheckFilePageTests(BillingPageFixture):
    def test_super_admins_download_the_collection_file_and_upload_stitchs_results(self):
        mandate = DebitMandate.objects.create(company=self.company, patient=self.patient, subscription=self.plan, method='debicheck',
                                              status='active', amount=self.dose.price, provider='stitch', provider_reference='consent-9',
                                              reference='MH-D000009', collection_day=self.today.isoweekday())
        self.plan.next_debit_on = self.today
        self.plan.save()
        from care.billing import bill_subscription

        bill_subscription(self.plan)
        payment = Payment.objects.get()
        self.login(self.admin)
        page = self.client.get(reverse('portal:ops-billing'))
        self.assertIsNone(page.context['debicheck'])
        self.assertEqual(self.client.post(reverse('portal:ops-billing-debicheck'), {'action': 'download'}).status_code, 403)
        self.login(self.super_admin)
        page = self.client.get(reverse('portal:ops-billing'))
        self.assertEqual(page.context['debicheck']['waiting'], 1)
        self.assertContains(page, 'Download collection file')
        download = self.client.post(reverse('portal:ops-billing-debicheck'), {'workflow_context': page.context['debicheck']['context'],
                                                                              'action': 'download'})
        self.assertEqual(download['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn(f'MHP{payment.pk:07d},consent-9,MH-D000009', download.content.decode())
        from django.core.files.uploadedfile import SimpleUploadedFile

        output = ('RECORD_TYPE,CLIENT_ID,PRODUCT,CHANNEL,FILE_TYPE\nP,test-client,COLLECTIONS,DEBICHECK,OUTPUT\n'
                  'RECORD_TYPE,EXTERNAL_COLLECTION_REFERENCE,COLLECTION_ID,COLLECTION_STATUS,COLLECTION_REASON\n'
                  f'D,MHP{payment.pk:07d},COL9,SUCCESS,PROCESSED\n')
        response = self.client.post(reverse('portal:ops-billing-debicheck'), {
            'workflow_context': page.context['debicheck']['context'], 'action': 'upload',
            'results': SimpleUploadedFile('DC_OUTPUT_1_abc123.csv', output.encode(), content_type='text/csv')}, follow=True)
        self.assertContains(response, '1 paid, 0 failed')
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'paid')
