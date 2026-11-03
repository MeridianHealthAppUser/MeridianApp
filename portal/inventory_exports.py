"""Authorised Inventory and Catalogue tables for the shared XLSX endpoint."""

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Prefetch
from django.http import Http404
from django.shortcuts import get_object_or_404

from care.inventory import effective_awc, stock_rows
from care.models import MedicationDose, MedicationProduct, Shipment, ShipmentItem, StartingPlan, StartingPlanStep
from care.operations import week_stock
from care.starting_plans import plan_summary

from .export_support import ExportTable
from .filter_menus import without_all
from .operations_forms import CatalogueFilterForm
from .inventory_filters import InventorySearchForm, filter_inventory_rows


TABLES = frozenset(('inventory', 'catalogue-products', 'catalogue-doses', 'catalogue-plans', 'inventory-reservations'))


def inventory_table(table, request, company, membership):
    """Build all matching table rows, without applying list pagination or changing stock."""
    if table not in TABLES:
        return None
    if (membership is None or not membership.is_admin or not membership.is_active
            or membership.company_id != company.pk or membership.user_id != request.user.pk):
        raise PermissionDenied('This export is for practice administrators.')
    if table == 'inventory':
        return _inventory(request, company)
    if table == 'catalogue-products':
        return _products(request, company)
    if table == 'catalogue-doses':
        return _doses(company)
    if table == 'catalogue-plans':
        return _plans(company)
    return _reservations(request, company)


def _inventory(request, company):
    from .operations_views import shipping_week, this_week

    form = InventorySearchForm(request.GET)
    if not form.is_valid():
        raise ValidationError('Correct the inventory search before exporting.')
    week = shipping_week(company)
    planning = {row['product'].pk: row for row in week_stock(
        company, week, consumption_week_start=this_week(), include_inactive=True)}
    next_week = week + timedelta(days=7)
    headings = ('Product', 'Strength', 'Category', 'On hand', 'Reserved', 'Available', 'AWC', 'Lead time (weeks)', 'Safety stock',
                f'This week’s parcels ({week:%-d %b %Y} – {week + timedelta(days=6):%-d %b %Y})',
                f'Next week’s parcels ({next_week:%-d %b %Y} – {next_week + timedelta(days=6):%-d %b %Y})')

    def rows():
        for row in filter_inventory_rows(stock_rows(company), form.cleaned_data['q']):
            product, demand = row['product'], planning.get(row['product'].pk, {})
            yield (product.name, product.strength, product.get_category_display(), row['on_hand'], row['reserved'],
                   row['available'], effective_awc(product, demand.get('average', 0)), getattr(product, 'lead_time_weeks', None), product.safety_stock,
                   demand.get('to_ship', 0), demand.get('next_week', 0))

    return ExportTable('Inventory', headings, rows(), widths=(38, 22, 25, 14, 14, 14, 14, 20, 16, 37, 37))


def _products(request, company):
    from .operations_views import _search

    form = CatalogueFilterForm(without_all(request.GET, 'status'))
    if not form.is_valid():
        raise ValidationError('Correct the catalogue filters before exporting.')
    products = MedicationProduct.objects.filter(company=company).order_by('name', 'strength', 'pk')
    products = _search(products, form.cleaned_data['q'], 'name', 'strength')
    if len(form.cleaned_data['status']) == 1:
        products = products.filter(is_active=form.cleaned_data['status'] == ['active'])
    rows = ((product.name, product.strength, product.get_category_display(), product.price,
             'Required' if product.requires_authorisation else 'Not required',
             'Published' if product.is_active else 'Hidden') for product in products.iterator(chunk_size=1000))
    return ExportTable('Catalogue products', ('Product', 'Strength', 'Category', 'Reference price (ZAR)',
                                             'Authorisation', 'Visibility'), rows, widths=(38, 22, 25, 24, 22, 18))


def _doses(company):
    doses = MedicationDose.objects.filter(company=company, pen__company=company).select_related('pen').order_by(
        'pen__name', 'amount_mg', 'frequency', 'pk')
    rows = ((dose.medicine, dose.label, dose.dispatch_text, dose.price, dose.billing_text,
             dose.get_tier_display(), 'Offered' if dose.is_active else 'Hidden') for dose in doses.iterator(chunk_size=1000))
    return ExportTable('Catalogue doses', ('Medicine', 'Dose', 'Sent', 'Price (ZAR)', 'Billing interval', 'Tier', 'Offered'),
                       rows, widths=(36, 25, 42, 18, 25, 24, 16))


def _plans(company):
    steps = StartingPlanStep.objects.filter(company=company, dose__company=company, dose__pen__company=company).select_related('dose__pen')
    plans = StartingPlan.objects.filter(company=company, then_dose__company=company,
                                       then_dose__pen__company=company).select_related('then_dose__pen').prefetch_related(
        Prefetch('steps', queryset=steps)).order_by('medicine', 'name', 'pk')
    rows = ((plan.medicine, plan.name, plan_summary(plan), plan.default_weeks,
             'Offered' if plan.is_active else 'Hidden') for plan in plans.iterator(chunk_size=500))
    return ExportTable('Catalogue starting plans', ('Medicine', 'Name', 'Plan', 'Usual length (weeks)', 'Offered'),
                       rows, widths=(36, 28, 75, 25, 16))


def _reservations(request, company):
    from care.billing import parcel_payment
    from .operations_views import shipment_doctor

    try:
        pk = int(request.GET.get('pk', ''))
    except (TypeError, ValueError):
        raise Http404('Choose a product from this practice.') from None
    product = get_object_or_404(MedicationProduct, company=company, pk=pk)
    items = ShipmentItem.objects.filter(
        company=company, product=product, shipment__company=company, shipment__patient__company=company,
        shipment__status=Shipment.Status.READY, shipment__locked_at__isnull=False,
    ).select_related('shipment__patient__assigned_doctor', 'shipment__authorization__prescribed_by',
                     'shipment__subscription', 'shipment__pharmacy_order').order_by('shipment__scheduled_for', 'pk')

    def rows():
        details = {}
        for item in items.iterator(chunk_size=1000):
            parcel = item.shipment
            if parcel.pk not in details:
                details[parcel.pk] = (shipment_doctor(parcel), parcel_payment(parcel))
            doctor, payment = details[parcel.pk]
            payment_label = {'paid': 'Paid', 'due': 'Due', 'retrying': 'Retrying', 'failed': 'Failed'}.get(payment[0], 'Not paid')
            yield (str(parcel.patient), parcel.subscription.plan_name if parcel.subscription_id else '',
                   doctor.titled_name if doctor else '', product.name, product.strength, item.quantity, item.dose,
                   parcel.scheduled_for, payment_label, payment[1], parcel.pk, parcel.waybill, parcel.tracking_number)

    return ExportTable('Inventory reservations', ('Customer', 'Subscription', 'Doctor', 'Product', 'Strength', 'Quantity',
        'Dose', 'Due date', 'Payment', 'Payment details', 'Shipment', 'Waybill', 'Tracking'), rows(),
        widths=(30, 35, 30, 36, 22, 14, 28, 18, 18, 45, 14, 28, 28))
