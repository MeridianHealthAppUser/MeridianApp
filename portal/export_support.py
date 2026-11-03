"""Shared table-export contract and links that preserve the current filters."""
from dataclasses import dataclass
from typing import Iterable

from django.http import QueryDict
from django.urls import reverse


@dataclass
class ExportTable:
    title: str
    headings: tuple
    rows: Iterable
    widths: tuple | None = None


def export_url(table, request=None, **params):
    query = request.GET.copy() if request is not None else QueryDict('', mutable=True)
    for name, value in params.items():
        if value is None:
            query.pop(name, None)
        else:
            query[name] = str(value)
    allowed = {
        'to-order': ('q', 'category', 'status'),
        'on-order': ('q', 'category', 'eta'), 'purchase-history': ('q', 'category'),
        'purchase-history-products': ('q', 'category'),
        'inventory': ('q',), 'inventory-reservations': ('pk',),
        'catalogue-products': ('q', 'status'), 'catalogue-doses': (), 'catalogue-plans': (),
        'subscriptions': ('q', 'status'), 'requests': ('q', 'status'), 'request-items': ('pk',),
        'billing': ('q', 'state'), 'billing-cycles': ('pk',), 'billing-payments': ('pk',),
        'shipping': ('week', 'q', 'status', 'product', 'doctor', 'payment', 'view', 'shipping_step'),
        'shipment-items': ('pk',), 'dispatch-history': ('q', 'start_month', 'end_month'),
        'stock-movements': (),
    }.get(table, ())
    for name in list(query):
        if name not in allowed:
            query.pop(name, None)
    if table == 'dispatch-history':
        url = reverse('portal:ops-history-export')
    elif table == 'stock-movements':
        url = reverse('portal:inventory-movements-export')
        query.clear()
    else:
        url = reverse('portal:ops-table-export', kwargs={'table': table})
    return f'{url}?{query.urlencode()}' if query else url
