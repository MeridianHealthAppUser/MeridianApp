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
        self.assertContains(page, '<th scope="col">Payments</th>', html=True)
        self.assertNotContains(page, 'Payment progress')
        self.assertContains(page, 'subscription-payment-progress-link')
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


    def test_single_payment_editor_does_not_require_bottom_form_or_effective_date(self):
        self.login(self.admin)
        page = self.client.get(self.url())
        rows = page.context['payment_schedule']['rows']
        row = next(row for row in rows if row['editable'])
        self.assertContains(page, 'data-line-discount-form')
        self.assertEqual(list(page.context['bulk_discount_form'].fields['scope'].choices),
                         [('selected', 'Selected payments'), ('future', 'From an effective date')])
        result = self.client.post(self.url(), {'workflow_context': page.context['discount_token'], 'scope': 'one',
            'payment': row['key'], 'kind': 'amount', 'value': '500'})
        self.assertEqual(result.status_code, 302)
        after = self.client.get(self.url())
        changed = [row for row in after.context['payment_schedule']['rows'] if row['discount_amount']]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]['amount'], Decimal('2795'))

    def test_multi_select_updates_only_checked_payments_and_keeps_other_rows_unchanged(self):
        self.login(self.admin)
        page = self.client.get(self.url())
        rows = [row for row in page.context['payment_schedule']['rows'] if row['editable']]
        keys = [row['key'] for row in rows[:2]]
        result = self.client.post(self.url(), {'workflow_context': page.context['discount_token'], 'scope': 'selected',
            'payments': keys, 'kind': 'amount', 'value': '200'})
        self.assertEqual(result.status_code, 302)
        after = self.client.get(self.url())
        for row in after.context['payment_schedule']['rows']:
            self.assertEqual(row['discount_amount'], Decimal('200') if row['key'] in keys else Decimal('0'))
        self.assertEqual(SubscriptionDiscount.objects.count(), 2)
        import os
        from pathlib import Path
        if os.environ.get('MERIDIAN_PAYMENTS_PREVIEW'):
            listing = self.client.get(reverse('portal:treatment-subscriptions'), {'payments': self.plan.pk})
            Path(os.environ['MERIDIAN_PAYMENTS_PREVIEW']).write_bytes(listing.content)

    def test_multi_select_is_atomic_if_a_later_payment_changes(self):
        from unittest.mock import patch
        from django.core.exceptions import ValidationError
        from care.subscription_discounts import apply_discount
        self.login(self.admin)
        page = self.client.get(self.url())
        keys = [row['key'] for row in page.context['payment_schedule']['rows'] if row['editable']][:2]
        calls = []
        def change(**kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise ValidationError('The second payment changed. Refresh the schedule.')
            return apply_discount(**kwargs)
        with patch('portal.subscription_payment_views.apply_discount', side_effect=change):
            response = self.client.post(self.url(), {'workflow_context': page.context['discount_token'], 'scope': 'selected',
                'payments': keys, 'kind': 'amount', 'value': '100'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(SubscriptionDiscount.objects.exists())

    def test_single_payment_validation_errors_stay_with_its_row(self):
        self.login(self.admin)
        page = self.client.get(self.url())
        row = next(row for row in page.context['payment_schedule']['rows'] if row['editable'])
        invalid = self.client.post(self.url(), {'workflow_context': page.context['discount_token'], 'scope': 'one',
            'payment': row['key'], 'kind': 'percent', 'value': '110'})
        self.assertEqual(invalid.status_code, 400)
        opened = [row for row in invalid.context['payment_schedule']['rows'] if row['edit_open']]
        self.assertEqual([row['key'] for row in opened], [row['key']])
        self.assertTrue(opened[0]['discount_form'].errors)
        self.assertFalse(invalid.context['bulk_open'])
