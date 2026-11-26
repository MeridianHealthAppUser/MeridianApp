"""Read-only summaries for the patient pages: what is current, what is next and what needs doing.

Every value comes from a stored record. Nothing here predicts a result, sets a
target, invents a price or implies that a payment is taken: the payments a treatment
timeline shows are the catalogue dose's reference prices, labelled as such.
"""

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db.models import Case, DateTimeField, DurationField, ExpressionWrapper, F, IntegerField, Max, Min, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Cast
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from care.models import (
    AuditEvent,
    Appointment, LabRequest, Lead, PatientEvent, PatientMedicalProfile, PatientSubscription, Payment,
    ScreeningQuestionnaire, Shipment, SubscriptionCycle, TreatmentAuthorization, WeightEntry,
)
from care.dose_prices import price_for
from care.treatment import current_authorizations
from video.access import attach_video_join


# A follow-up is prompted this long before an authorisation lapses, so there is
# still time to book one. Weight check-ins are prompted weekly.
RENEWAL_WINDOW_DAYS = 45
WEIGHT_PROMPT_DAYS = 7
REVIEWED_TASK_DAYS = 14  # Reviewed blood tests stay on My Tasks this long, marked Reviewed.


def current_plan(company, patient):
    return PatientSubscription.objects.for_company(company).filter(patient=patient).exclude(
        status=PatientSubscription.Status.CANCELLED,
    ).select_related(
        'authorization__product', 'authorization__prescribed_by', 'authorization__company', 'authorization__patient',
    ).order_by('-created_at', '-pk').first()


def current_authorization(company, patient, plan=None):
    """The authorisation the patient is treated under today, preferring their plan's."""
    candidates = current_authorizations().filter(company=company, patient=patient).select_related(
        'product', 'prescribed_by', 'company', 'patient',
    )
    preference = [Case(When(pk=plan.authorization_id, then=Value(0)), default=Value(1))] if plan and plan.authorization_id else []
    return candidates.order_by(*preference, '-expires_on', '-pk').first()


def previous_prescriptions(company, patient):
    """The other prescriptions previously shown in My Medications, including issued future plan steps."""
    prescriptions = TreatmentAuthorization.objects.for_company(company).filter(
        patient=patient, patient__company=company, product__company=company, product__requires_authorisation=True,
    )
    current_products = current_authorizations(prescriptions).order_by().values('product_id')
    latest = prescriptions.filter(product_id=OuterRef('product_id')).order_by('-expires_on', '-pk').values('pk')[:1]
    return prescriptions.exclude(product_id__in=current_products).filter(pk=Subquery(latest)).select_related(
        'product', 'prescribed_by',
    ).order_by('-starts_on', '-pk')


def weight_reference_course(company, patient, authorization):
    """Only the dates Home needs, from one scoped query regardless of renewal count.

    Follow replacements and starting-plan steps in memory. Superseded prescriptions
    retain their historical dates; an unreplaced cancelled prescription is not a course.
    """
    if authorization is None or authorization.status == TreatmentAuthorization.Status.CANCELLED:
        return None
    rows = list(TreatmentAuthorization.objects.for_company(company).filter(
        patient=patient, patient__company=company, product__company=company,
    ).values('pk', 'starts_on', 'expires_on', 'status', 'plan_key', 'replaces_id'))
    by_id = {row['pk']: row for row in rows}
    if authorization.pk not in by_id or by_id[authorization.pk]['status'] == TreatmentAuthorization.Status.CANCELLED:
        return None
    plans = {}
    for row in rows:
        if row['plan_key']:
            plans.setdefault(row['plan_key'], []).append(row['pk'])
    members, expanded_plans, pending = {}, set(), [authorization.pk]
    while pending:
        pk = pending.pop()
        if pk in members or pk not in by_id:
            continue
        row = members[pk] = by_id[pk]
        if row['replaces_id']:
            pending.append(row['replaces_id'])
        key = row['plan_key']
        if key and key not in expanded_plans:
            expanded_plans.add(key)
            pending.extend(plans[key])
    replaced_from = {}
    for row in members.values():
        if row['replaces_id']:
            previous = row['replaces_id']
            replaced_from[previous] = min(replaced_from.get(previous, row['starts_on']), row['starts_on'])
    segments = []
    for row in members.values():
        if row['status'] == TreatmentAuthorization.Status.CANCELLED and row['pk'] not in replaced_from:
            continue
        end = row['expires_on']
        if row['pk'] in replaced_from:
            end = min(end, replaced_from[row['pk']] - timedelta(days=1))
        if end >= row['starts_on']:
            segments.append((row['starts_on'], end))
    if not segments:
        return None
    return {'started_on': min(start for start, _ in segments), 'ends_on': max(end for _, end in segments)}


HOW_OFTEN = {'weekly': 'once a week', 'daily': 'once a day'}
PARCEL_STATUS = {Shipment.Status.DISPATCHED: 'shipped', Shipment.Status.DELIVERED: 'delivered'}


def _pens(quantity, product):
    return f'{quantity} × {product} pen{"" if quantity == 1 else "s"}'


def _parcel_line(item, medicine):
    """'1 × 1.0 mg pen' for the prescribed medicine's pens; anything else by its full name."""
    if item.product.name == medicine and item.product.strength:
        return _pens(item.quantity, item.product.strength)
    return f'{item.quantity} × {item.product}'


def treatment_course(authorization, today=None):
    """The treatment a prescription belongs to, for the patient's timeline: the prescriptions it has been on, oldest
    first, each with the dates it was in effect. That is a starting plan's steps and the renewals and changes before
    and after it, back through the prescriptions each one replaced. A prescription replaced early ends the day before
    its replacement starts; one cancelled before it took effect (a plan's later steps after a change) is left out."""
    from care.parcels import first_shipping_day
    from care.treatment import step_in_effect

    today = today or timezone.localdate()
    current = step_in_effect(authorization, today)
    members, seen, queue = [], set(), [current]
    while queue:
        prescription = queue.pop()
        if prescription.pk in seen:
            continue
        seen.add(prescription.pk)
        members.append(prescription)
        if prescription.plan_key:
            queue.extend(TreatmentAuthorization.objects.filter(
                company_id=prescription.company_id, patient_id=prescription.patient_id, plan_key=prescription.plan_key,
            ).select_related('dose__pen', 'product', 'prescribed_by'))
        if prescription.replaces_id:
            queue.append(TreatmentAuthorization.objects.select_related('dose__pen', 'product', 'prescribed_by').get(
                pk=prescription.replaces_id))
    # A revoked prescription (James, 26 Nov 2026) stays, to the end of the period already paid for (its pens) or the
    # day it was revoked if that is later; nothing is planned after it. That includes a starting plan's later steps:
    # revoking cancels them, but one revoked before that (26 Nov 2026) can still be active, so they are left out here.
    revocations = {int(event.target_id): event for event in AuditEvent.objects.filter(
        company_id=current.company_id, action='treatment.revoke', target_type='care.treatmentauthorization',
        target_id__in=[str(prescription.pk) for prescription in members]).select_related('actor')}
    revoked_on = {pk: timezone.localdate(event.created_at) for pk, event in revocations.items()}
    cut = {}
    for prescription in members:
        if prescription.pk in revocations and prescription.plan_key and prescription.plan_step:
            cut[prescription.plan_key] = min(cut.get(prescription.plan_key, prescription.plan_step), prescription.plan_step)
    if cut:
        members = [prescription for prescription in members
                   if not (prescription.plan_key in cut and (prescription.plan_step or 0) > cut[prescription.plan_key])]
        if current not in members:
            current = next(prescription for prescription in members if prescription.pk in revocations)
    replaced_from = {}
    for prescription in members:
        if prescription.replaces_id:
            replaced_from[prescription.replaces_id] = min(replaced_from.get(prescription.replaces_id, prescription.starts_on),
                                                          prescription.starts_on)
    segments = []
    for prescription in members:
        if prescription.status == TreatmentAuthorization.Status.CANCELLED and prescription.pk not in replaced_from:
            if prescription.pk not in revoked_on:
                continue
            paid_to = SubscriptionCycle.objects.filter(subscription__authorization_id=prescription.pk,
                                                       status=SubscriptionCycle.Status.PAID).aggregate(end=Max('ends_on'))['end']
            end = min(prescription.expires_on, max(revoked_on[prescription.pk], paid_to or revoked_on[prescription.pk]))
            if end >= prescription.starts_on:
                segments.append({'prescription': prescription, 'starts_on': prescription.starts_on, 'ends_on': end})
            continue
        end = prescription.expires_on
        if prescription.pk in replaced_from:
            end = min(end, replaced_from[prescription.pk] - timedelta(days=1))
        if end >= prescription.starts_on:
            segments.append({'prescription': prescription, 'starts_on': prescription.starts_on, 'ends_on': end})
    segments.sort(key=lambda segment: (segment['starts_on'], segment['prescription'].pk))
    started, ends = segments[0]['starts_on'], max(segment['ends_on'] for segment in segments)
    # Not started yet (nothing paid, nothing sent): treatment starts on the first payment, when the course moves to start
    # with its first pens (care.prescriptions.start_course_on). Until then it is shown as if the patient paid today.
    ids = [prescription.pk for prescription in members]
    not_started = not (Shipment.objects.filter(authorization_id__in=ids, status__in=PARCEL_STATUS).exists()
                       or SubscriptionCycle.objects.filter(subscription__authorization_id__in=ids,
                                                           status=SubscriptionCycle.Status.PAID).exists())
    # Its weeks run from its first pens, which go out on the first shipping day whose order cut-off is still to come
    # when paid: it moves by whole weeks, as it will on the first payment (care.prescriptions._move_course).
    starts_with_pens = first_shipping_day(today, current.company)
    shift = starts_with_pens - first_shipping_day(started, current.company)
    if not_started and shift.days > 0:
        segments = [{**segment, 'starts_on': segment['starts_on'] + shift, 'ends_on': segment['ends_on'] + shift}
                    for segment in segments]
        started, ends = started + shift, ends + shift
    # The patient's weeks start with their pens: week 1 is the calendar week of the first parcel (on the first shipping
    # day whose order cut-off is on or after the first payment, or the day it was actually planned for), and each week
    # after has its parcel day, a week apart. What came before it (the prescription, the first payment) is week 0.
    if not_started:
        first_parcel = first_shipping_day(started, current.company)  # Shown as if paid today.
    else:
        first_parcel = Shipment.objects.filter(authorization_id__in=ids).exclude(
            Q(status=Shipment.Status.CANCELLED) & ~Q(hold_reason__startswith='Missed: ')).order_by(
            'scheduled_for').values_list('scheduled_for', flat=True).first() or first_shipping_day(started, current.company)
    first_week = first_parcel - timedelta(days=first_parcel.weekday())
    # Up to the week the prescription ends in: a course starting with its pens runs its prescribed weeks exactly.
    total = max(1, -(-(ends - first_parcel).days // 7))
    # Who revoked a prescription and when, for the timeline's decisions (James, 26 Nov 2026).
    revoked = [{'prescription': prescription, 'on': revoked_on[prescription.pk], 'by': revocations[prescription.pk].actor}
               for prescription in members if prescription.pk in revoked_on]
    return {'current': current, 'segments': segments, 'started_on': started, 'ends_on': ends, 'total': total, 'revoked': revoked,
            'not_started': not_started,
            'first_week': first_week, 'first_parcel': first_parcel, 'before_start': today < first_week,
            'week': min(total, max(1, (today - first_week).days // 7 + 1)),
            'progress': min(100, max(0, round(100 * (today - first_week).days / max(1, (ends - first_week).days))))}


def _starts_with_pens(course, day):
    """The week a dose starting on day begins for the patient: the first week whose parcel day is on or after it."""
    weeks = max(0, -(-(day - course['first_parcel']).days // 7))
    return {'week': weeks + 1, 'week_starts_on': course['first_week'] + timedelta(weeks=weeks)}


def treatment_overview(company, patient, authorization, today=None):
    """What the patient sees about the prescription they are on: the dose and how often it is taken, the pens each
    parcel brings, the week of treatment, the next dose step and the next parcel. Every value is the prescription's
    or the catalogue dose's. None when the prescription isn't for a catalogue dose."""
    if authorization is None or not authorization.dose_id:
        return None
    today = today or timezone.localdate()
    course = treatment_course(authorization, today)
    current = course['current']
    dose = current.dose
    # The next dose step after the one the patient is on, or will start on with their first pens.
    pivot = max(today, course['first_parcel'])
    later = next((segment for segment in course['segments'] if segment['starts_on'] > pivot), None)
    from care.prescriptions import awaiting_confirmation, moved_note

    parcels = Shipment.objects.for_company(company).filter(patient=patient).prefetch_related('items__product')
    upcoming = parcels.filter(status__in=(Shipment.Status.DRAFT, Shipment.Status.READY, Shipment.Status.HELD),
                              scheduled_for__gte=today).order_by('scheduled_for', 'pk').first()
    on_its_way = parcels.filter(status=Shipment.Status.DISPATCHED).order_by('-dispatched_at', '-pk').first()
    return {
        'medicine': current.product.name, 'dose': current.max_dose, 'amount': f'{dose.amount_text} mg',
        'how_often': HOW_OFTEN.get(dose.frequency, 'as your doctor directed'), 'supply': dose.dispatch_text,
        'week': course['week'], 'weeks': course['total'], 'progress': course['progress'],
        'first_week': course['first_week'], 'before_start': course['before_start'], 'ends_on': course['ends_on'],
        'not_started': course['not_started'],
        'moved': moved_note(current),
        # Paid, but the doctor confirms an old prescription before the first parcel goes out.
        'waiting_for_doctor': awaiting_confirmation(current),
        'next_step': later and _starts_with_pens(course, later['starts_on']) | {'dose': later['prescription'].max_dose},
        'next_parcel': not course['not_started'] and upcoming and {'date': upcoming.scheduled_for, 'held': upcoming.status == Shipment.Status.HELD,
                                     'contents': [_pens(item.quantity, item.product) for item in upcoming.items.all()]},
        'on_its_way': on_its_way and {'date': timezone.localdate(on_its_way.dispatched_at) if on_its_way.dispatched_at else on_its_way.scheduled_for,
                                      'tracking': on_its_way.tracking_number},
        'prescribed_by': current.prescribed_by,
    }


def _dose_of(prescription, with_medicine):
    amount = f'{prescription.dose.amount_text} mg' if prescription.dose_id else prescription.max_dose
    return f'{prescription.product.name} {amount}' if with_medicine else amount


def day_text(day, year=False):
    """'Wed, 26 Aug' (or 'Wed, 26 Aug 2026'): the day of the week with the date, on the timeline and Prescription
    changes (James, 25 Nov 2026)."""
    return f'{day:%a}, {day:%-d %b}' + (f' {day:%Y}' if year else '')


def doctor_name(user):
    """'Dr Sam Marchant': the title on their profile; only doctors prescribe, so one without a title is still Dr."""
    if user is None:
        return ''
    return user.titled_name if user.title else f'Dr {user.full_name}'


def week_range(start):
    """'19–25 Oct 2026', '26 Oct–1 Nov 2026' or '28 Dec 2026–3 Jan 2027': a week's first and last day."""
    end = start + timedelta(days=6)
    if start.year != end.year:
        return f'{start:%-d %b %Y}–{end:%-d %b %Y}'
    if start.month != end.month:
        return f'{start:%-d %b}–{end:%-d %b %Y}'
    return f'{start:%-d}–{end:%-d %b %Y}'


def _segment_on(segments, day):
    return next((segment for segment in segments if segment['starts_on'] <= day <= segment['ends_on']), None)


def parcel_problem(shipment):
    """Why a parcel didn't go out, or is held, in the patient's words: a payment that didn't go through, a paused
    subscription, blood tests or the doctor to wait for, a changed prescription, or a delivery address to add. '' when
    nothing stops it."""
    from care.billing import parcel_payment
    from care.operations import delivery_problem

    reason = shipment.hold_reason or ''
    if reason.startswith('Missed: '):
        return reason.removeprefix('Missed: ')  # Kept on record when the pens went out later (care.prescriptions).
    if reason.startswith('Treatment authorisation paused') and shipment.authorization_id and \
            shipment.authorization.status == TreatmentAuthorization.Status.CANCELLED:
        return 'your doctor stopped this prescription'  # Revoked, not paused (26 Nov 2026).
    for start, words in (('Subscription paused', 'your subscription was paused'),
                         ('Paused until the blood tests', 'waiting for your blood tests'),
                         ('Prescription changed', 'your prescription changed'),
                         ('Treatment authorisation paused', 'your doctor paused your treatment'),
                         ('Waiting for', 'waiting for your doctor')):
        if reason.startswith(start):
            return words
    state = parcel_payment(shipment)[0]
    if state in ('failed', 'retrying'):
        return 'payment didn’t go through'
    if state in ('unpaid', 'due'):
        return 'not paid'
    problem = delivery_problem(shipment)
    if problem:
        return 'no delivery address' if problem.startswith('No delivery address') else 'delivery address incomplete'
    return ''


def _course_plan(company, patient, prescription_ids):
    """The care plan on this course: the current one, else the latest cancelled one."""
    plans = PatientSubscription.objects.for_company(company).filter(patient=patient, authorization_id__in=prescription_ids)
    return (plans.exclude(status=PatientSubscription.Status.CANCELLED).order_by('-created_at', '-pk').first()
            or plans.order_by('-created_at', '-pk').first())


def plan_payments(plan):
    """A care plan's payments for the patient's timeline, oldest first: each billing period with what happened to it
    (paid, due, retrying, not paid or covered by a discount), then the ones planned to the end of the prescription
    (care.subscription_discounts.payment_schedule, as the practice sees them). Each is {'due_on', 'amount', 'state',
    'note'}; state is paid, due, retrying, failed or planned."""
    from care.subscription_discounts import payment_schedule

    payments = []
    # Billing stops after a payment that failed completely, until it is paid (care.billing.bill_subscription).
    stopped = plan.cycles.filter(status=SubscriptionCycle.Status.FAILED).exists()
    for row in payment_schedule(plan)['rows']:
        cycle = row['cycle']
        if cycle is None:
            # Planned: on its day, unless the plan is paused (it carries on from when the patient resumes) or waits
            # for a missed payment (it carries on from when that is paid).
            state = 'planned'
            note = ('paused' if plan.status == PatientSubscription.Status.PAUSED else
                    'after your missed payment' if stopped else day_text(row['due_on']))
        elif cycle.status == SubscriptionCycle.Status.CANCELLED:
            continue
        elif cycle.status == SubscriptionCycle.Status.PAID:
            paid_on = timezone.localdate(cycle.paid_at) if cycle.paid_at else None
            state = 'paid'
            note = 'covered by a discount' if not cycle.amount else (f'paid {day_text(paid_on)}' if paid_on else 'paid')
            if paid_on and paid_on > cycle.due_on and any(
                    payment.status == Payment.Status.FAILED for payment in cycle.payments.all()):
                # Paid after a try that didn't go through: both, each on its day.
                payments.append({'due_on': cycle.due_on, 'amount': row['amount'], 'state': 'failed',
                                 'note': 'didn’t go through'})
                payments.append({'due_on': paid_on, 'amount': row['amount'], 'state': state, 'note': note})
                continue
        elif cycle.status == SubscriptionCycle.Status.FAILED:
            state, note = 'failed', 'not paid'
        elif row['status'] == 'Processing':
            state, note = 'due', 'being processed'
        elif cycle.attempts:
            state = 'retrying'
            note = f'didn’t go through · retry {day_text(cycle.next_attempt_on)}' if cycle.next_attempt_on else 'didn’t go through'
        else:
            state, note = 'due', f'due {day_text(cycle.due_on)}'
        payments.append({'due_on': row['due_on'], 'amount': row['amount'], 'state': state, 'note': note})
    return payments


def _decision(segment, segments, not_started):
    """What the doctor decided in a prescription, in a word: the first prescription, a new dose plan over one, a
    renewal or a change. None for a starting plan's later step (the plan, not a new decision). Nothing in effect
    before it, it starts the treatment, even if it replaced one that never took effect."""
    prescription = segment['prescription']
    replaced = prescription.replaces if prescription.replaces_id else None
    if prescription.plan_key and prescription.plan_step and prescription.plan_step > 1 and replaced is None:
        return None
    if replaced is not None and replaced.dose_id == prescription.dose_id and replaced.product_id == prescription.product_id \
            and not (prescription.plan_key and prescription.plan_step == 1):
        return 'renewed'
    if not any(other['starts_on'] < segment['starts_on'] for other in segments):
        return 'written' if not_started else 'started'
    if prescription.plan_key and prescription.plan_step == 1:
        return 'reinitiated'
    return 'changed'


DECISION_WORDS = {'renewed': 'Prescription renewed', 'written': 'Prescription written', 'started': 'Prescription started',
                  'reinitiated': 'Prescription reinitiated', 'changed': 'Prescription adjusted'}


def _pens_from(segments, first_parcel, not_started):
    """[(day, segment)]: when each prescription's pens start, its first parcel (sent, planned or missed); for one with
    none, the shipping day its first would be (care.parcels.first_parcel_day), and the course's first parcel for the
    first."""
    from care.parcels import first_parcel_day

    ids = [segment['prescription'].pk for segment in segments]
    first = {} if not_started else dict(Shipment.objects.filter(authorization_id__in=ids).exclude(
        Q(status=Shipment.Status.CANCELLED) & ~Q(hold_reason__startswith='Missed: ')).values(
        'authorization_id').annotate(day=Min('scheduled_for')).values_list('authorization_id', 'day'))
    days = []
    for index, segment in enumerate(segments):
        prescription = segment['prescription']
        if index == 0:
            day = first_parcel
        elif prescription.pk in first:
            day = first[prescription.pk]
        elif not_started:
            from care.parcels import shipping_day_from, shipping_weekday

            day = shipping_day_from(segment['starts_on'], shipping_weekday(prescription.company_id))
        else:
            day = first_parcel_day(prescription)
        days.append((day, segment))
    return days


def range_text(start, end):
    """'26–30 Aug 2026', '26 Aug–6 Sep 2026' or '28 Dec 2026–3 Jan 2027'."""
    if start.year != end.year:
        return f'{start:%-d %b %Y}–{end:%-d %b %Y}'
    if start.month != end.month:
        return f'{start:%-d %b}–{end:%-d %b %Y}'
    return f'{start:%-d}–{end:%-d %b %Y}'


def treatment_timeline(company, patient, authorization, today=None):
    """The treatment week by week, for a catalogue dose, with where the patient is now. Week 1 is the week of the
    first parcel; week 0 is what came before it (James, 25 Nov 2026): the prescription and the first payment, which is
    taken before the pens are ordered (care.parcels.first_shipping_day). Each week has the doctor's decisions made in
    it (starting, renewing or changing the prescription), the pens planned or sent, the dose in effect on its parcel
    day (so a new dose starts with its pens) with any change from the week before, and the payments: the care plan's
    real ones with what happened to each (plan_payments), or, before it has any, each dose's reference price a week
    before each of its parcels (the first: the day the patient pays)."""
    if authorization is None or not authorization.dose_id:
        return None
    today = today or timezone.localdate()
    course = treatment_course(authorization, today)
    segments, total = course['segments'], course['total']
    first_week, first_parcel = course['first_week'], course['first_parcel']
    # 0 for anything before week 1.
    week_of = lambda day: min(total, max(0, (day - first_week).days // 7 + 1))
    with_medicine = len({segment['prescription'].product.name for segment in segments}) > 1
    medicine = course['current'].product.name
    not_started = course['not_started']
    from care.prescriptions import awaiting_confirmation

    waiting = awaiting_confirmation(course['current'])

    # Week 0 first: shown only when something happened in it.
    weeks, previous = [{'number': 0, 'date': first_week - timedelta(weeks=1), 'dose': None, 'change': '', 'segment': None,
                        'events': [], 'parcels': [], 'payments': [], 'missed': False, 'no_pens': False, 'zero': True,
                        'current': False, 'past': False, 'next': False}], None
    # Each dose starts with its pens: a change written after the order cut-off has its first parcel the week after.
    pens_from = _pens_from(segments, first_parcel, not_started)
    for number in range(1, total + 1):
        day = first_week + timedelta(weeks=number - 1)
        parcel_day = first_parcel + timedelta(weeks=number - 1)
        segment = (max((item for item in pens_from if item[0] <= parcel_day and item[1]['ends_on'] >= parcel_day - timedelta(weeks=1)),
                       key=lambda item: item[0], default=(None, None))[1]
                   or _segment_on(segments, parcel_day) or next(
            (segment for segment in segments if segment['starts_on'] <= day + timedelta(days=6) and segment['ends_on'] >= day), None))
        prescription = segment['prescription'] if segment else None
        dose = _dose_of(prescription, with_medicine) if prescription else None
        change = ''
        if dose and previous and dose != previous[0]:
            before = previous[1]
            # The same medicine in a different pen is still a step up or down.
            if before.product.name == prescription.product.name and before.dose_id and prescription.dose_id:
                change = f'{"up" if prescription.dose.amount_mg > before.dose.amount_mg else "down"} from {_dose_of(before, False)}'
            else:
                change = f'was {_dose_of(before, True)}'
        weeks.append({'number': number, 'date': day, 'range': week_range(day), 'dose': dose, 'change': change,
                      'segment': segment, 'events': [], 'parcels': [], 'payments': [], 'missed': False, 'no_pens': False,
                      'zero': False,
                      'current': not not_started and day <= today < day + timedelta(days=7),
                      'past': not not_started and day + timedelta(days=7) <= today,
                      # Before week 1, it is next: the patient is waiting for their first pens (or to start).
                      'next': number == 1 and (course['before_start'] or not_started)})
        if prescription:
            previous = (dose, prescription)

    # Payments: the care plan's real ones (paid, due, retrying or not paid) and the ones planned after them; before a
    # care plan has any, each dose's reference price.
    plan = None if not_started else _course_plan(company, patient, [segment['prescription'].pk for segment in segments]
                                                 + [course['current'].pk])
    if plan is not None and plan.cycles.exists():
        for payment in plan_payments(plan):
            weeks[week_of(payment['due_on'])]['payments'].append(payment)
    elif not not_started:
        for segment in segments:
            prescription = segment['prescription']
            if prescription.dose_id and prescription.dose.price is not None:
                own = [week for week in weeks if week['segment'] is segment]
                for week in own[::prescription.dose.billing_weeks]:
                    # A scheduled new price from its day.
                    week['payments'].append({'amount': price_for(prescription.dose, week['date']), 'state': 'planned',
                                             'note': ''})

    if not_started:
        # The parcels and payments it would have, planned as the course is shown: the first payment today, its parcel
        # on the first shipping day whose order cut-off is still to come, and each payment after a week before its
        # parcel (care.billing).
        from care.parcels import PAYMENT_LEAD, shipping_day_from, shipping_weekday

        first, weekday = True, shipping_weekday(company)
        for segment in segments:
            dose = segment['prescription'].dose if segment['prescription'].dose_id else None
            if dose is None:
                continue
            day = first_parcel if first else shipping_day_from(segment['starts_on'], weekday)
            parcel_days = []
            # As the drafts are planned: a parcel on the last day would be for the weeks after it.
            while day < segment['ends_on'] or (first and not parcel_days):
                weeks[week_of(day)]['parcels'].append({
                    'contents': _pens(dose.pens_per_dispatch, dose.pen.strength).replace('  ', ' '),
                    'when': f'ships {day_text(day)}, after your first payment' if first else f'ships {day_text(day)}'})
                parcel_days.append(day)
                first = False
                day += timedelta(weeks=dose.dispatch_weeks or 4)
            if dose.price is not None:
                for index, parcel_day in enumerate(parcel_days[::max(1, (dose.billing_weeks or 4) // (dose.dispatch_weeks or 4))]):
                    due = today if parcel_day == first_parcel else parcel_day - PAYMENT_LEAD
                    weeks[week_of(due)]['payments'].append({
                        'amount': price_for(dose, due), 'state': 'planned',
                        'note': 'your first payment' if parcel_day == first_parcel else day_text(due)})
    from care.billing import paid_before_pause, parcel_payment

    this_week = today - timedelta(days=today.weekday())
    missed_parcels, sent_days = [], []
    # Cancelled parcels are left out, except one kept on record as missed when its pens went out later.
    for parcel in [] if not_started else Shipment.objects.for_company(company).filter(
        patient=patient, scheduled_for__range=(course['started_on'], course['ends_on']),
    ).exclude(Q(status=Shipment.Status.CANCELLED) & ~Q(hold_reason__startswith='Missed: ')).prefetch_related(
        'items__product').order_by('scheduled_for', 'pk'):
        day = timezone.localdate(parcel.dispatched_at) if parcel.dispatched_at else parcel.scheduled_for
        # A parcel from an earlier week that never went out was missed: say why (an unpaid period, no address...).
        missed = (parcel.status not in PARCEL_STATUS and day < this_week) or (parcel.hold_reason or '').startswith('Missed: ')
        if parcel.status in PARCEL_STATUS:
            when = f'{PARCEL_STATUS[parcel.status]} {day_text(day)}'
        elif missed:
            when = f'missed · {parcel_problem(parcel) or "not sent"}'
        elif parcel.status == Shipment.Status.HELD:
            problem = parcel_problem(parcel)
            when = f'on hold · {problem}' if problem else 'on hold'
        elif waiting:
            when = 'after your doctor confirms'
        elif paid_before_pause(parcel):
            when = f'ships {day_text(day)} · your last until you resume'
        elif parcel_payment(parcel)[0] in ('failed', 'retrying'):
            when = f'waits for your payment · {day_text(day)}'
        else:
            when = f'ships {day_text(day)}'
        if missed:
            missed_parcels.append(parcel)
        elif parcel.authorization_id:
            sent_days.append(day)  # Sent or still to go: the pens after a missed parcel.
        week = weeks[week_of(day)]
        week['missed'] = week['missed'] or missed
        week['parcels'].append({'contents': ', '.join(_parcel_line(item, medicine) for item in parcel.items.all()),
                                'when': when, 'missed': missed})

    # A missed parcel leaves the patient without pens for the weeks it would have covered, until a parcel goes out.
    for parcel in missed_parcels:
        dose = parcel.authorization.dose if parcel.authorization_id and parcel.authorization.dose_id else None
        until = parcel.scheduled_for + timedelta(weeks=dose.dispatch_weeks if dose and dose.dispatch_weeks else 4)
        until = min([until] + [day for day in sent_days if day > parcel.scheduled_for])
        until -= timedelta(days=until.weekday())  # The week the next pens are due has them.
        for week in weeks[1:]:
            if week['date'] + timedelta(days=6) >= parcel.scheduled_for and week['date'] < until:
                week['missed'] = week['no_pens'] = True

    # The doctor's decisions, in a word. What changed shows in the Dosage column, and in Prescription changes
    # (prescription_changes).
    for segment in segments:
        kind = _decision(segment, segments, not_started)
        if kind is None:
            continue
        prescription = segment['prescription']
        written = timezone.localdate(prescription.created_at)
        text = DECISION_WORDS[kind]
        if kind == 'changed' and prescription.dose_id:
            text = f'Dose changed to {_dose_of(prescription, with_medicine)}'
        weeks[week_of(written)]['events'].append({'text': text, 'date': written, 'by': doctor_name(prescription.prescribed_by),
                                                  'key': f'prescription-{prescription.pk}'})
    # A revoked prescription (James, 26 Nov 2026): the week it was revoked has it, with the day and who did it. It has
    # no card in Prescription changes, so its chip doesn't open them.
    for revoke in course['revoked']:
        weeks[week_of(revoke['on'])]['events'].append({
            'text': 'Prescription revoked', 'date': revoke['on'], 'key': '',
            'by': revoke['by'].titled_name if revoke['by'] else ''})  # A Super Admin may not be a doctor.

    # Week 0 runs from its first day with something in it to the day before week 1.
    zero = weeks[0]
    if zero['events'] or zero['payments'] or zero['parcels']:
        dates = [event['date'] for event in zero['events']] + [payment['due_on'] for payment in zero['payments'] if payment.get('due_on')]
        if not_started:
            dates.append(today)
        start = min([zero['date']] + [day - timedelta(days=day.weekday()) for day in dates])
        zero['date'], zero['range'] = start, range_text(start, first_week - timedelta(days=1))
        zero['current'] = not not_started and start <= today < first_week
        zero['past'] = not not_started and first_week <= today
        # Not started: paying is next. In week 0, week 1 (the first pens) is up next.
        zero['next'] = not_started
        weeks[1]['next'] = weeks[1]['next'] and not not_started
    else:
        weeks = weeks[1:]
    return {'weeks': weeks, 'total': total, 'first_week': first_week, 'ends_on': course['ends_on'],
            'not_started': not_started, 'waiting_for_doctor': waiting}


# Updates that only point the patient to their treatment page: the decision itself is listed instead.
GENERIC_EVENTS = ('Treatment authorised', 'Treatment authorisation renewed', 'Treatment approved')


def _days_of_pens(prescriptions):
    """How many days of pens prescriptions give, from their parcels: a whole dispatch interval for each parcel but the
    last, and the days from the last to the end. The weeks the doctor wrote, even after the practice moved a parcel and
    everything after it (care.prescriptions.move_with_later_parcels), which ends the prescription that much sooner or
    later. None when one has no parcels."""
    from care.prescriptions import course_parcels

    days = 0
    for prescription in prescriptions:
        parcels = list(course_parcels(Shipment.objects.filter(authorization_id=prescription.pk)).exclude(
            status=Shipment.Status.CANCELLED).order_by('scheduled_for').values_list('scheduled_for', flat=True))
        if not parcels or not prescription.dose_id:
            return None
        days += 7 * (prescription.dose.dispatch_weeks or 4) * (len(parcels) - 1) + (prescription.expires_on - parcels[-1]).days
    return days


def prescription_changes(company, patient, authorization, today=None):
    """The doctor's decisions on the treatment the patient is on, oldest first as on the timeline, for Prescription
    changes (James, 25 Nov 2026: the patient menu has no Updates page). Each is a short card: the week it was made in,
    what it was (started, renewed, changed) with the doctor and day, one line for what changed, and a few facts, as
    My Treatment shows its details: the first shipment it brought, the payment schedule and the next payment after it
    (from the care plan's payments), and how long it was prescribed for. What moved the course on its own (a first
    payment after a later cut-off, a missed payment paid late, the doctor confirming it) has a card of its own. The
    doctor's reason stays on the clinical record. Each is {'date', 'week' (of the timeline, 0 before the first pens),
    'title', 'by', 'change', 'facts': [(label, value, week)], 'key'}, where a fact's week is the timeline week of its day
    (None when it has no day: shown as a grey pill beside it); a decision's key is the one its chip on the timeline
    opens it with."""
    if authorization is None or not authorization.dose_id:
        return []
    from care.parcels import first_shipping_day
    from care.prescriptions import prescription_label

    today = today or timezone.localdate()
    course = treatment_course(authorization, today)
    segments, not_started = course['segments'], course['not_started']
    first_week = course['first_week']
    week_of = lambda day: min(course['total'], max(0, (day - first_week).days // 7 + 1))
    on = lambda day: (day_text(day), week_of(day))
    ids = {segment['prescription'].pk for segment in segments}
    plan = None if not_started else _course_plan(company, patient, list(ids) + [course['current'].pk])
    due = sorted(plan.cycles.exclude(status=SubscriptionCycle.Status.CANCELLED).values_list('due_on', flat=True)) if plan else []
    upcoming = plan.next_payment_on if plan else None

    def next_payment(after):
        """The first payment after a day: taken or due, else the one planned next."""
        return next((day for day in due if day > after), upcoming if upcoming and upcoming > after else None)

    entries = []
    for segment in segments:
        kind = _decision(segment, segments, not_started)
        prescription = segment['prescription']
        if kind is None:
            continue
        steps = sorted((other for other in segments if prescription.plan_key and other['prescription'].plan_key == prescription.plan_key),
                       key=lambda other: other['prescription'].plan_step or 0) or [segment]
        before = prescription.replaces if prescription.replaces_id else None
        mine = [prescription] if kind == 'renewed' else [step['prescription'] for step in steps]
        # As written, and as it ran: a prescription replaced by a later decision ended the day before it.
        written_to = max(item.expires_on for item in mine)
        ends = max(step['ends_on'] for step in steps) if kind != 'renewed' else segment['ends_on']
        replaced = ends < written_to and ends < today
        # Counted from its pens: it is valid from the day it was written (or first paid), its weeks from its first parcel.
        pens = course['first_parcel'] if segment is segments[0] else first_shipping_day(segment['starts_on'], company)
        weeks = max(1, round((written_to - pens).days / 7))
        if not not_started and not replaced:
            pens_days = _days_of_pens(mine)
            weeks = max(1, round(pens_days / 7)) if pens_days else weeks
        written = timezone.localdate(prescription.created_at)
        dose = prescription.dose if prescription.dose_id else None
        schedule = f'Every {dose.billing_weeks or 4} weeks' if dose else ''
        facts = []
        if kind == 'changed' and before is not None:
            title, change = 'Dose changed', f'{prescription_label(before)} → {prescription_label(prescription)}'
        elif kind == 'renewed':
            more = max(1, round((written_to - max(before.expires_on, segment['starts_on'])).days / 7)) if before else weeks
            title, change = 'Prescription renewed', f'{prescription_label(prescription)}, {more} more weeks'
        else:
            title = {'reinitiated': 'New starting plan', 'written': 'Prescription written'}.get(kind, 'Treatment started')
            later = [f'{_dose_of(step["prescription"], False)} from week {_starts_with_pens(course, step["starts_on"])["week"]}'
                     for step in steps[1:]]
            change = prescription_label(prescription) + (f', then {", then ".join(later)}' if later else '')
        if kind != 'renewed':
            sent = Shipment.objects.filter(authorization_id=prescription.pk).exclude(
                Q(status=Shipment.Status.CANCELLED) & ~Q(hold_reason__startswith='Missed: ')).order_by(
                'scheduled_for').values_list('scheduled_for', flat=True).first()
            facts.append(('First shipment', *(on(sent) if sent and not not_started else
                                              ('After your first payment' if not_started else 'Being planned', None))))
        if schedule and kind != 'renewed':
            facts.append(('Payment schedule', schedule, None))
        if kind == 'started' and due:
            facts.insert(0, ('First payment', *on(due[0])))  # Week 0, before its first shipment.
        elif kind != 'renewed' and not not_started:
            day = next_payment(written)
            if day:
                facts.append(('Next payment', *on(day)))
        facts.append(('Prescribed', f'{weeks} weeks' if not_started or replaced else f'{weeks} weeks, to {day_text(written_to, year=True)}', None)
                     if kind != 'renewed' else ('Runs to', day_text(written_to, year=True), None))
        entries.append({'date': written, 'week': week_of(written), 'title': title, 'by': doctor_name(prescription.prescribed_by),
                        'change': change, 'facts': facts, 'kind': kind, 'key': f'prescription-{prescription.pk}'})
    # What moved the course on its own, as the patient was told. (The payment days a decision changed are its facts.)
    events = PatientEvent.objects.filter(
        company=company, patient=patient, source_type=TreatmentAuthorization._meta.label_lower,
        source_id__in=[str(pk) for pk in ids], is_patient_visible=True,
    ).exclude(title__in=GENERIC_EVENTS).exclude(category=PatientEvent.Category.PAYMENT).order_by('occurred_at', 'pk')
    for event in events:
        day = timezone.localdate(event.occurred_at)
        entries.append({'date': day, 'week': week_of(day), 'title': event.title, 'by': '', 'change': event.detail,
                        'facts': [], 'kind': 'update', 'key': f'update-{event.pk}'})
    entries.sort(key=lambda entry: entry['date'])  # A decision before what followed it the same day.
    return entries


def latest_authorization(company, patient):
    return TreatmentAuthorization.objects.for_company(company).filter(patient=patient).select_related(
        'product', 'prescribed_by',
    ).order_by('-expires_on', '-pk').first()


def with_scheduled_end(appointments):
    """Each appointment's scheduled end, start + length, as scheduled_ends_at."""
    duration = ExpressionWrapper(
        Cast('duration_minutes', IntegerField()) * Value(timedelta(minutes=1)), output_field=DurationField(),
    )
    return appointments.annotate(scheduled_ends_at=ExpressionWrapper(F('starts_at') + duration, output_field=DateTimeField()))


def next_consult(request, company, patient):
    """The next booked consultation that has not finished, with its video join state."""
    now = timezone.now()
    appointment = with_scheduled_end(Appointment.objects.for_company(company).filter(
        patient=patient, status=Appointment.Status.BOOKED,
    )).filter(scheduled_ends_at__gte=now).select_related('clinician').order_by('starts_at', 'pk').first()
    if appointment is not None:
        attach_video_join([appointment], request.user.pk, allowed_role='patient', now=now)
    return appointment


def next_delivery(company, patient):
    """A parcel on its way, otherwise the next one scheduled."""
    shipments = Shipment.objects.for_company(company).filter(patient=patient)
    in_transit = shipments.filter(status=Shipment.Status.DISPATCHED, delivered_at__isnull=True).order_by('-dispatched_at', '-pk').first()
    if in_transit is not None:
        return in_transit
    return shipments.filter(
        status__in=(Shipment.Status.DRAFT, Shipment.Status.HELD, Shipment.Status.READY),
        scheduled_for__gte=timezone.localdate(),
    ).order_by('scheduled_for', 'pk').first()


def task_lab_requests(company, patient):
    """Blood tests on My Tasks: still to do, waiting for the doctor's review, and reviewed in the last two weeks."""
    reviewed_since = timezone.now() - timedelta(days=REVIEWED_TASK_DAYS)
    return list(LabRequest.objects.for_company(company).filter(patient=patient).filter(
        Q(status__in=(LabRequest.Status.REQUESTED, LabRequest.Status.DONE, LabRequest.Status.UPLOADED))
        | Q(status=LabRequest.Status.REVIEWED, reviewed_at__gte=reviewed_since),
    ).select_related('requested_by').order_by('due_on', 'requested_on', 'pk')[:5])


def medical_profile(company, patient):
    return PatientMedicalProfile.objects.for_company(company).filter(patient=patient).first()


def intake_answers(company, patient):
    """Questionnaire answers from the enquiry this patient record was created from."""
    lead = Lead.objects.for_company(company).filter(converted_patient=patient).first()
    if lead is None:
        return {}
    questionnaire = ScreeningQuestionnaire.objects.for_company(company).filter(lead=lead, stage=1).first()
    return questionnaire.answers if questionnaire else {}


def latest_weight(company, patient):
    return WeightEntry.objects.for_company(company).filter(patient=patient).order_by('-recorded_on', '-pk').first()


def format_kg(value):
    """A stored weight without trailing zeros, as patients write it: 93.60 becomes 93.6."""
    try:
        text = f'{Decimal(str(value)):f}'
    except (InvalidOperation, TypeError, ValueError):
        return value
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _decimal(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number > 0 else None


def medical_information(*, profile, intake, weight, recorded_height=None, start_weight=None):
    """Rows for the read-only Medical information card, from the patient's own record. Height and starting weight
    are the ones in their clinician's Summary data, when there are some, so a correction shows here too; the
    starting BMI comes from those two, as on the clinician's record."""
    answers = profile.answers if profile else {}
    height = _decimal(recorded_height) or _decimal(intake.get('height_cm'))
    start = _decimal(start_weight) or _decimal(intake.get('weight_kg'))
    rows = [('Height', f'{format_kg(height)} cm' if height else 'Not recorded')]
    if weight is not None:
        rows.append(('Weight', f'{format_kg(weight.weight_kg)} kg' + (f' · (started at {format_kg(start)} kg)' if start else '')))
    elif _decimal(intake.get('weight_kg')):
        rows.append(('Weight', f'{format_kg(intake["weight_kg"])} kg · from your questionnaire'))
    else:
        rows.append(('Weight', 'Not recorded'))
    if height and weight is not None:
        bmi = (weight.weight_kg / ((height / 100) ** 2)).quantize(Decimal('0.1'))
        start_bmi = (start / ((height / 100) ** 2)).quantize(Decimal('0.1')) if start else None
        rows.append(('BMI', f'{bmi}' + (f' · (started at {format_kg(start_bmi)})' if start_bmi else '')))
    elif _decimal(intake.get('bmi')):
        rows.append(('BMI', f'{_decimal(intake["bmi"]).quantize(Decimal("0.1"))} · from your questionnaire'))
    medication = answers.get('medications') or ''
    if medication:
        rows.append(('Current medication', medication))
    elif intake.get('health_context', '').strip():
        rows.append(('Medication and allergies', intake['health_context'].strip()))
    else:
        rows.append(('Current medication', 'Not recorded'))
    for key, label in (('allergies', 'Allergies'), ('medical_history', 'Medical history'), ('surgical_history', 'Previous operations')):
        rows.append((label, answers.get(key) or 'Not recorded'))
    return rows


def _task(title, body, actions, *, urgent=False, pill='', settled=False):
    """pill replaces To do / Action needed; a settled task needs nothing from the patient and isn't counted as open.
    An action with fields is a button that posts them."""
    return {'title': title, 'body': body, 'urgent': urgent, 'pill': pill, 'settled': settled,
            'actions': [{'label': label, 'url': url, 'primary': primary, 'post': post[0] if post else None}
                        for label, url, primary, *post in actions]}


def billing_tasks(plan):
    """What a subscription's billing needs from the patient: a card to add or confirm, or a payment that
    didn't go through. Most urgent first, like the rest."""
    from care.billing import patient_summary

    if plan is None or plan.status not in ('active', 'payment_retry'):
        return []
    summary = patient_summary(plan)
    if not summary['action']:
        return []
    details = {
        'no_mandate': 'Your parcels go out once each payment is in. Add your card to start.',
        'pending': 'You started adding your card but it isn’t saved yet. Add it again to finish.',
        'update': 'Your treatment or its price changed. Confirm your card for the new amount so your parcels keep going out.',
        'failed': 'Your next parcel waits until it’s paid. Pay now, or use a different card if yours has changed.',
        'retrying': 'We’ll try again soon. If your card has changed, use a different one so it goes through.',
    }
    return [_task(summary['text'], details.get(summary['state'], ''),
                  [(summary['action'], reverse('portal:patient-billing'), True)], urgent=summary['state'] != 'pending')]


def patient_tasks(*, authorization, plan, consult, weight, labs, lab_context=None):
    """What needs doing, most urgent first. Each task disappears once its record changes. lab_context(lab) signs the
    Done button of blood tests still to do."""
    today = timezone.localdate()
    book_url = reverse('portal:patient-appointments') + '?book=1'
    tasks = []
    if authorization is not None and consult is None and (authorization.expires_on - today).days <= RENEWAL_WINDOW_DAYS:
        expires = date_format(authorization.expires_on, 'j F')
        tasks.append(_task(
            f'Book a follow-up consult before {expires} to keep your treatment running',
            f'Your authorisation for {authorization.product} ends on {expires}. Your follow-up consult with '
            f'{authorization.prescribed_by.full_name} is where it can be renewed. If it lapses, deliveries are held '
            'until your doctor renews it.',
            [('Book a follow-up consult', book_url, True)], urgent=True,
        ))
    elif authorization is None and plan is not None and consult is not None:
        # Already booked (James, 26 Nov 2026): nothing to do but go to it, so no Book a consult.
        when = timezone.localtime(consult.starts_at)
        tasks.append(_task(
            'Your treatment authorisation has ended',
            f'Deliveries on your plan are held until your doctor renews it. {consult.clinician.titled_name} can review '
            f'your treatment at your consult on {date_format(when, "l j F")} at {when:%H:%M}.',
            [], pill='Consult booked', settled=True,
        ))
    elif authorization is None and plan is not None:
        tasks.append(_task(
            'Your treatment authorisation has ended',
            'Deliveries on your plan are held until your doctor renews your authorisation. Book a consult so they '
            'can review your treatment.',
            [('Book a consult', book_url, True)], urgent=True,
        ))
    # Blood tests: Done once the patient has had them, then the doctor's review (care.blood_tests).
    from care.blood_tests import pauses_on

    for lab in labs:
        doctor, url = lab.requested_by.titled_name, reverse('portal:patient-lab-detail', args=[lab.pk])
        if lab.status == LabRequest.Status.REVIEWED:
            tasks.append(_task(
                'Your blood tests have been reviewed',
                f'{doctor} reviewed your blood tests ({lab.panel_name}) on {date_format(timezone.localtime(lab.reviewed_at), "j F")}.',
                [('View', url, False)], pill='Reviewed', settled=True,
            ))
        elif lab.status in (LabRequest.Status.DONE, LabRequest.Status.UPLOADED):
            tasks.append(_task(
                'Your blood tests are waiting for review',
                f'You’ve had your blood tests ({lab.panel_name}). {doctor} has been asked to review them.',
                [('View', url, False)], pill='Review pending', settled=True,
            ))
        else:
            due = (f' Please have them done by {date_format(lab.due_on, "j F")}. If they aren’t done and reviewed by '
                   f'{date_format(pauses_on(lab), "j F")}, your subscription and deliveries are paused until they are.'
                   if lab.due_on else '')
            actions = [('View request', url, False)]
            if lab_context is not None:
                actions.insert(0, ('Mark as done', url, True, {'clinical_context': lab_context(lab), 'action': 'done', 'next': 'tasks'}))
            tasks.append(_task(
                'Get your blood tests done',
                f'{doctor} asked for {lab.panel_name} on {date_format(lab.requested_on, "j F")}.{due} Once you’ve had '
                'them, mark them Done so your doctor can review them.',
                actions, urgent=bool(lab.due_on and lab.due_on <= today),
            ))
    if weight is None or (today - weight.recorded_on).days >= WEIGHT_PROMPT_DAYS:
        tasks.append(_task(
            'Log this week’s weight',
            'It goes onto the chart your doctor reads at your follow-up consults.',
            [('Log my weight', reverse('portal:patient-dashboard') + '#weight-log', True)],
        ))
    return tasks
