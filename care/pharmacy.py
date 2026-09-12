"""Patient supply requests without payments or automatic dispensing."""

from collections import defaultdict
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from practices.models import Company, Patient

from .models import MedicationProduct, PatientEvent, PatientSubscription, PharmacyOrder, PharmacyOrderItem, Shipment, ShipmentItem
from .operations import product_allowance, require_operations_actor
from .services import record_audit


def _patient_context(company, patient, actor):
    company = Company.objects.select_for_update().get(pk=company.pk)
    patient = Patient.objects.select_for_update().filter(pk=patient.pk, company=company, is_active=True).first()
    if (patient is None or not company.is_active or not actor.is_active or patient.user_id != actor.pk
            or not get_user_model().objects.filter(pk=actor.pk, is_active=True).exists()):
        raise PermissionDenied('This basket is not available in your patient portal.')
    return company, patient


def _check_items(order, *, extra_product=None, extra_quantity=None, on_date=None):
    grouped = defaultdict(int)
    products = list(order.items.select_related('product'))
    if extra_product is not None:
        products = [item for item in products if item.product_id != extra_product.pk]
        if extra_quantity:
            products.append(PharmacyOrderItem(product=extra_product, quantity=extra_quantity))
    for item in products:
        product = item.product
        if (product.company_id != order.company_id or not product.is_active
                or (item.pk is not None and item.company_id != order.company_id)):
            raise ValidationError('A basket product is no longer available at this practice.')
        _, remaining, reason = product_allowance(order.patient, product, exclude_order=order, on_date=on_date)
        if reason:
            raise ValidationError(f'{product}: {reason}')
        group = ('group', product.allowance_group) if product.allowance_group else ('product', product.pk)
        grouped[group] += item.quantity
        if grouped[group] > remaining:
            raise ValidationError(f'{product}: your request exceeds the remaining supply allowance ({remaining}).')


@transaction.atomic
def set_basket_quantity(*, company, patient, actor, product, quantity, request=None):
    company, patient = _patient_context(company, patient, actor)
    if type(quantity) is not int or not 0 <= quantity <= 100:
        raise ValidationError('Choose a whole-number quantity between 0 and 100.')
    product = MedicationProduct.objects.filter(pk=product.pk, company=company).first()
    if product is None:
        raise ValidationError('Choose a product from this practice.')
    if not product.is_active and quantity:
        raise ValidationError('This item is not currently available.')
    basket = PharmacyOrder.objects.select_for_update().filter(company=company, patient=patient, status='draft').first()
    if basket is None:
        if not quantity:
            raise ValidationError('There is no basket to update.')
        basket = PharmacyOrder.objects.create(company=company, patient=patient)
    _check_items(basket, extra_product=product, extra_quantity=quantity)
    item = basket.items.filter(product=product).first()
    if quantity == 0:
        if item:
            # Explicit removal from a draft basket only; submitted lines remain immutable.
            item.delete()
    elif item and item.quantity == quantity and item.unit_price == product.price:
        return basket
    else:
        item = item or PharmacyOrderItem(company=company, order=basket, product=product)
        item.quantity, item.unit_price = quantity, product.price
        item.product_name, item.strength = product.name, product.strength
        item.full_clean()
        item.save()
    basket.subtotal = sum((item.quantity * item.unit_price for item in basket.items.all()), Decimal('0'))
    basket.revision += 1
    basket.save(update_fields=('subtotal', 'revision', 'updated_at'))
    record_audit(company=company, actor=actor, patient=patient, action='basket.updated', target=basket,
                 request=request, metadata={'product_id': product.pk, 'quantity': quantity})
    return basket


@transaction.atomic
def submit_basket(*, order, actor, delivery_address, note='', confirm=False, expected_revision=None, request=None):
    company, patient = _patient_context(order.company, order.patient, actor)
    order = PharmacyOrder.objects.select_for_update().get(pk=order.pk, company=company, patient=patient)
    if order.status in (PharmacyOrder.Status.SUBMITTED, PharmacyOrder.Status.ACCEPTED):
        return order
    if order.status != PharmacyOrder.Status.DRAFT or confirm is not True:
        raise ValidationError('Confirm the supply request before submitting the basket.')
    if expected_revision != order.revision:
        raise ValidationError('Your basket changed. Reload and confirm the latest items.')
    if not order.items.exists():
        raise ValidationError('Add at least one item before requesting supply.')
    allowed_fields = ('line1', 'line2', 'city', 'province', 'postal_code', 'phone')
    if not isinstance(delivery_address, dict) or set(delivery_address) - set(allowed_fields):
        raise ValidationError('Check the delivery address.')
    if any(not isinstance(value, str) or len(value) > 200 for value in delivery_address.values()):
        raise ValidationError('Delivery address fields must be no longer than 200 characters.')
    if any(not delivery_address.get(key, '').strip() for key in ('line1', 'city', 'province', 'postal_code', 'phone')):
        raise ValidationError('Complete the delivery address and contact number.')
    if not isinstance(note, str) or len(note) > 1000:
        raise ValidationError('The supply note must be no longer than 1,000 characters.')
    _check_items(order)
    order.delivery_address, order.note = delivery_address, note
    order.status, order.submitted_at = PharmacyOrder.Status.SUBMITTED, timezone.now()
    order.revision += 1
    order.full_clean()
    order.save()
    record_audit(company=company, actor=actor, patient=patient, action='pharmacy_order.submitted', target=order, request=request)
    PatientEvent.objects.create(company=company, patient=patient, category=PatientEvent.Category.MEDICATION,
                                title='Supply request submitted', detail='Your request is awaiting the practice team. No payment was collected.',
                                source_type='care.pharmacyorder', source_id=str(order.pk))
    return order


@transaction.atomic
def cancel_order(*, order, actor, confirm=False, request=None):
    company = Company.objects.select_for_update().get(pk=order.company_id)
    order = PharmacyOrder.objects.select_for_update().select_related('patient').get(pk=order.pk, company=company)
    owns = (actor.is_active and order.patient.is_active and order.patient.user_id == actor.pk
            and get_user_model().objects.filter(pk=actor.pk, is_active=True).exists())
    if not owns:
        require_operations_actor(company, actor)
    if not company.is_active or order.patient.company_id != company.pk:
        raise PermissionDenied('This order is not available.')
    if order.status == PharmacyOrder.Status.CANCELLED:
        return order
    if confirm is not True or order.status not in (PharmacyOrder.Status.DRAFT, PharmacyOrder.Status.SUBMITTED):
        raise ValidationError('Only an unaccepted request can be cancelled here. Contact the care team for a prepared order.')
    order.status = PharmacyOrder.Status.CANCELLED
    order.revision += 1
    order.save(update_fields=('status', 'revision', 'updated_at'))
    record_audit(company=company, actor=actor, patient=order.patient, action='pharmacy_order.cancelled', target=order, request=request)
    return order


@transaction.atomic
def accept_order(*, order, actor, scheduled_for, confirm=False, request=None):
    company = Company.objects.select_for_update().get(pk=order.company_id)
    require_operations_actor(company, actor)
    order = PharmacyOrder.objects.select_for_update().select_related('patient').get(pk=order.pk, company=company)
    if not order.patient.is_active or order.patient.company_id != company.pk:
        raise ValidationError('The patient is not active in this practice.')
    if order.status == PharmacyOrder.Status.ACCEPTED and order.shipment_id:
        return order.shipment
    if confirm is not True or order.status != PharmacyOrder.Status.SUBMITTED or scheduled_for < timezone.localdate():
        raise ValidationError('Confirm an outstanding request with a current or future planned dispatch date.')
    _check_items(order, on_date=scheduled_for)
    items = list(order.items.select_related('product'))
    if not items:
        raise ValidationError('The request has no items.')
    subscription = PatientSubscription.objects.filter(company=company, patient=order.patient, status='active').order_by('-created_at').first()
    shipment = Shipment.objects.create(company=company, patient=order.patient, subscription=subscription,
                                        cycle_number=subscription.cycle_number if subscription else 1,
                                        scheduled_for=scheduled_for, status=Shipment.Status.DRAFT)
    for item in items:
        authorization, _, reason = product_allowance(order.patient, item.product, on_date=scheduled_for, exclude_order=order)
        if reason:
            raise ValidationError(reason)
        ShipmentItem.objects.create(company=company, shipment=shipment, product=item.product, quantity=item.quantity,
                                    dose=authorization.max_dose if authorization else item.product.strength)
        if authorization and not shipment.authorization_id:
            shipment.authorization = authorization
    if shipment.authorization_id:
        shipment.save(update_fields=('authorization', 'updated_at'))
    order.status, order.shipment = PharmacyOrder.Status.ACCEPTED, shipment
    order.revision += 1
    order.save(update_fields=('status', 'shipment', 'revision', 'updated_at'))
    record_audit(company=company, actor=actor, patient=order.patient, action='pharmacy_order.accepted', target=order,
                 request=request, metadata={'shipment_id': shipment.pk})
    return shipment
