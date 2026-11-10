"""Supplier purchase orders and quantities to order from consumption and physical stock."""

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING
from uuid import UUID

from .demo_safety import require_same_demo_data

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Max, Sum
from django.utils import timezone

from .inventory import receive_stock_batch, stock_rows
from .models import AuditEvent, MedicationProduct, PurchaseOrder, PurchaseOrderItem, StockMovement, Supplier, SupplierProduct
from .operations import _lock_company, week_stock
from .services import record_audit


MAX_ORDER_QUANTITY = 2147483647


def outstanding_order_quantities(company):
    """Whole units on open supplier orders, scoped across both the order and its product."""
    return dict(PurchaseOrderItem.objects.filter(
        company=company, purchase_order__company=company, product__company=company,
        purchase_order__status=PurchaseOrder.Status.OPEN,
    ).values('product_id').annotate(total=Sum(F('quantity') - F('quantity_received'))).values_list('product_id', 'total'))


@transaction.atomic
def create_purchase_order(*, company, actor, items, submission_key, supplier=None, eta=None, request=None):
    """Create a complete supplier order once; retrying its key cannot duplicate or change the order.

    The company lock serialises numbering and submission retries. Identity is the selected products and
    quantities plus the optional ETA; catalogue labels are captured only on the first successful submission.
    """
    company = _lock_company(company, actor)
    if (not isinstance(supplier, Supplier) or supplier.pk is None or supplier._state.adding
            or supplier.company_id != company.pk):
        raise ValidationError('Choose a supplier from this practice.')
    supplier = Supplier.objects.select_for_update().filter(company=company, pk=supplier.pk).first()
    if supplier is None:
        raise ValidationError('Choose a supplier from this practice.')
    try:
        key = submission_key if isinstance(submission_key, UUID) else UUID(str(submission_key))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('Use a valid purchase order submission key.') from None
    if eta is not None and (not isinstance(eta, date) or isinstance(eta, datetime)):
        raise ValidationError('Choose a valid estimated arrival date, or leave it blank.')
    if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= 100:
        raise ValidationError('Choose between 1 and 100 products to order.')

    quantities = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise ValidationError('Choose a product and whole quantity for every order line.')
        product, quantity = item.get('product'), item.get('quantity')
        if (not isinstance(product, MedicationProduct) or product.pk is None or product._state.adding
                or product.company_id != company.pk):
            raise ValidationError('Choose active products from this practice.')
        if type(quantity) is not int or not 1 <= quantity <= MAX_ORDER_QUANTITY:
            raise ValidationError(f'Enter a whole order quantity from 1 to {MAX_ORDER_QUANTITY}.')
        if product.pk in quantities:
            raise ValidationError('Choose each product only once per purchase order.')
        quantities[product.pk] = quantity

    previous = PurchaseOrder.objects.filter(company=company, submission_key=key).first()
    if previous is not None:
        previous_quantities = dict(previous.items.filter(company=company).values_list('product_id', 'quantity'))
        if previous.supplier_id != supplier.pk or previous.eta != eta or previous_quantities != quantities:
            raise ValidationError('This submission has already created a different purchase order. Start a new order.')
        return previous

    products = {product.pk: product for product in MedicationProduct.objects.select_for_update().filter(
        company=company, pk__in=quantities, is_active=True,
    ).order_by('pk')}
    if products.keys() != quantities.keys():
        raise ValidationError('Choose active products from this practice.')
    demo_data_set_id = require_same_demo_data(supplier, *products.values())
    supplier_codes = dict(SupplierProduct.objects.filter(company=company, supplier=supplier,
        supplier__company=company, product__company=company, product_id__in=products).values_list('product_id', 'supplier_code'))
    if supplier_codes.keys() != products.keys() or any(not code.strip() for code in supplier_codes.values()):
        raise ValidationError('Every selected product must be linked to this supplier with a supplier product code.')
    number = (PurchaseOrder.objects.filter(company=company).aggregate(last=Max('number'))['last'] or 0) + 1
    if number > MAX_ORDER_QUANTITY:
        raise ValidationError('This practice has reached its purchase order number limit.')
    order = PurchaseOrder(company=company, number=number, submission_key=key, created_by=actor, eta=eta,
                          supplier=supplier, supplier_name=supplier.name, supplier_email=supplier.email,
                          demo_data_set_id=demo_data_set_id)
    order.full_clean()
    order.save()
    for product_id, quantity in quantities.items():
        product = products[product_id]
        line = PurchaseOrderItem(company=company, purchase_order=order, product=product, quantity=quantity,
                                 product_name=product.name, strength=product.strength, category=product.category,
                                 supplier_code=supplier_codes[product.pk])
        line.full_clean()
        line.save()
    record_audit(company=company, actor=actor, action='purchase_order.created', target=order, request=request,
                 metadata={'number': order.number, 'reference': order.reference, 'submission_key': str(key),
                           'supplier_id': supplier.pk, 'supplier_name': supplier.name,
                           'eta': eta.isoformat() if eta is not None else None,
                           'items': [{'product_id': pk, 'quantity': quantity} for pk, quantity in quantities.items()]})
    return order


@transaction.atomic
def receive_purchase_order(*, company, actor, order, items, submission_key, expected_updated=None, request=None):
    """Receive selected outstanding units once, preserving partial-order progress and stock traceability."""
    company = _lock_company(company, actor)
    order = PurchaseOrder.objects.select_for_update().filter(company=company, pk=order.pk).first()
    if order is None:
        raise ValidationError('Choose a purchase order from this practice.')
    try:
        key = submission_key if isinstance(submission_key, UUID) else UUID(str(submission_key))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('Use a valid purchase order receipt key.') from None
    if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= 100:
        raise ValidationError('Receive between 1 and 100 purchase order lines.')

    lines = list(PurchaseOrderItem.objects.select_for_update(of=('self',)).filter(
        purchase_order=order).select_related('product').order_by('pk'))
    require_same_demo_data(order, *(line.product for line in lines))
    by_id = {line.pk: line for line in lines}
    received = {}
    for values in items:
        if not isinstance(values, Mapping):
            raise ValidationError('Choose a purchase order line and quantity for every receipt.')
        item, quantity = values.get('item'), values.get('quantity')
        if (not isinstance(item, PurchaseOrderItem) or item.pk not in by_id or item._state.adding
                or item.company_id != company.pk or item.purchase_order_id != order.pk):
            raise ValidationError('Receive only lines from this purchase order and practice.')
        if item.pk in received:
            raise ValidationError('Receive each purchase order line only once in this receipt.')
        if type(quantity) is not int or not 1 <= quantity <= MAX_ORDER_QUANTITY:
            raise ValidationError(f'Enter a whole received quantity from 1 to {MAX_ORDER_QUANTITY}.')
        batch, expiry = values.get('batch', ''), values.get('expires_on')
        if not isinstance(batch, str) or len(batch.strip()) > 120:
            raise ValidationError('Keep batch references to 120 characters or fewer.')
        if expiry is not None and (not isinstance(expiry, date) or isinstance(expiry, datetime)):
            raise ValidationError('Enter a valid expiry date or leave it blank.')
        received[item.pk] = dict(quantity=quantity, batch=batch.strip(), expires_on=expiry)
    payload = [dict(item_id=pk, quantity=values['quantity'], batch=values['batch'],
                    expires_on=values['expires_on'].isoformat() if values['expires_on'] else None) for pk, values in sorted(received.items())]
    previous = AuditEvent.objects.filter(company=company, action='purchase_order.received',
                                         metadata__submission_key=str(key)).first()
    if previous is not None:
        if previous.target_id != str(order.pk) or previous.metadata.get('items') != payload:
            raise ValidationError('This submission has already recorded a different receipt. Start a new receipt.')
        movements = list(StockMovement.objects.filter(company=company, pk__in=previous.metadata['movement_ids'],
                         purchase_order_item__purchase_order=order).order_by('pk'))
        if len(movements) != len(previous.metadata['movement_ids']):
            raise ValidationError('This receipt could not be read. Refresh the purchase order before continuing.')
        return movements

    if expected_updated is not None and order.updated_at.isoformat() != expected_updated:
        raise ValidationError('This purchase order has changed. Refresh it before receiving stock.')
    if order.status != PurchaseOrder.Status.OPEN or not lines:
        raise ValidationError('Only an open purchase order with outstanding products can be received.')
    if any(line.company_id != company.pk or line.product.company_id != company.pk for line in lines):
        raise ValidationError('Every purchase order line and product must belong to this practice.')
    for pk, values in received.items():
        line = by_id[pk]
        if values['quantity'] > line.quantity - line.quantity_received:
            raise ValidationError(f'The received quantity for {line.product_name} exceeds the quantity outstanding.')
        if not line.product.is_active:
            raise ValidationError(f'Reactivate {line.product_name} in Catalogue before receiving this product.')

    selected = sorted(received.items())
    movements = receive_stock_batch(company=company, actor=actor, request=request,
        items=[dict(product=by_id[pk].product, **values) for pk, values in selected],
        reference=f'Purchase order {order.reference}', submission_key=f'purchase-order:{key}')
    for (pk, values), movement in zip(selected, movements):
        line = by_id[pk]
        movement.purchase_order_item = line
        movement.full_clean()
        movement.save(update_fields=('purchase_order_item', 'updated_at'))
        line.quantity_received += values['quantity']
        line.full_clean()
        line.save(update_fields=('quantity_received', 'updated_at'))
    if all(line.quantity_received == line.quantity for line in lines):
        order.status = PurchaseOrder.Status.RECEIVED
        order.received_at = timezone.now()
    order.save(update_fields=('status', 'received_at', 'updated_at'))
    record_audit(company=company, actor=actor, action='purchase_order.received', target=order, request=request,
                 metadata={'submission_key': str(key), 'items': payload,
                           'movement_ids': [movement.pk for movement in movements],
                           'completed': order.status == PurchaseOrder.Status.RECEIVED})
    return movements


def to_order_rows(company, *, week_start=None, on_order=None):
    """Return active products and max(0, ceil(AWC × lead weeks + safety − on hand − on order)).

    AWC uses its configured starting value when present, otherwise the preceding eight complete calendar weeks.
    Physical on-hand stock is used; shipment reservations are shown separately and never treated as supplier stock.
    On order defaults to the outstanding whole units on real open purchase orders. An explicit mapping of
    product PKs to outstanding quantities overrides that read for callers that already have the same data.
    """
    from .inventory import effective_awc

    if week_start is None:
        today = timezone.localdate()
        week_start = today - timedelta(days=(today.weekday() - 1) % 7)
    if (not isinstance(week_start, date) or isinstance(week_start, datetime) or week_start.weekday() != 1):
        raise ValidationError('Choose a consumption week starting on Tuesday.')
    if on_order is None:
        incoming = outstanding_order_quantities(company)
    else:
        if not isinstance(on_order, Mapping):
            raise ValidationError('Provide on-order quantities by product ID.')
        incoming = dict(on_order)
        if any(type(quantity) is not int or quantity < 0 for quantity in incoming.values()):
            raise ValidationError('On-order quantities must be non-negative whole numbers.')
        if any(type(pk) is not int or pk <= 0 for pk in incoming):
            raise ValidationError('Key on-order quantities by positive product IDs.')
    consumption = {row['product'].pk: row['average'] for row in week_stock(
        company, week_start, consumption_week_start=week_start, include_inactive=True)}
    rows = []
    for balance in stock_rows(company):
        product = balance['product']
        if product.company_id != company.pk or not product.is_active:
            continue
        historical = consumption.get(product.pk, 0)
        awc = effective_awc(product, historical)
        lead, safety = product.lead_time_weeks, product.safety_stock
        quantity_on_order = incoming.get(product.pk, 0)
        missing = tuple(label for label, value in (('Lead time', lead), ('Safety stock', safety)) if value is None)
        if missing:
            quantity, status = None, 'setup'
        else:
            requirement = awc * Decimal(str(lead)) + Decimal(safety) - Decimal(balance['on_hand']) - Decimal(quantity_on_order)
            quantity = max(0, int(requirement.to_integral_value(rounding=ROUND_CEILING)))
            status = 'needed' if quantity else 'covered'
        rows.append(dict(product=product, on_hand=balance['on_hand'], reserved=balance['reserved'], awc=awc,
                         awc_source='starting' if product.starting_awc is not None else 'history',
                         lead_time_weeks=lead, safety_stock=safety, on_order=quantity_on_order,
                         order_quantity=quantity, status=status, missing_fields=missing))
    return rows
