"""Scheduled price changes for catalogue doses (DosePriceChange).

A Super Admin changes a dose's price on its catalogue page, with the date it applies from:
- Today changes it now, as before: the dose's price is the new one, and patients on the dose are asked to approve
  their debit order for the new amount (care.billing.review_mandates, as Practice settings say).
- A later date keeps the current price until then. Billing periods that start on or after the date are billed at the
  new price (care.billing.price_on), and payment schedules show it on their planned payments. On the day, the daily
  debit run (care.billing.run_debits) makes it the dose's price and asks for the debit order approvals.
- A scheduled change can be cancelled until its day. One scheduled for the same day replaces it.

Billing periods already opened, and paid ones, keep their amounts. Every prescription for the dose follows its price:
a prescription doesn't keep a price of its own. A patient who should keep paying less gets a discount instead
(care.subscription_discounts).
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import DosePriceChange, MedicationDose
from .services import record_audit


def _changes(dose):
    """The dose's changes that weren't cancelled, oldest first, kept on the dose for the rest of the request."""
    cached = getattr(dose, '_price_changes', None)
    if cached is None:
        cached = list(DosePriceChange.objects.filter(dose_id=dose.pk, cancelled_at__isnull=True).order_by('effective_on', 'pk'))
        dose._price_changes = cached
    return cached


def price_for(dose, on_date):
    """What the dose costs for a billing period that starts on on_date: a scheduled price from that day, the price
    before a change that took effect after it, or else its price."""
    if dose is None:
        return None
    changes = _changes(dose)
    scheduled = [change for change in changes if change.applied_at is None and change.effective_on <= on_date]
    if scheduled:
        return scheduled[-1].price
    later = [change for change in changes if change.applied_at is not None and change.effective_on > on_date]
    if later:
        return later[0].previous_price
    return dose.price


def scheduled_changes(dose):
    return [change for change in _changes(dose) if change.applied_at is None]


def mark_directions(dose, changes):
    """Mark each of a dose's scheduled changes (soonest first) 'up' or 'down' against the price before it: the dose's
    price, then the change before it. The catalogue shows it as an arrow. Returns the changes."""
    before = dose.price if dose is not None else None  # A new dose has no changes yet.
    for change in changes:
        change.direction = None if before is None or change.price == before else ('up' if change.price > before else 'down')
        before = change.price
    return changes


def _apply(change, now):
    dose = MedicationDose.objects.select_for_update().get(pk=change.dose_id)
    change.previous_price, change.applied_at = dose.price, now
    change.save(update_fields=('previous_price', 'applied_at', 'updated_at'))
    dose.price = change.price
    dose.save(update_fields=('price', 'updated_at'))
    record_audit(company=dose.company, actor=change.created_by, action='catalogue.dose_price_applied', target=dose,
                 metadata={'price_change_id': change.pk, 'previous_price': str(change.previous_price),
                           'price': str(change.price), 'effective_on': change.effective_on.isoformat()})


@transaction.atomic
def change_price(*, dose, price, effective_on, actor, request=None):
    """Change a dose's price from effective_on (today or later). Returns the DosePriceChange."""
    today = timezone.localdate()
    if price is None or price < 0:
        raise ValidationError('Enter a price of R0 or more.')
    if effective_on < today:
        raise ValidationError('Choose today or a later date for the new price.')
    dose = MedicationDose.objects.select_for_update().get(pk=dose.pk)
    # A change already scheduled for the same day is replaced by this one.
    DosePriceChange.objects.filter(dose=dose, effective_on=effective_on, applied_at__isnull=True,
                                   cancelled_at__isnull=True).update(cancelled_at=timezone.now(), cancelled_by=actor,
                                                                     updated_at=timezone.now())
    change = DosePriceChange(company_id=dose.company_id, dose=dose, price=price, effective_on=effective_on, created_by=actor)
    change.full_clean()
    change.save()
    record_audit(company=dose.company, actor=actor, action='catalogue.dose_price_scheduled', target=dose, request=request,
                 metadata={'price_change_id': change.pk, 'price': str(price), 'effective_on': effective_on.isoformat(),
                           'current_price': str(dose.price)})
    if effective_on <= today:
        _apply(change, timezone.now())
        from .billing import review_mandates

        review_mandates(dose.company)
    return change


@transaction.atomic
def cancel_price_change(*, change, actor, request=None):
    change = DosePriceChange.objects.select_for_update().select_related('dose').get(pk=change.pk)
    if not change.is_scheduled:
        raise ValidationError('This price change has already taken effect or was cancelled.')
    change.cancelled_at, change.cancelled_by = timezone.now(), actor
    change.save(update_fields=('cancelled_at', 'cancelled_by', 'updated_at'))
    record_audit(company=change.dose.company, actor=actor, action='catalogue.dose_price_cancelled', target=change.dose,
                 request=request, metadata={'price_change_id': change.pk, 'price': str(change.price),
                                            'effective_on': change.effective_on.isoformat()})
    return change


@transaction.atomic
def apply_due_price_changes(company, today=None):
    """Make each scheduled price whose day has come its dose's price, then ask for the debit order approvals it needs.
    Run by the daily debit run before anything is billed. Returns how many prices changed."""
    today = today or timezone.localdate()
    due = list(DosePriceChange.objects.select_for_update().filter(
        company=company, applied_at__isnull=True, cancelled_at__isnull=True, effective_on__lte=today,
    ).select_related('created_by').order_by('effective_on', 'pk'))
    now = timezone.now()
    for change in due:
        _apply(change, now)
    if due:
        from .billing import review_mandates

        review_mandates(company)
    return len(due)
