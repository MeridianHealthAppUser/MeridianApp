"""Discounted subscription prices, saved cycles and read-only payment projections."""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from .models import Invoice, PatientSubscription, Payment, SubscriptionCycle, SubscriptionDiscount, TreatmentAuthorization
from .services import record_audit

OPEN = (SubscriptionCycle.Status.PENDING, SubscriptionCycle.Status.FAILED)
CENT = Decimal('.01')


def discounted_price(subscription, due_on, base, *, rules=None, cycle_id=None):
    if base is None:
        return None, None
    rules = list(rules) if rules is not None else subscription.discounts.filter(
        company_id=subscription.company_id, patient_id=subscription.patient_id).order_by('-pk')
    rule = next((rule for rule in rules if rule.effective_on <= due_on and
                 (rule.payment_on is None or rule.payment_on == due_on) and
                 (rule.target_cycle_id is None or rule.target_cycle_id == cycle_id)), None)
    if rule is None:
        return base, None
    reduction = base * rule.value / Decimal('100') if rule.kind == SubscriptionDiscount.Kind.PERCENT else rule.value
    return max(Decimal('0'), base - reduction).quantize(CENT, rounding=ROUND_HALF_UP), rule


def refresh_cycle_price(cycle):
    """Caller holds the cycle lock. Never rewrite a settled or submitted collection."""
    if cycle.status not in OPEN or cycle.payments.filter(status__in=(Payment.Status.PENDING, Payment.Status.PAID)).exists():
        return False
    base = cycle.base_amount if cycle.base_amount is not None else cycle.amount
    amount, rule = discounted_price(cycle.subscription, cycle.due_on, base, cycle_id=cycle.pk)
    if rule is None and cycle.discount_id is None:
        return False
    cycle.base_amount, cycle.amount, cycle.discount_amount, cycle.discount = base, amount, base - amount, rule
    cycle.save(update_fields=('base_amount', 'amount', 'discount_amount', 'discount', 'updated_at'))
    Invoice.objects.filter(subscription_cycle=cycle, status__in=(Invoice.Status.DRAFT, Invoice.Status.ISSUED)).update(
        subtotal=base, total=amount, updated_at=timezone.now())
    return True


def waive_cycle(cycle, actor=None):
    """A fully discounted cycle is settled locally; no zero-value request goes to a provider."""
    from .billing import _mark_paid
    payment = Payment.objects.create(company_id=cycle.company_id, patient_id=cycle.patient_id,
        subscription=cycle.subscription, subscription_cycle=cycle, kind=Payment.Kind.SUBSCRIPTION,
        method=Payment.Method.OTHER, amount=0, due_on=cycle.due_on, status=Payment.Status.PAID,
        paid_at=timezone.now(), recorded_by=actor, note='Covered in full by subscription discount')
    _mark_paid(cycle, payment)
    record_audit(company=cycle.company, actor=actor, patient=cycle.patient, action='billing.cycle_discount_covered',
                 target=cycle, metadata={'discount_id': cycle.discount_id})
    return payment


def _tone(cycle, in_flight):
    """A payment's status colour, as Billing's pills have them (James, 26 Nov 2026): paid, failed, retrying (being
    collected or tried again) or due (to come, or no longer due)."""
    if cycle.status == SubscriptionCycle.Status.PAID:
        return 'paid'
    if cycle.status == SubscriptionCycle.Status.FAILED:
        return 'failed'
    if in_flight or (cycle.status == SubscriptionCycle.Status.PENDING and cycle.attempts):
        return 'retrying'
    return 'due'


def payment_schedule(subscription):
    """Actual cycles plus future estimates through the prescribed course; viewing creates no bills."""
    from .billing import billing_weeks, payment_day_for, period_start, prescription_ends_on, price_on
    cycles = list(subscription.cycles.filter(company_id=subscription.company_id, patient_id=subscription.patient_id)
                  .select_related('discount').prefetch_related('payments').order_by('due_on', 'pk'))
    rules = list(subscription.discounts.filter(company_id=subscription.company_id, patient_id=subscription.patient_id).order_by('-pk'))
    rows = []
    paid_total = subscription.payments.filter(company_id=subscription.company_id, patient_id=subscription.patient_id,
        status=Payment.Status.PAID).aggregate(total=Sum('amount'))['total'] or Decimal('0')
    for cycle in cycles:
        payments = list(cycle.payments.all())
        in_flight = any(payment.status == Payment.Status.PENDING for payment in payments)
        rows.append(dict(cycle=cycle, due_on=cycle.due_on, base=cycle.base_amount if cycle.base_amount is not None else cycle.amount,
            amount=cycle.amount, discount_amount=cycle.discount_amount, discount=cycle.discount,
            key=f'cycle:{cycle.pk}', status=('Covered by discount' if cycle.status == cycle.Status.PAID and cycle.amount == 0 else
                    'Processing' if in_flight else cycle.get_status_display()),
            editable=subscription.status != PatientSubscription.Status.CANCELLED and cycle.status in OPEN and not in_flight and not any(payment.status == Payment.Status.PAID for payment in payments), paid_at=cycle.paid_at, projected=False,
            starts_on=cycle.starts_on, tone=_tone(cycle, in_flight)))
    end = prescription_ends_on(subscription)
    live = [cycle for cycle in cycles if cycle.status != SubscriptionCycle.Status.CANCELLED]
    last = max(live, key=lambda cycle: cycle.cycle_number) if live else None
    after = (last.ends_on or last.starts_on + timedelta(weeks=billing_weeks(subscription, last.starts_on), days=-1)) if last else None
    day = subscription.next_debit_on or (payment_day_for(after + timedelta(days=1)) if after else subscription.starts_on)
    existing_dates = {cycle.due_on for cycle in cycles}
    projected = 0
    # Each payment pays for the period from its parcel, a week later (care.billing.period_start); the last is the one
    # whose parcel still goes out before the prescription ends.
    # Nothing more is planned once the prescription is revoked (James, 26 Nov 2026): only what was paid or tried.
    revoked = subscription.authorization_id and subscription.authorization.status == TreatmentAuthorization.Status.CANCELLED
    while end and not revoked and day <= end and subscription.status != PatientSubscription.Status.CANCELLED and projected < 520:
        starts = period_start(subscription, day, after=after)
        if starts >= end:
            break
        weeks = billing_weeks(subscription, starts)
        if day not in existing_dates:
            base = price_on(subscription, starts)
            amount, rule = discounted_price(subscription, day, base, rules=rules)
            rows.append(dict(cycle=None, due_on=day, base=base, amount=amount,
                discount_amount=base - amount if base is not None else Decimal('0'), discount=rule,
                key=f'date:{day.isoformat()}', status='Planned · paused' if subscription.status == PatientSubscription.Status.PAUSED else 'Planned',
                editable=base is not None, paid_at=None, projected=True, starts_on=starts, tone='due'))
        after = starts + timedelta(weeks=weeks, days=-1)
        day = payment_day_for(after + timedelta(days=1))
        projected += 1
    rows.sort(key=lambda row: row['due_on'])
    relevant = [row for row in rows if not row['cycle'] or row['cycle'].status != SubscriptionCycle.Status.CANCELLED]
    return dict(rows=rows, paid_count=sum(row['cycle'] is not None and row['cycle'].status == SubscriptionCycle.Status.PAID for row in relevant),
                total_count=len(relevant), paid_total=paid_total, rules=rules, projected_through=end,
                has_discounts=any(row['discount_amount'] for row in relevant))


@transaction.atomic
def apply_discount(*, subscription, actor, kind, value, effective_on, payment_on=None, target_cycle=None, reason='', request=None):
    from .billing import require_staff
    require_staff(subscription.company, actor)
    subscription = PatientSubscription.objects.select_for_update(of=('self',)).select_related('company', 'patient', 'authorization').get(pk=subscription.pk)
    if subscription.status == PatientSubscription.Status.CANCELLED:
        raise ValidationError('This subscription is cancelled.')
    rule = SubscriptionDiscount(company=subscription.company, patient=subscription.patient, subscription=subscription,
        kind=kind, value=value, effective_on=effective_on, payment_on=payment_on, target_cycle=target_cycle, reason=reason, created_by=actor)
    if target_cycle is not None and (target_cycle.subscription_id != subscription.pk or target_cycle.company_id != subscription.company_id or target_cycle.patient_id != subscription.patient_id):
        raise ValidationError('Choose a payment from this subscription.')
    rule.full_clean()
    schedule = payment_schedule(subscription)
    affected = [row for row in schedule['rows'] if row['editable'] and row['due_on'] >= effective_on
                and (payment_on is None or row['due_on'] == payment_on)
                and (target_cycle is None or row['cycle'] and row['cycle'].pk == target_cycle.pk)]
    if not affected:
        raise ValidationError('No editable unpaid payments match this date. Paid or processing payments cannot be changed.')
    # Collections lock cycles. Skip a cycle being processed rather than waiting while holding the plan lock.
    candidates = subscription.cycles.filter(status__in=OPEN, due_on__gte=effective_on)
    if payment_on is not None:
        candidates = candidates.filter(due_on=payment_on)
    if target_cycle is not None:
        candidates = candidates.filter(pk=target_cycle.pk)
    locked = list(candidates.select_for_update(of=('self',), skip_locked=True))
    expected_ids = {row['cycle'].pk for row in affected if row['cycle']}
    skipped = len(expected_ids - {cycle.pk for cycle in locked})
    if payment_on is not None and skipped:
        raise ValidationError('This payment is currently being processed. Refresh before changing its amount.')
    rule.save()
    updated = sum(refresh_cycle_price(cycle) for cycle in locked)
    subscription.save(update_fields=('updated_at',))
    record_audit(company=subscription.company, actor=actor, patient=subscription.patient,
        action='billing.discount_applied', target=subscription, request=request,
        metadata={'discount_id': rule.pk, 'kind': kind, 'value': str(value), 'effective_on': effective_on.isoformat(),
                  'payment_on': payment_on.isoformat() if payment_on else None, 'updated_cycles': updated, 'processing_skipped': skipped})
    return rule, skipped
