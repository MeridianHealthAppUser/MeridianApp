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

from .demo_safety import require_same_demo_data

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, Max, OuterRef, Q
from django.utils import timezone
from django.utils.formats import date_format

from .clinical import _lock_context, _require_doctor, is_super_admin, renewing_doctor
from .models import (
    MedicationDose, MedicationProduct, PatientEvent, PatientSubscription, Shipment, SubscriptionCycle, TreatmentAuthorization,
)
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


def _main_prescriptions(company, patient):
    return TreatmentAuthorization.objects.for_company(company).filter(
        patient=patient, product__company=company,
        product__category=MedicationProduct.Category.WEIGHT_MANAGEMENT, product__requires_authorisation=True)


def revoked_prescription(company, patient):
    """The main prescription a doctor or the Super Admin revoked, when nothing has been prescribed since (James,
    26 Nov 2026), with revoked_on and revoked_by from the audit trail. The Overview shows it as revoked, and there is
    no current prescription until a new one is written. None otherwise."""
    from .models import AuditEvent

    event = AuditEvent.objects.for_company(company).filter(
        patient=patient, action='treatment.revoke', target_type='care.treatmentauthorization',
    ).select_related('actor').order_by('-created_at', '-pk').first()
    if event is None or not event.target_id.isdigit():
        return None
    revoked = _main_prescriptions(company, patient).select_related('product', 'dose__pen', 'prescribed_by').filter(
        pk=int(event.target_id)).first()
    if revoked is None or _main_prescriptions(company, patient).filter(created_at__gt=event.created_at).exists():
        return None
    revoked.revoked_on, revoked.revoked_by = timezone.localdate(event.created_at), event.actor
    revoked.plan_steps, revoked.ends_on = [], revoked.expires_on
    return revoked


def current_prescription(company, patient):
    """The latest main prescription that hasn't been replaced or revoked. In a starting plan, the step in effect
    (a later step counts once it starts), with plan_steps (all its steps, in order) and ends_on (the last step's end).
    None after a revoke, until a new one is written (an earlier plan step still marked active doesn't come back)."""
    if revoked_prescription(company, patient) is not None:
        return None
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
    # A doctor renews a prescription, and so does the practice's Super Admin, doctor or not (James, 26 Nov 2026).
    if not is_super_admin(company, actor):
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
    require_same_demo_data(patient, previous.product)
    if not previous.product.is_active:
        raise ValidationError('This medicine is no longer in the catalogue. Change the prescription instead.')
    if previous.dose_id and not previous.dose.is_prescribable:
        raise ValidationError('This dose is no longer offered in the catalogue. Change the prescription instead.')
    flags = _check_flags(company, patient, acknowledge_flags)
    expires_on = add_weeks(max(previous.expires_on, today), weeks)
    # A Super Admin who isn't a doctor renews it for the doctor who prescribed it (James, 26 Nov 2026).
    doctor = renewing_doctor(company, actor, previous)
    renewal = TreatmentAuthorization(
        company=company, patient=patient, prescribed_by=doctor, product=previous.product, dose=previous.dose,
        max_dose=previous.max_dose, quantity_per_cycle=previous.quantity_per_cycle,
        starts_on=previous.starts_on if midway else today, expires_on=expires_on,
        review_interval_days=previous.review_interval_days, instructions=previous.instructions,
        replaces=previous, plan_key=previous.plan_key if midway else None, plan_step=previous.plan_step if midway else None,
    )
    renewal.log_text = (f'Prescription renewed for {weeks_text(weeks)}: {prescription_label(renewal)}, '
                        f'valid to {date_format(expires_on, "j M Y")}.'
                        + (f' Renewed by {actor.titled_name} (Super Admin).' if doctor != actor else ''))
    renewal.full_clean()
    renewal.save()
    _supersede(company, patient, previous, renewal, hold=False)
    schedule_parcels(renewal)
    if midway:
        # Care plans stay on the step in effect; their review moves to the plan's new end.
        PatientSubscription.objects.for_company(company).filter(
            patient=patient, authorization__plan_key=renewal.plan_key,
        ).exclude(status=PatientSubscription.Status.CANCELLED).update(review_due_on=expires_on, updated_at=timezone.now())
    make_prescribing_doctor(company=company, patient=patient, doctor=doctor, request=request)
    record_audit(company=company, actor=actor, patient=patient, action='prescription.renewed', target=renewal,
                 request=request, metadata={'previous_authorization_id': previous.pk, 'weeks': weeks,
                                            'safety_flags_acknowledged': bool(flags)})
    _event(renewal, 'Treatment authorisation renewed', 'Your doctor recorded a treatment decision. Open your treatment page to see its details.')
    _review_debit_orders(company, patient, changed=False, prescription=renewal)
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
    require_same_demo_data(patient, dose.pen)
    if previous is not None and previous.dose_id == dose.pk:
        raise ValidationError('That is the current prescription. Use Renew to extend it.')
    flags = _check_flags(company, patient, acknowledge_flags)
    today = timezone.localdate()
    # Valid from today; its weeks run from its first pens.
    expires_on = add_weeks(weeks_from(company, today), weeks)
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
    _review_debit_orders(company, patient, changed=True, prescription=prescription)
    return prescription


def _review_debit_orders(company, patient, *, changed, prescription=None):
    """A new or changed prescription (changed), or one that now costs more, asks the patient to approve a new debit
    order (care.billing.review_mandates, as Practice settings say). A new dose's first parcel is paid for by the period
    covering it, which then runs the new dose's billing interval from that parcel (care.billing.refit_current_period),
    so payments stay in step with its parcels. One made after billing stopped at the end of the last prescription
    starts payments again today, with a parcel on the first shipping day whose order cut-off is still to come
    (care.parcels.schedule_parcels). When the payment days or how often they come change, the prescription's line in
    the clinical log says so, and so does the patient's list of prescription changes on My Treatment."""
    from .billing import (
        BILLABLE, billing_weeks, next_payment_on, period_start, refit_current_period, resume_debit_date, review_mandates,
    )

    today = timezone.localdate()
    first_parcel = None
    interval_changed = False
    if prescription is not None and changed:
        first_parcel = Shipment.objects.filter(authorization=prescription, status__in=UNDISPATCHED).order_by(
            'scheduled_for').values_list('scheduled_for', flat=True).first()
        before = prescription.replaces if prescription.replaces_id else None
        interval_changed = bool(before is not None and before.dose_id and prescription.dose_id
                                and (before.dose.billing_weeks or 4) != (prescription.dose.billing_weeks or 4))
    for plan in PatientSubscription.objects.filter(company=company, patient=patient).exclude(
            status=PatientSubscription.Status.CANCELLED):
        moved = refit_current_period(plan, first_parcel) is not None
        resumed = False
        if plan.status in BILLABLE and plan.next_debit_on and plan.next_debit_on < today and not plan.cycles.filter(
                status__in=(SubscriptionCycle.Status.PENDING, SubscriptionCycle.Status.FAILED)).exists():
            # No period running and none waiting to be paid: billing stopped when the last prescription ended.
            day = resume_debit_date(plan)
            if day is not None and day != plan.next_debit_on:
                plan.next_debit_on = day
                plan.save(update_fields=('next_debit_on', 'updated_at'))
                resumed = True
        if prescription is not None and plan.next_debit_on and (moved or resumed or interval_changed):
            weeks = billing_weeks(plan, period_start(plan, plan.next_debit_on))
            _note_payment_days(plan, prescription, resumed=resumed, weeks=weeks, day=next_payment_on(plan))
    review_mandates(company, patient, prescription_changed=changed)


def _note_payment_days(plan, prescription, *, resumed, weeks, day):
    """Say when payments now come: on the prescription's line in the doctor's clinical log, and in the patient's
    prescription changes on My Treatment (portal.patient_summary.prescription_changes; James, 25 Nov 2026)."""
    when = f'the next on {date_format(day, "j M Y")}' if day else 'none after this one: the prescription ends first'
    line = f'Payments {"start again" if resumed else "now"}: every {weeks_text(weeks)}, {when}.'
    prescription.log_text = f'{prescription.log_text} {line}'.strip()
    TreatmentAuthorization.objects.filter(pk=prescription.pk).update(log_text=prescription.log_text)
    if resumed:
        title, detail = 'Your payments carry on', f'With your new prescription, payments carry on every {weeks_text(weeks)}.'
    else:
        title, detail = 'Your payment days changed', (
            f'With your new dose, payments are now every {weeks_text(weeks)}, in step with your pens.')
    detail += f' The next is on {date_format(day, "D, j M Y")}.' if day else ' There are none after this one.'
    PatientEvent.objects.create(company_id=plan.company_id, patient_id=plan.patient_id, category=PatientEvent.Category.PAYMENT,
                                title=title, detail=detail, source_type=prescription._meta.label_lower,
                                source_id=str(prescription.pk))


def renewal_expiry(authorization, weeks, today=None):
    today = today or timezone.localdate()
    return add_weeks(max(ends_on(authorization), today), weeks)


def weeks_from(company, today=None):
    """When a prescription written today starts its weeks: with its first pens, on the first shipping day whose order
    cut-off is on or after today (care.parcels.first_shipping_day). It is valid from today; its weeks of pens run from
    then, so "12 weeks" is twelve weeks of pens (25 Nov 2026)."""
    from .parcels import first_shipping_day

    return first_shipping_day(today or timezone.localdate(), company)


def expiry_preview(weeks, today=None, company=None):
    today = today or timezone.localdate()
    return add_weeks(weeks_from(company, today) if company is not None else today, weeks)


# Treatment starts on the first payment, and an old prescription needs the doctor to confirm it

SENT = (Shipment.Status.DISPATCHED, Shipment.Status.DELIVERED)
MOVED_PARCEL = 'Treatment start moved. Planned again from the new start.'
CONFIRM_TASK = 'Confirm prescription:'
# A parcel that never went out, kept on record (cancelled) for the patient's timeline: "Missed: <why, in their words>".
MISSED = 'Missed: '


def course_steps(authorization):
    """The prescription a care plan is on, as the steps it runs through: every step of a starting plan, in order, or the
    prescription alone. Always read afresh, since moving a course changes its dates."""
    if authorization is None:
        return []
    if not authorization.plan_key:
        return list(TreatmentAuthorization.objects.filter(pk=authorization.pk).select_related('dose', 'prescribed_by'))
    return list(TreatmentAuthorization.objects.filter(
        company_id=authorization.company_id, patient_id=authorization.patient_id, plan_key=authorization.plan_key,
    ).exclude(status=TreatmentAuthorization.Status.CANCELLED).order_by('plan_step', 'pk'))


def first_period_days(steps):
    """How long the first payment period of a course is: its first dose's billing interval (4 or 5 weeks)."""
    dose = steps[0].dose if steps and steps[0].dose_id else None
    return 7 * (dose.billing_weeks if dose is not None and dose.billing_weeks else 4)


def _under_way(steps):
    """A parcel of this course, or of a prescription it renews, has gone out: the patient has started treatment."""
    from .operations import dose_run

    earlier = {run.pk for step in steps for run in dose_run(step)}
    return Shipment.objects.filter(authorization_id__in=earlier, status__in=SENT).exists()


def moved_note(authorization):
    """{'from': the end as the doctor wrote it, 'because': why it moved} when the course has moved, else None."""
    steps = course_steps(authorization)
    if not steps or not steps[0].ends_as_written:
        return None
    ends = max(step.expires_on for step in steps)
    if ends == steps[0].ends_as_written:
        return None
    return {'from': steps[0].ends_as_written, 'because': steps[0].moved_because}


def awaiting_confirmation(authorization):
    """The doctor must confirm this prescription before anything more happens: nothing is charged or sent until then."""
    steps = course_steps(authorization)
    return bool(steps) and steps[0].needs_confirmation


def confirmation_hold_reason(authorization):
    steps = course_steps(authorization)
    if not steps or not steps[0].needs_confirmation:
        return ''
    doctor = steps[0].prescribed_by.titled_name if steps[0].prescribed_by_id else 'the doctor'
    return f'Waiting for {doctor} to confirm the prescription before the first parcel goes out.'


def start_check(authorization, day=None):
    """Whether the patient can start their subscription on this prescription themselves. Not when it was written more
    than a first payment period ago and they haven't started treatment: the doctor confirms it first (and may ask for
    blood tests and change it). None when they can start, or {'doctor', 'written_on', 'weeks', 'asked'}."""
    steps = course_steps(authorization)
    if not steps:
        return None
    day = day or timezone.localdate()
    started = min(step.starts_on for step in steps)
    too_old = (day - started).days > first_period_days(steps) and not _under_way(steps)
    if not (too_old or steps[0].needs_confirmation):
        return None
    return {'doctor': steps[0].prescribed_by if steps[0].prescribed_by_id else None, 'written_on': started,
            'weeks': first_period_days(steps) // 7, 'asked': steps[0].needs_confirmation}


def _confirmation_task(steps, patient, description):
    """A task for the prescribing doctor to confirm the prescription, unless one is open already."""
    from .models import ClinicalTask

    title = f'{CONFIRM_TASK} {patient.first_name} {patient.last_name}'.strip()[:255]
    open_states = (ClinicalTask.Status.OPEN, ClinicalTask.Status.IN_PROGRESS)
    if ClinicalTask.objects.filter(company_id=patient.company_id, patient=patient, title__startswith=CONFIRM_TASK,
                                   status__in=open_states).exists():
        return None
    task = ClinicalTask(company_id=patient.company_id, patient=patient, title=title, description=description,
                        assigned_to_id=steps[0].prescribed_by_id, priority=ClinicalTask.Priority.HIGH)
    task.full_clean()
    task.save()
    return task


def _move_course(steps, subscription, day, why, because, *, valid_from=None):
    """Move the course so its weeks start on day (a shipping day: its first pens), as long as nothing has been locked
    for shipping or sent. A course's weeks run from the first shipping day whose order cut-off was on or after it
    started (weeks_from), so it moves by whole weeks, and not at all when day is that shipping day already (paid the
    day it was written, or before the same cut-off). Each step keeps its length (so it ends that much later), the care
    plan's review date moves with it, and the parcels are planned again from the new start. valid_from, when given
    (the first payment), is when the first step takes effect. why ends the clinical log line; the end as written is
    kept for the "Moved from" note and the patient is told. Returns the days moved (0 if none)."""
    started = min(step.starts_on for step in steps)
    gap = (day - weeks_from(steps[0].company_id, started)).days
    if gap <= 0:
        return 0
    parcels = Shipment.objects.select_for_update().filter(
        authorization__in=steps, company_id=steps[0].company_id, patient_id=steps[0].patient_id)
    if parcels.filter(status=Shipment.Status.READY, locked_at__isnull=False).exists() or _under_way(steps):
        return 0
    shift = timedelta(days=gap)
    ends_before = max(step.expires_on for step in steps)
    for step in steps:
        # Only the dates change, so nothing else about the prescription is checked again.
        starts = step.starts_on + shift
        if step.pk == steps[0].pk and valid_from is not None:
            starts = valid_from  # Valid from the payment (or the doctor's confirmation), before its first pens.
        TreatmentAuthorization.objects.filter(pk=step.pk).update(
            starts_on=starts, expires_on=step.expires_on + shift, updated_at=timezone.now())
    if subscription is not None and subscription.review_due_on:
        subscription.review_due_on += shift
        subscription.save(update_fields=('review_due_on', 'updated_at'))
    # The parcels it had planned are planned again from the new start. One that lands on the same day for the same
    # step is kept as it was (a course paid for the day it was written already had its parcels from its first pens),
    # so a parcel marked ready stays ready.
    before = {(parcel.authorization_id, parcel.scheduled_for): parcel
              for parcel in parcels.filter(status__in=UNDISPATCHED).order_by('pk')}
    first = steps[0]
    if before:
        parcels.filter(pk__in=[parcel.pk for parcel in before.values()]).update(
            status=Shipment.Status.CANCELLED, hold_reason=MOVED_PARCEL, updated_at=timezone.now())
        for step in course_steps(TreatmentAuthorization.objects.get(pk=first.pk)):
            for parcel in schedule_parcels(step):
                kept = before.get((parcel.authorization_id, parcel.scheduled_for))
                if kept is not None:
                    parcel.delete()
                    Shipment.objects.filter(pk=kept.pk).update(status=kept.status, hold_reason=kept.hold_reason,
                                                               updated_at=timezone.now())
    ends = ends_before + shift
    first.refresh_from_db()
    written = first.log_text or f'Prescription recorded: {prescription_label(first)}, valid to {ends_before:%-d %b %Y}.'
    TreatmentAuthorization.objects.filter(pk=first.pk).update(
        ends_as_written=first.ends_as_written or ends_before, moved_because=because, log_text=(
            f'{written.rstrip()} Start moved from {date_format(started, "j M Y")} to {date_format(day, "j M Y")}, {why}: '
            f'each step {gap} day{"s" if gap != 1 else ""} later, valid to {date_format(ends, "j M Y")}.'))
    record_audit(company=first.company, actor=None, patient=first.patient, action='prescription.start_moved', target=first,
                 metadata={'from': started.isoformat(), 'to': day.isoformat(), 'days': gap, 'ends_on': ends.isoformat()})
    _event(first, 'Treatment start moved',
           f'Your treatment now starts with your first pens on {date_format(day, "D, j M")} and runs to '
           f'{date_format(ends, "D, j M Y")}.')
    return gap


def carry_on_from(subscription, day, *, missed, because):
    """Treatment carries on from day after pens didn't go out: a payment that didn't go through and was paid late, or
    the doctor confirming the prescription first (22 Nov 2026). The first parcel that should have gone out (one whose
    order cut-off had passed by day) is kept on record as missed (missed: why, in the patient's words), a parcel with
    the same pens goes out on the first shipping day whose cut-off is on or after day (care.parcels.first_shipping_day),
    and every later parcel moves by the same weeks, as do the prescription's later steps and its end, so each dose runs
    its full weeks from when its pens arrive. Returns the days moved: 0 when nothing was waiting."""
    from .parcels import first_shipping_day

    steps = course_steps(subscription.authorization)
    if not steps:
        return 0
    parcels = list(Shipment.objects.select_for_update().filter(
        authorization__in=steps, company_id=subscription.company_id, patient_id=subscription.patient_id,
        status__in=UNDISPATCHED).prefetch_related('items').order_by('scheduled_for', 'pk'))
    new_day = first_shipping_day(day, subscription.company_id)
    waiting = [parcel for parcel in parcels if parcel.scheduled_for < new_day]
    if not waiting:
        return 0
    first_missed = waiting[0]
    shift = new_day - first_missed.scheduled_for
    if shift.days <= 0:
        return 0
    now = timezone.now()
    # The pens that didn't go out go out now, in a parcel of their own; the missed one stays on record.
    replacement = Shipment.objects.create(
        company_id=first_missed.company_id, patient_id=first_missed.patient_id, subscription=subscription,
        authorization=first_missed.authorization, cycle_number=first_missed.cycle_number, scheduled_for=new_day,
        status=Shipment.Status.DRAFT)
    for item in first_missed.items.all():
        item.pk, item.shipment = None, replacement
        item.save()
    Shipment.objects.filter(pk=first_missed.pk).update(status=Shipment.Status.CANCELLED, hold_reason=f'{MISSED}{missed}',
                                                       locked_at=None, updated_at=now)
    # Everything after it carries on the same weeks later; holds for payment (or the doctor) are checked again.
    for parcel in parcels[1:]:
        released = parcel.status == Shipment.Status.HELD and (parcel.hold_reason or '').startswith(('Not paid', 'Waiting for'))
        Shipment.objects.filter(pk=parcel.pk).update(
            scheduled_for=parcel.scheduled_for + shift, locked_at=None, updated_at=now,
            **({'status': Shipment.Status.DRAFT, 'hold_reason': ''} if released or parcel.status == Shipment.Status.READY else {}))
    ends_before = max(step.expires_on for step in steps)
    for step in steps:
        if step.starts_on > first_missed.scheduled_for:
            TreatmentAuthorization.objects.filter(pk=step.pk).update(
                starts_on=step.starts_on + shift, expires_on=step.expires_on + shift, updated_at=now)
        elif step.expires_on >= first_missed.scheduled_for:
            TreatmentAuthorization.objects.filter(pk=step.pk).update(expires_on=step.expires_on + shift, updated_at=now)
    if subscription.review_due_on:
        subscription.review_due_on += shift
        subscription.save(update_fields=('review_due_on', 'updated_at'))
    ends = ends_before + shift
    first = TreatmentAuthorization.objects.get(pk=steps[0].pk)
    weeks = shift.days // 7
    written = first.log_text or f'Prescription recorded: {prescription_label(first)}, valid to {ends_before:%-d %b %Y}.'
    TreatmentAuthorization.objects.filter(pk=first.pk).update(
        ends_as_written=first.ends_as_written or ends_before, moved_because=because, log_text=(
            f'{written.rstrip()} Carried on {weeks} week{"s" if weeks != 1 else ""} later {because} '
            f'({date_format(day, "j M Y")}): the parcel of {date_format(first_missed.scheduled_for, "j M")} goes out '
            f'{date_format(new_day, "j M")}, valid to {date_format(ends, "j M Y")}.'))
    record_audit(company=first.company, actor=None, patient=first.patient, action='prescription.course_moved', target=first,
                 metadata={'missed': first_missed.scheduled_for.isoformat(), 'sent': new_day.isoformat(),
                           'days': shift.days, 'ends_on': ends.isoformat(), 'because': because})
    _event(first, 'Your treatment carries on',
           f'Your next pens go out on {date_format(new_day, "D, j M")}, and your prescription now runs to '
           f'{date_format(ends, "D, j M Y")}.')
    return shift.days


# The "Moved from" note (moved_note) when the practice moved a parcel and everything after it.
PARCEL_MOVED = 'when the practice moved a parcel'


def course_parcels(parcels):
    """Of parcels, the ones a prescription planned (care.parcels.schedule_parcels): with pens (a medicine that needs a
    prescription), and not a shop order (care.pharmacy), which carries a prescription for an item that needs one."""
    from .models import PharmacyOrder, ShipmentItem

    pen = ShipmentItem.objects.filter(shipment=OuterRef('pk'), product__requires_authorisation=True)
    return parcels.filter(authorization__isnull=False).filter(Exists(pen)).exclude(
        Exists(PharmacyOrder.objects.filter(shipment=OuterRef('pk'))))


def moves_with_later(parcel):
    """Whether a parcel can take everything after it along when it moves: one its prescription planned."""
    return bool(parcel.authorization_id) and course_parcels(Shipment.objects.filter(pk=parcel.pk)).exists()


def move_with_later_parcels(parcel, day, *, actor, request=None):
    """This parcel and everything after it (James, 25 Nov 2026): the practice moves a prescription parcel on its
    shipment page (care.operations.change_parcel moves the parcel itself) and the rest of the treatment moves with it
    by the same whole weeks: the patient's later parcels, the payment periods from it on and the next payment (still a
    week before its parcel, and never before today), the end of the prescription it is under and of any later one or
    plan step (so each dose still runs its full weeks from its pens), and the care plan's review date. Its new day
    must come after the parcel before it and make an order cut-off still to come (care.parcels.first_shipping_day),
    and no later parcel may be locked for shipping or sent. The clinical log notes it and the patient has a
    Prescription changes card. Returns None when the day is the same, else {'weeks' (negative when earlier),
    'parcels' (later parcels moved), 'next_payment', 'ends_on'}."""
    from .billing import _cycle_end, next_payment_on
    from .parcels import PAYMENT_LEAD, first_shipping_day, order_cutoff_text

    old = parcel.scheduled_for
    shift = day - old
    if not moves_with_later(parcel):
        raise ValidationError('Only a prescription parcel can move the parcels and payments after it.')
    if not shift:
        return None
    if shift.days % 7:
        raise ValidationError(f'To move the parcels after it too, move this one by whole weeks: choose another {old:%A}.')
    today = timezone.localdate()
    company_id, patient_id = parcel.company_id, parcel.patient_id
    parcels = course_parcels(Shipment.objects.select_for_update(of=('self',)).filter(
        company_id=company_id, patient_id=patient_id)).exclude(pk=parcel.pk).exclude(status=Shipment.Status.CANCELLED)
    before = parcels.filter(scheduled_for__lt=old).order_by('-scheduled_for', '-pk').values_list('scheduled_for', flat=True).first()
    if before is not None and day <= before:
        raise ValidationError(f'Choose a day after the parcel before it, on {date_format(before, "D, j M")}.')
    earliest = first_shipping_day(today, company_id)
    if day < earliest:
        raise ValidationError(
            f'Pens for {date_format(day, "D, j M")} were ordered at its order cut-off, {order_cutoff_text(day, company_id)}. '
            f'Choose {date_format(earliest, "D, j M")} or later, '
            'or move only this parcel.')
    later = list(parcels.filter(scheduled_for__gte=old).order_by('scheduled_for', 'pk'))
    for other in later:
        if other.status not in UNDISPATCHED:
            raise ValidationError(f'The parcel of {date_format(other.scheduled_for, "D, j M")} has gone out. Move only this parcel.')
        if other.locked_at:
            raise ValidationError(f'The parcel of {date_format(other.scheduled_for, "D, j M")} is locked for shipping. '
                                  'Hold it first to unlock it, or move only this parcel.')
    now = timezone.now()
    # A later parcel marked ready goes back to draft, to be checked again, as this one does.
    for other in later:
        Shipment.objects.filter(pk=other.pk).update(
            scheduled_for=other.scheduled_for + shift, updated_at=now,
            **({'status': Shipment.Status.DRAFT} if other.status == Shipment.Status.READY else {}))

    # Every prescription still running the day before the parcel: one that starts on its day or later (a plan's later
    # step, a renewal) moves whole; the one before it or that it falls in keeps its start (it is valid now) and ends
    # that much sooner or later, so a plan's steps still meet.
    prescriptions = list(TreatmentAuthorization.objects.select_for_update().filter(
        company_id=company_id, patient_id=patient_id, expires_on__gte=old - timedelta(days=1),
    ).exclude(status__in=(TreatmentAuthorization.Status.CANCELLED, TreatmentAuthorization.Status.EXPIRED)).order_by('starts_on', 'pk'))
    courses = {}
    for prescription in prescriptions:
        steps = course_steps(prescription)
        courses.setdefault(steps[0].pk, max(step.expires_on for step in steps))
    ends = None
    for prescription in prescriptions:
        moves_whole = prescription.starts_on >= old and prescription.starts_on > today
        starts = prescription.starts_on + shift if moves_whole else prescription.starts_on
        expires = max(starts, prescription.expires_on + shift)
        TreatmentAuthorization.objects.filter(pk=prescription.pk).update(starts_on=starts, expires_on=expires, updated_at=now)
        ends = max(ends, expires) if ends else expires

    # The payment periods from the parcel on move with it (the one before it ends with it), and the next payment stays
    # a week before its parcel.
    plan = parcel.subscription if parcel.subscription_id else PatientSubscription.objects.filter(
        company_id=company_id, patient_id=patient_id).exclude(status=PatientSubscription.Status.CANCELLED).order_by('-created_at', '-pk').first()
    next_payment = None
    if plan is not None:
        plan = PatientSubscription.objects.select_for_update().get(pk=plan.pk)
        for cycle in plan.cycles.select_for_update().exclude(status=SubscriptionCycle.Status.CANCELLED):
            end = _cycle_end(cycle)
            if cycle.starts_on >= old:
                cycle.starts_on, cycle.ends_on = cycle.starts_on + shift, end + shift
            elif end >= old - timedelta(days=1):
                cycle.ends_on = end + shift
            else:
                continue
            cycle.save(update_fields=('starts_on', 'ends_on', 'updated_at'))
        fields = []
        if plan.next_debit_on and plan.next_debit_on + PAYMENT_LEAD >= old:
            plan.next_debit_on = max(today, plan.next_debit_on + shift)
            fields.append('next_debit_on')
        if plan.review_due_on and plan.review_due_on >= old - timedelta(days=1):
            plan.review_due_on += shift
            fields.append('review_due_on')
        if fields:
            plan.save(update_fields=(*fields, 'updated_at'))
        next_payment = next_payment_on(plan)

    weeks = shift.days // 7
    way = f'{"a week" if abs(weeks) == 1 else weeks_text(abs(weeks))} {"earlier" if weeks < 0 else "later"}'
    name = getattr(actor, 'titled_name', '') or 'the practice'
    for pk, ends_before in courses.items():
        first = TreatmentAuthorization.objects.get(pk=pk)
        course_ends = max(step.expires_on for step in course_steps(first))
        written = first.log_text or f'Prescription recorded: {prescription_label(first)}, valid to {ends_before:%-d %b %Y}.'
        TreatmentAuthorization.objects.filter(pk=pk).update(
            ends_as_written=first.ends_as_written or ends_before, moved_because=PARCEL_MOVED, log_text=(
                f'{written.rstrip()} Parcel of {date_format(old, "j M Y")} moved to {date_format(day, "j M Y")} by {name}, '
                f'with the parcels after it: {way}, valid to {date_format(course_ends, "j M Y")}.'))
    record_audit(company=parcel.company, actor=actor, patient=parcel.patient, action='shipment.moved_with_later',
                 target=parcel, request=request, metadata={
                     'from': old.isoformat(), 'to': day.isoformat(), 'days': shift.days, 'parcels': len(later),
                     'next_payment': next_payment.isoformat() if next_payment else None,
                     'ends_on': ends.isoformat() if ends else None})
    text = f'Your parcel of {date_format(old, "D, j M")} now goes out on {date_format(day, "D, j M")}'
    text += f', and your parcels after it {way} too.' if later else '.'
    after = [f'your next payment is on {date_format(next_payment, "D, j M")}'] if next_payment else []
    after += [f'your prescription now runs to {date_format(ends, "D, j M Y")}'] if ends else []
    if after:
        sentence = ', and '.join(after)
        text += f' {sentence[0].upper()}{sentence[1:]}.'
    _event(parcel.authorization, 'Parcels moved', text)
    return {'weeks': weeks, 'parcels': len(later), 'next_payment': next_payment, 'ends_on': ends}


def start_course_on(subscription, day):
    """Treatment starts on the first payment (22 Nov 2026), with its first pens (25 Nov 2026). A prescription is dated
    from the day the doctor writes it, but the patient may only start their subscription and pay days or weeks later,
    and their pens go out on the first shipping day whose order cut-off is on or after the payment
    (care.parcels.first_shipping_day). The course then moves forward to start that shipping day (see _move_course), so
    week 1 and each dose's weeks start with their pens, while the prescription is valid from the payment; what came
    before the pens (the prescription, the payment) is week 0.
    When that payment came more than a first payment period (4 or 5 weeks) after the prescription was written, the
    doctor must confirm it before the first parcel goes out, and nothing more is charged until they do. Returns the
    days moved."""
    from .parcels import first_shipping_day

    steps = course_steps(subscription.authorization)
    if not steps or any(step.status != TreatmentAuthorization.Status.ACTIVE for step in steps):
        return 0
    started = min(step.starts_on for step in steps)
    late = (day - started).days > first_period_days(steps)
    first_parcel = first_shipping_day(day, subscription.company_id)
    gap = _move_course(steps, subscription, first_parcel, f'the day of its first parcel, after the first payment on {date_format(day, "j M Y")}',
                       'to start with the first pens', valid_from=day)
    if gap and late:
        patient = steps[0].patient
        TreatmentAuthorization.objects.filter(pk=steps[0].pk).update(needs_confirmation=True)
        weeks = (day - started).days // 7
        _confirmation_task(steps, patient, (
            f'{patient.first_name} first paid on {date_format(day, "j M Y")}, {weeks} weeks after the prescription was '
            f'written on {date_format(started, "j M Y")}. Check it is still right (ask for blood tests and change it if '
            'needed), then confirm it on their record. Their first parcel and further payments wait until you do.'))
        record_audit(company=steps[0].company, actor=None, patient=patient, action='prescription.confirmation_needed',
                     target=steps[0], metadata={'weeks': weeks, 'why': 'late_first_payment'})
        _event(steps[0], 'Your doctor is checking your prescription',
               'It has been a while since it was written, so your doctor will confirm it before your first parcel goes '
               'out. We will let you know.')
    return gap


@transaction.atomic
def ask_to_confirm(*, company, patient, actor, authorization, request=None):
    """The patient asks the doctor to confirm a prescription written too long ago to start on by themselves."""
    if patient.user_id != actor.pk or not getattr(actor, 'is_active', False):
        raise ValidationError('Only the patient can ask about their own prescription.')
    steps = course_steps(authorization)
    check = start_check(authorization)
    if not steps or check is None or steps[0].company_id != company.pk or steps[0].patient_id != patient.pk:
        raise ValidationError('Your prescription doesn’t need your doctor to confirm it. You can start your subscription.')
    if not check['asked']:
        TreatmentAuthorization.objects.filter(pk=steps[0].pk).update(needs_confirmation=True)
        _confirmation_task(steps, patient, (
            f'{patient.first_name} wants to start their subscription, but the prescription was written on '
            f'{date_format(check["written_on"], "j M Y")}, more than {check["weeks"]} weeks ago. Check it is still right '
            '(ask for blood tests and change it if needed), then confirm it on their record.'))
        record_audit(company=company, actor=actor, patient=patient, action='prescription.confirmation_needed',
                     target=steps[0], request=request, metadata={'why': 'asked_to_start'})
    return steps[0]


@transaction.atomic
def confirm_prescription(*, company, patient, actor, authorization, request=None):
    """The doctor confirms an old prescription is still right. The course starts again from today (and a payment period
    already paid for from its first parcel, on the first shipping day whose order cut-off is still to come), so the
    patient gets every week they paid for; the patient can start, or their parcels and payments carry on. Changing the prescription instead also
    does: the new one needs no confirmation."""
    from .models import ClinicalTask

    company, patient = _lock_context(company, patient, actor)
    _require_doctor(company, actor)
    steps = course_steps(authorization)
    if not steps or steps[0].patient_id != patient.pk or not steps[0].needs_confirmation:
        raise ValidationError('This prescription doesn’t need confirming.')
    today = timezone.localdate()
    subscription = PatientSubscription.objects.select_for_update().filter(
        company=company, patient=patient, authorization__in=steps).exclude(status=PatientSubscription.Status.CANCELLED).first()
    TreatmentAuthorization.objects.filter(pk=steps[0].pk).update(needs_confirmation=False)
    because = f'when {actor.titled_name} confirmed it'
    from .parcels import first_shipping_day

    first_parcel = first_shipping_day(today, company)
    if subscription is not None and _under_way(steps):
        # Treatment under way (a late payment after a long gap): the period paid for, and the parcels, start today.
        from .billing import restart_period

        latest = subscription.cycles.exclude(status__in=('cancelled',)).order_by('-cycle_number').first()
        if latest is not None and latest.status == 'paid':
            restart_period(latest, first_parcel)
        carry_on_from(subscription, today, missed='waiting for your doctor', because=because)
    else:
        if subscription is not None:
            from .billing import restart_first_period

            restart_first_period(subscription, first_parcel)
        _move_course(steps, subscription, first_parcel, f'the day of its first parcel, after {actor.titled_name} confirmed it', because,
                     valid_from=today)
    ClinicalTask.objects.filter(company=company, patient=patient, title__startswith=CONFIRM_TASK,
                                status__in=(ClinicalTask.Status.OPEN, ClinicalTask.Status.IN_PROGRESS)).update(
        status=ClinicalTask.Status.DONE, completed_at=timezone.now(), updated_at=timezone.now())
    record_audit(company=company, actor=actor, patient=patient, action='prescription.confirmed', target=steps[0],
                 request=request)
    _event(steps[0], 'Prescription confirmed', 'Your doctor confirmed your prescription. ' + (
        f'Your pens go out on {date_format(first_parcel, "D, j M")}.' if subscription is not None
        else 'You can start your subscription now.'))
    return steps[0]
