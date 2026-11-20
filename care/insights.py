"""Insights for the practice's Super Admin (Dr Czech, Nov 2026).

Month by month, over all time or a chosen period: bookings and where they came from, new subscriptions, recurring
revenue, how long subscriptions last and what they are worth, dropouts, each doctor's consults and signups, and weight
lost. Cohorts: of everyone whose subscription started in a month, how many are still subscribed 1, 2, 3, 6 and 12 months
on. Demo patients are always left out. A month with a figure from the practice's spreadsheet (InsightFigure, from
before the app) uses it in place of the app's own count for that month and measure.

Definitions:
- A booking is an appointment that wasn't cancelled, counted in the month it is for. New is an initial consultation;
  Follow-up is every other kind (follow-up, six-month review, ad-hoc).
- Sources come from How did you hear about us? on the screening form, for enquiries that went on to book.
- A signup is a subscription, counted in the month it starts; its medicine is the one it was prescribed for.
- Monthly recurring revenue is, at the end of each month (today, for this month), what the subscriptions paid up to
  then bring in a month: each paid cycle's amount scaled from its length to a month. One whose next debit is still
  within its week to be collected counts at its last cycle's amount.
- A subscription lasts as long as its paid cycles cover (months of successful billing). Average lifetime value is the
  average length of the subscriptions that ended times the average revenue per subscriber per month.
- A dropout is a cancelled subscription, in the month it was cancelled. Churn is the month's dropouts as a share of
  the subscriptions running when the month began.
- Still subscribed k months on: paid up to at least k months after starting. A cohort is only measured at month k
  once all of it has reached k months and a week has passed to collect the debit, so young cohorts aren't shown as
  lost, and years are compared at the same age.
- Weight lost: from the weight on the screening form (or the first one logged) to the latest one logged.
"""

import calendar
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import (Appointment, InsightFigure, InsightNote, Lead, PatientSubscription, ScreeningQuestionnaire,
                     SubscriptionCycle, WeightEntry)
from .reporting import REPORT_TIMEZONE, reporting_today

MONTH_DAYS = 30.4375
MEDICINES = (('semaglutide', 'Semaglutide'), ('tirzepatide', 'Tirzepatide'))
SOURCES = Lead.HEARD_FROM
SOURCE_METRICS = {key: f'source_{key}' for key, _ in SOURCES}
MILESTONES = (1, 3, 6, 12)
# Days after a cohort reaches a month for its debits to be collected, before it is measured there.
COLLECTION_DAYS = 7


def first_of(day):
    return day.replace(day=1)


def add_months(day, months):
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def last_of(month):
    return add_months(first_of(month), 1) - timedelta(days=1)


def month_range(first, last):
    months, month = [], first_of(first)
    while month <= first_of(last):
        months.append(month)
        month = add_months(month, 1)
    return months


def local_day(moment):
    return timezone.localtime(moment, REPORT_TIMEZONE).date()


def medicine_of(name):
    """Semaglutide (Semagard, Wegovy) or Tirzepatide, from a product's name; None for anything else."""
    name = (name or '').lower()
    if 'tirzepatide' in name or 'mounjaro' in name:
        return 'tirzepatide'
    if any(word in name for word in ('semaglutide', 'semagard', 'wegovy', 'ozempic')):
        return 'semaglutide'
    return None


def person(user):
    return getattr(user, 'titled_name', '') or user.get_full_name() or user.email


def subscriptions(company):
    """Every real subscription: when it started and ended, its medicine and prescriber, and its paid cycles."""
    from .billing import DEFAULT_WEEKS

    queryset = (PatientSubscription.objects.for_company(company)
                .filter(patient__company=company, patient__demo_data_set__isnull=True)
                .select_related('authorization__product').order_by('starts_on', 'pk'))
    paid = defaultdict(list)
    for cycle in (SubscriptionCycle.objects.for_company(company).filter(subscription__in=queryset, status=SubscriptionCycle.Status.PAID)
                  .values('subscription_id', 'starts_on', 'ends_on', 'due_on', 'amount')):
        end = cycle['ends_on'] or cycle['starts_on'] + timedelta(weeks=DEFAULT_WEEKS, days=-1)
        paid[cycle['subscription_id']].append({'start': cycle['starts_on'], 'end': end, 'due': cycle['due_on'],
                                               'amount': cycle['amount'], 'days': max(1, (end - cycle['starts_on']).days + 1)})
    rows = []
    for subscription in queryset:
        authorization = subscription.authorization
        cycles = sorted(paid.get(subscription.pk, ()), key=lambda cycle: cycle['start'])
        paid_through = max((cycle['end'] for cycle in cycles), default=None)
        ended = None
        if subscription.status == PatientSubscription.Status.CANCELLED:
            ended = local_day(subscription.cancelled_at) if subscription.cancelled_at else (paid_through or subscription.starts_on)
        rows.append({
            'pk': subscription.pk, 'patient': subscription.patient_id, 'start': subscription.starts_on, 'status': subscription.status,
            'medicine': medicine_of(authorization.product.name if authorization else subscription.plan_name),
            'prescriber': authorization.prescribed_by_id if authorization else None,
            'ended': ended, 'cycles': cycles, 'paid_through': paid_through,
            'paid_days': sum(cycle['days'] for cycle in cycles), 'paid_amount': sum((cycle['amount'] for cycle in cycles), Decimal('0')),
        })
    return rows


def retained(subscription, months):
    """Still subscribed this many months after starting: paid up to then."""
    if months == 0:
        return True
    return subscription['paid_through'] is not None and subscription['paid_through'] >= add_months(subscription['start'], months)


def monthly_revenue(subscription, day):
    """What the subscription brings in a month, from the paid cycle covering this day, or its last one for the week its
    next debit has to be collected (unless it was cancelled); 0 otherwise."""
    for cycle in subscription['cycles']:
        if cycle['start'] <= day <= cycle['end']:
            return float(cycle['amount']) * MONTH_DAYS / cycle['days']
    last = subscription['cycles'][-1] if subscription['cycles'] else None
    if (last and last['end'] < day <= last['end'] + timedelta(days=COLLECTION_DAYS)
            and (subscription['ended'] is None or subscription['ended'] > day)):
        return float(last['amount']) * MONTH_DAYS / last['days']
    return 0.0


def figures(company):
    return {(figure.month, figure.metric): figure.value for figure in InsightFigure.objects.for_company(company)}


def notes(company):
    return [{'id': note.pk, 'month': f'{note.on:%Y-%m}', 'on': note.on.isoformat(), 'label': note.label}
            for note in InsightNote.objects.for_company(company)]


def _sources(company):
    """(day, [sources]) for each enquiry that went on to book, from its screening form."""
    rows = []
    for submitted, answers in (ScreeningQuestionnaire.objects.for_company(company)
                               .filter(stage=1, submitted_at__isnull=False, lead__converted_patient__isnull=False,
                                       lead__converted_patient__demo_data_set__isnull=True)
                               .values_list('submitted_at', 'answers')):
        picked = (answers or {}).get('heard_from') or []
        rows.append((local_day(submitted), [key for key in picked if key in SOURCE_METRICS]))
    return rows


def _weights(company):
    """Each real patient's logged weights, oldest first, and the weight on their screening form."""
    logged = defaultdict(list)
    for patient, day, kg in (WeightEntry.objects.for_company(company).filter(patient__company=company, patient__demo_data_set__isnull=True)
                             .order_by('recorded_on').values_list('patient_id', 'recorded_on', 'weight_kg')):
        logged[patient].append((day, float(kg)))
    screened = {}
    for patient, answers in (ScreeningQuestionnaire.objects.for_company(company)
                             .filter(stage=1, lead__converted_patient__isnull=False, lead__converted_patient__company=company,
                                     lead__converted_patient__demo_data_set__isnull=True)
                             .values_list('lead__converted_patient_id', 'answers')):
        try:
            screened[patient] = float((answers or {}).get('weight_kg'))
        except (TypeError, ValueError):
            pass
    return logged, screened


def weight_lost(company):
    """Kilograms lost by every patient with a starting and a later weight, together: (kg, patients)."""
    logged, screened = _weights(company)
    total, patients = 0.0, 0
    for patient in set(logged) | set(screened):
        entries = logged.get(patient, [])
        start = screened.get(patient, entries[0][1] if entries else None)
        if start is None or not entries or (patient not in screened and len(entries) < 2):
            continue
        total += start - entries[-1][1]
        patients += 1
    return round(total, 1), patients


def weight_by_month(company, subs, *, months=12):
    """Average kilograms lost by month of subscription (1 to 12) for each medicine, from each patient's first
    subscription to it: the weight at its start (the latest logged before it, else the screening form's, else the first
    logged in its first fortnight) to the last logged in each month of it. [(medicine, [(kg, patients)...])]."""
    logged, screened = _weights(company)
    firsts = {}
    for subscription in subs:
        if subscription['medicine']:
            firsts.setdefault((subscription['patient'], subscription['medicine']), subscription['start'])
    losses = {key: [[] for _ in range(months + 1)] for key, _ in MEDICINES}
    for (patient, medicine), start in firsts.items():
        entries = logged.get(patient, [])
        before = [kg for day, kg in entries if day <= start]
        early = [kg for day, kg in entries if start < day <= start + timedelta(days=14)]
        baseline = before[-1] if before else screened.get(patient, early[0] if early else None)
        if baseline is None:
            continue
        for month in range(1, months + 1):
            lower, upper = add_months(start, month - 1), add_months(start, month)
            inside = [kg for day, kg in entries if lower < day <= upper]
            if inside:
                losses[medicine][month].append(baseline - inside[-1])
    return [(key, [(0.0, len([1 for (patient, medicine) in firsts if medicine == key]))] + [
        (round(sum(values) / len(values), 1) if values else None, len(values)) for values in losses[key][1:]])
        for key, _ in MEDICINES]


def overview(company, *, start=None, end=None, today=None):
    """Everything on the Insights tab for the months from start to end (all time without them). Counts take only the
    days from start to end, so the first and last months can be part months; without an end, the current month counts
    all of it, so its bookings later in the month are in. A month's figure from the spreadsheet is the whole month's."""
    today = today or reporting_today()
    subs = subscriptions(company)
    entered = figures(company)
    appointments = list(Appointment.objects.for_company(company)
                        .filter(patient__company=company, patient__demo_data_set__isnull=True)
                        .exclude(status=Appointment.Status.CANCELLED)
                        .values_list('starts_at', 'appointment_type', 'status', 'clinician_id'))
    appointments = [(local_day(at), kind, status, clinician) for at, kind, status, clinician in appointments]
    sources = _sources(company)
    known = [month for month, _ in entered] + [day for day, *_ in appointments] + [s['start'] for s in subs] + [day for day, _ in sources]
    first = first_of(start) if start else first_of(min(known, default=today))
    last = first_of(min(end, today) if end else today)
    months = month_range(first, max(first, last))
    keys = [month.isoformat()[:7] for month in months]
    index = {month: position for position, month in enumerate(months)}
    upper = min(end, today) if end else None

    def place(day):
        """The month a day is counted in, or None when it's outside the period."""
        if (start and day < start) or (upper and day > upper):
            return None
        return index.get(first_of(day))

    def series():
        return [0] * len(months)

    # 1. Bookings.
    new, follow_up = series(), series()
    for day, kind, status, clinician in appointments:
        position = place(day)
        if position is not None:
            (new if kind == Appointment.Type.INITIAL else follow_up)[position] += 1
    for position, month in enumerate(months):
        new[position] = entered.get((month, InsightFigure.Metric.NEW_BOOKINGS), new[position])
    bookings = {'all': [a + b for a, b in zip(new, follow_up)], 'new': new, 'follow_up': follow_up}

    # 2. Sources of bookings.
    by_source = {key: series() for key, _ in SOURCES}
    for day, picked in sources:
        position = place(day)
        if position is not None:
            for key in picked:
                by_source[key][position] += 1
    for key, metric in SOURCE_METRICS.items():
        for position, month in enumerate(months):
            by_source[key][position] = entered.get((month, metric), by_source[key][position])

    # 3. New subscriptions.
    signups = {'all': series(), **{key: series() for key, _ in MEDICINES}}
    for subscription in subs:
        position = place(subscription['start'])
        if position is not None:
            signups['all'][position] += 1
            if subscription['medicine']:
                signups[subscription['medicine']][position] += 1

    # 4. Monthly recurring revenue, and 7. dropouts and churn.
    mrr, dropouts, running, churn = [], series(), series(), []
    for position, month in enumerate(months):
        as_of = min(last_of(month), today)
        mrr.append(round(sum(monthly_revenue(subscription, as_of) for subscription in subs)))
        for subscription in subs:
            if subscription['start'] < month and (subscription['ended'] is None or subscription['ended'] >= month):
                running[position] += 1
            if subscription['ended'] and place(subscription['ended']) == position:
                dropouts[position] += 1
        churn.append(round(100 * dropouts[position] / running[position], 1) if running[position] else None)

    # 9. Each doctor's consults and signups.
    consults = defaultdict(lambda: defaultdict(int))
    for day, kind, status, clinician in appointments:
        position = place(day)
        if position is not None and status != Appointment.Status.NO_SHOW:
            consults[clinician][position] += 1
    prescribed = defaultdict(lambda: defaultdict(int))
    for subscription in subs:
        position = place(subscription['start'])
        if position is not None and subscription['prescriber'] and subscription['medicine']:
            prescribed[(subscription['prescriber'], subscription['medicine'])][position] += 1
    doctor_ids = set(consults) | {doctor for doctor, _ in prescribed}
    doctors = [{'id': user.pk, 'name': person(user)} for user in
               get_user_model().objects.filter(pk__in=doctor_ids).order_by('first_name', 'last_name', 'pk')]
    doctor_months = [{'month': month, 'cells': [{
        'consults': consults[doctor['id']][position],
        **{key: prescribed[(doctor['id'], key)][position] for key, _ in MEDICINES}} for doctor in doctors]}
        for position, month in reversed(list(enumerate(months)))]

    # 5 and 6. How long subscriptions last, and what one is worth.
    period_start, period_end = start or months[0], min(upper or last_of(months[-1]), today)
    ended = [s for s in subs if s['ended'] and period_start <= s['ended'] <= period_end]
    duration = round(sum(s['paid_days'] for s in ended) / len(ended) / MONTH_DAYS, 1) if ended else None
    cycles = [cycle for s in subs for cycle in s['cycles'] if period_start <= cycle['due'] <= period_end]
    paid_months = sum(cycle['days'] for cycle in cycles) / MONTH_DAYS
    arpu = round(float(sum(cycle['amount'] for cycle in cycles)) / paid_months) if paid_months else None
    rates = [rate for rate in churn if rate is not None]
    running_start = sum(1 for s in subs if s['start'] < period_start and (s['ended'] is None or s['ended'] >= period_start))

    kg, weighed = weight_lost(company)
    return {
        'months': months, 'keys': keys, 'first': months[0], 'last': months[-1], 'all_time': not start and not end,
        'bookings': bookings, 'sources': by_source, 'signups': signups, 'mrr': mrr,
        'dropouts': dropouts, 'running': running, 'churn': churn,
        'doctors': doctors, 'doctor_months': doctor_months,
        'doctor_consults': {doctor['id']: [consults[doctor['id']][position] for position in range(len(months))] for doctor in doctors},
        'weight_by_month': weight_by_month(company, subs),
        'kpis': {
            'kg_lost': kg, 'weighed': weighed,
            'active': sum(1 for s in subs if s['status'] != PatientSubscription.Status.CANCELLED),
            'mrr': mrr[-1] if mrr else 0,
            'duration': duration, 'duration_count': len(ended),
            'arpu': arpu, 'ltv': round(duration * arpu) if duration is not None and arpu is not None else None,
            'dropouts': len(ended), 'churn': round(sum(rates) / len(rates), 1) if rates else None,
            # The period's dropouts as a share of the subscriptions running when it began (none for all time).
            'running_start': running_start,
            'period_churn': round(100 * sum(dropouts) / running_start, 1) if running_start else None,
            'period_dropouts': sum(dropouts),
            'signups': sum(signups['all']), 'bookings': sum(bookings['all']),
        },
        'notes': notes(company),
    }


def cohorts(company, *, medicine=None, today=None):
    """Cohort retention for Cohorts: each signup month's share still subscribed 0 to 12 (or 24) months on, the same
    pooled by signup year (weighted by cohort size), and 1, 3, 6 and 12 months on across every month's cohort."""
    today = today or reporting_today()
    subs = [s for s in subscriptions(company) if medicine is None or s['medicine'] == medicine]
    groups = defaultdict(list)
    for subscription in subs:
        groups[first_of(subscription['start'])].append(subscription)
    if not groups:
        return {'rows': [], 'quarters': [], 'years': [], 'milestones': [], 'horizon': 12, 'kpis': {}, 'notes': notes(company)}

    def measured(month, months, *, through=None):
        """Every subscriber in a cohort (through its last month) has reached this month, with a week to collect."""
        return months == 0 or add_months(through or month, months + 1) + timedelta(days=COLLECTION_DAYS) <= today

    longest = max((k for month in groups for k in range(25) if measured(month, k)), default=0)
    horizon = 24 if longest > 12 else 12

    def share(members, months):
        return round(100 * sum(1 for s in members if retained(s, months)) / len(members), 1) if members else None

    cohort_months = month_range(min(groups), first_of(today))
    rows = [{'month': month, 'size': len(groups.get(month, [])),
             'cells': [share(groups.get(month, []), k) if measured(month, k) else None for k in range(horizon + 1)]}
            for month in cohort_months]

    quarters = []
    for month in cohort_months:
        if (month.month - 1) % 3:
            continue
        span = [month, add_months(month, 1), add_months(month, 2)]
        members = [s for part in span for s in groups.get(part, [])]
        quarters.append({'month': month, 'label': f'Q{(month.month - 1) // 3 + 1} {month.year}', 'size': len(members),
                         'cells': [share(members, k) if measured(month, k, through=span[-1]) else None for k in range(horizon + 1)]})
    if cohort_months[0].month % 3 != 1:
        # The first quarter only partly in range: pool what there is.
        start = date(cohort_months[0].year, (cohort_months[0].month - 1) // 3 * 3 + 1, 1)
        span = [m for m in (start, add_months(start, 1), add_months(start, 2))]
        members = [s for part in span for s in groups.get(part, [])]
        quarters.insert(0, {'month': start, 'label': f'Q{(start.month - 1) // 3 + 1} {start.year}', 'size': len(members),
                            'cells': [share(members, k) if measured(start, k, through=span[-1]) else None for k in range(horizon + 1)]})

    years = []
    for year in sorted({month.year for month in groups}):
        values = []
        for k in range(horizon + 1):
            members = [s for month, group in groups.items() if month.year == year and measured(month, k) for s in group]
            values.append(share(members, k))
        size = sum(len(group) for month, group in groups.items() if month.year == year)
        years.append({'year': year, 'size': size, 'values': values})

    milestones = [{'months': k, 'values': [row['cells'][k] if k <= horizon and row['size'] else None for row in rows]}
                  for k in MILESTONES]

    def pooled(k):
        members = [s for month, group in groups.items() if measured(month, k) for s in group]
        return share(members, k), len(members)

    return {'rows': rows, 'quarters': quarters, 'years': years, 'milestones': milestones, 'horizon': horizon,
            'months': cohort_months, 'keys': [month.isoformat()[:7] for month in cohort_months],
            'kpis': {k: pooled(k) for k in (1, 3, 6, 12)}, 'size': len(subs), 'notes': notes(company)}
