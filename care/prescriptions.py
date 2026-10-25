"""Renew, change or create a patient's main prescription from the patient record.

A prescription is a treatment authorisation for a dose of a main weight-management medicine, from the Pharmacy
catalogue (MedicationDose), for a number of weeks. Every renewal or change writes a new authorisation that replaces
the current one, so the history is kept, and writes a line to the clinical log in the same transaction. Any doctor at
the practice may do this, and the doctor who writes it becomes the patient's Prescribing doctor.
A safety flag in the patient's summary data must be acknowledged first.

A prescription started from a starting plan (care.starting_plans) is a chain of steps sharing a plan_key. The
current prescription is the step in effect; the prescription ends when its last step does. Renewing mid-plan
extends the last step; changing replaces the rest of the plan from today.

Saving a prescription puts its parcels on the Weekly shipping list as drafts (care.parcels). A renewal adds the
parcels its extra weeks need; a change cancels the old prescription's drafts and holds any already marked ready.
"""

import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone
from django.utils.formats import date_format

from .clinical import _lock_context, _require_doctor
from .models import MedicationDose, MedicationProduct, PatientSubscription, Shipment, TreatmentAuthorization
from .parcels import schedule_parcels
from .patient_assignment import make_prescribing_doctor
from .services import record_audit
from .treatment import _event


RENEWAL_DUE_DAYS = 30
# Treatment runs in weeks; 24 weeks is the usual cycle.
WEEK_CHOICES = (4, 8, 12, 16, 20, 24)
DEFAULT_WEEKS = 24
STALE = 'The prescription changed in another tab. Reload the record and try again.'
UNDISPATCHED = (Shipment.Status.DRAFT, Shipment.Status.READY, Shipment.Status.HELD)


def add_weeks(day, weeks):
    return day + timedelta(weeks=weeks)


def weeks_text(weeks):
    return f'{weeks} week{"" if weeks == 1 else "s"}'


def dose_mg(text):
    """The mg in a dose as written ('0.25 mg weekly' is 0.25), or None."""
    match = re.search(r'\d+(?:\.\d+)?', text or '')
    try:
        return Decimal(match.group()) if match else None
    except InvalidOperation:
        return None


def prescription_label(authorization):
    return f'{authorization.product.name} {authorization.max_dose}'.strip()


def main_doses(company):
    """Doses a doctor can prescribe: offered, priced, and of a main medicine still in the catalogue."""
    return MedicationDose.objects.for_company(company).filter(
        is_active=True, price__isnull=False, pen__is_active=True,
        pen__category=MedicationProduct.Category.WEIGHT_MANAGEMENT, pen__requires_authorisation=True,
    ).select_related('pen')


def prescription_options(company, current=None):
    """Each main medicine with its doses, lowest first. The current medicine's doses are always offered."""
    doses = main_doses(company)
    if current is not None:
        doses = doses | MedicationDose.objects.for_company(company).filter(
            is_active=True, price__isnull=False, pen__is_active=True, pen__name=current.product.name,
        ).select_related('pen')
    options = {}
    for dose in doses.distinct():
        options.setdefault(dose.medicine, []).append(dose)
    for doses in options.values():
        doses.sort(key=lambda dose: (dose.amount_mg, dose.frequency, dose.pk))
    return dict(sorted(options.items()))


def started(authorizations, today=None):
    """Leave out a starting plan's later steps that haven't begun. They are saved ahead of time; the step in effect
    is the patient's prescription."""
    return authorizations.exclude(plan_step__gt=1, starts_on__gt=today or timezone.localdate())


def with_plan_ends(authorizations):
    """Set ends_on on each authorisation: a starting plan's prescription ends when its last step does."""
    keys = {authorization.plan_key for authorization in authorizations if authorization.plan_key}
    ends = dict(TreatmentAuthorization.objects.filter(plan_key__in=keys).exclude(
        status=TreatmentAuthorization.Status.CANCELLED,
    ).values('plan_key').annotate(end=Max('expires_on')).values_list('plan_key', 'end')) if keys else {}
    for authorization in authorizations:
        authorization.ends_on = ends.get(authorization.plan_key, authorization.expires_on)
    return authorizations


def current_prescription(company, patient):
    """The latest main prescription that hasn't been replaced or revoked. In a starting plan, the step in effect
    (a later step counts once it starts), with plan_steps (all its steps, in order) and ends_on (the last step's end)."""
    current = started(TreatmentAuthorization.objects.for_company(company).filter(
        patient=patient, product__company=company,
        product__category=MedicationProduct.Category.WEIGHT_MANAGEMENT, product__requires_authorisation=True,
    ).exclude(status=TreatmentAuthorization.Status.CANCELLED)).select_related(
        'product', 'dose__pen', 'prescribed_by',
    ).order_by('-starts_on', '-created_at', '-pk').first()
    if current is not None:
        current.plan_steps = list(plan_steps(current).select_related('product')) if current.plan_key else []
        current.ends_on = max((step.expires_on for step in current.plan_steps), default=current.expires_on)
    return current


def plan_steps(authorization):
    """The steps of a starting plan that are still part of it, in order."""
    return TreatmentAuthorization.objects.filter(
        company_id=authorization.company_id, patient_id=authorization.patient_id, plan_key=authorization.plan_key,
    ).exclude(status=TreatmentAuthorization.Status.CANCELLED).order_by('plan_step', 'pk')


def ends_on(authorization):
    """When the prescription ends: a starting plan's last step, otherwise its own expiry."""
    return getattr(authorization, 'ends_on', None) or authorization.expires_on


def prescription_state(authorization, today=None):
    """none, paused, expired, due (valid but expiring within 30 days) or valid."""
    today = today or timezone.localdate()
    if authorization is None:
        return 'none'
    if authorization.status == TreatmentAuthorization.Status.PAUSED:
        return 'paused'
    if authorization.status == TreatmentAuthorization.Status.EXPIRED or ends_on(authorization) < today:
        return 'expired'
    if (ends_on(authorization) - today).days <= RENEWAL_DUE_DAYS:
        return 'due'
    return 'valid'


def _weeks(value):
    if type(value) is not int or value not in WEEK_CHOICES:
        raise ValidationError(f'Choose between {WEEK_CHOICES[0]} and {WEEK_CHOICES[-1]} weeks.')
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


CHANGED = 'Prescription changed. Dispensing review is required before dispatch.'


def _retire_parcels(parcels):
    """The undispatched parcels of a prescription being replaced: its drafts are cancelled, since the new prescription
    plans its own, and any already marked ready or held are held for dispensing review."""
    now = timezone.now()
    parcels.filter(status=Shipment.Status.DRAFT).update(
        status=Shipment.Status.CANCELLED, updated_at=now, hold_reason='Prescription changed. Replaced by the new prescription’s parcels.')
    parcels.filter(status__in=UNDISPATCHED).update(status=Shipment.Status.HELD, updated_at=now, hold_reason=CHANGED)


def _cancel_later_steps(company, patient, previous):
    """Replacing a step of a starting plan replaces the rest of it too: its later steps are cancelled with their
    parcels (see _retire_parcels)."""
    if not previous.plan_key:
        return
    later = list(TreatmentAuthorization.objects.select_for_update().filter(
        company=company, patient=patient, plan_key=previous.plan_key, plan_step__gt=previous.plan_step or 0,
    ).exclude(status=TreatmentAuthorization.Status.CANCELLED))
    if not later:
        return
    TreatmentAuthorization.objects.filter(pk__in=[step.pk for step in later]).update(
        status=TreatmentAuthorization.Status.CANCELLED, updated_at=timezone.now())
    _retire_parcels(Shipment.objects.select_for_update().for_company(company).filter(
        patient=patient, authorization__in=later, status__in=UNDISPATCHED,
    ))


def _supersede(company, patient, previous, replacement, *, hold, review_due_on=None):
    """Move care plans to the new prescription. A changed medicine or dose holds undispatched parcels."""
    previous.status = TreatmentAuthorization.Status.CANCELLED
    previous.save(update_fields=('status', 'updated_at'))
    plans = list(PatientSubscription.objects.select_for_update().for_company(company).filter(
        patient=patient, authorization=previous,
    ).exclude(status=PatientSubscription.Status.CANCELLED))
    for plan in plans:
        plan.authorization = replacement
        plan.review_due_on = review_due_on or replacement.expires_on
        plan.full_clean()
        plan.save(update_fields=('authorization', 'review_due_on', 'updated_at'))
    parcels = Shipment.objects.select_for_update().for_company(company).filter(
        Q(subscription_id__in=[plan.pk for plan in plans]) | Q(authorization=previous),
        patient=patient, status__in=UNDISPATCHED,
    )
    if hold:
        _retire_parcels(parcels.filter(authorization=previous))
        # Other parcels on the care plan, such as an add-on, are held for review.
        parcels.update(status=Shipment.Status.HELD, updated_at=timezone.now(), hold_reason=CHANGED)
    else:
        # Same medicine and dose: parcels carry on under the renewed prescription.
        parcels.filter(authorization=previous).update(authorization=replacement, updated_at=timezone.now())


@transaction.atomic
def renew_prescription(*, company, patient, actor, current, weeks, acknowledge_flags=False, request=None):
    company, patient = _lock_context(company, patient, actor)
    _require_doctor(company, actor)
    weeks = _weeks(weeks)
    previous = _locked_current(company, patient, current)
    if previous is None:
        raise ValidationError('There is no prescription to renew. Create one instead.')
    today = timezone.localdate()
    # Mid-way through a starting plan, the renewal extends its last step (the dose it settles on).
    last = plan_steps(previous).select_for_update().last() if previous.plan_key else None
    midway = last is not None and last.pk != previous.pk
    if midway:
        previous = TreatmentAuthorization.objects.select_related('product', 'dose__pen', 'prescribed_by').get(pk=last.pk)
    if not previous.product.is_active:
        raise ValidationError('This medicine is no longer in the catalogue. Change the prescription instead.')
    if previous.dose_id and not previous.dose.is_prescribable:
        raise ValidationError('This dose is no longer offered in the catalogue. Change the prescription instead.')
    flags = _check_flags(company, patient, acknowledge_flags)
    expires_on = add_weeks(max(previous.expires_on, today), weeks)
    renewal = TreatmentAuthorization(
        company=company, patient=patient, prescribed_by=actor, product=previous.product, dose=previous.dose,
        max_dose=previous.max_dose, quantity_per_cycle=previous.quantity_per_cycle,
        starts_on=previous.starts_on if midway else today, expires_on=expires_on,
        review_interval_days=previous.review_interval_days, instructions=previous.instructions,
        replaces=previous, plan_key=previous.plan_key if midway else None, plan_step=previous.plan_step if midway else None,
    )
    renewal.log_text = (f'Prescription renewed for {weeks_text(weeks)}: {prescription_label(renewal)}, '
                        f'valid to {date_format(expires_on, "j M Y")}.')
    renewal.full_clean()
    renewal.save()
    _supersede(company, patient, previous, renewal, hold=False)
    schedule_parcels(renewal)
    if midway:
        # Care plans stay on the step in effect; their review moves to the plan's new end.
        PatientSubscription.objects.for_company(company).filter(
            patient=patient, authorization__plan_key=renewal.plan_key,
        ).exclude(status=PatientSubscription.Status.CANCELLED).update(review_due_on=expires_on, updated_at=timezone.now())
    make_prescribing_doctor(company=company, patient=patient, doctor=actor, request=request)
    record_audit(company=company, actor=actor, patient=patient, action='prescription.renewed', target=renewal,
                 request=request, metadata={'previous_authorization_id': previous.pk, 'weeks': weeks,
                                            'safety_flags_acknowledged': bool(flags)})
    _event(renewal, 'Treatment authorisation renewed', 'Your doctor recorded a treatment decision. Open your treatment page to see its details.')
    _review_debit_orders(company, patient, changed=False)
    return renewal


@transaction.atomic
def change_prescription(*, company, patient, actor, current, dose, weeks, reason='', acknowledge_flags=False,
                        request=None):
    """Replace the current prescription from today with a dose from the catalogue, or create the first one."""
    company, patient = _lock_context(company, patient, actor)
    _require_doctor(company, actor)
    weeks = _weeks(weeks)
    previous = _locked_current(company, patient, current)
    if not isinstance(reason, str) or len(reason) > 500:
        raise ValidationError('Keep the reason to 500 characters or fewer.')
    reason = ' '.join(reason.split())
    allowed = {item.pk for doses in prescription_options(company, previous).values() for item in doses}
    if getattr(dose, 'pk', None) not in allowed:
        raise ValidationError('Choose a medicine and dose from this patient’s authorised medications.')
    dose = MedicationDose.objects.select_related('pen').get(pk=dose.pk, company=company)
    if previous is not None and previous.dose_id == dose.pk:
        raise ValidationError('That is the current prescription. Use Renew to extend it.')
    flags = _check_flags(company, patient, acknowledge_flags)
    today = timezone.localdate()
    expires_on = add_weeks(today, weeks)
    prescription = TreatmentAuthorization(
        company=company, patient=patient, prescribed_by=actor, product=dose.pen, dose=dose, max_dose=dose.label,
        quantity_per_cycle=dose.pens_per_dispatch, starts_on=today, expires_on=expires_on,
        review_interval_days=previous.review_interval_days if previous else 180,
        replaces=previous, change_reason=reason,
    )
    valid_to = f'{weeks_text(weeks)}, valid to {date_format(expires_on, "j M Y")}.'
    if previous is not None:
        prescription.log_text = (f'{reason.rstrip(".")}. ' if reason else '') + (
            f'Prescription changed from {prescription_label(previous)} to {prescription_label(prescription)}, {valid_to}')
    else:
        prescription.log_text = (f'{reason.rstrip(".")}. ' if reason else '') + (
            f'Prescription created: {prescription_label(prescription)}, {valid_to}')
    prescription.full_clean()
    prescription.save()
    if previous is not None:
        _cancel_later_steps(company, patient, previous)
        _supersede(company, patient, previous, prescription, hold=True)
    schedule_parcels(prescription)
    make_prescribing_doctor(company=company, patient=patient, doctor=actor, request=request)
    record_audit(company=company, actor=actor, patient=patient,
                 action='prescription.changed' if previous else 'prescription.created', target=prescription,
                 request=request, metadata={'previous_authorization_id': previous.pk if previous else None,
                                            'weeks': weeks, 'safety_flags_acknowledged': bool(flags)})
    _event(prescription, 'Treatment authorised', 'Your doctor recorded a treatment decision. Open your treatment page to see its details.')
    _review_debit_orders(company, patient, changed=True)
    return prescription


def _review_debit_orders(company, patient, *, changed):
    """A new or changed prescription (changed), or one that now costs more, asks the patient to approve a new debit
    order (care.billing.review_mandates, as Practice settings say)."""
    from .billing import review_mandates

    review_mandates(company, patient, prescription_changed=changed)


def renewal_expiry(authorization, weeks, today=None):
    today = today or timezone.localdate()
    return add_weeks(max(ends_on(authorization), today), weeks)


def expiry_preview(weeks, today=None):
    return add_weeks(today or timezone.localdate(), weeks)
