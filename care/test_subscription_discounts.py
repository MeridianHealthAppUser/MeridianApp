from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import override_settings
from django.utils import timezone
from .test_billing import BillingFixture
from .models import DebitMandate, Invoice, Payment, SubscriptionCycle, SubscriptionDiscount
from .billing import _open_cycle, collect_cycle, bill_subscription
from .subscription_discounts import apply_discount, payment_schedule


class SubscriptionDiscountTests(BillingFixture):
    def discount(self, **kwargs):
        return apply_discount(subscription=self.plan, actor=self.admin, kind='amount', value=Decimal('500'),
                              effective_on=self.today, **kwargs)

    def cycle(self, **kwargs):
        defaults = dict(company=self.company, patient=self.patient, subscription=self.plan, cycle_number=1,
                        starts_on=self.today, due_on=self.today, amount=Decimal('3295'))
        defaults.update(kwargs)
        return SubscriptionCycle.objects.create(**defaults)

    def test_future_discount_updates_unpaid_cycles_and_invoice_but_not_paid_or_processing(self):
        paid = self.cycle(status='paid', paid_at=timezone.now())
        pending = self.cycle(cycle_number=2, starts_on=self.today + timedelta(weeks=4), due_on=self.today + timedelta(weeks=4))
        invoice = Invoice.objects.create(company=self.company, patient=self.patient, subscription_cycle=pending,
            invoice_number='discount-invoice', due_on=pending.due_on, subtotal=3295, total=3295, status='issued')
        processing = self.cycle(cycle_number=3, starts_on=self.today + timedelta(weeks=8), due_on=self.today + timedelta(weeks=8))
        payment = Payment.objects.create(company=self.company, patient=self.patient, subscription=self.plan,
            subscription_cycle=processing, amount=3295, due_on=processing.due_on, status='pending', provider='peach')
        self.discount()
        for cycle in (paid, pending, processing):
            cycle.refresh_from_db()
        invoice.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual((paid.amount, pending.base_amount, pending.discount_amount, pending.amount, processing.amount, payment.amount),
                         (Decimal('3295'), Decimal('3295'), Decimal('500'), Decimal('2795'), Decimal('3295'), Decimal('3295')))
        self.assertEqual((invoice.subtotal, invoice.total), (Decimal('3295'), Decimal('2795')))

    def test_one_cycle_discount_does_not_change_other_cycle_with_the_same_due_date(self):
        first = self.cycle()
        second = self.cycle(cycle_number=2)
        self.discount(payment_on=first.due_on, target_cycle=first)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.amount, second.amount), (Decimal('2795'), Decimal('3295')))

    def test_effective_date_and_future_uncreated_cycles_use_discounted_amount(self):
        effective = self.today + timedelta(weeks=4)
        apply_discount(subscription=self.plan, actor=self.admin, kind='percent', value=Decimal('10'), effective_on=effective)
        self.plan.next_debit_on = self.today
        self.plan.save(update_fields=['next_debit_on'])
        first = _open_cycle(self.plan, None)
        next_cycle = _open_cycle(self.plan, None)
        self.assertEqual((first.amount, next_cycle.base_amount, next_cycle.amount, next_cycle.discount_amount),
                         (Decimal('3295'), Decimal('3295'), Decimal('2965.50'), Decimal('329.50')))
        invoice = Invoice.objects.get(subscription_cycle=next_cycle)
        self.assertEqual((invoice.subtotal, invoice.total), (Decimal('3295'), Decimal('2965.50')))

    def test_paid_processing_foreign_and_unauthorized_discounts_are_rejected(self):
        cycle = self.cycle(status='paid')
        with self.assertRaises(ValidationError):
            self.discount(payment_on=cycle.due_on, target_cycle=cycle)
        with self.assertRaises(PermissionDenied):
            apply_discount(subscription=self.plan, actor=self.doctor, kind='amount', value=Decimal('100'), effective_on=self.today)
        with self.assertRaises(ValidationError):
            apply_discount(subscription=self.plan, actor=self.admin, kind='percent', value=Decimal('101'), effective_on=self.today)
        self.assertFalse(SubscriptionDiscount.objects.exists())

    def test_replacing_discount_does_not_stack_and_zero_restores_original_amount(self):
        cycle = self.cycle()
        self.discount()
        apply_discount(subscription=self.plan, actor=self.admin, kind='amount', value=Decimal('200'), effective_on=self.today)
        cycle.refresh_from_db()
        self.assertEqual((cycle.base_amount, cycle.amount), (Decimal('3295'), Decimal('3095')))
        apply_discount(subscription=self.plan, actor=self.admin, kind='amount', value=Decimal('0'), effective_on=self.today)
        cycle.refresh_from_db()
        self.assertEqual((cycle.amount, cycle.discount_amount), (Decimal('3295'), Decimal('0')))

    def test_fully_discounted_cycle_never_calls_a_provider_and_replays_do_not_double_settle(self):
        cycle = self.cycle()
        apply_discount(subscription=self.plan, actor=self.admin, kind='percent', value=Decimal('100'), effective_on=self.today)
        with patch('care.billing.provider_named') as provider:
            payment = collect_cycle(cycle)
            self.assertEqual(payment.amount, Decimal('0'))
            self.assertIsNone(collect_cycle(cycle))
            provider.assert_not_called()
        self.assertEqual(Payment.objects.filter(subscription_cycle=cycle).count(), 1)

    def test_schedule_is_read_only_and_paid_total_uses_actual_payments(self):
        self.debit_order()
        cycles_before = SubscriptionCycle.objects.count()
        schedule = payment_schedule(self.plan)
        self.assertEqual(SubscriptionCycle.objects.count(), cycles_before)
        self.assertEqual((schedule['paid_count'], schedule['paid_total']), (1, Decimal('3295')))
        self.assertGreater(schedule['total_count'], 1)
        self.assertTrue(any(row['projected'] for row in schedule['rows']))

    @override_settings(PAYMENT_PROVIDER='peach', PEACH_API_URL='https://peach.test', PEACH_ENTITY_ID='entity',
                       PEACH_ACCESS_TOKEN='test-token', PEACH_RECURRING_ENTITY_ID='recurring', PEACH_RECURRING_ACCESS_TOKEN='test-recurring')
    def test_peach_receives_discounted_amount_from_new_future_cycle(self):
        self.discount()
        mandate = DebitMandate.objects.create(company=self.company, patient=self.patient, subscription=self.plan,
            method='card', provider='peach', status='active', amount=3295, interval_weeks=4,
            provider_reference='reg-discount', reference='discount-mandate')
        self.plan.next_debit_on = self.today
        self.plan.save(update_fields=['next_debit_on'])
        with patch('care.peach.PeachProvider._call', return_value={'id': 'discount-charge', 'result': {'code': '000.000.000'}}) as call:
            payments = bill_subscription(self.plan, today=self.today)
        self.assertEqual(len(payments), 1)
        self.assertEqual(call.call_args.args[2]['amount'], '2795.00')
        self.assertEqual(payments[0].amount, Decimal('2795'))
        cycle = SubscriptionCycle.objects.get(subscription=self.plan)
        self.assertEqual((cycle.status, cycle.base_amount, cycle.discount_amount, cycle.amount),
                         ('paid', Decimal('3295'), Decimal('500'), Decimal('2795')))

    def test_discount_is_reapplied_to_failed_retry_without_rewriting_previous_attempt(self):
        cycle = self.cycle(status='failed')
        attempt = Payment.objects.create(company=self.company, patient=self.patient, subscription=self.plan,
            subscription_cycle=cycle, amount=3295, due_on=self.today, status='failed', provider='peach')
        self.discount()
        cycle.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual((cycle.amount, attempt.amount), (Decimal('2795'), Decimal('3295')))
        self.assertEqual(cycle.status, 'failed')

    def test_single_discount_cannot_target_a_different_subscription(self):
        from .models import PatientSubscription
        other = PatientSubscription.objects.create(company=self.company, patient=self.patient, plan_name='Other plan',
                                                   authorization=self.auth)
        target = self.cycle(subscription=other)
        with self.assertRaises(ValidationError):
            self.discount(payment_on=target.due_on, target_cycle=target)
        self.assertFalse(SubscriptionDiscount.objects.exists())

    def test_progress_counts_legacy_actual_payments_without_creating_bills(self):
        Payment.objects.create(company=self.company, patient=self.patient, subscription=self.plan,
            amount=Decimal('1000'), due_on=self.today, status='paid', kind='subscription')
        self.assertEqual(payment_schedule(self.plan)['paid_total'], Decimal('1000'))
        self.assertFalse(SubscriptionCycle.objects.exists())
