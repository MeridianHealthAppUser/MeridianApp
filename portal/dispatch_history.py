"""Shared presentation of actual dispatches in Shipping and patient records."""
from django.urls import reverse
from care.models import DeliveryAddress, Shipment

DISPATCHED_STATUSES = (Shipment.Status.DISPATCHED, Shipment.Status.DELIVERED)


def history_queryset(queryset):
    return queryset.filter(status__in=DISPATCHED_STATUSES).select_related(
        'patient', 'authorization__prescribed_by', 'patient__assigned_doctor').prefetch_related(
        'items__product').order_by('-dispatched_at', '-pk')


def prepare_history_shipments(shipments, company, *, workspace=False, can_open=True):
    shipments = list(shipments)
    addresses = {address.patient_id: address for address in DeliveryAddress.objects.filter(
        company=company, patient_id__in={shipment.patient_id for shipment in shipments})}
    for shipment in shipments:
        shipment.doctor = shipment_doctor(shipment)
        shipment.history_delivery = dispatch_delivery(shipment, addresses.get(shipment.patient_id))
        shipment.history_record_url = (reverse('portal:ops-shipment-detail', args=[shipment.pk]) +
                                       ('?workspace=patient' if workspace else '')) if can_open else ''
    return shipments


def shipment_doctor(shipment):
    """Who prescribed what's in the parcel, or else the patient's prescribing doctor."""
    authorization = shipment.authorization
    return authorization.prescribed_by if authorization and authorization.prescribed_by_id else shipment.patient.assigned_doctor


def dispatch_delivery(shipment, address=None):
    """The saved dispatch address, then the booked address, then a legacy record's current address."""
    from care.courier import booked_delivery

    snapshot = shipment.dispatch_snapshot or {}
    delivery = snapshot.get('delivery_address') or {}
    if not delivery.get('lines'):
        delivery = booked_delivery(shipment)
    if delivery.get('lines'):
        return delivery
    if address is not None:
        return {'lines': address.lines(), 'phone': address.phone, 'recipient': address.recipient}
    return {}
