"""Shipment detail follows the same confirmation gates as the weekly Pending queue."""

from datetime import timedelta

from django.contrib.messages import WARNING, get_messages
from django.urls import reverse
from django.utils import timezone

from care.inventory import stock_rows
from care.models import AuditEvent, Shipment, StockMovement
from .test_operations import OperationsPortalFixture


class ShipmentDetailConfirmationTests(OperationsPortalFixture):
    def detail(self, shipment, **params):
        return self.page('ops-shipment-detail', [shipment.pk], **params)

    def post_action(self, shipment, action, **extra):
        page = self.detail(shipment)
        return self.client.post(reverse('portal:ops-shipment-detail', args=[shipment.pk]), {
            'workflow_context': page.context['workflow_context'], 'action': action, 'confirm': 'on', **extra})

    def test_pending_detail_exposes_confirmation_in_place_of_mark_ready(self):
        for state in (Shipment.Status.DRAFT, Shipment.Status.HELD, Shipment.Status.READY):
            parcel = self.shipment(quantity=1, status=state)
            with self.subTest(state=state):
                page = self.detail(parcel)
                choices = dict(page.context['form'].fields['action'].choices)
                self.assertEqual(choices, {'confirm': 'Confirm & lock', 'hold': 'Hold', 'cancel': 'Cancel before dispatch'})
                self.assertNotContains(page, 'Mark ready')

    def test_detail_confirmation_prepares_and_locks_without_dispatching_or_deducting_stock(self):
        parcel = self.shipment(quantity=4)
        movements = list(StockMovement.objects.order_by('pk').values())

        response = self.post_action(parcel, 'confirm')

        self.assertRedirects(response, reverse('portal:ops-shipment-detail', args=[parcel.pk]), fetch_redirect_response=False)
        parcel.refresh_from_db()
        self.assertEqual((parcel.status, parcel.prepared_by), (Shipment.Status.READY, self.admin))
        self.assertIsNotNone(parcel.prepared_at)
        self.assertIsNotNone(parcel.locked_at)
        self.assertIsNone(parcel.dispatched_at)
        self.assertEqual((parcel.waybill, parcel.courier_shipment_id), ('', ''))
        self.assertEqual(list(StockMovement.objects.order_by('pk').values()), movements)
        self.assertEqual(AuditEvent.objects.filter(action='shipment.prepared').count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action='shipment.locked').count(), 1)
        choices = dict(self.detail(parcel).context['form'].fields['action'].choices)
        self.assertEqual(set(choices), {'hold', 'cancel', 'dispatch'})

    def test_detail_confirmation_checks_prescription_address_and_payment(self):
        parcel = self.shipment(quantity=4)
        for gate in ('clinical', 'address', 'payment'):
            self.plan.status = 'paused' if gate == 'clinical' else 'active'
            self.plan.save(update_fields=('status',))
            self.address.phone = '' if gate == 'address' else '082 555 0114'
            self.address.save(update_fields=('phone',))
            self.cycle.status = 'failed' if gate == 'payment' else 'paid'
            self.cycle.save(update_fields=('status',))
            with self.subTest(gate=gate):
                response = self.post_action(parcel, 'confirm')
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.context['form'].non_field_errors())
                parcel.refresh_from_db()
                self.assertEqual((parcel.status, parcel.locked_at, parcel.prepared_at), ('draft', None, None))
        self.assertFalse(AuditEvent.objects.filter(action__in=('shipment.prepared', 'shipment.locked')).exists())

    def test_detail_confirmation_warns_about_shortages_without_blocking_or_deducting_stock(self):
        StockMovement.objects.all().delete()
        parcel = self.shipment(quantity=4)

        response = self.post_action(parcel, 'confirm')

        self.assertRedirects(response, reverse('portal:ops-shipment-detail', args=[parcel.pk]), fetch_redirect_response=False)
        parcel.refresh_from_db()
        self.assertEqual(parcel.status, Shipment.Status.READY)
        self.assertIsNotNone(parcel.locked_at)
        self.assertIsNone(parcel.dispatched_at)
        self.assertFalse(StockMovement.objects.exists())
        row = next(row for row in stock_rows(self.company) if row['product'].pk == self.product.pk)
        self.assertEqual((row['on_hand'], row['reserved'], row['available']), (0, 4, -4))
        notices = list(get_messages(response.wsgi_request))
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].level, WARNING)
        self.assertIn('Parcel confirmed and locked. Stock reserved.', str(notices[0]))
        self.assertIn(f'{self.product}: 4 short', str(notices[0]))

    def test_prepare_and_confirm_cannot_unlock_or_reconfirm_a_booked_locked_parcel(self):
        parcel = self.shipment(quantity=4, status=Shipment.Status.READY, locked_at=timezone.now(),
                               waybill='EXISTING-WAYBILL', tracking_number='EXISTING-TRACKING',
                               courier_shipment_id='existing-courier', booked_address={'street': 'Booked address'})
        before = Shipment.objects.filter(pk=parcel.pk).values().get()

        for action in ('prepare', 'confirm'):
            with self.subTest(action=action):
                response = self.post_action(parcel, action)
                self.assertEqual(response.status_code, 400)
                self.assertIn('action', response.context['form'].errors)
                self.assertEqual(Shipment.objects.filter(pk=parcel.pk).values().get(), before)
        row = next(row for row in stock_rows(self.company) if row['product'].pk == self.product.pk)
        self.assertEqual(row['reserved'], 4)
        self.assertFalse(AuditEvent.objects.filter(action__in=('shipment.prepared', 'shipment.locked')).exists())

    def test_held_booked_parcel_can_be_reconfirmed_from_detail(self):
        old_lock = timezone.now() - timedelta(days=1)
        parcel = self.shipment(quantity=4, status=Shipment.Status.HELD, locked_at=old_lock,
                               waybill='HELD-WAYBILL', hold_reason='Prescription was paused')

        self.assertEqual(self.post_action(parcel, 'confirm').status_code, 302)

        parcel.refresh_from_db()
        self.assertEqual((parcel.status, parcel.hold_reason, parcel.waybill), ('ready', '', 'HELD-WAYBILL'))
        self.assertGreater(parcel.locked_at, old_lock)

    def test_confirmation_needs_the_current_record_token(self):
        parcel = self.shipment(quantity=1)
        page = self.detail(parcel)
        url = reverse('portal:ops-shipment-detail', args=[parcel.pk])
        data = {'action': 'confirm', 'workflow_context': page.context['workflow_context']}
        parcel.note = 'Changed in another tab'
        parcel.save()
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'changed in another tab', status_code=400)
        parcel.refresh_from_db()
        self.assertEqual((parcel.status, parcel.locked_at), ('draft', None))

    def test_hold_dispatch_and_delivery_remain_available_at_their_valid_states(self):
        parcel = self.shipment(quantity=4)
        self.assertEqual(self.post_action(parcel, 'confirm').status_code, 302)
        self.assertEqual(self.post_action(parcel, 'hold', reason='Patient travelling').status_code, 302)
        parcel.refresh_from_db()
        self.assertEqual((parcel.status, parcel.locked_at), ('held', None))
        self.assertEqual(self.post_action(parcel, 'confirm').status_code, 302)
        item = parcel.items.get()
        self.assertEqual(self.post_action(parcel, 'dispatch', tracking_number='ACTUAL-TRACKING',
                                          **{f'batch_{item.pk}': 'BATCH-123, expiry 2027'}).status_code, 302)
        parcel.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual((parcel.status, item.batch_note), ('dispatched', 'BATCH-123, expiry 2027'))
        self.assertEqual(set(dict(self.detail(parcel).context['form'].fields['action'].choices)), {'deliver'})
        self.assertEqual(self.post_action(parcel, 'deliver').status_code, 302)
        parcel.refresh_from_db()
        self.assertEqual(parcel.status, Shipment.Status.DELIVERED)

    def test_pending_parcel_can_still_be_cancelled(self):
        parcel = self.shipment(quantity=1)

        self.assertEqual(self.post_action(parcel, 'cancel', reason='Patient cancelled').status_code, 302)

        parcel.refresh_from_db()
        self.assertEqual(parcel.status, Shipment.Status.CANCELLED)

    def test_confirmation_keeps_shipping_privileges_and_tenant_boundaries(self):
        parcel = self.shipment(quantity=1)
        token = self.detail(parcel).context['workflow_context']
        url = reverse('portal:ops-shipment-detail', args=[parcel.pk])
        for actor in (self.patient_user, self.doctor):
            self.login(actor)
            with self.subTest(actor=actor):
                self.assertEqual(self.client.post(url, {'action': 'confirm', 'confirm': 'on',
                                                        'workflow_context': token}).status_code, 403)
        self.login(self.beta_admin, self.beta)
        self.assertEqual(self.client.post(url, {'action': 'confirm', 'confirm': 'on',
                                                'workflow_context': token}).status_code, 404)
        parcel.refresh_from_db()
        self.assertEqual((parcel.status, parcel.locked_at), ('draft', None))
