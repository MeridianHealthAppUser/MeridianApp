"""A dose's new price from a date (care/dose_prices.py), set on its catalogue page."""
from datetime import timedelta
from decimal import Decimal

from django.core import mail
from django.urls import reverse

from care.billing import mandate_amount, run_debits, start_debit_order
from care.dose_prices import price_for
from care.models import DebitMandate, DosePriceChange, MedicationDose, SubscriptionCycle
from care.subscription_discounts import payment_schedule
from .test_billing import BillingPageFixture


class DosePriceChangeTests(BillingPageFixture):
    def dose_page(self):
        return self.client.get(reverse('portal:ops-dose-detail', args=[self.dose.pk]))

    def change_price(self, price, price_from, page=None):
        page = page or self.dose_page()
        return self.client.post(reverse('portal:ops-dose-detail', args=[self.dose.pk]), {
            'workflow_context': page.context['workflow_context'], 'pen': self.dose.pen_id, 'amount_mg': '1',
            'frequency': 'weekly', 'pens_per_dispatch': '1', 'dispatch_weeks': '4', 'price': price,
            'price_from': price_from.isoformat(), 'billing_weeks': '4', 'tier': 'standard', 'is_active': 'on'})

    def test_a_later_price_keeps_the_current_one_until_its_day_then_bills_and_asks_for_approval(self):
        mandate = self.approved()  # The first 4 weeks are paid at R3295.
        self.login(self.super_admin)
        day = self.today + timedelta(weeks=6)
        response = self.change_price('3495', day)
        self.assertRedirects(response, reverse('portal:ops-dose-detail', args=[self.dose.pk]), fetch_redirect_response=False)
        self.dose.refresh_from_db()
        self.assertEqual(self.dose.price, Decimal('3295'))
        page = self.dose_page()
        self.assertContains(page, f'R3495.00 from {day:%-d %b %Y}')
        self.assertContains(page, 'Cancel change')
        self.assertContains(self.client.get(reverse('portal:ops-catalogue')), f'R3495.00 from {day:%-d %b %Y}')
        # Planned payments before the day stay R3295; from it, R3495. Nobody is asked to approve anything yet.
        rows = [(row['due_on'], row['base']) for row in payment_schedule(self.plan)['rows'] if row['projected']]
        self.assertTrue(rows)
        for due_on, base in rows:
            self.assertEqual(base, Decimal('3495') if due_on >= day else Decimal('3295'), due_on)
        mandate.refresh_from_db()
        self.assertEqual(mandate.status, 'active')
        # Week 4 is collected at the current price.
        run_debits(self.company, today=self.today + timedelta(weeks=4))
        self.assertEqual(SubscriptionCycle.objects.get(cycle_number=2).amount, Decimal('3295'))
        # On the day the price becomes R3495, and the patient is asked to approve their debit order for it.
        with self.captureOnCommitCallbacks(execute=True):
            run_debits(self.company, today=day)
        self.dose.refresh_from_db()
        change = DosePriceChange.objects.get()
        self.assertEqual((self.dose.price, change.previous_price), (Decimal('3495'), Decimal('3295')))
        self.assertIsNotNone(change.applied_at)
        mandate.refresh_from_db()
        self.assertEqual(mandate.status, 'update_needed')
        self.assertIn('Please approve your new debit order', [message.subject for message in mail.outbox])
        # Week 8 is billed at the new price, and approving the new amount collects it.
        run_debits(self.company, today=self.today + timedelta(weeks=8))
        self.assertEqual(SubscriptionCycle.objects.get(cycle_number=3).amount, Decimal('3495'))
        self.plan.refresh_from_db()
        new, _ = start_debit_order(subscription=self.plan, actor=self.patient_user)
        self.assertEqual(new.amount, Decimal('3495'))
        # The page keeps the change as history.
        self.assertContains(self.dose_page(), f'R3295.00 → R3495.00 from {day:%-d %b %Y}')

    def test_today_changes_the_price_now(self):
        mandate = self.approved()
        self.login(self.super_admin)
        self.change_price('3495', self.today)
        self.dose.refresh_from_db()
        self.assertEqual(self.dose.price, Decimal('3495'))
        self.assertIsNotNone(DosePriceChange.objects.get().applied_at)
        mandate.refresh_from_db()
        self.assertEqual(mandate.status, 'update_needed')

    def test_a_scheduled_change_can_be_cancelled_and_a_past_date_is_refused(self):
        self.login(self.super_admin)
        day = self.today + timedelta(weeks=2)
        self.change_price('3495', day)
        page = self.dose_page()
        change = DosePriceChange.objects.get()
        self.client.post(reverse('portal:ops-dose-detail', args=[self.dose.pk]), {
            'workflow_context': page.context['workflow_context'], 'cancel_price_change': change.pk})
        change.refresh_from_db()
        self.assertIsNotNone(change.cancelled_at)
        self.assertEqual(price_for(MedicationDose.objects.get(pk=self.dose.pk), day), Decimal('3295'))
        self.assertNotContains(self.dose_page(), 'Cancel change')
        refused = self.change_price('3595', self.today - timedelta(days=1))
        self.assertEqual(refused.status_code, 400)
        self.assertContains(refused, 'Choose today or a later date.', status_code=400)
        self.assertEqual(DosePriceChange.objects.filter(cancelled_at__isnull=True).count(), 0)

    def test_each_scheduled_price_is_a_tag_with_an_arrow_against_the_price_before_it(self):
        self.login(self.super_admin)
        up, down = self.today + timedelta(weeks=2), self.today + timedelta(weeks=6)
        self.change_price('3495', up)
        self.change_price('3395', down)
        page = self.dose_page()
        self.assertEqual([change.direction for change in page.context['scheduled_prices']], ['up', 'down'])
        self.assertContains(page, f'<span class="sr-only">Up to </span>R3495.00 from {up:%-d %b %Y}')
        self.assertContains(page, f'<span class="sr-only">Down to </span>R3395.00 from {down:%-d %b %Y}')
        catalogue = self.client.get(reverse('portal:ops-catalogue'))
        self.assertContains(catalogue, 'title="Scheduled price increase"')
        self.assertNotContains(catalogue, 'R3395.00 from')  # The list shows the next change only.

    def test_a_new_dose_and_one_without_a_price_get_their_price_straight_away(self):
        MedicationDose.objects.filter(pk=self.dose.pk).update(price=None)
        self.login(self.super_admin)
        page = self.dose_page()
        self.assertNotIn('price_from', page.context['form'].fields)
        self.change_price('3295', self.today + timedelta(weeks=2), page=page)
        self.dose.refresh_from_db()
        self.assertEqual(self.dose.price, Decimal('3295'))
        self.assertFalse(DosePriceChange.objects.exists())

    def test_only_super_admins_change_prices(self):
        self.login(self.admin)
        page = self.dose_page()
        self.assertNotContains(page, 'New price applies from')
        self.change_price('3495', self.today + timedelta(weeks=2), page=page)
        self.assertFalse(DosePriceChange.objects.exists())

    def test_a_new_debit_order_covers_a_payment_already_waiting_at_a_higher_amount(self):
        mandate = self.approved()
        SubscriptionCycle.objects.create(company=self.company, patient=self.patient, subscription=self.plan, cycle_number=9,
                                         starts_on=self.today + timedelta(weeks=4), due_on=self.today + timedelta(weeks=4),
                                         amount=Decimal('3495'), mandate=mandate)
        self.assertEqual(mandate_amount(self.plan), Decimal('3495'))

    def test_the_approval_email_says_how_often_the_prescription_is_now_billed(self):
        mandate = self.approved()
        self.assertEqual(mandate.interval_weeks, 4)
        # Stepped up to a dose billed every 5 weeks (Tirzepatide 7.5 mg), at a new price.
        MedicationDose.objects.filter(pk=self.dose.pk).update(billing_weeks=5, price='3495')
        from care.billing import review_mandates

        with self.captureOnCommitCallbacks(execute=True):
            review_mandates(self.company)
        message = next(message for message in mail.outbox if message.subject == 'Please approve your new debit order')
        self.assertIn('every 5 weeks', message.body)
        self.assertEqual(DebitMandate.objects.get(pk=mandate.pk).status, 'update_needed')
