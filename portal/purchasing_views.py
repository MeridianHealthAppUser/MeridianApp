"""Placing supplier orders and receiving their deliveries into the stock ledger."""
from urllib.parse import urlencode
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from care.models import MedicationProduct, PurchaseOrder, SupplierProduct
from care.purchasing import create_purchase_order, receive_purchase_order
from .export_support import export_url
from .filter_menus import list_tools, ticked_sections
from .operations_views import OperationsView
from .pagination import numbered_pagination
from .purchase_order_data import history_items, history_products, order_totals, purchase_orders, scoped_items
from .purchase_order_forms import PurchaseOrderCreateForm, order_lines, receipt_lines
from .purchasing_data import purchasing_rows
from .workflow_context import make_workflow_context, validate_workflow_context


def purchasing_url(view='to-order', **extra):
    query = ({'view': view} if view != 'to-order' else {}) | extra
    return reverse('portal:purchasing') + ('?' + urlencode(query, doseq=True) if query else '')


def selected_order(company, value, **filters):
    try:
        pk = int(value)
    except (ValueError, TypeError):
        raise Http404('Choose a purchase order from this practice.') from None
    return get_object_or_404(PurchaseOrder, company=company, pk=pk, **filters)


class PurchasingView(OperationsView):
    nav_section, page_title = 'purchasing', 'Purchasing'
    shipping = False
    http_method_names = ('get', 'head', 'options')

    def display(self, request, *, status=200, create_form=None, create_formset=None,
                receive_order=None, receive_formset=None, email_order=None, email_form=None):
        data = request.POST if request.method == 'POST' else request.GET
        view = data.get('view', 'to-order')
        if view not in ('to-order', 'on-order', 'history'): view = 'to-order'
        group = 'products' if view == 'history' and data.get('group') == 'products' else 'orders'
        context = self.context(purchasing_view=view, purchasing_group=group,
            purchasing_title={'to-order': 'To order', 'on-order': 'On order', 'history': 'Purchase history'}[view])
        names = ('q', 'category', 'status') if view == 'to-order' else ('q', 'category', 'eta')
        query = {name: data.getlist(name) for name in names if data.get(name)}
        if view != 'to-order': query['view'] = view
        if group == 'products': query['group'] = group
        if view == 'to-order':
            form, rows = purchasing_rows(self.company, data)
            page = Paginator(rows, 25).get_page(data.get('page'))
            for row in page.object_list:
                product = row['product']
                settings = {'q': str(product)[:100]}
                if 'Lead time' in row['missing_fields']: settings['lead-time'] = product.pk
                elif 'Safety stock' in row['missing_fields']: settings['safety-stock'] = product.pk
                row['settings_url'] = f'{reverse("portal:inventory")}?{urlencode(settings)}'
            sections, table, noun = ('category', 'status'), 'to-order', 'product'
            context.update(rows=page.object_list)
        elif group == 'products':
            form, rows = history_products(self.company, data)
            page = Paginator(rows, 25).get_page(data.get('page'))
            products = list(page.object_list)
            ids = [row['product_id'] for row in products]
            _, details = history_items(self.company, data)
            details = details.filter(product_id__in=ids)
            buckets = {}
            for item in details:
                remaining_query = {key: value for key, value in query.items() if key != 'view'}
                if data.get('page'): remaining_query['page'] = data.get('page')
                item.purchase_order.email_url = purchasing_url(view, **remaining_query, email=item.purchase_order_id)
                buckets.setdefault(item.product_id, []).append(item)
            categories = dict(MedicationProduct.Category.choices)
            for row in products:
                row['category_label'] = categories.get(row['product__category'], row['product__category'])
                row['order_items'] = buckets.get(row['product_id'], [])
            context.update(product_rows=products)
            sections, table, noun = ('category',), 'purchase-history-products', 'product'
        else:
            form, rows = purchase_orders(self.company, data, history=view == 'history')
            page = Paginator(rows, 25).get_page(data.get('page'))
            orders = [order_totals(order) for order in page.object_list]
            for order in orders:
                order.expanded = str(order.pk) == data.get('order')
                remaining_query = {key: value for key, value in query.items() if key != 'view'}
                if data.get('page'): remaining_query['page'] = data.get('page')
                order.receive_url = purchasing_url(view, **remaining_query, receive=order.pk)
                order.email_url = purchasing_url(view, **remaining_query, email=order.pk)
            context.update(orders=orders)
            sections = ('category',) if view == 'history' else ('category', 'eta')
            table, noun = ('purchase-history' if view == 'history' else 'on-order'), 'purchase order'
        return_query = dict(query)
        if data.get('page') and page.number > 1: return_query['page'] = page.number
        return_url = purchasing_url(**return_query)
        tools = list_tools(action=reverse('portal:purchasing'), data=data, form=form,
            label='Filter ' + context['purchasing_title'].lower(), sections=ticked_sections(form, data, sections,
                icons={'category': 'tag', 'status': 'status', 'eta': 'date'}),
            placeholder='Purchase order, product or category' if view != 'to-order' else 'Product, strength or category')
        extras = {'view': view} if view != 'to-order' else {}
        if group == 'products': extras['group'] = group
        tools['search_hidden'].update(extras)
        tools['filter_menu']['hidden'].extend(extras.items())
        if extras:
            tools['search_clear_url'] += ('&' if '?' in tools['search_clear_url'] else '?') + urlencode(extras)
            clear = tools['filter_menu']['clear_url']
            tools['filter_menu']['clear_url'] = clear + ('&' if '?' in clear else '?') + urlencode(extras)
        tools['table_export_url'] = export_url(table, request=type('ExportRequest', (), {'GET': data})())
        context.update(filter_form=form, page_obj=page,
            count_label=f'{page.paginator.count} {noun}' + ('' if page.paginator.count == 1 else 's'),
            **tools, **numbered_pagination(page, base_url=reverse('portal:purchasing'), query=query),
            purchasing_return_url=return_url, purchase_create_form=create_form if create_form is not None else PurchaseOrderCreateForm(company=self.company),
            history_orders_url=purchasing_url('history', **{key: value for key, value in query.items() if key not in ('view', 'group', 'eta')}),
            history_products_url=purchasing_url('history', group='products', **{key: value for key, value in query.items() if key not in ('view', 'group', 'eta')}),
            purchase_formset=create_formset if create_formset is not None else order_lines(self.company),
            purchase_create_open=create_formset is not None,
            purchase_create_context=(request.POST.get('workflow_context', '') if create_formset is not None else
                make_workflow_context(request, self.company, 'purchase-order-create')))
        supplier_products = {}
        for supplier_id, product_id in SupplierProduct.objects.filter(company=self.company,
                supplier__company=self.company, product__company=self.company).order_by('supplier_id', 'product_id').values_list('supplier_id', 'product_id'):
            supplier_products.setdefault(str(supplier_id), []).append(product_id)
        context['purchase_supplier_products'] = supplier_products
        if create_formset is not None:
            own_products = {str(product.pk): product for product in MedicationProduct.objects.filter(company=self.company, is_active=True)}
            suggestions = {str(row['product'].pk): row['order_quantity'] for row in rows} if view == 'to-order' else {}
            for line in create_formset:
                line.display_product = own_products.get(str(line['product'].value()))
                line.display_suggested = suggestions.get(str(line['product'].value()))
        if receive_order is None and data.get('receive'):
            receive_order = selected_order(self.company, data.get('receive'), status=PurchaseOrder.Status.OPEN)
        if receive_order is not None:
            items = list(scoped_items(self.company).filter(purchase_order=receive_order))
            receive_order.display_items = items
            order_totals(receive_order)
            receive_formset = receive_formset if receive_formset is not None else receipt_lines(self.company, receive_order, items=items)
            by_id = {str(item.pk): item for item in items}
            for line in receive_formset: line.display_item = by_id.get(str(line['item'].value()))
            context.update(receive_order=receive_order, receive_formset=receive_formset,
                purchase_receive_context=request.POST.get('workflow_context', '') if request.method == 'POST' else
                    make_workflow_context(request, self.company, 'purchase-order-receive', receive_order))
        if email_order is None and data.get('email'):
            email_order = selected_order(self.company, data.get('email'))
        if email_order is not None:
            from .purchase_document_views import PurchaseOrderEmailForm
            context.update(email_order=email_order, email_form=email_form if email_form is not None else
                PurchaseOrderEmailForm(order=email_order),
                purchase_email_context=request.POST.get('workflow_context', '') if request.method == 'POST' else
                    make_workflow_context(request, self.company, 'purchase-order-email', email_order))
        return render(request, 'portal/purchasing.html', context, status=status)

    def get(self, request):
        return self.display(request)


class PurchaseOrderCreateView(PurchasingView):
    http_method_names = ('post', 'options')

    def post(self, request):
        form, formset = PurchaseOrderCreateForm(request.POST, company=self.company), order_lines(self.company, request.POST)
        try:
            intent = validate_workflow_context(request, self.company, 'purchase-order-create')
            valid_form, valid_lines = form.is_valid(), formset.is_valid()
            if valid_form and valid_lines:
                order = create_purchase_order(company=self.company, actor=request.user, supplier=form.cleaned_data['supplier'], eta=form.cleaned_data['eta'],
                    items=[line.cleaned_data for line in formset], submission_key=intent['key'], request=request)
                messages.success(request, f'{order.reference} created.')
                return redirect(purchasing_url('on-order', order=order.pk))
        except ValidationError as error:
            form.add_error(None, error)
        return self.display(request, status=400, create_form=form, create_formset=formset)


class PurchaseOrderReceiveView(PurchasingView):
    http_method_names = ('post', 'options')

    def post(self, request, pk):
        order = get_object_or_404(PurchaseOrder, company=self.company, pk=pk)
        formset = receipt_lines(self.company, order, request.POST)
        try:
            intent = validate_workflow_context(request, self.company, 'purchase-order-receive', order, check_version=False)
            if formset.is_valid():
                movements = receive_purchase_order(company=self.company, actor=request.user, order=order,
                    items=[line.cleaned_data for line in formset if line.cleaned_data.get('quantity')],
                    submission_key=intent['key'], expected_updated=intent['updated'], request=request)
                order.refresh_from_db()
                messages.success(request, f'{order.reference}: {sum(item.quantity for item in movements)} units received into stock.')
                return redirect(purchasing_url('history' if order.status == PurchaseOrder.Status.RECEIVED else 'on-order', order=order.pk))
        except ValidationError as error:
            formset.non_form_errors()
            formset._non_form_errors.extend(error.messages)
        return self.display(request, status=400, receive_order=order, receive_formset=formset)
