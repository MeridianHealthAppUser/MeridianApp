"""Separate practice operations and patient pharmacy pages."""

import csv
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from care.models import MedicationBatch, MedicationProduct, PharmacyOrder, Shipment, StockMovement, TreatmentAuthorization
from care.operations import (
    UNDISPATCHED, change_batch, dispatch_shipment, hold_or_cancel_shipment, lock_shipping_week,
    mark_delivered, prepare_shipment, product_allowance, receive_stock, require_operations_actor, shipment_hold_reason,
)
from care.pharmacy import accept_order, cancel_order, set_basket_quantity, submit_basket
from care.services import record_audit
from care.shipment_intake import create_manual_shipment
from practices.models import Company, CompanyMembership

from .clinical_forms import ClinicalFilterForm
from .operations_forms import (
    BasketQuantityForm, BatchActionForm, BatchReceiptForm, CatalogueFilterForm, ConfirmForm, HistoryFilterForm,
    OrderReviewForm, ProductForm, ShipmentActionForm, ShipmentCreateForm, ShippingFilterForm, StockFilterForm, SupplyRequestForm,
)
from .patient_views import patient_page_context
from .views import PatientPortalRequiredMixin, StaffCompanyRequiredMixin
from .workflow_context import make_workflow_context, validate_workflow_context


def _paginate(request, queryset):
    page = Paginator(queryset, 20).get_page(request.GET.get('page'))
    filters = request.GET.copy()
    filters.pop('page', None)
    return dict(page_obj=page, is_paginated=page.has_other_pages(), pagination_query=filters.urlencode())


def _form_data(request, **defaults):
    data = request.GET.copy()
    for key, value in defaults.items():
        data.setdefault(key, value)
    return data


def _errors(request, form, error):
    for message in error.messages:
        form.add_error(None, message)
    # Read-only terminal states still show a rejected stale submission clearly.
    messages.error(request, ' '.join(error.messages))


@method_decorator(never_cache, name='dispatch')
class OperationsView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    nav_section = ''
    page_title = ''
    http_method_names = ('get', 'post', 'head', 'options')

    @property
    def can_edit(self):
        return self.membership.role in (CompanyMembership.Role.PRACTICE_ADMIN, CompanyMembership.Role.SUPER_ADMIN)

    def context(self, **kwargs):
        return dict(company=self.company, active_membership=self.membership, nav_section=self.nav_section,
                    page_title=self.page_title, can_edit=self.can_edit, **kwargs)

    def token(self, kind, record=None):
        if self.request.method == 'POST':
            return self.request.POST.get('workflow_context', '')
        return make_workflow_context(self.request, self.company, kind, record)

    def shipments(self):
        return Shipment.objects.for_company(self.company).filter(patient__company=self.company).select_related(
            'patient', 'company', 'subscription',
        ).prefetch_related('items__product', 'items__batch')

    def orders(self):
        return PharmacyOrder.objects.for_company(self.company).filter(patient__company=self.company).exclude(status='draft').select_related('patient', 'shipment')


class CatalogueView(OperationsView):
    nav_section, page_title = 'catalogue', 'Pharmacy catalogue'
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        form = CatalogueFilterForm(_form_data(request, status='all'))
        queryset = MedicationProduct.objects.for_company(self.company).order_by('name', 'strength', 'pk')
        if form.is_valid():
            if form.cleaned_data['q']:
                queryset = queryset.filter(Q(name__icontains=form.cleaned_data['q']) | Q(strength__icontains=form.cleaned_data['q']))
            if form.cleaned_data['status'] != 'all':
                queryset = queryset.filter(is_active=form.cleaned_data['status'] == 'active')
        else:
            queryset = queryset.none()
        context = self.context(filter_form=form, **_paginate(request, queryset))
        context['can_edit'] = self.membership.role == CompanyMembership.Role.SUPER_ADMIN
        context['products'] = list(context['page_obj'].object_list)
        return render(request, 'portal/operations_catalogue.html', context)


class ProductEditorView(OperationsView):
    nav_section, page_title = 'catalogue', 'Catalogue item'

    def display(self, request, product, form, status=200):
        context = self.context(product=product, form=form, workflow_context=self.token('catalogue-product', product))
        context['can_edit'] = self.membership.role == CompanyMembership.Role.SUPER_ADMIN
        return render(request, 'portal/operations_product_form.html', context, status=status)

    def get(self, request, pk=None):
        product = get_object_or_404(MedicationProduct.objects.for_company(self.company), pk=pk) if pk else None
        if not product:
            require_operations_actor(self.company, request.user, catalogue=True)
        return self.display(request, product, ProductForm(company=self.company, instance=product))

    @transaction.atomic
    def post(self, request, pk=None):
        self.company = Company.objects.select_for_update().get(pk=self.company.pk)
        require_operations_actor(self.company, request.user, catalogue=True)
        product = get_object_or_404(MedicationProduct.objects.for_company(self.company), pk=pk) if pk else None
        form = ProductForm(request.POST, company=self.company, instance=product)
        try:
            # Check the version before ModelForm mutates its in-memory instance.
            validate_workflow_context(request, self.company, 'catalogue-product', product)
            if form.is_valid():
                saved = form.save()
                record_audit(company=self.company, actor=request.user, action='catalogue.updated' if pk else 'catalogue.created',
                             target=saved, request=request, metadata={'changed_fields': form.changed_data})
                messages.success(request, 'Catalogue item saved. Historical order prices are unchanged.')
                return redirect('portal:ops-product-detail', pk=saved.pk)
        except ValidationError as error:
            if not form.is_bound or not hasattr(form, 'cleaned_data'):
                form.is_valid()
            _errors(request, form, error)
        return self.display(request, product, form, status=400)


class StockListView(OperationsView):
    nav_section, page_title = 'stock', 'Batches and stock'
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        form = StockFilterForm(_form_data(request, status='all', expiry='all'))
        all_batches = MedicationBatch.objects.for_company(self.company)
        today = timezone.localdate()
        queryset = all_batches.select_related('product').order_by('expires_on', 'pk')
        if form.is_valid():
            if form.cleaned_data['status'] != 'all':
                queryset = queryset.filter(status=form.cleaned_data['status'])
            if form.cleaned_data['expiry'] == 'soon':
                queryset = queryset.filter(expires_on__range=(today, today + timedelta(days=60)))
            elif form.cleaned_data['expiry'] == 'expired':
                queryset = queryset.filter(expires_on__lt=today)
        else:
            queryset = queryset.none()
        context = self.context(filter_form=form, **_paginate(request, queryset), metrics={
            'on_hand': all_batches.aggregate(total=Sum('quantity_on_hand'))['total'] or 0,
            'near_expiry': all_batches.filter(expires_on__range=(today, today + timedelta(days=60))).aggregate(total=Sum('quantity_on_hand'))['total'] or 0,
            'quarantined': all_batches.filter(status='quarantined').aggregate(total=Sum('quantity_on_hand'))['total'] or 0,
        })
        context['batches'] = list(context['page_obj'].object_list)
        for batch in context['batches']:
            batch.expiring = batch.expires_on <= today + timedelta(days=60)
        return render(request, 'portal/operations_stock.html', context)


class StockReceiveView(OperationsView):
    nav_section, page_title = 'stock', 'Receive stock'

    def display(self, request, form, status=200):
        return render(request, 'portal/operations_batch_form.html', self.context(form=form,
                      workflow_context=self.token('stock-receipt')), status=status)

    def get(self, request):
        require_operations_actor(self.company, request.user)
        return self.display(request, BatchReceiptForm(company=self.company))

    def post(self, request):
        require_operations_actor(self.company, request.user)
        form = BatchReceiptForm(request.POST, company=self.company)
        valid = form.is_valid()
        try:
            intent = validate_workflow_context(request, self.company, 'stock-receipt')
            if valid:
                batch = receive_stock(company=self.company, actor=request.user, submission_key=intent['key'], request=request, **form.cleaned_data)
                messages.success(request, 'Stock receipt recorded.')
                return redirect('portal:ops-batch-detail', pk=batch.pk)
        except ValidationError as error:
            _errors(request, form, error)
        return self.display(request, form, status=400)


class BatchDetailView(OperationsView):
    nav_section, page_title = 'stock', 'Stock batch'

    def batch(self, pk):
        return get_object_or_404(MedicationBatch.objects.for_company(self.company).select_related('product'), pk=pk)

    def display(self, request, batch, form=None, status=200):
        movements = StockMovement.objects.for_company(self.company).filter(batch=batch).select_related('recorded_by', 'shipment').order_by('-created_at', '-pk')
        context = self.context(batch=batch, form=form if form is not None else BatchActionForm(),
                               workflow_context=self.token('stock-batch', batch), **_paginate(request, movements))
        context['movements'] = list(context['page_obj'].object_list)
        if batch.status == MedicationBatch.Status.WRITTEN_OFF:
            context['can_edit'] = False
        return render(request, 'portal/operations_batch_detail.html', context, status=status)

    def get(self, request, pk):
        return self.display(request, self.batch(pk))

    @transaction.atomic
    def post(self, request, pk):
        self.company = Company.objects.select_for_update().get(pk=self.company.pk)
        require_operations_actor(self.company, request.user)
        batch, form = self.batch(pk), BatchActionForm(request.POST)
        valid = form.is_valid()
        try:
            validate_workflow_context(request, self.company, 'stock-batch', batch)
            if valid:
                data = dict(form.cleaned_data)
                data.pop('confirm')
                change_batch(batch=batch, actor=request.user, request=request, **data)
                messages.success(request, 'Stock action recorded. Held shipments are not automatically dispatched.')
                return redirect('portal:ops-batch-detail', pk=pk)
        except ValidationError as error:
            _errors(request, form, error)
        return self.display(request, batch, form, status=400)


def _shipping_filter(request, queryset):
    today = timezone.localdate()
    form = ShippingFilterForm(_form_data(request, week=(today - timedelta(days=today.weekday())).isoformat(), status='all'))
    if form.is_valid():
        week = form.cleaned_data['week']
        queryset = queryset.filter(scheduled_for__range=(week, week + timedelta(days=6)))
        if form.cleaned_data['status'] != 'all':
            queryset = queryset.filter(status=form.cleaned_data['status'])
    else:
        queryset = queryset.none()
    return form, queryset


class ShippingListView(OperationsView):
    nav_section, page_title = 'shipping', 'Weekly shipping list'
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        form, queryset = _shipping_filter(request, self.shipments())
        queryset = queryset.order_by('scheduled_for', 'patient__last_name', 'pk')
        context = self.context(filter_form=form, lock_context=self.token('shipping-week'), **_paginate(request, queryset), metrics={
            'ready': queryset.filter(status=Shipment.Status.READY).count(), 'held': queryset.filter(status=Shipment.Status.HELD).count(),
            'units': queryset.aggregate(total=Sum('items__quantity'))['total'] or 0,
        })
        context['shipments'] = list(context['page_obj'].object_list)
        for shipment in context['shipments']:
            shipment.current_hold_reason = shipment_hold_reason(shipment) if shipment.status in UNDISPATCHED else ''
        return render(request, 'portal/operations_shipping.html', context)


class ShipmentCreateView(OperationsView):
    nav_section, page_title = 'shipping', 'Create shipment'

    def display(self, request, form, status=200):
        return render(request, 'portal/operations_shipment_form.html', self.context(form=form,
                      workflow_context=self.token('shipment-create')), status=status)

    def get(self, request):
        require_operations_actor(self.company, request.user)
        return self.display(request, ShipmentCreateForm(company=self.company))

    def post(self, request):
        require_operations_actor(self.company, request.user)
        form = ShipmentCreateForm(request.POST, company=self.company)
        valid = form.is_valid()
        try:
            intent = validate_workflow_context(request, self.company, 'shipment-create')
            if valid:
                data = dict(form.cleaned_data)
                data.pop('confirm')
                shipment = create_manual_shipment(company=self.company, actor=request.user, submission_key=intent['key'], request=request, **data)
                messages.success(request, 'Draft shipment created. Stock has not yet been allocated.')
                return redirect('portal:ops-shipment-detail', pk=shipment.pk)
        except ValidationError as error:
            _errors(request, form, error)
        return self.display(request, form, status=400)


class ShipmentDetailView(OperationsView):
    nav_section, page_title = 'shipping', 'Shipment'

    def display(self, request, shipment, form=None, status=200):
        from .patient_action_context import patient_action_context

        context = self.context(shipment=shipment, items=list(shipment.items.select_related('product', 'batch')),
                               form=form if form is not None else ShipmentActionForm(),
                               workflow_context=self.token('shipment', shipment),
                               current_hold_reason=shipment_hold_reason(shipment) if shipment.status in UNDISPATCHED else '')
        if shipment.status in (Shipment.Status.CANCELLED, Shipment.Status.DELIVERED):
            context['can_edit'] = False
        context.update(patient_action_context(self, shipment.patient, 'deliveries',
            content_template='portal/includes/shipment_detail_content.html', stylesheets=('css/operations.css',),
            title=f'Shipment {shipment.pk}'))
        return render(request, 'portal/patient_workspace_action.html' if context.get('patient_workspace') else 'portal/operations_shipment_detail.html', context, status=status)

    def get(self, request, pk):
        return self.display(request, get_object_or_404(self.shipments(), pk=pk))

    @transaction.atomic
    def post(self, request, pk):
        self.company = Company.objects.select_for_update().get(pk=self.company.pk)
        require_operations_actor(self.company, request.user)
        shipment = get_object_or_404(self.shipments(), pk=pk)
        form = ShipmentActionForm(request.POST)
        valid = form.is_valid()
        try:
            validate_workflow_context(request, self.company, 'shipment', shipment)
            if valid:
                action = form.cleaned_data['action']
                if action == 'prepare':
                    prepare_shipment(shipment=shipment, actor=request.user, request=request)
                elif action in ('hold', 'cancel'):
                    hold_or_cancel_shipment(shipment=shipment, actor=request.user, cancel=action == 'cancel', reason=form.cleaned_data['reason'], request=request)
                elif action == 'dispatch':
                    dispatch_shipment(shipment=shipment, actor=request.user, tracking_number=form.cleaned_data['tracking_number'], confirm=True, request=request)
                elif action == 'deliver':
                    mark_delivered(shipment=shipment, actor=request.user, confirm=True, request=request)
                messages.success(request, 'Shipment action recorded. No courier API was called.')
                from .patient_action_context import workspace_redirect
                return workspace_redirect(request, 'portal:ops-shipment-detail', pk=pk)
        except ValidationError as error:
            _errors(request, form, error)
        return self.display(request, shipment, form, status=400)


class ShippingLockView(OperationsView):
    http_method_names = ('post',)

    def post(self, request):
        require_operations_actor(self.company, request.user)
        form = ShippingFilterForm({'week': request.POST.get('week'), 'status': 'all'})
        try:
            validate_workflow_context(request, self.company, 'shipping-week')
            if not form.is_valid():
                raise ValidationError('Choose a valid shipping week.')
            shipments = lock_shipping_week(company=self.company, actor=request.user, week_start=form.cleaned_data['week'], request=request)
            messages.success(request, f'{len(shipments)} ready shipments checked and locked. Nothing was dispatched.')
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
        return redirect(f'{reverse("portal:ops-shipping")}?{urlencode({"week": request.POST.get("week", "")})}')


def _history_filter(request, queryset):
    form = HistoryFilterForm(request.GET)
    if form.is_valid():
        if form.cleaned_data.get('start'):
            queryset = queryset.filter(dispatched_at__date__gte=form.cleaned_data['start'])
        if form.cleaned_data.get('end'):
            queryset = queryset.filter(dispatched_at__date__lte=form.cleaned_data['end'])
    else:
        queryset = queryset.none()
    return form, queryset


class DispatchHistoryView(OperationsView):
    nav_section, page_title = 'history', 'Dispatch history'
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        form, queryset = _history_filter(request, self.shipments().filter(status__in=('dispatched', 'delivered')).order_by('-dispatched_at', '-pk'))
        context = self.context(filter_form=form, **_paginate(request, queryset))
        context['shipments'] = list(context['page_obj'].object_list)
        return render(request, 'portal/operations_history.html', context)


def _csv_response(filename, headings, rows):
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response['Cache-Control'] = 'private, no-store, max-age=0'
    response['X-Content-Type-Options'] = 'nosniff'
    writer = csv.writer(response)
    writer.writerow(headings)
    for row in rows:
        values = []
        for value in row:
            value = '' if value is None else str(value)
            starts_with_formula = value.lstrip(' \t\r\n\v\f\ufeff').startswith(('=', '+', '-', '@'))
            starts_with_control = value.startswith(('\t', '\r', '\n', '\v', '\f', '\ufeff'))
            values.append("'" + value if starts_with_formula or starts_with_control else value)
        writer.writerow(values)
    return response


class ManifestDownloadView(OperationsView):
    http_method_names = ('get', 'head', 'options')
    history = False

    def get(self, request):
        queryset = self.shipments()
        if self.history:
            form, queryset = _history_filter(request, queryset.filter(status__in=('dispatched', 'delivered')))
        else:
            form, queryset = _shipping_filter(request, queryset)
        if not form.is_valid():
            return HttpResponse('Correct the export date filters.', status=400)
        if queryset.count() > 10000:
            return HttpResponse('Narrow the export dates to at most 10,000 shipments.', status=400)
        rows = []
        for shipment in queryset.order_by('scheduled_for', 'pk'):
            snapshot = shipment.dispatch_snapshot or {}
            patient_name = snapshot.get('patient_name', str(shipment.patient))
            id_number = snapshot.get('patient_id_number', shipment.patient.id_number)
            items = snapshot.get('items') or [dict(product=str(item.product), dose=item.dose, quantity=item.quantity,
                                                       batch=item.batch.batch_number if item.batch else '',
                                                       expires_on=item.batch.expires_on if item.batch else '')
                                               for item in shipment.items.all()]
            for item in items:
                rows.append((shipment.pk, shipment.scheduled_for, snapshot.get('company_name', self.company.name), patient_name,
                             id_number, item['product'], item['dose'], item['quantity'], item['batch'], item['expires_on'],
                             shipment.get_status_display(), shipment.tracking_number, shipment.hold_reason))
        if request.method != 'HEAD':
            record_audit(company=self.company, actor=request.user, action='dispatch.history_exported' if self.history else 'shipping.manifest_exported',
                         request=request, metadata={'rows': len(rows)})
        return _csv_response('dispatch-history.csv' if self.history else 'shipping-manifest.csv',
                             ('Shipment', 'Planned date', 'Practice', 'Patient', 'ID / passport', 'Product', 'Dose', 'Quantity', 'Batch', 'Expiry', 'Status', 'Tracking', 'Hold reason'), rows)


class StockExportView(OperationsView):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        batches = MedicationBatch.objects.for_company(self.company).select_related('product').order_by('batch_number')
        if batches.count() > 10000:
            return HttpResponse('This export exceeds the 10,000-batch limit.', status=400)
        rows = [(batch.batch_number, str(batch.product), batch.received_on, batch.expires_on, batch.quantity_received,
                 batch.quantity_on_hand, batch.cold_chain_confirmed, batch.get_status_display()) for batch in batches]
        if request.method != 'HEAD':
            record_audit(company=self.company, actor=request.user, action='stock.exported', request=request, metadata={'rows': len(rows)})
        return _csv_response('stock-and-cold-chain.csv', ('Batch', 'Product', 'Received', 'Expiry', 'Units received', 'On hand', 'Cold chain confirmed', 'Status'), rows)


class StaffOrderListView(OperationsView):
    nav_section, page_title = 'orders', 'Supply requests'
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        form = ClinicalFilterForm(_form_data(request, status='all'), company=self.company, statuses=PharmacyOrder.Status.choices)
        queryset = self.orders()
        if form.is_valid():
            if form.cleaned_data['status'] != 'all':
                queryset = queryset.filter(status=form.cleaned_data['status'])
            if form.cleaned_data.get('patient'):
                queryset = queryset.filter(patient=form.cleaned_data['patient'])
        else:
            queryset = queryset.none()
        context = self.context(filter_form=form, **_paginate(request, queryset))
        context['orders'] = list(context['page_obj'].object_list)
        return render(request, 'portal/operations_orders.html', context)


def _order_items(order):
    items = list(order.items.select_related('product'))
    for item in items:
        item.line_total = item.quantity * item.unit_price
    return items


class StaffOrderDetailView(OperationsView):
    nav_section, page_title = 'orders', 'Supply request'

    def display(self, request, order, form=None, status=200):
        context = self.context(order=order, items=_order_items(order), form=form if form is not None else OrderReviewForm(),
                               workflow_context=self.token('supply-request', order))
        context['can_edit'] = self.can_edit and order.status == 'submitted'
        return render(request, 'portal/operations_order_detail.html', context, status=status)

    def get(self, request, pk):
        return self.display(request, get_object_or_404(self.orders(), pk=pk))

    @transaction.atomic
    def post(self, request, pk):
        self.company = Company.objects.select_for_update().get(pk=self.company.pk)
        require_operations_actor(self.company, request.user)
        order, form = get_object_or_404(self.orders(), pk=pk), OrderReviewForm(request.POST)
        valid = form.is_valid()
        try:
            validate_workflow_context(request, self.company, 'supply-request', order)
            if valid:
                if form.cleaned_data['action'] == 'cancel':
                    cancel_order(order=order, actor=request.user, confirm=True, request=request)
                else:
                    if not form.cleaned_data.get('scheduled_for'):
                        raise ValidationError('Choose a planned dispatch date.')
                    shipment = accept_order(order=order, actor=request.user, scheduled_for=form.cleaned_data['scheduled_for'], confirm=True, request=request)
                    messages.success(request, 'Request accepted. Prepare the draft shipment separately; no payment or dispatch occurred.')
                    return redirect('portal:ops-shipment-detail', pk=shipment.pk)
                messages.success(request, 'Unaccepted supply request cancelled.')
                return redirect('portal:ops-order-detail', pk=pk)
        except ValidationError as error:
            _errors(request, form, error)
        return self.display(request, order, form, status=400)


class PatientPharmacyBase(LoginRequiredMixin, PatientPortalRequiredMixin, View):
    http_method_names = ('get', 'post', 'head', 'options')

    def context(self, **kwargs):
        return dict(patient_page_context(self.request, self.patient_company, self.patient, 'pharmacy', 'My Medications'), **kwargs)

    def orders(self):
        return PharmacyOrder.objects.for_company(self.patient_company).filter(patient=self.patient).select_related('shipment')

    def token(self, kind, record):
        return make_workflow_context(self.request, self.patient_company, kind, record, patient=self.patient)


class PatientPharmacyView(PatientPharmacyBase):
    http_method_names = ('get', 'head', 'options')

    def display(self, request, failed_product=None, failed_form=None, status=200):
        from .patient_summary import current_plan

        company, patient = self.patient_company, self.patient
        products = list(MedicationProduct.objects.for_company(company).filter(is_active=True).order_by('name', 'strength', 'pk'))
        basket = self.orders().filter(status='draft').first()
        existing = {item.product_id: item.quantity for item in basket.items.all()} if basket else {}
        plan = current_plan(company, patient)
        # The latest authorisation for each product, to show what was prescribed before.
        previous = {}
        for authorization in TreatmentAuthorization.objects.for_company(company).filter(
            patient=patient, product__in=[product for product in products if product.requires_authorisation],
        ).select_related('prescribed_by').order_by('expires_on', 'pk'):
            previous[authorization.product_id] = authorization
        groups = {'authorised': [], 'open': [], 'previous': []}
        for product in products:
            product.authorization, product.remaining, product.locked_reason = product_allowance(patient, product)
            product.workflow_context = self.token('basket-product', product)
            if request.method == 'POST' and failed_product and product.pk == failed_product.pk:
                product.workflow_context = request.POST.get('workflow_context', '')
            product.quantity_form = failed_form if failed_product and product.pk == failed_product.pk else BasketQuantityForm(
                initial={'quantity': existing.get(product.pk, 1)}, auto_id=f'product_{product.pk}_%s',
            )
            product.in_basket = existing.get(product.pk, 0)
            product.plan = plan if plan and plan.authorization_id and plan.authorization.product_id == product.pk else None
            if not product.requires_authorisation:
                groups['open'].append(product)
            elif product.authorization is not None:
                groups['authorised'].append(product)
            elif product.pk in previous:
                # Products never prescribed to this patient are not listed.
                product.previous_authorization = previous[product.pk]
                groups['previous'].append(product)
        shown = {product.pk for group in groups.values() for product in group}
        prescribers = {product.authorization.prescribed_by for product in groups['authorised']}
        context = self.context(
            products=products, product_groups=groups, basket=basket, basket_count=sum(existing.values()),
            authorised_prescriber=next(iter(prescribers)) if len(prescribers) == 1 else None,
            authorised_until=min((product.authorization.expires_on for product in groups['authorised']), default=None),
            hidden_failed_form=failed_form if failed_product and failed_product.pk not in shown else None,
        )
        return render(request, 'portal/pharmacy_catalogue.html', context, status=status)

    def get(self, request):
        return self.display(request)


class PatientBasketItemView(PatientPharmacyView):
    http_method_names = ('post',)

    @transaction.atomic
    def post(self, request, pk):
        self.patient_company = Company.objects.select_for_update().get(pk=self.patient_company.pk)
        product = get_object_or_404(MedicationProduct.objects.for_company(self.patient_company), pk=pk)
        form = BasketQuantityForm(request.POST, auto_id=f'product_{pk}_%s')
        valid = form.is_valid()
        try:
            validate_workflow_context(request, self.patient_company, 'basket-product', product, patient=self.patient)
            if valid:
                set_basket_quantity(company=self.patient_company, patient=self.patient, actor=request.user, product=product,
                                    quantity=form.cleaned_data['quantity'], request=request)
                messages.success(request, 'Basket updated. No payment or stock allocation has occurred.')
                return redirect('portal:patient-basket')
        except ValidationError as error:
            _errors(request, form, error)
        return self.display(request, product, form, status=400)


class PatientBasketView(PatientPharmacyBase):
    http_method_names = ('get', 'head', 'options')

    def display(self, request, order, form=None, status=200):
        items = _order_items(order) if order else []
        for item in items:
            item.remove_context = self.token('basket-product', item.product)
        return render(request, 'portal/pharmacy_basket.html', self.context(
            order=order, items=items,
            form=form if form is not None else SupplyRequestForm(initial={'city': self.patient.city, 'phone': self.patient.phone}),
            workflow_context=request.POST.get('workflow_context', '') if request.method == 'POST' else self.token('patient-order', order) if order else '',
        ), status=status)

    def get(self, request):
        return self.display(request, self.orders().filter(status='draft').first())


class PatientOrderListView(PatientPharmacyBase):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        context = self.context(**_paginate(request, self.orders().exclude(status='draft')))
        context['patient_section'] = 'orders'
        context['orders'] = list(context['page_obj'].object_list)
        return render(request, 'portal/pharmacy_orders.html', context)


class PatientOrderDetailView(PatientPharmacyBase):
    def display(self, request, order, form=None, status=200):
        if order.status == 'draft':
            return PatientBasketView.display(self, request, order, form, status)
        return render(request, 'portal/pharmacy_order_detail.html', self.context(
            order=order, items=_order_items(order), shipment=order.shipment,
            cancel_form=form if form is not None else ConfirmForm(), can_cancel=order.status == 'submitted',
            patient_section='orders',
            workflow_context=(request.POST.get('workflow_context', '') if request.method == 'POST'
                              else self.token('patient-order', order)),
        ), status=status)

    def get(self, request, pk):
        return self.display(request, get_object_or_404(self.orders(), pk=pk))

    @transaction.atomic
    def post(self, request, pk):
        self.patient_company = Company.objects.select_for_update().get(pk=self.patient_company.pk)
        order = get_object_or_404(self.orders(), pk=pk)
        action = request.POST.get('action', '')
        form = SupplyRequestForm(request.POST) if action == 'submit' else ConfirmForm(request.POST)
        valid = form.is_valid()
        try:
            validate_workflow_context(request, self.patient_company, 'patient-order', order, patient=self.patient)
            if action not in ('submit', 'cancel'):
                raise ValidationError('Choose submit request or cancel.')
            if valid:
                if action == 'submit':
                    data = dict(form.cleaned_data)
                    note, confirm = data.pop('note'), data.pop('confirm')
                    submit_basket(order=order, actor=request.user, delivery_address=data, note=note, confirm=confirm,
                                  expected_revision=order.revision, request=request)
                    messages.success(request, 'Supply request submitted for practice review. No payment was collected.')
                else:
                    cancel_order(order=order, actor=request.user, confirm=True, request=request)
                    messages.success(request, 'Supply request cancelled.')
                return redirect('portal:patient-order-detail', pk=pk)
        except ValidationError as error:
            _errors(request, form, error)
        return self.display(request, order, form, status=400)
