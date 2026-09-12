"""Audited local stock/dispatch operations. No payment, pharmacy or courier API."""

from collections import defaultdict
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient

from .models import (
    MedicationBatch, MedicationProduct, PatientEvent, PatientSubscription,
    PharmacyOrder, PracticeSettings, Shipment, ShipmentItem, StockMovement, TreatmentAuthorization,
)
from .services import record_audit
from .treatment import authorization_is_current, ensure_subscription_eligible


UNDISPATCHED = (Shipment.Status.DRAFT, Shipment.Status.READY, Shipment.Status.HELD)


def require_operations_actor(company, actor, *, catalogue=False):
    roles = (CompanyMembership.Role.SUPER_ADMIN,) if catalogue else (
        CompanyMembership.Role.PRACTICE_ADMIN, CompanyMembership.Role.SUPER_ADMIN,
    )
    if not (company.is_active and actor.is_active and get_user_model().objects.filter(pk=actor.pk, is_active=True).exists()
            and CompanyMembership.objects.filter(company=company, user=actor, is_active=True, role__in=roles).exists()):
        raise PermissionDenied('You do not have permission to change these practice operations.')


def _lock_company(company, actor, **kwargs):
    company = Company.objects.select_for_update().get(pk=company.pk)
    require_operations_actor(company, actor, **kwargs)
    return company


def _stock(batch, quantity, actor, reason, *, shipment=None, key=''):
    if not quantity:
        return None
    if batch.quantity_on_hand + quantity < 0 or batch.quantity_on_hand + quantity > batch.quantity_received:
        raise ValidationError('This movement would create an invalid stock balance.')
    movement = StockMovement(company=batch.company, batch=batch, quantity=quantity,
                             direction=StockMovement.Direction.IN if quantity > 0 else StockMovement.Direction.OUT,
                             reason=reason, shipment=shipment, recorded_by=actor, idempotency_key=key)
    movement.full_clean()
    movement.save()
    batch.quantity_on_hand += quantity
    batch.save(update_fields=('quantity_on_hand', 'updated_at'))
    return movement


@transaction.atomic
def receive_stock(*, company, actor, product, batch_number, received_on, expires_on, quantity,
                  cold_chain_confirmed=False, submission_key='', request=None):
    company = _lock_company(company, actor)
    if submission_key:
        previous = StockMovement.objects.filter(company=company, idempotency_key=f'receive:{submission_key}').first()
        if previous:
            return previous.batch
    product = MedicationProduct.objects.filter(pk=product.pk, company=company, is_active=True).first()
    if product is None:
        raise ValidationError('Choose an active product in this practice.')
    if received_on > timezone.localdate() or expires_on <= received_on or expires_on <= timezone.localdate():
        raise ValidationError('Check the receipt/expiry dates; expired stock cannot be received as available stock.')
    if type(quantity) is not int or not 1 <= quantity <= 100000:
        raise ValidationError('Receive between 1 and 100,000 units.')
    status = MedicationBatch.Status.AVAILABLE
    if product.requires_cold_chain and not cold_chain_confirmed:
        status = MedicationBatch.Status.QUARANTINED
    batch = MedicationBatch(company=company, product=product, batch_number=batch_number.strip(), received_on=received_on,
                            expires_on=expires_on, quantity_received=quantity, quantity_on_hand=0,
                            cold_chain_confirmed=cold_chain_confirmed, status=status)
    batch.full_clean()
    batch.save()
    _stock(batch, quantity, actor, 'Stock received', key=f'receive:{submission_key}' if submission_key else '')
    record_audit(company=company, actor=actor, action='stock.received', target=batch, request=request,
                 metadata={'quantity': quantity, 'quarantined': status == MedicationBatch.Status.QUARANTINED})
    return batch


def _hold_batch_shipments(batch, actor, reason):
    shipments = Shipment.objects.filter(company=batch.company, status__in=UNDISPATCHED, items__batch=batch).distinct()
    for shipment in shipments:
        shipment.status, shipment.hold_reason, shipment.locked_at = Shipment.Status.HELD, reason, None
        shipment.save(update_fields=('status', 'hold_reason', 'locked_at', 'updated_at'))
        record_audit(company=batch.company, actor=actor, patient=shipment.patient, action='shipment.held', target=shipment,
                     metadata={'batch_id': batch.pk})


@transaction.atomic
def change_batch(*, batch, actor, action, reason, quantity=None, cold_chain_confirmed=False, request=None):
    company = _lock_company(batch.company, actor)
    batch = MedicationBatch.objects.select_for_update().select_related('product').get(pk=batch.pk, company=company)
    if batch.status == MedicationBatch.Status.WRITTEN_OFF:
        if action == 'write_off':
            return batch
        raise ValidationError('Written-off stock is retained as history and cannot be reactivated or adjusted.')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 255:
        raise ValidationError('Record a reason, up to 255 characters.')
    if action == 'quarantine':
        batch.status = MedicationBatch.Status.QUARANTINED
        batch.save(update_fields=('status', 'updated_at'))
        _hold_batch_shipments(batch, actor, 'Allocated stock is quarantined. Re-prepare using eligible stock.')
    elif action == 'release':
        if batch.status != MedicationBatch.Status.QUARANTINED or batch.expires_on <= timezone.localdate():
            raise ValidationError('Only a non-expired quarantined batch can be released.')
        if batch.product.requires_cold_chain and not cold_chain_confirmed:
            raise ValidationError('Confirm that the cold-chain issue has been reviewed and stock is safe to release.')
        batch.cold_chain_confirmed, batch.status = cold_chain_confirmed, MedicationBatch.Status.AVAILABLE
        batch.save(update_fields=('cold_chain_confirmed', 'status', 'updated_at'))
    elif action == 'adjust':
        if type(quantity) is not int or quantity == 0:
            raise ValidationError('Enter a non-zero whole-number adjustment.')
        reserved = ShipmentItem.objects.filter(company=company, batch=batch,
            shipment__company=company, shipment__status__in=UNDISPATCHED).aggregate(total=Sum('quantity'))['total'] or 0
        if quantity > 0 and batch.quantity_on_hand + quantity + reserved > batch.quantity_received:
            raise ValidationError('This count includes units already allocated to an undispatched shipment.')
        _stock(batch, quantity, actor, reason.strip())
    elif action == 'write_off':
        # Allocated but undispatched stock is also written off, not returned to
        # availability. Its original allocation remains in the stock ledger.
        _hold_batch_shipments(batch, actor, 'Allocated stock was written off. Re-prepare using eligible stock.')
        ShipmentItem.objects.filter(company=company, batch=batch, shipment__company=company,
                                    shipment__status__in=UNDISPATCHED).update(batch=None)
        _stock(batch, -batch.quantity_on_hand, actor, f'Write-off: {reason.strip()}'[:255])
        batch.status = MedicationBatch.Status.WRITTEN_OFF
        batch.save(update_fields=('status', 'updated_at'))
    else:
        raise ValidationError('Choose a valid stock action.')
    record_audit(company=company, actor=actor, action=f'stock.{action}', target=batch, request=request,
                 metadata={'reason': reason.strip(), 'quantity': quantity if action == 'adjust' else None})
    return batch


def current_product_authorization(patient, product, on_date=None):
    on_date = on_date or timezone.localdate()
    candidates = TreatmentAuthorization.objects.filter(
        company_id=patient.company_id, patient=patient, product=product, status=TreatmentAuthorization.Status.ACTIVE,
        starts_on__lte=on_date, expires_on__gte=on_date,
    ).select_related('company', 'patient', 'product', 'prescribed_by').order_by('-expires_on', '-pk')
    return next((authorization for authorization in candidates if authorization_is_current(authorization, on_date)), None)


def allowance_window(patient, authorization, on_date):
    settings = PracticeSettings.objects.filter(company_id=patient.company_id).first()
    days = max(1, settings.delivery_interval_days if settings else 28)
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
    company = _lock_company(shipment.company, actor)
    shipment = Shipment.objects.select_for_update(of=('self',)).select_related('company', 'patient', 'subscription').get(pk=shipment.pk, company=company)
    if shipment.patient.company_id != company.pk or not shipment.patient.is_active:
        raise ValidationError('The shipment patient is not active in this practice.')
    return company, shipment


def _unallocate(shipment, actor):
    for item in shipment.items.select_related('batch'):
        if item.batch_id:
            batch = MedicationBatch.objects.select_for_update().filter(pk=item.batch_id, company=shipment.company).first()
            if (batch is None or item.company_id != shipment.company_id or batch.product_id != item.product_id
                    or item.quantity <= 0 or batch.status == MedicationBatch.Status.WRITTEN_OFF):
                raise ValidationError('An allocation does not match this shipment and cannot be returned automatically.')
            _stock(batch, item.quantity, actor, 'Undispatched allocation returned', shipment=shipment)
            item.batch = None
            item.save(update_fields=('batch', 'updated_at'))


@transaction.atomic
def prepare_shipment(*, shipment, actor, request=None):
    company, shipment = _locked_shipment(shipment, actor)
    if shipment.status not in UNDISPATCHED:
        raise ValidationError('Only an undispatched shipment can be prepared.')
    reason = shipment_hold_reason(shipment)
    if reason:
        raise ValidationError(reason)
    _unallocate(shipment, actor)
    # Rebuild only mutable preparation lines. Submitted order lines and stock
    # movements retain their history; dispatched shipment lines never enter here.
    quantities = defaultdict(int)
    products = {}
    for item in shipment.items.select_related('product'):
        quantities[(item.product_id, item.dose)] += item.quantity
        products[item.product_id] = item.product
    plans = []
    interval = PracticeSettings.objects.filter(company=company).values_list('delivery_interval_days', flat=True).first() or 28
    treatment_end = max(timezone.localdate(), shipment.scheduled_for) + timedelta(days=interval)
    for (product_id, dose), quantity in quantities.items():
        batches = MedicationBatch.objects.select_for_update().filter(
            company=company, product_id=product_id, status__in=(MedicationBatch.Status.AVAILABLE, MedicationBatch.Status.LOW),
            expires_on__gt=treatment_end, quantity_on_hand__gt=0,
        ).order_by('expires_on', 'pk')
        if products[product_id].requires_cold_chain:
            batches = batches.filter(cold_chain_confirmed=True)
        remaining = quantity
        for batch in batches:
            taken = min(remaining, batch.quantity_on_hand)
            _stock(batch, -taken, actor, 'Stock allocated for dispatch preparation', shipment=shipment)
            plans.append((product_id, dose, batch, taken))
            remaining -= taken
            if not remaining:
                break
        if remaining:
            raise ValidationError(f'Not enough eligible stock for {products[product_id]}. Existing allocations have not changed.')
    shipment.items.all().delete()
    for product_id, dose, batch, quantity in plans:
        ShipmentItem.objects.create(company=company, shipment=shipment, product_id=product_id, dose=dose, batch=batch, quantity=quantity)
    shipment.status, shipment.hold_reason = Shipment.Status.READY, ''
    shipment.prepared_by, shipment.prepared_at, shipment.locked_at = actor, timezone.now(), None
    shipment.save(update_fields=('status', 'hold_reason', 'prepared_by', 'prepared_at', 'locked_at', 'updated_at'))
    record_audit(company=company, actor=actor, patient=shipment.patient, action='shipment.prepared', target=shipment, request=request)
    return shipment


@transaction.atomic
def hold_or_cancel_shipment(*, shipment, actor, cancel=False, reason, request=None):
    company, shipment = _locked_shipment(shipment, actor)
    if shipment.status not in UNDISPATCHED:
        raise ValidationError('A dispatched, delivered or cancelled shipment cannot be changed here.')
    if not reason.strip() or len(reason) > 500:
        raise ValidationError('Give a reason, up to 500 characters.')
    if cancel:
        _unallocate(shipment, actor)
    shipment.status = Shipment.Status.CANCELLED if cancel else Shipment.Status.HELD
    shipment.hold_reason, shipment.locked_at = reason, None
    shipment.save(update_fields=('status', 'hold_reason', 'locked_at', 'updated_at'))
    record_audit(company=company, actor=actor, patient=shipment.patient, action='shipment.cancelled' if cancel else 'shipment.held',
                 target=shipment, request=request, metadata={'reason': reason})
    return shipment


def _validate_allocations(shipment):
    reason = shipment_hold_reason(shipment)
    if reason:
        raise ValidationError(reason)
    interval = PracticeSettings.objects.filter(company=shipment.company).values_list('delivery_interval_days', flat=True).first() or 28
    horizon = max(timezone.localdate(), shipment.scheduled_for) + timedelta(days=interval)
    for item in shipment.items.select_related('batch', 'product'):
        batch = item.batch
        if (not batch or batch.company_id != shipment.company_id or batch.product_id != item.product_id
                or batch.status not in (MedicationBatch.Status.AVAILABLE, MedicationBatch.Status.LOW)
                or batch.expires_on <= horizon or (item.product.requires_cold_chain and not batch.cold_chain_confirmed)):
            raise ValidationError('An allocated batch is no longer eligible. Hold and re-prepare this shipment.')


@transaction.atomic
def lock_shipping_week(*, company, actor, week_start, request=None):
    company = _lock_company(company, actor)
    shipments = list(Shipment.objects.select_for_update(of=('self',)).filter(
        company=company, status=Shipment.Status.READY, scheduled_for__range=(week_start, week_start + timedelta(days=6)),
    ).select_related('company', 'patient', 'subscription'))
    if not shipments:
        raise ValidationError('There are no ready shipments to lock for this week.')
    for shipment in shipments:
        _validate_allocations(shipment)
    for shipment in shipments:
        if not shipment.locked_at:
            shipment.locked_at = timezone.now()
            shipment.save(update_fields=('locked_at', 'updated_at'))
            record_audit(company=company, actor=actor, patient=shipment.patient, action='shipment.locked', target=shipment, request=request)
    return shipments


@transaction.atomic
def dispatch_shipment(*, shipment, actor, tracking_number, confirm=False, request=None):
    company, shipment = _locked_shipment(shipment, actor)
    if shipment.status in (Shipment.Status.DISPATCHED, Shipment.Status.DELIVERED):
        if shipment.tracking_number == tracking_number.strip():
            return shipment
        raise ValidationError('The dispatched shipment record is immutable.')
    if confirm is not True or shipment.status != Shipment.Status.READY or not shipment.locked_at:
        raise ValidationError('Prepare and lock the shipment, then explicitly confirm dispatch.')
    if not tracking_number.strip() or len(tracking_number) > 120:
        raise ValidationError('Enter the actual tracking reference, up to 120 characters.')
    _validate_allocations(shipment)
    actual_day_reason = shipment_hold_reason(shipment, on_date=timezone.localdate())
    if actual_day_reason:
        raise ValidationError(f'Cannot dispatch on this date: {actual_day_reason}')
    shipment.dispatch_snapshot = {
        'patient_name': str(shipment.patient), 'patient_id_number': shipment.patient.id_number,
        'company_name': company.name, 'scheduled_for': shipment.scheduled_for.isoformat(),
        'items': [{'product': str(item.product), 'dose': item.dose, 'quantity': item.quantity,
                   'batch': item.batch.batch_number, 'expires_on': item.batch.expires_on.isoformat()}
                  for item in shipment.items.select_related('product', 'batch')],
    }
    order = getattr(shipment, 'pharmacy_order', None)
    if order:
        shipment.dispatch_snapshot['delivery_address'] = order.delivery_address
    shipment.status, shipment.tracking_number, shipment.dispatched_at = Shipment.Status.DISPATCHED, tracking_number.strip(), timezone.now()
    shipment.save(update_fields=('status', 'tracking_number', 'dispatched_at', 'dispatch_snapshot', 'updated_at'))
    record_audit(company=company, actor=actor, patient=shipment.patient, action='shipment.dispatched', target=shipment, request=request)
    PatientEvent.objects.create(company=company, patient=shipment.patient, category=PatientEvent.Category.DELIVERY,
                                title='Shipment dispatched', detail='Your care team recorded a dispatch. See your treatment page for tracking.',
                                source_type='care.shipment', source_id=str(shipment.pk))
    return shipment


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
