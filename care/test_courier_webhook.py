"""Courier callbacks update the linked parcel without repeating dispatch or subscription actions."""
from datetime import timedelta
from unittest.mock import patch

from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from . import courier
from .models import AuditEvent, PatientEvent, PracticeSettings, Shipment, StockMovement
from .operations import dispatch_shipment, hold_or_cancel_shipment
from .test_operations import OperationsFixture


@override_settings(TCG_WEBHOOK_TOKEN='courier-webhook-test-secret')
class CourierWebhookTests(OperationsFixture):
    def setUp(self):
        # Courier callbacks authenticate with the configured endpoint token, without a browser session or CSRF token.
        self.client = Client(enforce_csrf_checks=True)
        self.url = reverse('portal:courier-webhook', args=['courier-webhook-test-secret'])
        self.event_time = timezone.now() - timedelta(minutes=5)

    def dispatched(self, **kwargs):
        defaults = dict(status=Shipment.Status.DISPATCHED, dispatched_at=timezone.now() - timedelta(days=1),
                        tracking_number='TCG-101', waybill='TCG-101', courier_shipment_id='101')
        defaults.update(kwargs)
        return self.shipment(**defaults)

    def event(self, status='in-transit', **kwargs):
        return dict(shipment_id=101, short_tracking_reference='TCG-101', status=status,
                    shipment_time_modified=self.event_time.isoformat(), **kwargs)

    def post(self, payload, url=None):
        return self.client.post(url or self.url, payload, content_type='application/json')

    def test_token_is_required_and_disabled_webhook_is_not_accessible(self):
        parcel = self.dispatched()
        bad_url = reverse('portal:courier-webhook', args=['wrong-token'])
        self.assertEqual(self.post(self.event(), bad_url).status_code, 404)
        with override_settings(TCG_WEBHOOK_TOKEN=''):
            self.assertEqual(self.post(self.event()).status_code, 404)
        parcel.refresh_from_db()
        self.assertEqual(parcel.courier_status, '')
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_authenticated_callback_updates_linked_parcel_without_csrf_or_login(self):
        parcel = self.dispatched()
        response = self.post(self.event())
        self.assertEqual((response.status_code, response.content), (200, b'1'))
        parcel.refresh_from_db()
        self.assertEqual((parcel.courier_status, parcel.status, parcel.subscription_id),
                         ('in-transit', Shipment.Status.DISPATCHED, self.plan.pk))
        self.assertEqual(parcel.courier_status_updated_at, self.event_time)
        self.assertFalse(AuditEvent.objects.filter(action='subscription.cancel').exists())
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.status, 'active')

    def test_delivered_callback_records_carrier_time_once_and_does_not_deduct_stock_again(self):
        parcel = self.locked(self.shipment(quantity=1))
        parcel = dispatch_shipment(shipment=parcel, actor=self.admin, tracking_number='TCG-101', confirm=True)
        Shipment.objects.filter(pk=parcel.pk).update(waybill='TCG-101', courier_shipment_id='101')
        movements_before = StockMovement.objects.count()
        delivered_at = self.event_time - timedelta(minutes=1)
        event = self.event('delivered', shipment_delivered_date=delivered_at.isoformat())
        for _ in range(2):
            response = self.post([event])
            self.assertEqual((response.status_code, response.content), (200, b'1'))
        parcel.refresh_from_db()
        self.assertEqual((parcel.status, parcel.courier_status), (Shipment.Status.DELIVERED, 'delivered'))
        self.assertEqual(parcel.delivered_at, delivered_at)
        self.assertEqual(StockMovement.objects.count(), movements_before)
        self.assertEqual(AuditEvent.objects.filter(action='shipment.delivered', target_id=str(parcel.pk)).count(), 1)
        self.assertEqual(PatientEvent.objects.filter(source_type='care.shipment', source_id=str(parcel.pk),
                                                     title='Shipment delivered').count(), 1)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.status, 'active')

    def test_delivered_event_does_not_dispatch_a_ready_parcel(self):
        parcel = self.dispatched(status=Shipment.Status.READY, dispatched_at=None)
        self.assertEqual(self.post(self.event('delivered')).content, b'1')
        parcel.refresh_from_db()
        self.assertEqual((parcel.status, parcel.delivered_at), (Shipment.Status.READY, None))
        self.assertFalse(AuditEvent.objects.filter(action='shipment.delivered').exists())

    def test_stale_event_cannot_overwrite_newer_tracking_or_delivery(self):
        parcel = self.dispatched()
        delivered = self.event('delivered', shipment_delivered_date=self.event_time.isoformat())
        self.assertEqual(self.post(delivered).status_code, 200)
        earlier = self.event_time - timedelta(hours=1)
        for event in (
            {**self.event('in-transit'), 'shipment_time_modified': earlier.isoformat()},
            {**self.event('at-hub'), 'shipment_time_modified': self.event_time.isoformat()},
            {'short_tracking_reference': 'TCG-101', 'status': 'in-transit'},
        ):
            self.assertEqual(self.post(event).status_code, 200)
        parcel.refresh_from_db()
        self.assertEqual((parcel.courier_status, parcel.status, parcel.delivered_at),
                         ('delivered', Shipment.Status.DELIVERED, self.event_time))

    def test_later_return_updates_courier_status_without_reopening_delivered_parcel(self):
        parcel = self.dispatched()
        self.post(self.event('delivered'))
        later = self.event_time + timedelta(minutes=1)
        self.post({**self.event('returned-to-sender'), 'shipment_time_modified': later.isoformat()})
        parcel.refresh_from_db()
        self.assertEqual((parcel.courier_status, parcel.status), ('returned-to-sender', Shipment.Status.DELIVERED))

    def test_manual_external_tracking_reference_can_match_dispatched_parcel(self):
        parcel = self.dispatched(waybill='', courier_shipment_id='')
        event = {'short_tracking_reference': 'TCG-101', 'status': 'out-for-delivery',
                 'shipment_time_modified': self.event_time.isoformat()}
        self.assertEqual(self.post(event).content, b'1')
        parcel.refresh_from_db()
        self.assertEqual(parcel.courier_status, 'out-for-delivery')

    def test_conflicting_and_ambiguous_references_do_not_update_either_practice(self):
        parcel = self.dispatched()
        other = self.shipment(company=self.beta, patient=self.beta_patient, product=self.beta_product,
                              subscription=None, status=Shipment.Status.DISPATCHED, waybill='TCG-202',
                              courier_shipment_id='202', tracking_number='TCG-202')
        conflict = {**self.event(), 'short_tracking_reference': 'TCG-202'}
        self.assertEqual(self.post(conflict).content, b'0')
        Shipment.objects.filter(pk=other.pk).update(waybill='TCG-101', tracking_number='TCG-101')
        self.assertEqual(self.post({'short_tracking_reference': 'TCG-101', 'status': 'delivered'}).content, b'0')
        for item in (parcel, other):
            item.refresh_from_db()
            self.assertEqual(item.courier_status, '')
            self.assertEqual(item.status, Shipment.Status.DISPATCHED)

    def test_batch_accepts_unmatched_events_and_updates_matching_ones(self):
        parcel = self.dispatched()
        unknown = {**self.event(), 'shipment_id': 999999, 'short_tracking_reference': 'UNKNOWN'}
        self.assertEqual(self.post([unknown, self.event()]).content, b'1')
        parcel.refresh_from_db()
        self.assertEqual(parcel.courier_status, 'in-transit')

    def test_malformed_payloads_return_400_without_partial_batch_updates(self):
        parcel = self.dispatched()
        malformed = [
            'not-json', 'null', '17', '"status"', 'true',
            {}, {'status': ['delivered'], 'shipment_id': 101},
            {**self.event(), 'shipment_id': {'id': 101}},
            {**self.event(), 'shipment_time_modified': 'not-a-date'},
            {**self.event('delivered'), 'shipment_delivered_date': 'not-a-date'},
            [self.event(), {'status': False, 'shipment_id': 101}],
        ]
        for payload in malformed:
            with self.subTest(payload=payload):
                self.assertEqual(self.post(payload).status_code, 400)
                parcel.refresh_from_db()
                self.assertEqual(parcel.courier_status, '')
                self.assertEqual(parcel.status, Shipment.Status.DISPATCHED)

    def test_delivered_before_staff_dispatch_reconciles_on_equal_timestamp_retry(self):
        parcel = self.locked(self.shipment(quantity=1))
        Shipment.objects.filter(pk=parcel.pk).update(waybill='TCG-101', courier_shipment_id='101')
        event = self.event('delivered', shipment_delivered_date=self.event_time.isoformat())
        self.assertEqual(self.post(event).content, b'1')
        parcel.refresh_from_db()
        self.assertEqual((parcel.status, parcel.courier_status, parcel.courier_status_updated_at),
                         (Shipment.Status.READY, 'delivered', self.event_time))
        self.assertIsNone(parcel.delivered_at)
        dispatch_shipment(shipment=parcel, actor=self.admin, tracking_number='TCG-101', confirm=True)
        movements_after_dispatch = StockMovement.objects.count()
        for _ in range(2):
            self.assertEqual(self.post(event).content, b'1')
        parcel.refresh_from_db()
        self.assertEqual((parcel.status, parcel.delivered_at), (Shipment.Status.DELIVERED, self.event_time))
        self.assertEqual(StockMovement.objects.count(), movements_after_dispatch)
        self.assertEqual(AuditEvent.objects.filter(action='shipment.delivered', target_id=str(parcel.pk)).count(), 1)
        self.assertEqual(PatientEvent.objects.filter(title='Shipment delivered', source_type='care.shipment',
                                                     source_id=str(parcel.pk)).count(), 1)

    def test_unicode_invalid_endpoint_token_returns_404_without_server_error(self):
        parcel = self.dispatched()
        unicode_url = reverse('portal:courier-webhook', args=['incorrect-🔒-token'])
        self.assertEqual(self.post(self.event(), unicode_url).status_code, 404)
        parcel.refresh_from_db()
        self.assertEqual(parcel.courier_status, '')

    @override_settings(TCG_API_KEY='test-key', TCG_API_URL='https://sandbox.example.test')
    def test_cancel_and_rebook_clear_old_courier_timestamp(self):
        PracticeSettings.objects.filter(company=self.company).update(
            collection_street='1 Practice Road', collection_suburb='Claremont', collection_city='Cape Town',
            collection_province='WC', collection_postal_code='7708', collection_contact='Dispensary',
            collection_phone='021 555 0100')
        parcel = self.locked(self.shipment(quantity=1))
        Shipment.objects.filter(pk=parcel.pk).update(waybill='TCG-101', courier_shipment_id='101')
        self.post(self.event())
        with patch.object(courier, '_call', return_value={}):
            hold_or_cancel_shipment(shipment=parcel, actor=self.admin, reason='Reschedule collection')
        parcel.refresh_from_db()
        self.assertEqual((parcel.courier_status, parcel.waybill), ('cancelled', ''))
        self.assertIsNone(parcel.courier_status_updated_at)
        parcel = self.locked(parcel)
        with patch.object(courier, '_call', return_value={
                'id': 202, 'short_tracking_reference': 'TCG-202', 'status': 'submitted'}):
            booked, failed = courier.book_waybills(company=self.company, actor=self.admin, week_start=self.today)
        self.assertEqual(([item.pk for item in booked], failed), ([parcel.pk], []))
        parcel.refresh_from_db()
        self.assertIsNone(parcel.courier_status_updated_at)
        earlier = self.event_time - timedelta(days=1)
        replacement_event = {**self.event('at-hub'), 'shipment_id': 202,
                             'short_tracking_reference': 'TCG-202', 'shipment_time_modified': earlier.isoformat()}
        self.assertEqual(self.post(replacement_event).content, b'1')
        parcel.refresh_from_db()
        self.assertEqual((parcel.courier_status, parcel.courier_status_updated_at), ('at-hub', earlier))
        self.assertEqual(self.post(self.event('delivered')).content, b'0')
