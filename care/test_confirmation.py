"""One confirmation action validates and reserves selected pending parcels atomically."""

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from .inventory import stock_rows
from .models import AuditEvent, DeliveryAddress, Payment, PracticeSettings, Shipment, StockMovement
from .operations import confirm_shipments
from .test_operations import OperationsFixture


class ShipmentConfirmationTests(OperationsFixture):
    def confirm(self, *shipments, actor=None):
        return confirm_shipments(company=self.company, actor=actor or self.admin, week_start=self.today,
                                 shipment_ids=[shipment.pk for shipment in shipments])

    def assert_pending(self, *shipments):
        for shipment in shipments:
            shipment.refresh_from_db()
            self.assertIsNone(shipment.locked_at)
        self.assertFalse(AuditEvent.objects.filter(action__in=('shipment.prepared', 'shipment.locked')).exists())

    def test_confirms_draft_held_and_previously_ready_in_one_action(self):
        draft = self.shipment(quantity=4)
        held = self.shipment(product=self.open_product, quantity=1, status=Shipment.Status.HELD,
                             hold_reason='Waiting for address confirmation')
        previous_preparation = timezone.now() - timedelta(days=1)
        ready = self.shipment(product=self.open_product, quantity=2, status=Shipment.Status.READY,
                              prepared_by=self.super_admin, prepared_at=previous_preparation)
        unselected = self.shipment(product=self.open_product, quantity=1)
        movement_count = StockMovement.objects.count()

        confirmed = self.confirm(draft, held, ready)

        self.assertEqual([parcel.pk for parcel in confirmed], [draft.pk, held.pk, ready.pk])
        for parcel in confirmed:
            parcel.refresh_from_db()
            self.assertEqual((parcel.status, parcel.hold_reason), (Shipment.Status.READY, ''))
            self.assertIsNotNone(parcel.locked_at)
            self.assertIsNotNone(parcel.prepared_at)
        self.assertEqual((confirmed[0].prepared_by, confirmed[1].prepared_by), (self.admin, self.admin))
        ready.refresh_from_db()
        self.assertEqual((ready.prepared_by, ready.prepared_at), (self.super_admin, previous_preparation))
        unselected.refresh_from_db()
        self.assertEqual((unselected.status, unselected.locked_at), (Shipment.Status.DRAFT, None))
        self.assertEqual(AuditEvent.objects.filter(action='shipment.prepared').count(), 2)
        self.assertEqual(AuditEvent.objects.filter(action='shipment.locked').count(), 3)
        self.assertEqual(StockMovement.objects.count(), movement_count)

    def test_bad_address_prevents_confirmation_of_entire_selection(self):
        good = self.shipment(quantity=1)
        bad = self.shipment(product=self.open_product, quantity=1, patient=self.other_patient, subscription=None,
                            status=Shipment.Status.HELD, hold_reason='Address pending')
        Payment.objects.create(company=self.company, patient=self.other_patient, shipment=bad, amount=40,
                               due_on=self.today, status=Payment.Status.PAID)

        with self.assertRaisesMessage(ValidationError, 'No delivery address'):
            self.confirm(good, bad)

        self.assert_pending(good, bad)
        self.assertEqual((good.status, bad.status, bad.hold_reason), ('draft', 'held', 'Address pending'))

    def test_unpaid_parcel_prevents_confirmation_of_entire_selection(self):
        good = self.shipment(quantity=1)
        bad = self.shipment(product=self.open_product, quantity=1, patient=self.other_patient, subscription=None)
        DeliveryAddress.objects.create(company=self.company, patient=self.other_patient, street='2 Test Street',
                                       suburb='Gardens', city='Cape Town', province='WC', postal_code='8001',
                                       phone='082 555 0115')

        with self.assertRaisesMessage(ValidationError, 'Not paid'):
            self.confirm(good, bad)

        self.assert_pending(good, bad)
        self.assertEqual((good.status, bad.status), ('draft', 'draft'))

    def test_selection_must_be_nonempty_pending_and_in_practice_and_week(self):
        good = self.shipment(quantity=1)
        foreign = self.shipment(company=self.beta, patient=self.beta_patient, subscription=None,
                                product=self.beta_product, quantity=1)
        other_week = self.shipment(product=self.open_product, quantity=1, scheduled_for=self.today + timedelta(days=7))
        previous_week = self.shipment(product=self.open_product, quantity=1, scheduled_for=self.today - timedelta(days=1))
        mismatched_patient = self.shipment(product=self.open_product, quantity=1, patient=self.beta_patient, subscription=None)
        locked_draft = self.shipment(product=self.open_product, quantity=1, locked_at=timezone.now())
        closed = [self.shipment(product=self.open_product, quantity=1, status=status)
                  for status in (Shipment.Status.DISPATCHED, Shipment.Status.DELIVERED, Shipment.Status.CANCELLED)]
        selections = ([], None, [True], [good.pk, 999999999], [good.pk, foreign.pk], [good.pk, other_week.pk],
                      [good.pk, previous_week.pk], [good.pk, mismatched_patient.pk], [good.pk, locked_draft.pk],
                      *([good.pk, parcel.pk] for parcel in closed))
        before = list(Shipment.objects.order_by('pk').values())

        for shipment_ids in selections:
            with self.subTest(selection=shipment_ids), self.assertRaises(ValidationError):
                confirm_shipments(company=self.company, actor=self.admin, week_start=self.today, shipment_ids=shipment_ids)
            self.assertEqual(list(Shipment.objects.order_by('pk').values()), before)
        self.assert_pending(good)

    def test_stale_selection_leaves_already_locked_booking_and_other_parcels_unchanged(self):
        pending = self.shipment(quantity=1)
        booked = self.shipment(product=self.open_product, quantity=1, status=Shipment.Status.READY,
                               locked_at=timezone.now(), prepared_by=self.super_admin, prepared_at=timezone.now(),
                               courier_shipment_id='courier-existing', waybill='waybill-existing',
                               courier_status='booked', booked_address={'street': 'Original booked address'})
        before = list(Shipment.objects.order_by('pk').values())

        with self.assertRaisesMessage(ValidationError, 'already confirmed'):
            self.confirm(pending, booked)

        self.assertEqual(list(Shipment.objects.order_by('pk').values()), before)
        self.assert_pending(pending)

    def test_held_parcel_can_be_reconfirmed_without_losing_its_existing_booking(self):
        old_lock = timezone.now() - timedelta(days=2)
        booked = self.shipment(quantity=4, status=Shipment.Status.HELD, hold_reason='Prescription changed',
                               locked_at=old_lock, prepared_by=self.super_admin, prepared_at=old_lock,
                               courier_shipment_id='held-courier', waybill='held-waybill', tracking_number='held-tracking',
                               courier_status='booked', booked_address={'street': 'Original booked address'})
        def row():
            return next(row for row in stock_rows(self.company) if row['product'].pk == self.product.pk)

        self.assertEqual(row()['reserved'], 0)
        movements = list(StockMovement.objects.order_by('pk').values())

        self.confirm(booked)

        booked.refresh_from_db()
        self.assertEqual((booked.status, booked.hold_reason), (Shipment.Status.READY, ''))
        self.assertGreater(booked.locked_at, old_lock)
        self.assertEqual((booked.prepared_by, booked.prepared_at), (self.super_admin, old_lock))
        self.assertEqual((booked.courier_shipment_id, booked.waybill, booked.tracking_number, booked.courier_status,
                          booked.booked_address), ('held-courier', 'held-waybill', 'held-tracking', 'booked',
                                                   {'street': 'Original booked address'}))
        self.assertEqual(row()['reserved'], 4)
        self.assertEqual(list(StockMovement.objects.order_by('pk').values()), movements)
        self.assertEqual(AuditEvent.objects.filter(action='shipment.prepared').count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action='shipment.locked').count(), 1)

    def test_held_parcel_with_old_lock_must_still_pass_clinical_address_and_payment_checks(self):
        held = self.shipment(quantity=4, status=Shipment.Status.HELD, hold_reason='Prescription changed',
                             locked_at=timezone.now() - timedelta(days=2), waybill='existing-waybill')
        before = Shipment.objects.filter(pk=held.pk).values().get()
        for gate in ('clinical', 'address', 'payment'):
            self.plan.status = 'paused' if gate == 'clinical' else 'active'
            self.plan.save(update_fields=('status',))
            self.address.phone = '' if gate == 'address' else '082 555 0114'
            self.address.save(update_fields=('phone',))
            self.cycle.status = 'failed' if gate == 'payment' else 'paid'
            self.cycle.save(update_fields=('status',))
            with self.subTest(gate=gate), self.assertRaises(ValidationError):
                self.confirm(held)
            self.assertEqual(Shipment.objects.filter(pk=held.pk).values().get(), before)
        self.assertFalse(AuditEvent.objects.filter(action__in=('shipment.prepared', 'shipment.locked')).exists())

    def test_confirmation_includes_the_last_day_of_the_selected_week(self):
        last_day = self.shipment(product=self.open_product, quantity=1, scheduled_for=self.today + timedelta(days=6))

        self.assertEqual([parcel.pk for parcel in self.confirm(last_day)], [last_day.pk])

    def test_authorisation_must_be_valid_on_the_planned_shipping_date(self):
        later = self.shipment(quantity=1, scheduled_for=self.today + timedelta(days=1))
        self.auth.expires_on = self.today
        self.auth.save(update_fields=('expires_on',))

        with self.assertRaises(ValidationError):
            self.confirm(later)

        self.assert_pending(later)

    def test_shortage_is_allowed_and_confirmation_only_reserves_stock(self):
        StockMovement.objects.all().delete()
        parcel = self.shipment(quantity=4)

        self.confirm(parcel)

        row = next(row for row in stock_rows(self.company) if row['product'].pk == self.product.pk)
        self.assertEqual((row['on_hand'], row['reserved'], row['available'], row['shortage']), (0, 4, -4, 4))
        self.assertFalse(StockMovement.objects.exists())

    def test_selected_parcels_cannot_together_exceed_the_supply_allowance(self):
        first = self.shipment(quantity=3)
        second = self.shipment(quantity=2, scheduled_for=self.today + timedelta(days=1))

        with self.assertRaisesMessage(ValidationError, 'remaining authorised quantity'):
            self.confirm(first, second)

        self.assert_pending(first, second)
        self.assertEqual((first.status, second.status), ('draft', 'draft'))

    def test_selected_parcels_can_share_the_exact_supply_allowance(self):
        first = self.shipment(quantity=2)
        second = self.shipment(quantity=2, scheduled_for=self.today + timedelta(days=1))

        self.assertEqual([parcel.pk for parcel in self.confirm(first, second)], [first.pk, second.pk])

    def test_reconfirming_held_parcel_cannot_exceed_allowance_with_selected_draft(self):
        first = self.shipment(quantity=3)
        held = self.shipment(quantity=2, status=Shipment.Status.HELD, locked_at=timezone.now() - timedelta(days=1))
        before = list(Shipment.objects.order_by('pk').values())

        with self.assertRaisesMessage(ValidationError, 'remaining authorised quantity'):
            self.confirm(first, held)

        self.assertEqual(list(Shipment.objects.order_by('pk').values()), before)
        self.assertFalse(AuditEvent.objects.filter(action__in=('shipment.prepared', 'shipment.locked')).exists())

    def test_shipping_permission_applies_to_the_combined_action(self):
        parcel = self.shipment(quantity=1)
        for actor in (self.patient_user, self.doctor, self.beta_admin):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                self.confirm(parcel, actor=actor)
        self.assert_pending(parcel)
        PracticeSettings.objects.filter(company=self.company).update(doctors_ship=True)
        self.assertEqual(self.confirm(parcel, actor=self.doctor)[0].prepared_by, self.doctor)
