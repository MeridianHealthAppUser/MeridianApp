"""The Courier Guy, through its ShipLogic API: book a shipment (and so a waybill) for each locked parcel of a week,
for collection from the practice on its shipping day, and cancel the booking of a parcel held after it was booked.

Set TCG_API_KEY to switch it on; without a key, nothing is sent and the practice records tracking references by hand
as before. The key is a Bearer API key from The Courier Guy's portal (Integrations > API Keys). TCG_API_URL is
https://api.portal.thecourierguy.co.za for the live account and https://api.shiplogic.com for a sandbox account
(tcg-sandbox.shiplogic.com). The collection address, contact, service level (ECO, economy road, by default) and the
usual parcel's size and weight are in Practice settings (Courier).

The API as used here was checked against ShipLogic's published TCG API collection and The Courier Guy's own
WooCommerce plugin (v5.5.4): POST /v2/shipments, whose short_tracking_reference is the waybill;
GET /v2/shipments/label?id= for a link to the label PDF; POST /v2/shipments/cancel with the tracking reference.
"""

import json
import logging
import re
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from urllib import error as urlerror
from urllib import request as urlrequest

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .addresses import FIELDS as ADDRESS_FIELDS
from .availability import SAST
from .billing import practice_settings
from .models import DeliveryAddress, Shipment
from .demo_safety import is_demo_record
from .services import record_audit

logger = logging.getLogger(__name__)
TIMEOUT_SECONDS = 20
SETTINGS_FIELDS = ('collection_street', 'collection_suburb', 'collection_city', 'collection_province',
                   'collection_postal_code', 'collection_contact', 'collection_phone')


class CourierError(Exception):
    pass


def courier_enabled():
    return bool(settings.TCG_API_KEY)


def collection_problem(company):
    """What Practice settings still needs before parcels can be booked, or ''."""
    row = practice_settings(company)
    missing = [str(row._meta.get_field(name).verbose_name).lower() for name in SETTINGS_FIELDS if not getattr(row, name)]
    return f'Add the collection {", ".join(missing)} in Practice settings > Courier.' if missing else ''


def _call(method, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urlrequest.Request(f'{settings.TCG_API_URL}{path}', data=body, method=method, headers={
        'Authorization': f'Bearer {settings.TCG_API_KEY}', 'Content-Type': 'application/json', 'Accept': 'application/json',
    })
    try:
        with urlrequest.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode() or '{}')
    except urlerror.HTTPError as error:
        # Errors come back as JSON ({"message": ...}) or as plain text.
        detail = error.read().decode(errors='replace')[:300]
        try:
            parsed = json.loads(detail)
            detail = (parsed.get('message') or parsed.get('error') or detail) if isinstance(parsed, dict) else detail
        except ValueError:
            pass
        raise CourierError(f'The Courier Guy answered {error.code}: {detail or error.reason}') from None
    except (urlerror.URLError, TimeoutError, ValueError) as error:
        raise CourierError(f'The Courier Guy could not be reached: {error}') from None


def _address(*, business, street, suburb, city, province, postal_code):
    return {'type': 'business' if business else 'residential', 'company': business, 'street_address': street,
            'local_area': suburb, 'city': city, 'zone': province, 'country': 'ZA', 'code': postal_code}


def _start_of_day(day):
    """The start of a South African day, as the API's timestamps are written: 26 Oct is 2026-10-25T22:00:00.000Z."""
    return datetime.combine(day, time.min, tzinfo=SAST).astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def shipment_payload(shipment, address, row):
    """The booking for one parcel: from the practice's collection address to the patient, collected no earlier than
    the parcel's day."""
    day = _start_of_day(shipment.scheduled_for)
    return {
        'collection_address': _address(business=row.collection_business, street=row.collection_street,
                                       suburb=row.collection_suburb, city=row.collection_city,
                                       province=row.collection_province, postal_code=row.collection_postal_code),
        'collection_contact': {'name': row.collection_contact, 'mobile_number': row.collection_phone,
                               'email': row.collection_email},
        'delivery_address': _address(business=address.business, street=address.street, suburb=address.suburb,
                                     city=address.city, province=address.province, postal_code=address.postal_code),
        'delivery_contact': {'name': address.recipient or str(shipment.patient), 'mobile_number': address.phone,
                             'email': shipment.patient.user.email if shipment.patient.user_id else ''},
        'parcels': [{'parcel_description': 'Cold-chain medication parcel',
                     'submitted_length_cm': row.parcel_length_cm, 'submitted_width_cm': row.parcel_width_cm,
                     'submitted_height_cm': row.parcel_height_cm, 'submitted_weight_kg': float(Decimal(row.parcel_weight_kg))}],
        'service_level_code': row.courier_service_level,
        'collection_min_date': day,
        'delivery_min_date': day,
        'special_instructions_delivery': address.instructions,
        'customer_reference': f'MH-{shipment.pk}',
        'customer_reference_name': 'Parcel',
        # The Courier Guy emails and texts the patient their tracking updates.
        'mute_notifications': False,
    }


def booked_snapshot(shipment, address):
    """What The Courier Guy is given for the parcel, kept on it (Shipment.booked_address)."""
    return {'patient_name': str(shipment.patient), **{name: getattr(address, name) for name in ADDRESS_FIELDS},
            'lines': address.lines()}


def booked_delivery(shipment):
    """Where a booked parcel is going, as dispatch records it ({recipient, lines, phone}), or {} when it isn't booked."""
    booked = shipment.booked_address or {}
    if not booked.get('lines'):
        return {}
    return {'recipient': booked.get('recipient') or booked.get('patient_name', ''), 'lines': booked['lines'],
            'phone': booked.get('phone', '')}


def changed_since_booking(shipment, address):
    """Whether the patient's delivery address is no longer the one the parcel was booked with."""
    booked = shipment.booked_address or {}
    return bool(booked.get('lines')) and (
        address is None or any(booked.get(name, '') != getattr(address, name) for name in ADDRESS_FIELDS))


def book_waybills(*, company, actor, week_start, request=None, demo_only=False):
    """Book a shipment with The Courier Guy for each of the week's locked parcels that has no waybill yet. Each parcel
    is booked on its own, so one that fails (its error is kept on the parcel) doesn't stop the rest. Returns
    (booked, failed) parcels."""
    from .operations import _lock_company

    with transaction.atomic():
        company = _lock_company(company, actor, shipping=True)
        candidates = Shipment.objects.filter(
            company=company, patient__company=company, status=Shipment.Status.READY, locked_at__isnull=False, waybill='', tracking_number='',
            scheduled_for__range=(week_start, week_start + timedelta(days=6)),
        ).select_related('patient__user', 'company').order_by('scheduled_for', 'pk')
        if demo_only:
            candidates = candidates.filter(patient__demo_data_set__isnull=False)
        parcels = list(candidates)
    if not parcels:
        raise ValidationError('There are no locked parcels without a waybill this week. Check and lock the week first.')
    if any(not is_demo_record(parcel) for parcel in parcels):
        if not courier_enabled():
            raise ValidationError('The Courier Guy isn’t connected yet. Add TCG_API_KEY to the app’s settings to book waybills.')
        problem = collection_problem(company)
        if problem:
            raise ValidationError(problem)
    row = practice_settings(company)
    booked, failed = [], []
    for parcel in parcels:
        address = DeliveryAddress.objects.filter(company=company, patient=parcel.patient).first()
        try:
            if address is None or address.missing():
                raise CourierError('The delivery address is incomplete.')
            if is_demo_record(parcel):
                response = {'short_tracking_reference': f'DEMO-{parcel.pk:08d}',
                            'id': f'demo-{parcel.pk}', 'status': 'booked'}
            elif demo_only:
                raise CourierError('This parcel is no longer part of demo data.')
            else:
                response = _call('POST', '/v2/shipments', shipment_payload(parcel, address, row))
            # The short reference ("3BM3") is the waybill The Courier Guy's own plugin keeps; the custom one
            # ("SLX3BM3") is the same shipment with the account's prefix.
            waybill = str(response.get('short_tracking_reference') or response.get('custom_tracking_reference') or '')
            if not waybill:
                raise CourierError('The Courier Guy didn’t return a tracking reference.')
        except CourierError as error:
            Shipment.objects.filter(pk=parcel.pk).update(courier_error=str(error)[:500], updated_at=timezone.now())
            record_audit(company=company, actor=actor, patient=parcel.patient, action='courier.booking_failed',
                         target=parcel, request=request, metadata={'error': str(error)[:300]})
            failed.append(parcel)
            continue
        Shipment.objects.filter(pk=parcel.pk).update(
            waybill=waybill[:60], courier_shipment_id=str(response.get('id', ''))[:60],
            courier_status=str(response.get('status', ''))[:60], courier_status_updated_at=None, courier_error='', waybill_created_at=timezone.now(),
            booked_address=booked_snapshot(parcel, address), updated_at=timezone.now())
        record_audit(company=company, actor=actor, patient=parcel.patient, action='courier.booked', target=parcel,
                     request=request, metadata={'waybill': waybill})
        booked.append(parcel)
    return booked, failed


def label_url(shipment):
    """A short-lived link to the parcel's waybill label, from The Courier Guy."""
    if is_demo_record(shipment):
        raise ValidationError('Demo courier bookings are simulated and have no courier label.')
    if not (courier_enabled() and shipment.courier_shipment_id):
        raise ValidationError('This parcel has no booking with The Courier Guy.')
    try:
        response = _call('GET', f'/v2/shipments/label?id={shipment.courier_shipment_id}')
    except CourierError as error:
        raise ValidationError(str(error)) from None
    url = response.get('url') if isinstance(response, dict) else None
    if not url:
        raise ValidationError('The Courier Guy didn’t return a label for this parcel.')
    return url


def cancel_booking(shipment):
    """Cancel a parcel's booking with The Courier Guy, so a parcel held or cancelled after it was booked isn't
    collected (or charged for). The Courier Guy refuses once the parcel has been collected. Returns whether a booking
    was cancelled; clearing the waybill is left to the caller, inside its own change."""
    if not shipment.waybill:
        return False
    if is_demo_record(shipment):
        return True
    if not courier_enabled():
        raise ValidationError('This parcel has a waybill with The Courier Guy, but the courier isn’t connected any more. '
                              'Cancel the booking on The Courier Guy’s portal first.')
    try:
        _call('POST', '/v2/shipments/cancel', {'tracking_reference': shipment.waybill})
    except CourierError as error:
        raise ValidationError(f'The Courier Guy booking {shipment.waybill} couldn’t be cancelled, so the parcel wasn’t '
                              f'changed. {error}') from None
    return True


# Tracking: The Courier Guy's "Tracking event" webhook (Settings > Webhook subscriptions on its portal)

STATUS_LABELS = {
    'submitted': 'Awaiting collection', 'booked': 'Awaiting collection',
    'collection-assigned': 'Collection assigned', 'collected': 'Collected',
    'at-hub': 'At the hub', 'returned-to-hub': 'Returned to the hub', 'in-transit': 'In transit', 'out-for-delivery': 'Out for delivery',
    'delivered': 'Delivered', 'cancelled': 'Cancelled', 'collection-failed-attempt': 'Collection attempt failed',
    'delivery-failed-attempt': 'Delivery attempt failed', 'returned-to-sender': 'Returned to the practice',
}


def status_label(status):
    return STATUS_LABELS.get(status, str(status).replace('-', ' ').capitalize())


def webhook_token_ok(token):
    """The tracking webhook's address carries a secret (TCG_WEBHOOK_TOKEN): The Courier Guy doesn't sign webhooks."""
    import hmac

    return bool(settings.TCG_WEBHOOK_TOKEN) and hmac.compare_digest(str(token).encode('utf-8'), settings.TCG_WEBHOOK_TOKEN.encode('utf-8'))


def _tracking_data(payload):
    """Validate the Tracking event shape published by The Courier Guy. Unknown status codes remain supported."""
    if not isinstance(payload, dict):
        raise ValidationError('Each tracking event must be an object.')
    status = payload.get('status')
    if not isinstance(status, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,59}', status):
        raise ValidationError('A tracking event needs a valid status.')
    references = []
    for key in ('shipment_id', 'short_tracking_reference'):
        value = payload.get(key)
        if value is None or value == '':
            references.append('')
        elif isinstance(value, bool) or not isinstance(value, (str, int)) or len(str(value)) > 120:
            raise ValidationError('Invalid courier shipment reference.')
        else:
            references.append(str(value).strip())
    if not any(references):
        raise ValidationError('A shipment ID or short tracking reference is required.')
    dates = []
    for key in ('shipment_time_modified', 'shipment_delivered_date'):
        value = payload.get(key)
        if value is None or value == '':
            dates.append(None)
            continue
        try:
            parsed = parse_datetime(value) if isinstance(value, str) else None
        except (ValueError, TypeError):
            parsed = None
        if parsed is None:
            raise ValidationError(f'{key} must be an ISO 8601 date and time.')
        dates.append(timezone.make_aware(parsed, UTC) if timezone.is_naive(parsed) else parsed)
    return status, *references, *dates


def validate_tracking_event(payload):
    _tracking_data(payload)


def _tracking_matches(parcel, shipment_id, waybill):
    # Both identifiers must agree with the stored booking. An externally booked parcel may only have tracking_number.
    if shipment_id and parcel.courier_shipment_id and parcel.courier_shipment_id != shipment_id:
        return False
    if waybill and waybill not in (parcel.waybill, parcel.tracking_number):
        return False
    return bool((shipment_id and parcel.courier_shipment_id == shipment_id)
                or (waybill and waybill in (parcel.waybill, parcel.tracking_number)))


@transaction.atomic
def apply_tracking(payload):
    """Apply a tracking event to one unambiguous parcel. Replays and older updates do not regress tracking.

    Authenticated collection and delivery events record actual dispatch once; delivery closes the parcel.
    """
    from .models import PatientEvent

    status, shipment_id, waybill, modified_at, delivered_at = _tracking_data(payload)
    matches = Q(pk__in=[])
    if shipment_id:
        matches |= Q(courier_shipment_id=shipment_id)
    if waybill:
        matches |= Q(waybill=waybill) | Q(tracking_number=waybill, status__in=(Shipment.Status.DISPATCHED, Shipment.Status.DELIVERED))
    candidates = list(Shipment.objects.filter(matches).values_list('pk', flat=True)[:2])
    if len(candidates) != 1:
        return None
    from practices.models import Company
    company_id = Shipment.objects.values_list('company_id', flat=True).get(pk=candidates[0])
    Company.objects.select_for_update().get(pk=company_id)
    parcel = Shipment.objects.select_for_update(of=('self',)).select_related('company', 'patient').get(pk=candidates[0])
    if is_demo_record(parcel) or not _tracking_matches(parcel, shipment_id, waybill):
        return None
    latest = parcel.courier_status_updated_at
    completing_delivery = status == 'delivered' and (parcel.status == Shipment.Status.DISPATCHED or
        (parcel.status == Shipment.Status.READY and parcel.locked_at is not None))
    if latest and modified_at:
        if modified_at < latest or (modified_at == latest and not completing_delivery):
            return parcel
    elif latest and not modified_at:
        # A dated snapshot cannot be replaced by an event with unknown ordering. A delivery can still complete it.
        if not completing_delivery:
            return parcel
    if parcel.status == Shipment.Status.DELIVERED and status != 'delivered' and not modified_at:
        return parcel
    if (parcel.status == Shipment.Status.READY and parcel.locked_at
            and status in ('collected', 'in-transit', 'out-for-delivery', 'delivered')):
        from .operations import _record_actual_dispatch
        # Collection is an observed fact, even if the stock count needs reconciliation.
        parcel = _record_actual_dispatch(parcel, None, parcel.waybill or parcel.tracking_number,
            stock_override=True, stock_override_reason='Actual collection confirmed by courier webhook')
        completing_delivery = status == 'delivered'
    previous = parcel.courier_status
    fields = []
    if previous != status:
        parcel.courier_status = status
        fields.append('courier_status')
        record_audit(company=parcel.company, actor=None, patient=parcel.patient, action='shipment.tracking_updated',
                     target=parcel, metadata={'source': 'courier', 'waybill': parcel.waybill or parcel.tracking_number,
                                             'previous_status': previous, 'status': status,
                                             'event_at': modified_at.isoformat() if modified_at else None})
    if modified_at and modified_at != latest:
        parcel.courier_status_updated_at = modified_at
        fields.append('courier_status_updated_at')
    if completing_delivery:
        parcel.status, parcel.delivered_at = Shipment.Status.DELIVERED, delivered_at or modified_at or timezone.now()
        fields += ['status', 'delivered_at']
        record_audit(company=parcel.company, actor=None, patient=parcel.patient, action='shipment.delivered',
                     target=parcel, metadata={'source': 'courier', 'waybill': parcel.waybill or parcel.tracking_number})
        PatientEvent.objects.create(company=parcel.company, patient=parcel.patient, category=PatientEvent.Category.DELIVERY,
                                    title='Shipment delivered', detail='The Courier Guy delivered your parcel.',
                                    source_type='care.shipment', source_id=str(parcel.pk))
    if fields:
        parcel.save(update_fields=(*fields, 'updated_at'))
    return parcel
