"""This parcel and everything after it (James, 25 Nov 2026): moving a prescription parcel on its shipment page can take
the rest of the treatment with it, by the same whole weeks: the patient's later parcels, the payment periods and the
next payment (a week before its parcel), the prescription's end and any later plan step, and the care plan's review
date. The clinical log notes it and the patient sees it in Prescription changes."""

from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.utils import timezone

from .billing import next_payment_on
from .models import (
    MedicationDose, MedicationProduct, PatientEvent, Shipment, ShipmentItem, StartingPlan, StartingPlanStep, SubscriptionCycle,
    TreatmentAuthorization,
)
from .operations import change_parcel
from .parcels import schedule_parcels
from .prescriptions import PARCEL_MOVED, move_with_later_parcels, moved_note, moves_with_later
from .test_billing import BillingFixture

WEEK = timedelta(weeks=1)


class MoveWithLaterTests(BillingFixture):
    def setUp(self):
        super().setUp()
        self.first = self.first_parcel
        # As the app writes it today: valid from today, its 24 weeks from its first pens, its parcels planned; paid today.
        TreatmentAuthorization.objects.filter(pk=self.auth.pk).update(expires_on=self.first + 24 * WEEK)
        self.auth.refresh_from_db()
        schedule_parcels(self.auth)
        self.debit_order()
        self.plan.refresh_from_db()

    def parcels(self):
        return list(Shipment.objects.exclude(status=Shipment.Status.CANCELLED).order_by('scheduled_for')
                    .values_list('scheduled_for', flat=True))

    def weeks(self, *numbers):
        return [self.first + number * WEEK for number in numbers]

    def parcel(self, weeks):
        return Shipment.objects.get(scheduled_for=self.first + weeks * WEEK)

    def move(self, parcel, day):
        return change_parcel(shipment=parcel, actor=self.admin, scheduled_for=day, with_later=True,
                             quantities={item.pk: item.quantity for item in parcel.items.all()}).moved_with_later

    def test_a_week_earlier_brings_the_later_parcels_the_next_payment_and_the_end_with_it(self):
        self.assertEqual(self.parcels(), self.weeks(0, 4, 8, 12, 16, 20))
        self.assertEqual(self.plan.next_debit_on, self.first + 3 * WEEK)
        review = self.plan.review_due_on
        Shipment.objects.filter(pk=self.parcel(8).pk).update(status=Shipment.Status.READY)
        moved = self.move(self.parcel(4), self.first + 3 * WEEK)
        self.assertEqual(moved, {'weeks': -1, 'parcels': 4, 'next_payment': self.first + 2 * WEEK,
                                 'ends_on': self.first + 23 * WEEK})
        self.assertEqual(self.parcels(), self.weeks(0, 3, 7, 11, 15, 19))
        # A later parcel marked ready goes back to draft, to be checked again.
        self.assertEqual(Shipment.objects.get(scheduled_for=self.first + 7 * WEEK).status, Shipment.Status.DRAFT)
        # Still valid from the payment; it ends a week sooner, with its last parcel's pens.
        self.auth.refresh_from_db()
        self.assertEqual((self.auth.starts_on, self.auth.expires_on), (self.today, self.first + 23 * WEEK))
        # The period paid for ends with the moved parcel, whose payment comes a week before it.
        cycle = SubscriptionCycle.objects.get()
        self.assertEqual((cycle.due_on, cycle.starts_on, cycle.ends_on), (self.today, self.first, self.first + 3 * WEEK - timedelta(days=1)))
        self.plan.refresh_from_db()
        self.assertEqual((self.plan.next_debit_on, next_payment_on(self.plan)), (self.first + 2 * WEEK, self.first + 2 * WEEK))
        self.assertEqual(self.plan.review_due_on, review - WEEK)
        # The clinical log and the "Moved from" note; the patient's Prescription changes card.
        self.assertIn(f'Parcel of {self.first + 4 * WEEK:%-d %b %Y} moved to {self.first + 3 * WEEK:%-d %b %Y} by ', self.auth.log_text)
        self.assertIn(f'with the parcels after it: a week earlier, valid to {self.first + 23 * WEEK:%-d %b %Y}.', self.auth.log_text)
        self.assertEqual(moved_note(self.auth), {'from': self.first + 24 * WEEK, 'because': PARCEL_MOVED})
        self.assertEqual(PatientEvent.objects.get(title='Parcels moved').detail, (
            f'Your parcel of {self.first + 4 * WEEK:%a, %-d %b} now goes out on {self.first + 3 * WEEK:%a, %-d %b}, and your '
            f'parcels after it a week earlier too. Your next payment is on {self.first + 2 * WEEK:%a, %-d %b}, and your '
            f'prescription now runs to {self.first + 23 * WEEK:%a, %-d %b %Y}.'))

        # Moved back, everything is as the doctor wrote it again.
        self.move(self.parcel(3), self.first + 4 * WEEK)
        self.assertEqual(self.parcels(), self.weeks(0, 4, 8, 12, 16, 20))
        self.auth.refresh_from_db()
        self.assertEqual(self.auth.expires_on, self.first + 24 * WEEK)
        self.assertIsNone(moved_note(self.auth))
        self.assertEqual(SubscriptionCycle.objects.get().ends_on, self.first + 4 * WEEK - timedelta(days=1))
        self.plan.refresh_from_db()
        self.assertEqual((self.plan.next_debit_on, self.plan.review_due_on), (self.first + 3 * WEEK, review))

    def test_the_first_parcel_a_week_later_moves_the_period_paid_for_with_it(self):
        self.move(self.parcel(0), self.first + WEEK)
        self.assertEqual(self.parcels(), self.weeks(1, 5, 9, 13, 17, 21))
        cycle = SubscriptionCycle.objects.get()
        self.assertEqual((cycle.due_on, cycle.starts_on, cycle.ends_on),
                         (self.today, self.first + WEEK, self.first + 5 * WEEK - timedelta(days=1)))
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.next_debit_on, self.first + 4 * WEEK)
        self.auth.refresh_from_db()
        self.assertEqual((self.auth.starts_on, self.auth.expires_on), (self.today, self.first + 25 * WEEK))

    def test_a_payment_brought_before_today_is_taken_today(self):
        # The day after the first parcel (a Wednesday), the second is brought forward to the next shipping day: its
        # payment, a week before it, would have been yesterday.
        wednesday = self.first + timedelta(days=1)
        with mock.patch('django.utils.timezone.now', return_value=timezone.now() + (wednesday - self.today)):
            moved = self.move(self.parcel(4), self.first + WEEK)
        self.assertEqual(moved['next_payment'], wednesday)
        self.assertEqual(self.parcels(), self.weeks(0, 1, 5, 9, 13, 17))
        self.assertEqual(SubscriptionCycle.objects.get().ends_on, self.first + WEEK - timedelta(days=1))

    def test_it_moves_by_whole_weeks_after_the_parcel_before_and_before_its_order_goes_in(self):
        second = self.parcel(4)
        with self.assertRaisesMessage(ValidationError, 'move this one by whole weeks: choose another'):
            self.move(second, self.first + 4 * WEEK + timedelta(days=1))
        with self.assertRaisesMessage(ValidationError, 'Choose a day after the parcel before it'):
            self.move(second, self.first)
        Shipment.objects.filter(pk=self.parcel(8).pk).update(status=Shipment.Status.READY, locked_at=timezone.now())
        with self.assertRaisesMessage(ValidationError, 'is locked for shipping. Hold it first'):
            self.move(second, self.first + 5 * WEEK)
        Shipment.objects.filter(pk=self.parcel(8).pk).update(status=Shipment.Status.DELIVERED, locked_at=None)
        with self.assertRaisesMessage(ValidationError, 'has gone out. Move only this parcel.'):
            self.move(second, self.first + 5 * WEEK)
        Shipment.objects.filter(pk=self.parcel(8).pk).update(status=Shipment.Status.DRAFT)
        # A shipping day whose order cut-off has passed: its pens were ordered.
        with mock.patch('care.parcels.first_shipping_day', return_value=self.first + 4 * WEEK):
            with self.assertRaisesMessage(ValidationError, 'were ordered at its order cut-off'):
                self.move(second, self.first + 3 * WEEK)
        # Nothing moved.
        self.assertEqual(self.parcels(), self.weeks(0, 4, 8, 12, 16, 20))
        self.auth.refresh_from_db()
        self.assertEqual(self.auth.expires_on, self.first + 24 * WEEK)
        self.assertFalse(PatientEvent.objects.filter(title='Parcels moved').exists())
        # The same day (only the pens changed): nothing else moves.
        self.assertIsNone(self.move(second, second.scheduled_for))

    def test_only_a_parcel_its_prescription_planned_moves_what_comes_after(self):
        self.assertTrue(moves_with_later(self.parcel(4)))
        # Something that needs no prescription, even on the prescription (as the demo's creatine is), or no prescription.
        sachets = MedicationProduct.objects.create(company=self.company, name='Creatine', strength='5 g', price='300',
                                                   requires_authorisation=False)
        for authorization in (self.auth, None):
            extra = Shipment.objects.create(company=self.company, patient=self.patient, authorization=authorization,
                                            scheduled_for=self.first + 6 * WEEK, cycle_number=1)
            ShipmentItem.objects.create(company=self.company, shipment=extra, product=sachets, quantity=1)
            self.assertFalse(moves_with_later(extra))
            with self.assertRaisesMessage(ValidationError, 'Only a prescription parcel'):
                move_with_later_parcels(extra, self.first + 7 * WEEK, actor=self.admin)
            # And a later one isn't taken along.
            self.move(self.parcel(4), self.first + 5 * WEEK)
            self.assertEqual(Shipment.objects.get(pk=extra.pk).scheduled_for, self.first + 6 * WEEK)
            self.move(self.parcel(5), self.first + 4 * WEEK)
            extra.delete()

    def test_weekly_parcels_paid_every_four_weeks(self):
        weekly = MedicationDose.objects.create(company=self.company, pen=self.dose.pen, amount_mg='2', price='3295',
                                               dispatch_weeks=1, billing_weeks=4)
        Shipment.objects.all().delete()
        TreatmentAuthorization.objects.filter(pk=self.auth.pk).update(dose=weekly)
        self.auth.refresh_from_db()
        schedule_parcels(self.auth)
        self.assertEqual(self.parcels()[:4], self.weeks(0, 1, 2, 3))
        # The parcel before is a week before: it can't go earlier, only later.
        with self.assertRaisesMessage(ValidationError, 'Choose a day after the parcel before it'):
            self.move(self.parcel(2), self.first + WEEK)
        self.move(self.parcel(2), self.first + 3 * WEEK)
        self.assertEqual(self.parcels()[:4], self.weeks(0, 1, 3, 4))
        # The period paid for (four weeks from the first parcel) runs a week longer, and the next payment with it.
        self.assertEqual(SubscriptionCycle.objects.get().ends_on, self.first + 5 * WEEK - timedelta(days=1))
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.next_debit_on, self.first + 4 * WEEK)


class MoveWithLaterPlanTests(BillingFixture):
    def test_a_starting_plans_steps_still_meet(self):
        from .starting_plans import initiate_prescription

        pen = dict(company=self.company, name='Semaglutide', price='100.00', category=MedicationProduct.Category.WEIGHT_MANAGEMENT)
        quarter, half, one = (
            MedicationDose.objects.create(company=self.company, pen=MedicationProduct.objects.create(strength=f'{mg} mg', **pen),
                                          amount_mg=mg, price='1995')
            for mg in ('0.25', '0.5', '1'))
        plan = StartingPlan.objects.create(company=self.company, medicine='Semaglutide', then_dose=one, default_weeks=24)
        StartingPlanStep.objects.create(company=self.company, plan=plan, position=1, dose=quarter, weeks=4)
        StartingPlanStep.objects.create(company=self.company, plan=plan, position=2, dose=half, weeks=4)
        first = initiate_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=self.auth,
                                      plan=plan, weeks=24, acknowledge_flags=True)

        def steps():
            return list(TreatmentAuthorization.objects.filter(plan_key=first.plan_key).exclude(
                status=TreatmentAuthorization.Status.CANCELLED).order_by('plan_step'))

        before = [(step.starts_on, step.expires_on) for step in steps()]
        second = Shipment.objects.filter(authorization=steps()[1]).order_by('scheduled_for').first()
        later = list(Shipment.objects.filter(scheduled_for__gte=second.scheduled_for).exclude(status=Shipment.Status.CANCELLED)
                     .order_by('scheduled_for').values_list('scheduled_for', flat=True))
        change_parcel(shipment=second, actor=self.admin, scheduled_for=second.scheduled_for - WEEK, with_later=True,
                      quantities={item.pk: item.quantity for item in second.items.all()})
        after = [(step.starts_on, step.expires_on) for step in steps()]
        # The first step keeps its start and ends a week sooner; the later steps move whole: they still meet.
        self.assertEqual(after[0], (before[0][0], before[0][1] - WEEK))
        self.assertEqual(after[1:], [(starts - WEEK, ends - WEEK) for starts, ends in before[1:]])
        self.assertEqual(after[1][0] - after[0][1], before[1][0] - before[0][1])
        self.assertEqual(list(Shipment.objects.filter(scheduled_for__gte=second.scheduled_for - WEEK).exclude(
            status=Shipment.Status.CANCELLED).order_by('scheduled_for').values_list('scheduled_for', flat=True)),
            [day - WEEK for day in later])
        self.assertEqual(moved_note(TreatmentAuthorization.objects.get(pk=first.pk))['from'], max(ends for _, ends in before))
