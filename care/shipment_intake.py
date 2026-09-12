"""Create manual local shipments through the same authorisation gate as orders."""

from datetime import date, datetime
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from practices.models import Company, Patient
from .models import AuditEvent, MedicationProduct, PatientSubscription, Shipment, ShipmentItem
from .operations import product_allowance, require_operations_actor, shipment_hold_reason
from .services import record_audit


@transaction.atomic
def create_manual_shipment(*, company, patient, actor, product, quantity, scheduled_for, submission_key, request=None):
    company = Company.objects.select_for_update().get(pk=company.pk)
    require_operations_actor(company, actor)
    try:
        submission_key = str(UUID(str(submission_key)))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('The shipment submission reference is invalid. Reload the form.') from None
    previous = AuditEvent.objects.filter(company=company, action='shipment.created',
                                          metadata__submission_key=submission_key).first()
    if previous:
        shipment = Shipment.objects.get(pk=previous.target_id, company=company)
        if (previous.actor_id != actor.pk or shipment.patient_id != patient.pk
                or not shipment.items.filter(product_id=product.pk).exists()):
            raise PermissionDenied('This shipment submission reference belongs to a different request.')
        return shipment
    patient = Patient.objects.filter(pk=patient.pk, company=company, is_active=True).first()
    product = MedicationProduct.objects.filter(pk=product.pk, company=company, is_active=True).first()
    if (patient is None or product is None or type(quantity) is not int or not 1 <= quantity <= 100
            or not isinstance(scheduled_for, date) or isinstance(scheduled_for, datetime)
            or scheduled_for < timezone.localdate()):
        raise ValidationError('Check the active patient, product, quantity and dispatch date.')
    authorization, remaining, reason = product_allowance(patient, product, on_date=scheduled_for)
    if reason or quantity > remaining:
        raise ValidationError(reason or 'The shipment exceeds the remaining supply allowance.')
    subscription = PatientSubscription.objects.filter(company=company, patient=patient, status='active').order_by('-created_at').first()
    shipment = Shipment.objects.create(company=company, patient=patient, subscription=subscription, authorization=authorization,
                                        cycle_number=subscription.cycle_number if subscription else 1,
                                        scheduled_for=scheduled_for, status=Shipment.Status.DRAFT)
    ShipmentItem.objects.create(company=company, shipment=shipment, product=product, quantity=quantity,
                                dose=authorization.max_dose if authorization else product.strength)
    reason = shipment_hold_reason(shipment)
    if reason:
        raise ValidationError(reason)
    record_audit(company=company, actor=actor, patient=patient, action='shipment.created', target=shipment, request=request,
                 metadata={'submission_key': submission_key})
    return shipment
