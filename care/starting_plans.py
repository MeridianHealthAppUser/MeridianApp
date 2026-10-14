"""Starting plans: the standard ways to start a main medicine, kept in the Pharmacy catalogue by Super Admins and
offered by Initiate prescription on the patient record.

A plan is a few steps of one strength for whole months each, then a last strength until the prescription ends. The
doctor picks the total months, so the last strength fills whatever the steps leave. Initiating saves one prescription
(treatment authorisation) per step with its own dates, sharing a plan_key: the dispensary can only send each month's
strength while its step is current, and the clinical log shows the plan once, on the first step.
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
from .patient_assignment import make_prescribing_doctor
from .prescriptions import (
    MONTH_CHOICES, _cancel_later_steps, _check_flags, _locked_current, _months, _months_text, _supersede, add_months,
    dose_label,
)
from .services import record_audit
from .treatment import _event


MAX_STEPS = 4
STEP_MONTHS = range(1, 6)


def _months_phrase(months):
    return f'for {_months_text(months)}'


def plan_summary(plan):
    """"0.25 mg weekly for 1 month, then 0.5 mg weekly for 1 month, then 1.0 mg weekly": the plan's own words."""
    parts = [f'{dose_label(step.product)} {_months_phrase(step.months)}' for step in plan.steps.all()]
    parts.append(dose_label(plan.then_product))
    return ', then '.join(parts)


def plan_fixed_months(plan):
    return sum(step.months for step in plan.steps.all())


def plan_month_choices(plan):
    """The prescription lengths this plan allows: longer than its steps, so the last strength gets at least a month."""
    return [months for months in MONTH_CHOICES if months > plan_fixed_months(plan)]


def plan_schedule(plan, months, today=None):
    """[(product, starts_on, expires_on)]: each step to the day before the next, the last to the prescription's end."""
    start = today or timezone.localdate()
    end = add_months(start, months)
    schedule = []
    for step in plan.steps.all():
        step_end = add_months(start, step.months) - timedelta(days=1)
        schedule.append((step.product, start, step_end))
        start = step_end + timedelta(days=1)
    schedule.append((plan.then_product, start, end))
    return schedule


def schedule_text(schedule):
    """"0.25 mg weekly to 13 Nov 2026, 0.5 mg weekly to 13 Dec 2026, then 1.0 mg weekly to 14 Apr 2027"."""
    parts = [f'{dose_label(product)} to {date_format(expires_on, "j M Y")}' for product, _, expires_on in schedule]
    return ', then '.join([', '.join(parts[:-1]), parts[-1]]) if len(parts) > 1 else parts[0]


def offered_plans(company):
    """Active plans whose strengths are all main medicines still in the catalogue, by medicine."""
    plans = StartingPlan.objects.for_company(company).filter(is_active=True).select_related('then_product').prefetch_related(
        'steps__product').order_by('medicine', 'name', 'pk')
    return [plan for plan in plans if all(_is_main(product, plan.medicine) for product in _plan_products(plan))]


def _plan_products(plan):
    return [step.product for step in plan.steps.all()] + [plan.then_product]


def _is_main(product, medicine):
    return (product.is_active and product.name == medicine and product.requires_authorisation
            and product.category == MedicationProduct.Category.WEIGHT_MANAGEMENT)


def check_plan(*, company, medicine, steps, then_product, default_months):
    """steps: [(product, months)] in order. Raises ValidationError with what's wrong, for the catalogue editor."""
    if not isinstance(medicine, str) or not medicine.strip():
        raise ValidationError('Choose the medicine this plan starts.')
    if not 1 <= len(steps) <= MAX_STEPS:
        raise ValidationError(f'Add between 1 and {MAX_STEPS} steps before the last strength.')
    for product, months in [*steps, (then_product, None)]:
        if product is None or product.company_id != company.pk or not _is_main(product, medicine):
            raise ValidationError(f'Every strength must be an active {medicine} in the catalogue that needs a prescription.')
        if months is not None and months not in STEP_MONTHS:
            raise ValidationError('Each step lasts between 1 and 5 months.')
    fixed = sum(months for _, months in steps)
    if default_months not in MONTH_CHOICES or default_months <= fixed:
        raise ValidationError(f'The usual length must be longer than the steps ({_months_text(fixed)}) and at most '
                              f'{_months_text(MONTH_CHOICES[-1])}.')


@transaction.atomic
def save_starting_plan(*, company, actor, plan=None, medicine, name, steps, then_product, default_months, is_active,
                       request=None):
    """Create or edit a starting plan. Prescriptions already written from it keep their own steps."""
    if not CompanyMembership.objects.filter(company=company, user=actor, is_active=True,
                                            role=CompanyMembership.Role.SUPER_ADMIN).exists():
        raise PermissionDenied('Only a Super Admin can change starting plans.')
    check_plan(company=company, medicine=medicine, steps=steps, then_product=then_product, default_months=default_months)
    plan = plan or StartingPlan(company=company)
    plan.medicine, plan.name, plan.then_product = medicine, ' '.join((name or '').split())[:80], then_product
    plan.default_months, plan.is_active = default_months, bool(is_active)
    plan.full_clean()
    created = plan.pk is None
    plan.save()
    plan.steps.all().delete()
    for position, (product, months) in enumerate(steps, start=1):
        StartingPlanStep.objects.create(company=company, plan=plan, position=position, product=product, months=months)
    record_audit(company=company, actor=actor, action='starting_plan.created' if created else 'starting_plan.updated',
                 target=plan, request=request)
    return plan


@transaction.atomic
def initiate_prescription(*, company, patient, actor, current, plan, months, reason='', acknowledge_flags=False,
                          request=None):
    """Start the patient on a starting plan from today, replacing any current prescription (as Change does)."""
    company, patient = _lock_context(company, patient, actor)
    _require_doctor(company, actor)
    months = _months(months)
    previous = _locked_current(company, patient, current)
    plan = next((offered for offered in offered_plans(company) if offered.pk == getattr(plan, 'pk', None)), None)
    if plan is None:
        raise ValidationError('Choose one of the starting plans.')
    if months not in plan_month_choices(plan):
        raise ValidationError(f'This plan’s steps take {_months_text(plan_fixed_months(plan))}. Choose a longer prescription.')
    if not isinstance(reason, str) or len(reason) > 500:
        raise ValidationError('Keep the reason to 500 characters or fewer.')
    reason = ' '.join(reason.split())
    flags = _check_flags(company, patient, acknowledge_flags)
    key = uuid.uuid4()
    schedule = plan_schedule(plan, months)
    steps = []
    for position, (product, starts_on, expires_on) in enumerate(schedule, start=1):
        step = TreatmentAuthorization(
            company=company, patient=patient, prescribed_by=actor, product=product, max_dose=dose_label(product),
            quantity_per_cycle=previous.quantity_per_cycle if previous else 1, starts_on=starts_on, expires_on=expires_on,
            review_interval_days=previous.review_interval_days if previous else 180, plan_key=key, plan_step=position,
            replaces=previous if position == 1 else None, change_reason=reason if position == 1 else '',
        )
        if position == 1:
            step.log_text = (f'{reason.rstrip(".")}. ' if reason else '') + (
                f'Prescription initiated: {plan.medicine} {plan_summary(plan)}, '
                f'{_months_text(months)} in all, valid to {date_format(schedule[-1][2], "j M Y")}.')
        step.full_clean()
        step.save()
        steps.append(step)
    if previous is not None:
        _cancel_later_steps(company, patient, previous)
        _supersede(company, patient, previous, steps[0], hold=True, review_due_on=schedule[-1][2])
    make_prescribing_doctor(company=company, patient=patient, doctor=actor, request=request)
    record_audit(company=company, actor=actor, patient=patient, action='prescription.initiated', target=steps[0],
                 request=request, metadata={'starting_plan_id': plan.pk, 'months': months,
                                            'previous_authorization_id': previous.pk if previous else None,
                                            'steps': [step.pk for step in steps], 'safety_flags_acknowledged': bool(flags)})
    _event(steps[0], 'Treatment authorised', 'Your doctor recorded a treatment decision. Open your treatment page to see its details.')
    return steps[0]
