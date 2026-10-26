"""Audited dispatch operations for the weekly shipping list.

Pens aren't kept in stock: once a week's parcels are ready and the week is locked, just enough is ordered for them
(week_order), and the parcels are dispatched. Nothing waits on stock, and each parcel notes its batch at dispatch.

Marking a parcel ready checks its prescription, care plan and supply allowance. Locking the week checks those again
and what the courier and the practice need: a delivery address and contact number (care.addresses), and payment
(care.billing). Waybills are booked with The Courier Guy for locked parcels (care.courier).
"""

from collections import defaultdict
from datetime import date, datetime, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient
from practices.tenancy import require_enabled_company

from .billing import parcel_payment_reason
from .models import (
    DeliveryAddress, MedicationProduct, PatientEvent, PatientSubscription, PharmacyOrder, PracticeSettings, Shipment,
    ShipmentItem, TreatmentAuthorization,
)
from .services import record_audit
from .treatment import authorization_is_current, ensure_subscription_eligible


UNDISPATCHED = (Shipment.Status.DRAFT, Shipment.Status.READY, Shipment.Status.HELD)
# How long one dispatch lasts for an authorisation that isn't for a catalogue dose.
DEFAULT_SUPPLY_DAYS = 28


def doctors_ship(company):
    """Whether the practice's doctors use Weekly shipping and Dispatch history (Practice settings; on by default)."""
    value = PracticeSettings.objects.filter(company=company).values_list('doctors_ship', flat=True).first()
    return True if value is None else value


def can_ship(company, membership):
    """Practice administrators and Super Admins, and doctors when the practice lets doctors ship."""
    return membership is not None and (membership.is_admin or (
        membership.role == CompanyMembership.Role.DOCTOR and doctors_ship(company)))


def require_operations_actor(company, actor, *, catalogue=False, shipping=False):
    """Practice administrators and Super Admins (only Super Admins for the catalogue); for shipping, doctors too when
    the practice lets them."""
    require_enabled_company(company)
    roles = (CompanyMembership.Role.SUPER_ADMIN,) if catalogue else (
        CompanyMembership.Role.PRACTICE_ADMIN, CompanyMembership.Role.SUPER_ADMIN,
    )
    if shipping and not catalogue and doctors_ship(company):
        roles += (CompanyMembership.Role.DOCTOR,)
    if not (company.is_active and actor.is_active and get_user_model().objects.filter(pk=actor.pk, is_active=True).exists()
            and CompanyMembership.objects.filter(company=company, user=actor, is_active=True, role__in=roles).exists()):
        raise PermissionDenied('You do not have permission to change these practice operations.')


def _lock_company(company, actor, **kwargs):
    company = Company.objects.select_for_update().get(pk=company.pk)
    require_operations_actor(company, actor, **kwargs)
    return company


def current_product_authorization(patient, product, on_date=None):
    on_date = on_date or timezone.localdate()
    candidates = TreatmentAuthorization.objects.filter(
        company_id=patient.company_id, patient=patient, product=product, status=TreatmentAuthorization.Status.ACTIVE,
        starts_on__lte=on_date, expires_on__gte=on_date,
    ).select_related('company', 'patient', 'product', 'dose', 'prescribed_by').order_by('-expires_on', '-pk')
    return next((authorization for authorization in candidates if authorization_is_current(authorization, on_date)), None)


def supply_days(authorization):
    """How long one dispatch lasts: the prescribed dose's interval (a 10 mg pen every 5 weeks is 35 days)."""
    if authorization is not None and authorization.dose_id:
        return authorization.dose.dispatch_weeks * 7
    return DEFAULT_SUPPLY_DAYS


def dose_run(authorization):
    """The prescription and the ones it renews for the same dose, latest first. A renewal carries on the same dose,
    so it keeps the supply cycle and parcel rhythm the patient started on."""
    run = [authorization]
    while authorization.replaces_id:
        previous = authorization.replaces
        if previous.dose_id != authorization.dose_id or previous.product_id != authorization.product_id:
            break
        run.append(previous)
        authorization = previous
    return run


def dose_started_on(authorization):
    """When the patient started this dose."""
    return dose_run(authorization)[-1].starts_on


def allowance_window(patient, authorization, on_date):
    """The supply cycle on_date falls in. A dose's cycles run from when the patient started it, so a starting plan's
    2-weekly step and the weekly step after it each send on their own schedule."""
    days = supply_days(authorization)
    if authorization.dose_id:
        anchor = dose_started_on(authorization)
    else:
        subscription = PatientSubscription.objects.filter(company_id=patient.company_id, patient=patient,
                                                           status=PatientSubscription.Status.ACTIVE).order_by('starts_on').first()
        anchor = subscription.starts_on if subscription and subscription.starts_on <= on_date else authorization.starts_on
    start = anchor + timedelta(days=((on_date - anchor).days // days) * days)
    return start, start + timedelta(days=days - 1)


def product_allowance(patient, product, *, on_date=None, exclude_shipment=None, exclude_order=None):
    """Only explicitly authorised products; never infer allowed medication doses."""
    on_date = on_date or timezone.localdate()
    if not product.requires_authorisation:
        return None, 100, ''
    authorization = current_product_authorization(patient, product, on_date)
    if authorization is None:
        return None, 0, 'A current doctor authorisation for this product is required.'
    group = MedicationProduct.objects.filter(company_id=patient.company_id)
    group = group.filter(allowance_group=product.allowance_group) if product.allowance_group else group.filter(pk=product.pk)
    cap = authorization.quantity_per_cycle
    if product.allowance_group:
        authorizations = TreatmentAuthorization.objects.filter(
            company_id=patient.company_id, patient=patient, product__in=group, status='active',
            starts_on__lte=on_date, expires_on__gte=on_date,
        ).select_related('company', 'patient', 'product', 'prescribed_by')
        cap = min([cap, *(a.quantity_per_cycle for a in authorizations if authorization_is_current(a, on_date))])
    start, end = allowance_window(patient, authorization, on_date)
    dispatched = (Shipment.Status.DISPATCHED, Shipment.Status.DELIVERED)
    # Actual supply consumes the actual dispatch cycle, not an editable/planned
    # date. Old imported dispatches without a timestamp retain their scheduled
    # date; outstanding parcels reserve their planned supply cycle.
    supplied_in_window = Q(shipment__status__in=dispatched,
                           shipment__dispatched_at__date__range=(start, end))
    planned_in_window = (Q(shipment__scheduled_for__range=(start, end)) &
                        (~Q(shipment__status__in=dispatched) | Q(shipment__dispatched_at__isnull=True)))
    items = ShipmentItem.objects.filter(
        company_id=patient.company_id, shipment__company_id=patient.company_id,
        shipment__patient=patient, product__in=group,
    ).filter(supplied_in_window | planned_in_window).exclude(shipment__status=Shipment.Status.CANCELLED)
    if exclude_shipment:
        items = items.exclude(shipment=exclude_shipment)
    used = items.aggregate(total=Sum('quantity'))['total'] or 0
    requested = PharmacyOrder.objects.filter(company_id=patient.company_id, patient=patient,
                                             status=PharmacyOrder.Status.SUBMITTED)
    if exclude_order:
        requested = requested.exclude(pk=exclude_order.pk)
    requested_quantity = requested.filter(items__product__in=group).aggregate(total=Sum('items__quantity'))['total'] or 0
    return authorization, max(0, cap - used - requested_quantity), ''


def shipment_hold_reason(shipment, on_date=None):
    on_date = on_date or shipment.scheduled_for
    if not shipment.company.is_active or not shipment.patient.is_active or shipment.patient.company_id != shipment.company_id:
        return 'The patient/practice is not active or no longer matches.'
    if shipment.subscription_id:
        if shipment.subscription.patient_id != shipment.patient_id or shipment.subscription.company_id != shipment.company_id:
            return 'The subscription does not match this patient and practice.'
        try:
            ensure_subscription_eligible(shipment.subscription, on_date=on_date)
        except ValidationError as error:
            return ' '.join(error.messages)
    items = list(shipment.items.select_related('product'))
    if not items:
        return 'Add at least one product before preparing a shipment.'
    grouped = defaultdict(int)
    for item in items:
        if item.company_id != shipment.company_id or item.product.company_id != shipment.company_id or not item.product.is_active:
            return 'A shipment product is inactive or belongs to another practice.'
        if type(item.quantity) is not int or item.quantity <= 0:
            return 'Each shipment item must have a positive whole-number quantity.'
        auth, remaining, reason = product_allowance(shipment.patient, item.product, on_date=on_date,
                                                   exclude_shipment=shipment)
        if reason:
            return reason
        if auth and item.dose != auth.max_dose:
            return 'The recorded dose does not match the current explicit doctor authorisation.'
        key = ('group', item.product.allowance_group) if item.product.allowance_group else ('product', item.product_id)
        grouped[key] += item.quantity
        if grouped[key] > remaining:
            return 'This shipment exceeds the remaining authorised quantity for the supply cycle.'
    return ''


def _locked_shipment(shipment, actor):
    company = _lock_company(shipment.company, actor, shipping=True)
    shipment = Shipment.objects.select_for_update(of=('self',)).select_related('company', 'patient', 'subscription').get(pk=shipment.pk, company=company)
    if shipment.patient.company_id != company.pk or not shipment.patient.is_active:
        raise ValidationError('The shipment patient is not active in this practice.')
    return company, shipment


@transaction.atomic
def prepare_shipment(*, shipment, actor, request=None):
    """Mark a parcel ready: its patient, care plan, prescription, dose and supply allowance are checked. Pens are
    ordered for the week's ready parcels, so nothing waits on stock."""
    company, shipment = _locked_shipment(shipment, actor)
    if shipment.status not in UNDISPATCHED:
        raise ValidationError('Only an undispatched shipment can be prepared.')
    reason = shipment_hold_reason(shipment)
    if reason:
        raise ValidationError(reason)
    shipment.status, shipment.hold_reason = Shipment.Status.READY, ''
    shipment.prepared_by, shipment.prepared_at, shipment.locked_at = actor, timezone.now(), None
    shipment.save(update_fields=('status', 'hold_reason', 'prepared_by', 'prepared_at', 'locked_at', 'updated_at'))
    record_audit(company=company, actor=actor, patient=shipment.patient, action='shipment.prepared', target=shipment, request=request)
    return shipment


@transaction.atomic
def prepare_week(*, company, actor, week_start, request=None):
    """Mark the week's draft parcels ready, each checked as prepare_shipment does. A draft that doesn't pass stays a
    draft, with the reason shown on the list. Returns (marked ready, left as drafts)."""
    company = _lock_company(company, actor, shipping=True)
    drafts = list(Shipment.objects.select_for_update(of=('self',)).filter(
        company=company, patient__company=company, status=Shipment.Status.DRAFT,
        scheduled_for__range=(week_start, week_start + timedelta(days=6)),
    ).select_related('company', 'patient', 'subscription').order_by('scheduled_for', 'pk'))
    ready, left = [], []
    for shipment in drafts:
        if shipment_hold_reason(shipment):
            left.append(shipment)
            continue
        shipment.status, shipment.hold_reason = Shipment.Status.READY, ''
        shipment.prepared_by, shipment.prepared_at, shipment.locked_at = actor, timezone.now(), None
        shipment.save(update_fields=('status', 'hold_reason', 'prepared_by', 'prepared_at', 'locked_at', 'updated_at'))
        record_audit(company=company, actor=actor, patient=shipment.patient, action='shipment.prepared', target=shipment,
                     request=request)
        ready.append(shipment)
    return ready, left


@transaction.atomic
def change_parcel(*, shipment, actor, scheduled_for, quantities, request=None):
    """Change when a parcel that isn't locked goes out and how many pens each line has ({item pk: pens}). A ready
    parcel goes back to draft, to be checked again."""
    company, shipment = _locked_shipment(shipment, actor)
    if shipment.status not in UNDISPATCHED or shipment.locked_at:
        raise ValidationError('Only a parcel that isn’t locked or dispatched can be changed. Hold it first to unlock it.')
    if not isinstance(scheduled_for, date) or isinstance(scheduled_for, datetime) or scheduled_for < timezone.localdate():
        raise ValidationError('Choose today or a later dispatch date.')
    items = {item.pk: item for item in shipment.items.all()}
    if set(quantities) != set(items) or any(type(pens) is not int or not 1 <= pens <= 100 for pens in quantities.values()):
        raise ValidationError('Enter between 1 and 100 pens for each line.')
    before = {'scheduled_for': shipment.scheduled_for.isoformat(), 'pens': {str(pk): item.quantity for pk, item in items.items()}}
    for pk, pens in quantities.items():
        if items[pk].quantity != pens:
            items[pk].quantity = pens
            items[pk].save(update_fields=('quantity', 'updated_at'))
    shipment.scheduled_for = scheduled_for
    if shipment.status == Shipment.Status.READY:
        shipment.status = Shipment.Status.DRAFT
    shipment.save(update_fields=('scheduled_for', 'status', 'updated_at'))
    record_audit(company=company, actor=actor, patient=shipment.patient, action='shipment.changed', target=shipment,
                 request=request, metadata={'before': before, 'scheduled_for': scheduled_for.isoformat(),
                                            'pens': {str(pk): pens for pk, pens in quantities.items()}})
    return shipment


@transaction.atomic
def hold_or_cancel_shipment(*, shipment, actor, cancel=False, reason, request=None):
    company, shipment = _locked_shipment(shipment, actor)
    if shipment.status not in UNDISPATCHED:
        raise ValidationError('A dispatched, delivered or cancelled shipment cannot be changed here.')
    if not reason.strip() or len(reason) > 500:
        raise ValidationError('Give a reason, up to 500 characters.')
    # A parcel already booked with The Courier Guy has its booking cancelled first, so it isn't collected; it is
    # booked again once it is ready and locked.
    from .courier import cancel_booking

    waybill = shipment.waybill
    cancelled_booking = cancel_booking(shipment)
    shipment.status = Shipment.Status.CANCELLED if cancel else Shipment.Status.HELD
    shipment.hold_reason, shipment.locked_at = reason, None
    fields = ['status', 'hold_reason', 'locked_at', 'updated_at']
    if cancelled_booking:
        shipment.waybill, shipment.courier_shipment_id, shipment.courier_status = '', '', 'cancelled'
        shipment.waybill_created_at, shipment.booked_address = None, {}
        fields += ['waybill', 'courier_shipment_id', 'courier_status', 'waybill_created_at', 'booked_address']
    shipment.save(update_fields=fields)
    record_audit(company=company, actor=actor, patient=shipment.patient, action='shipment.cancelled' if cancel else 'shipment.held',
                 target=shipment, request=request,
                 metadata={'reason': reason, **({'cancelled_waybill': waybill} if cancelled_booking else {})})
    return shipment


def _check_eligible(shipment, on_date=None):
    reason = shipment_hold_reason(shipment, on_date=on_date)
    if reason:
        raise ValidationError(reason)


def delivery_problem(shipment):
    """What the courier still needs to deliver this parcel: an address and a number to call. '' when nothing."""
    address = DeliveryAddress.objects.filter(company_id=shipment.company_id, patient_id=shipment.patient_id).first()
    if address is None:
        return 'No delivery address.'
    missing = address.missing()
    return f'The delivery address needs its {", ".join(label.lower() for label in missing)}.' if missing else ''


def lock_reasons(shipment, on_date=None):
    """Everything that stops a parcel being locked for its week: its prescription and care plan, its delivery
    address and contact number, and payment."""
    return [reason for reason in (shipment_hold_reason(shipment, on_date=on_date), delivery_problem(shipment),
                                  parcel_payment_reason(shipment)) if reason]


@transaction.atomic
def lock_week(*, company, actor, week_start, hold_failing=False, request=None):
    """Check and lock the week's ready parcels: their prescriptions again, delivery addresses, contact numbers and
    payment. The week locks only when every ready parcel passes; with hold_failing, the ones that don't are held with
    their reasons (shown on the list) and the rest are locked. Returns {'locked': [...], 'held': [(parcel, reason)]}."""
    company = _lock_company(company, actor, shipping=True)
    shipments = list(Shipment.objects.select_for_update(of=('self',)).filter(
        company=company, status=Shipment.Status.READY, scheduled_for__range=(week_start, week_start + timedelta(days=6)),
    ).select_related('company', 'patient', 'subscription').order_by('scheduled_for', 'patient__last_name', 'pk'))
    if not shipments:
        raise ValidationError('There are no ready shipments to lock for this week.')
    failing = [(shipment, ' '.join(reasons)) for shipment in shipments if (reasons := lock_reasons(shipment))]
    if failing and not hold_failing:
        raise ValidationError([f'{shipment.patient} ({shipment.scheduled_for:%a %-d %b}): {reason}' for shipment, reason in failing])
    held_ids = set()
    for shipment, reason in failing:
        hold_or_cancel_shipment(shipment=shipment, actor=actor, reason=f'Held at Check and lock week: {reason}'[:500], request=request)
        held_ids.add(shipment.pk)
    locked = [shipment for shipment in shipments if shipment.pk not in held_ids]
    for shipment in locked:
        if not shipment.locked_at:
            shipment.locked_at = timezone.now()
            shipment.save(update_fields=('locked_at', 'updated_at'))
            record_audit(company=company, actor=actor, patient=shipment.patient, action='shipment.locked', target=shipment, request=request)
    return {'locked': locked, 'held': failing}


def lock_shipping_week(*, company, actor, week_start, request=None):
    """Confirm the week's ready parcels: each is checked again, then the week's pens can be ordered."""
    return lock_week(company=company, actor=actor, week_start=week_start, request=request)['locked']


def week_order(company, week_start):
    """What to order for a shipping week: the pens in its ready parcels by product, how many of those parcels are
    locked, and the parcels left out because they are drafts or held."""
    week = (week_start, week_start + timedelta(days=6))
    shipments = Shipment.objects.filter(company=company, patient__company=company, scheduled_for__range=week)
    ready = shipments.filter(status=Shipment.Status.READY)
    totals = ShipmentItem.objects.filter(company=company, shipment__in=ready).values('product').annotate(pens=Sum('quantity'))
    pens = {row['product']: row['pens'] for row in totals}
    products = MedicationProduct.objects.filter(pk__in=pens).order_by('name', 'strength', 'pk')
    return {
        'lines': [{'product': product, 'pens': pens[product.pk]} for product in products],
        'parcels': ready.count(), 'locked': ready.filter(locked_at__isnull=False).count(),
        'drafts': shipments.filter(status=Shipment.Status.DRAFT).count(),
        'held': shipments.filter(status=Shipment.Status.HELD).count(),
    }


# Dispatch stock's average looks back over this many full weeks of dispatched parcels.
AVERAGE_WEEKS = 8


def _pens_by_product(items):
    return {row['product']: row['pens'] for row in items.values('product').annotate(pens=Sum('quantity'))}


def week_stock(company, week_start):
    """Each active catalogue item for a shipping week (Dispatch stock): the pens going out in its parcels that aren't
    cancelled, and the pens to order, which are those in its ready parcels (as week_order). Pens aren't kept in stock,
    so there is nothing on hand to take off. For Super Admins, how To order is worked out: the pens still in draft or
    held parcels, the average dispatched a week over the AVERAGE_WEEKS before this one, and next week's parcels."""
    week = (week_start, week_start + timedelta(days=6))
    items = ShipmentItem.objects.filter(company=company, shipment__patient__company=company)
    in_week = items.filter(shipment__scheduled_for__range=week).exclude(shipment__status=Shipment.Status.CANCELLED)
    counts = {
        'to_ship': _pens_by_product(in_week),
        'to_order': _pens_by_product(in_week.filter(shipment__status=Shipment.Status.READY)),
        'draft': _pens_by_product(in_week.filter(shipment__status=Shipment.Status.DRAFT)),
        'held': _pens_by_product(in_week.filter(shipment__status=Shipment.Status.HELD)),
        'next_week': _pens_by_product(items.filter(
            shipment__scheduled_for__range=(week[0] + timedelta(days=7), week[1] + timedelta(days=7)),
        ).exclude(shipment__status=Shipment.Status.CANCELLED)),
        'dispatched': _pens_by_product(items.filter(
            shipment__status__in=(Shipment.Status.DISPATCHED, Shipment.Status.DELIVERED),
            shipment__dispatched_at__date__range=(week_start - timedelta(weeks=AVERAGE_WEEKS), week_start - timedelta(days=1)),
        )),
    }
    rows = []
    for product in MedicationProduct.objects.filter(company=company, is_active=True).order_by('name', 'strength', 'pk'):
        row = {name: values.get(product.pk, 0) for name, values in counts.items()}
        row['average'] = row.pop('dispatched') / AVERAGE_WEEKS
        rows.append(dict(row, product=product))
    return rows


@transaction.atomic
def set_shipment_note(*, shipment, actor, note, request=None):
    """The practice's note on a parcel, from the Weekly shipping list. Blank clears it."""
    company, shipment = _locked_shipment(shipment, actor)
    note = ' '.join(note.split()) if isinstance(note, str) else ''
    if len(note) > 500:
        raise ValidationError('Keep the note to 500 characters or fewer.')
    if note != shipment.note:
        shipment.note = note
        shipment.save(update_fields=('note', 'updated_at'))
        record_audit(company=company, actor=actor, patient=shipment.patient, action='shipment.note_updated',
                     target=shipment, request=request, metadata={'cleared': not note})
    return shipment


@transaction.atomic
def dispatch_shipment(*, shipment, actor, tracking_number, confirm=False, batch_notes=None, request=None):
    """Record a parcel's actual dispatch. batch_notes ({item pk: 'SG-2604, expiry 04/2027'}) note what went into it."""
    company, shipment = _locked_shipment(shipment, actor)
    if shipment.status in (Shipment.Status.DISPATCHED, Shipment.Status.DELIVERED):
        if shipment.tracking_number == tracking_number.strip():
            return shipment
        raise ValidationError('The dispatched shipment record is immutable.')
    if confirm is not True or shipment.status != Shipment.Status.READY or not shipment.locked_at:
        raise ValidationError('Prepare and lock the shipment, then explicitly confirm dispatch.')
    if not tracking_number.strip() or len(tracking_number) > 120:
        raise ValidationError('Enter the actual tracking reference, up to 120 characters.')
    batch_notes = batch_notes or {}
    if any(not isinstance(note, str) or len(note.strip()) > 120 for note in batch_notes.values()):
        raise ValidationError('Keep each batch note to 120 characters or fewer.')
    _check_eligible(shipment)
    actual_day_reason = shipment_hold_reason(shipment, on_date=timezone.localdate())
    if actual_day_reason:
        raise ValidationError(f'Cannot dispatch on this date: {actual_day_reason}')
    # The address or payment may have changed since the week was locked.
    for reason in (delivery_problem(shipment), parcel_payment_reason(shipment)):
        if reason:
            raise ValidationError(f'Cannot dispatch: {reason}')
    items = list(shipment.items.select_related('product'))
    for item in items:
        if item.pk in batch_notes:
            item.batch_note = ' '.join(batch_notes[item.pk].split())
            item.save(update_fields=('batch_note', 'updated_at'))
    shipment.dispatch_snapshot = {
        'patient_name': str(shipment.patient), 'patient_id_number': shipment.patient.id_number,
        'company_name': company.name, 'scheduled_for': shipment.scheduled_for.isoformat(),
        'items': [{'product': str(item.product), 'dose': item.dose, 'quantity': item.quantity, 'batch_note': item.batch_note}
                  for item in items],
    }
    from .courier import booked_delivery

    order = getattr(shipment, 'pharmacy_order', None)
    address = DeliveryAddress.objects.filter(company=company, patient=shipment.patient).first()
    if booked := booked_delivery(shipment):
        # Where The Courier Guy was sent when it was booked, even if the patient's address changed after that.
        shipment.dispatch_snapshot['delivery_address'] = booked
    elif address is not None:
        shipment.dispatch_snapshot['delivery_address'] = {
            'recipient': address.recipient or str(shipment.patient), 'lines': address.lines(), 'phone': address.phone}
    elif order:
        shipment.dispatch_snapshot['delivery_address'] = order.delivery_address
    if shipment.waybill:
        shipment.dispatch_snapshot['waybill'] = shipment.waybill
    shipment.status, shipment.tracking_number, shipment.dispatched_at = Shipment.Status.DISPATCHED, tracking_number.strip(), timezone.now()
    shipment.save(update_fields=('status', 'tracking_number', 'dispatched_at', 'dispatch_snapshot', 'updated_at'))
    record_audit(company=company, actor=actor, patient=shipment.patient, action='shipment.dispatched', target=shipment, request=request)
    PatientEvent.objects.create(company=company, patient=shipment.patient, category=PatientEvent.Category.DELIVERY,
                                title='Shipment dispatched', detail='Your care team recorded a dispatch. See your treatment page for tracking.',
                                source_type='care.shipment', source_id=str(shipment.pk))
    return shipment


def dispatch_week(*, company, actor, week_start, batch_notes=None, request=None):
    """Record the week's locked parcels as collected by the courier: each with its waybill as the tracking reference,
    and the batch noted for each medicine ({product pk: 'SG-2604, expiry 04/2027'}). A parcel that can't go (its
    payment or address changed) is left with the reason. Returns (dispatched, [(parcel, reason)])."""
    batch_notes = batch_notes or {}
    parcels = list(Shipment.objects.filter(
        company=company, status=Shipment.Status.READY, locked_at__isnull=False,
        scheduled_for__range=(week_start, week_start + timedelta(days=6)),
    ).exclude(waybill='').order_by('scheduled_for', 'pk'))
    if not parcels:
        raise ValidationError('There are no locked parcels with a waybill to record as collected this week.')
    dispatched, failed = [], []
    for parcel in parcels:
        notes = {item.pk: batch_notes.get(item.product_id, '') for item in parcel.items.all()}
        try:
            dispatch_shipment(shipment=parcel, actor=actor, tracking_number=parcel.waybill, confirm=True,
                              batch_notes={pk: note for pk, note in notes.items() if note}, request=request)
        except ValidationError as error:
            failed.append((parcel, ' '.join(error.messages)))
            continue
        dispatched.append(parcel)
    return dispatched, failed


@transaction.atomic
def mark_delivered(*, shipment, actor, confirm=False, request=None):
    company, shipment = _locked_shipment(shipment, actor)
    if shipment.status == Shipment.Status.DELIVERED:
        return shipment
    if confirm is not True or shipment.status != Shipment.Status.DISPATCHED:
        raise ValidationError('Only a dispatched shipment can be confirmed delivered.')
    shipment.status, shipment.delivered_at = Shipment.Status.DELIVERED, timezone.now()
    shipment.save(update_fields=('status', 'delivered_at', 'updated_at'))
    record_audit(company=company, actor=actor, patient=shipment.patient, action='shipment.delivered', target=shipment, request=request)
    PatientEvent.objects.create(company=company, patient=shipment.patient, category=PatientEvent.Category.DELIVERY,
                                title='Shipment delivered', detail='Your care team recorded delivery of your shipment.',
                                source_type='care.shipment', source_id=str(shipment.pk))
    return shipment
