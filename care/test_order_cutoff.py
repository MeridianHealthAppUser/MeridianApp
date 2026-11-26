"""The order cut-off (James, 25 Nov 2026; a day and a time since Dr Czech, 26 Nov 2026): pens are ordered at the cut-off
(Monday at 15:00 by default) for the next shipping day (Tuesday), so a first payment received by then has its parcel
that Tuesday and one received later the Tuesday after; each payment after the first is taken a week before its parcel,
with a retry on the cut-off day."""

from datetime import date, datetime, time, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase

from practices.models import Company

from .billing import run_debits
from .models import DebitMandate, PatientEvent, PracticeSettings, Shipment, SubscriptionCycle, TreatmentAuthorization
from .parcels import (PAYMENT_LEAD, cutoff_days, first_shipping_day, order_cutoff, order_cutoff_text,
                      schedule_parcels)
from .test_billing import BillingFixture

HERE = ZoneInfo('Africa/Johannesburg')


def at(day, hour, minute=0, month=11):
    return datetime(2026, month, day, hour, minute, tzinfo=HERE)


# Nothing here is "today", whenever the tests run.
@mock.patch('django.utils.timezone.now', return_value=at(1, 9, month=1))
class FirstShippingDayTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Cut-off Practice', slug='cut-off-practice')
        PracticeSettings.objects.create(company=cls.company)

    def test_a_payment_by_monday_at_three_makes_the_next_tuesday(self, _now):
        self.assertEqual(cutoff_days(self.company), 1)
        for moment, parcel in ((at(24, 9), date(2026, 12, 1)), (at(26, 23, 59), date(2026, 12, 1)),
                               (at(29, 12), date(2026, 12, 1)), (at(30, 14, 59), date(2026, 12, 1)),
                               (at(30, 15), date(2026, 12, 8)), (at(30, 22), date(2026, 12, 8)),
                               (at(1, 8, month=12), date(2026, 12, 8))):  # The shipping day itself.
            self.assertEqual(first_shipping_day(moment, self.company), parcel, moment)
        # A day that isn't today counts from its start, as the morning's debit run does.
        self.assertEqual(first_shipping_day(date(2026, 11, 30), self.company), date(2026, 12, 1))
        self.assertEqual(order_cutoff(date(2026, 12, 1), self.company), date(2026, 11, 30))
        self.assertEqual(order_cutoff_text(date(2026, 12, 1), self.company), 'Mon, 30 Nov at 15:00')

    def test_today_means_now(self, now):
        now.return_value = at(30, 16)
        self.assertEqual(first_shipping_day(date(2026, 11, 30), self.company), date(2026, 12, 8))
        now.return_value = at(30, 10)
        self.assertEqual(first_shipping_day(date(2026, 11, 30), self.company), date(2026, 12, 1))

    def test_another_day_and_time(self, _now):
        PracticeSettings.objects.filter(company=self.company).update(
            order_cutoff_weekday=PracticeSettings.Weekday.THURSDAY, order_cutoff_time=time(17, 30))
        self.assertEqual(cutoff_days(self.company), 5)
        self.assertEqual(first_shipping_day(at(26, 17, 29), self.company), date(2026, 12, 1))
        self.assertEqual(first_shipping_day(at(26, 17, 30), self.company), date(2026, 12, 8))
        self.assertEqual(first_shipping_day(at(27, 8), self.company), date(2026, 12, 8))
        self.assertEqual(order_cutoff_text(date(2026, 12, 1), self.company), 'Thu, 26 Nov at 17:30')

    def test_a_cut_off_on_the_shipping_weekday_is_the_week_before(self, _now):
        PracticeSettings.objects.filter(company=self.company).update(order_cutoff_weekday=PracticeSettings.Weekday.TUESDAY)
        self.assertEqual(cutoff_days(self.company), 7)
        self.assertEqual(first_shipping_day(at(24, 10), self.company), date(2026, 12, 1))
        self.assertEqual(first_shipping_day(at(24, 15), self.company), date(2026, 12, 8))
        self.assertEqual(first_shipping_day(at(25, 8), self.company), date(2026, 12, 8))

    def test_a_practice_without_settings_uses_tuesday_and_monday_at_three(self, _now):
        other = Company.objects.create(name='No Settings', slug='no-settings')
        self.assertEqual(first_shipping_day(at(30, 14), other), date(2026, 12, 1))
        self.assertEqual(first_shipping_day(at(30, 15), other), date(2026, 12, 8))

    def test_new_settings_default_to_monday_at_three(self, _now):
        row = PracticeSettings.objects.get(company=self.company)
        self.assertEqual((row.order_cutoff_weekday, row.order_cutoff_time), (PracticeSettings.Weekday.MONDAY, time(15, 0)))


class CutoffBillingTests(BillingFixture):
    def setUp(self):
        super().setUp()
        self.first = first_shipping_day(self.today, self.company)

    def test_the_first_payment_pays_for_the_weeks_from_its_parcel_and_the_course_starts_with_it(self):
        # As the app writes it today: valid from today, its 24 weeks from its first pens; its parcels planned.
        TreatmentAuthorization.objects.filter(pk=self.auth.pk).update(expires_on=self.first + timedelta(weeks=24))
        self.auth.refresh_from_db()
        schedule_parcels(self.auth)
        ready = Shipment.objects.order_by('scheduled_for').first()
        Shipment.objects.filter(pk=ready.pk).update(status='ready')
        self.debit_order()  # Written and paid today.
        cycle = SubscriptionCycle.objects.get()
        self.assertEqual((cycle.due_on, cycle.starts_on, cycle.ends_on),
                         (self.today, self.first, self.first + timedelta(weeks=4, days=-1)))
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.next_debit_on, self.first + timedelta(weeks=4) - PAYMENT_LEAD)
        # Valid from the payment; its 24 weeks run from the first pens.
        self.auth.refresh_from_db()
        self.assertEqual((self.auth.starts_on, self.auth.expires_on), (self.today, self.first + timedelta(weeks=24)))
        parcels = list(Shipment.objects.exclude(status='cancelled').order_by('scheduled_for').values_list('scheduled_for', flat=True)[:2])
        self.assertEqual(parcels, [self.first, self.first + timedelta(weeks=4)])
        # Its parcels were already planned from its first pens, so the one marked ready stays ready.
        self.assertEqual(Shipment.objects.get(pk=ready.pk).status, 'ready')
        # Paid the day it was written: nothing moves.
        self.assertFalse(self.auth.log_text)
        self.assertIsNone(self.auth.ends_as_written)
        self.assertFalse(PatientEvent.objects.filter(title='Treatment start moved').exists())

    def test_each_payment_after_the_first_comes_a_week_before_its_parcel(self):
        self.debit_order()
        due = self.first + timedelta(weeks=3)
        self.assertEqual(run_debits(self.company, today=due - timedelta(days=1))['tried'], 0)
        run_debits(self.company, today=due)
        second = SubscriptionCycle.objects.get(cycle_number=2)
        self.assertEqual((second.due_on, second.starts_on, second.status), (due, self.first + timedelta(weeks=4), 'paid'))
        self.assertEqual(due.weekday(), self.first.weekday())  # The shipping weekday, a week before.

    def test_a_declined_repeat_payment_is_tried_again_on_the_order_cut_off(self):
        self.debit_order()
        DebitMandate.objects.update(provider_reference='sandbox-decline-cut-off')
        due = self.first + timedelta(weeks=3)
        parcel = self.first + timedelta(weeks=4)
        run_debits(self.company, today=due)
        second = SubscriptionCycle.objects.get(cycle_number=2)
        # The practice's retry days (3 and 7 after the payment date) and the cut-off day: Friday, Monday, Tuesday.
        self.assertEqual((second.attempts, second.next_attempt_on), (1, due + timedelta(days=3)))
        run_debits(self.company, today=second.next_attempt_on)
        second.refresh_from_db()
        self.assertEqual((second.attempts, second.next_attempt_on), (2, order_cutoff(parcel, self.company)))
        run_debits(self.company, today=second.next_attempt_on)
        second.refresh_from_db()
        self.assertEqual((second.attempts, second.next_attempt_on), (3, due + timedelta(days=7)))

    def test_paid_after_a_later_cut_off_than_it_was_written_moves_by_whole_weeks(self):
        written = self.today - timedelta(days=9)
        TreatmentAuthorization.objects.filter(pk=self.auth.pk).update(
            starts_on=written, expires_on=first_shipping_day(written, self.company) + timedelta(weeks=24))
        self.debit_order()
        self.auth.refresh_from_db()
        moved = self.first - first_shipping_day(written, self.company)
        self.assertIn(moved.days, (7, 14))
        self.assertEqual((self.auth.starts_on, self.auth.expires_on), (self.today, self.first + timedelta(weeks=24)))
        self.assertTrue(PatientEvent.objects.filter(title='Treatment start moved').exists())

    def test_a_changed_prescription_is_valid_today_and_its_weeks_run_from_its_pens(self):
        from .prescriptions import change_prescription, expiry_preview
        from .models import MedicationDose

        higher = MedicationDose.objects.create(company=self.company, pen=self.dose.pen, amount_mg='2', price='3295')
        prescription = change_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=self.auth,
                                           dose=higher, weeks=12, acknowledge_flags=True)
        self.assertEqual((prescription.starts_on, prescription.expires_on), (self.today, self.first + timedelta(weeks=12)))
        self.assertEqual(expiry_preview(12, self.today, self.company), prescription.expires_on)
        self.assertEqual(Shipment.objects.filter(authorization=prescription).count(), 3)  # Twelve weeks of pens.

    def test_a_dose_change_after_the_cut_off_sends_its_pens_the_week_after(self):
        from .prescriptions import change_prescription
        from .models import MedicationDose

        self.debit_order()
        higher = MedicationDose.objects.create(company=self.company, pen=self.dose.pen, amount_mg='2', price='3295')
        prescription = change_prescription(company=self.company, patient=self.patient, actor=self.doctor,
                                           current=TreatmentAuthorization.objects.get(pk=self.auth.pk), dose=higher,
                                           weeks=12, acknowledge_flags=True)
        first = Shipment.objects.filter(authorization=prescription).order_by('scheduled_for').first()
        self.assertEqual(first.scheduled_for, first_shipping_day(self.today, self.company))
        self.assertGreaterEqual(order_cutoff(first.scheduled_for, self.company), self.today)
