"""Weekly shipping stages partition parcels and move them forward only after explicit actions."""
from datetime import timedelta
from html.parser import HTMLParser
from unittest.mock import patch
from urllib.parse import urlencode

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from care import courier
from care.models import AuditEvent, DeliveryAddress, PracticeSettings, Shipment, ShipmentItem, StockMovement, SubscriptionCycle, TreatmentAuthorization
from .test_operations import OperationsPortalFixture


class TableHeaders(HTMLParser):
    def __init__(self):
        super().__init__()
        self.headers, self.parts, self.in_header = [], [], False

    def handle_starttag(self, tag, attrs):
        if tag == 'th':
            self.in_header, self.parts = True, []

    def handle_data(self, data):
        if self.in_header:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'th':
            self.headers.append(' '.join(''.join(self.parts).split()))
            self.in_header = False


class TableCells(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cells, self.current = [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'td':
            self.current = dict(label=attrs.get('data-label'), parts=[], buttons=[], links=[], ids=[], tooltip_ids=[])
        if self.current is not None:
            if tag == 'button':
                self.current['buttons'].append(attrs)
            if tag == 'a':
                self.current['links'].append(attrs.get('href'))
            if attrs.get('id'):
                self.current['ids'].append(attrs['id'])
                if attrs.get('role') == 'tooltip':
                    self.current['tooltip_ids'].append(attrs['id'])

    def handle_data(self, data):
        if self.current is not None:
            self.current['parts'].append(data)

    def handle_endtag(self, tag):
        if tag == 'td' and self.current is not None:
            self.current['text'] = ' '.join(''.join(self.current.pop('parts')).split())
            self.cells.append(self.current)
            self.current = None

    def labelled(self, label):
        return [cell for cell in self.cells if cell['label'] == label]


class ShippingStageTests(OperationsPortalFixture):
    def setUp(self):
        super().setUp()
        self.week = self.today - timedelta(days=self.today.weekday())

    def stage(self, step=None, **params):
        if step is not None:
            params['shipping_step'] = step
        response = self.client.get(reverse('portal:ops-shipping'), {'week': self.week, **params})
        self.assertEqual(response.status_code, 200)
        return response

    def ids(self, response):
        return {parcel.pk for parcel in response.context['page_obj']}

    def test_stages_partition_the_week_and_get_requests_do_not_move_parcels(self):
        draft = self.shipment(quantity=1)
        held = self.shipment(quantity=1, status=Shipment.Status.HELD)
        ready = self.shipment(quantity=1, status=Shipment.Status.READY)
        locked = self.shipment(quantity=1, status=Shipment.Status.READY, locked_at=timezone.now())
        waybill = self.shipment(quantity=1, status=Shipment.Status.READY, locked_at=timezone.now(), waybill='STAGE-WAYBILL')
        external = self.shipment(quantity=1, status=Shipment.Status.READY, locked_at=timezone.now(), tracking_number='STAGE-EXTERNAL')
        sent = self.shipment(quantity=1, status=Shipment.Status.DISPATCHED, tracking_number='STAGE-SENT')
        delivered = self.shipment(quantity=1, status=Shipment.Status.DELIVERED, tracking_number='STAGE-DELIVERED')
        cancelled = self.shipment(quantity=1, status=Shipment.Status.CANCELLED)
        self.shipment(quantity=1, scheduled_for=self.week + timedelta(days=7))
        expected = {'pending': {draft.pk, held.pk, ready.pk}, 'confirmed': {locked.pk},
                    'tracking': {waybill.pk, external.pk, sent.pk, delivered.pk},
                    'all': {draft.pk, held.pk, ready.pk, locked.pk, waybill.pk, external.pk, sent.pk, delivered.pk, cancelled.pk}}
        snapshots = list(Shipment.objects.order_by('pk').values('pk', 'status', 'locked_at', 'dispatched_at', 'delivered_at'))
        movements = StockMovement.objects.count()
        audits = AuditEvent.objects.count()
        for step, wanted in expected.items():
            for mode in ('customers', 'medication'):
                with self.subTest(step=step, mode=mode):
                    self.assertEqual(self.ids(self.stage(step, view=mode)), wanted)
        self.assertEqual(list(Shipment.objects.order_by('pk').values('pk', 'status', 'locked_at', 'dispatched_at', 'delivered_at')), snapshots)
        self.assertEqual(StockMovement.objects.count(), movements)
        self.assertEqual(AuditEvent.objects.count(), audits)

    def test_default_legacy_and_unrecognised_steps_show_pending_parcels(self):
        draft = self.shipment(quantity=1)
        held = self.shipment(quantity=1, status=Shipment.Status.HELD)
        ready = self.shipment(quantity=1, status=Shipment.Status.READY)
        self.shipment(quantity=1, status=Shipment.Status.READY, locked_at=timezone.now())
        for step in (None, 'ready', 'lock', 'unknown-step'):
            with self.subTest(step=step):
                response = self.stage(step)
                self.assertEqual(self.ids(response), {draft.pk, held.pk, ready.pk})
                self.assertEqual(response.context['shipping_step'], 'pending')

    def test_legacy_courier_and_collection_links_normalise_to_new_stages(self):
        locked = self.shipment(quantity=1, status=Shipment.Status.READY, locked_at=timezone.now())
        booked = self.shipment(quantity=1, status=Shipment.Status.READY, locked_at=timezone.now(), waybill='LEGACY-BOOKING')
        for old, canonical, parcel in (('courier', 'confirmed', locked), ('collection', 'tracking', booked)):
            with self.subTest(old=old):
                response = self.stage(old)
                self.assertEqual(response.context['shipping_step'], canonical)
                self.assertEqual(self.ids(response), {parcel.pk})

    def test_status_columns_match_stage_and_due_date_precedes_reference_in_both_views(self):
        self.shipment(quantity=1)
        self.shipment(quantity=1, status=Shipment.Status.READY, locked_at=timezone.now())
        self.shipment(quantity=1, status=Shipment.Status.DISPATCHED, waybill='HEADER-TRACKING')
        for stage in ('pending', 'confirmed', 'tracking', 'all'):
            for mode in ('customers', 'medication'):
                with self.subTest(stage=stage, mode=mode):
                    response = self.stage(stage, view=mode)
                    headers = TableHeaders()
                    headers.feed(response.content.decode())
                    self.assertIn('Due date', headers.headers)
                    position = headers.headers.index('Due date')
                    self.assertEqual(headers.headers[position + 1], 'Waybill')
                    self.assertNotIn('Shipping info', headers.headers)
                    self.assertNotIn('Shipping date', headers.headers)
                    self.assertNotIn('Scheduled', headers.headers)
                    self.assertEqual('Status' in headers.headers, stage == 'all')
                    self.assertEqual('Shipping status' in headers.headers, stage == 'tracking')

    def cells(self, response):
        parser = TableCells()
        parser.feed(response.content.decode())
        return parser

    def name_cells(self, response):
        return [cell for cell in self.cells(response).cells if cell['label'] in ('Patient', 'Customer')]

    def warnings(self, cell):
        return [button for button in cell['buttons'] if 'shipping-patient-warning-trigger' in button.get('class', '').split()]

    def test_stage_combines_with_product_doctor_and_payment_filters(self):
        chosen = self.shipment(quantity=1, authorization=self.auth)
        self.shipment(product=self.open_product, quantity=1, authorization=self.auth)
        self.shipment(quantity=1, authorization=self.auth, subscription=None)
        self.shipment(quantity=1, authorization=self.auth, status=Shipment.Status.READY, locked_at=timezone.now())
        for mode in ('customers', 'medication'):
            response = self.stage('pending', view=mode, product=self.product.pk, doctor=self.doctor.pk, payment='paid')
            self.assertEqual(self.ids(response), {chosen.pk})

    def test_confirm_selected_moves_only_selected_parcel_to_confirmed_stage(self):
        selected = self.shipment(quantity=1)
        left = self.shipment(quantity=1)
        page = self.stage('pending')
        response = self.client.post(reverse('portal:ops-shipping-confirm'), {
            'workflow_context': page.context['confirm_context'], 'week': self.week,
            'shipment_ids': [selected.pk], 'list_query': urlencode({'week': self.week, 'shipping_step': 'pending'})})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.ids(self.stage('pending')), {left.pk})
        self.assertEqual(self.ids(self.stage('confirmed')), {selected.pk})
        selected.refresh_from_db()
        self.assertEqual(selected.status, Shipment.Status.READY)
        self.assertIsNotNone(selected.locked_at)

    def test_confirm_existing_ready_parcel_moves_to_confirmed_stage(self):
        parcel = self.ready(self.shipment(quantity=1))
        page = self.stage('pending')
        response = self.client.post(reverse('portal:ops-shipping-confirm'), {
            'workflow_context': page.context['confirm_context'], 'week': self.week, 'shipment_ids': [parcel.pk]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.ids(self.stage('pending')), set())
        self.assertEqual(self.ids(self.stage('confirmed')), {parcel.pk})
        parcel.refresh_from_db()
        self.assertIsNotNone(parcel.locked_at)
        self.assertEqual(parcel.status, Shipment.Status.READY)

    def test_record_collection_moves_booked_parcel_to_history_and_deducts_stock_once(self):
        parcel = self.locked(self.shipment(quantity=1))
        Shipment.objects.filter(pk=parcel.pk).update(waybill='STAGE-COLLECT')
        page = self.stage('tracking')
        before = StockMovement.objects.count()
        data = {'workflow_context': page.context['collect_context'], 'week': self.week, 'confirm': 'on'}
        response = self.client.post(reverse('portal:ops-shipping-collect'), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.ids(self.stage('tracking')), {parcel.pk})
        self.assertEqual(self.stage('tracking').context['to_collect'], 0)
        parcel.refresh_from_db()
        self.assertEqual(parcel.status, Shipment.Status.DISPATCHED)
        self.assertEqual(parcel.tracking_number, 'STAGE-COLLECT')
        self.assertEqual(StockMovement.objects.count(), before + 1)
        history = self.page('ops-history', week=self.week)
        self.assertIn(parcel.pk, self.ids(history))
        self.client.post(reverse('portal:ops-shipping-collect'), data)
        self.assertEqual(StockMovement.objects.count(), before + 1)

    def test_external_tracking_parcel_can_be_recorded_as_collected(self):
        parcel = self.locked(self.shipment(quantity=1))
        Shipment.objects.filter(pk=parcel.pk).update(tracking_number='STAGE-EXTERNAL-COLLECT')
        page = self.stage('tracking')
        self.assertEqual(self.ids(page), {parcel.pk})
        self.assertEqual(page.context['to_collect'], 1)
        response = self.client.post(reverse('portal:ops-shipping-collect'), {
            'workflow_context': page.context['collect_context'], 'week': self.week, 'confirm': 'on'})
        self.assertEqual(response.status_code, 302)
        parcel.refresh_from_db()
        self.assertEqual(parcel.status, Shipment.Status.DISPATCHED)
        self.assertEqual(parcel.tracking_number, 'STAGE-EXTERNAL-COLLECT')

    @override_settings(TCG_API_KEY='stage-test-key')
    def test_booking_moves_confirmed_parcel_to_tracking_and_shows_latest_shipping_info(self):
        PracticeSettings.objects.filter(company=self.company).update(
            collection_street='1 Practice Road', collection_suburb='Claremont', collection_city='Cape Town',
            collection_province='WC', collection_postal_code='7708', collection_contact='Dispensary',
            collection_phone='021 555 0100')
        parcel = self.locked(self.shipment(quantity=1))
        page = self.stage('confirmed')
        self.assertEqual(self.ids(page), {parcel.pk})
        stock_before = StockMovement.objects.count()
        with patch.object(courier, '_call', return_value={
                'id': 401, 'short_tracking_reference': 'STAGE-BOOKED', 'status': 'submitted'}) as booking:
            response = self.client.post(reverse('portal:ops-shipping-courier'), {
                'workflow_context': page.context['courier_context'], 'week': self.week})
        self.assertEqual(response.status_code, 302)
        booking.assert_called_once()
        self.assertEqual(self.ids(self.stage('confirmed')), set())
        self.assertEqual(self.ids(self.stage('tracking')), {parcel.pk})
        self.assertEqual(StockMovement.objects.count(), stock_before)
        courier.apply_tracking({'shipment_id': 401, 'short_tracking_reference': 'STAGE-BOOKED',
                                'status': 'out-for-delivery', 'shipment_time_modified': timezone.now().isoformat()})
        for mode in ('customers', 'medication'):
            with self.subTest(mode=mode):
                tracking = self.stage('tracking', view=mode)
                cells = self.cells(tracking)
                self.assertEqual(cells.labelled('Waybill')[0]['text'], 'STAGE-BOOKED')
                self.assertIn('Out for delivery', cells.labelled('Shipping status')[0]['text'])
        parcel.refresh_from_db()
        self.assertEqual(parcel.status, Shipment.Status.READY)
        self.assertEqual(StockMovement.objects.count(), stock_before)

    def test_booked_reference_is_separate_from_actual_shipping_status_in_both_views(self):
        parcel = self.shipment(quantity=1, status=Shipment.Status.READY, locked_at=timezone.now(),
                               waybill='SHIPPING-STATUS', courier_shipment_id='501')
        for status in ('submitted', 'booked'):
            Shipment.objects.filter(pk=parcel.pk).update(courier_status=status)
            for mode in ('customers', 'medication'):
                with self.subTest(status=status, mode=mode):
                    cells = self.cells(self.stage('tracking', view=mode))
                    reference = cells.labelled('Waybill')[0]
                    actual_status = cells.labelled('Shipping status')[0]
                    self.assertEqual(reference['text'], 'SHIPPING-STATUS')
                    self.assertIn('Awaiting collection', actual_status['text'])
                    self.assertNotRegex(actual_status['text'], r'\bBooked\b')
                    self.assertNotIn('View label', reference['text'])
                    self.assertNotIn(reverse('portal:ops-shipment-label', args=[parcel.pk]), reference['links'])

    def test_tracking_status_keeps_actual_transit_and_delivery_statuses(self):
        parcel = self.shipment(quantity=1, status=Shipment.Status.DISPATCHED, locked_at=timezone.now(),
                               dispatched_at=timezone.now(), waybill='SHIPPING-LIVE-STATUS', courier_shipment_id='502')
        for status, parcel_status, label in (
                ('in-transit', Shipment.Status.DISPATCHED, 'In transit'),
                ('out-for-delivery', Shipment.Status.DISPATCHED, 'Out for delivery'),
                ('delivered', Shipment.Status.DELIVERED, 'Delivered')):
            Shipment.objects.filter(pk=parcel.pk).update(courier_status=status, status=parcel_status)
            for mode in ('customers', 'medication'):
                with self.subTest(status=status, mode=mode):
                    cells = self.cells(self.stage('tracking', view=mode))
                    self.assertIn(label, cells.labelled('Shipping status')[0]['text'])
                    self.assertEqual(cells.labelled('Waybill')[0]['text'], 'SHIPPING-LIVE-STATUS')

    def test_pending_warning_preserves_clinical_address_required_payment_and_held_reasons(self):
        held_reason = 'Patient asked to review the delivery arrangement'
        self.shipment(quantity=1, status=Shipment.Status.HELD, hold_reason=held_reason)
        TreatmentAuthorization.objects.filter(pk=self.auth.pk).update(status=TreatmentAuthorization.Status.EXPIRED)
        DeliveryAddress.objects.filter(pk=self.address.pk).update(phone='')
        SubscriptionCycle.objects.filter(pk=self.cycle.pk).update(status='pending', attempts=1,
                                                                  next_attempt_on=self.today + timedelta(days=3))
        for mode in ('customers', 'medication'):
            with self.subTest(mode=mode):
                names = self.name_cells(self.stage('pending', view=mode))
                self.assertEqual(len(names), 1)
                cell = names[0]
                buttons = self.warnings(cell)
                self.assertEqual(len(buttons), 1)
                self.assertEqual(buttons[0].get('type'), 'button')
                self.assertEqual(buttons[0].get('aria-expanded'), 'false')
                target = buttons[0].get('aria-describedby')
                self.assertTrue(target)
                self.assertIn(target, cell['tooltip_ids'])
                self.assertIn('authorisation', cell['text'].lower())
                self.assertIn('doctor', cell['text'].lower())
                self.assertIn('contact number', cell['text'].lower())
                self.assertIn('not paid', cell['text'].lower())
                self.assertIn(held_reason, cell['text'])

    def test_healthy_and_payment_optional_pending_parcels_have_no_warning(self):
        self.shipment(quantity=1)
        self.shipment(quantity=1, subscription=None)
        PracticeSettings.objects.filter(company=self.company).update(require_payment=False)
        for mode in ('customers', 'medication'):
            with self.subTest(mode=mode):
                names = self.name_cells(self.stage('pending', view=mode))
                self.assertEqual(len(names), 2)
                self.assertTrue(all(not self.warnings(cell) for cell in names))

    def test_medication_group_warnings_have_distinct_popover_targets_for_the_same_parcel(self):
        parcel = self.shipment(quantity=1)
        ShipmentItem.objects.create(company=self.company, shipment=parcel, product=self.open_product,
                                    quantity=1, dose=self.open_product.strength)
        DeliveryAddress.objects.filter(pk=self.address.pk).update(phone='')
        names = self.name_cells(self.stage('pending', view='medication'))
        self.assertEqual(len(names), 2)
        targets = [self.warnings(cell)[0]['aria-describedby'] for cell in names]
        self.assertEqual(len(set(targets)), 2)
        for cell, target in zip(names, targets):
            self.assertIn(target, cell['tooltip_ids'])
            self.assertIn('contact number', cell['text'].lower())

    def test_confirmed_courier_failure_remains_visible_in_a_name_warning(self):
        reason = 'Courier rejected the collection area'
        self.shipment(quantity=1, status=Shipment.Status.READY, locked_at=timezone.now(), courier_error=reason)
        for mode in ('customers', 'medication'):
            with self.subTest(mode=mode):
                names = self.name_cells(self.stage('confirmed', view=mode))
                self.assertEqual(len(self.warnings(names[0])), 1)
                self.assertIn(reason, names[0]['text'])
