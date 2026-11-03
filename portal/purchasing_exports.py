"""The complete filtered To order calculation as a typed Excel table."""
from django.core.exceptions import ValidationError

from care.operations import require_operations_actor
from .export_support import ExportTable
from .purchasing_data import purchasing_rows
from .purchase_order_data import history_items, purchase_orders


def purchasing_table(table, request, company, membership):
    if table not in ('to-order', 'on-order', 'purchase-history', 'purchase-history-products'):
        return None
    require_operations_actor(company, request.user)
    if table != 'to-order':
        if table == 'purchase-history-products':
            form, items = history_items(company, request.GET)
            lines = ((item.purchase_order, item) for item in items)
        else:
            form, orders = purchase_orders(company, request.GET, history=table == 'purchase-history')
            lines = ((order, item) for order in orders for item in order.display_items)
        if not form.is_valid():
            raise ValidationError('Correct the purchase order filters before exporting.')
        return ExportTable('On order' if table == 'on-order' else 'Purchase history',
            ('Purchase order', 'Supplier', 'Created', 'ETA', 'Received on', 'Product', 'Strength', 'Category', 'Supplier code',
             'Ordered', 'Received', 'Outstanding', 'Status'),
            ((order.reference, order.supplier_name or 'Unassigned', order.created_at, order.eta, order.received_at, item.product_name, item.strength,
              item.get_category_display(), item.supplier_code, item.quantity, item.quantity_received, item.quantity - item.quantity_received,
              order.get_status_display()) for order, item in lines),
            widths=(20, 30, 23, 18, 23, 38, 22, 25, 22, 16, 16, 16, 18))
    form, rows = purchasing_rows(company, request.GET)
    if not form.is_valid():
        raise ValidationError('Correct the To order filters before exporting.')
    return ExportTable('To order', ('Product', 'Strength', 'Category', 'AWC', 'AWC source', 'Lead time (weeks)',
        'Safety stock', 'On hand', 'On order', 'To order', 'Calculation'),
        ((row['product'].name, row['product'].strength, row['product'].get_category_display(), row['awc'],
          'Starting value' if row['awc_source'] == 'starting' else 'Dispatch history', row['lead_time_weeks'],
          row['safety_stock'], row['on_hand'], row['on_order'], row['order_quantity'],
          'Set ' + ' and '.join(row['missing_fields']) if row['missing_fields'] else
          'Order needed' if row['order_quantity'] else 'No order needed') for row in rows),
        widths=(38, 22, 25, 14, 24, 23, 18, 16, 16, 16, 40))
