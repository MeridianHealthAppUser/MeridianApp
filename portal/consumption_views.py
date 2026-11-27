"""Read-only Inventory consumption, with traceable weekly dispatch totals."""
from datetime import date, timedelta
from urllib.parse import urlencode

from django import forms
from django.core.paginator import Paginator
from django.db.models import Sum
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone

from care.consumption import consumption_graph_report, consumption_report, three_months_ago, weekly_items
from care.models import MedicationProduct
from care.services import record_audit
from .filter_menus import TickedChoiceField, list_tools, ticked_sections, ticked_values
from .operations_views import OperationsView
from .pagination import numbered_pagination


class ConsumptionFilterForm(forms.Form):
    q = forms.CharField(label='Search consumption', required=False, max_length=100)
    category = TickedChoiceField(label='Category', choices=MedicationProduct.Category.choices)
    weeks = forms.TypedChoiceField(label='Weeks to show', choices=((4, '4 weeks'), (8, '8 weeks'), (12, '12 weeks')),
                                  coerce=int, required=False, empty_value=8)
    end_week = forms.DateField(label='Week ending in this range', required=False,
                              widget=forms.DateInput(attrs={'type': 'date'}))
    view = forms.ChoiceField(choices=(('weekly', 'By week'), ('averages', 'Averages'), ('graph', 'Graph')), required=False)

    def clean_end_week(self):
        value = self.cleaned_data['end_week']
        if value:
            value -= timedelta(days=(value.weekday() - 1) % 7)
            today = timezone.localdate()
            if value > today - timedelta(days=(today.weekday() - 1) % 7):
                raise forms.ValidationError('Choose this week or an earlier week.')
            if value < date(1900, 4, 1):
                raise forms.ValidationError('Choose a week after March 1900.')
        return value


class ConsumptionGraphFilterForm(forms.Form):
    products = TickedChoiceField(label='Products')
    grouping = forms.ChoiceField(label='Group by', choices=(('weeks', 'Weeks'), ('months', 'Months')))
    date_from = forms.DateField(label='From', widget=forms.DateInput(attrs={'type': 'date'}))
    date_to = forms.DateField(label='To', widget=forms.DateInput(attrs={'type': 'date'}))

    def __init__(self, data, *, company):
        self.available_products = list(MedicationProduct.objects.filter(company=company).order_by('name', 'strength', 'pk'))
        data = data.copy()
        today = timezone.localdate()
        for name, value in (('grouping', 'weeks'), ('date_from', three_months_ago(today).isoformat()),
                            ('date_to', today.isoformat())):
            if name not in data:
                data[name] = value
        # An initial visit selects every product; submitting an empty selection
        # deliberately shows an empty graph rather than silently selecting all.
        if 'products' not in data and 'products_set' not in data:
            ids = [str(product.pk) for product in self.available_products]
            if hasattr(data, 'setlist'):
                data.setlist('products', ids)
            else:
                data['products'] = ids
        super().__init__(data)
        self.fields['products'].choices = [(str(product.pk), str(product)) for product in self.available_products]

    def clean(self):
        values = super().clean()
        start, end = values.get('date_from'), values.get('date_to')
        if start and start < date(1900, 1, 1):
            self.add_error('date_from', 'Choose a date from January 1900 or later.')
        if end and end > timezone.localdate():
            self.add_error('date_to', 'Choose today or an earlier date.')
        if start and end and start > end:
            self.add_error('date_to', 'The to date must be on or after the from date.')
        return values


GRAPH_QUERY_FIELDS = ('products', 'products_set', 'grouping', 'date_from', 'date_to')


def graph_query(data):
    """Keep graph controls when moving through the other consumption tabs."""
    return {name: ticked_values(data.getlist(name)) if name == 'products' else data[name]
            for name in GRAPH_QUERY_FIELDS if name in data}


def report_for(company, form):
    return consumption_report(company, end_week=form.cleaned_data['end_week'],
        weeks=form.cleaned_data['weeks'], query=form.cleaned_data['q'], categories=form.cleaned_data['category'])


def report_query(form, **overrides):
    data = {key: value for key, value in form.cleaned_data.items() if value not in ('', None, [])}
    if data.get('end_week'):
        data['end_week'] = data['end_week'].isoformat()
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


def url_with(name, query):
    return reverse(name) + ('?' + urlencode(query, doseq=True) if query else '')


class ConsumptionView(OperationsView):
    nav_section, page_title, shipping = 'consumption', 'Consumption', False
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        if request.GET.get('view') == 'graph':
            return self.graph(request)
        form = ConsumptionFilterForm(request.GET)
        valid = form.is_valid()
        report = report_for(self.company, form) if valid else None
        rows = report['rows'] if report else []
        page = Paginator(rows, 25).get_page(request.GET.get('page'))
        view = form.cleaned_data.get('view') or 'weekly'
        query = {**(report_query(form) if valid else {}), **graph_query(request.GET)}
        tools = list_tools(action=reverse('portal:inventory-consumption'), data=request.GET,
            sections=ticked_sections(form, request.GET, ('category',)), label='Filter consumption', form=form,
            placeholder='Product, strength or category')
        # The separate period picker and view tabs survive search/filter changes.
        for key in ('weeks', 'end_week', 'view', *GRAPH_QUERY_FIELDS):
            if query.get(key):
                values = query[key] if isinstance(query[key], list) else [query[key]]
                tools['search_hidden'][key] = ','.join(str(value) for value in values)
                tools['filter_menu']['hidden'].extend((key, str(value)) for value in values)
        tools['search_clear_url'] = url_with('portal:inventory-consumption', {**query, 'q': ''})
        tools['filter_menu']['clear_url'] = url_with('portal:inventory-consumption', {**query, 'category': []})
        tools.update(count_label=f'{len(rows)} product' + ('' if len(rows) == 1 else 's'),
                     table_export_url=url_with('portal:inventory-consumption-export',
                         {k: v for k, v in query.items() if k not in ('view', *GRAPH_QUERY_FIELDS)}), table_export_disabled=not valid)
        peak = max((cell['quantity'] for row in rows for cell in row['cells']), default=0)
        period_total = sum(row['total'] for row in rows)
        for row in page:
            row_peak = max((cell['quantity'] for cell in row['cells']), default=0)
            for cell in row['cells']:
                cell['level'] = min(4, max(1, (cell['quantity'] * 4 + peak - 1) // peak)) if cell['quantity'] and peak else 0
                cell['bar_height'] = max(5, round(cell['quantity'] * 100 / row_peak)) if row_peak and cell['quantity'] else 3
                cell['detail_url'] = reverse('portal:inventory-consumption-detail', kwargs={
                    'pk': row['product'].pk, 'week': cell['start'].isoformat()})
        context = self.context(inventory_view='consumption', filter_form=form, report=report, rows=page.object_list,
            page_obj=page, consumption_view=view, period_total=period_total,
            active_products=sum(row['total'] > 0 for row in rows), **tools,
            weekly_url=url_with('portal:inventory-consumption', {**query, 'view': 'weekly'}),
            averages_url=url_with('portal:inventory-consumption', {**query, 'view': 'averages'}),
            graph_url=url_with('portal:inventory-consumption', {**query, 'view': 'graph'}),
            period_hidden=[(key, value) for key, values in query.items() if key not in ('weeks', 'end_week')
                           for value in (values if isinstance(values, list) else [values])],
            **numbered_pagination(page, base_url=reverse('portal:inventory-consumption'), query=query))
        if report:
            context.update(previous_period=url_with('portal:inventory-consumption', {**query,
                'end_week': (report['end_week'] - timedelta(weeks=form.cleaned_data['weeks'])).isoformat()}),
                next_period=url_with('portal:inventory-consumption', {**query,
                'end_week': min(report['current_week_start'], report['end_week'] + timedelta(weeks=form.cleaned_data['weeks'])).isoformat()})
                    if report['end_week'] < report['current_week_start'] else None,
                latest_period=url_with('portal:inventory-consumption', {k: v for k, v in query.items() if k != 'end_week'}))
        return render(request, 'portal/inventory_consumption.html', context, status=200 if valid else 400)

    def graph(self, request):
        form = ConsumptionGraphFilterForm(request.GET, company=self.company)
        valid = form.is_valid()
        report = None
        if valid:
            report = consumption_graph_report(self.company, date_from=form.cleaned_data['date_from'],
                date_to=form.cleaned_data['date_to'], grouping=form.cleaned_data['grouping'],
                product_ids=form.cleaned_data['products'])
        data = {**report, 'date_from': report['date_from'].isoformat(), 'date_to': report['date_to'].isoformat()} if report else None
        # Keep the independent table settings while using the graph controls.
        table_form = ConsumptionFilterForm(request.GET)
        table_query = report_query(table_form) if table_form.is_valid() else {}
        table_query.pop('view', None)
        graph_values = graph_query(form.data)
        graph_values['products_set'] = '1'
        query = {**table_query, **graph_values}
        chosen = set(graph_values.get('products', []))
        options = [{'id': product.pk, 'label': str(product), 'selected': str(product.pk) in chosen}
                   for product in form.available_products]
        context = self.context(inventory_view='consumption', consumption_view='graph',
            filter_form=form, graph_form=form, graph_product_options=options,
            graph_report=report, graph_data=data, graph_as_of=timezone.localdate(),
            graph_table_rows=[{'label': period['label'], 'values': [series['values'][index] for series in report['series']]}
                              for index, period in enumerate(report['periods'])] if report else [],
            graph_hidden=[(key, value) for key, values in table_query.items()
                          for value in (values if isinstance(values, list) else [values])],
            weekly_url=url_with('portal:inventory-consumption', {**query, 'view': 'weekly'}),
            averages_url=url_with('portal:inventory-consumption', {**query, 'view': 'averages'}),
            graph_url=url_with('portal:inventory-consumption', {**query, 'view': 'graph'}))
        return render(request, 'portal/inventory_consumption.html', context, status=200 if valid else 400)


def detail_scope(company, pk, week):
    product = get_object_or_404(MedicationProduct, company=company, pk=pk)
    form = ConsumptionFilterForm({'end_week': week})
    if not form.is_valid() or not form.cleaned_data['end_week']:
        raise Http404('Choose a valid consumption week.')
    start = form.cleaned_data['end_week']
    return product, start, weekly_items(company, product, start)


class ConsumptionDetailView(OperationsView):
    nav_section, page_title, shipping = 'consumption', 'Consumption detail', False
    http_method_names = ('get', 'head', 'options')

    def get(self, request, pk, week):
        from care.consumption_export import consumption_dispatch_data
        product, start, items = detail_scope(self.company, pk, week)
        total = items.aggregate(total=Sum('quantity'))['total'] or 0
        page = Paginator(items.select_related('product', 'shipment__patient__assigned_doctor',
            'shipment__authorization__prescribed_by'), 25).get_page(request.GET.get('page'))
        page.object_list = list(page.object_list)
        for row in consumption_dispatch_data(self.company, page.object_list):
            item = row['item']
            item.patient_label, item.doctor_label = row['patient'], row['doctor'] or '-'
            item.waybill_label = row['waybill'] or row['tracking'] or '-'
        path = reverse('portal:inventory-consumption-detail', kwargs={'pk': product.pk, 'week': start.isoformat()})
        export = reverse('portal:inventory-consumption-detail-export', kwargs={'pk': product.pk, 'week': start.isoformat()})
        context = self.context(inventory_view='consumption', product=product, week_start=start,
            week_end=start + timedelta(days=6), total=total, items=page.object_list, page_obj=page,
            current_partial=start <= timezone.localdate() <= start + timedelta(days=6), detail_export_url=export,
            detail_back_url=url_with('portal:inventory-consumption', {'end_week': start.isoformat()}),
            **numbered_pagination(page, base_url=path))
        fragment = request.GET.get('fragment') == '1'
        response = render(request, 'portal/includes/consumption_detail.html' if fragment else 'portal/inventory_consumption_detail.html', context)
        response['Cache-Control'] = 'private, no-store'
        if fragment:
            response['X-Consumption-Fragment'] = '1'
        return response


class ConsumptionExportView(OperationsView):
    nav_section, page_title, shipping = 'consumption', 'Consumption', False
    http_method_names = ('get', 'head', 'options')

    def get(self, request, pk=None, week=None):
        from care.consumption_export import consumption_detail_workbook, consumption_workbook
        if pk is not None:
            product, start, items = detail_scope(self.company, pk, week)
            output = consumption_detail_workbook(self.company, product, start)
            filename = f'consumption-product-{product.pk}-{start.isoformat()}.xlsx'
        else:
            form = ConsumptionFilterForm(request.GET)
            if not form.is_valid() or form.cleaned_data.get('view') == 'graph':
                return HttpResponse('Correct the consumption filters before exporting.', status=400, content_type='text/plain')
            report = report_for(self.company, form)
            output = consumption_workbook(self.company, report)
            filename = f'consumption-{report["period_start"]}-{report["period_end"]}.xlsx'
        try:
            if request.method != 'HEAD':
                record_audit(company=self.company, actor=request.user, action='inventory.consumption_exported',
                    request=request, metadata={'product_id': pk, 'week': week, 'filename': filename})
            response = FileResponse(output, as_attachment=True, filename=filename,
                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            response['Cache-Control'] = 'private, no-store'
            response['X-Content-Type-Options'] = 'nosniff'
            return response
        except Exception:
            output.close()
            raise
