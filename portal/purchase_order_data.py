"""One scoped search/filter rule for purchase order lists, inversions and exports."""
from django import forms
from django.db.models import Case, CharField, Count, Prefetch, Q, Sum, Value, When
from django.db.models.functions import Cast, Concat, LPad
from django.utils import timezone
from care.models import MedicationProduct, PurchaseOrder, PurchaseOrderItem
from .filter_menus import TickedChoiceField, without_all


class PurchaseHistoryFilterForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, label='Search purchase orders')
    category = TickedChoiceField(label='Category', choices=MedicationProduct.Category.choices)


class PurchaseOrderFilterForm(PurchaseHistoryFilterForm):
    eta = TickedChoiceField(label='ETA', choices=(('overdue', 'Overdue'), ('upcoming', 'Today or later'), ('unset', 'Not set')))


def scoped_items(company):
    return PurchaseOrderItem.objects.filter(company=company, purchase_order__company=company,
        product__company=company).select_related('product', 'purchase_order__created_by').order_by('pk')


def _match(term, *, prefix=''):
    condition = (Q(**{prefix + 'product_name__icontains': term}) | Q(**{prefix + 'strength__icontains': term}))
    categories = [value for value, label in MedicationProduct.Category.choices if term.casefold() in str(label).casefold()]
    return condition | Q(**{prefix + 'category__in': categories})


def _references(queryset, prefix=''):
    field = prefix + 'number'
    number = Cast(field, output_field=CharField())
    padded = Case(When(**{field + '__lt': 100000}, then=LPad(number, 5, Value('0'))),
                  default=number, output_field=CharField())
    return queryset.annotate(_po_reference=Concat(Value('PO-'), padded))


def purchase_orders(company, data, *, history=False):
    form = (PurchaseHistoryFilterForm if history else PurchaseOrderFilterForm)(without_all(data, 'category', 'eta'))
    status = PurchaseOrder.Status.RECEIVED if history else PurchaseOrder.Status.OPEN
    orders = _references(PurchaseOrder.objects.filter(company=company, status=status).select_related('created_by'))
    if not form.is_valid():
        return form, orders.none()
    safe_lines = scoped_items(company)
    if form.cleaned_data['category']:
        orders = orders.filter(items__in=safe_lines.filter(category__in=form.cleaned_data['category']))
    for term in form.cleaned_data['q'].split():
        condition = Q(items__in=safe_lines.filter(_match(term))) | Q(_po_reference__icontains=term)
        orders = orders.filter(condition)
    if not history and form.cleaned_data['eta']:
        eta_condition = Q(pk__in=[])
        for choice in form.cleaned_data['eta']:
            if choice == 'overdue': eta_condition |= Q(eta__lt=timezone.localdate())
            elif choice == 'upcoming': eta_condition |= Q(eta__gte=timezone.localdate())
            elif choice == 'unset': eta_condition |= Q(eta__isnull=True)
        orders = orders.filter(eta_condition)
    return form, orders.distinct().order_by('-created_at', '-pk').prefetch_related(
        Prefetch('items', queryset=safe_lines, to_attr='display_items'))


def history_items(company, data):
    form = PurchaseHistoryFilterForm(without_all(data, 'category'))
    items = _references(scoped_items(company).filter(purchase_order__status=PurchaseOrder.Status.RECEIVED), 'purchase_order__')
    if not form.is_valid():
        return form, items.none()
    if form.cleaned_data['category']:
        items = items.filter(category__in=form.cleaned_data['category'])
    for term in form.cleaned_data['q'].split():
        condition = _match(term) | Q(_po_reference__icontains=term)
        items = items.filter(condition)
    return form, items


def history_products(company, data):
    form, items = history_items(company, data)
    rows = items.order_by().values('product_id', 'product__name', 'product__strength', 'product__category').annotate(
        ordered_units=Sum('quantity'), received_units=Sum('quantity_received'), order_count=Count('purchase_order', distinct=True))
    return form, rows.order_by('product__name', 'product__strength', 'product_id')


def order_totals(order):
    items = order.display_items
    order.line_count = len(items)
    order.ordered_units = sum(item.quantity for item in items)
    order.received_units = sum(item.quantity_received for item in items)
    order.remaining_units = order.ordered_units - order.received_units
    order.progress = round(order.received_units * 100 / order.ordered_units) if order.ordered_units else 0
    for item in items:
        item.remaining = item.quantity - item.quantity_received
    return order
