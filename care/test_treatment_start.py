"""Treatment starts on the first payment, and a parcel paid for before a pause still goes out (22 Nov 2026)."""

import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError

from .billing import complete_card_payment, paid_before_pause, run_debits, start_cycle_payment
from .models import (
    AuditEvent, ClinicalTask, MedicationDose, PatientEvent, Shipment, SubscriptionCycle, TreatmentAuthorization,
)
from .operations import shipment_hold_reason
from .parcels import first_shipping_day, schedule_parcels
from .prescriptions import MOVED_PARCEL, ask_to_confirm, confirm_prescription, start_check
from .test_billing import BillingFixture
from .treatment import PATIENT_STOPPED, change_authorization_status, change_subscription_status


class StartOnFirstPaymentTests(BillingFixture):
    def prescribed(self, days_ago, weeks=12):
        """The prescription was written days_ago, with its parcels planned from then."""
        start = self.today - timedelta(days=days_ago)
        TreatmentAuthorization.objects.filter(pk=self.auth.pk).update(starts_on=start, expires_on=start + timedelta(weeks=weeks))
        self.auth.refresh_from_db()
        schedule_parcels(self.auth)
        return start

    def test_a_course_prescribed_two_weeks_before_the_first_payment_starts_on_the_payment(self):
        start = self.prescribed(14)
        review_due = self.plan.review_due_on
        old_parcels = set(Shipment.objects.values_list('pk', flat=True))
        self.debit_order()
        self.auth.refresh_from_db()
        # Valid from the payment; its weeks now run from the first pens that payment can have, two weeks later than
        # when it was written (25 Nov 2026).
        first_pens = first_shipping_day(self.today, self.company)
        moved = (first_pens - first_shipping_day(start, self.company)).days  # Three weeks on a Monday after the cut-off.
        self.assertIn(moved, (14, 21))
        self.assertEqual((self.auth.starts_on, self.auth.expires_on), (self.today, start + timedelta(weeks=12, days=moved)))
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.review_due_on, review_due + timedelta(days=moved))
        # The old drafts are cancelled and the parcels planned again from the first pens.
        self.assertTrue(all(status == 'cancelled' and reason == MOVED_PARCEL for status, reason in
                            Shipment.objects.filter(pk__in=old_parcels).values_list('status', 'hold_reason')))
        first = Shipment.objects.exclude(status='cancelled').order_by('scheduled_for').first()
        self.assertEqual((first.scheduled_for, first.cycle_number), (first_pens, 1))
        # The doctor sees it in the prescription's log line, the patient on their timeline, and it's audited.
        self.assertIn(f'Start moved from {start:%-d %b %Y}', self.auth.log_text)
        self.assertIn(f'valid to {self.auth.expires_on:%-d %b %Y}', self.auth.log_text)
        self.assertTrue(PatientEvent.objects.filter(title='Treatment start moved', patient=self.patient).exists())
        self.assertTrue(AuditEvent.objects.filter(action='prescription.start_moved', metadata__days=moved).exists())
        # Within the first payment period, nobody needs to confirm anything.
        self.assertFalse(self.auth.needs_confirmation)
        self.assertFalse(ClinicalTask.objects.exists())
        # Later payments leave it alone.
        run_debits(self.company, today=first_pens + timedelta(weeks=3))
        self.assertEqual(SubscriptionCycle.objects.filter(status='paid').count(), 2)
        self.auth.refresh_from_db()
        self.assertEqual(self.auth.starts_on, self.today)

    def test_every_step_of_a_starting_plan_moves_together(self):
        higher = MedicationDose.objects.create(company=self.company, pen=self.dose.pen, amount_mg='2', price='3295')
        key, start = uuid.uuid4(), self.today - timedelta(days=10)
        TreatmentAuthorization.objects.filter(pk=self.auth.pk).update(
            plan_key=key, plan_step=1, starts_on=start, expires_on=start + timedelta(weeks=4, days=-1))
        step_two = TreatmentAuthorization.objects.create(
            company=self.company, patient=self.patient, prescribed_by=self.doctor, product=self.dose.pen, dose=higher,
            max_dose=higher.label, quantity_per_cycle=1, plan_key=key, plan_step=2,
            starts_on=start + timedelta(weeks=4), expires_on=start + timedelta(weeks=12))
        self.debit_order()
        self.auth.refresh_from_db()
        step_two.refresh_from_db()
        # The first step is valid from the payment; each step moves by the whole weeks its first pens moved.
        moved = first_shipping_day(self.today, self.company) - first_shipping_day(start, self.company)
        self.assertEqual((self.auth.starts_on, self.auth.expires_on), (self.today, start + timedelta(weeks=4, days=-1) + moved))
        self.assertEqual((step_two.starts_on, step_two.expires_on),
                         (start + timedelta(weeks=4) + moved, start + timedelta(weeks=12) + moved))

    def test_nothing_moves_once_a_parcel_has_gone_out_or_on_the_day_it_was_prescribed(self):
        start = self.prescribed(14)
        Shipment.objects.filter(pk=Shipment.objects.order_by('scheduled_for').first().pk).update(status='dispatched')
        self.debit_order()
        self.auth.refresh_from_db()
        self.assertEqual(self.auth.starts_on, start)
        self.assertFalse(AuditEvent.objects.filter(action='prescription.start_moved').exists())

    def test_a_first_payment_made_days_late_starts_the_first_period_that_day_too(self):
        start = self.prescribed(20)
        self.debit_order(fail=True)
        cycle = SubscriptionCycle.objects.get()
        # The card was declined five days ago; the patient pays now.
        late = self.today - timedelta(days=5)
        SubscriptionCycle.objects.filter(pk=cycle.pk).update(starts_on=late, due_on=late, ends_on=late + timedelta(weeks=4, days=-1))
        self.plan.next_debit_on = late + timedelta(weeks=4)
        self.plan.save(update_fields=('next_debit_on',))
        payment, _ = start_cycle_payment(cycle=cycle, actor=self.patient_user)
        complete_card_payment(payment=payment, paid=True)
        cycle.refresh_from_db()
        self.plan.refresh_from_db()
        self.auth.refresh_from_db()
        # The period starts with the first parcel today's payment can make; the next payment is a week before the next.
        first_pens = first_shipping_day(self.today, self.company)
        self.assertEqual((cycle.status, cycle.starts_on, cycle.ends_on), ('paid', first_pens, first_pens + timedelta(weeks=4, days=-1)))
        self.assertEqual(self.plan.next_debit_on, first_pens + timedelta(weeks=3))
        self.assertEqual(self.auth.starts_on, self.today)
        self.assertNotEqual(self.auth.starts_on, start)


class PausedParcelTests(BillingFixture):
    def setUp(self):
        super().setUp()
        self.debit_order()  # Pays the four weeks from the first parcel.
        self.paid = self.parcel(self.first_parcel)
        self.later = self.parcel(self.first_parcel + timedelta(weeks=4))

    def parcel(self, day):
        shipment = Shipment.objects.create(company=self.company, patient=self.patient, subscription=self.plan,
                                           authorization=self.auth, scheduled_for=day)
        shipment.items.create(company=self.company, product=self.dose.pen, dose=self.auth.max_dose, quantity=1)
        return shipment

    def pause(self):
        change_subscription_status(subscription=self.plan, actor=self.patient_user, action='pause', confirm=True)
        for shipment in (self.paid, self.later):
            shipment.refresh_from_db()

    def test_a_parcel_paid_for_before_the_pause_still_goes_out_and_later_ones_wait(self):
        self.pause()
        self.assertEqual(self.paid.status, 'draft')
        self.assertTrue(paid_before_pause(self.paid))
        self.assertEqual(shipment_hold_reason(self.paid), '')
        self.assertEqual((self.later.status, self.later.hold_reason), ('held', PATIENT_STOPPED))
        self.assertFalse(paid_before_pause(self.later))

    def test_resuming_puts_the_held_parcels_back_on_the_list(self):
        self.pause()
        change_subscription_status(subscription=self.plan, actor=self.patient_user, action='resume', confirm=True)
        self.later.refresh_from_db()
        self.assertEqual((self.later.status, self.later.hold_reason), ('draft', ''))
        self.assertFalse(paid_before_pause(self.paid))

    def test_nothing_goes_out_when_the_doctor_pauses_the_treatment(self):
        change_authorization_status(authorization=self.auth, actor=self.doctor, action='pause')
        self.paid.refresh_from_db()
        self.assertFalse(paid_before_pause(self.paid))
        self.assertNotEqual(shipment_hold_reason(self.paid), '')


class DoctorConfirmsOldPrescriptionTests(BillingFixture):
    """More than a first payment period (4 or 5 weeks) after the prescription was written, the doctor confirms it: before
    the patient can start, or before the first parcel goes out when the first payment came that late."""

    def written(self, days_ago):
        start = self.today - timedelta(days=days_ago)
        TreatmentAuthorization.objects.filter(pk=self.auth.pk).update(starts_on=start, expires_on=start + timedelta(weeks=12))
        self.auth.refresh_from_db()
        schedule_parcels(self.auth)
        return start

    def test_a_patient_cant_start_on_an_old_prescription_until_the_doctor_confirms_it(self):
        start = self.written(35)
        check = start_check(self.auth)
        self.assertEqual((check['written_on'], check['weeks'], check['asked']), (start, 4, False))
        self.assertIsNone(start_check(self.auth, day=start + timedelta(days=28)))  # On the last day of the period, fine.
        with self.assertRaises(ValidationError):
            ask_to_confirm(company=self.company, patient=self.patient, actor=self.doctor, authorization=self.auth)
        ask_to_confirm(company=self.company, patient=self.patient, actor=self.patient_user, authorization=self.auth)
        task = ClinicalTask.objects.get()
        self.assertEqual((task.assigned_to, task.priority, task.status), (self.doctor, 'high', 'open'))
        self.assertTrue(start_check(self.auth)['asked'])
        # Asking twice makes one task.
        ask_to_confirm(company=self.company, patient=self.patient, actor=self.patient_user, authorization=self.auth)
        self.assertEqual(ClinicalTask.objects.count(), 1)
        confirm_prescription(company=self.company, patient=self.patient, actor=self.doctor, authorization=self.auth)
        self.auth.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual((self.auth.starts_on, self.auth.needs_confirmation, task.status), (self.today, False, 'done'))
        self.assertIsNone(start_check(self.auth))
        self.assertIn('confirmed it', self.auth.log_text)
        with self.assertRaises(ValidationError):
            confirm_prescription(company=self.company, patient=self.patient, actor=self.doctor, authorization=self.auth)

    def test_a_first_payment_more_than_a_period_late_waits_for_the_doctor_before_anything_ships(self):
        start = self.written(42)
        self.debit_order()
        self.auth.refresh_from_db()
        self.assertEqual((self.auth.starts_on, self.auth.needs_confirmation), (self.today, True))
        task = ClinicalTask.objects.get()
        self.assertIn(f'{start:%-d %b %Y}', task.description)
        first = Shipment.objects.exclude(status='cancelled').order_by('scheduled_for').first()
        self.assertIn('to confirm the prescription before the first parcel goes out', shipment_hold_reason(first))
        # Nothing more is charged while it waits.
        run_debits(self.company, today=self.today + timedelta(weeks=4))
        self.assertEqual(SubscriptionCycle.objects.count(), 1)
        # The doctor confirms three days after the payment: the paid period, the course and the parcels start that day.
        paid_on = self.today - timedelta(days=3)
        cycle = SubscriptionCycle.objects.get()
        SubscriptionCycle.objects.filter(pk=cycle.pk).update(starts_on=paid_on, due_on=paid_on, ends_on=paid_on + timedelta(weeks=4, days=-1))
        TreatmentAuthorization.objects.filter(pk=self.auth.pk).update(starts_on=paid_on, expires_on=paid_on + timedelta(weeks=12))
        self.plan.next_debit_on = paid_on + timedelta(weeks=4)
        self.plan.save(update_fields=('next_debit_on',))
        confirm_prescription(company=self.company, patient=self.patient, actor=self.doctor, authorization=self.auth)
        cycle.refresh_from_db()
        self.plan.refresh_from_db()
        self.auth.refresh_from_db()
        # The paid period starts with the first parcel the confirmation can make.
        first_pens = first_shipping_day(self.today, self.company)
        self.assertEqual((cycle.starts_on, self.plan.next_debit_on), (first_pens, first_pens + timedelta(weeks=3)))
        # Valid from the confirmation, unless its first pens are where they were (confirmed before the same cut-off).
        same_week = first_shipping_day(paid_on, self.company) == first_pens
        self.assertEqual((self.auth.starts_on, self.auth.needs_confirmation), (paid_on if same_week else self.today, False))
        first = Shipment.objects.exclude(status='cancelled').order_by('scheduled_for').first()
        self.assertEqual(shipment_hold_reason(first), '')
        self.assertEqual(first.scheduled_for, first_pens)


class LatePaymentTests(BillingFixture):
    """A payment that failed completely stops billing; paid late after its parcel was missed, the period starts again
    that day, the missed pens go out on the next shipping day and the course carries on from there (22 Nov 2026)."""

    def history(self, weeks_ago):
        """Prescribed and first paid weeks_ago; the first parcel went out, the next period's payment failed completely
        and its parcel was held; one more parcel is planned."""
        from .billing import approve_mandate, bill_subscription, start_debit_order
        from .models import DebitMandate

        start = self.today - timedelta(weeks=weeks_ago)
        TreatmentAuthorization.objects.filter(pk=self.auth.pk).update(starts_on=start, expires_on=start + timedelta(weeks=24))
        self.auth.refresh_from_db()
        self.plan.next_debit_on = self.today + timedelta(days=1)
        self.plan.save(update_fields=('next_debit_on',))
        mandate, _ = start_debit_order(subscription=self.plan, actor=self.patient_user)
        approve_mandate(mandate=mandate, account_label='Visa ···4242', actor=self.patient_user)
        self.plan.next_debit_on = start
        self.plan.save(update_fields=('next_debit_on',))
        bill_subscription(self.plan, today=start)
        DebitMandate.objects.filter(pk=mandate.pk).update(provider_reference=f'sandbox-decline-{mandate.pk}')
        first_parcel = first_shipping_day(start, self.company)
        second = first_parcel + timedelta(weeks=3)  # A week before the second parcel, then each retry.
        for day in range(8):
            bill_subscription(self.plan, today=second + timedelta(days=day))
        self.auth.refresh_from_db()  # Its weeks now run from its first pens.
        parcels = []
        for number, (day, status) in enumerate(((first_parcel, 'delivered'), (first_parcel + timedelta(weeks=4), 'held'),
                                                (first_parcel + timedelta(weeks=8), 'draft')), start=1):
            parcel = Shipment.objects.create(company=self.company, patient=self.patient, subscription=self.plan,
                                             authorization=self.auth, cycle_number=number, scheduled_for=day, status=status,
                                             hold_reason='Not paid: payment failed.' if status == 'held' else '')
            parcel.items.create(company=self.company, product=self.dose.pen, dose=self.auth.max_dose, quantity=1)
            parcels.append(parcel)
        return start, parcels, SubscriptionCycle.objects.get(cycle_number=2)

    def pay(self, cycle):
        payment, _ = start_cycle_payment(cycle=cycle, actor=self.patient_user)
        complete_card_payment(payment=payment, paid=True)
        cycle.refresh_from_db()

    def test_a_late_payment_restarts_the_period_and_the_missed_pens_go_out_next(self):
        from portal.patient_summary import treatment_timeline
        from .prescriptions import moved_note

        start, (sent, missed, later), cycle = self.history(5)
        self.assertEqual(cycle.status, 'failed')
        # Billing stops while it's unpaid: no more periods, so no debt for weeks without pens.
        run_debits(self.company, today=self.today + timedelta(weeks=4))
        self.assertEqual(SubscriptionCycle.objects.count(), 2)
        ends_before = self.auth.expires_on
        self.pay(cycle)
        # The missed pens go out on the first shipping day whose order cut-off is still to come, and the period paid
        # for starts with them.
        new_day = first_shipping_day(self.today, self.company)
        shift = new_day - missed.scheduled_for
        self.plan.refresh_from_db()
        self.auth.refresh_from_db()
        self.assertEqual((cycle.status, cycle.starts_on, self.plan.next_debit_on),
                         ('paid', new_day, new_day + timedelta(weeks=3)))
        missed.refresh_from_db()
        later.refresh_from_db()
        self.assertEqual((missed.status, missed.hold_reason), ('cancelled', 'Missed: payment didn’t go through'))
        replacement = Shipment.objects.get(scheduled_for=new_day)
        self.assertEqual((replacement.status, replacement.items.get().quantity), ('draft', 1))
        self.assertEqual(later.scheduled_for, missed.scheduled_for + timedelta(weeks=4) + shift)
        self.assertEqual(self.auth.expires_on, ends_before + shift)
        self.assertEqual(moved_note(self.auth), {'from': ends_before, 'because': 'after a missed payment'})
        self.assertIn('Carried on', self.auth.log_text)
        self.assertFalse(self.auth.needs_confirmation)
        # The timeline keeps the missed parcel in its week and shows the pens going out.
        parcels = [(item['when'], item['missed']) for week in treatment_timeline(self.company, self.patient, self.auth)['weeks']
                   for item in week['parcels']]
        self.assertIn(('missed · payment didn’t go through', True), parcels)
        self.assertIn((f'ships {new_day:%a, %-d %b}', False), parcels)
        # Billing carries on from the new start: the next payment a week before the next parcel.
        run_debits(self.company, today=new_day + timedelta(weeks=3))
        self.assertEqual(SubscriptionCycle.objects.order_by('-cycle_number').first().starts_on, new_day + timedelta(weeks=4))

    def test_after_more_than_a_period_without_pens_the_doctor_confirms_first(self):
        start, (sent, missed, later), cycle = self.history(9)
        self.pay(cycle)
        self.auth.refresh_from_db()
        self.assertTrue(self.auth.needs_confirmation)
        self.assertTrue(ClinicalTask.objects.filter(title__startswith='Confirm prescription', status='open').exists())
        replacement = Shipment.objects.filter(status='draft').order_by('scheduled_for').first()
        self.assertIn('to confirm the prescription', shipment_hold_reason(replacement))
        run_debits(self.company, today=self.today + timedelta(weeks=4))
        self.assertEqual(SubscriptionCycle.objects.exclude(status='cancelled').count(), 2)  # Nothing charged meanwhile.
        confirm_prescription(company=self.company, patient=self.patient, actor=self.doctor, authorization=self.auth)
        self.auth.refresh_from_db()
        self.assertFalse(self.auth.needs_confirmation)
        self.assertEqual(shipment_hold_reason(replacement), '')


class DoseChangeIntervalTests(BillingFixture):
    """A prescription changed to a dose billed every 5 weeks: the period running now becomes 5 weeks long from its start,
    so the next payment matches the new dose's parcels."""

    def test_the_period_running_now_takes_the_new_doses_interval(self):
        from .prescriptions import change_prescription

        self.debit_order()  # Paid today, for the 4 weeks from its parcel.
        first = self.first_parcel
        five_weekly = MedicationDose.objects.create(company=self.company, pen=self.dose.pen, amount_mg='2', price='3295',
                                                    billing_weeks=5, dispatch_weeks=5)
        prescription = change_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=self.auth,
                                           dose=five_weekly, weeks=16, acknowledge_flags=True)
        # Its first parcel is the one already paid for, so that period runs 5 weeks from it.
        self.assertEqual(Shipment.objects.filter(authorization=prescription).order_by('scheduled_for').first().scheduled_for, first)
        cycle = SubscriptionCycle.objects.get()
        self.plan.refresh_from_db()
        next_on = first + timedelta(weeks=4)  # A week before the second 5-weekly parcel.
        self.assertEqual((cycle.ends_on, self.plan.next_debit_on), (first + timedelta(weeks=5, days=-1), next_on))
        # The doctor's clinical log and the patient's prescription changes say when payments now come (25 Nov 2026).
        prescription.refresh_from_db()
        self.assertTrue(prescription.log_text.endswith(f'Payments now: every 5 weeks, the next on {next_on:%-d %b %Y}.'))
        update = PatientEvent.objects.get(title='Your payment days changed')
        self.assertEqual(update.detail, 'With your new dose, payments are now every 5 weeks, in step with your pens. '
                                        f'The next is on {next_on:%a, %-d %b %Y}.')
        run_debits(self.company, today=next_on)
        latest = SubscriptionCycle.objects.order_by('-cycle_number').first()
        self.assertEqual((latest.starts_on, latest.ends_on), (first + timedelta(weeks=5), first + timedelta(weeks=10, days=-1)))


class PrescriptionEndTests(BillingFixture):
    """Nothing is taken for a period the prescription sends no pens in (James, 25 Nov 2026): a 12-week prescription paid
    every 4 weeks has three payments, the last a week before its third parcel, not a fourth for weeks after it ends.
    Renewing in time carries billing on; renewing after it ended starts payments again that day, with pens on the first
    shipping day whose order cut-off is still to come."""

    def prescribed(self, weeks_ago, weeks=12):
        """Prescribed and first paid weeks_ago, for weeks, with each payment since taken on its day (the daily run)."""
        from .billing import approve_mandate, bill_subscription, start_debit_order

        start = self.today - timedelta(weeks=weeks_ago)
        # As the app writes it: valid from start, its weeks from its first pens.
        TreatmentAuthorization.objects.filter(pk=self.auth.pk).update(
            starts_on=start, expires_on=first_shipping_day(start, self.company) + timedelta(weeks=weeks))
        self.auth.refresh_from_db()
        self.plan.next_debit_on = self.today + timedelta(days=1)
        self.plan.save(update_fields=('next_debit_on',))
        mandate, _ = start_debit_order(subscription=self.plan, actor=self.patient_user)
        approve_mandate(mandate=mandate, account_label='Visa ···4242', actor=self.patient_user)
        self.plan.next_debit_on = start
        self.plan.save(update_fields=('next_debit_on',))
        day = start
        while day <= self.today:
            bill_subscription(self.plan, today=day)
            day += timedelta(days=1)
        self.plan.refresh_from_db()
        self.first = first_shipping_day(start, self.company)
        return start

    def starts(self):
        return list(SubscriptionCycle.objects.exclude(status='cancelled').order_by('cycle_number').values_list('starts_on', flat=True))

    def test_the_last_payment_is_the_one_before_the_prescription_ends(self):
        from .subscription_discounts import payment_schedule

        start = self.prescribed(0)
        first = self.first
        # Paid today, then a week before the second and third parcels; the 12 weeks run from the first.
        payments = [start, first + timedelta(weeks=3), first + timedelta(weeks=7)]
        self.assertEqual([row['due_on'] for row in payment_schedule(self.plan)['rows']], payments)
        for week in (3, 7, 11, 15):
            run_debits(self.company, today=first + timedelta(weeks=week))
        self.assertEqual(self.starts(), [first, first + timedelta(weeks=4), first + timedelta(weeks=8)])
        self.plan.refresh_from_db()
        # Billing waits where it would carry on after a renewal; nothing more is due meanwhile.
        self.assertEqual(self.plan.next_debit_on, first + timedelta(weeks=11))
        self.assertIsNone(self.plan.next_payment_on)
        self.assertEqual(self.plan.prescription_ends_on, first + timedelta(weeks=12))

    def test_renewing_in_time_carries_billing_on(self):
        from .prescriptions import renew_prescription

        self.prescribed(0)
        first = self.first
        self.auth.refresh_from_db()
        renewal = renew_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=self.auth,
                                     weeks=12, acknowledge_flags=True)
        for week in (3, 7, 11):
            run_debits(self.company, today=first + timedelta(weeks=week))
        self.assertEqual(self.starts()[-1], first + timedelta(weeks=12))
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.next_payment_on, first + timedelta(weeks=15))
        renewal.refresh_from_db()
        self.assertNotIn('Payments', renewal.log_text)  # The payment days didn't move.

    def test_renewing_after_it_ended_starts_payments_that_day_and_the_pens_go_out_next(self):
        from .prescriptions import renew_prescription

        self.prescribed(14)  # Ended a week or two ago, after three payments and three parcels.
        self.auth.refresh_from_db()
        for number, week in enumerate((0, 4, 8), start=1):
            parcel = Shipment.objects.create(company=self.company, patient=self.patient, subscription=self.plan,
                                             authorization=self.auth, cycle_number=number, status='delivered',
                                             scheduled_for=self.first + timedelta(weeks=week))
            parcel.items.create(company=self.company, product=self.dose.pen, dose=self.auth.max_dose, quantity=1)
        self.assertEqual(len(self.starts()), 3)
        self.assertIsNone(self.plan.next_payment_on)
        renewal = renew_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=self.auth,
                                     weeks=12, acknowledge_flags=True)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.next_payment_on, self.today)
        first_pens = first_shipping_day(self.today, self.company)
        first = Shipment.objects.filter(status='draft').order_by('scheduled_for').first()
        self.assertEqual(first.scheduled_for, first_pens)
        run_debits(self.company, today=self.today)
        latest = SubscriptionCycle.objects.order_by('-cycle_number').first()
        self.assertEqual((latest.due_on, latest.starts_on, latest.status), (self.today, first_pens, 'paid'))
        renewal.refresh_from_db()
        self.assertTrue(renewal.log_text.endswith(f'Payments start again: every 4 weeks, the next on {self.today:%-d %b %Y}.'))
        self.assertTrue(PatientEvent.objects.filter(title='Your payments carry on').exists())
