"""The combined shipping confirmation endpoint validates scope and moves selected parcels atomically."""
from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

from django.urls import reverse
from django.utils import timezone

from care.models import AuditEvent, Shipment, StockMovement
from .test_operations import OperationsPortalFixture


class ShippingConfirmationTests(OperationsPortalFixture):
    def setUp(self):
        super().setUp()
        self.week = self.today - timedelta(days=self.today.weekday())
        self.url = reverse('portal:ops-shipping-confirm')

    def pending(self, **params):
        return self.page('ops-shipping', week=self.week, shipping_step='pending', **params)

    def data(self, page, parcels, **extra):
        return {'workflow_context': page.context['confirm_context'], 'week': self.week,
                'shipment_ids': [parcel.pk for parcel in parcels], **extra}

    def test_confirm_locks_selected_draft_held_and_ready_without_dispatch_or_stock_deduction(self):
        draft = self.shipment(quantity=1)
        held = self.shipment(quantity=1, status=Shipment.Status.HELD, hold_reason='Old preparation hold')
        ready = self.shipment(quantity=1, status=Shipment.Status.READY)
        left = self.shipment(quantity=1)
        before = StockMovement.objects.count()
        response = self.client.post(self.url, self.data(self.pending(), [draft, held, ready]))
        self.assertEqual(response.status_code, 302)
        for parcel in (draft, held, ready):
            parcel.refresh_from_db()
            self.assertEqual(parcel.status, Shipment.Status.READY)
            self.assertIsNotNone(parcel.locked_at)
            self.assertIsNone(parcel.dispatched_at)
            self.assertEqual(parcel.hold_reason, '')
        left.refresh_from_db()
        self.assertEqual(left.status, Shipment.Status.DRAFT)
        self.assertEqual(StockMovement.objects.count(), before)
        self.assertEqual(AuditEvent.objects.filter(action='shipment.locked').count(), 3)

    def test_invalid_second_selected_parcel_does_not_confirm_first(self):
        valid = self.shipment(quantity=1)
        unpaid = self.shipment(quantity=1, subscription=None)
        response = self.client.post(self.url, self.data(self.pending(), [valid, unpaid]))
        self.assertEqual(response.status_code, 302)
        for parcel in (valid, unpaid):
            parcel.refresh_from_db()
            self.assertEqual(parcel.status, Shipment.Status.DRAFT)
            self.assertIsNone(parcel.locked_at)
        self.assertFalse(AuditEvent.objects.filter(action='shipment.locked').exists())
        self.assertContains(self.pending(), 'Not paid')

    def test_foreign_or_other_week_or_already_locked_selection_does_not_change_valid_parcel(self):
        valid = self.shipment(quantity=1)
        foreign = self.shipment(company=self.beta, patient=self.beta_patient, product=self.beta_product, subscription=None, quantity=1)
        future = self.shipment(quantity=1, scheduled_for=self.week + timedelta(days=7))
        locked = self.shipment(quantity=1, status=Shipment.Status.READY, locked_at=timezone.now())
        for rejected in (foreign, future, locked):
            with self.subTest(rejected=rejected.pk):
                response = self.client.post(self.url, self.data(self.pending(), [valid, rejected]))
                self.assertEqual(response.status_code, 302)
                valid.refresh_from_db()
                self.assertEqual(valid.status, Shipment.Status.DRAFT)
                self.assertIsNone(valid.locked_at)

    def test_per_row_confirmation_accepts_only_its_record_token(self):
        selected = self.shipment(quantity=1)
        other = self.shipment(quantity=1)
        page = self.pending()
        row = next(parcel for parcel in page.context['shipments'] if parcel.pk == selected.pk)
        response = self.client.post(self.url, {'workflow_context': row.confirm_context, 'week': self.week,
                                              'single_shipment': selected.pk, 'shipment_ids': [selected.pk]})
        self.assertEqual(response.status_code, 302)
        selected.refresh_from_db()
        self.assertIsNotNone(selected.locked_at)
        response = self.client.post(self.url, {'workflow_context': row.confirm_context, 'week': self.week,
                                              'single_shipment': other.pk, 'shipment_ids': [other.pk]})
        self.assertEqual(response.status_code, 302)
        other.refresh_from_db()
        self.assertIsNone(other.locked_at)

    def test_missing_or_wrong_token_and_get_do_not_confirm_any_parcel(self):
        parcel = self.shipment(quantity=1)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        for token in ('', 'not-a-valid-context'):
            response = self.client.post(self.url, {'workflow_context': token, 'week': self.week,
                                                  'shipment_ids': [parcel.pk]})
            self.assertEqual(response.status_code, 302)
            parcel.refresh_from_db()
            self.assertEqual(parcel.status, Shipment.Status.DRAFT)
            self.assertIsNone(parcel.locked_at)

    def test_doctor_without_shipping_permission_cannot_confirm(self):
        parcel = self.shipment(quantity=1)
        page = self.pending()
        self.login(self.doctor)
        response = self.client.post(self.url, self.data(page, [parcel]))
        self.assertEqual(response.status_code, 403)
        parcel.refresh_from_db()
        self.assertIsNone(parcel.locked_at)

    def test_bulk_confirmation_return_keeps_filters_and_pending_stage(self):
        parcel = self.shipment(quantity=1, authorization=self.auth)
        page = self.pending(product=self.product.pk, doctor=self.doctor.pk, payment='paid', view='medication')
        query = dict(week=self.week.isoformat(), product=[self.product.pk], doctor=[self.doctor.pk],
                     payment=['paid'], view='medication', shipping_step='pending')
        response = self.client.post(self.url, self.data(page, [parcel], list_query=urlencode(query, doseq=True)))
        self.assertEqual(response.status_code, 302)
        returned = parse_qs(urlsplit(response['Location']).query)
        for key in ('product', 'doctor', 'payment'):
            self.assertEqual(returned[key], [str(value) for value in query[key]])
        self.assertEqual(returned['view'], ['medication'])
        self.assertEqual(returned['shipping_step'], ['pending'])

    def test_empty_selection_rejected_without_touching_pending_parcels(self):
        parcel = self.shipment(quantity=1)
        response = self.client.post(self.url, self.data(self.pending(), []))
        self.assertEqual(response.status_code, 302)
        parcel.refresh_from_db()
        self.assertEqual(parcel.status, Shipment.Status.DRAFT)
        self.assertIsNone(parcel.locked_at)

    def test_reconfirming_held_booked_parcel_preserves_tracking_and_returns_to_tracking_stage(self):
        old_lock = timezone.now() - timedelta(days=1)
        parcel = self.shipment(quantity=1, status=Shipment.Status.HELD, locked_at=old_lock,
                               waybill='HELD-BOOKING', hold_reason='Treatment was paused')
        page = self.pending()
        row = next(item for item in page.context['shipments'] if item.pk == parcel.pk)
        response = self.client.post(self.url, {'workflow_context': row.confirm_context, 'week': self.week,
                                              'single_shipment': parcel.pk, 'shipment_ids': [parcel.pk]})
        self.assertEqual(response.status_code, 302)
        parcel.refresh_from_db()
        self.assertEqual(parcel.status, Shipment.Status.READY)
        self.assertGreater(parcel.locked_at, old_lock)
        self.assertEqual(parcel.waybill, 'HELD-BOOKING')
        pending = self.pending()
        self.assertNotIn(parcel.pk, {item.pk for item in pending.context['page_obj']})
        tracking = self.page('ops-shipping', week=self.week, shipping_step='tracking')
        self.assertIn(parcel.pk, {item.pk for item in tracking.context['page_obj']})
