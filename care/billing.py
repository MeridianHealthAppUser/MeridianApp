"""Subscription billing: debit orders, the debit run, retries and the failure queue, once-off payments, and whether a
parcel is paid for.

How it fits together:
- A patient starts their subscription by giving a delivery address and approving a debit order (DebitMandate):
  DebiCheck by default, or a card. The mandate's amount is the most the practice may collect each time: the highest
  price their prescription reaches (a starting plan's last step).
- Each subscription is billed every few weeks: the billing interval of the dose in effect, 4 weeks unless the
  catalogue says otherwise. `manage.py run_debits` (daily) opens the next SubscriptionCycle when next_debit_on comes
  round and collects it. The first cycle is collected as soon as the debit order is approved.
- The provider (care.payment_providers) answers a debit at once, or later by webhook or file (DebiCheck with Stitch,
  collected from files a few days ahead). A debit waiting for its answer isn't sent again; one the provider never
  answered is looked up the next day (check_unanswered).
- A failed debit is retried on the practice's retry days (Practice settings, "3,7" by default). After the last retry
  the cycle is Failed: it waits in the failure queue (Operations > Billing) with a task for the practice, until it is
  paid or the practice records a payment received another way. The patient is emailed to update their details.
- A prescription or price change that needs more than the mandate's amount marks it "New amount to confirm"; the
  patient is asked to approve a new debit order, and nothing more is collected until they do.
- A parcel is paid for when the cycle covering its day is paid (a cycle covers the parcels up to its last day), when
  its non-prescription order is paid, or when the practice recorded a payment against it. Check and lock week
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
    ClinicalTask, DebitMandate, Invoice, PatientEvent, PatientSubscription, Payment, PharmacyOrder, PracticeSettings,
    SubscriptionCycle, TreatmentAuthorization,
)
from .notifications import queue as notify
from .payment_providers import current_provider, provider_named, require_provider
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
        raise PermissionDenied('Only the patient can set up their own debit order.')


def money(amount):
    """R3 295.00 as the practice writes it."""
    whole, cents = f'{Decimal(amount):.2f}'.split('.')
    return f'R{int(whole):,}'.replace(',', ' ') + f'.{cents}'


# What a subscription costs

def billing_weeks(subscription, on_date=None):
    dose = dose_in_effect(subscription, on_date)
    return dose.billing_weeks if dose is not None and dose.billing_weeks else DEFAULT_WEEKS


def price_on(subscription, on_date):
    """What the cycle starting on_date costs: the price of the dose in effect that day, or the product's price for
    a prescription that isn't for a catalogue dose."""
    dose = dose_in_effect(subscription, on_date)
    if dose is not None and dose.price is not None:
        return dose.price
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
    return prescription_amount(subscription.authorization) if subscription.authorization_id else None


def current_mandate(subscription):
    """The debit order collections use: the live one, else the latest still waiting for approval."""
    mandates = DebitMandate.objects.filter(company_id=subscription.company_id, subscription=subscription)
    return (mandates.filter(status__in=LIVE_MANDATES).order_by('-approved_at', '-pk').first()
            or mandates.filter(status=DebitMandate.Status.PENDING).order_by('-pk').first())


# Debit orders

@transaction.atomic
def start_debit_order(*, subscription, actor, method=DebitMandate.Method.DEBICHECK, request=None):
    """The patient asks for a debit order for their subscription. Returns the new mandate and the page where they
    approve it (the provider's, or the sandbox's)."""
    provider = require_provider()
    company = Company.objects.select_for_update().get(pk=subscription.company_id)
    subscription = PatientSubscription.objects.select_for_update().select_related(
        'patient', 'authorization__dose', 'authorization__product').get(pk=subscription.pk, company=company)
    _require_patient(subscription.patient, actor)
    if subscription.status == PatientSubscription.Status.CANCELLED:
        raise ValidationError('This subscription is cancelled. Start a new one to set up a debit order.')
    if method not in DebitMandate.Method.values:
        raise ValidationError('Choose a DebiCheck debit order or a card.')
    if method not in provider.methods:
        raise ValidationError(f'{provider.label} takes {" or ".join(DebitMandate.Method(m).label.lower() for m in provider.methods)} only.')
    amount = mandate_amount(subscription)
    if amount is None:
        raise ValidationError('Your prescription has no price yet, so the practice can’t set up a debit order for it. '
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
def approve_mandate(*, mandate, account_label='', provider_reference='', fail_debits=False, actor=None, request=None):
    """The patient approved the debit order with their bank or card provider. It replaces any earlier one, sets the
    billing date when this is the first, and collects what is due now: the first debit, or a payment that failed.
    provider_reference is what the provider collects with (its mandate, or the stored card's token), when it is only
    known once approved."""
    mandate = DebitMandate.objects.select_for_update().select_related('subscription__patient').get(pk=mandate.pk)
    if mandate.status == DebitMandate.Status.ACTIVE:
        return mandate
    if mandate.status != DebitMandate.Status.PENDING:
        raise ValidationError('This debit order request is no longer open. Start a new one.')
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
                                title='Debit order approved',
                                detail=f'{mandate.get_method_display()} for up to {money(mandate.amount)} every '
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
    for mandate in mandates.exclude(provider_reference=''):
        provider = provider_named(mandate.provider)
        if provider is not None:
            transaction.on_commit(lambda mandate=mandate, provider=provider: provider.cancel_mandate(mandate))


@transaction.atomic
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
    """After a prescription or price change, a live debit order is marked "New amount to confirm" and the patient is
    asked to approve a new one: by default whenever the prescription changed (a new or changed prescription, not a
    renewal) or its amount did; with Practice settings' "Only when the amount goes above", only when it is now too low.
    Returns the mandates marked."""
    any_change = practice_settings(company).new_debit_order == PracticeSettings.NewDebitOrder.ANY_CHANGE
    marked = []
    mandates = DebitMandate.objects.filter(company=company, status=DebitMandate.Status.ACTIVE).select_related('subscription')
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

def _cycle_end(cycle):
    return cycle.ends_on or cycle.starts_on + timedelta(weeks=DEFAULT_WEEKS, days=-1)


def _open_cycle(subscription, mandate):
    """The subscription's next cycle, from its next debit date. Moves the next debit date on by the interval."""
    starts = subscription.next_debit_on
    weeks = billing_weeks(subscription, starts)
    amount = price_on(subscription, starts)
    number = (subscription.cycles.order_by('-cycle_number').values_list('cycle_number', flat=True).first() or 0) + 1
    cycle = SubscriptionCycle(
        company_id=subscription.company_id, patient_id=subscription.patient_id, subscription=subscription,
        cycle_number=number, starts_on=starts, ends_on=starts + timedelta(weeks=weeks, days=-1), due_on=starts,
        amount=amount if amount is not None else Decimal('0'), mandate=mandate,
        idempotency_key=f'subscription-{subscription.pk}-cycle-{number}',
    )
    cycle.full_clean()
    cycle.save()
    invoice = Invoice(company_id=subscription.company_id, patient_id=subscription.patient_id, subscription_cycle=cycle,
                      invoice_number=f'SUB-{subscription.pk:05d}-{number:03d}', issued_on=starts, due_on=starts,
                      subtotal=cycle.amount, total=cycle.amount, status=Invoice.Status.ISSUED)
    invoice.full_clean()
    invoice.save()
    subscription.cycle_number = number
    subscription.next_debit_on = starts + timedelta(weeks=weeks)
    subscription.save(update_fields=('cycle_number', 'next_debit_on', 'updated_at'))
    return cycle


def resume_debit_date(subscription):
    """When a resumed subscription is next debited: the day after the last period it paid for, or today; nothing
    before its debit order is approved."""
    last = subscription.cycles.exclude(status=SubscriptionCycle.Status.CANCELLED).order_by('-cycle_number').first()
    today = timezone.localdate()
    if last is not None:
        return max(today, _cycle_end(last) + timedelta(days=1))
    live = DebitMandate.objects.filter(subscription=subscription, status__in=LIVE_MANDATES).exists()
    return today if live else None


def _retry_on(cycle, settings_row):
    """When the next retry is, or None after the last one."""
    days = settings_row.retry_days()
    return cycle.due_on + timedelta(days=days[cycle.attempts - 1]) if cycle.attempts <= len(days) else None


def _mark_paid(cycle, payment):
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
    today = today or timezone.localdate()
    cycle = SubscriptionCycle.objects.select_for_update().select_related('subscription', 'patient').get(pk=cycle.pk)
    if cycle.status in (SubscriptionCycle.Status.PAID, SubscriptionCycle.Status.CANCELLED):
        return None
    if Payment.objects.filter(subscription_cycle=cycle, status=Payment.Status.PENDING, checkout=False).exists():
        return None  # A debit already sent is waiting for the provider's answer (a DebiCheck collection takes days).
    subscription = cycle.subscription
    mandate = DebitMandate.objects.filter(subscription=subscription, status__in=LIVE_MANDATES).order_by('-approved_at', '-pk').first()
    if mandate is None:
        cycle.failure_reason = 'No approved debit order.'
        cycle.save(update_fields=('failure_reason', 'updated_at'))
        return None
    if mandate.status == DebitMandate.Status.UPDATE_NEEDED or mandate.amount < cycle.amount:
        if mandate.status != DebitMandate.Status.UPDATE_NEEDED:
            mandate.status = DebitMandate.Status.UPDATE_NEEDED
            mandate.save(update_fields=('status', 'updated_at'))
            notify('debit_order_update', mandate.pk)
        cycle.failure_reason = 'Waiting for the patient to approve a debit order for the new amount.'
        cycle.save(update_fields=('failure_reason', 'updated_at'))
        return None
    # Collected by the provider that holds the debit order, which may not be the one new debit orders go to.
    provider = provider_named(mandate.provider)
    if provider is None:
        cycle.failure_reason = 'The payment provider that holds this debit order isn’t connected.'
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
def record_collection(*, payment, status, reference='', reason='', today=None, actor=None, request=None):
    """What the provider said about a debit: straight away, or later by webhook for one that was pending."""
    today = today or timezone.localdate()
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.status != Payment.Status.PENDING:
        return payment  # Already settled: the provider told us twice (its answer, then a webhook).
    cycle = SubscriptionCycle.objects.select_for_update().select_related('subscription', 'patient').get(pk=payment.subscription_cycle_id)
    payment.provider_reference = reference[:120] or payment.provider_reference
    if status == 'pending':
        payment.save(update_fields=('provider_reference', 'updated_at'))
        return payment
    if status == 'paid':
        payment.status, payment.paid_at = Payment.Status.PAID, timezone.now()
        payment.save(update_fields=('status', 'paid_at', 'provider_reference', 'updated_at'))
        cycle.attempts = payment.attempt
        cycle.save(update_fields=('attempts', 'updated_at'))
        _mark_paid(cycle, payment)
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
    today = today or timezone.localdate()
    subscription = PatientSubscription.objects.select_for_update().select_related('authorization').get(pk=subscription.pk)
    if subscription.status not in BILLABLE:
        return []
    mandate = DebitMandate.objects.filter(subscription=subscription, status__in=LIVE_MANDATES).order_by('-approved_at', '-pk').first()
    if mandate is None:
        return []
    opened = 0
    # A debit order collected from files ahead of time (DebiCheck with Stitch) opens each cycle that many days early, so
    # the collection lands on the cycle's first day. Catching up is limited, so a long-forgotten next debit date can't
    # open months of cycles in one run.
    ahead = today + timedelta(days=lead_days(mandate))
    while subscription.next_debit_on is not None and subscription.next_debit_on <= ahead and opened < 3:
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
    stale = Payment.objects.filter(company=company, status=Payment.Status.PENDING, checkout=False,
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
    today = today or timezone.localdate()
    check_unanswered(company)
    counts = {'subscriptions': 0, 'tried': 0, 'paid': 0, 'failed': 0}
    due = PatientSubscription.objects.filter(company=company, status__in=BILLABLE, patient__is_active=True).filter(
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
def start_order_payment(*, order, actor, request=None):
    """The patient pays for their non-prescription request by card. Returns the payment and where to pay."""
    provider = require_provider()
    order = PharmacyOrder.objects.select_for_update().select_related('patient').get(pk=order.pk)
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
def start_cycle_payment(*, cycle, actor, request=None):
    """The patient pays a failed or retrying debit now, by card, instead of waiting for the next retry."""
    provider = require_provider()
    cycle = SubscriptionCycle.objects.select_for_update().select_related('patient', 'subscription').get(pk=cycle.pk)
    _require_patient(cycle.patient, actor)
    if cycle.status in (SubscriptionCycle.Status.PAID, SubscriptionCycle.Status.CANCELLED):
        raise ValidationError('This payment is already settled.')
    payment = Payment.objects.create(
        company_id=cycle.company_id, patient=cycle.patient, subscription=cycle.subscription, subscription_cycle=cycle,
        kind=Payment.Kind.SUBSCRIPTION, method=Payment.Method.CARD, provider=provider.name, attempt=cycle.attempts + 1,
        amount=cycle.amount, due_on=timezone.localdate(), status=Payment.Status.PENDING, checkout=True)
    url = provider.start_checkout(payment)
    record_audit(company=cycle.company, actor=actor, patient=cycle.patient, action='billing.card_payment_started',
                 target=payment, request=request, metadata={'cycle_id': cycle.pk, 'amount': str(cycle.amount)})
    return payment, url


@transaction.atomic
def complete_card_payment(*, payment, paid, reference='', reason='', actor=None, request=None):
    """The provider's answer for a card payment: a non-prescription request, or a debit paid by card instead."""
    payment = Payment.objects.select_for_update().select_related('pharmacy_order', 'subscription_cycle').get(pk=payment.pk)
    if payment.status != Payment.Status.PENDING:
        return payment
    payment.provider_reference = reference[:120] or payment.provider_reference
    if paid:
        payment.status, payment.paid_at = Payment.Status.PAID, timezone.now()
    else:
        payment.status, payment.failure_reason = Payment.Status.FAILED, (reason or 'The card payment was not completed.')[:500]
    payment.save(update_fields=('status', 'paid_at', 'failure_reason', 'provider_reference', 'updated_at'))
    if paid and payment.subscription_cycle_id:
        cycle = SubscriptionCycle.objects.select_for_update().select_related('subscription').get(pk=payment.subscription_cycle_id)
        if cycle.status != SubscriptionCycle.Status.PAID:
            _mark_paid(cycle, payment)
    elif paid and payment.pharmacy_order_id:
        order = payment.pharmacy_order
        if not order.paid_at:
            order.paid_at = payment.paid_at
            order.save(update_fields=('paid_at', 'updated_at'))
        PatientEvent.objects.create(company_id=payment.company_id, patient_id=payment.patient_id,
                                    category=PatientEvent.Category.PAYMENT, title='Payment received',
                                    detail=f'{money(payment.amount)} for your request.', source_type='care.payment',
                                    source_id=str(payment.pk))
    record_audit(company=payment.company, actor=actor, patient=payment.patient,
                 action='billing.card_paid' if paid else 'billing.card_payment_failed', target=payment, request=request,
                 metadata={'for': 'cycle' if payment.subscription_cycle_id else 'order'})
    return payment


# The provider's answers: when the patient comes back from its page, and by webhook

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
def revoke_mandate(*, mandate, reason='', actor=None, request=None):
    """The patient's bank stopped an approved debit order (the patient revoked or paused it there): nothing more is
    collected with it, the patient is asked for a new one and the practice sees it on Operations > Billing."""
    mandate = DebitMandate.objects.select_for_update().select_related('subscription').get(pk=mandate.pk)
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
        if mandate is not None:
            settle_mandate(mandate=mandate, approval=notice.outcome)
    elif notice.payment_id:
        payment = Payment.objects.filter(pk=notice.payment_id, provider=provider.name).first()
        if payment is not None:
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
        mandate = current_mandate(subscription)
        if mandate is None or mandate.status == DebitMandate.Status.PENDING:
            return 'unpaid', 'No approved debit order yet'
        if subscription.next_debit_on:
            return 'due', f'Debit due {subscription.next_debit_on:%-d %b}'
        return 'unpaid', 'Not billed yet'
    if cycle.status == SubscriptionCycle.Status.PAID:
        return 'paid', 'Paid'
    if cycle.status == SubscriptionCycle.Status.FAILED:
        return 'failed', 'Debit failed'
    if cycle.attempts:
        return 'retrying', f'Debit failed, retrying {cycle.next_attempt_on:%-d %b}' if cycle.next_attempt_on else 'Debit failed'
    return 'due', cycle.failure_reason or f'Debit due {cycle.due_on:%-d %b}'


def parcel_payment_reason(shipment):
    """Why Check and lock week holds this parcel back for payment, or '' when it's paid or payment isn't required."""
    if not practice_settings(shipment.company).require_payment:
        return ''
    state, text = parcel_payment(shipment)
    return '' if state == 'paid' else f'Not paid: {text[0].lower()}{text[1:]}.'


# Overviews for the billing pages

def billing_state(subscription):
    """One word for where a subscription's billing stands, and its detail, for Operations > Billing."""
    mandate = current_mandate(subscription)
    failed = subscription.cycles.filter(status=SubscriptionCycle.Status.FAILED).order_by('-cycle_number').first()
    if failed is not None:
        return 'failed', f'{money(failed.amount)} failed {failed.due_on:%-d %b}'
    retrying = subscription.cycles.filter(status=SubscriptionCycle.Status.PENDING, attempts__gt=0).order_by('-cycle_number').first()
    if retrying is not None:
        when = f', retry {retrying.next_attempt_on:%-d %b}' if retrying.next_attempt_on else ''
        return 'retrying', f'{money(retrying.amount)} failed {retrying.attempts}×{when}'
    if mandate is None:
        return 'no_mandate', 'No debit order'
    if mandate.status == DebitMandate.Status.PENDING:
        return 'pending', 'Debit order waiting for approval'
    if mandate.status == DebitMandate.Status.UPDATE_NEEDED:
        return 'update', 'New amount to confirm'
    waiting = subscription.cycles.filter(status=SubscriptionCycle.Status.PENDING).order_by('-cycle_number').first()
    if waiting is not None:
        return 'due', waiting.failure_reason or f'{money(waiting.amount)} due {waiting.due_on:%-d %b}'
    return 'paid', 'Paid up'


def patient_summary(subscription):
    """A line for the patient about their subscription's billing, and whether something is waiting on them."""
    state, text = billing_state(subscription)
    mandate = current_mandate(subscription)
    if state == 'no_mandate':
        return {'state': state, 'text': 'No debit order yet', 'action': 'Set up my debit order'}
    if state == 'pending':
        return {'state': state, 'text': 'Your debit order is waiting for your approval', 'action': 'Approve my debit order'}
    if state == 'update':
        return {'state': state, 'text': 'Your new debit order amount needs your approval', 'action': 'Approve the new amount'}
    if state == 'retrying':
        retrying = subscription.cycles.filter(status=SubscriptionCycle.Status.PENDING, attempts__gt=0).order_by('-cycle_number').first()
        when = f' We’ll try again on {retrying.next_attempt_on:%-d %B}.' if retrying and retrying.next_attempt_on else ''
        return {'state': state, 'text': f'Your last payment didn’t go through.{when}', 'action': 'Update my payment details'}
    if state == 'failed':
        return {'state': state, 'text': 'A payment is outstanding, so your next parcel is waiting', 'action': 'Update my payment details'}
    next_text = f', next payment {subscription.next_debit_on:%-d %B}' if subscription.next_debit_on else ''
    method = 'card' if mandate.method == DebitMandate.Method.CARD else 'DebiCheck debit order'
    return {'state': state, 'text': f'Paid by {method}{next_text}', 'action': ''}


def outstanding(subscription):
    """What the patient owes: every cycle not paid yet."""
    return sum((cycle.amount for cycle in subscription.cycles.filter(
        status__in=(SubscriptionCycle.Status.PENDING, SubscriptionCycle.Status.FAILED))), Decimal('0'))
