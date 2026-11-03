"""Explicitly owned synthetic data, with atomic and conservative removal.

Demo records never own accounts. A retained DemoDataSet and protected root tags,
not names or email addresses, decide what can be removed. Unknown dependencies
stop the whole operation instead of broadening its scope.
"""
from collections import defaultdict, deque
from datetime import datetime, time, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone

from practices.models import Company, CompanyMembership, DemoDataSet, Patient
from .models import (
    AuditEvent, ClinicalNote, ClinicalTask, DeliveryAddress, MedicationDose, MedicationProduct, PatientClinicalSummary,
    PatientEvent, PatientSubscription, PurchaseOrder, PurchaseOrderItem, Shipment,
    RecordTag, ShipmentItem, StockMovement, Supplier, SupplierProduct, TreatmentAuthorization, WeightEntry,
)
from .notifications import paused
from .operations import _lock_company
from .services import record_audit

ROOT_MODELS = (Patient, MedicationProduct, Supplier, PurchaseOrder)
# Financial records, emails, uploaded files, messages and unknown future features
# are deliberately absent. They must never be cascaded away by this tool.
SAFE_LABELS = {
    'practices.patient', 'care.medicationproduct', 'care.supplier', 'care.purchaseorder',
    'care.medicationdose', 'care.treatmentauthorization', 'care.patientsubscription',
    'care.shipment', 'care.shipmentitem', 'care.stockmovement', 'care.purchaseorderitem',
    'care.supplierproduct', 'care.patientclinicalsummary', 'care.patientsummaryedit',
    'care.weightentry', 'care.deliveryaddress', 'care.patientevent',
    'care.patientmedicalprofile', 'care.patientmedicalprofilerevision',
    'care.patientcommunicationpreference',
    'care.clinicalnote', 'care.clinicaltask', 'care.clinicalencounter',
    'care.tasktagassignment', 'care.clinicalnotetagassignment',
}
MAX_OWNED_RECORDS = 10000
ACTIONS = ('demo_data.created', 'demo_data.cleared', 'demo_data.rebuilt')


def _counts(dataset):
    if dataset is None or dataset.status != DemoDataSet.Status.ACTIVE:
        return {}
    scope = {'company_id': dataset.company_id, 'demo_data_set_id': dataset.pk}
    return {
        'patients': Patient.objects.filter(**scope).count(),
        'products': MedicationProduct.objects.filter(**scope).count(),
        'suppliers': Supplier.objects.filter(**scope).count(),
        'purchase_orders': PurchaseOrder.objects.filter(**scope).count(),
        'subscriptions': PatientSubscription.objects.filter(company_id=dataset.company_id,
            patient__demo_data_set_id=dataset.pk).count(),
        'shipments': Shipment.objects.filter(company_id=dataset.company_id,
            patient__demo_data_set_id=dataset.pk).count(),
        'stock_movements': StockMovement.objects.filter(company_id=dataset.company_id,
            product__demo_data_set_id=dataset.pk).count(),
    }


def demo_status(company):
    """Read-only status; cleared sets remain available for audit and safe retries."""
    latest = DemoDataSet.objects.filter(company=company).order_by('-created_at', '-pk').first()
    active = latest if latest and latest.status == DemoDataSet.Status.ACTIVE else None
    return {'active': active is not None, 'dataset': active, 'latest': latest,
            'counts': _counts(active), 'version': active.updated_at.isoformat() if active else ''}


def _key(value):
    if not isinstance(value, (str, UUID)):
        raise ValidationError('Refresh the page and try again.')
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise ValidationError('Refresh the page and try again.') from None


def _previous(company, actor, action, key, dataset):
    previous = AuditEvent.objects.filter(company=company, action__in=ACTIONS,
                                         metadata__submission_key=key).first()
    if not previous:
        return None
    requested = str(dataset.pk) if dataset is not None else ''
    if previous.actor_id != actor.pk or previous.action != action or previous.metadata.get('requested_dataset', '') != requested:
        raise ValidationError('This request has already been used. Refresh the page and try again.')
    return DemoDataSet.objects.get(company=company, pk=previous.metadata['result_dataset'])


def _active(company, dataset, expected_updated):
    qs = DemoDataSet.objects.select_for_update().filter(company=company)
    if dataset is not None:
        found = qs.filter(pk=dataset.pk).first()
    else:
        found = qs.filter(status=DemoDataSet.Status.ACTIVE).first()
    if not found or found.status != DemoDataSet.Status.ACTIVE:
        raise ValidationError('There is no active demo dataset to change. Refresh the page.')
    version = expected_updated
    if version is None and dataset is not None:
        version = getattr(dataset, 'updated_at', None)
    if version is not None:
        version = version.isoformat() if hasattr(version, 'isoformat') else str(version)
        if found.updated_at.isoformat() != version:
            raise ValidationError('The demo dataset changed. Refresh the page and try again.')
    return found


def _audit(company, actor, action, key, result, requested, request, **metadata):
    record_audit(company=company, actor=actor, action=action, target=result, request=request,
        metadata={'submission_key': key, 'requested_dataset': str(requested.pk) if requested else '',
                  'result_dataset': str(result.pk), **metadata})


@transaction.atomic
def create_demo_data(*, company, actor, submission_key, request=None):
    company = _lock_company(company, actor, catalogue=True)
    key = _key(submission_key)
    previous = _previous(company, actor, ACTIONS[0], key, None)
    if previous:
        return previous
    if DemoDataSet.objects.filter(company=company, status=DemoDataSet.Status.ACTIVE).exists():
        raise ValidationError('Demo data already exists. Use Rebuild to replace the current demo dataset.')
    with paused():
        dataset = _seed(company, actor)
    _audit(company, actor, ACTIONS[0], key, dataset, None, request, counts=dataset.summary['counts'])
    return dataset


@transaction.atomic
def clear_demo_data(*, company, actor, submission_key, dataset=None, expected_updated=None, request=None):
    company = _lock_company(company, actor, catalogue=True)
    key = _key(submission_key)
    previous = _previous(company, actor, ACTIONS[1], key, dataset)
    if previous:
        return previous
    current = _active(company, dataset, expected_updated)
    removed = _clear(current, actor)
    _audit(company, actor, ACTIONS[1], key, current, dataset, request, removed=removed)
    return current


@transaction.atomic
def rebuild_demo_data(*, company, actor, submission_key, dataset=None, expected_updated=None, request=None):
    company = _lock_company(company, actor, catalogue=True)
    key = _key(submission_key)
    previous = _previous(company, actor, ACTIONS[2], key, dataset)
    if previous:
        return previous
    current = _active(company, dataset, expected_updated)
    removed = _clear(current, actor)
    with paused():
        replacement = _seed(company, actor)
    _audit(company, actor, ACTIONS[2], key, replacement, dataset, request,
           previous_dataset=str(current.pk), removed=removed, counts=replacement.summary['counts'])
    return replacement


def _unsafe(label):
    raise ValidationError(f'Demo data is linked to {label}. Nothing was removed. Resolve this dependency before deleting or rebuilding demo data.')


def _owned_graph(dataset):
    """Discover only a closed graph of tagged roots and explicitly allowed children."""
    graph = defaultdict(set)
    for model in ROOT_MODELS:
        # Do not silently omit malformed foreign-company ownership tags.
        graph[model].update(model.objects.select_for_update().filter(demo_data_set=dataset).values_list('pk', flat=True))
    pending = deque(model for model, ids in graph.items() if ids)
    scanned = defaultdict(set)
    while pending:
        model = pending.popleft()
        new_ids = graph[model] - scanned[model]
        if not new_ids:
            continue
        scanned[model].update(new_ids)
        for relation in model._meta.related_objects:
            related = relation.related_model
            if related is AuditEvent:
                # Keep every audit row; its nullable patient reference is cleared by Django.
                if AuditEvent.objects.filter(**{f'{relation.field.name}__in': new_ids}).exclude(
                        company_id=dataset.company_id).exists():
                    _unsafe('audit records from another practice')
                continue
            rows = related._base_manager.select_for_update().filter(**{f'{relation.field.name}__in': new_ids})
            ids = set(rows.values_list('pk', flat=True))
            if not ids:
                continue
            if related._meta.label_lower not in SAFE_LABELS:
                _unsafe(str(related._meta.verbose_name_plural))
            unseen = ids - graph[related]
            if unseen:
                graph[related].update(unseen)
                pending.append(related)
                if sum(map(len, graph.values())) > MAX_OWNED_RECORDS:
                    _unsafe('more records than this demo tool can safely remove')
    records = {}
    for model, ids in graph.items():
        for obj in model._base_manager.select_for_update().filter(pk__in=ids):
            if obj.company_id != dataset.company_id:
                _unsafe('records from another practice')
            if model in ROOT_MODELS and obj.demo_data_set_id != dataset.pk:
                _unsafe('records outside this demo dataset')
            if isinstance(obj, Patient) and obj.user_id:
                _unsafe('a patient login')
            records[(model, obj.pk)] = obj
    permitted_external = {Company, DemoDataSet, get_user_model()}
    dependencies = {}
    for key, obj in records.items():
        refs = set()
        for field in obj._meta.concrete_fields:
            if not field.is_relation or not getattr(obj, field.attname, None):
                continue
            related = field.remote_field.model
            value = getattr(obj, field.attname)
            if related is RecordTag:
                # A label is shared practice configuration, never demo-owned.
                if not RecordTag.objects.filter(pk=value, company_id=dataset.company_id).exists():
                    _unsafe('records from another practice')
                continue
            if related in permitted_external:
                continue
            ref = (related, value)
            if ref not in records:
                _unsafe('records outside this demo dataset')
            refs.add(ref)
        dependencies[key] = refs
    return records, dependencies


def _clear(dataset, actor):
    records, dependencies = _owned_graph(dataset)
    # Delete leaves first so PROTECT relationships remain enforced. A cycle is
    # refused rather than relying on cascades or temporarily disabling constraints.
    incoming = {key: 0 for key in records}
    for refs in dependencies.values():
        for ref in refs:
            incoming[ref] += 1
    ready = deque(key for key, count in incoming.items() if count == 0)
    ordered = []
    while ready:
        key = ready.popleft()
        ordered.append(key)
        for ref in dependencies[key]:
            incoming[ref] -= 1
            if incoming[ref] == 0:
                ready.append(ref)
    if len(ordered) != len(records):
        _unsafe('a circular record dependency')
    removed = defaultdict(int)
    for model, pk in ordered:
        model._base_manager.filter(pk=pk).delete()
        removed[model._meta.label_lower] += 1
    dataset.status = DemoDataSet.Status.CLEARED
    dataset.cleared_at = timezone.now()
    dataset.cleared_by = actor
    dataset.summary = {**dataset.summary, 'removed': dict(removed)}
    dataset.save(update_fields=('status', 'cleared_at', 'cleared_by', 'summary', 'updated_at'))
    return dict(removed)


def _seed(company, actor):
    """Bounded local ORM writes only; never invokes payment, email or courier APIs."""
    dataset = DemoDataSet.objects.create(company=company, created_by=actor)
    tag = str(dataset.pk)[:8]
    today, now = timezone.localdate(), timezone.now()
    week = today - timedelta(days=today.weekday())
    doctor = CompanyMembership.objects.filter(company=company, is_active=True, user__is_active=True,
        clinician_type__in=CompanyMembership.PRESCRIBER_TYPES).select_related('user').order_by('pk').first()
    prescriber = doctor.user if doctor else None
    products, doses = [], []
    specs = (('Semaglutide', '1.0 mg', '1', '1995', '.14', 5),
             ('Semaglutide', '2.0 mg', '2', '2295', '.28', 3),
             ('Tirzepatide', '5.0 mg', '5', '3295', '.42', 12),
             ('Creatine sachets', '30 × 5 g', None, '240', '.14', 5))
    for name, strength, amount, price, lead, safety in specs:
        product = MedicationProduct.objects.create(company=company, demo_data_set=dataset,
            name=f'{name} (Demo {tag})', strength=strength, price=Decimal(price),
            category='weight_management' if amount else 'other', requires_authorisation=bool(amount),
            requires_cold_chain=bool(amount), lead_time_weeks=Decimal(lead), safety_stock=safety,
            description='Synthetic demonstration product. Not for real prescribing, ordering or supply.')
        products.append(product)
        doses.append(MedicationDose.objects.create(company=company, pen=product, amount_mg=Decimal(amount),
            price=product.price, pens_per_dispatch=1, dispatch_weeks=1, billing_weeks=4) if amount else None)
    suppliers = []
    for index, label in enumerate(('Medical Supplies', 'Wellness Wholesale')):
        supplier = Supplier.objects.create(company=company, demo_data_set=dataset,
            name=f'Demo {label} {tag}', contact_name='Fictional supplier contact',
            email=f'demo-{index}-{tag}@example.invalid')
        suppliers.append(supplier)
        for number, product in enumerate(products):
            if index == 1 and number == 2:
                continue
            SupplierProduct.objects.create(company=company, supplier=supplier, product=product,
                supplier_code=f'DEMO-{tag}-{index+1}-{number+1}')
    patients, plans, authorizations, addresses = [], [], [], []
    for index, name in enumerate(('Emma', 'Liam', 'Zola', 'Sara', 'Noah', 'Ava', 'Leo', 'Mia')):
        product_index = (0, 1, 3, 2, 2, 0, 1, 1)[index]
        product, dose = products[product_index], doses[product_index]
        patient = Patient.objects.create(company=company, demo_data_set=dataset, first_name=name,
            last_name=f'Demo {tag}', date_of_birth=today.replace(year=today.year-35-index, day=1),
            assigned_doctor=prescriber, medical_record_number=f'DEMO-{tag}-{index+1:02}',
            phone='0000000000', city='Demonstration City')
        patients.append(patient)
        address = DeliveryAddress.objects.create(company=company, patient=patient,
            recipient=str(patient), street=f'{index+1} Example Road', suburb='Demo suburb',
            city='Demonstration City', province='GP', postal_code='0000', phone='0000000000',
            instructions='Fictional address. Never book a real collection for this demo parcel.')
        addresses.append(address)
        PatientClinicalSummary.objects.create(company=company, patient=patient, height_cm=170+index,
            start_weight_kg=Decimal(95+index), goal_weight_kg=Decimal(80+index),
            medical_history='Fictional demonstration patient. No real clinical information.',
            allergies='Demo: none recorded', pancreatitis='no', thyroid_cancer='no', pregnancy='not_applicable')
        for offset in range(9):
            WeightEntry.objects.create(company=company, patient=patient, recorded_by=actor,
                recorded_on=today-timedelta(weeks=8-offset), weight_kg=Decimal(95+index)-Decimal(offset)/2,
                note='Synthetic demonstration measurement.')
        authorization = None
        if dose and prescriber:
            authorization = TreatmentAuthorization.objects.create(company=company, patient=patient,
                product=product, dose=dose, prescribed_by=prescriber, max_dose=dose.label,
                quantity_per_cycle=12, starts_on=week-timedelta(weeks=9), expires_on=week+timedelta(weeks=24),
                instructions='Fictional demo only. Not a real prescription.')
        authorizations.append(authorization)
        plan = PatientSubscription.objects.create(company=company, patient=patient, authorization=authorization,
            plan_name=f'Demo {product.name.split(" (")[0]} plan', starts_on=week-timedelta(weeks=9),
            review_due_on=week+timedelta(weeks=12), status='paused' if index == 7 else 'active') if authorization else None
        plans.append(plan)
        PatientEvent.objects.create(company=company, patient=patient, category='admin',
            title='Demonstration record', detail='Synthetic patient for exploring the app. No login or billing account was created.',
            is_patient_visible=False)
        ClinicalNote.objects.create(company=company, patient=patient, author=prescriber or actor,
            note_type='general', body='Fictional demonstration note. Review the weight trend and current demo supply. No real clinical advice.')
        if index in (0, 1):
            ClinicalTask.objects.create(company=company, patient=patient, created_by=actor,
                assigned_to=prescriber or actor, title=f'Review {name}’s demo progress',
                description='Synthetic task for exploring the clinical workflow.',
                status='open' if index == 0 else 'in_progress', due_at=now+timedelta(days=index+1))

    def parcel(patient_index, product_index, due, *, quantity=1, status='draft', cycle=1, historical=False):
        patient, product = patients[patient_index], products[product_index]
        dose = doses[product_index]
        dispatched = status in ('dispatched', 'delivered')
        when = timezone.make_aware(datetime.combine(due, time(11))) if historical else now-timedelta(minutes=5)
        waybill = f'DEMO-{tag}-{patient_index+1}-{cycle}' if dispatched else ''
        address = addresses[patient_index]
        snapshot = {'patient_name': str(patient), 'company_name': company.name,
            'scheduled_for': due.isoformat(), 'waybill': waybill,
            'delivery_address': {'recipient': str(patient), 'lines': address.lines(), 'phone': address.phone},
            'items': [{'product': str(product), 'dose': dose.label if dose else product.strength,
                       'quantity': quantity, 'batch_note': f'DEMO-{tag}'}]} if dispatched else {}
        shipment = Shipment.objects.create(company=company, patient=patient,
            subscription=plans[patient_index] if product_index != 3 else None,
            authorization=authorizations[patient_index] if product_index == (0, 1, 3, 2, 2, 0, 1, 1)[patient_index] else None,
            cycle_number=cycle, scheduled_for=due, status=status,
            locked_at=when if status in ('ready', 'dispatched', 'delivered') else None,
            prepared_at=when if status in ('ready', 'dispatched', 'delivered') else None,
            prepared_by=actor if status in ('ready', 'dispatched', 'delivered') else None,
            dispatched_at=when if dispatched else None,
            delivered_at=when+timedelta(minutes=1) if status == 'delivered' else None,
            waybill=waybill, tracking_number=waybill, courier_status='delivered' if status == 'delivered' else 'in-transit' if dispatched else '',
            courier_status_updated_at=when if dispatched else None,
            waybill_created_at=when if dispatched else None, dispatch_snapshot=snapshot,
            note='Synthetic demo parcel. No courier booking or payment was made.',
            hold_reason='The demo subscription is paused.' if status == 'held' else '')
        ShipmentItem.objects.create(company=company, shipment=shipment, product=product,
            dose=dose.label if dose else product.strength, quantity=quantity, batch_note=f'DEMO-{tag}' if dispatched else '')
        if dispatched:
            movement = StockMovement.objects.create(company=company, product=product, shipment=shipment,
                quantity=-quantity, actor=actor, batch=f'DEMO-{tag}', reason='Synthetic demo dispatch')
            StockMovement.objects.filter(pk=movement.pk).update(created_at=when)
        return shipment

    # Eight complete weeks produce non-flat charts and exact drill-down rows.
    for week_index in range(8):
        due = week-timedelta(weeks=8-week_index)+timedelta(days=1)
        for product_index, patient_index in enumerate((0, 1, 3, 2)):
            parcel(patient_index, product_index, due, quantity=1+(week_index+product_index) % 3,
                   status='delivered', cycle=week_index+1, historical=True)
    due = min(today, week+timedelta(days=1))
    for index, status in enumerate(('draft', 'draft', 'draft', 'ready', 'ready', 'dispatched', 'delivered', 'held')):
        product_index = (0, 1, 3, 2, 2, 0, 1, 1)[index]
        if not prescriber and status in ('ready', 'dispatched', 'delivered'):
            product_index = 3
        parcel(index, product_index, due, status=status, cycle=9)
        if index != 7:
            parcel(index, product_index, week+timedelta(weeks=1, days=1), cycle=10)

    next_number = (PurchaseOrder.objects.filter(company=company).aggregate(value=Max('number'))['value'] or 0)+1
    order_specs = (((0, 8, 0), (1, 6, 0)), ((1, 10, 3), (2, 8, 0)), ((3, 6, 6),))
    for index, lines in enumerate(order_specs):
        complete = all(quantity == received for _, quantity, received in lines)
        when = now-timedelta(days=7-index*2)
        order = PurchaseOrder.objects.create(company=company, demo_data_set=dataset,
            number=next_number+index, submission_key=uuid4(), created_by=actor, supplier=suppliers[0],
            supplier_name=suppliers[0].name, supplier_email=suppliers[0].email,
            eta=today+timedelta(days=index+1) if not complete else today-timedelta(days=1),
            status='received' if complete else 'open', received_at=when if complete else None)
        PurchaseOrder.objects.filter(pk=order.pk).update(created_at=when)
        for product_index, quantity, received in lines:
            product = products[product_index]
            line = PurchaseOrderItem.objects.create(company=company, purchase_order=order, product=product,
                quantity=quantity, quantity_received=received, product_name=product.name,
                strength=product.strength, category=product.category,
                supplier_code=f'DEMO-{tag}-1-{product_index+1}')
            if received:
                movement = StockMovement.objects.create(company=company, product=product,
                    purchase_order_item=line, quantity=received, actor=actor, batch=f'DEMO-PO-{tag}-{index+1}',
                    expires_on=today+timedelta(days=365), reason=f'Synthetic receipt: {order.reference}')
                StockMovement.objects.filter(pk=movement.pk).update(created_at=when)
    # Opening receipts reconcile the complete historical ledger to useful current
    # balances; the low Tirzepatide balance demonstrates a reservation shortage.
    for product, desired in zip(products, (20, 8, 1, 2)):
        balance = StockMovement.objects.filter(company=company, product=product).aggregate(total=Sum('quantity'))['total'] or 0
        movement = StockMovement.objects.create(company=company, product=product,
            quantity=desired-balance, actor=actor, batch=f'DEMO-OPEN-{tag}',
            expires_on=today+timedelta(days=365), reason='Synthetic opening stock for this demo dataset')
        StockMovement.objects.filter(pk=movement.pk).update(created_at=now-timedelta(weeks=9))
    dataset.summary = {'schema_version': 1, 'generated_on': today.isoformat(),
                       'prescriber_available': bool(prescriber), 'counts': _counts(dataset)}
    dataset.save(update_fields=('summary', 'updated_at'))
    return dataset
