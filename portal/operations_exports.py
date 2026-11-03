"""Complete, filtered Operations table data for XLSX downloads."""
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404

from care.models import DeliveryAddress, PharmacyOrder, Shipment
from care.operations import require_operations_actor
from .export_support import ExportTable
from .filter_menus import without_all
from .operations_forms import OrderFilterForm


TABLES = frozenset(('shipping', 'shipment-items', 'requests', 'request-items'))


def operations_table(table, request, company, membership):
    if table not in TABLES:
        return None
    require_operations_actor(company, request.user, shipping=table in ('shipping', 'shipment-items'))
    if table == 'shipping':
        return _shipping(request, company)
    if table == 'shipment-items':
        return _shipment_items(request, company)
    orders = PharmacyOrder.objects.filter(company=company, patient__company=company).exclude(
        status=PharmacyOrder.Status.DRAFT).select_related('patient', 'shipment')
    if table == 'request-items':
        order = get_object_or_404(orders, pk=_pk(request))
        items = order.items.filter(company=company, product__company=company).order_by('pk')
        return ExportTable('Requested items', ('Product', 'Strength', 'Quantity', 'Reference unit price (ZAR)',
            'Reference line total (ZAR)'), ((item.product_name, item.strength, item.quantity, item.unit_price,
            item.quantity * item.unit_price) for item in items), widths=(42, 25, 14, 29, 29))
    from .operations_views import _search

    form = OrderFilterForm(without_all(request.GET, 'status'))
    if not form.is_valid():
        raise ValidationError('Correct the request filters before exporting.')
    if form.cleaned_data['status']:
        orders = orders.filter(status__in=form.cleaned_data['status'])
    orders = _search(orders, form.cleaned_data['q'], 'patient__first_name', 'patient__last_name')
    return ExportTable('Non-prescription requests', ('Request', 'Patient', 'Requested (SAST)',
        'Reference subtotal (ZAR)', 'Status'), ((order.pk, str(order.patient), order.submitted_at,
        order.subtotal, order.get_status_display()) for order in orders), widths=(14, 32, 26, 29, 30))


def _pk(request):
    try:
        return int(request.GET.get('pk', ''))
    except (TypeError, ValueError):
        raise Http404('Choose a record from this practice.') from None


def _shipments(company):
    return Shipment.objects.filter(company=company, patient__company=company).select_related(
        'patient__assigned_doctor', 'authorization__prescribed_by', 'subscription').prefetch_related('items__product')


def _shipment_items(request, company):
    from .operations_views import batch_text

    shipment = get_object_or_404(_shipments(company), pk=_pk(request))
    items = shipment.dispatch_snapshot.get('items') if shipment.dispatch_snapshot else None
    if not items:
        items = [dict(product=str(item.product), dose=item.dose, quantity=item.quantity,
                      batch_note=item.batch_note) for item in shipment.items.filter(company=company, product__company=company)]
    return ExportTable('Shipment items', ('Product', 'Authorised dose', 'Quantity', 'Batch and expiry'),
        ((item.get('product', ''), item.get('dose', ''), item.get('quantity'), batch_text(item)) for item in items),
        widths=(42, 32, 16, 48))


def _shipping(request, company):
    from care.billing import parcel_payment
    from .operations_views import (_shipping_filter, selected_shipping_stage, shipment_doctor,
                                   shipping_stage_parcels, shipping_week)

    form, shipments = _shipping_filter(request, _shipments(company), shipping_week(company), company=company)
    if not form.is_valid():
        raise ValidationError('Correct the shipment filters before exporting.')
    stage = selected_shipping_stage(request)
    shipments = shipping_stage_parcels(shipments, stage).order_by('scheduled_for', 'patient__last_name', 'pk')
    addresses = {address.patient_id: address for address in DeliveryAddress.objects.filter(
        company=company, patient_id__in=shipments.values('patient_id'))}
    medication = request.GET.get('view') == 'medication'
    products = {str(pk) for pk in form.cleaned_data.get('product', ())}
    headings = ('Patient', 'Doctor', 'Deliver to', 'Contact number', 'Recipient', 'Product', 'Strength', 'Dose',
                'Quantity', 'Payment', 'Status', 'Due date', 'Waybill', 'Note', 'Shipment')

    def rows():
        for shipment in shipments.iterator(chunk_size=500):
            doctor = shipment_doctor(shipment)
            address = addresses.get(shipment.patient_id)
            payment = getattr(form, 'payment_states', {}).get(shipment.pk) or parcel_payment(shipment)
            workflow = ('Pending' if shipment.status == Shipment.Status.DRAFT or
                (shipment.status == Shipment.Status.READY and not shipment.locked_at) else
                'Confirmed & Locked' if shipment.status == Shipment.Status.READY else shipment.get_status_display())
            status = shipment.courier_status_label or workflow if stage == 'tracking' else workflow
            for item in shipment.items.all():
                if item.company_id != company.pk or item.product.company_id != company.pk:
                    continue
                if medication and products and str(item.product_id) not in products:
                    continue
                values = (str(shipment.patient), doctor.titled_name if doctor else '',
                    ', '.join(address.lines()) if address else '', address.phone if address else '',
                    (address.recipient or str(shipment.patient)) if address else '', item.product.name,
                    item.product.strength, item.dose, item.quantity, payment[1], status, shipment.scheduled_for,
                    shipment.waybill or shipment.tracking_number, shipment.note, shipment.pk)
                if medication:
                    yield values[5:9] + values[:5] + values[9:]
                else:
                    yield values

    widths = (30, 30, 58, 24, 28, 38, 22, 28, 14, 44, 28, 19, 28, 52, 14)
    if medication:
        headings = headings[5:9] + headings[:5] + headings[9:]
        widths = widths[5:9] + widths[:5] + widths[9:]
    return ExportTable('Weekly shipping', headings, rows(), widths=widths)
