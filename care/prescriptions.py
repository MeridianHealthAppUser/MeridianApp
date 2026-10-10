"""Renew, change or create a patient's main prescription from the patient record.

A prescription is a treatment authorisation for a main weight-management medicine. Every renewal
or change writes a new authorisation that replaces the current one, so the history is kept, and
writes a line to the clinical log in the same transaction. Any doctor at the practice may do this.
A safety flag in the patient's summary data must be acknowledged first.
"""

import calendar
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.formats import date_format

from .clinical import _lock_context, _require_doctor
from .models import MedicationProduct, PatientSubscription, Shipment, TreatmentAuthorization
from .services import record_audit
from .treatment import _event


RENEWAL_DUE_DAYS = 30
MONTH_CHOICES = tuple(range(1, 7))
STALE = 'The prescription changed in another tab. Reload the record and try again.'
UNDISPATCHED = (Shipment.Status.DRAFT, Shipment.Status.READY, Shipment.Status.HELD)


def add_months(day, months):
    """The same day N months later, or that month's last day when the day doesn't exist there."""
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def strength_mg(product):
    match = re.search(r'\d+(?:\.\d+)?', product.strength or '')
    try:
        return Decimal(match.group()) if match else None
    except InvalidOperation:
        return None


def dose_label(product):
    """'0.25 mg weekly': the strength and how often it is taken."""
    frequency = product.get_dose_frequency_display().lower()
    return ' '.join(part for part in (product.strength.strip(), frequency) if part)


def prescription_label(authorization):
    return f'{authorization.product.name} {authorization.max_dose}'.strip()


def main_treatments(company):
    return MedicationProduct.objects.for_company(company).filter(
        category=MedicationProduct.Category.WEIGHT_MANAGEMENT, requires_authorisation=True, is_active=True,
    )


def prescription_options(company, current=None):
    """Each main medicine with its doses, lowest first. The current medicine is always offered."""
    products = main_treatments(company)
    if current is not None:
        products = products | MedicationProduct.objects.for_company(company).filter(name=current.product.name, is_active=True)
    options = {}
    for product in products.distinct():
        options.setdefault(product.name, []).append(product)
    for name, doses in options.items():
        doses.sort(key=lambda product: (strength_mg(product) is None, strength_mg(product) or 0, product.strength))
    return dict(sorted(options.items()))


def current_prescription(company, patient):
    """The latest main prescription that hasn't been replaced or revoked."""
    return TreatmentAuthorization.objects.for_company(company).filter(
        patient=patient, product__company=company,
        product__category=MedicationProduct.Category.WEIGHT_MANAGEMENT, product__requires_authorisation=True,
    ).exclude(status=TreatmentAuthorization.Status.CANCELLED).select_related('product', 'prescribed_by').order_by(
        '-starts_on', '-created_at', '-pk',
    ).first()


def prescription_state(authorization, today=None):
    """none, paused, expired, due (valid but expiring within 30 days) or valid."""
    today = today or timezone.localdate()
    if authorization is None:
        return 'none'
    if authorization.status == TreatmentAuthorization.Status.PAUSED:
        return 'paused'
    if authorization.status == TreatmentAuthorization.Status.EXPIRED or authorization.expires_on < today:
        return 'expired'
    if (authorization.expires_on - today).days <= RENEWAL_DUE_DAYS:
        return 'due'
    return 'valid'


def _months(value):
    if type(value) is not int or value not in MONTH_CHOICES:
        raise ValidationError('Choose between 1 and 6 months.')
    return value


def _check_flags(company, patient, acknowledged):
    from .clinical_summary import summary_safety_flags

    flags = summary_safety_flags(company, patient)
    if flags and acknowledged is not True:
        raise ValidationError('This patient has a safety flag. Tick to confirm you have reviewed it before prescribing.')
    return flags


def _locked_current(company, patient, expected):
    current = current_prescription(company, patient)
    if (current.pk if current else None) != (expected.pk if expected else None):
        raise ValidationError(STALE)
    if current is None:
        return None
    return TreatmentAuthorization.objects.select_for_update().select_related('product', 'prescribed_by').get(pk=current.pk)


def _supersede(company, patient, previous, replacement, *, hold):
    """Move care plans to the new prescription. A changed medicine or dose holds undispatched parcels."""
    previous.status = TreatmentAuthorization.Status.CANCELLED
    previous.save(update_fields=('status', 'updated_at'))
    plans = list(PatientSubscription.objects.select_for_update().for_company(company).filter(
        patient=patient, authorization=previous,
    ).exclude(status=PatientSubscription.Status.CANCELLED))
    for plan in plans:
        plan.authorization = replacement
        plan.review_due_on = replacement.expires_on
        plan.full_clean()
        plan.save(update_fields=('authorization', 'review_due_on', 'updated_at'))
    parcels = Shipment.objects.select_for_update().for_company(company).filter(
        Q(subscription_id__in=[plan.pk for plan in plans]) | Q(authorization=previous),
        patient=patient, status__in=UNDISPATCHED,
    )
    if hold:
        parcels.update(status=Shipment.Status.HELD, updated_at=timezone.now(),
                       hold_reason='Prescription changed. Dispensing review is required before dispatch.')
    else:
        # Same medicine and dose: parcels carry on under the renewed prescription.
        parcels.filter(authorization=previous).update(authorization=replacement, updated_at=timezone.now())


def _months_text(months):
    return f'{months} month{"" if months == 1 else "s"}'


@transaction.atomic
def renew_prescription(*, company, patient, actor, current, months, acknowledge_flags=False, request=None):
    company, patient = _lock_context(company, patient, actor)
    _require_doctor(company, actor)
    months = _months(months)
    previous = _locked_current(company, patient, current)
    if previous is None:
        raise ValidationError('There is no prescription to renew. Create one instead.')
    if not previous.product.is_active:
        raise ValidationError('This medicine is no longer in the catalogue. Change the prescription instead.')
    flags = _check_flags(company, patient, acknowledge_flags)
    today = timezone.localdate()
    expires_on = add_months(max(previous.expires_on, today), months)
    renewal = TreatmentAuthorization(
        company=company, patient=patient, prescribed_by=actor, product=previous.product, max_dose=previous.max_dose,
        quantity_per_cycle=previous.quantity_per_cycle, starts_on=today, expires_on=expires_on,
        review_interval_days=previous.review_interval_days, instructions=previous.instructions, replaces=previous,
    )
    renewal.log_text = (f'Prescription renewed for {_months_text(months)}: {prescription_label(renewal)}, '
                        f'valid to {date_format(expires_on, "j M Y")}.')
    renewal.full_clean()
    renewal.save()
    _supersede(company, patient, previous, renewal, hold=False)
    record_audit(company=company, actor=actor, patient=patient, action='prescription.renewed', target=renewal,
                 request=request, metadata={'previous_authorization_id': previous.pk, 'months': months,
                                            'safety_flags_acknowledged': bool(flags)})
    _event(renewal, 'Treatment authorisation renewed', 'Your doctor recorded a treatment decision. Open your treatment page to see its details.')
    return renewal


@transaction.atomic
def change_prescription(*, company, patient, actor, current, product, months, reason='', acknowledge_flags=False,
                        request=None):
    """Replace the current prescription from today, or create the first one."""
    company, patient = _lock_context(company, patient, actor)
    _require_doctor(company, actor)
    months = _months(months)
    previous = _locked_current(company, patient, current)
    if not isinstance(reason, str) or len(reason) > 500:
        raise ValidationError('Keep the reason to 500 characters or fewer.')
    reason = ' '.join(reason.split())
    allowed = {item.pk for doses in prescription_options(company, previous).values() for item in doses}
    if getattr(product, 'pk', None) not in allowed:
        raise ValidationError('Choose a medicine and dose from this patient’s authorised medications.')
    product = MedicationProduct.objects.get(pk=product.pk, company=company)
    if previous is not None and previous.product_id == product.pk:
        raise ValidationError('That is the current prescription. Use Renew to extend it.')
    flags = _check_flags(company, patient, acknowledge_flags)
    today = timezone.localdate()
    expires_on = add_months(today, months)
    prescription = TreatmentAuthorization(
        company=company, patient=patient, prescribed_by=actor, product=product, max_dose=dose_label(product),
        quantity_per_cycle=previous.quantity_per_cycle if previous else 1, starts_on=today, expires_on=expires_on,
        review_interval_days=previous.review_interval_days if previous else 180,
        replaces=previous, change_reason=reason,
    )
    valid_to = f'{_months_text(months)}, valid to {date_format(expires_on, "j M Y")}.'
    if previous is not None:
        prescription.log_text = (f'{reason.rstrip(".")}. ' if reason else '') + (
            f'Prescription changed from {prescription_label(previous)} to {prescription_label(prescription)}, {valid_to}')
    else:
        prescription.log_text = (f'{reason.rstrip(".")}. ' if reason else '') + (
            f'Prescription created: {prescription_label(prescription)}, {valid_to}')
    prescription.full_clean()
    prescription.save()
    if previous is not None:
        _supersede(company, patient, previous, prescription, hold=True)
    record_audit(company=company, actor=actor, patient=patient,
                 action='prescription.changed' if previous else 'prescription.created', target=prescription,
                 request=request, metadata={'previous_authorization_id': previous.pk if previous else None,
                                            'months': months, 'safety_flags_acknowledged': bool(flags)})
    _event(prescription, 'Treatment authorised', 'Your doctor recorded a treatment decision. Open your treatment page to see its details.')
    return prescription


def renewal_expiry(authorization, months, today=None):
    today = today or timezone.localdate()
    return add_months(max(authorization.expires_on, today), months)


def expiry_preview(months, today=None):
    return add_months(today or timezone.localdate(), months)
