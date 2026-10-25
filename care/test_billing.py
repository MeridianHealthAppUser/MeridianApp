"""Subscription billing, delivery addresses, the weekly lock's address and payment checks, and The Courier Guy."""

from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient

from . import courier
from .addresses import save_delivery_address
from .billing import (
    approve_mandate, bill_subscription, complete_card_payment, mandate_amount, parcel_payment, record_manual_payment,
    retry_now, review_mandates, run_debits, set_next_debit, start_debit_order, start_order_payment,
)
from .models import (
    AuditEvent, ClinicalTask, DebitMandate, DeliveryAddress, EmailDelivery, Invoice, MedicationDose, MedicationProduct,
    PatientSubscription, Payment, PharmacyOrder, PracticeSettings, Shipment, ShipmentItem, SubscriptionCycle,
    TreatmentAuthorization,
)
from .operations import dispatch_shipment, lock_week, prepare_shipment
from .treatment import enroll_local_subscription

ADDRESS = dict(street='12 Kloof Street', suburb='Gardens', city='Cape Town', province='WC', postal_code='8001',
               phone='082 555 0114')


@override_settings(MULTI_PRACTICE_ENABLED=True, PAYMENT_PROVIDER='sandbox')
class BillingFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.today = timezone.localdate()
        cls.company = Company.objects.create(name='Billing Practice', slug='billing-practice')
        cls.doctor = users.create_user(email='billing-doctor@example.test')
        cls.admin = users.create_user(email='billing-admin@example.test')
        cls.patient_user = users.create_user(email='billing-patient@example.test', first_name='Nadia')
        CompanyMembership.objects.create(company=cls.company, user=cls.doctor, role='doctor', clinician_type='doctor')
        CompanyMembership.objects.create(company=cls.company, user=cls.admin, role='practice_admin')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Nadia',
                                             last_name='Mokoena', assigned_doctor=cls.doctor)
        PracticeSettings.objects.create(company=cls.company, doctors_ship=False)
        pen = MedicationProduct.objects.create(company=cls.company, name='Semaglutide', strength='1.0 mg', price='100.00')
        cls.dose = MedicationDose.objects.create(company=cls.company, pen=pen, amount_mg='1', price='3295')

    def setUp(self):
        self.auth = TreatmentAuthorization.objects.create(
            company=self.company, patient=self.patient, prescribed_by=self.doctor, product=self.dose.pen, dose=self.dose,
            max_dose=self.dose.label, quantity_per_cycle=1, starts_on=self.today, expires_on=self.today + timedelta(weeks=24))
        self.plan = enroll_local_subscription(company=self.company, patient=self.patient, actor=self.patient_user,
                                              authorization=self.auth, confirm=True)

    def debit_order(self, *, fail=False):
        mandate, url = start_debit_order(subscription=self.plan, actor=self.patient_user)
        self.assertTrue(url.endswith(f'/patient/billing/sandbox/mandate/{mandate.pk}/'))
        return approve_mandate(mandate=mandate, account_label='Sandbox Bank ···1234', fail_debits=fail, actor=self.patient_user)


class DebitOrderTests(BillingFixture):
    def test_approving_the_debit_order_collects_the_first_cycle_and_sets_the_next_debit(self):
        mandate = self.debit_order()
        self.assertEqual((mandate.status, mandate.amount, mandate.interval_weeks), ('active', Decimal('3295'), 4))
        self.assertTrue(mandate.reference.startswith('MH-D'))
        cycle = SubscriptionCycle.objects.get()
        self.assertEqual((cycle.cycle_number, cycle.status, cycle.amount, cycle.starts_on, cycle.ends_on),
                         (1, 'paid', Decimal('3295'), self.today, self.today + timedelta(weeks=4, days=-1)))
        self.assertEqual(Invoice.objects.get(subscription_cycle=cycle).status, 'paid')
        self.plan.refresh_from_db()
        self.assertEqual((self.plan.cycle_number, self.plan.next_debit_on), (1, self.today + timedelta(weeks=4)))
        payment = Payment.objects.get()
        self.assertEqual((payment.kind, payment.method, payment.status, payment.attempt), ('subscription', 'debicheck', 'paid', 1))
        # Nothing more is due until the next debit date; the run is idempotent.
        self.assertEqual(run_debits(self.company)['tried'], 0)
        self.assertEqual(SubscriptionCycle.objects.count(), 1)

    def test_the_next_cycle_opens_on_its_debit_date(self):
        self.debit_order()
        counts = run_debits(self.company, today=self.today + timedelta(weeks=4))
        self.assertEqual((counts['tried'], counts['paid']), (1, 1))
        self.assertEqual(list(SubscriptionCycle.objects.order_by('cycle_number').values_list('cycle_number', 'status')),
                         [(1, 'paid'), (2, 'paid')])

    def test_a_failed_debit_is_retried_then_goes_to_the_failure_queue_with_a_task_and_emails(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.debit_order(fail=True)
        cycle = SubscriptionCycle.objects.get()
        self.plan.refresh_from_db()
        self.assertEqual((cycle.status, cycle.attempts, cycle.next_attempt_on), ('pending', 1, self.today + timedelta(days=3)))
        self.assertEqual(self.plan.status, 'payment_retry')
        self.assertEqual([message.subject for message in mail.outbox], ['We couldn’t collect your payment'])
        # The email is logged against the patient.
        self.assertTrue(EmailDelivery.objects.filter(patient=self.patient, kind='payment_failed').exists())
        # Not before its retry day.
        self.assertEqual(run_debits(self.company, today=self.today + timedelta(days=1))['tried'], 0)
        run_debits(self.company, today=self.today + timedelta(days=3))
        run_debits(self.company, today=self.today + timedelta(days=7))
        cycle.refresh_from_db()
        self.assertEqual((cycle.status, cycle.attempts, cycle.next_attempt_on), ('failed', 3, None))
        self.assertTrue(ClinicalTask.objects.filter(patient=self.patient, title__startswith='Debit failed').exists())
        # In the failure queue it isn't retried by the run.
        self.assertEqual(run_debits(self.company, today=self.today + timedelta(days=8))['tried'], 0)

    def test_a_new_debit_order_collects_a_failed_payment_straight_away(self):
        self.debit_order(fail=True)
        self.debit_order()
        cycle = SubscriptionCycle.objects.get()
        self.plan.refresh_from_db()
        self.assertEqual((cycle.status, self.plan.status), ('paid', 'active'))
        self.assertEqual(DebitMandate.objects.filter(status='cancelled').count(), 1)

    def test_the_practice_records_a_payment_received_another_way_or_retries(self):
        self.debit_order(fail=True)
        cycle = SubscriptionCycle.objects.get()
        with self.assertRaises(PermissionDenied):
            record_manual_payment(company=self.company, actor=self.doctor, cycle=cycle, amount='3295', method='eft')
        # Retrying now tries the same (failing) debit order again.
        self.assertEqual(retry_now(cycle=cycle, actor=self.admin).status, 'failed')
        payment = record_manual_payment(company=self.company, actor=self.admin, cycle=cycle, amount='3295.00',
                                        method='eft', reference='EFT 123')
        cycle.refresh_from_db()
        self.assertEqual((payment.kind, payment.recorded_by, cycle.status), ('manual', self.admin, 'paid'))
        self.assertTrue(AuditEvent.objects.filter(action='billing.payment_recorded').exists())

    def test_by_default_any_prescription_or_price_change_asks_for_a_new_debit_order_but_a_renewal_doesnt(self):
        mandate = self.debit_order()
        # A renewal at the same price changes nothing.
        self.assertEqual(review_mandates(self.company, self.patient), [])
        # A changed prescription does, even at the same price.
        self.assertEqual(review_mandates(self.company, self.patient, prescription_changed=True), [mandate])
        mandate.refresh_from_db()
        self.assertEqual(mandate.status, 'update_needed')
        # So does a lower price.
        mandate.status = 'active'
        mandate.save()
        MedicationDose.objects.filter(pk=self.dose.pk).update(price='2995')
        self.assertEqual(review_mandates(self.company), [mandate])

    def test_the_practice_can_ask_only_when_the_amount_goes_above_what_was_approved(self):
        mandate = self.debit_order()
        PracticeSettings.objects.filter(company=self.company).update(new_debit_order='higher')
        MedicationDose.objects.filter(pk=self.dose.pk).update(price='2995')
        self.assertEqual(review_mandates(self.company, self.patient, prescription_changed=True), [])
        MedicationDose.objects.filter(pk=self.dose.pk).update(price='3495')
        self.assertEqual(review_mandates(self.company, self.patient, prescription_changed=True), [mandate])

    def test_a_higher_price_asks_for_a_new_debit_order_and_holds_collection(self):
        mandate = self.debit_order()
        MedicationDose.objects.filter(pk=self.dose.pk).update(price='4295')
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(review_mandates(self.company), [mandate])
        mandate.refresh_from_db()
        self.assertEqual(mandate.status, 'update_needed')
        self.assertIn('Please approve your new debit order', [message.subject for message in mail.outbox])
        run_debits(self.company, today=self.today + timedelta(weeks=4))
        waiting = SubscriptionCycle.objects.get(cycle_number=2)
        self.assertEqual((waiting.status, waiting.attempts), ('pending', 0))
        self.assertIn('new amount', waiting.failure_reason)
        # Approving the new amount collects it.
        self.plan.refresh_from_db()
        self.assertEqual(mandate_amount(self.plan), Decimal('4295'))
        mandate, _ = start_debit_order(subscription=self.plan, actor=self.patient_user)
        approve_mandate(mandate=mandate)
        waiting.refresh_from_db()
        self.assertEqual(waiting.status, 'paid')

    def test_only_the_patient_starts_their_debit_order_and_staff_move_the_next_debit(self):
        with self.assertRaises(PermissionDenied):
            start_debit_order(subscription=self.plan, actor=self.admin)
        with override_settings(PAYMENT_PROVIDER=''):
            with self.assertRaises(ValidationError):
                start_debit_order(subscription=self.plan, actor=self.patient_user)
        self.debit_order()
        with self.assertRaises(PermissionDenied):
            set_next_debit(subscription=self.plan, actor=self.patient_user, day=self.today)
        set_next_debit(subscription=self.plan, actor=self.admin, day=self.today + timedelta(days=10))
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.next_debit_on, self.today + timedelta(days=10))

    def test_pausing_stops_debits_resuming_carries_on_after_the_paid_period_and_cancelling_ends_the_debit_order(self):
        from .treatment import change_subscription_status

        self.debit_order()
        change_subscription_status(subscription=self.plan, actor=self.patient_user, action='pause', confirm=True)
        self.assertEqual(run_debits(self.company, today=self.today + timedelta(weeks=4))['tried'], 0)
        self.plan.refresh_from_db()
        change_subscription_status(subscription=self.plan, actor=self.patient_user, action='resume', confirm=True)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.next_debit_on, self.today + timedelta(weeks=4))
        change_subscription_status(subscription=self.plan, actor=self.patient_user, action='cancel', confirm=True,
                                   reasons=['cost'])
        self.assertFalse(DebitMandate.objects.exclude(status='cancelled').exists())

    def test_the_run_debits_command(self):
        self.debit_order()
        out = StringIO()
        with override_settings(MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='billing-practice'):
            call_command('run_debits', stdout=out)
        self.assertIn('Billing Practice: 0 debits tried', out.getvalue())
        with override_settings(PAYMENT_PROVIDER=''):
            out = StringIO()
            call_command('run_debits', stdout=out)
            self.assertIn('No payment provider', out.getvalue())


class ParcelPaymentTests(BillingFixture):
    def parcel(self, **values):
        shipment = Shipment.objects.create(company=self.company, patient=self.patient, subscription=self.plan,
                                           authorization=self.auth, scheduled_for=values.pop('day', self.today), **values)
        ShipmentItem.objects.create(company=self.company, shipment=shipment, product=self.dose.pen, dose=self.auth.max_dose, quantity=1)
        return shipment

    def test_a_parcel_is_paid_when_the_cycle_covering_its_day_is(self):
        parcel = self.parcel()
        self.assertEqual(parcel_payment(parcel)[0], 'unpaid')
        self.debit_order()
        self.assertEqual(parcel_payment(parcel), ('paid', 'Paid'))
        later = Shipment.objects.get(pk=self.parcel(day=self.today + timedelta(weeks=5)).pk)
        self.assertEqual(parcel_payment(later)[0], 'due')
        no_plan = Shipment.objects.create(company=self.company, patient=self.patient, scheduled_for=self.today)
        self.assertEqual(parcel_payment(no_plan)[0], 'unpaid')

    def test_lock_checks_address_contact_and_payment_and_can_hold_the_rest(self):
        good = prepare_shipment(shipment=self.parcel(), actor=self.admin)
        with self.assertRaises(ValidationError) as raised:
            lock_week(company=self.company, actor=self.admin, week_start=self.today - timedelta(days=self.today.weekday()))
        self.assertIn('No delivery address.', ' '.join(raised.exception.messages))
        self.assertIn('Not paid', ' '.join(raised.exception.messages))
        save_delivery_address(company=self.company, patient=self.patient, actor=self.patient_user, data=ADDRESS)
        self.debit_order()
        result = lock_week(company=self.company, actor=self.admin, week_start=self.today - timedelta(days=self.today.weekday()))
        self.assertEqual([parcel.pk for parcel in result['locked']], [good.pk])

    def test_hold_failing_holds_the_unpaid_parcel_with_its_reason(self):
        prepare_shipment(shipment=self.parcel(), actor=self.admin)
        result = lock_week(company=self.company, actor=self.admin, hold_failing=True,
                           week_start=self.today - timedelta(days=self.today.weekday()))
        self.assertEqual(result['locked'], [])
        held = Shipment.objects.get()
        self.assertEqual(held.status, 'held')
        self.assertIn('Not paid', held.hold_reason)

    def test_payment_is_optional_when_the_practice_says_so(self):
        PracticeSettings.objects.filter(company=self.company).update(require_payment=False)
        save_delivery_address(company=self.company, patient=self.patient, actor=self.admin, data=ADDRESS)
        prepare_shipment(shipment=self.parcel(), actor=self.admin)
        result = lock_week(company=self.company, actor=self.admin, week_start=self.today - timedelta(days=self.today.weekday()))
        self.assertEqual(len(result['locked']), 1)
        # Dispatch snapshots where it went.
        shipment = dispatch_shipment(shipment=result['locked'][0], actor=self.admin, tracking_number='TCG-1', confirm=True)
        self.assertEqual(shipment.dispatch_snapshot['delivery_address']['phone'], '082 555 0114')

    def test_a_non_prescription_request_is_paid_by_card(self):
        order = PharmacyOrder.objects.create(company=self.company, patient=self.patient, status='submitted', subtotal='240.00')
        payment, url = start_order_payment(order=order, actor=self.patient_user)
        self.assertTrue(url.endswith(f'/patient/billing/sandbox/checkout/{payment.pk}/'))
        complete_card_payment(payment=payment, paid=True, reference='sandbox')
        order.refresh_from_db()
        self.assertIsNotNone(order.paid_at)
        parcel = Shipment.objects.create(company=self.company, patient=self.patient, scheduled_for=self.today)
        order.shipment = parcel
        order.save()
        self.assertEqual(parcel_payment(parcel)[0], 'paid')


class AddressTests(BillingFixture):
    def test_the_patient_and_practice_admins_keep_the_address_with_an_audit_of_fields_only(self):
        address, changed = save_delivery_address(company=self.company, patient=self.patient, actor=self.patient_user, data=ADDRESS)
        self.assertIn('street', changed)
        audit = AuditEvent.objects.get(action='delivery_address.saved')
        self.assertNotIn('Kloof', str(audit.metadata))
        save_delivery_address(company=self.company, patient=self.patient, actor=self.admin, data={**ADDRESS, 'suburb': 'Tamboerskloof'})
        self.assertEqual(DeliveryAddress.objects.get().suburb, 'Tamboerskloof')
        with self.assertRaises(PermissionDenied):
            save_delivery_address(company=self.company, patient=self.patient, actor=self.doctor, data=ADDRESS)
        for bad in ({'postal_code': '80'}, {'phone': 'call me'}, {'province': 'XX'}, {'street': ''}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                save_delivery_address(company=self.company, patient=self.patient, actor=self.patient_user, data={**ADDRESS, **bad})


@override_settings(TCG_API_KEY='test-key', TCG_API_URL='https://sandbox.example.test')
class CourierTests(BillingFixture):
    def setUp(self):
        super().setUp()
        PracticeSettings.objects.filter(company=self.company).update(
            collection_street='1 Practice Road', collection_suburb='Claremont', collection_city='Cape Town',
            collection_province='WC', collection_postal_code='7708', collection_contact='Dispensary', collection_phone='021 555 0100')
        save_delivery_address(company=self.company, patient=self.patient, actor=self.patient_user, data=ADDRESS)
        self.debit_order()
        shipment = Shipment.objects.create(company=self.company, patient=self.patient, subscription=self.plan,
                                           authorization=self.auth, scheduled_for=self.today)
        ShipmentItem.objects.create(company=self.company, shipment=shipment, product=self.dose.pen, dose=self.auth.max_dose, quantity=1)
        prepare_shipment(shipment=shipment, actor=self.admin)
        self.week = self.today - timedelta(days=self.today.weekday())
        lock_week(company=self.company, actor=self.admin, week_start=self.week)
        self.shipment = shipment

    def test_waybills_are_booked_for_locked_parcels_with_the_practice_and_patient_addresses(self):
        with patch.object(courier, '_call', return_value={'id': 991, 'short_tracking_reference': 'TCG91', 'status': 'submitted'}) as call:
            booked, failed = courier.book_waybills(company=self.company, actor=self.admin, week_start=self.week)
        self.assertEqual((len(booked), failed), (1, []))
        method, path, payload = call.call_args.args
        self.assertEqual((method, path), ('POST', '/v2/shipments'))
        self.assertEqual(payload['delivery_address']['street_address'], '12 Kloof Street')
        self.assertEqual(payload['collection_address']['local_area'], 'Claremont')
        self.assertEqual(payload['delivery_contact']['mobile_number'], '082 555 0114')
        self.shipment.refresh_from_db()
        self.assertEqual((self.shipment.waybill, self.shipment.courier_shipment_id), ('TCG91', '991'))
        # Booked parcels aren't booked twice.
        with self.assertRaises(ValidationError):
            courier.book_waybills(company=self.company, actor=self.admin, week_start=self.week)

    def test_the_booking_asks_for_collection_from_the_start_of_the_parcels_day_in_south_africa(self):
        payload = courier.shipment_payload(self.shipment, self.patient.delivery_address, PracticeSettings.objects.get(company=self.company))
        day = self.shipment.scheduled_for
        self.assertEqual(payload['collection_min_date'], f'{day - timedelta(days=1):%Y-%m-%d}T22:00:00.000Z')
        self.assertEqual((payload['service_level_code'], payload['customer_reference']), ('ECO', f'MH-{self.shipment.pk}'))

    def test_the_api_is_called_with_the_bearer_key_and_its_errors_read_as_json_or_text(self):
        from io import BytesIO
        from urllib.error import HTTPError

        response = BytesIO(b'{"id": 7, "short_tracking_reference": "AB12"}')
        with patch('care.courier.urlrequest.urlopen', return_value=response) as urlopen:
            self.assertEqual(courier._call('POST', '/v2/shipments', {'a': 1})['short_tracking_reference'], 'AB12')
        sent = urlopen.call_args.args[0]
        self.assertEqual((sent.full_url, sent.get_method()), ('https://sandbox.example.test/v2/shipments', 'POST'))
        self.assertEqual(sent.get_header('Authorization'), 'Bearer test-key')
        for body, shown in ((b'{"message": "zone is required"}', 'zone is required'), (b'Bad Request', 'Bad Request')):
            error = HTTPError('https://sandbox.example.test/v2/shipments', 400, 'Bad Request', {}, BytesIO(body))
            with patch('care.courier.urlrequest.urlopen', side_effect=error):
                with self.assertRaisesMessage(courier.CourierError, f'answered 400: {shown}'):
                    courier._call('POST', '/v2/shipments', {})

    def test_holding_a_booked_parcel_cancels_its_booking_and_a_collected_one_cant_be_held(self):
        from .operations import hold_or_cancel_shipment

        with patch.object(courier, '_call', return_value={'id': 991, 'short_tracking_reference': 'TCG91'}):
            courier.book_waybills(company=self.company, actor=self.admin, week_start=self.week)
        self.shipment.refresh_from_db()
        with patch.object(courier, '_call', side_effect=courier.CourierError('The Courier Guy answered 400: already collected')):
            with self.assertRaisesMessage(ValidationError, 'already collected'):
                hold_or_cancel_shipment(shipment=self.shipment, actor=self.admin, reason='Patient away')
        self.shipment.refresh_from_db()
        self.assertEqual((self.shipment.status, self.shipment.waybill), ('ready', 'TCG91'))
        with patch.object(courier, '_call', return_value={}) as call:
            hold_or_cancel_shipment(shipment=self.shipment, actor=self.admin, reason='Patient away')
        call.assert_called_once_with('POST', '/v2/shipments/cancel', {'tracking_reference': 'TCG91'})
        self.shipment.refresh_from_db()
        self.assertEqual((self.shipment.status, self.shipment.waybill, self.shipment.courier_status), ('held', '', 'cancelled'))

    def test_tracking_webhooks_keep_the_courier_status_and_mark_a_delivered_parcel_delivered(self):
        from django.test import Client
        from django.urls import reverse

        with patch.object(courier, '_call', return_value={'id': 991, 'short_tracking_reference': 'TCG91'}):
            courier.book_waybills(company=self.company, actor=self.admin, week_start=self.week)
        client = Client()
        event = {'shipment_id': 991, 'short_tracking_reference': 'TCG91', 'status': 'at-hub', 'tracking_events': []}
        with override_settings(TCG_WEBHOOK_TOKEN='tracking-secret'):
            self.assertEqual(client.post(reverse('portal:courier-webhook', args=['wrong']), event, content_type='application/json').status_code, 404)
            self.assertEqual(client.post(reverse('portal:courier-webhook', args=['tracking-secret']), event, content_type='application/json').content, b'1')
            self.shipment.refresh_from_db()
            self.assertEqual((self.shipment.courier_status_label, self.shipment.status), ('At the hub', 'ready'))
            dispatch_shipment(shipment=self.shipment, actor=self.admin, tracking_number='TCG91', confirm=True)
            client.post(reverse('portal:courier-webhook', args=['tracking-secret']), {**event, 'status': 'delivered'},
                        content_type='application/json')
        self.shipment.refresh_from_db()
        self.assertEqual((self.shipment.status, self.shipment.courier_status), ('delivered', 'delivered'))
        self.assertTrue(AuditEvent.objects.filter(action='shipment.delivered', metadata__source='courier').exists())
        # Without a token set, the webhook isn't there at all.
        self.assertEqual(Client().post(reverse('portal:courier-webhook', args=['tracking-secret']), event, content_type='application/json').status_code, 404)

    def test_a_failed_booking_is_kept_on_the_parcel(self):
        with patch.object(courier, '_call', side_effect=courier.CourierError('The Courier Guy answered 422: bad zone')):
            booked, failed = courier.book_waybills(company=self.company, actor=self.admin, week_start=self.week)
        self.assertEqual((booked, len(failed)), ([], 1))
        self.shipment.refresh_from_db()
        self.assertIn('bad zone', self.shipment.courier_error)

    def test_nothing_is_booked_without_a_key_or_a_collection_address(self):
        with override_settings(TCG_API_KEY=''):
            with self.assertRaises(ValidationError):
                courier.book_waybills(company=self.company, actor=self.admin, week_start=self.week)
        PracticeSettings.objects.filter(company=self.company).update(collection_street='')
        with self.assertRaises(ValidationError):
            courier.book_waybills(company=self.company, actor=self.admin, week_start=self.week)
