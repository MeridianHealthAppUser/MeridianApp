"""Subscription billing: debit orders, the debit run, retries and the failure queue, once-off payments, and whether a
parcel is paid for.

How it fits together:
- A patient starts their subscription by giving a delivery address and adding their card (a DebitMandate with the
  card method: the provider keeps the card and it is charged again each period). DebiCheck debit orders are hidden for
  now (settings.DEBIT_ORDERS_ENABLED); the code calls every such authority a "debit order". The mandate's amount is the most the practice may collect each time: the highest
  price their prescription reaches (a starting plan's last step).
- Each subscription is billed every few weeks: the billing interval of the dose in effect, 4 weeks unless the
  catalogue says otherwise. `manage.py run_debits` (daily) opens the next SubscriptionCycle when next_debit_on (the
  next payment day) comes round and collects it. The first cycle is collected as soon as the debit order is approved.
- Each payment pays for a period that starts with its parcel (James, 25 Nov 2026): pens are ordered at the order
  cut-off (Practice settings, Monday 15:00 by default) for the next shipping day, so the first payment's period starts on
  the first shipping day whose cut-off is still to come (care.parcels.first_shipping_day), and every later payment is
  taken a week before its parcel (care.parcels.PAYMENT_LEAD), with a retry on the cut-off day.
- The provider (care.payment_providers) answers a debit at once, or later by webhook or file (DebiCheck with Stitch,
  collected from files a few days ahead). A debit waiting for its answer isn't sent again; one the provider never
  answered is looked up the next day (check_unanswered).
- A failed debit is retried on the practice's retry days (Practice settings, "3,7" by default). After the last retry
  the cycle is Failed: it waits in the failure queue (Operations > Billing) with a task for the practice, until it is
  paid or the practice records a payment received another way. The patient is emailed to update their details.
- A prescription or price change that needs more than the mandate's amount marks it "New amount to confirm"; the
  patient is asked to approve a new debit order, and nothing more is collected until they do.
- A parcel is paid for when the cycle covering its day is paid (a cycle covers the parcels up to its last day), when
  its non-prescription order is paid, or when the practice recorded a payment against it. A paid order of items that
  need no prescription goes on the next shipping day's list by itself (care.pharmacy.ship_paid_order). Check and lock week
  (care.operations) holds back parcels that aren't, when the practice requires payment (the default).
"""

import logging
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from practices.models import Company, CompanyMembership

from .models import (
    AuditEvent, ClinicalTask, DebitMandate, Invoice, PatientEvent, PatientSubscription, Payment, PharmacyOrder, PracticeSettings,
    Shipment, SubscriptionCycle, TreatmentAuthorization,
)
from .demo_safety import block_demo_billing, is_demo_record
from .notifications import queue as notify
from .payment_providers import current_provider, offered_methods, provider_named, require_provider
from .services import record_audit
from .treatment import dose_in_effect, step_in_effect

logger = logging.getLogger(__name__)
STAFF_ROLES = (CompanyMembership.Role.PRACTICE_ADMIN, CompanyMembership.Role.SUPER_ADMIN)
BILLABLE = (PatientSubscription.Status.ACTIVE, PatientSubscription.Status.PAYMENT_RETRY)
LIVE_MANDATES = (DebitMandate.Status.ACTIVE, DebitMandate.Status.UPDATE_NEEDED)
DEFAULT_WEEKS = 4


def practice_settings(company):
    return PracticeSettings.objects.filter(company=company).first() or PracticeSettings(company=company)


def require_staff(company, actor):
    if not (getattr(actor, 'is_active', False) and CompanyMembership.objects.filter(
            company=company, user=actor, is_active=True, role__in=STAFF_ROLES).exists()):
        raise PermissionDenied('Only practice administrators and Super Admins can manage billing.')


def _require_patient(patient, actor):
    if not getattr(actor, 'is_active', False) or patient.user_id != actor.pk or not patient.is_active:
        raise PermissionDenied('Only the patient can add their own card.')


def money(amount):
    """R3 295.00 as the practice writes it."""
    whole, cents = f'{Decimal(amount):.2f}'.split('.')
    return f'R{int(whole):,}'.replace(',', ' ') + f'.{cents}'


# What a subscription costs

def billing_weeks(subscription, on_date=None):
    dose = dose_in_effect(subscription, on_date)
    return dose.billing_weeks if dose is not None and dose.billing_weeks else DEFAULT_WEEKS


def price_on(subscription, on_date):
    """What the cycle starting on_date costs: the price of the dose in effect that day (a scheduled new price from
    its day, care.dose_prices), or the product's price for a prescription that isn't for a catalogue dose."""
    from .dose_prices import price_for

    dose = dose_in_effect(subscription, on_date)
    price = price_for(dose, on_date) if dose is not None else None
    if price is not None:
        return price
    authorization = step_in_effect(subscription.authorization, on_date) if subscription.authorization_id else None
    return authorization.product.price if authorization is not None else None


def _authorised_price(authorization):
    if authorization.dose_id and authorization.dose.price is not None:
        return authorization.dose.price
    return authorization.product.price


def prescription_amount(authorization):
    """The most one collection will be for this prescription from today: the highest price left in it, so a starting
    plan's step up doesn't need a new debit order."""
    if authorization is None:
        return None
    today = timezone.localdate()
    prices = [_authorised_price(step_in_effect(authorization, today))]
    if authorization.plan_key:
        steps = TreatmentAuthorization.objects.filter(
            company_id=authorization.company_id, patient_id=authorization.patient_id, plan_key=authorization.plan_key,
            expires_on__gte=today,
        ).exclude(status=TreatmentAuthorization.Status.CANCELLED).select_related('dose', 'product')
        prices += [_authorised_price(step) for step in steps]
    amount = max(price for price in prices if price is not None) if any(price is not None for price in prices) else None
    return amount if amount and amount > 0 else None


def mandate_amount(subscription):
    """What a debit order for this subscription must cover: the highest price left in its prescription, and any
    billing period still waiting to be collected (one opened early at a scheduled new price, or a failed one), so a
    new debit order can collect it."""
    amount = prescription_amount(subscription.authorization) if subscription.authorization_id else None
    waiting = subscription.cycles.filter(status__in=(SubscriptionCycle.Status.PENDING, SubscriptionCycle.Status.FAILED)).order_by(
        '-amount').values_list('amount', flat=True).first()
    if amount is not None and waiting is not None and waiting > amount:
        return waiting
    return amount


def current_mandate(subscription):
    """The debit order collections use: the live one, else the latest still waiting for approval."""
    mandates = DebitMandate.objects.filter(company_id=subscription.company_id, subscription=subscription)
    return (mandates.filter(status__in=LIVE_MANDATES).order_by('-approved_at', '-pk').first()
            or mandates.filter(status=DebitMandate.Status.PENDING).order_by('-pk').first())


# Debit orders

@transaction.atomic
@block_demo_billing('subscription')
def start_debit_order(*, subscription, actor, method=DebitMandate.Method.CARD, request=None):
    """The patient asks for a debit order for their subscription. Returns the new mandate and the page where they
    approve it (the provider's, or the sandbox's)."""
    provider = require_provider()
    company = Company.objects.select_for_update().get(pk=subscription.company_id)
    subscription = PatientSubscription.objects.select_for_update(of=('self',)).select_related(
        'patient', 'authorization__dose', 'authorization__product').get(pk=subscription.pk, company=company)
    _require_patient(subscription.patient, actor)
    if subscription.status == PatientSubscription.Status.CANCELLED:
        raise ValidationError('This subscription is cancelled. Start a new one to add your card.')
    methods = offered_methods(provider)
    if method not in methods:
        raise ValidationError(f'Pay by {" or ".join(DebitMandate.Method(m).label.lower() for m in methods)}.')
    amount = mandate_amount(subscription)
    if amount is None:
        raise ValidationError('Your prescription has no price yet, so your card can’t be set up for it. '
                              'Message your care team.')
    # A debit order still waiting for approval is replaced by the new request.
    DebitMandate.objects.filter(subscription=subscription, status=DebitMandate.Status.PENDING).update(
        status=DebitMandate.Status.CANCELLED, cancelled_at=timezone.now(), updated_at=timezone.now())
    mandate = DebitMandate(company=company, patient=subscription.patient, subscription=subscription, method=method,
                           amount=amount, interval_weeks=billing_weeks(subscription), provider=provider.name, created_by=actor)
    mandate.full_clean()
    mandate.save()
    mandate.reference = f'MH-D{mandate.pk:06d}'
    url = provider.start_mandate(mandate)
    mandate.save(update_fields=('reference', 'provider_reference', 'collection_day', 'updated_at'))
    record_audit(company=company, actor=actor, patient=subscription.patient, action='billing.mandate_started',
                 target=mandate, request=request, metadata={'method': method, 'amount': str(amount), 'provider': provider.name})
    return mandate, url


@transaction.atomic
@block_demo_billing('mandate')
def approve_mandate(*, mandate, account_label='', provider_reference='', fail_debits=False, actor=None, request=None):
    """The patient approved the debit order with their bank or card provider. It replaces any earlier one, sets the
    billing date when this is the first, and collects what is due now: the first debit, or a payment that failed.
    provider_reference is what the provider collects with (its mandate, or the stored card's token), when it is only
    known once approved."""
    mandate = DebitMandate.objects.select_for_update(of=('self',)).select_related('subscription__patient').get(pk=mandate.pk)
    if mandate.status == DebitMandate.Status.ACTIVE:
        return mandate
    if mandate.status != DebitMandate.Status.PENDING:
        raise ValidationError('This request is no longer open. Start again on Billing and payments.')
    subscription = PatientSubscription.objects.select_for_update().get(pk=mandate.subscription_id)
    now = timezone.now()
    replaced = DebitMandate.objects.filter(subscription=subscription, status__in=LIVE_MANDATES).exclude(pk=mandate.pk)
    release_mandates(replaced)
    replaced.update(status=DebitMandate.Status.CANCELLED, cancelled_at=now, updated_at=now)
    if provider_reference:
        mandate.provider_reference = provider_reference[:120]
    if fail_debits:
        mandate.provider_reference = f'sandbox-decline-{mandate.provider_reference.removeprefix("sandbox-")}'
    mandate.status, mandate.approved_at = DebitMandate.Status.ACTIVE, now
    mandate.account_label = ' '.join(account_label.split())[:80]
    mandate.save(update_fields=('status', 'approved_at', 'account_label', 'provider_reference', 'updated_at'))
    if subscription.next_debit_on is None and not subscription.cycles.exists():
        # The first debit: today, or as soon as the debit order can collect (a DebiCheck collection is filed days ahead).
        subscription.next_debit_on = timezone.localdate() + timedelta(days=lead_days(mandate))
        subscription.save(update_fields=('next_debit_on', 'updated_at'))
    record_audit(company=mandate.company, actor=actor, patient=mandate.patient, action='billing.mandate_approved',
                 target=mandate, request=request, metadata={'method': mandate.method})
    PatientEvent.objects.create(company=mandate.company, patient=mandate.patient, category=PatientEvent.Category.PAYMENT,
                                title='Card saved' if mandate.method == DebitMandate.Method.CARD else 'Debit order approved',
                                detail=f'{mandate.account_label or mandate.get_method_display()} for up to {money(mandate.amount)} every '
                                       f'{mandate.interval_weeks} weeks.',
                                source_type='care.debitmandate', source_id=str(mandate.pk))
    bill_subscription(subscription)
    return mandate


def lead_days(mandate):
    """How many days ahead this debit order's collections are sent: 0, or its provider's lead time."""
    provider = provider_named(mandate.provider)
    return provider.lead_days(mandate) if provider is not None and hasattr(provider, 'lead_days') else 0


def release_mandates(mandates):
    """Tell the provider that these debit orders are finished (Peach forgets the card; Stitch revokes the DebiCheck
    mandate at the bank), once the change that ends them is saved. Each goes to the provider that holds it."""
    for mandate in mandates.filter(patient__demo_data_set__isnull=True).exclude(provider_reference=''):
        provider = provider_named(mandate.provider)
        if provider is not None:
            transaction.on_commit(lambda mandate=mandate, provider=provider: None if is_demo_record(mandate) else provider.cancel_mandate(mandate))


@transaction.atomic
@block_demo_billing('mandate')
def decline_mandate(*, mandate, reason='', actor=None, request=None):
    mandate = DebitMandate.objects.select_for_update().get(pk=mandate.pk)
    if mandate.status != DebitMandate.Status.PENDING:
        return mandate
    mandate.status, mandate.failure_reason = DebitMandate.Status.FAILED, (reason or 'Not approved.')[:300]
    mandate.save(update_fields=('status', 'failure_reason', 'updated_at'))
    record_audit(company=mandate.company, actor=actor, patient=mandate.patient, action='billing.mandate_declined',
                 target=mandate, request=request)
    return mandate


def review_mandates(company, patient=None, *, prescription_changed=False):
    """After a prescription or price change, a live card (debit order) is marked "New amount to confirm" and the patient
    is asked to confirm it again: by default only when the amount needed is now above what they approved, so a dose
    change at the same or a lower price carries on as it is; with Practice settings' "Whenever the prescription or its
    price changes", also for any new or changed prescription (not a renewal) or any change in amount. Returns the
    mandates marked."""
    any_change = practice_settings(company).new_debit_order == PracticeSettings.NewDebitOrder.ANY_CHANGE
    marked = []
    mandates = DebitMandate.objects.filter(company=company, status=DebitMandate.Status.ACTIVE, patient__demo_data_set__isnull=True).select_related('subscription')
    if patient is not None:
        mandates = mandates.filter(patient=patient)
    for mandate in mandates:
        subscription = mandate.subscription
        if subscription is None or subscription.status not in BILLABLE:
            continue
        needed = mandate_amount(subscription)
        if needed is None:
            continue
        if needed > mandate.amount or (any_change and (prescription_changed or needed != mandate.amount)):
            mandate.status = DebitMandate.Status.UPDATE_NEEDED
            mandate.save(update_fields=('status', 'updated_at'))
            record_audit(company=company, actor=None, patient=mandate.patient, action='billing.mandate_update_needed',
                         target=mandate, metadata={'amount': str(mandate.amount), 'needed': str(needed),
                                                   'prescription_changed': prescription_changed})
            notify('debit_order_update', mandate.pk)
            marked.append(mandate)
    return marked


# The debit run

def billed_to(patient):
    """Who an invoice is issued to, kept on it (Invoice.billed_to) so a later correction to the patient's details
    doesn't change an invoice already issued."""
    name = ' '.join(part for part in (patient.get_title_display(), patient.first_name, patient.last_name) if part)
    return {'name': name, 'id_number': patient.id_number}


def _cycle_end(cycle):
    return cycle.ends_on or cycle.starts_on + timedelta(weeks=DEFAULT_WEEKS, days=-1)


def prescription_ends_on(subscription):
    """The last day of the prescription a care plan is on: its own, or a starting plan's last step's."""
    authorization = subscription.authorization
    if authorization is None:
        return subscription.review_due_on
    if authorization.plan_key:
        last = TreatmentAuthorization.objects.filter(
            company_id=authorization.company_id, patient_id=authorization.patient_id, plan_key=authorization.plan_key,
        ).exclude(status=TreatmentAuthorization.Status.CANCELLED).order_by('-expires_on').values_list('expires_on', flat=True).first()
        return last or authorization.expires_on
    return authorization.expires_on


def last_period_end(subscription):
    last = subscription.cycles.exclude(status=SubscriptionCycle.Status.CANCELLED).order_by('-cycle_number').first()
    return _cycle_end(last) if last is not None else None


def period_start(subscription, due, *, after=None):
    """The first day of the period a payment on due pays for: the day of its parcel, the first shipping day whose order
    cut-off is on or after due (care.parcels.first_shipping_day), and never before the last period (after, or the
    subscription's latest) has ended: then the first shipping day after it. A repeat payment, a week before its
    parcel, pays for the period starting with that parcel."""
    from .parcels import first_shipping_day, shipping_day_from, shipping_weekday

    after = after if after is not None else last_period_end(subscription)
    day = first_shipping_day(due, subscription.company)
    if after is not None and day <= after:
        day = shipping_day_from(after + timedelta(days=1), shipping_weekday(subscription.company))
    return day


def payment_day_for(starts):
    """When the payment for a period starting on starts (a parcel's day) is taken: a week before it."""
    from .parcels import PAYMENT_LEAD

    return starts - PAYMENT_LEAD


def period_has_parcel(subscription, due, *, after=None):
    """Whether the payment due on due pays for pens under the prescription. Its period starts with its parcel
    (period_start), and a prescription sends nothing on or after its last day (care.parcels.schedule_parcels), so its
    last payment is the one before: nothing is taken for weeks it gives no pens for (James, 25 Nov 2026). Renewing or
    changing the prescription lets billing carry on (care.prescriptions._review_debit_orders)."""
    end = prescription_ends_on(subscription)
    if due is None or end is None:
        return due is not None
    return period_start(subscription, due, after=after) < end


def next_payment_on(subscription):
    """When the card is next charged: None before a card is added, or when the prescription ends first."""
    day = subscription.next_debit_on
    return day if period_has_parcel(subscription, day) else None


def _open_cycle(subscription, mandate):
    """The subscription's next cycle, for the payment due on its next debit date: the period from its parcel's day
    (period_start), as long as the dose in effect then is billed for. The next debit date moves to a week before the
    period after it."""
    due = subscription.next_debit_on
    starts = period_start(subscription, due)
    weeks = billing_weeks(subscription, starts)
    base = price_on(subscription, starts)
    from .subscription_discounts import discounted_price
    amount, discount = discounted_price(subscription, due, base)
    number = (subscription.cycles.order_by('-cycle_number').values_list('cycle_number', flat=True).first() or 0) + 1
    cycle = SubscriptionCycle(
        company_id=subscription.company_id, patient_id=subscription.patient_id, subscription=subscription,
        cycle_number=number, starts_on=starts, ends_on=starts + timedelta(weeks=weeks, days=-1), due_on=due,
        amount=amount if amount is not None else Decimal('0'), base_amount=base,
        discount_amount=base - amount if base is not None else Decimal('0'), discount=discount, mandate=mandate,
        idempotency_key=f'subscription-{subscription.pk}-cycle-{number}',
    )
    cycle.full_clean()
    cycle.save()
    invoice = Invoice(company_id=subscription.company_id, patient_id=subscription.patient_id, subscription_cycle=cycle,
                      invoice_number=f'SUB-{subscription.pk:05d}-{number:03d}', issued_on=due, due_on=due,
                      subtotal=cycle.base_amount if cycle.base_amount is not None else cycle.amount, total=cycle.amount, status=Invoice.Status.ISSUED,
                      billed_to=billed_to(subscription.patient))
    invoice.full_clean()
    invoice.save()
    subscription.cycle_number = number
    subscription.next_debit_on = payment_day_for(starts + timedelta(weeks=weeks))
    subscription.save(update_fields=('cycle_number', 'next_debit_on', 'updated_at'))
    return cycle


def resume_debit_date(subscription):
    """When a resumed subscription is next debited: a week before the period after the last one it paid for, or today;
    nothing before its debit order is approved."""
    last = subscription.cycles.exclude(status=SubscriptionCycle.Status.CANCELLED).order_by('-cycle_number').first()
    today = timezone.localdate()
    if last is not None:
        return max(today, payment_day_for(_cycle_end(last) + timedelta(days=1)))
    live = DebitMandate.objects.filter(subscription=subscription, status__in=LIVE_MANDATES).exists()
    return today if live else None


def retry_days(cycle, settings_row):
    """The days after a cycle's payment date it is tried again: the practice's retry days and the order cut-off day
    for the period's parcel (James, 25 Nov 2026), so a card declined on the day is tried again in time for the order.
    The debit run is in the morning, before the cut-off time (Monday 15:00 by default)."""
    from .parcels import order_cutoff, shipping_day_from, shipping_weekday

    days = settings_row.retry_days()
    parcel = shipping_day_from(cycle.starts_on, shipping_weekday(cycle.company_id))
    before_order = (order_cutoff(parcel, cycle.company_id) - cycle.due_on).days
    return sorted({before_order, *days}) if before_order > 0 else days


def _retry_on(cycle, settings_row):
    """When the next retry is, or None after the last one."""
    days = retry_days(cycle, settings_row)
    return cycle.due_on + timedelta(days=days[cycle.attempts - 1]) if cycle.attempts <= len(days) else None


def _mark_paid(cycle, payment, *, today=None):
    now = timezone.now()
    cycle.status, cycle.paid_at, cycle.next_attempt_on, cycle.failure_reason = SubscriptionCycle.Status.PAID, now, None, ''
    cycle.save(update_fields=('status', 'paid_at', 'next_attempt_on', 'failure_reason', 'updated_at'))
    invoice = Invoice.objects.filter(subscription_cycle=cycle).first()
    if invoice is not None:
        invoice.status = Invoice.Status.PAID
        invoice.save(update_fields=('status', 'updated_at'))
        if payment is not None and not Payment.objects.filter(invoice=invoice).exists():
            payment.invoice = invoice
            payment.save(update_fields=('invoice', 'updated_at'))
    subscription = cycle.subscription
    outstanding = subscription.cycles.filter(status__in=(SubscriptionCycle.Status.PENDING, SubscriptionCycle.Status.FAILED),
                                             attempts__gt=0)
    if subscription.status == PatientSubscription.Status.PAYMENT_RETRY and not outstanding.exists():
        subscription.status = PatientSubscription.Status.ACTIVE
        subscription.save(update_fields=('status', 'updated_at'))
    PatientEvent.objects.create(company_id=cycle.company_id, patient_id=cycle.patient_id, category=PatientEvent.Category.PAYMENT,
                                title='Payment received', detail=f'{money(cycle.amount)} for cycle {cycle.cycle_number}.',
                                source_type='care.subscriptioncycle', source_id=str(cycle.pk))
    _start_on_first_payment(cycle, today or timezone.localdate())
    _carry_on_after_late_payment(cycle, today or timezone.localdate())


def _carry_on_after_late_payment(cycle, today):
    """A payment that didn't go through, paid late after its parcel's order cut-off (22 Nov 2026; the cut-off since
    25 Nov): the missed pens go out on the first shipping day they can make (care.parcels.first_shipping_day), the
    period paid for starts again then (its full 4 or 5 weeks, so the next payment moves too) and the course carries on
    from there (care.prescriptions.carry_on_from). Any other period left unpaid is cancelled: no pens went out for it.
    A gap longer than a period means the doctor confirms the prescription before the pens go out (they may want to
    restart at a lower dose). Paid by the cut-off, nothing changes. Anything that goes wrong here is logged and left
    alone: it never stops a payment being recorded."""
    subscription = cycle.subscription
    if is_demo_record(cycle) or not cycle.payments.filter(status=Payment.Status.FAILED).exists():
        return
    if not subscription.cycles.filter(status=SubscriptionCycle.Status.PAID).exclude(pk=cycle.pk).exists():
        return  # The first payment: treatment starts on it instead (_start_on_first_payment).
    try:
        with transaction.atomic():
            from .parcels import first_shipping_day
            from .prescriptions import carry_on_from, course_steps, _confirmation_task

            length = (_cycle_end(cycle) - cycle.starts_on).days + 1
            moved = carry_on_from(subscription, today, missed='payment didn’t go through', because='after a missed payment')
            if not moved:
                return
            restart_period(cycle, first_shipping_day(today, cycle.company_id))
            for other in subscription.cycles.filter(status__in=(SubscriptionCycle.Status.PENDING, SubscriptionCycle.Status.FAILED)).exclude(pk=cycle.pk):
                other.status, other.next_attempt_on = SubscriptionCycle.Status.CANCELLED, None
                other.save(update_fields=('status', 'next_attempt_on', 'updated_at'))
                Invoice.objects.filter(subscription_cycle=other).exclude(status=Invoice.Status.PAID).update(status=Invoice.Status.VOID)
            record_audit(company=cycle.company, actor=None, patient=cycle.patient, action='billing.period_restarted',
                         target=cycle, metadata={'days': moved, 'starts_on': today.isoformat()})
            if moved > length:
                steps = course_steps(subscription.authorization)
                TreatmentAuthorization.objects.filter(pk=steps[0].pk).update(needs_confirmation=True)
                weeks = moved // 7
                _confirmation_task(steps, cycle.patient, (
                    f'{cycle.patient.first_name} paid on {today:%-d %b %Y} after {weeks} weeks without pens (a payment '
                    'that didn’t go through). Check the prescription is still right (a lower dose to start again, '
                    'perhaps) and confirm it on their record. Their pens and further payments wait until you do.'))
                record_audit(company=cycle.company, actor=None, patient=cycle.patient, action='prescription.confirmation_needed',
                             target=steps[0], metadata={'weeks': weeks, 'why': 'long_gap'})
    except Exception:
        logger.exception('Could not carry subscription %s on after a late payment.', subscription.pk)


def refit_current_period(subscription, first_parcel):
    """After a dose change, the payment period that pays for the new dose's first parcel (first_parcel) runs until the
    new dose's billing interval after that parcel, so the next payment comes a week before the new dose's second
    parcel: Tirzepatide 7.5 mg (every 5 weeks) after 5 mg (every 4) moves the next payment a week later, in step with
    its parcels. A change made mid-period sends the new pens in the period already paid for, which then runs on to
    them. Nothing changes when no period covers that parcel yet: the payment before it opens one at the new interval."""
    if first_parcel is None:
        return None
    cycle = subscription.cycles.exclude(status=SubscriptionCycle.Status.CANCELLED).order_by('-cycle_number').first()
    if cycle is None or cycle.starts_on > first_parcel or _cycle_end(cycle) < first_parcel:
        return None
    end = first_parcel + timedelta(weeks=billing_weeks(subscription, first_parcel), days=-1)
    if end == cycle.ends_on:
        return None
    before = cycle.ends_on
    cycle.ends_on = end
    cycle.save(update_fields=('ends_on', 'updated_at'))
    subscription.next_debit_on = payment_day_for(end + timedelta(days=1))
    subscription.save(update_fields=('next_debit_on', 'updated_at'))
    record_audit(company=cycle.company, actor=None, patient=cycle.patient, action='billing.period_refitted', target=cycle,
                 metadata={'ends_before': before.isoformat() if before else None, 'ends_on': end.isoformat()})
    return cycle


def restart_period(cycle, day):
    """Start a payment period on day instead, keeping its length; when it is the latest, the next payment moves with
    it, so the patient gets every week they paid for."""
    if cycle.starts_on >= day:
        return cycle
    length = _cycle_end(cycle) - cycle.starts_on
    cycle.starts_on, cycle.ends_on = day, day + length
    cycle.save(update_fields=('starts_on', 'ends_on', 'updated_at'))
    subscription = cycle.subscription
    if not subscription.cycles.filter(cycle_number__gt=cycle.cycle_number).exclude(status=SubscriptionCycle.Status.CANCELLED).exists():
        subscription.next_debit_on = payment_day_for(cycle.ends_on + timedelta(days=1))
        subscription.save(update_fields=('next_debit_on', 'updated_at'))
    return cycle


def _start_on_first_payment(cycle, today):
    """Treatment starts on the first payment (22 Nov 2026): the prescription's course moves forward to start that day
    (care.prescriptions.start_course_on), with its first parcel on the first shipping day whose order cut-off is still
    to come. The first billing period starts with that parcel: it moves too when it was paid after the cut-off it was
    opened for (its first try failed) and no later period has opened, so payments, doses and parcels stay in step.
    Anything that goes wrong here is logged and left alone: it never stops a payment being recorded."""
    subscription = cycle.subscription
    if is_demo_record(cycle) or subscription.cycles.filter(status=SubscriptionCycle.Status.PAID).exclude(pk=cycle.pk).exists():
        return
    try:
        with transaction.atomic():
            from .parcels import first_shipping_day

            restart_first_period(subscription, first_shipping_day(today, cycle.company_id))
            from .prescriptions import start_course_on

            start_course_on(subscription, today)
    except Exception:
        logger.exception('Could not move subscription %s to start on its first payment.', subscription.pk)


def restart_first_period(subscription, day):
    """Start a subscription's first payment period on day instead, when it is its only one and started earlier: paid
    days after it opened (its first try failed), or confirmed by the doctor after it was paid. Its length stays, and the
    next payment moves with it, so the patient gets every week they paid for."""
    cycles = list(subscription.cycles.exclude(status=SubscriptionCycle.Status.CANCELLED)[:2])
    if len(cycles) != 1 or cycles[0].starts_on >= day:
        return None
    return restart_period(cycles[0], day)


def _failure_task(cycle, reason):
    title = f'Debit failed: follow up with {cycle.patient} (cycle {cycle.cycle_number})'[:255]
    if ClinicalTask.objects.filter(company_id=cycle.company_id, title=title,
                                   status__in=(ClinicalTask.Status.OPEN, ClinicalTask.Status.IN_PROGRESS)).exists():
        return
    task = ClinicalTask(company_id=cycle.company_id, patient_id=cycle.patient_id, title=title, priority=ClinicalTask.Priority.HIGH,
                        description=(f'{money(cycle.amount)} due {cycle.due_on:%-d %b %Y} could not be collected after '
                                     f'{cycle.attempts} attempt{"s" if cycle.attempts != 1 else ""}: {reason} '
                                     'The patient has been emailed to update their payment details. Their parcels are held '
                                     'until it is paid; record a payment received another way on Operations > Billing.'))
    task.full_clean()
    task.save()


def collect_cycle(cycle, *, today=None, actor=None, request=None):
    """Try to collect one cycle with the subscription's debit order. Returns the Payment tried, or None when nothing
    could be tried (no debit order, or one waiting for the patient to confirm a new amount)."""
    if is_demo_record(cycle):
        return None
    today = today or timezone.localdate()
    cycle = SubscriptionCycle.objects.select_for_update(of=('self',)).select_related('subscription', 'patient').get(pk=cycle.pk)
    if cycle.status in (SubscriptionCycle.Status.PAID, SubscriptionCycle.Status.CANCELLED):
        return None
    if Payment.objects.filter(subscription_cycle=cycle, status=Payment.Status.PENDING).exists():
        return None  # A debit already sent is waiting for the provider's answer (a DebiCheck collection takes days).
    from .subscription_discounts import refresh_cycle_price, waive_cycle
    refresh_cycle_price(cycle)
    if cycle.amount == 0 and cycle.discount_amount > 0:
        return waive_cycle(cycle, actor)
    subscription = cycle.subscription
    mandate = DebitMandate.objects.filter(subscription=subscription, status__in=LIVE_MANDATES).order_by('-approved_at', '-pk').first()
    if mandate is None:
        cycle.failure_reason = 'No card saved.'
        cycle.save(update_fields=('failure_reason', 'updated_at'))
        return None
    if mandate.status == DebitMandate.Status.UPDATE_NEEDED or mandate.amount < cycle.amount:
        if mandate.status != DebitMandate.Status.UPDATE_NEEDED:
            mandate.status = DebitMandate.Status.UPDATE_NEEDED
            mandate.save(update_fields=('status', 'updated_at'))
            notify('debit_order_update', mandate.pk)
        cycle.failure_reason = 'Waiting for the patient to confirm their card for the new amount.'
        cycle.save(update_fields=('failure_reason', 'updated_at'))
        return None
    # Collected by the provider that holds the debit order, which may not be the one new debit orders go to.
    provider = provider_named(mandate.provider)
    if provider is None:
        cycle.failure_reason = 'The payment provider that holds this card isn’t connected.'
        cycle.save(update_fields=('failure_reason', 'updated_at'))
        return None
    payment = Payment.objects.create(
        company_id=cycle.company_id, patient_id=cycle.patient_id, subscription=subscription, subscription_cycle=cycle,
        kind=Payment.Kind.SUBSCRIPTION, method=mandate.method, provider=provider.name, attempt=cycle.attempts + 1,
        amount=cycle.amount, due_on=cycle.due_on, status=Payment.Status.PENDING)
    try:
        result = provider.collect(payment, mandate)
    except ValidationError as error:
        payment.status, payment.failure_reason = Payment.Status.FAILED, ' '.join(error.messages)[:500]
        payment.save(update_fields=('status', 'failure_reason', 'updated_at'))
        cycle.failure_reason = payment.failure_reason[:300]
        cycle.save(update_fields=('failure_reason', 'updated_at'))
        return payment
    return record_collection(payment=payment, status=result.status, reference=result.reference, reason=result.reason,
                             today=today, actor=actor, request=request)


@transaction.atomic
@block_demo_billing('payment')
def record_collection(*, payment, status, reference='', reason='', today=None, actor=None, request=None):
    """What the provider said about a debit: straight away, or later by webhook for one that was pending."""
    today = today or timezone.localdate()
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.status != Payment.Status.PENDING:
        return payment  # Already settled: the provider told us twice (its answer, then a webhook).
    cycle = SubscriptionCycle.objects.select_for_update(of=('self',)).select_related('subscription', 'patient').get(pk=payment.subscription_cycle_id)
    payment.provider_reference = reference[:120] or payment.provider_reference
    if status == 'pending':
        payment.save(update_fields=('provider_reference', 'updated_at'))
        return payment
    if status == 'paid':
        payment.status, payment.paid_at = Payment.Status.PAID, timezone.now()
        payment.save(update_fields=('status', 'paid_at', 'provider_reference', 'updated_at'))
        cycle.attempts = payment.attempt
        cycle.save(update_fields=('attempts', 'updated_at'))
        _mark_paid(cycle, payment, today=today)
        record_audit(company=cycle.company, actor=actor, patient=cycle.patient, action='billing.debit_paid', target=cycle,
                     request=request, metadata={'payment_id': payment.pk, 'attempt': payment.attempt})
        return payment
    reason = (reason or 'The debit was not paid.')[:300]
    payment.status, payment.failure_reason = Payment.Status.FAILED, reason
    payment.save(update_fields=('status', 'failure_reason', 'provider_reference', 'updated_at'))
    cycle.attempts, cycle.failure_reason = payment.attempt, reason
    first_failure = cycle.attempts == 1
    cycle.next_attempt_on = _retry_on(cycle, practice_settings(cycle.company))
    if cycle.next_attempt_on is None:
        cycle.status = SubscriptionCycle.Status.FAILED
        _failure_task(cycle, reason)
    cycle.save(update_fields=('attempts', 'failure_reason', 'next_attempt_on', 'status', 'updated_at'))
    subscription = cycle.subscription
    if subscription.status == PatientSubscription.Status.ACTIVE:
        subscription.status = PatientSubscription.Status.PAYMENT_RETRY
        subscription.save(update_fields=('status', 'updated_at'))
    record_audit(company=cycle.company, actor=actor, patient=cycle.patient, action='billing.debit_failed', target=cycle,
                 request=request, metadata={'payment_id': payment.pk, 'attempt': payment.attempt,
                                            'retry_on': cycle.next_attempt_on.isoformat() if cycle.next_attempt_on else None})
    PatientEvent.objects.create(company_id=cycle.company_id, patient_id=cycle.patient_id, category=PatientEvent.Category.PAYMENT,
                                title='Payment failed',
                                detail=(f'{money(cycle.amount)} could not be collected. '
                                        + (f'We’ll try again on {cycle.next_attempt_on:%-d %B}.' if cycle.next_attempt_on
                                           else 'Update your payment details so your parcels can go out.')),
                                source_type='care.subscriptioncycle', source_id=str(cycle.pk))
    if first_failure or cycle.status == SubscriptionCycle.Status.FAILED:
        notify('payment_failed', cycle.pk)
    return payment


@transaction.atomic
def bill_subscription(subscription, *, today=None):
    """Open every cycle that has come round for this subscription and collect what is due: new cycles, retries that
    are due, and any cycle still waiting (a new debit order collects a failed payment straight away)."""
    if is_demo_record(subscription):
        return []
    today = today or timezone.localdate()
    subscription = PatientSubscription.objects.select_for_update(of=('self',)).select_related('authorization').get(pk=subscription.pk)
    if subscription.status not in BILLABLE:
        return []
    from .blood_tests import pause_reason

    if pause_reason(subscription.company_id, subscription.patient_id, today):
        return []  # Paused for blood tests: nothing is collected until the doctor reviews them (care.blood_tests).
    from .prescriptions import awaiting_confirmation

    if subscription.authorization_id and awaiting_confirmation(subscription.authorization):
        return []  # The doctor confirms an old prescription first (care.prescriptions.confirm_prescription).
    mandate = DebitMandate.objects.filter(subscription=subscription, status__in=LIVE_MANDATES).order_by('-approved_at', '-pk').first()
    if mandate is None:
        return []
    opened = 0
    # A payment that failed completely stops billing until it is paid: no more periods, so no debt for weeks without
    # pens. Paying it late carries the treatment on from that day (_carry_on_after_late_payment).
    stopped = subscription.cycles.filter(status=SubscriptionCycle.Status.FAILED).exists()
    # A debit order collected from files ahead of time (DebiCheck with Stitch) opens each cycle that many days early, so
    # the collection lands on the cycle's first day. Catching up is limited, so a long-forgotten next debit date can't
    # open months of cycles in one run.
    ahead = today + timedelta(days=lead_days(mandate))
    # Nothing is taken for a period the prescription sends no pens in: the payment before it was its last.
    while not stopped and subscription.next_debit_on is not None and subscription.next_debit_on <= ahead and opened < 3 \
            and period_has_parcel(subscription, subscription.next_debit_on):
        _open_cycle(subscription, mandate)
        opened += 1
    tried = []
    for cycle in subscription.cycles.filter(status__in=(SubscriptionCycle.Status.PENDING, SubscriptionCycle.Status.FAILED)).order_by('cycle_number'):
        retry_due = cycle.next_attempt_on is None or cycle.next_attempt_on <= today
        fresh_mandate = cycle.attempts and (cycle.mandate_id != mandate.pk)
        if cycle.status == SubscriptionCycle.Status.FAILED and not fresh_mandate:
            continue  # In the failure queue: a new debit order, or the practice, collects it.
        if cycle.attempts and not (retry_due or fresh_mandate):
            continue
        if fresh_mandate:
            cycle.mandate = mandate
            cycle.status = SubscriptionCycle.Status.PENDING
            cycle.save(update_fields=('mandate', 'status', 'updated_at'))
        payment = collect_cycle(cycle, today=today)
        if payment is not None:
            tried.append(payment)
    return tried


def check_unanswered(company):
    """Debits the provider didn't answer when they were sent (it timed out), and no webhook has settled since a day
    later: ask the provider what happened, so the cycle can be retried or marked paid instead of waiting for ever.
    (A DebiCheck collection is pending for days by design; the provider's files settle it, not this.)"""
    stale = Payment.objects.filter(company=company, patient__demo_data_set__isnull=True, status=Payment.Status.PENDING, checkout=False,
                                   subscription_cycle__isnull=False, created_at__lt=timezone.now() - timedelta(days=1))
    checked = 0
    for payment in stale.exclude(method=Payment.Method.DEBICHECK):
        provider = provider_named(payment.provider)
        if provider is None or not hasattr(provider, 'check_payment'):
            continue
        try:
            result = provider.check_payment(payment)
        except Exception:  # Not reachable now either: try again tomorrow.
            logger.warning('Could not check %s payment %s.', provider.name, payment.pk, exc_info=True)
            continue
        settle_payment(payment=payment, collection=result)
        checked += 1
    return checked


def run_debits(company, *, today=None):
    """The daily debit run for a practice. Returns how many debits were tried, paid and failed."""
    from .dose_prices import apply_due_price_changes

    today = today or timezone.localdate()
    apply_due_price_changes(company, today)  # A dose's scheduled new price, from its day.
    check_unanswered(company)
    counts = {'subscriptions': 0, 'tried': 0, 'paid': 0, 'failed': 0}
    due = PatientSubscription.objects.filter(company=company, status__in=BILLABLE, patient__is_active=True, patient__demo_data_set__isnull=True).filter(
        mandates__status__in=LIVE_MANDATES).distinct()
    for subscription in due:
        counts['subscriptions'] += 1
        for payment in bill_subscription(subscription, today=today):
            payment.refresh_from_db()
            counts['tried'] += 1
            if payment.status == Payment.Status.PAID:
                counts['paid'] += 1
            elif payment.status == Payment.Status.FAILED:
                counts['failed'] += 1
    return counts


# By the practice

@transaction.atomic
@block_demo_billing('cycle')
def retry_now(*, cycle, actor, request=None):
    """Try a failed or retrying debit again today, from Operations > Billing."""
    require_staff(cycle.company, actor)
    cycle = SubscriptionCycle.objects.select_for_update().get(pk=cycle.pk)
    if cycle.status == SubscriptionCycle.Status.PAID:
        raise ValidationError('This cycle is already paid.')
    if Payment.objects.filter(subscription_cycle=cycle, status=Payment.Status.PENDING, checkout=False).exists():
        raise ValidationError('A debit for this cycle is already with the payment provider, waiting for its answer.')
    if cycle.status == SubscriptionCycle.Status.FAILED:
        cycle.status = SubscriptionCycle.Status.PENDING
        cycle.save(update_fields=('status', 'updated_at'))
    payment = collect_cycle(cycle, actor=actor, request=request)
    if payment is None:
        cycle.refresh_from_db()
        raise ValidationError(cycle.failure_reason or 'Nothing could be collected.')
    record_audit(company=cycle.company, actor=actor, patient=cycle.patient, action='billing.debit_retried', target=cycle,
                 request=request, metadata={'payment_id': payment.pk})
    return payment


@transaction.atomic
@block_demo_billing('cycle', 'order', 'shipment')
def record_manual_payment(*, company, actor, amount, method, reference='', note='', cycle=None, order=None,
                          shipment=None, from_shipping=False, request=None):
    """A payment the practice received another way (EFT, cash, or the old debit order while patients move over),
    recorded against a cycle, a non-prescription order or one parcel. From Weekly shipping (a parcel, or its
    non-prescription request), a doctor who ships parcels can record one too."""
    if from_shipping and cycle is None:
        from .operations import require_operations_actor

        require_operations_actor(company, actor, shipping=True)
    else:
        require_staff(company, actor)
    targets = [target for target in (cycle, order, shipment) if target is not None]
    if len(targets) != 1:
        raise ValidationError('Record the payment against one cycle, order or parcel.')
    target = targets[0]
    if target.company_id != company.pk:
        raise PermissionDenied('This record belongs to another practice.')
    try:
        amount = Decimal(str(amount))
    except (ArithmeticError, ValueError):
        raise ValidationError('Enter the amount received.') from None
    if amount <= 0 or amount > Decimal('100000'):
        raise ValidationError('Enter the amount received, more than R0.')
    if method not in (Payment.Method.EFT, Payment.Method.CASH, Payment.Method.CARD, Payment.Method.DEBICHECK, Payment.Method.OTHER):
        raise ValidationError('Choose how it was paid.')
    reference, note = ' '.join(str(reference).split())[:120], ' '.join(str(note).split())[:300]
    payment = Payment(company=company, patient_id=target.patient_id, kind=Payment.Kind.MANUAL, method=method,
                      amount=amount, due_on=timezone.localdate(), paid_at=timezone.now(), status=Payment.Status.PAID,
                      provider_reference=reference, recorded_by=actor, note=note)
    if cycle is not None:
        cycle = SubscriptionCycle.objects.select_for_update().get(pk=cycle.pk)
        if cycle.status == SubscriptionCycle.Status.PAID:
            raise ValidationError('This cycle is already paid.')
        payment.subscription_id, payment.subscription_cycle = cycle.subscription_id, cycle
    elif order is not None:
        order = PharmacyOrder.objects.select_for_update().get(pk=order.pk)
        if order.paid_at:
            raise ValidationError('This order is already paid.')
        payment.pharmacy_order = order
    else:
        payment.shipment = shipment
    payment.full_clean()
    payment.save()
    if cycle is not None:
        _mark_paid(cycle, payment)
    elif order is not None:
        order.paid_at = payment.paid_at
        order.save(update_fields=('paid_at', 'updated_at'))
    record_audit(company=company, actor=actor, patient=payment.patient, action='billing.payment_recorded', target=payment,
                 request=request, metadata={'method': method, 'amount': str(amount),
                                            'for': 'cycle' if cycle else 'order' if order else 'parcel'})
    return payment


@transaction.atomic
@block_demo_billing('subscription')
def set_next_debit(*, subscription, actor, day, request=None):
    """Move the date the next debit is collected: a moved patient's existing debit date (the billing anchor), or a
    pause agreed with them."""
    require_staff(subscription.company, actor)
    subscription = PatientSubscription.objects.select_for_update().get(pk=subscription.pk)
    if subscription.status not in BILLABLE + (PatientSubscription.Status.PAUSED,):
        raise ValidationError('This subscription isn’t billed any more.')
    if day < timezone.localdate():
        raise ValidationError('Choose today or a later date.')
    before = subscription.next_debit_on
    subscription.next_debit_on = day
    subscription.save(update_fields=('next_debit_on', 'updated_at'))
    record_audit(company=subscription.company, actor=actor, patient=subscription.patient, action='billing.next_debit_moved',
                 target=subscription, request=request,
                 metadata={'before': before.isoformat() if before else None, 'after': day.isoformat()})
    return subscription


@transaction.atomic
@block_demo_billing('subscription')
def ask_for_new_debit_order(*, subscription, actor, request=None):
    """The practice asks the patient to approve a new debit order: their details changed, or one keeps failing."""
    require_staff(subscription.company, actor)
    mandate = current_mandate(subscription)
    if mandate is not None and mandate.status == DebitMandate.Status.ACTIVE:
        mandate.status = DebitMandate.Status.UPDATE_NEEDED
        mandate.save(update_fields=('status', 'updated_at'))
    notify('debit_order_request', subscription.pk, actor.pk)
    record_audit(company=subscription.company, actor=actor, patient=subscription.patient,
                 action='billing.new_debit_order_requested', target=subscription, request=request)


# Once-off payments: non-prescription requests

@transaction.atomic
@block_demo_billing('order')
def start_order_payment(*, order, actor, request=None):
    """The patient pays for their non-prescription request by card. Returns the payment and where to pay."""
    provider = require_provider()
    order = PharmacyOrder.objects.select_for_update(of=('self',)).select_related('patient').get(pk=order.pk)
    _require_patient(order.patient, actor)
    if order.paid_at:
        raise ValidationError('This request is already paid.')
    if order.status not in (PharmacyOrder.Status.SUBMITTED, PharmacyOrder.Status.ACCEPTED):
        raise ValidationError('Only a submitted request can be paid.')
    if order.subtotal <= 0:
        raise ValidationError('There is nothing to pay for this request.')
    payment = Payment.objects.create(company_id=order.company_id, patient=order.patient, pharmacy_order=order,
                                     kind=Payment.Kind.ONCE_OFF, method=Payment.Method.CARD, provider=provider.name,
                                     amount=order.subtotal, due_on=timezone.localdate(), status=Payment.Status.PENDING,
                                     checkout=True)
    url = provider.start_checkout(payment)
    record_audit(company=order.company, actor=actor, patient=order.patient, action='billing.order_payment_started',
                 target=payment, request=request, metadata={'order_id': order.pk, 'amount': str(order.subtotal)})
    return payment, url


@transaction.atomic
@block_demo_billing('cycle')
def start_cycle_payment(*, cycle, actor, request=None):
    """The patient pays a failed or retrying debit now, by card, instead of waiting for the next retry."""
    cycle = SubscriptionCycle.objects.select_for_update(of=('self',)).select_related('patient', 'subscription').get(pk=cycle.pk)
    _require_patient(cycle.patient, actor)
    if cycle.status in (SubscriptionCycle.Status.PAID, SubscriptionCycle.Status.CANCELLED):
        raise ValidationError('This payment is already settled.')
    if cycle.payments.filter(status=Payment.Status.PENDING).exists():
        raise ValidationError('A payment is already being processed. Wait for its result before starting another.')
    from .subscription_discounts import refresh_cycle_price, waive_cycle
    refresh_cycle_price(cycle)
    if cycle.amount == 0 and cycle.discount_amount > 0:
        from django.urls import reverse
        return waive_cycle(cycle, actor), reverse('portal:patient-billing')
    provider = require_provider()
    payment = Payment.objects.create(
        company_id=cycle.company_id, patient=cycle.patient, subscription=cycle.subscription, subscription_cycle=cycle,
        kind=Payment.Kind.SUBSCRIPTION, method=Payment.Method.CARD, provider=provider.name, attempt=cycle.attempts + 1,
        amount=cycle.amount, due_on=timezone.localdate(), status=Payment.Status.PENDING, checkout=True)
    url = provider.start_checkout(payment)
    record_audit(company=cycle.company, actor=actor, patient=cycle.patient, action='billing.card_payment_started',
                 target=payment, request=request, metadata={'cycle_id': cycle.pk, 'amount': str(cycle.amount)})
    return payment, url


@transaction.atomic
@block_demo_billing('payment')
def complete_card_payment(*, payment, paid, reference='', reason='', actor=None, request=None):
    """The provider's answer for a card payment: a non-prescription request, or a debit paid by card instead."""
    payment = Payment.objects.select_for_update(of=('self',)).select_related('pharmacy_order', 'subscription_cycle').get(pk=payment.pk)
    if payment.status != Payment.Status.PENDING:
        return payment
    payment.provider_reference = reference[:120] or payment.provider_reference
    if paid:
        payment.status, payment.paid_at = Payment.Status.PAID, timezone.now()
    else:
        payment.status, payment.failure_reason = Payment.Status.FAILED, (reason or 'The card payment was not completed.')[:500]
    payment.save(update_fields=('status', 'paid_at', 'failure_reason', 'provider_reference', 'updated_at'))
    if paid and payment.subscription_cycle_id:
        cycle = SubscriptionCycle.objects.select_for_update(of=('self',)).select_related('subscription').get(pk=payment.subscription_cycle_id)
        if cycle.status != SubscriptionCycle.Status.PAID:
            _mark_paid(cycle, payment)
    elif paid and payment.pharmacy_order_id:
        from .pharmacy import ship_paid_order

        order = payment.pharmacy_order
        if not order.paid_at:
            order.paid_at = payment.paid_at
            order.save(update_fields=('paid_at', 'updated_at'))
        # Items that need no prescription go on the next shipping day's list now; anything else waits for the practice.
        parcel = ship_paid_order(order, request=request)
        PatientEvent.objects.create(company_id=payment.company_id, patient_id=payment.patient_id,
                                    category=PatientEvent.Category.PAYMENT, title='Payment received',
                                    detail=f'{money(payment.amount)} for your order.' + (
                                        f' It goes out with the parcels on {parcel.scheduled_for:%-d %b}.' if parcel else ''),
                                    source_type='care.payment', source_id=str(payment.pk))
    record_audit(company=payment.company, actor=actor, patient=payment.patient,
                 action='billing.card_paid' if paid else 'billing.card_payment_failed', target=payment, request=request,
                 metadata={'for': 'cycle' if payment.subscription_cycle_id else 'order'})
    return payment


# The provider's answers: when the patient comes back from its page, and by webhook

@block_demo_billing('mandate')
def settle_mandate(*, mandate, approval, actor=None, request=None):
    """Apply what the provider said about a debit order the patient was sent to approve (an Approval)."""
    if approval.status == 'approved':
        return approve_mandate(mandate=mandate, account_label=approval.account_label, provider_reference=approval.reference,
                               actor=actor, request=request)
    if approval.status == 'declined':
        return decline_mandate(mandate=mandate, reason=approval.reason, actor=actor, request=request)
    if approval.status == 'revoked':
        return revoke_mandate(mandate=mandate, reason=approval.reason, actor=actor, request=request)
    return mandate


@transaction.atomic
@block_demo_billing('mandate')
def revoke_mandate(*, mandate, reason='', actor=None, request=None):
    """The patient's bank stopped an approved debit order (the patient revoked or paused it there): nothing more is
    collected with it, the patient is asked for a new one and the practice sees it on Operations > Billing."""
    mandate = DebitMandate.objects.select_for_update(of=('self',)).select_related('subscription').get(pk=mandate.pk)
    if mandate.status not in LIVE_MANDATES:
        return mandate
    mandate.status, mandate.cancelled_at = DebitMandate.Status.CANCELLED, timezone.now()
    mandate.failure_reason = (reason or 'Stopped at the bank.')[:300]
    mandate.save(update_fields=('status', 'cancelled_at', 'failure_reason', 'updated_at'))
    record_audit(company=mandate.company, actor=actor, patient=mandate.patient, action='billing.mandate_revoked',
                 target=mandate, request=request, metadata={'reason': mandate.failure_reason})
    PatientEvent.objects.create(company=mandate.company, patient=mandate.patient, category=PatientEvent.Category.PAYMENT,
                                title='Debit order stopped',
                                detail='Your bank stopped your debit order. Set up a new one so your parcels can keep going out.',
                                source_type='care.debitmandate', source_id=str(mandate.pk))
    if mandate.subscription_id and mandate.subscription.status in BILLABLE:
        notify('debit_order_request', mandate.subscription_id, None)
    return mandate


@block_demo_billing('payment')
def settle_payment(*, payment, collection, actor=None, request=None):
    """Apply what the provider said about a payment (a Collection): a card checkout, or a debit it answered later."""
    if collection.status == 'pending':
        if collection.reference and not payment.provider_reference:
            Payment.objects.filter(pk=payment.pk).update(provider_reference=collection.reference[:120], updated_at=timezone.now())
        return payment
    if payment.checkout:
        return complete_card_payment(payment=payment, paid=collection.status == 'paid', reference=collection.reference,
                                     reason=collection.reason, actor=actor, request=request)
    if payment.subscription_cycle_id:
        return record_collection(payment=payment, status=collection.status, reference=collection.reference,
                                 reason=collection.reason, actor=actor, request=request)
    return payment


def apply_notice(provider, notice):
    """A verified webhook (a Notice): find the debit order or payment it is about, made with this provider, and apply
    what happened. One about a record this app doesn't have is ignored."""
    if notice.mandate_id:
        mandate = DebitMandate.objects.filter(pk=notice.mandate_id, provider=provider.name).first()
        if mandate is not None and not is_demo_record(mandate):
            settle_mandate(mandate=mandate, approval=notice.outcome)
    elif notice.payment_id:
        payment = Payment.objects.filter(pk=notice.payment_id, provider=provider.name).first()
        if payment is not None and not is_demo_record(payment):
            settle_payment(payment=payment, collection=notice.outcome)


# Whether a parcel is paid for

def covering_cycle(shipment):
    """The subscription cycle that pays for this parcel: the first whose last day is on or after the parcel's day."""
    if not shipment.subscription_id:
        return None
    cycles = SubscriptionCycle.objects.filter(subscription_id=shipment.subscription_id).exclude(
        status=SubscriptionCycle.Status.CANCELLED).order_by('cycle_number')
    return next((cycle for cycle in cycles if _cycle_end(cycle) >= shipment.scheduled_for), None)


def parcel_payment(shipment):
    """(state, text) for a parcel: paid, due (not collected yet), retrying, failed or unpaid, with what the shipping
    list shows beside it."""
    if is_demo_record(shipment):
        return 'paid', 'Demo · payment simulated'
    if Payment.objects.filter(shipment=shipment, status=Payment.Status.PAID).exists():
        return 'paid', 'Paid (recorded by the practice)'
    order = getattr(shipment, 'pharmacy_order', None)
    if order is not None:
        return ('paid', 'Paid') if order.paid_at else ('unpaid', 'Request not paid yet')
    if not shipment.subscription_id:
        return 'unpaid', 'No subscription: the patient hasn’t started one'
    cycle = covering_cycle(shipment)
    if cycle is None:
        subscription = shipment.subscription
        if subscription.cycles.filter(status=SubscriptionCycle.Status.FAILED).exists():
            return 'failed', 'Waiting for a missed payment'
        mandate = current_mandate(subscription)
        if mandate is None or mandate.status == DebitMandate.Status.PENDING:
            return 'unpaid', 'No card saved yet'
        if subscription.next_debit_on:
            return 'due', f'Payment due {subscription.next_debit_on:%-d %b}'
        return 'unpaid', 'Not billed yet'
    if cycle.status == SubscriptionCycle.Status.PAID:
        return 'paid', 'Paid'
    if cycle.status == SubscriptionCycle.Status.FAILED:
        return 'failed', 'Payment failed'
    if cycle.attempts:
        return 'retrying', f'Payment failed, retrying {cycle.next_attempt_on:%-d %b}' if cycle.next_attempt_on else 'Payment failed'
    return 'due', cycle.failure_reason or f'Payment due {cycle.due_on:%-d %b}'


def paid_before_pause(shipment):
    """A parcel the patient had already paid for when they paused their subscription: it still goes out, as the last one
    until they resume, and staff see it marked "Last parcel before pause". Not when the doctor paused or revoked the
    treatment: then nothing goes out."""
    subscription = shipment.subscription if shipment.subscription_id else None
    if (subscription is None or subscription.status != PatientSubscription.Status.PAUSED
            or shipment.status not in (Shipment.Status.DRAFT, Shipment.Status.READY, Shipment.Status.HELD)):
        return False
    authorization = step_in_effect(subscription.authorization, shipment.scheduled_for) if subscription.authorization_id else None
    if authorization is None or authorization.status != TreatmentAuthorization.Status.ACTIVE:
        return False
    return parcel_payment(shipment)[0] == 'paid'


def paid_before_revoke(shipment):
    """A parcel already paid for when the doctor (or the Super Admin) revoked its prescription (James, 26 Nov 2026): it
    stays held, but the practice may still send it: Confirm & lock lets it through, since the patient paid for its
    pens."""
    if not shipment.authorization_id or shipment.status not in (Shipment.Status.DRAFT, Shipment.Status.READY, Shipment.Status.HELD):
        return False
    if shipment.authorization.status != TreatmentAuthorization.Status.CANCELLED:
        return False
    if not AuditEvent.objects.filter(company_id=shipment.company_id, action='treatment.revoke',
                                     target_type='care.treatmentauthorization', target_id=str(shipment.authorization_id)).exists():
        return False
    return parcel_payment(shipment)[0] == 'paid'


def parcel_payment_reason(shipment):
    """Why Check and lock week holds this parcel back for payment, or '' when it's paid or payment isn't required."""
    if not practice_settings(shipment.company).require_payment:
        return ''
    state, text = parcel_payment(shipment)
    return '' if state == 'paid' else f'Not paid: {text[0].lower()}{text[1:]}.'


# Overviews for the billing pages

def billing_state(subscription):
    """One word for where a subscription's billing stands, and its detail, for Operations > Billing."""
    if is_demo_record(subscription):
        return 'demo', 'Demo · billing disabled'
    mandate = current_mandate(subscription)
    failed = subscription.cycles.filter(status=SubscriptionCycle.Status.FAILED).order_by('-cycle_number').first()
    if failed is not None:
        return 'failed', f'{money(failed.amount)} failed {failed.due_on:%-d %b}'
    retrying = subscription.cycles.filter(status=SubscriptionCycle.Status.PENDING, attempts__gt=0).order_by('-cycle_number').first()
    if retrying is not None:
        when = f', retry {retrying.next_attempt_on:%-d %b}' if retrying.next_attempt_on else ''
        return 'retrying', f'{money(retrying.amount)} failed {retrying.attempts}×{when}'
    if mandate is None:
        return 'no_mandate', 'No card saved'
    if mandate.status == DebitMandate.Status.PENDING:
        return 'pending', 'Card not added yet'
    if mandate.status == DebitMandate.Status.UPDATE_NEEDED:
        return 'update', 'New amount to confirm'
    waiting = subscription.cycles.filter(status=SubscriptionCycle.Status.PENDING).order_by('-cycle_number').first()
    if waiting is not None:
        return 'due', waiting.failure_reason or f'{money(waiting.amount)} due {waiting.due_on:%-d %b}'
    return 'paid', 'Paid up'


def patient_summary(subscription):
    """A line for the patient about their subscription's billing, and whether something is waiting on them."""
    if is_demo_record(subscription):
        return {'state': 'demo', 'text': 'Demo · billing disabled', 'action': ''}
    state, text = billing_state(subscription)
    mandate = current_mandate(subscription)
    if state == 'no_mandate':
        return {'state': state, 'text': 'No card saved yet', 'action': 'Add my card'}
    if state == 'pending':
        return {'state': state, 'text': 'Your card isn’t saved yet', 'action': 'Add my card'}
    if state == 'update':
        return {'state': state, 'text': 'Your treatment or its price changed: confirm your card for the new amount', 'action': 'Confirm my card'}
    if state == 'retrying':
        retrying = subscription.cycles.filter(status=SubscriptionCycle.Status.PENDING, attempts__gt=0).order_by('-cycle_number').first()
        when = f' We’ll try again on {retrying.next_attempt_on:%-d %B}.' if retrying and retrying.next_attempt_on else ''
        return {'state': state, 'text': f'Your last payment didn’t go through.{when}', 'action': 'Use a different card'}
    if state == 'failed':
        return {'state': state, 'text': 'A payment is outstanding, so your next parcel is waiting', 'action': 'Use a different card'}
    day = next_payment_on(subscription)
    next_text = f', next payment {day:%-d %B}' if day else ''
    method = (mandate.account_label or 'card') if mandate.method == DebitMandate.Method.CARD else 'DebiCheck debit order'
    return {'state': state, 'text': f'Paid by {method}{next_text}', 'action': ''}


def outstanding(subscription):
    """What the patient owes: every cycle not paid yet."""
    if is_demo_record(subscription):
        return Decimal('0')
    return sum((cycle.amount for cycle in subscription.cycles.filter(
        status__in=(SubscriptionCycle.Status.PENDING, SubscriptionCycle.Status.FAILED))), Decimal('0'))
