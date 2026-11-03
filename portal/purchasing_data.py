"""Shared read-only To order search and filters for the table and Excel download."""
from django import forms

from care.models import MedicationProduct
from care.purchasing import to_order_rows
from .filter_menus import TickedChoiceField, without_all
from .inventory_filters import filter_inventory_rows


ORDER_STATUSES = (('needed', 'To order'), ('covered', 'No order needed'), ('setup', 'Missing settings'))


class PurchasingFilterForm(forms.Form):
    q = forms.CharField(label='Search products to order', required=False, max_length=100)
    category = TickedChoiceField(label='Category', choices=MedicationProduct.Category.choices)
    status = TickedChoiceField(label='Order status', choices=ORDER_STATUSES)


def purchasing_rows(company, data):
    data = without_all(data, 'category', 'status')
    form = PurchasingFilterForm(data)
    if not form.is_valid():
        return form, []
    rows = filter_inventory_rows(to_order_rows(company), form.cleaned_data['q'])
    categories, statuses = set(form.cleaned_data['category']), set(form.cleaned_data['status'])
    if categories:
        rows = [row for row in rows if row['product'].category in categories]
    if statuses:
        rows = [row for row in rows if row['status'] in statuses]
    return form, rows
