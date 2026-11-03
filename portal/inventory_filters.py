"""One Inventory search rule for the live stock table and its complete Excel export."""

from urllib.parse import urlencode

from django import forms
from django.urls import reverse


class InventorySearchForm(forms.Form):
    q = forms.CharField(label='Search inventory', required=False, max_length=100)


def filter_inventory_rows(rows, query):
    """Every search word must occur in the product name, strength or displayed category."""
    words = query.casefold().split()
    if not words:
        return list(rows)
    matched = []
    for row in rows:
        product = row['product']
        text = ' '.join((product.name, product.strength, str(product.get_category_display()))).casefold()
        if all(word in text for word in words):
            matched.append(row)
    return matched


def inventory_search_data(request):
    return request.POST if request.method == 'POST' else request.GET


def inventory_return_url(request):
    form = InventorySearchForm(inventory_search_data(request))
    query = form.cleaned_data['q'] if form.is_valid() else ''
    base = reverse('portal:inventory')
    return f'{base}?{urlencode({"q": query})}' if query else base
