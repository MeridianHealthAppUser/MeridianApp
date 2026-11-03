from datetime import timedelta
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.cache import patch_vary_headers

from care.inventory import effective_awc, receive_stock_batch, set_lead_time, set_safety_stock, set_starting_awc, stock_rows
from care.models import MedicationProduct, StockMovement
from .operations_views import OperationsView
from .inventory_filters import InventorySearchForm, filter_inventory_rows, inventory_return_url, inventory_search_data
from .workflow_context import make_workflow_context, validate_workflow_context


MOVEMENTS_PER_PAGE = 25


def stock_movements(company):
    return StockMovement.objects.filter(company=company).select_related(
        'product', 'actor', 'shipment',
    ).order_by('-created_at', '-pk')


class ReceiptItemForm(forms.Form):
    product = forms.ModelChoiceField(label='Medication', queryset=MedicationProduct.objects.none())
    quantity = forms.IntegerField(min_value=1)
    batch = forms.CharField(max_length=120)
    expires_on = forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}))

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['product'].queryset = MedicationProduct.objects.filter(company=company, is_active=True)


class ReceiptForm(forms.Form):
    reference = forms.CharField(label='Receipt / opening balance reference', max_length=500)


class SafetyStockForm(forms.Form):
    quantity = forms.IntegerField(label='Safety stock', min_value=0, max_value=2147483647,
        required=False, help_text='Minimum buffer to keep for this medication. Leave blank to clear it.')


class LeadTimeForm(forms.Form):
    weeks = forms.DecimalField(label='Lead time (weeks)', min_value=Decimal('0'), max_value=Decimal('9999.99'),
        max_digits=6, decimal_places=2, required=False, widget=forms.NumberInput(attrs={'step': '0.01'}),
        help_text='How many weeks supply usually takes to arrive. Leave blank to clear it.')


class StartingAWCForm(forms.Form):
    starting_awc = forms.DecimalField(label='Starting AWC', min_value=Decimal('0'),
        max_value=Decimal('99999999.99'), max_digits=10, decimal_places=2, required=False,
        widget=forms.NumberInput(attrs={'step': '0.01'}),
        help_text='Used for purchasing until cleared. Leave blank to use the calculated eight-week average.')


ReceiptItemFormSet = forms.formset_factory(
    ReceiptItemForm, extra=0, min_num=1, validate_min=True, max_num=100, validate_max=True,
    absolute_max=100, can_delete=True,
)


def receipt_items(company, data=None):
    return ReceiptItemFormSet(data, prefix='items', form_kwargs={'company': company})


class InventoryView(OperationsView):
    nav_section, page_title = 'inventory', 'Inventory'
    shipping = False

    def display(self, request, form=None, formset=None, status=200, *, safety_product=None, safety_form=None,
                lead_product=None, lead_form=None, awc_product=None, awc_form=None):
        from care.billing import parcel_payment
        from .export_support import export_url
        from .filter_menus import list_tools
        from .operations_views import shipment_doctor, shipping_week, this_week
        from care.operations import AVERAGE_WEEKS, week_stock
        week = shipping_week(self.company)
        can_edit_awc = self.membership.role == 'super_admin'
        can_edit_metrics = can_edit_awc
        modal_rows = stock_rows(self.company)
        search_data = inventory_search_data(request)
        search_form = InventorySearchForm(search_data)
        planning = {row['product'].pk: row for row in week_stock(
            self.company, week, consumption_week_start=this_week(), include_inactive=True)}
        receipt_open = safety_product is None and lead_product is None and awc_product is None and (
            request.method == 'POST' or request.GET.get('receive') == '1')
        selected_awc = (str(awc_product.pk) if awc_product else request.GET.get('awc')) if can_edit_awc else None
        product_ids = {str(row['product'].pk) for row in modal_rows}
        has_awc_dialog = not receipt_open and selected_awc in product_ids
        selected_safety = str(safety_product.pk) if safety_product else (None if lead_product or awc_product else request.GET.get('safety-stock'))
        selected_lead = str(lead_product.pk) if lead_product else (None if safety_product or awc_product else request.GET.get('lead-time'))
        if not can_edit_metrics:
            selected_safety = selected_lead = None
        has_lead_dialog = not receipt_open and not has_awc_dialog and selected_lead in product_ids
        reservation_details = {}
        for row in modal_rows:
            row['demand'] = planning.get(row['product'].pk, {})
            row['historical_awc'] = Decimal(str(row['demand'].get('average', 0)))
            row['awc'] = effective_awc(row['product'], row['historical_awc'])
            row['awc_source'] = 'starting' if row['product'].starting_awc is not None else 'history'
            if can_edit_awc:
                awc_selected = str(row['product'].pk) == selected_awc
                row['awc_open'] = awc_selected and has_awc_dialog
                row['awc_form'] = awc_form if awc_selected and awc_form is not None else StartingAWCForm(
                    initial={'starting_awc': row['product'].starting_awc})
                row['awc_context'] = (request.POST.get('workflow_context', '') if awc_selected and awc_form is not None
                    else make_workflow_context(request, self.company, 'inventory-starting-awc', row['product']))
            row['reservations_export_url'] = export_url('inventory-reservations', pk=row['product'].pk)
            lead_selected = str(row['product'].pk) == selected_lead
            row['lead_time_open'] = lead_selected and has_lead_dialog
            selected = str(row['product'].pk) == selected_safety
            row['safety_stock_open'] = selected and not receipt_open and not has_lead_dialog and not has_awc_dialog
            if can_edit_metrics:
                row['lead_time_form'] = lead_form if lead_selected and lead_form is not None else LeadTimeForm(
                    initial={'weeks': row['product'].lead_time_weeks})
                row['lead_time_context'] = (request.POST.get('workflow_context', '') if lead_selected and lead_form is not None
                    else make_workflow_context(request, self.company, 'inventory-lead-time', row['product']))
                row['safety_stock_form'] = safety_form if selected and safety_form is not None else SafetyStockForm(
                    initial={'quantity': row['product'].safety_stock})
                row['safety_stock_context'] = (request.POST.get('workflow_context', '') if selected and safety_form is not None
                    else make_workflow_context(request, self.company, 'inventory-safety-stock', row['product']))
            row['reserved_open'] = bool(row['reservations'] and not receipt_open and not has_lead_dialog and not has_awc_dialog and
                                        not row['safety_stock_open'] and
                                        request.GET.get('reserved') == str(row['product'].pk))
            for item in row['reservations']:
                if item.shipment_id not in reservation_details:
                    reservation_details[item.shipment_id] = (shipment_doctor(item.shipment), *parcel_payment(item.shipment))
                item.reservation_doctor, item.reservation_payment_state, item.reservation_payment_text = reservation_details[item.shipment_id]
        rows = filter_inventory_rows(modal_rows, search_form.cleaned_data['q']) if search_form.is_valid() else []
        tools = list_tools(action=reverse('portal:inventory'), data=search_data, sections=[], form=search_form,
                           label='Search inventory', placeholder='Product, strength or category')
        tools.update(filter_menu=None, count_label=f'{len(rows)} product' + ('' if len(rows) == 1 else 's'))
        return_url = inventory_return_url(request)
        return render(request, 'portal/inventory.html', self.context(rows=rows, modal_rows=modal_rows,
            filter_form=search_form, **tools,
            inventory_view='inventory',
            can_edit_awc=can_edit_awc,
            can_edit_metrics=can_edit_metrics,
            inventory_export_url=tools['table_export_url'],
            inventory_return_url=return_url, receive_url=return_url + ('&' if '?' in return_url else '?') + 'receive=1',
            receipt_form=form if form is not None else ReceiptForm(),
            formset=formset if formset is not None else receipt_items(self.company),
            receipt_open=receipt_open,
            workflow_context=make_workflow_context(request, self.company, 'inventory-receipt'),
            average_weeks=AVERAGE_WEEKS,
            planning_week=week, planning_week_end=week + timedelta(days=6),
            next_week_start=week + timedelta(days=7), next_week_end=week + timedelta(days=13)), status=status)

    def get(self, request):
        return self.display(request)

    def post(self, request):
        form = ReceiptForm(request.POST)
        data = request.POST
        # Previously rendered single-item forms can still be submitted during a rolling deployment.
        if 'items-TOTAL_FORMS' not in data and 'product' in data:
            data = data.copy()
            data.update({'items-TOTAL_FORMS': '1', 'items-INITIAL_FORMS': '0'})
            for field in ('product', 'quantity', 'batch', 'expires_on'):
                data[f'items-0-{field}'] = request.POST.get(field, '')
        formset = receipt_items(self.company, data)
        try:
            intent = validate_workflow_context(request, self.company, 'inventory-receipt')
            valid_reference, valid_items = form.is_valid(), formset.is_valid()
            if valid_reference and valid_items:
                items = [{key: line.cleaned_data[key] for key in ('product', 'quantity', 'batch', 'expires_on')}
                         for line in formset if line.cleaned_data and not line.cleaned_data.get('DELETE')]
                movements = receive_stock_batch(company=self.company, actor=request.user, request=request,
                    submission_key=intent['key'], reference=form.cleaned_data['reference'], items=items)
                messages.success(request, f'Stock received: {len(movements)} medication row{"s" if len(movements) != 1 else ""}.')
                return redirect(inventory_return_url(request))
        except ValidationError as error:
            form.add_error(None, error)
        return self.display(request, form, formset, status=400)


class InventorySafetyStockView(InventoryView):
    http_method_names = ('post', 'options')

    @transaction.atomic
    def post(self, request, pk):
        from care.operations import require_operations_actor
        from practices.models import Company

        self.company = Company.objects.select_for_update().get(pk=self.company.pk)
        require_operations_actor(self.company, request.user, catalogue=True)
        product = get_object_or_404(MedicationProduct.objects.select_for_update(), company=self.company, pk=pk)
        form = SafetyStockForm(request.POST)
        try:
            validate_workflow_context(request, self.company, 'inventory-safety-stock', product)
            if form.is_valid():
                set_safety_stock(company=self.company, actor=request.user, product=product,
                    quantity=form.cleaned_data['quantity'], request=request)
                messages.success(request, f'Safety stock updated for {product}.')
                return redirect(inventory_return_url(request))
        except ValidationError as error:
            form.add_error(None, error)
        return self.display(request, status=400, safety_product=product, safety_form=form)


class InventoryLeadTimeView(InventoryView):
    http_method_names = ('post', 'options')

    @transaction.atomic
    def post(self, request, pk):
        from care.operations import require_operations_actor
        from practices.models import Company

        self.company = Company.objects.select_for_update().get(pk=self.company.pk)
        require_operations_actor(self.company, request.user, catalogue=True)
        product = get_object_or_404(MedicationProduct.objects.select_for_update(), company=self.company, pk=pk)
        form = LeadTimeForm(request.POST)
        try:
            validate_workflow_context(request, self.company, 'inventory-lead-time', product)
            if form.is_valid():
                set_lead_time(company=self.company, actor=request.user, product=product,
                              weeks=form.cleaned_data['weeks'], request=request)
                messages.success(request, f'Lead time updated for {product}.')
                return redirect(inventory_return_url(request))
        except ValidationError as error:
            form.add_error(None, error)
        return self.display(request, status=400, lead_product=product, lead_form=form)


class InventoryStartingAWCView(InventoryView):
    http_method_names = ('post', 'options')

    @transaction.atomic
    def post(self, request, pk):
        from care.operations import require_operations_actor
        from practices.models import Company

        self.company = Company.objects.select_for_update().get(pk=self.company.pk)
        require_operations_actor(self.company, request.user, catalogue=True)
        product = get_object_or_404(MedicationProduct.objects.select_for_update(), company=self.company, pk=pk)
        form = StartingAWCForm(request.POST)
        try:
            validate_workflow_context(request, self.company, 'inventory-starting-awc', product)
            if form.is_valid():
                set_starting_awc(company=self.company, actor=request.user, product=product,
                                value=form.cleaned_data['starting_awc'], request=request)
                messages.success(request, f'Starting AWC updated for {product}.')
                return redirect(inventory_return_url(request))
        except ValidationError as error:
            form.add_error(None, error)
        return self.display(request, status=400, awc_product=product, awc_form=form)


class InventoryMovementsView(OperationsView):
    nav_section, page_title = 'inventory', 'Stock movements'
    shipping = False
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        from .pagination import numbered_pagination

        page = Paginator(stock_movements(self.company), MOVEMENTS_PER_PAGE).get_page(request.GET.get('page'))
        fragment = request.headers.get('X-Inventory-Movements') == '1'
        response = render(request,
            'portal/includes/inventory_movements.html' if fragment else 'portal/inventory_movements.html',
            self.context(page_obj=page, movements=page.object_list, history_fragment=fragment,
                export_url=reverse('portal:inventory-movements-export'),
                **numbered_pagination(page, base_url=reverse('portal:inventory-movements'))))
        patch_vary_headers(response, ('X-Inventory-Movements',))
        return response


class InventoryMovementsExportView(OperationsView):
    nav_section, page_title = 'inventory', 'Stock movements'
    shipping = False
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        from care.stock_export import stock_history_workbook

        return FileResponse(stock_history_workbook(stock_movements(self.company)), as_attachment=True,
            filename='stock-movements.xlsx',
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
