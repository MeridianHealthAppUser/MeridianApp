"""Stock ledger and live reservations for locked, undispatched shipments."""
from decimal import Decimal

from .demo_safety import require_same_demo_data

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum

from .models import AuditEvent, MedicationProduct, Shipment, ShipmentItem, StockMovement
from .services import record_audit


def effective_awc(product, historical):
    """Use the chosen starting consumption when set, otherwise the dispatch-based average."""
    value = product.starting_awc if product.starting_awc is not None else historical
    return Decimal(str(value))


@transaction.atomic
def set_starting_awc(*, company, actor, product, value, request=None):
    """Let a practice's Super Admin set its initial consumption without changing stock."""
    from .operations import _lock_company

    company = _lock_company(company, actor, catalogue=True)
    product = MedicationProduct.objects.select_for_update().filter(company=company, pk=product.pk).first()
    if product is None:
        raise ValidationError('Choose a product from this practice.')
    if value is not None:
        if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
            raise ValidationError('Enter a non-negative AWC value with at most two decimal places, or leave it unset.')
        value = Decimal(value)
        if not value.is_finite() or value < 0 or value > Decimal('99999999.99'):
            raise ValidationError('Enter an AWC value from 0 to 99999999.99, or leave it unset.')
        MedicationProduct._meta.get_field('starting_awc').run_validators(value)
        value = value.quantize(Decimal('0.01'))
    before = product.starting_awc
    if before != value:
        product.starting_awc = value
        product.save(update_fields=('starting_awc', 'updated_at'))
        record_audit(company=company, actor=actor, action='inventory.starting_awc_updated', target=product,
                     request=request, metadata={'before': str(before) if before is not None else None,
                                                'after': str(value) if value is not None else None})
    return product


def stock_rows(company):
    balances = dict(StockMovement.objects.filter(company=company, product__company=company).values('product_id').annotate(total=Sum('quantity')).values_list('product_id', 'total'))
    reservations = ShipmentItem.objects.filter(company=company, shipment__company=company,
        shipment__patient__company=company, product__company=company,
        shipment__status=Shipment.Status.READY, shipment__locked_at__isnull=False).select_related(
            'shipment__patient__assigned_doctor', 'shipment__authorization__prescribed_by',
            'shipment__subscription', 'shipment__pharmacy_order', 'product',
        ).order_by('shipment__scheduled_for', 'pk')
    reserved = {}
    for item in reservations:
        reserved.setdefault(item.product_id, []).append(item)
    rows = []
    for product in MedicationProduct.objects.filter(company=company).order_by('name', 'strength', 'pk'):
        items = reserved.get(product.pk, [])
        on_hand = balances.get(product.pk, 0)
        total = sum(item.quantity for item in items)
        rows.append(dict(product=product, on_hand=on_hand, reserved=total, available=on_hand-total,
                         shortage=max(0, total-on_hand), reservations=items))
    return rows


@transaction.atomic
def set_lead_time(*, company, actor, product, weeks, request=None):
    """Set the practice's chosen delivery lead time without changing stock or reservations."""
    from .operations import _lock_company

    company = _lock_company(company, actor, catalogue=True)
    product = MedicationProduct.objects.select_for_update().filter(company=company, pk=product.pk).first()
    if product is None:
        raise ValidationError('Choose a product from this practice.')
    if weeks is not None:
        if isinstance(weeks, bool) or not isinstance(weeks, (Decimal, int)):
            raise ValidationError('Enter a non-negative number of weeks with at most two decimal places, or leave it unset.')
        weeks = Decimal(weeks)
        if not weeks.is_finite() or weeks < 0 or weeks > Decimal('9999.99'):
            raise ValidationError('Enter a number of weeks from 0 to 9999.99, or leave it unset.')
        MedicationProduct._meta.get_field('lead_time_weeks').run_validators(weeks)
        weeks = weeks.quantize(Decimal('0.01'))
    before = product.lead_time_weeks
    if before != weeks:
        product.lead_time_weeks = weeks
        product.save(update_fields=('lead_time_weeks', 'updated_at'))
        record_audit(company=company, actor=actor, action='inventory.lead_time_updated', target=product,
                     request=request, metadata={'before': str(before) if before is not None else None,
                                                'after': str(weeks) if weeks is not None else None})
    return product


@transaction.atomic
def set_safety_stock(*, company, actor, product, quantity, request=None):
    """Set the practice's chosen stock buffer without changing its stock ledger."""
    from .operations import _lock_company

    company = _lock_company(company, actor, catalogue=True)
    product = MedicationProduct.objects.select_for_update().filter(company=company, pk=product.pk).first()
    if product is None:
        raise ValidationError('Choose a product from this practice.')
    if quantity is not None:
        if type(quantity) is not int or quantity < 0:
            raise ValidationError('Enter a non-negative whole quantity, or leave safety stock unset.')
        MedicationProduct._meta.get_field('safety_stock').run_validators(quantity)
    before = product.safety_stock
    if before != quantity:
        product.safety_stock = quantity
        product.save(update_fields=('safety_stock', 'updated_at'))
        record_audit(company=company, actor=actor, action='inventory.safety_stock_updated', target=product,
                     request=request, metadata={'before': before, 'after': quantity})
    return product


@transaction.atomic
def receive_stock(*, company, actor, product, quantity, batch='', expires_on=None, reference='', submission_key=None, request=None):
    from .operations import _lock_company
    company = _lock_company(company, actor)
    if submission_key:
        previous = AuditEvent.objects.filter(company=company, actor=actor, action='inventory.received',
                                             metadata__submission_key=submission_key).first()
        if previous:
            return StockMovement.objects.get(company=company, pk=previous.target_id)
    if not MedicationProduct.objects.filter(company=company, pk=product.pk, is_active=True).exists():
        raise ValidationError('Choose an active product from this practice.')
    if type(quantity) is not int or quantity <= 0:
        raise ValidationError('Enter a positive whole quantity.')
    movement = StockMovement(company=company, product=product, quantity=quantity, actor=actor,
                             batch=batch, expires_on=expires_on, reason=reference)
    movement.full_clean()
    movement.save()
    record_audit(company=company, actor=actor, action='inventory.received', target=movement, request=request,
                 metadata={'quantity': quantity, 'product_id': product.pk, 'submission_key': submission_key})
    return movement


@transaction.atomic
def receive_stock_batch(*, company, actor, items, reference, submission_key=None, request=None):
    """Save a complete receipt once. A validation failure rolls back every medication line."""
    from .operations import _lock_company

    company = _lock_company(company, actor)
    if submission_key:
        previous = AuditEvent.objects.filter(company=company, actor=actor, action='inventory.receipt_saved',
                                             metadata__submission_key=submission_key).first()
        if previous:
            return list(StockMovement.objects.filter(company=company, pk__in=previous.metadata['movement_ids']).order_by('pk'))
    if not items or len(items) > 100:
        raise ValidationError('Add between 1 and 100 medication rows.')
    movements = [receive_stock(company=company, actor=actor, reference=reference, request=request, **item) for item in items]
    record_audit(company=company, actor=actor, action='inventory.receipt_saved', target=movements[0], request=request,
                 metadata={'submission_key': submission_key, 'movement_ids': [movement.pk for movement in movements],
                           'line_count': len(movements)})
    return movements


def consume_stock(shipment, actor, *, override=False, reason='', request=None):
    """Called inside dispatch's company lock and transaction, so checking and deducting are atomic."""
    quantities = dict(shipment.items.values('product_id').annotate(total=Sum('quantity')).values_list('product_id', 'total'))
    require_same_demo_data(shipment.patient, *MedicationProduct.objects.filter(pk__in=quantities))
    balances = dict(StockMovement.objects.filter(company=shipment.company, product_id__in=quantities)
                    .values('product_id').annotate(total=Sum('quantity')).values_list('product_id', 'total'))
    shortages = [f'{product}: {quantities[product.pk] - balances.get(product.pk, 0)} short'
                 for product in MedicationProduct.objects.filter(pk__in=quantities)
                 if balances.get(product.pk, 0) < quantities[product.pk]]
    reason = reason.strip()
    if shortages and (override is not True or not reason):
        raise ValidationError('Insufficient stock: ' + '; '.join(shortages) + '. Receive stock or explicitly override with a reason.')
    if len(reason) > 500:
        raise ValidationError('Keep the stock override reason to 500 characters.')
    for product_id, quantity in quantities.items():
        StockMovement.objects.create(company=shipment.company, product_id=product_id, shipment=shipment,
            quantity=-quantity, actor=actor, reason=reason if shortages else 'Shipment dispatched',
            is_override=bool(shortages))
    if shortages:
        record_audit(company=shipment.company, actor=actor, patient=shipment.patient, action='inventory.shortage_overridden',
                     target=shipment, request=request, metadata={'reason': reason, 'shortages': shortages})
