from decimal import Decimal
from django.urls import reverse
from care.models import SubscriptionCycle, SubscriptionDiscount
from .test_billing import BillingPageFixture


class SubscriptionPaymentPopupTests(BillingPageFixture):
    def url(self):
        return reverse('portal:subscription-payments', args=[self.plan.pk])

    def test_popup_has_paid_and_planned_rows_and_saves_a_discount_with_effective_date(self):
        self.approved()
        self.login(self.admin)
        page = self.client.get(reverse('portal:treatment-subscriptions'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, f'data-modal-open="subscription-payments-{self.plan.pk}"')
        self.assertContains(page, 'R3295.00 paid to date')
        self.assertContains(page, 'Payment progress')
        dialog = self.client.get(self.url())
        self.assertTrue(dialog.context['payment_open'])
        schedule = dialog.context['payment_schedule']
        self.assertEqual(schedule['paid_count'], 1)
        future = next(row for row in schedule['rows'] if row['projected'])
        result = self.client.post(self.url(), {
            'workflow_context': dialog.context['discount_token'], 'scope': 'future',
            'effective_on': future['due_on'], 'kind': 'amount', 'value': '500', 'reason': 'Loyalty discount'})
        self.assertEqual(result.status_code, 302)
        refreshed = self.client.get(self.url())
        rows = refreshed.context['payment_schedule']['rows']
        self.assertEqual(rows[0]['amount'], Decimal('3295'))
        self.assertEqual(next(row for row in rows if row['projected'])['amount'], Decimal('2795'))
        self.assertContains(refreshed, 'Loyalty discount')
        self.assertContains(refreshed, '−R500.00')
        self.assertEqual(SubscriptionCycle.objects.count(), 1)

    def test_one_planned_payment_and_validation_and_stale_token(self):
        self.login(self.admin)
        page = self.client.get(self.url())
        row = next(row for row in page.context['payment_schedule']['rows'] if row['editable'])
        data = {'workflow_context': page.context['discount_token'], 'scope': 'one', 'payment': row['key'],
                'effective_on': row['due_on'], 'kind': 'percent', 'value': '10'}
        invalid = self.client.post(self.url(), {**data, 'value': '110'})
        self.assertEqual(invalid.status_code, 400)
        self.assertTrue(invalid.context['discount_form'].errors)
        self.assertFalse(SubscriptionDiscount.objects.exists())
        self.assertEqual(self.client.post(self.url(), data).status_code, 302)
        replay = self.client.post(self.url(), data)
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(SubscriptionDiscount.objects.count(), 1)
        schedule = self.client.get(self.url()).context['payment_schedule']['rows']
        self.assertEqual(sum(bool(row['discount_amount']) for row in schedule), 1)

    def test_patients_and_doctors_cannot_read_or_edit_staff_discounts(self):
        for user in (self.patient_user, self.doctor):
            self.login(user)
            self.assertEqual(self.client.get(self.url()).status_code, 403)
            self.assertEqual(self.client.post(self.url(), {'kind': 'amount', 'value': '100'}).status_code, 403)
        self.assertFalse(SubscriptionDiscount.objects.exists())

    def test_popup_return_keeps_pagination_and_filters_and_closes_without_reopening(self):
        self.login(self.admin)
        page = self.client.get(self.url())
        data = {'workflow_context': page.context['discount_token'], 'scope': 'future',
                'effective_on': self.today, 'kind': 'amount', 'value': '100',
                'return_to': '/subscriptions/?page=2&status=active&status=paused'}
        response = self.client.post(self.url(), data)
        self.assertEqual(response.status_code, 302)
        self.assertIn('page=2', response.url)
        self.assertIn('status=active&status=paused', response.url)
        self.assertIn(f'payments={self.plan.pk}', response.url)
        self.assertEqual(SubscriptionDiscount.objects.count(), 1)
