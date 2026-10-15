"""Draft parcels from prescriptions.

When a doctor saves a prescription for a catalogue dose, its parcels go on the Weekly shipping list as drafts: on the
practice's shipping day, the first on or after the prescription starts and then one every dispatch interval while it
runs, each with the dose's pen and pens. A renewal carries on the same rhythm; a change replaces the old drafts.
Operations check the drafts (Mark ready), change one if needed and lock the week. Nothing is sent until a parcel is
dispatched, and a parcel is only ever planned, never sent, by this module.
"""

from datetime import timedelta

from django.db.models import Min
from django.utils import timezone

from .models import PatientSubscription, PracticeSettings, Shipment, ShipmentItem, TreatmentAuthorization
from .operations import dose_run


def shipping_weekday(company):
    weekday = PracticeSettings.objects.filter(company=company).values_list('shipping_weekday', flat=True).first()
    return PracticeSettings.Weekday.TUESDAY if weekday is None else weekday


def shipping_day_from(day, weekday):
    """The first shipping day on or after day."""
    return day + timedelta(days=(weekday - day.weekday()) % 7)


def treatment_started_on(authorization):
    """When the treatment a parcel belongs to started, to number its month: a starting plan's first step, or the
    prescription its renewals carry on."""
    first = dose_run(authorization)[-1]
    if first.plan_key:
        return TreatmentAuthorization.objects.filter(
            company_id=first.company_id, patient_id=first.patient_id, plan_key=first.plan_key,
        ).aggregate(start=Min('starts_on'))['start']
    return first.starts_on


def schedule_parcels(authorization):
    """Add the draft parcels a prescription (or a starting plan's step) still needs, from today until it ends.

    Each parcel goes out within its own supply period (care.operations.allowance_window): the first shipping day on
    or after the dose starts is less than a week in, and every later one is a whole dispatch interval after it.
    """
    if not authorization.dose_id:
        return []
    interval = timedelta(weeks=authorization.dose.dispatch_weeks)
    today = timezone.localdate()
    last = Shipment.objects.filter(
        company_id=authorization.company_id, patient_id=authorization.patient_id, authorization__in=dose_run(authorization),
    ).exclude(status=Shipment.Status.CANCELLED).order_by('-scheduled_for').values_list('scheduled_for', flat=True).first()
    day = last + interval if last else shipping_day_from(authorization.starts_on, shipping_weekday(authorization.company))
    while day < today:
        day += interval
    plan = PatientSubscription.objects.filter(
        company_id=authorization.company_id, patient_id=authorization.patient_id,
    ).exclude(status=PatientSubscription.Status.CANCELLED).order_by('-created_at', '-pk').first()
    started = treatment_started_on(authorization)
    parcels = []
    # A parcel on the last day would be for the weeks after it, unless it is the only one a short step gets.
    while day < authorization.expires_on or (day == authorization.expires_on and not parcels and last is None):
        parcel = Shipment.objects.create(
            company_id=authorization.company_id, patient_id=authorization.patient_id, subscription=plan,
            authorization=authorization, cycle_number=(day - started).days // 28 + 1, scheduled_for=day,
            status=Shipment.Status.DRAFT)
        ShipmentItem.objects.create(company_id=authorization.company_id, shipment=parcel, product_id=authorization.product_id,
                                    dose=authorization.max_dose, quantity=authorization.quantity_per_cycle)
        parcels.append(parcel)
        day += interval
    return parcels
