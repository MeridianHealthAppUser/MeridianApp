"""Starting plans: the standard ways to start a main medicine, kept in the Pharmacy catalogue by Super Admins and
offered by Initiate prescription on the patient record.

A plan is a few steps of one dose for some weeks each, then a last dose until the prescription ends. The doctor picks
the total weeks, so the last dose fills whatever the steps leave. Initiating saves one prescription (treatment
authorisation) per step with its own dates, sharing a plan_key: the dispensary can only send each step's dose while
its step is current, and the clinical log shows the plan once, on the first step.
"""

import uuid
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.formats import date_format

from practices.models import CompanyMembership
from .clinical import _lock_context, _require_doctor
from .models import MedicationProduct, StartingPlan, StartingPlanStep, TreatmentAuthorization
from .parcels import schedule_parcels
from .patient_assignment import make_prescribing_doctor
from .prescriptions import (
    WEEK_CHOICES, _cancel_later_steps, _check_flags, _locked_current, _supersede, _weeks, add_weeks, weeks_text,
)
from .services import record_audit
from .treatment import _event


MAX_STEPS = 4
STEP_WEEKS = range(1, 21)


def plan_summary(plan):
    """"0.25 mg weekly for 4 weeks, then 0.5 mg weekly for 4 weeks, then 1 mg weekly": the plan's own words."""
    parts = [f'{step.dose.label} for {weeks_text(step.weeks)}' for step in plan.steps.all()]
    parts.append(plan.then_dose.label)
    return ', then '.join(parts)


def plan_fixed_weeks(plan):
    return sum(step.weeks for step in plan.steps.all())


def plan_week_choices(plan):
    """The prescription lengths this plan allows: longer than its steps, so the last dose gets some weeks."""
    return [weeks for weeks in WEEK_CHOICES if weeks > plan_fixed_weeks(plan)]


def plan_schedule(plan, weeks, today=None):
    """[(dose, starts_on, expires_on)]: each step to the day before the next, the last to the prescription's end."""
    start = today or timezone.localdate()
    end = add_weeks(start, weeks)
    schedule = []
    for step in plan.steps.all():
        step_end = add_weeks(start, step.weeks) - timedelta(days=1)
        schedule.append((step.dose, start, step_end))
        start = step_end + timedelta(days=1)
    schedule.append((plan.then_dose, start, end))
    return schedule


def schedule_text(schedule):
    """"0.25 mg weekly to 10 Nov 2026, 0.5 mg weekly to 8 Dec 2026, then 1 mg weekly to 30 Mar 2027"."""
    parts = [f'{dose.label} to {date_format(expires_on, "j M Y")}' for dose, _, expires_on in schedule]
    return ', then '.join([', '.join(parts[:-1]), parts[-1]]) if len(parts) > 1 else parts[0]


def offered_plans(company):
    """Active plans whose doses can all still be prescribed, by medicine."""
    plans = StartingPlan.objects.for_company(company).filter(is_active=True).select_related('then_dose__pen').prefetch_related(
        'steps__dose__pen').order_by('medicine', 'name', 'pk')
    return [plan for plan in plans if all(_is_main(dose, plan.medicine) for dose in plan_doses(plan))]


def published_prices(company):
    """What treatment costs, for patients before they book: each medicine's first offered starting plan, as
    [{'medicine', 'rows': [{'when': 'Weeks 1–4', 'dose'}, …, {'when': 'Thereafter', 'dose'}]}]."""
    prices, seen = [], set()
    for plan in offered_plans(company):
        if plan.medicine in seen:
            continue
        seen.add(plan.medicine)
        rows, week = [], 1
        for step in plan.steps.all():
            end = week + step.weeks - 1
            rows.append({'when': f'Week {week}' if week == end else f'Weeks {week}–{end}', 'dose': step.dose})
            week = end + 1
        rows.append({'when': 'Thereafter', 'dose': plan.then_dose})
        prices.append({'medicine': plan.medicine, 'rows': rows})
    return prices


def plan_doses(plan):
    return [step.dose for step in plan.steps.all()] + [plan.then_dose]


def _is_main(dose, medicine):
    pen = dose.pen
    return (dose.is_prescribable and pen.name == medicine and pen.requires_authorisation
            and pen.category == MedicationProduct.Category.WEIGHT_MANAGEMENT)


def check_plan(*, company, medicine, steps, then_dose, default_weeks):
    """steps: [(dose, weeks)] in order. Raises ValidationError with what's wrong, for the catalogue editor."""
    if not isinstance(medicine, str) or not medicine.strip():
        raise ValidationError('Choose the medicine this plan starts.')
    if not 1 <= len(steps) <= MAX_STEPS:
        raise ValidationError(f'Add between 1 and {MAX_STEPS} steps before the last dose.')
    for dose, weeks in [*steps, (then_dose, None)]:
        if dose is None or dose.company_id != company.pk or not _is_main(dose, medicine):
            raise ValidationError(f'Every dose must be an offered, priced {medicine} dose that needs a prescription.')
        if weeks is not None and weeks not in STEP_WEEKS:
            raise ValidationError(f'Each step lasts between {STEP_WEEKS[0]} and {STEP_WEEKS[-1]} weeks.')
    fixed = sum(weeks for _, weeks in steps)
    if default_weeks not in WEEK_CHOICES or default_weeks <= fixed:
        raise ValidationError(f'The usual length must be longer than the steps ({weeks_text(fixed)}) and at most '
                              f'{weeks_text(WEEK_CHOICES[-1])}.')


@transaction.atomic
def save_starting_plan(*, company, actor, plan=None, medicine, name, steps, then_dose, default_weeks, is_active,
                       request=None):
    """Create or edit a starting plan. Prescriptions already written from it keep their own steps."""
    if not CompanyMembership.objects.filter(company=company, user=actor, is_active=True,
                                            role=CompanyMembership.Role.SUPER_ADMIN).exists():
        raise PermissionDenied('Only a Super Admin can change starting plans.')
    check_plan(company=company, medicine=medicine, steps=steps, then_dose=then_dose, default_weeks=default_weeks)
    plan = plan or StartingPlan(company=company)
    plan.medicine, plan.name, plan.then_dose = medicine, ' '.join((name or '').split())[:80], then_dose
    plan.default_weeks, plan.is_active = default_weeks, bool(is_active)
    plan.full_clean()
    created = plan.pk is None
    plan.save()
    plan.steps.all().delete()
    for position, (dose, weeks) in enumerate(steps, start=1):
        StartingPlanStep.objects.create(company=company, plan=plan, position=position, dose=dose, weeks=weeks)
    record_audit(company=company, actor=actor, action='starting_plan.created' if created else 'starting_plan.updated',
                 target=plan, request=request)
    return plan


@transaction.atomic
def initiate_prescription(*, company, patient, actor, current, plan, weeks, reason='', acknowledge_flags=False,
                          request=None):
    """Start the patient on a starting plan from today, replacing any current prescription (as Change does)."""
    company, patient = _lock_context(company, patient, actor)
    _require_doctor(company, actor)
    weeks = _weeks(weeks)
    previous = _locked_current(company, patient, current)
    plan = next((offered for offered in offered_plans(company) if offered.pk == getattr(plan, 'pk', None)), None)
    if plan is None:
        raise ValidationError('Choose one of the starting plans.')
    if weeks not in plan_week_choices(plan):
        raise ValidationError(f'This plan’s steps take {weeks_text(plan_fixed_weeks(plan))}. Choose a longer prescription.')
    if not isinstance(reason, str) or len(reason) > 500:
        raise ValidationError('Keep the reason to 500 characters or fewer.')
    reason = ' '.join(reason.split())
    flags = _check_flags(company, patient, acknowledge_flags)
    key = uuid.uuid4()
    schedule = plan_schedule(plan, weeks)
    steps = []
    for position, (dose, starts_on, expires_on) in enumerate(schedule, start=1):
        step = TreatmentAuthorization(
            company=company, patient=patient, prescribed_by=actor, product=dose.pen, dose=dose, max_dose=dose.label,
            quantity_per_cycle=dose.pens_per_dispatch, starts_on=starts_on, expires_on=expires_on,
            review_interval_days=previous.review_interval_days if previous else 180, plan_key=key, plan_step=position,
            replaces=previous if position == 1 else None, change_reason=reason if position == 1 else '',
        )
        if position == 1:
            step.log_text = (f'{reason.rstrip(".")}. ' if reason else '') + (
                f'Prescription initiated: {plan.medicine} {plan_summary(plan)}, '
                f'{weeks_text(weeks)} in all, valid to {date_format(schedule[-1][2], "j M Y")}.')
        step.full_clean()
        step.save()
        steps.append(step)
    if previous is not None:
        _cancel_later_steps(company, patient, previous)
        _supersede(company, patient, previous, steps[0], hold=True, review_due_on=schedule[-1][2])
    for step in steps:
        schedule_parcels(step)
    make_prescribing_doctor(company=company, patient=patient, doctor=actor, request=request)
    record_audit(company=company, actor=actor, patient=patient, action='prescription.initiated', target=steps[0],
                 request=request, metadata={'starting_plan_id': plan.pk, 'weeks': weeks,
                                            'previous_authorization_id': previous.pk if previous else None,
                                            'steps': [step.pk for step in steps], 'safety_flags_acknowledged': bool(flags)})
    _event(steps[0], 'Treatment authorised', 'Your doctor recorded a treatment decision. Open your treatment page to see its details.')
    from .billing import review_mandates

    review_mandates(company, patient, prescription_changed=True)
    return steps[0]
