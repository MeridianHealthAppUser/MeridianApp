"""Authorised XLSX endpoints for Operations and Inventory tables."""
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404, HttpResponse

from care.services import record_audit
from care.table_export import table_workbook
from .operations_views import OperationsView


class TableExportView(OperationsView):
    http_method_names = ('get', 'head', 'options')

    def dispatch(self, request, *args, **kwargs):
        self.shipping = kwargs.get('table') in ('shipping', 'shipment-items')
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, table):
        from .billing_exports import billing_table
        from .inventory_exports import inventory_table
        from .operations_exports import operations_table
        from .purchasing_exports import purchasing_table

        try:
            result = None
            for adapter in (inventory_table, billing_table, operations_table, purchasing_table):
                result = adapter(table, request, self.company, self.membership)
                if result is not None:
                    break
            if result is None:
                raise Http404('Unknown table export.')
            output, count = table_workbook(result.title, result.headings, result.rows, widths=result.widths)
        except ValidationError as error:
            return HttpResponse(' '.join(error.messages), status=400, content_type='text/plain')
        try:
            if request.method != 'HEAD':
                record_audit(company=self.company, actor=request.user, action='operations.table_exported',
                    request=request, metadata={'table': table, 'rows': count})
            return FileResponse(output, as_attachment=True, filename=f'{table}.xlsx',
                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        except Exception:
            output.close()
            raise
