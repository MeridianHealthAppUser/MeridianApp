"""Draft parcels from prescriptions.

When a doctor saves a prescription for a catalogue dose, its parcels go on the Weekly shipping list as drafts: on the
practice's shipping day, the first whose order cut-off (Practice settings) is on or after the prescription starts and
then one every dispatch interval while it runs, each with the dose's pen and pens. A renewal carries on the same rhythm; a change replaces the old drafts.
Operations check the drafts (Mark ready), change one if needed and lock the week. Nothing is sent until a parcel is
dispatched, and a parcel is only ever planned, never sent, by this module.
"""

from datetime import datetime, time, timedelta

from django.db.models import Min
from .demo_safety import require_same_demo_data

from django.utils import timezone

from .models import PatientSubscription, PracticeSettings, Shipment, ShipmentItem, TreatmentAuthorization
from .operations import dose_run


def shipping_weekday(company):
    weekday = PracticeSettings.objects.filter(company=company).values_list('shipping_weekday', flat=True).first()
    return PracticeSettings.Weekday.TUESDAY if weekday is None else weekday


def shipping_day_from(day, weekday):
    """The first shipping day on or after day."""
    return day + timedelta(days=(weekday - day.weekday()) % 7)


# Repeat payments are taken a week before each parcel (James, 25 Nov 2026): on the shipping weekday, so a card that
# fails is tried again by the order cut-off and the parcel still makes its order (care.billing._retry_on).
PAYMENT_LEAD = timedelta(weeks=1)
# The order cut-off without Practice settings (Dr Czech, 26 Nov 2026): Monday at 15:00 for Tuesday parcels.
DEFAULT_CUTOFF_WEEKDAY = PracticeSettings.Weekday.MONDAY
DEFAULT_CUTOFF_TIME = time(15, 0)


def cutoff_days(company):
    """How many days before a shipping day its order cut-off is (Practice settings: Monday by default, the day before
    a Tuesday). A cut-off on the shipping weekday is the week before's."""
    row = PracticeSettings.objects.filter(company=company).values('shipping_weekday', 'order_cutoff_weekday').first()
    shipping = PracticeSettings.Weekday.TUESDAY if row is None else row['shipping_weekday']
    cutoff = DEFAULT_CUTOFF_WEEKDAY if row is None else row['order_cutoff_weekday']
    return (shipping - cutoff) % 7 or 7


def cutoff_time(company):
    """The time on the cut-off day the pens are ordered (15:00 by default)."""
    value = PracticeSettings.objects.filter(company=company).values_list('order_cutoff_time', flat=True).first()
    return DEFAULT_CUTOFF_TIME if value is None else value


def _moment(when):
    """(day, time of day) for when: a datetime, or a date. Today means now; another day counts from its start, as the
    morning's debit run takes the payments due that day."""
    if isinstance(when, datetime):
        local = timezone.localtime(when) if timezone.is_aware(when) else when
        return local.date(), local.time()
    if when == timezone.localdate():
        return when, timezone.localtime().time()
    return when, time.min


def first_shipping_day(when, company):
    """The first shipping day whose order cut-off is still to come at when (a date, today meaning now, or a datetime):
    the day the pens for a payment (or a prescription) then go out. With Tuesday parcels and a Monday 15:00 cut-off, a
    payment from Tuesday to Monday 14:59 has its parcel the next Tuesday; one on Monday from 15:00, the Tuesday after."""
    day, at = _moment(when)
    days = cutoff_days(company)
    parcel = shipping_day_from(day + timedelta(days=days), shipping_weekday(company))
    if parcel - timedelta(days=days) == day and at >= cutoff_time(company):
        parcel += timedelta(weeks=1)  # Today is its cut-off, and the time has passed.
    return parcel


def order_cutoff(parcel_day, company):
    """The day of the order cut-off for a parcel on parcel_day (a shipping day): a payment received by its cut-off time
    that day goes out then."""
    return parcel_day - timedelta(days=cutoff_days(company))


def order_cutoff_text(parcel_day, company):
    """The order cut-off for a parcel on parcel_day, as the app shows it: "Mon, 30 Nov at 15:00"."""
    from django.utils.formats import date_format

    return f'{date_format(order_cutoff(parcel_day, company), "D, j M")} at {cutoff_time(company):%H:%M}'


def first_parcel_day(authorization):
    """The first shipping day a dose's pens go out on, by itself: a course's first step (valid from its first payment)
    or a changed dose (from the day the doctor writes it) on the first shipping day whose order cut-off is on or after
    it starts; a starting plan's later step on the first shipping day from its start, and never before the pens of the
    step before it run out."""
    if (authorization.plan_step or 1) <= 1:
        return first_shipping_day(authorization.starts_on, authorization.company_id)
    day = shipping_day_from(authorization.starts_on, shipping_weekday(authorization.company_id))
    before = TreatmentAuthorization.objects.filter(
        company_id=authorization.company_id, patient_id=authorization.patient_id, plan_key=authorization.plan_key,
        plan_step=authorization.plan_step - 1).exclude(status=TreatmentAuthorization.Status.CANCELLED).select_related('dose').first()
    last = before and before.dose_id and Shipment.objects.filter(authorization=before).exclude(
        status=Shipment.Status.CANCELLED).order_by('-scheduled_for').values_list('scheduled_for', flat=True).first()
    return max(day, last + timedelta(weeks=before.dose.dispatch_weeks or 4)) if last else day


def treatment_started_on(authorization):
    """When the treatment a parcel belongs to started, to number its parcels: a starting plan's first step, or the
    prescription its renewals carry on."""
    first = dose_run(authorization)[-1]
    if first.plan_key:
        return TreatmentAuthorization.objects.filter(
            company_id=first.company_id, patient_id=first.patient_id, plan_key=first.plan_key,
        ).aggregate(start=Min('starts_on'))['start']
    return first.starts_on


def schedule_parcels(authorization):
    """Add the draft parcels a prescription (or a starting plan's step) still needs, from today until it ends.

    Each dose's first parcel goes on the first shipping day on or after it starts (a course starts with its first
    pens: care.prescriptions.start_course_on), and every later one a whole dispatch interval after it. Nothing new is
    planned for a shipping day whose order cut-off has passed (its pens are already ordered): a change the doctor makes
    today has its first parcel on the first shipping day whose cut-off is still to come (first_shipping_day).
    """
    require_same_demo_data(authorization.patient, authorization.product)
    if not authorization.dose_id:
        return []
    interval = timedelta(weeks=authorization.dose.dispatch_weeks)
    today = timezone.localdate()
    earliest = first_shipping_day(today, authorization.company)
    last = Shipment.objects.filter(
        company_id=authorization.company_id, patient_id=authorization.patient_id, authorization__in=dose_run(authorization),
    ).exclude(status=Shipment.Status.CANCELLED).order_by('-scheduled_for').values_list('scheduled_for', flat=True).first()
    day = last + interval if last else first_parcel_day(authorization)
    if last and day < earliest:
        # Renewed after the order for the next parcel went in (or after the pens ran out): the first shipping day it
        # can make, with the payment that starts again today (care.prescriptions._review_debit_orders).
        day = earliest
    skipped = 0  # Parcels a prescription that started in the past would already have had.
    while day < today:
        day += interval
        skipped += 1
    if not skipped and day < earliest:
        day = earliest  # Decided today, after its order went in: the shipping day after.
    plan = PatientSubscription.objects.filter(
        company_id=authorization.company_id, patient_id=authorization.patient_id,
    ).exclude(status=PatientSubscription.Status.CANCELLED).order_by('-created_at', '-pk').first()
    started = treatment_started_on(authorization)
    parcels = []
    # A parcel on the last day would be for the weeks after it, unless it is the only one a short step gets.
    while day < authorization.expires_on or (day == authorization.expires_on and not parcels and last is None):
        # Parcel N of the treatment, whatever the dose's interval: one after the parcel before it (which may have been
        # recorded with its number, as a moved patient's is), or the first, after any the start date already passed.
        previous = Shipment.objects.filter(
            company_id=authorization.company_id, patient_id=authorization.patient_id, authorization__isnull=False,
            scheduled_for__gte=started, scheduled_for__lt=day,
        ).exclude(status=Shipment.Status.CANCELLED).order_by('-scheduled_for', '-pk').values_list('cycle_number', flat=True).first()
        number = previous + 1 if previous else skipped + 1
        parcel = Shipment.objects.create(
            company_id=authorization.company_id, patient_id=authorization.patient_id, subscription=plan,
            authorization=authorization, cycle_number=number, scheduled_for=day, status=Shipment.Status.DRAFT)
        ShipmentItem.objects.create(company_id=authorization.company_id, shipment=parcel, product_id=authorization.product_id,
                                    dose=authorization.max_dose, quantity=authorization.quantity_per_cycle)
        parcels.append(parcel)
        day += interval
    return parcels
