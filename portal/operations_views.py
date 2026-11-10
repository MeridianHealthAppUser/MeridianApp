"""Separate practice operations and patient pharmacy pages."""

import csv
from calendar import monthrange
from datetime import date, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.http import FileResponse, HttpResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import pluralize
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt

from care.models import MedicationDose, MedicationProduct, PharmacyOrder, Shipment, ShipmentItem
from care.operations import (
    AVERAGE_WEEKS, UNDISPATCHED, can_ship, change_parcel, dispatch_shipment, dispatch_week, hold_or_cancel_shipment, lock_reasons,
    lock_week, mark_delivered, prepare_shipment, prepare_week, require_operations_actor,
    set_shipment_note, shipment_hold_reason, week_order, week_stock,
)
from care.pharmacy import accept_order, cancel_order, set_basket_quantity, submit_basket
from care.services import record_audit
from care.shipment_intake import create_manual_shipment
from practices.models import Company, CompanyMembership

from .dispatch_history import dispatch_delivery, shipment_doctor
from .filter_menus import fields_section, list_tools, ticked_sections, without_all
from .operations_forms import (
    SHIPPING_FILTER_FIELDS, BasketQuantityForm, CatalogueFilterForm, ConfirmForm, DeliveryAddressForm, DoseForm,
    HistoryFilterForm, OrderFilterForm, OrderForm, OrderReviewForm, ProductForm, ShipmentActionForm, ShipmentChangeForm,
    ShipmentCreateForm, ShippingFilterForm,
)
from .patient_views import patient_page_context
from .views import PatientPortalRequiredMixin, StaffCompanyRequiredMixin
from .workflow_context import make_workflow_context, validate_workflow_context


def _paginate(request, queryset, per_page=20):
    page = Paginator(queryset, per_page).get_page(request.GET.get('page'))
    filters = request.GET.copy()
    filters.pop('page', None)
    return dict(page_obj=page, is_paginated=page.has_other_pages(), pagination_query=filters.urlencode())


def _form_data(request, **defaults):
    data = request.GET.copy()
    for key, value in defaults.items():
        data.setdefault(key, value)
    return data


def _list_tools(request, form, *, url_name, sections, label, placeholder, count, noun):
    """The search at the top right of an Operations table, its Filters menu before it, and its count."""
    return dict(list_tools(action=reverse(url_name), data=request.GET, form=form, label=label, sections=sections,
                           placeholder=placeholder), count_label=f'{count} {noun}{pluralize(count)}')


def _search(queryset, query, *fields):
    """Every word of the search in at least one of the fields."""
    for term in query.split():
        condition = Q()
        for field in fields:
            condition |= Q(**{f'{field}__icontains': term})
        queryset = queryset.filter(condition)
    return queryset


def _shipment_search(queryset, query):
    return _search(queryset, query, 'patient__first_name', 'patient__last_name', 'tracking_number', 'waybill')


def _errors(request, form, error):
    for message in error.messages:
        form.add_error(None, message)
    # Read-only terminal states still show a rejected stale submission clearly.
    messages.error(request, ' '.join(error.messages))


@method_decorator(never_cache, name='dispatch')
class OperationsView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    admin_only = True
    nav_section = ''
    page_title = ''
    http_method_names = ('get', 'post', 'head', 'options')

    @property
    def can_edit(self):
        return self.membership.role in (CompanyMembership.Role.PRACTICE_ADMIN, CompanyMembership.Role.SUPER_ADMIN) or (
            self.shipping and can_ship(self.company, self.membership))

    def context(self, **kwargs):
        return dict(company=self.company, active_membership=self.membership, nav_section=self.nav_section,
                    page_title=self.page_title, can_edit=self.can_edit, **kwargs)

    def token(self, kind, record=None):
        if self.request.method == 'POST':
            return self.request.POST.get('workflow_context', '')
        return make_workflow_context(self.request, self.company, kind, record)

    def shipments(self):
        return Shipment.objects.for_company(self.company).filter(patient__company=self.company).select_related(
            'patient', 'company', 'subscription', 'authorization__prescribed_by', 'patient__assigned_doctor',
        ).prefetch_related('items__product')

    def orders(self):
        return PharmacyOrder.objects.for_company(self.company).filter(patient__company=self.company).exclude(status='draft').select_related('patient', 'shipment')


class CatalogueView(OperationsView):
    nav_section, page_title = 'catalogue', 'Pharmacy catalogue'
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        from .export_support import export_url

        form = CatalogueFilterForm(without_all(request.GET, 'status'))
        queryset = MedicationProduct.objects.for_company(self.company).order_by('name', 'strength', 'pk')
        if form.is_valid():
            queryset = _search(queryset, form.cleaned_data['q'], 'name', 'strength')
            # Published and Hidden together are every product.
            if len(form.cleaned_data['status']) == 1:
                queryset = queryset.filter(is_active=form.cleaned_data['status'] == ['active'])
        else:
            queryset = queryset.none()
        context = self.context(filter_form=form, **_paginate(request, queryset))
        context.update(_list_tools(request, form, url_name='portal:ops-catalogue', label='Filter products',
                                   sections=ticked_sections(form, request.GET, ('status',)), placeholder='Product or strength',
                                   count=context['page_obj'].paginator.count, noun='product'))
        context['can_edit'] = self.membership.role == CompanyMembership.Role.SUPER_ADMIN
        context['products'] = list(context['page_obj'].object_list)
        # What doctors prescribe: each dose, its pen, how often it is sent and what it costs.
        context['doses'] = list(MedicationDose.objects.for_company(self.company).select_related('pen').order_by(
            'pen__name', 'amount_mg', 'frequency', 'pk'))
        # The standard ways to start a main medicine, offered by Initiate prescription on the patient record.
        from care.models import StartingPlan
        from care.starting_plans import plan_summary

        context['starting_plans'] = list(StartingPlan.objects.for_company(self.company).select_related('then_dose__pen').prefetch_related('steps__dose__pen'))
        for plan in context['starting_plans']:
            plan.summary = plan_summary(plan)
        context.update(doses_export_url=export_url('catalogue-doses'), plans_export_url=export_url('catalogue-plans'))
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


class DoseEditorView(OperationsView):
    """Add or change a dose doctors can prescribe. Super Admins change doses; practice administrators read them."""

    nav_section, page_title = 'catalogue', 'Dose'

    def display(self, request, dose, form, status=200):
        context = self.context(dose=dose, form=form, workflow_context=self.token('catalogue-dose', dose))
        context['can_edit'] = self.membership.role == CompanyMembership.Role.SUPER_ADMIN
        context['in_use'] = bool(dose and (dose.authorizations.exists() or dose.starting_plan_steps.exists()
                                           or dose.starting_plans_ending.exists()))
        return render(request, 'portal/operations_dose_form.html', context, status=status)

    def dose(self, pk):
        return get_object_or_404(MedicationDose.objects.for_company(self.company).select_related('pen'), pk=pk) if pk else None

    def get(self, request, pk=None):
        dose = self.dose(pk)
        if not dose:
            require_operations_actor(self.company, request.user, catalogue=True)
        return self.display(request, dose, DoseForm(company=self.company, instance=dose))

    @transaction.atomic
    def post(self, request, pk=None):
        self.company = Company.objects.select_for_update().get(pk=self.company.pk)
        require_operations_actor(self.company, request.user, catalogue=True)
        dose = self.dose(pk)
        form = DoseForm(request.POST, company=self.company, instance=dose)
        try:
            validate_workflow_context(request, self.company, 'catalogue-dose', dose)
            if form.is_valid():
                saved = form.save()
                record_audit(company=self.company, actor=request.user,
                             action='catalogue.dose_updated' if pk else 'catalogue.dose_created',
                             target=saved, request=request, metadata={'changed_fields': form.changed_data})
                # A new price can put a patient's collection over their debit order: they're asked to approve it.
                from care.billing import review_mandates

                review_mandates(self.company)
                messages.success(request, 'Dose saved. Prescriptions already written keep their own dose and dates.')
                return redirect('portal:ops-dose-detail', pk=saved.pk)
        except ValidationError as error:
            if not form.is_bound or not hasattr(form, 'cleaned_data'):
                form.is_valid()
            _errors(request, form, error)
        return self.display(request, dose, form, status=400)


def _shipping_filter(request, queryset, default_week=None, *, company):
    # A shipping week runs Monday to Sunday: any day in ?week= means its week. One that isn't a date is reported.
    data = _form_data(request)
    try:
        day = date.fromisoformat(request.GET['week'][:10]) if request.GET.get('week') else (default_week or this_week())
        data['week'] = (day - timedelta(days=(day.weekday() - 1) % 7)).isoformat()
    except ValueError:
        pass
    form = ShippingFilterForm(without_all(data, *SHIPPING_FILTER_FIELDS), company=company)
    if form.is_valid():
        week = form.cleaned_data['week']
        queryset = queryset.filter(scheduled_for__range=(week, week + timedelta(days=6)))
        if form.cleaned_data['status']:
            queryset = queryset.filter(status__in=form.cleaned_data['status'])
        if form.cleaned_data['product']:
            queryset = queryset.filter(items__product_id__in=form.cleaned_data['product']).distinct()
        if form.cleaned_data['doctor']:
            doctors = form.cleaned_data['doctor']
            queryset = queryset.filter(Q(authorization__prescribed_by_id__in=doctors) | Q(
                authorization__prescribed_by__isnull=True, patient__assigned_doctor_id__in=doctors))
        queryset = _shipment_search(queryset, form.cleaned_data['q'])
        payments = set(form.cleaned_data['payment'])
        form.payment_states = {}
        if len(payments) == 1:
            from care.billing import parcel_payment
            matches = []
            for shipment in queryset:
                state = parcel_payment(shipment)
                if ('paid' if state[0] == 'paid' else 'unpaid') in payments:
                    matches.append(shipment.pk)
                    form.payment_states[shipment.pk] = state
            queryset = queryset.filter(pk__in=matches)
    else:
        queryset = queryset.none()
    return form, queryset


def this_week():
    today = timezone.localdate()
    return today - timedelta(days=(today.weekday() - 1) % 7)


def chosen_week(request, default=None):
    """The Tuesday of the week in ?week=, or of the default week (this week unless given)."""
    try:
        day = date.fromisoformat(request.GET.get('week', '')[:10])
    except ValueError:
        return default or this_week()
    return day - timedelta(days=(day.weekday() - 1) % 7)


def shipping_week(company):
    """The week Weekly shipping opens on: the one with the next shipping day (today's, on the day itself). After the
    courier's collection, that is next week's list, which is the one being set up."""
    from care.parcels import shipping_day_from, shipping_weekday

    day = shipping_day_from(timezone.localdate(), shipping_weekday(company))
    return day - timedelta(days=(day.weekday() - 1) % 7)


def week_navigation(url_name, week, *, home=None, **query):
    """Week 43 · 19 Oct 2026 – 25 Oct 2026 with the weeks either side, as Calendar & Bookings' Week view, and a link
    back to the page's own week (home: this week unless given)."""
    link = lambda monday: f'{reverse(url_name)}?{urlencode({**query, "week": monday.isoformat()})}'
    home = home or this_week()
    return dict(week_start=week, week_end=week + timedelta(days=6), week_number=week.isocalendar().week,
                prev_week_url=link(week - timedelta(days=7)), next_week_url=link(week + timedelta(days=7)),
                this_week_url=link(home) if week != home else None,
                this_week_label='This week' if home == this_week() else 'Next week')


SHIPPING_STAGES = ('pending', 'confirmed', 'tracking')
SHIPPING_STAGE_ALIASES = {'ready': 'pending', 'lock': 'pending', 'courier': 'confirmed', 'collection': 'tracking'}


def selected_shipping_stage(request):
    stage = request.GET.get('shipping_step', 'pending')
    stage = SHIPPING_STAGE_ALIASES.get(stage, stage)
    return stage if stage in (*SHIPPING_STAGES, 'all') else 'pending'


def shipping_stage_parcels(queryset, stage):
    """The work waiting at a stage. Viewing a stage never advances a parcel."""
    stage = SHIPPING_STAGE_ALIASES.get(stage, stage)
    if stage == 'pending':
        return queryset.filter(Q(status__in=(Shipment.Status.DRAFT, Shipment.Status.HELD)) |
                               Q(status=Shipment.Status.READY, locked_at__isnull=True))
    if stage == 'confirmed':
        return queryset.filter(status=Shipment.Status.READY, locked_at__isnull=False, waybill='', tracking_number='')
    if stage == 'tracking':
        return queryset.filter(Q(status=Shipment.Status.DISPATCHED) |
            (Q(status=Shipment.Status.READY, locked_at__isnull=False) & (~Q(waybill='') | ~Q(tracking_number=''))))
    return queryset


def _shipping_workflow_context(request, week, order, parcels, *, to_collect):
    counts = parcels.aggregate(
        active=Count('pk', filter=~Q(status=Shipment.Status.CANCELLED)),
        delivered=Count('pk', filter=Q(status=Shipment.Status.DELIVERED)),
    )
    pending = shipping_stage_parcels(parcels, 'pending').count()
    confirmed = shipping_stage_parcels(parcels, 'confirmed').count()
    tracking = shipping_stage_parcels(parcels, 'tracking').count()
    current = 'pending' if pending else ('confirmed' if confirmed else ('tracking' if tracking and counts['delivered'] < counts['active'] else None))
    definitions = (
        ('pending', 'Pending', f'{pending} awaiting confirmation', bool(counts['active'] and not pending)),
        ('confirmed', 'Confirmed & Locked', f'{confirmed} awaiting courier', bool(tracking and not pending and not confirmed)),
        ('tracking', 'Tracking', f'{tracking} shipment' + ('s' if tracking != 1 else ''),
         bool(counts['active'] and counts['active'] == counts['delivered'])),
    )
    selected = selected_shipping_stage(request)
    stage_counts = {'pending': pending, 'confirmed': confirmed, 'tracking': tracking}
    query = request.GET.copy()
    for name in ('create', 'collect', 'collect_demo', 'note', '__shell', '__full', 'shipping_step', 'page'):
        query.pop(name, None)
    query['week'] = week.isoformat()
    query['__full'] = '1'
    steps = []
    for key, title, summary, complete in definitions:
        query['shipping_step'] = key
        steps.append(dict(key=key, title=title, count=stage_counts[key], summary=summary, complete=complete,
                          current=current == key, selected=selected == key,
                          url=f"{reverse('portal:ops-shipping')}?{query.urlencode()}#shipping-workflow"))
    query['shipping_step'] = 'all'
    all_shipments_url = f"{reverse('portal:ops-shipping')}?{query.urlencode()}"
    query['shipping_step'] = 'tracking'
    collection_close_url = steps[-1]['url']
    query['collect'] = '1'
    collection_url = f"{reverse('portal:ops-shipping')}?{query.urlencode()}#record-collection"
    query['collect_demo'] = '1'
    return dict(shipping_steps=steps, shipping_step=selected, all_shipments_url=all_shipments_url,
                collection_url=collection_url,
                demo_collection_url=f"{reverse('portal:ops-shipping')}?{query.urlencode()}#record-collection",
                collect_demo_mode=request.GET.get('collect_demo') == '1',
                collection_close_url=collection_close_url, collect_open=request.GET.get('collect') == '1')


class ShippingListView(OperationsView):
    """A shipping week split into actionable stage queues, with shared product, doctor and payment filters."""
    shipping = True

    nav_section, page_title = 'shipping', 'Weekly shipping'
    http_method_names = ('get', 'head', 'options')
    per_page = 100

    def display(self, request, create_form=None, status=200):
        from care.billing import parcel_payment, parcel_payment_reason, practice_settings
        from care.courier import changed_since_booking, collection_problem, courier_enabled
        from care.models import DeliveryAddress
        from care.operations import delivery_problem

        home = shipping_week(self.company)
        week = chosen_week(request, home)
        form, queryset = _shipping_filter(request, self.shipments(), home, company=self.company)
        stage = selected_shipping_stage(request)
        queryset = shipping_stage_parcels(queryset, stage).order_by('scheduled_for', 'patient__last_name', 'pk')
        fresh = lambda kind: make_workflow_context(request, self.company, kind)
        context = self.context(filter_form=form, lock_context=fresh('shipping-week'), **_paginate(request, queryset, self.per_page),
                               **week_navigation('portal:ops-shipping', week, home=home,
                                                 **{key: ','.join(request.GET.getlist(key)) if key in SHIPPING_FILTER_FIELDS else value
                                                    for key, value in request.GET.items()
                                                    if key in (*SHIPPING_FILTER_FIELDS, 'q', 'view', 'shipping_step')}))
        context['is_all_shipments'] = stage == 'all'
        context['can_bulk_confirm'] = self.can_edit and stage == 'pending'
        context['show_workflow_status'] = stage == 'all'
        context['show_shipping_status'] = stage == 'tracking'
        context['shipping_column_count'] = 9 + int(context['can_bulk_confirm']) + int(stage in ('all', 'tracking'))
        context['shipments'] = list(context['page_obj'].object_list)
        addresses = {address.patient_id: address for address in DeliveryAddress.objects.filter(
            company=self.company, patient__in={shipment.patient_id for shipment in context['shipments']})}
        payment_required = practice_settings(self.company).require_payment
        here = request.get_full_path()
        for shipment in context['shipments']:
            open_parcel = shipment.status in UNDISPATCHED
            shipment.current_hold_reason = shipment_hold_reason(shipment) if open_parcel else ''
            shipment.doctor = shipment_doctor(shipment)
            shipment.address = addresses.get(shipment.patient_id)
            # Changed after The Courier Guy was booked: the waybill still has the old address.
            shipment.address_changed = open_parcel and changed_since_booking(shipment, shipment.address)
            shipment.payment_state, shipment.payment_text = (getattr(form, 'payment_states', {}).get(shipment.pk)
                                                                or parcel_payment(shipment))
            # What Check and lock week would hold it back for, besides its prescription; payment is in its own column.
            shipment.lock_problems = [delivery_problem(shipment)] if open_parcel and delivery_problem(shipment) else []
            if self.can_edit and shipment.status not in (Shipment.Status.CANCELLED, Shipment.Status.DELIVERED):
                shipment.address_url = f'{reverse("portal:ops-delivery-address", args=[shipment.patient_id])}?{urlencode({"next": here})}'
            # Mark ready on its line, for a draft or held parcel.
            if self.can_edit and shipment.status in (Shipment.Status.DRAFT, Shipment.Status.HELD):
                shipment.ready_context = make_workflow_context(request, self.company, 'shipment-ready', shipment)
            if self.can_edit and (shipment.status == Shipment.Status.HELD or
                    (shipment.status in (Shipment.Status.DRAFT, Shipment.Status.READY) and not shipment.locked_at)):
                shipment.confirm_context = make_workflow_context(request, self.company, 'shipping-confirm', shipment)
                shipment.confirm_week = shipment.scheduled_for - timedelta(days=(shipment.scheduled_for.weekday() - 1) % 7)
            if self.can_edit and shipment.status == Shipment.Status.READY and shipment.locked_at and not (shipment.waybill or shipment.tracking_number):
                shipment.waybill_context = make_workflow_context(request, self.company, 'shipment-waybill', shipment)
            shipment.shipping_status_label = ('Pending' if shipment.status == Shipment.Status.DRAFT or
                (shipment.status == Shipment.Status.READY and not shipment.locked_at) else
                ('Confirmed & Locked' if shipment.status == Shipment.Status.READY else shipment.get_status_display()))
            shipment.pending_problems = []
            if stage == 'pending':
                problems = [shipment.current_hold_reason, *shipment.lock_problems, parcel_payment_reason(shipment)]
                if shipment.status == Shipment.Status.HELD:
                    problems.append(shipment.hold_reason or 'This parcel is on hold and needs review.')
                shipment.pending_problems = list(dict.fromkeys(problem for problem in problems if problem))
            elif stage == 'confirmed' and shipment.courier_error:
                shipment.pending_problems = [shipment.courier_error]
            # Its note, in the Notes column, until it's delivered or cancelled.
            if self.can_edit and shipment.status not in (Shipment.Status.CANCELLED, Shipment.Status.DELIVERED):
                shipment.note_context = make_workflow_context(request, self.company, 'shipment-note', shipment)
        context['shipping_view'] = 'medication' if request.GET.get('view') == 'medication' else 'customers'
        grouped = {}
        selected_products = set(form.cleaned_data.get('product', ())) if form.is_valid() else set()
        for parcel in context['shipments']:
            for item in parcel.items.all():
                if selected_products and str(item.product_id) not in selected_products:
                    continue
                group = grouped.setdefault(item.product_id, dict(product=item.product, quantity=0, parcels=[]))
                group['quantity'] += item.quantity
                group['parcels'].append(dict(shipment=parcel, quantity=item.quantity, dose=item.dose,
                                            warning_key=f"{item.product_id}-{len(group['parcels']) + 1}"))
        context['medication_groups'] = list(grouped.values())
        for mode in ('customers', 'medication'):
            query = request.GET.copy()
            query.pop('note', None)
            query['view'] = mode
            context[f'{mode}_view_url'] = '?' + query.urlencode()
        list_query = request.GET.copy()
        list_query.pop('note', None)
        context['list_query'] = list_query.urlencode()
        for shipment in context['shipments']:
            note_query = list_query.copy()
            note_query['note'] = str(shipment.pk)
            shipment.note_url = f"{reverse('portal:ops-shipping')}?{note_query.urlencode()}#shipment-note-dialog"
        note_record = next((shipment for shipment in context['shipments']
                            if str(shipment.pk) == request.GET.get('note')
                            and (getattr(shipment, 'note_context', None) or shipment.note)), None)
        context.update(note_record=note_record, note_open=note_record is not None,
                       note_read_only=note_record is not None and not bool(getattr(note_record, 'note_context', None)))
        tools = _list_tools(request, form, url_name='portal:ops-shipping', label='Filter shipments',
                            sections=ticked_sections(form, request.GET, SHIPPING_FILTER_FIELDS,
                                                     icons={'product': 'tag', 'doctor': 'person'}), placeholder='Patient or waybill',
                            count=context['page_obj'].paginator.count, noun='shipment')
        # The search and the Filters keep the week; clearing either stays on it.
        kept = {'week': week.isoformat(), 'view': context['shipping_view']}
        tools['search_hidden'] = {**tools['search_hidden'], **kept}
        tools['search_clear_url'] = f"{reverse('portal:ops-shipping')}?{urlencode({**{k: v for k, v in tools['search_hidden'].items() if k != 'q'}})}"
        tools['filter_menu'].update(hidden=[*tools['filter_menu']['hidden'], *kept.items()],
                                    clear_url=f"{reverse('portal:ops-shipping')}?{urlencode({**kept, 'q': tools['search']})}")
        context.update(tools)
        # The week's steps and pens, from what was Dispatch stock.
        order = week_order(self.company, week)
        week_parcels = Shipment.objects.filter(company=self.company, patient__company=self.company,
                                               scheduled_for__range=(week, week + timedelta(days=6)))
        locked = week_parcels.filter(status=Shipment.Status.READY, locked_at__isnull=False)
        context.update(
            week_order=order, average_weeks=AVERAGE_WEEKS,
            show_stock_detail=self.membership.role == CompanyMembership.Role.SUPER_ADMIN,
            ready_unlocked=order['parcels'] - order['locked'],
            lock_blocked=sum(1 for shipment in week_parcels.filter(status=Shipment.Status.READY, locked_at__isnull=True)
                             if lock_reasons(shipment)),
            courier_enabled=courier_enabled(), courier_problem=collection_problem(self.company) if courier_enabled() else '',
            to_book=shipping_stage_parcels(week_parcels, 'confirmed').count(),
            to_book_demo=shipping_stage_parcels(week_parcels, 'confirmed').filter(patient__demo_data_set__isnull=False).count(),
            to_collect=week_parcels.filter(status=Shipment.Status.READY, locked_at__isnull=False).exclude(waybill='', tracking_number='').count(),
            to_collect_demo=week_parcels.filter(status=Shipment.Status.READY, locked_at__isnull=False, patient__demo_data_set__isnull=False).exclude(waybill='', tracking_number='').count(),
            payment_required=payment_required,
            order_csv_url=f'{reverse("portal:ops-week-order")}?{urlencode({"week": week.isoformat()})}',
            manifest_url=f'{reverse("portal:ops-manifest")}?{urlencode({"week": week.isoformat()})}',
        )
        from care.inventory import stock_rows
        context['stock_shortages'] = [row for row in stock_rows(self.company) if row['shortage']]
        context['stock_shortage_count'] = sum(row['shortage'] for row in context['stock_shortages'])
        context.update(_shipping_workflow_context(request, week, order, week_parcels,
                                                  to_collect=context['to_collect']))
        # Search/filter requests retain the selected workflow panel without changing its actions.
        context['search_hidden']['shipping_step'] = context['shipping_step']
        context['filter_menu']['hidden'].append(('shipping_step', context['shipping_step']))
        context['search_clear_url'] += f"&shipping_step={context['shipping_step']}"
        context['filter_menu']['clear_url'] += f"&shipping_step={context['shipping_step']}"
        if self.can_edit:
            collection_parcels = week_parcels.filter(status=Shipment.Status.READY, locked_at__isnull=False).exclude(waybill='', tracking_number='')
            if context['collect_demo_mode']:
                collection_parcels = collection_parcels.filter(patient__demo_data_set__isnull=False)
            context['collect_count'] = context['to_collect_demo'] if context['collect_demo_mode'] else context['to_collect']
            # A dialog reopened after a mistake keeps the token it was sent with.
            context.update(create_form=create_form or ShipmentCreateForm(company=self.company),
                           create_context=self.token('shipment-create') if create_form else fresh('shipment-create'),
                           prepare_context=fresh('shipping-prepare'), confirm_context=fresh('shipping-confirm'), courier_context=fresh('shipping-courier'),
                           collect_context=fresh('shipping-collect'),
                           collect_products=MedicationProduct.objects.filter(
                               pk__in=ShipmentItem.objects.filter(shipment__in=collection_parcels).values('product')),
                           create_open=create_form is not None or request.GET.get('create') == '1')
        return render(request, 'portal/operations_shipping.html', context, status=status)

    def get(self, request):
        return self.display(request)


def _shipping_list_url(query):
    """Return to the filtered shipping list, preserving repeated selections."""
    kept = QueryDict(query or '').copy()
    allowed = (*SHIPPING_FILTER_FIELDS, 'week', 'page', 'q', 'view', 'shipping_step')
    for key in list(kept):
        if key not in allowed:
            kept.pop(key)
    return f'{reverse("portal:ops-shipping")}?{kept.urlencode()}' if kept else reverse('portal:ops-shipping')


class ShipmentReadyView(OperationsView):
    """Mark ready from a parcel's line on the Weekly shipping list, with the checks Mark ready makes on its page."""
    shipping = True

    http_method_names = ('post',)

    @transaction.atomic
    def post(self, request, pk):
        self.company = Company.objects.select_for_update().get(pk=self.company.pk)
        require_operations_actor(self.company, request.user, shipping=True)
        shipment = get_object_or_404(self.shipments(), pk=pk)
        try:
            validate_workflow_context(request, self.company, 'shipment-ready', shipment)
            prepare_shipment(shipment=shipment, actor=request.user, request=request)
            messages.success(request, f'Parcel for {shipment.patient} on {shipment.scheduled_for:%-d %b} marked ready.')
        except ValidationError as error:
            messages.error(request, f'Parcel for {shipment.patient} not marked ready: {" ".join(error.messages)}')
        return redirect(_shipping_list_url(request.POST.get('list_query')))


class ShipmentNoteView(OperationsView):
    """A parcel's note, from the Notes column of the Weekly shipping list."""
    shipping = True

    http_method_names = ('post',)

    def post(self, request, pk):
        require_operations_actor(self.company, request.user, shipping=True)
        shipment = get_object_or_404(self.shipments(), pk=pk)
        try:
            validate_workflow_context(request, self.company, 'shipment-note', shipment)
            if shipment.status in (Shipment.Status.CANCELLED, Shipment.Status.DELIVERED):
                raise ValidationError('This parcel is closed, so its note can’t be changed.')
            shipment = set_shipment_note(shipment=shipment, actor=request.user, note=request.POST.get('note', ''), request=request)
            messages.success(request, f'Note {"saved" if shipment.note else "removed"} for {shipment.patient}’s parcel.')
        except ValidationError as error:
            messages.error(request, f'Note for {shipment.patient} not saved: {" ".join(error.messages)}')
        return redirect(_shipping_list_url(request.POST.get('list_query')))


class ShipmentWaybillView(OperationsView):
    shipping = True
    nav_section, page_title = 'shipping', 'Add waybill'
    http_method_names = ('get', 'post')

    def display(self, request, shipment, *, errors=(), status=200):
        return render(request, 'portal/operations_waybill.html', self.context(
            waybill_shipment=shipment, waybill_open=True, waybill_errors=errors,
            waybill_value=request.POST.get('waybill', ''),
            waybill_token=make_workflow_context(request, self.company, 'shipment-waybill', shipment),
            waybill_next=_safe_next(request, reverse('portal:ops-shipping'))), status=status)

    def get(self, request, pk):
        require_operations_actor(self.company, request.user, shipping=True)
        shipment = get_object_or_404(self.shipments(), pk=pk, status=Shipment.Status.READY,
                                     locked_at__isnull=False, waybill='', tracking_number='')
        return self.display(request, shipment)

    @transaction.atomic
    def post(self, request, pk):
        self.company = Company.objects.select_for_update().get(pk=self.company.pk)
        require_operations_actor(self.company, request.user, shipping=True)
        shipment = get_object_or_404(self.shipments().select_for_update(of=('self',)), pk=pk)
        try:
            validate_workflow_context(request, self.company, 'shipment-waybill', shipment)
            reference = request.POST.get('waybill', '').strip()
            if not reference or len(reference) > 60:
                raise ValidationError('Enter a waybill of up to 60 characters.')
            if shipment.status != Shipment.Status.READY or not shipment.locked_at or shipment.waybill or shipment.tracking_number:
                raise ValidationError('This parcel has changed. Refresh the shipping list.')
            if Shipment.objects.filter(Q(waybill=reference) | Q(tracking_number=reference)).exclude(pk=pk).exists():
                raise ValidationError('This waybill is already assigned to another parcel.')
            shipment.waybill = reference
            shipment.waybill_created_at = timezone.now()
            shipment.save(update_fields=('waybill', 'waybill_created_at', 'updated_at'))
            from care.services import record_audit
            record_audit(company=self.company, actor=request.user, patient=shipment.patient,
                         action='shipment.waybill_recorded', target=shipment, request=request,
                         metadata={'waybill': reference})
            messages.success(request, 'Waybill saved. Parcel moved to Tracking.')
        except ValidationError as error:
            return self.display(request, shipment, errors=error.messages, status=400)
        return redirect(_safe_next(request, _shipping_list_url(request.POST.get('list_query'))))


class DispatchStockView(OperationsView):
    """The former stock screen now lives in Inventory."""
    shipping = False

    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        return redirect('portal:inventory')


class ShippingConfirmView(OperationsView):
    """Confirm and reserve selected parcels with one explicit, fully validated action."""
    shipping = True
    http_method_names = ('post',)

    @transaction.atomic
    def post(self, request):
        from care.operations import confirm_shipments

        self.company = Company.objects.select_for_update().get(pk=self.company.pk)
        require_operations_actor(self.company, request.user, shipping=True)
        form = ShippingFilterForm({'week': request.POST.get('week')})
        try:
            try:
                selected = {int(value) for value in request.POST.getlist('shipment_ids')}
            except (ValueError, TypeError):
                raise ValidationError('Select valid parcels to confirm.')
            single = request.POST.get('single_shipment')
            if single:
                if len(selected) != 1 or single != str(next(iter(selected))):
                    raise ValidationError('The confirmation does not match this parcel.')
                parcel = get_object_or_404(self.shipments(), pk=single)
                validate_workflow_context(request, self.company, 'shipping-confirm', parcel)
            else:
                validate_workflow_context(request, self.company, 'shipping-confirm')
            if not form.is_valid():
                raise ValidationError('Choose a valid shipping week.')
            week = form.cleaned_data['week']
            week -= timedelta(days=(week.weekday() - 1) % 7)
            confirmed = confirm_shipments(company=self.company, actor=request.user, week_start=week,
                                          shipment_ids=selected, request=request)
            message = f'{len(confirmed)} parcel{pluralize(len(confirmed))} confirmed and locked. Stock reserved.'
            from care.inventory import stock_rows
            shortages = [f"{row['product']}: {row['shortage']} short" for row in stock_rows(self.company) if row['shortage']]
            if shortages:
                messages.warning(request, message + ' Stock shortages: ' + '; '.join(shortages) + '.')
            else:
                messages.success(request, message)
        except ValidationError as error:
            messages.error(request, 'Not confirmed: ' + ' · '.join(error.messages))
        return _week_redirect(request, 'pending')


class ShippingPrepareView(OperationsView):
    """Mark selected draft parcels ready after their clinical and supply checks."""
    shipping = True

    http_method_names = ('post',)

    def post(self, request):
        require_operations_actor(self.company, request.user, shipping=True)
        form = ShippingFilterForm({'week': request.POST.get('week')})
        try:
            validate_workflow_context(request, self.company, 'shipping-prepare')
            if not form.is_valid():
                raise ValidationError('Choose a valid shipping week.')
            try:
                selected = {int(value) for value in request.POST.getlist('shipment_ids')}
            except ValueError:
                raise ValidationError('Select valid parcels.')
            ready, left = prepare_week(company=self.company, actor=request.user, week_start=form.cleaned_data['week'],
                                       shipment_ids=selected, request=request)
            message = f'{len(ready)} draft parcel{"" if len(ready) == 1 else "s"} marked ready.'
            if left:
                message += f' {len(left)} could not be: see the hold reason beside {"it" if len(left) == 1 else "them"}.'
            (messages.warning if left else messages.success)(request, message)
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
        return redirect(_shipping_list_url(request.POST.get('list_query') or urlencode({'week': request.POST.get('week', '')})))


class WeekOrderDownloadView(OperationsView):
    """The week's pens to order, as a CSV for the supplier order."""
    shipping = True

    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        form, _ = _shipping_filter(request, self.shipments(), shipping_week(self.company), company=self.company)
        if not form.is_valid():
            return HttpResponse('Choose a valid shipping week.', status=400)
        week = form.cleaned_data['week']
        order = week_order(self.company, week)
        rows = [(line['product'].name, line['product'].strength, line['pens']) for line in order['lines']]
        if request.method != 'HEAD':
            record_audit(company=self.company, actor=request.user, action='shipping.order_exported', request=request,
                         metadata={'week': week.isoformat(), 'rows': len(rows)})
        return _csv_response(f'pens-to-order-{week.isoformat()}.csv', ('Medicine', 'Pen', 'Pens'), rows)


class ShipmentCreateView(ShippingListView):
    """An ad-hoc parcel, from the Create shipment dialog on the Weekly shipping list. A mistake reopens the dialog."""

    http_method_names = ('get', 'post')

    def get(self, request):
        require_operations_actor(self.company, request.user, shipping=True)
        return redirect(f'{reverse("portal:ops-shipping")}?create=1')

    def post(self, request):
        require_operations_actor(self.company, request.user, shipping=True)
        form = ShipmentCreateForm(request.POST, company=self.company)
        valid = form.is_valid()
        try:
            intent = validate_workflow_context(request, self.company, 'shipment-create')
            if valid:
                data = dict(form.cleaned_data)
                data.pop('confirm')
                shipment = create_manual_shipment(company=self.company, actor=request.user, submission_key=intent['key'], request=request, **data)
                day = shipment.scheduled_for
                messages.success(request, f'Draft parcel added for {shipment.patient} on {day:%-d %b %Y}. Confirm & lock it when it is ready for shipping.')
                return redirect(f'{reverse("portal:ops-shipping")}?{urlencode({"week": (day - timedelta(days=(day.weekday() - 1) % 7)).isoformat()})}')
        except ValidationError as error:
            for message in error.messages:
                form.add_error(None, message)
        return self.display(request, create_form=form, status=400)


class ShipmentDetailView(OperationsView):
    shipping = True
    nav_section, page_title = 'shipping', 'Shipment'

    def display(self, request, shipment, form=None, status=200, change_form=None):
        from .patient_action_context import patient_action_context
        from .export_support import export_url

        items = list(shipment.items.select_related('product'))
        if self.can_edit and shipment.status == Shipment.Status.READY and shipment.locked_at and not (shipment.waybill or shipment.tracking_number):
            shipment.waybill_context = make_workflow_context(request, self.company, 'shipment-waybill', shipment)
        # Each form keeps the token it was sent with after a mistake; the other gets a current one.
        changing = change_form is not None
        context = self.context(shipment=shipment, items=items, change_form=None,
                               form=form if form is not None else ShipmentActionForm(shipment=shipment, items=self.batch_items(shipment, items)),
                               workflow_context=make_workflow_context(request, self.company, 'shipment', shipment) if changing
                               else self.token('shipment', shipment),
                               current_hold_reason=shipment_hold_reason(shipment) if shipment.status in UNDISPATCHED else '')
        if shipment.status in (Shipment.Status.CANCELLED, Shipment.Status.DELIVERED):
            context['can_edit'] = False
        # A parcel that isn't locked can be moved to another day or given other pens.
        if context['can_edit'] and shipment.status in UNDISPATCHED and not shipment.locked_at:
            context.update(change_form=change_form or ShipmentChangeForm(items=items, initial={'scheduled_for': shipment.scheduled_for}),
                           change_context=self.token('shipment-change', shipment) if changing
                           else make_workflow_context(request, self.company, 'shipment-change', shipment))
        context.update(patient_action_context(self, shipment.patient, 'deliveries',
            content_template='portal/includes/shipment_detail_content.html', stylesheets=('css/operations.css',),
            title=f'Shipment {shipment.pk}'))
        if not context.get('patient_workspace'):
            context['shipment_items_export_url'] = export_url('shipment-items', pk=shipment.pk)
            context['shipment_items_count'] = len(shipment.dispatch_snapshot.get('items', ())) if shipment.dispatch_snapshot else len(items)
        return render(request, 'portal/patient_workspace_action.html' if context.get('patient_workspace') else 'portal/operations_shipment_detail.html', context, status=status)

    @staticmethod
    def batch_items(shipment, items):
        """The lines whose batch can still be noted: until the parcel is dispatched."""
        return items if shipment.status in UNDISPATCHED else []

    def get(self, request, pk):
        return self.display(request, get_object_or_404(self.shipments(), pk=pk))

    @transaction.atomic
    def post(self, request, pk):
        self.company = Company.objects.select_for_update().get(pk=self.company.pk)
        require_operations_actor(self.company, request.user, shipping=True)
        shipment = get_object_or_404(self.shipments(), pk=pk)
        form = ShipmentActionForm(request.POST, shipment=shipment,
                                  items=self.batch_items(shipment, list(shipment.items.select_related('product'))))
        valid = form.is_valid()
        try:
            validate_workflow_context(request, self.company, 'shipment', shipment)
            if valid:
                action = form.cleaned_data['action']
                if action == 'confirm':
                    from care.operations import confirm_shipments

                    week = shipment.scheduled_for - timedelta(days=(shipment.scheduled_for.weekday() - 1) % 7)
                    confirm_shipments(company=self.company, actor=request.user, week_start=week,
                                      shipment_ids=[shipment.pk], request=request)
                elif action in ('hold', 'cancel'):
                    hold_or_cancel_shipment(shipment=shipment, actor=request.user, cancel=action == 'cancel', reason=form.cleaned_data['reason'], request=request)
                elif action == 'dispatch':
                    dispatch_shipment(shipment=shipment, actor=request.user, tracking_number=form.cleaned_data['tracking_number'] or shipment.waybill or shipment.tracking_number,
                                      confirm=True, batch_notes=form.batch_notes(), stock_override=form.cleaned_data['stock_override'],
                                      stock_override_reason=form.cleaned_data['stock_override_reason'], request=request)
                elif action == 'deliver':
                    mark_delivered(shipment=shipment, actor=request.user, confirm=True, request=request)
                if action == 'confirm':
                    from care.inventory import stock_rows

                    product_ids = set(shipment.items.values_list('product_id', flat=True))
                    shortages = [f"{row['product']}: {row['shortage']} short" for row in stock_rows(self.company)
                                 if row['product'].pk in product_ids and row['shortage']]
                    message = 'Parcel confirmed and locked. Stock reserved.'
                    if shortages:
                        messages.warning(request, message + ' Stock shortages: ' + '; '.join(shortages) + '.')
                    else:
                        messages.success(request, message)
                else:
                    messages.success(request, 'Shipment action recorded.')
                from .patient_action_context import workspace_redirect
                return workspace_redirect(request, 'portal:ops-shipment-detail', pk=pk)
        except ValidationError as error:
            _errors(request, form, error)
        return self.display(request, shipment, form, status=400)


class ShipmentChangeView(ShipmentDetailView):
    """Change this parcel: its planned dispatch date and pens, until its week is locked."""

    http_method_names = ('post',)

    @transaction.atomic
    def post(self, request, pk):
        self.company = Company.objects.select_for_update().get(pk=self.company.pk)
        require_operations_actor(self.company, request.user, shipping=True)
        shipment = get_object_or_404(self.shipments(), pk=pk)
        form = ShipmentChangeForm(request.POST, items=list(shipment.items.select_related('product')))
        try:
            validate_workflow_context(request, self.company, 'shipment-change', shipment)
            if form.is_valid():
                change_parcel(shipment=shipment, actor=request.user, scheduled_for=form.cleaned_data['scheduled_for'],
                              quantities=form.quantities(), request=request)
                messages.success(request, 'Parcel changed. Confirm & lock it again when it is ready for shipping.')
                return redirect('portal:ops-shipment-detail', pk=pk)
        except ValidationError as error:
            _errors(request, form, error)
        return self.display(request, shipment, status=400, change_form=form)


def _week_redirect(request, stage):
    query = QueryDict(request.POST.get('list_query', '')).copy()
    query['week'] = request.POST.get('week', '')
    query['shipping_step'] = stage
    query.pop('page', None)
    return redirect(_shipping_list_url(query.urlencode()))


class ShippingLockView(OperationsView):
    """Check and lock week; "Hold them and lock the rest" sends hold_failing."""
    shipping = True

    http_method_names = ('post',)

    def post(self, request):
        require_operations_actor(self.company, request.user, shipping=True)
        form = ShippingFilterForm({'week': request.POST.get('week')})
        try:
            validate_workflow_context(request, self.company, 'shipping-week')
            if not form.is_valid():
                raise ValidationError('Choose a valid shipping week.')
            result = lock_week(company=self.company, actor=request.user, week_start=form.cleaned_data['week'],
                               hold_failing=request.POST.get('hold_failing') == '1', request=request)
            locked, held = len(result['locked']), len(result['held'])
            message = f'{locked} ready parcel{pluralize(locked)} checked and locked. Nothing was dispatched.'
            if held:
                message += f' {held} held with the reason beside {"it" if held == 1 else "them"}.'
            from care.inventory import stock_rows
            shortages = [f"{row['product']}: {row['shortage']} short" for row in stock_rows(self.company) if row['shortage']]
            if shortages:
                message += ' Stock reserved despite shortages: ' + '; '.join(shortages) + '.'
            (messages.warning if held or shortages else messages.success)(request, message)
        except ValidationError as error:
            problems = error.messages
            messages.error(request, 'The week isn’t locked. Fix these, or hold them and lock the rest: ' + ' · '.join(problems)
                           if len(problems) > 1 or ':' in problems[0] else problems[0])
        return _week_redirect(request, 'pending')


class ShippingCourierView(OperationsView):
    """Book The Courier Guy for the week's locked parcels: one shipment, and so one waybill, each."""
    shipping = True

    http_method_names = ('post',)

    def post(self, request):
        from care.courier import book_waybills

        require_operations_actor(self.company, request.user, shipping=True)
        form = ShippingFilterForm({'week': request.POST.get('week')})
        try:
            validate_workflow_context(request, self.company, 'shipping-courier')
            if not form.is_valid():
                raise ValidationError('Choose a valid shipping week.')
            demo_only = request.POST.get('demo_only') == '1'
            booking_options = {'demo_only': True} if demo_only else {}
            booked, failed = book_waybills(company=self.company, actor=request.user, week_start=form.cleaned_data['week'],
                                           request=request, **booking_options)
            simulated = sum(bool(parcel.patient.demo_data_set_id) for parcel in booked)
            if demo_only:
                message = f'{len(booked)} demo waybill{pluralize(len(booked))} created. No courier was contacted.'
            elif simulated:
                message = f'{len(booked)} waybill{pluralize(len(booked))} created, including {simulated} simulated demo booking{pluralize(simulated)}.'
            else:
                message = f'{len(booked)} waybill{pluralize(len(booked))} booked with The Courier Guy for collection.'
            if failed:
                message += f' {len(failed)} couldn’t be booked: see the reason beside {"it" if len(failed) == 1 else "them"}.'
            (messages.warning if failed else messages.success)(request, message)
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
        return _week_redirect(request, 'confirmed')


class ShippingCollectView(OperationsView):
    """Record the collection: the week's locked parcels with a waybill are recorded as dispatched, each with its
    waybill as tracking and the batch noted for each medicine."""
    shipping = True

    http_method_names = ('post',)

    @transaction.atomic
    def post(self, request):
        require_operations_actor(self.company, request.user, shipping=True)
        form = ShippingFilterForm({'week': request.POST.get('week')})
        try:
            validate_workflow_context(request, self.company, 'shipping-collect')
            if not form.is_valid():
                raise ValidationError('Choose a valid shipping week.')
            if request.POST.get('confirm') != 'on':
                raise ValidationError('Tick to confirm the courier has collected these parcels.')
            notes = {}
            for product in MedicationProduct.objects.for_company(self.company):
                note = ' '.join(request.POST.get(f'batch_{product.pk}', '').split())
                if len(note) > 120:
                    raise ValidationError('Keep each batch note to 120 characters or fewer.')
                if note:
                    notes[product.pk] = note
            demo_only = request.POST.get('demo_only') == '1'
            dispatch_options = {'demo_only': True} if demo_only else {}
            dispatched, failed = dispatch_week(company=self.company, actor=request.user,
                                               week_start=form.cleaned_data['week'], batch_notes=notes,
                                               stock_override=request.POST.get('stock_override') == 'on',
                                               stock_override_reason=request.POST.get('stock_override_reason', ''), request=request,
                                               **dispatch_options)
            message = f'{len(dispatched)} {"demo " if demo_only else ""}parcel{pluralize(len(dispatched))} recorded as collected.'
            if failed:
                message += ' Not recorded: ' + ' · '.join(f'{parcel.patient}: {reason}' for parcel, reason in failed)
            (messages.warning if failed else messages.success)(request, message)
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
        return _week_redirect(request, 'tracking')


class ShipmentLabelView(OperationsView):
    """The parcel's waybill label from The Courier Guy (a link that only lasts a while, so it is fetched each time)."""
    shipping = True

    http_method_names = ('get',)

    def get(self, request, pk):
        from care.courier import label_url

        require_operations_actor(self.company, request.user, shipping=True)
        shipment = get_object_or_404(self.shipments(), pk=pk)
        try:
            return redirect(label_url(shipment))
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
            monday = shipment.scheduled_for - timedelta(days=(shipment.scheduled_for.weekday() - 1) % 7)
            return redirect(f'{reverse("portal:ops-shipping")}?{urlencode({"week": monday.isoformat()})}')


@method_decorator(csrf_exempt, name='dispatch')
class CourierWebhookView(View):
    """The Courier Guy's tracking webhook: each parcel's courier status, and delivery. Its address carries a secret
    token (TCG_WEBHOOK_TOKEN), as The Courier Guy doesn't sign webhooks; anything else is refused."""

    http_method_names = ('post',)

    def post(self, request, token):
        import json

        from care.courier import apply_tracking, validate_tracking_event, webhook_token_ok

        if not webhook_token_ok(token):
            return HttpResponse(status=404)
        try:
            payload = json.loads(request.body)
        except ValueError:
            return HttpResponse(status=400)
        events = payload if isinstance(payload, list) else [payload]
        try:
            for event in events:
                validate_tracking_event(event)
        except ValidationError:
            return HttpResponse(status=400)
        with transaction.atomic():
            matched = sum(1 for event in events if apply_tracking(event) is not None)
        return HttpResponse(f'{matched}', content_type='text/plain')


def _safe_next(request, default):
    """Back to the page the form was opened from, when that is a page of this app."""
    target = request.POST.get('next') or request.GET.get('next') or ''
    return target if target.startswith('/') and not target.startswith('//') else default


class DeliveryAddressView(OperationsView):
    """A patient's delivery address and contact number, changed by the practice for them (from Weekly shipping).
    Practice administrators can't open patient records, so this is a page of its own."""
    shipping = True

    nav_section, page_title = 'shipping', 'Delivery address'

    def patient(self, pk):
        from practices.models import Patient

        return get_object_or_404(Patient.objects.filter(company=self.company, is_active=True), pk=pk)

    def display(self, request, patient, form, status=200):
        from care.addresses import address_for

        return render(request, 'portal/operations_delivery_address.html', self.context(
            patient=patient, form=form, address=address_for(patient), next_url=_safe_next(request, reverse('portal:ops-shipping'))),
            status=status)

    def get(self, request, pk):
        from care.addresses import address_for

        patient = self.patient(pk)
        require_operations_actor(self.company, request.user, shipping=True)
        return self.display(request, patient, DeliveryAddressForm(instance_address=address_for(patient), patient=patient))

    def post(self, request, pk):
        from care.addresses import save_delivery_address

        patient = self.patient(pk)
        require_operations_actor(self.company, request.user, shipping=True)
        form = DeliveryAddressForm(request.POST, patient=patient)
        if form.is_valid():
            try:
                _, changed = save_delivery_address(company=self.company, patient=patient, actor=request.user,
                                                   data=form.cleaned_data, request=request)
                messages.success(request, f'Delivery address saved for {patient}.' if changed else 'Nothing changed.')
                return redirect(_safe_next(request, reverse('portal:ops-shipping')))
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
        return self.display(request, patient, form, status=400)


def _history_filter(request, queryset):
    form = HistoryFilterForm(request.GET)
    if form.is_valid():
        start, end = form.cleaned_data.get('start_month'), form.cleaned_data.get('end_month')
        if start or end:
            queryset = queryset.alias(history_dispatch_date=TruncDate('dispatched_at', tzinfo=ZoneInfo('Africa/Johannesburg')))
        if start:
            queryset = queryset.filter(history_dispatch_date__gte=start)
        if end:
            queryset = queryset.filter(history_dispatch_date__lte=end.replace(day=monthrange(end.year, end.month)[1]))
        queryset = _shipment_search(queryset, form.cleaned_data['q'])
    else:
        queryset = queryset.none()
    return form, queryset


class DispatchHistoryView(OperationsView):
    """A paginated dispatch list, with an optional inclusive month range."""
    shipping = True

    nav_section, page_title = 'history', 'Dispatch history'
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        from .pagination import numbered_pagination
        from .dispatch_history import history_queryset, prepare_history_shipments

        form, queryset = _history_filter(request, history_queryset(self.shipments()))
        context = self.context(filter_form=form, **_paginate(request, queryset, per_page=25))
        context['shipments'] = prepare_history_shipments(context['page_obj'].object_list, self.company)
        count = context['page_obj'].paginator.count
        dated = int(bool(form['start_month'].value() or form['end_month'].value()))
        tools = _list_tools(request, form, url_name='portal:ops-history', label='Filter dispatches',
                            sections=[fields_section('dispatched', 'Dispatch month',
                                [form['start_month'], form['end_month']], count=dated)],
                            placeholder='Patient or waybill', count=count, noun='shipment')
        filters = {name: request.GET[name] for name in ('start_month', 'end_month', 'q') if request.GET.get(name)}
        query = urlencode(filters)
        context.update(tools, export_query=query, pagination_query=query,
                       **numbered_pagination(context['page_obj'], base_url=reverse('portal:ops-history'), query=filters))
        return render(request, 'portal/operations_history.html', context)


class DispatchHistoryExportView(OperationsView):
    shipping = True
    nav_section, page_title = 'history', 'Dispatch history'
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        from care.dispatch_export import dispatch_history_workbook

        form, queryset = _history_filter(request, self.shipments().filter(
            status__in=('dispatched', 'delivered')).order_by('-dispatched_at', '-pk'))
        if not form.is_valid():
            return HttpResponse('Correct the export month filters.', status=400, content_type='text/plain')
        if queryset.count() > 10000:
            return HttpResponse('Narrow the export months to at most 10,000 shipments.', status=400, content_type='text/plain')
        row_count = 0

        def rows():
            nonlocal row_count
            for row in history_export_rows(queryset, self.company):
                row_count += 1
                yield row

        output = dispatch_history_workbook(rows())
        start, end = form.cleaned_data.get('start_month'), form.cleaned_data.get('end_month')
        filename = 'dispatch-history.xlsx'
        if start or end:
            filename = f'dispatch-history-{start:%Y-%m}-to-{end:%Y-%m}.xlsx' if start and end else (
                f'dispatch-history-{start:%Y-%m}-to-latest.xlsx' if start else f'dispatch-history-start-to-{end:%Y-%m}.xlsx')
        try:
            if request.method != 'HEAD':
                record_audit(company=self.company, actor=request.user, action='dispatch.history_exported',
                    request=request, metadata={'rows': row_count})
            return FileResponse(output, as_attachment=True, filename=filename,
                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        except Exception:
            output.close()
            raise


def history_export_rows(queryset, company):
    """One row per dispatched product, using its saved snapshot before current records."""
    from care.models import DeliveryAddress

    addresses = {address.patient_id: address for address in DeliveryAddress.objects.filter(
        company=company, patient__in=queryset.values('patient'))}
    for shipment in queryset.iterator(chunk_size=500):
        snapshot = shipment.dispatch_snapshot or {}
        sent_to = dispatch_delivery(shipment, addresses.get(shipment.patient_id))
        deliver_to, phone = ', '.join(sent_to.get('lines', ())), sent_to.get('phone', '')
        doctor = shipment_doctor(shipment)
        items = snapshot.get('items') or [dict(product=str(item.product), dose=item.dose,
            quantity=item.quantity, batch_note=item.batch_note) for item in shipment.items.all()]
        for item in items:
            yield (shipment.pk, shipment.scheduled_for, shipment.dispatched_at,
                snapshot.get('company_name', company.name), snapshot.get('patient_name', str(shipment.patient)),
                snapshot.get('patient_id_number', shipment.patient.id_number), doctor.titled_name if doctor else '',
                item.get('product', ''), item.get('dose', ''), item.get('quantity'), batch_text(item),
                shipment.get_status_display(), shipment.tracking_number, shipment.hold_reason, shipment.note,
                deliver_to, phone, shipment.waybill or snapshot.get('waybill', ''))


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


def batch_text(item):
    """A dispatched item's batch note. Parcels dispatched from stock saved the batch and its expiry instead."""
    if item.get('batch_note'):
        return item['batch_note']
    return ', expiry '.join(str(part) for part in (item.get('batch'), item.get('expires_on')) if part)


class ManifestDownloadView(OperationsView):
    shipping = True
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        form, queryset = _shipping_filter(request, self.shipments(), shipping_week(self.company), company=self.company)
        if not form.is_valid():
            return HttpResponse('Correct the export date filters.', status=400)
        if queryset.count() > 10000:
            return HttpResponse('Narrow the export dates to at most 10,000 shipments.', status=400)
        from care.billing import parcel_payment
        from care.courier import booked_delivery
        from care.models import DeliveryAddress

        addresses = {address.patient_id: address for address in DeliveryAddress.objects.filter(
            company=self.company, patient__in=queryset.values('patient'))}
        rows = []
        for shipment in queryset.order_by('scheduled_for', 'pk'):
            snapshot = shipment.dispatch_snapshot or {}
            # Where it went: the address saved at dispatch, else the one it was booked with, else the patient's now.
            sent_to = snapshot.get('delivery_address') or booked_delivery(shipment)
            address = addresses.get(shipment.patient_id)
            if sent_to.get('lines'):
                deliver_to, phone = ', '.join(sent_to['lines']), sent_to.get('phone', '')
            elif address is not None:
                deliver_to, phone = ', '.join(address.lines()), address.phone
            else:
                deliver_to, phone = '', ''
            paid = parcel_payment(shipment)[1]
            waybill = shipment.waybill or snapshot.get('waybill', '')
            patient_name = snapshot.get('patient_name', str(shipment.patient))
            id_number = snapshot.get('patient_id_number', shipment.patient.id_number)
            doctor = shipment_doctor(shipment)
            items = snapshot.get('items') or [dict(product=str(item.product), dose=item.dose, quantity=item.quantity,
                                                       batch_note=item.batch_note) for item in shipment.items.all()]
            for item in items:
                rows.append((shipment.pk, shipment.scheduled_for, snapshot.get('company_name', self.company.name), patient_name,
                             id_number, doctor.titled_name if doctor else '', item['product'], item['dose'], item['quantity'],
                             batch_text(item), shipment.get_status_display(), shipment.tracking_number, shipment.hold_reason, shipment.note,
                             deliver_to, phone, paid, waybill))
        if request.method != 'HEAD':
            record_audit(company=self.company, actor=request.user, action='shipping.manifest_exported',
                         request=request, metadata={'rows': len(rows)})
        return _csv_response('shipping-manifest.csv',
                             ('Shipment', 'Planned date', 'Practice', 'Patient', 'ID / passport', 'Doctor', 'Product', 'Dose', 'Quantity',
                              'Batch and expiry', 'Status', 'Tracking', 'Hold reason', 'Note', 'Deliver to', 'Contact number',
                              'Paid', 'Waybill'), rows)


class StaffOrderListView(OperationsView):
    nav_section, page_title = 'orders', 'Non-prescription requests'
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        form = OrderFilterForm(without_all(request.GET, 'status'))
        queryset = self.orders()
        if form.is_valid():
            if form.cleaned_data['status']:
                queryset = queryset.filter(status__in=form.cleaned_data['status'])
            queryset = _search(queryset, form.cleaned_data['q'], 'patient__first_name', 'patient__last_name')
        else:
            queryset = queryset.none()
        context = self.context(filter_form=form, **_paginate(request, queryset))
        context['orders'] = list(context['page_obj'].object_list)
        context.update(_list_tools(request, form, url_name='portal:ops-orders', label='Filter non-prescription requests',
                                   sections=ticked_sections(form, request.GET, ('status',)), placeholder='Patient name',
                                   count=context['page_obj'].paginator.count, noun='request'))
        return render(request, 'portal/operations_orders.html', context)


def _order_items(order):
    items = list(order.items.select_related('product'))
    for item in items:
        item.line_total = item.quantity * item.unit_price
    return items


class StaffOrderDetailView(OperationsView):
    nav_section, page_title = 'orders', 'Non-prescription request'

    def display(self, request, order, form=None, status=200):
        from .export_support import export_url

        context = self.context(order=order, items=_order_items(order), form=form if form is not None else OrderReviewForm(),
                               workflow_context=self.token('supply-request', order))
        context['request_items_export_url'] = export_url('request-items', pk=order.pk)
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
                    messages.success(request, 'Request accepted. Its parcel is drafted on Weekly shipping.')
                    return redirect('portal:ops-shipment-detail', pk=shipment.pk)
                messages.success(request, 'Unaccepted request cancelled.')
                return redirect('portal:ops-order-detail', pk=pk)
        except ValidationError as error:
            _errors(request, form, error)
        return self.display(request, order, form, status=400)


def _shop_url():
    return f"{reverse('portal:patient-treatment')}#once-off-items"


class PatientShopBase(LoginRequiredMixin, PatientPortalRequiredMixin, View):
    """A patient's once-off orders (care.pharmacy): the basket, and the orders placed from it."""

    http_method_names = ('get', 'post', 'head', 'options')

    def orders(self):
        return PharmacyOrder.objects.filter(company=self.patient_company, patient=self.patient).select_related('shipment')

    def context(self, title, **kwargs):
        return dict(patient_page_context(self.request, self.patient_company, self.patient, 'orders', title), **kwargs)

    def token(self, kind, record):
        return make_workflow_context(self.request, self.patient_company, kind, record, patient=self.patient)


class PatientPharmacyView(PatientShopBase):
    """The old My Medications address: once-off items are on My Treatment now."""

    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        return redirect(_shop_url())


class PatientBasketItemView(PatientShopBase):
    """Add a once-off item to the basket or change how many, from My Treatment; 0 removes it, from the basket."""

    http_method_names = ('post',)

    @transaction.atomic
    def post(self, request, pk):
        self.patient_company = Company.objects.select_for_update().get(pk=self.patient_company.pk)
        product = get_object_or_404(MedicationProduct.objects.for_company(self.patient_company), pk=pk)
        form = BasketQuantityForm(request.POST)
        back = 'portal:patient-basket' if request.POST.get('next') == 'basket' else None
        try:
            validate_workflow_context(request, self.patient_company, 'basket-product', product, patient=self.patient)
            if not form.is_valid():
                raise ValidationError('Choose a quantity from 0 to 100.')
            quantity = form.cleaned_data['quantity']
            # Prescription medicine comes with the subscription; an old basket can still remove it.
            if quantity and product.requires_authorisation:
                raise ValidationError('This item needs a prescription and comes with your subscription.')
            set_basket_quantity(company=self.patient_company, patient=self.patient, actor=request.user, product=product,
                                quantity=quantity, request=request)
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
            return redirect(back) if back else redirect(_shop_url())
        messages.success(request, 'Removed from your basket.' if not quantity else 'Basket updated.')
        return redirect('portal:patient-basket')


class PatientBasketView(PatientShopBase):
    """The basket: its items, where they're delivered, and Pay by card, which places the order."""

    http_method_names = ('get', 'head', 'options')

    def display(self, request, order, form=None, status=200):
        from care.addresses import address_for
        from care.payment_providers import current_provider

        items = _order_items(order) if order else []
        for item in items:
            item.remove_context = self.token('basket-product', item.product)
        return render(request, 'portal/pharmacy_basket.html', self.context(
            'Basket', order=order, items=items, address=address_for(self.patient),
            needs_prescription=[item for item in items if item.product.requires_authorisation],
            pays_by_card=current_provider() is not None, form=form if form is not None else OrderForm(),
            workflow_context=(request.POST.get('workflow_context', '') if request.method == 'POST'
                              else self.token('patient-order', order) if order else ''),
        ), status=status)

    def get(self, request):
        return self.display(request, self.orders().filter(status=PharmacyOrder.Status.DRAFT).first())


class PatientOrderListView(PatientShopBase):
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        context = self.context('My orders', **_paginate(request, self.orders().exclude(status=PharmacyOrder.Status.DRAFT)))
        context['orders'] = list(context['page_obj'].object_list)
        return render(request, 'portal/pharmacy_orders.html', context)


class PatientOrderDetailView(PatientShopBase):
    """One order: paying for it, how it's going and cancelling it before it's accepted. The basket places one here."""

    def display(self, request, order, form=None, status=200):
        from care.payment_providers import current_provider

        payable = (order.status in (PharmacyOrder.Status.SUBMITTED, PharmacyOrder.Status.ACCEPTED)
                   and not order.paid_at and order.subtotal > 0 and current_provider() is not None)
        items = [item for item in _order_items(order) if item.company_id == self.patient_company.pk]
        shipment = order.shipment if order.shipment_id else None
        if shipment and (shipment.company_id != self.patient_company.pk or shipment.patient_id != self.patient.pk):
            shipment = None
        return render(request, 'portal/pharmacy_order_detail.html', self.context(
            f'Order {order.pk}', order=order, items=items, shipment=shipment, payable=payable,
            can_cancel=order.status == PharmacyOrder.Status.SUBMITTED,
            cancel_form=form if form is not None else ConfirmForm(),
            pay_context=make_workflow_context(request, self.patient_company, 'order-pay', order, patient=self.patient) if payable else '',
            workflow_context=(request.POST.get('workflow_context', '') if request.method == 'POST'
                              else make_workflow_context(request, self.patient_company, 'patient-order', order, patient=self.patient)),
        ), status=status)

    def get(self, request, pk):
        order = get_object_or_404(self.orders(), pk=pk)
        if order.status == PharmacyOrder.Status.DRAFT:
            return redirect('portal:patient-basket')
        return self.display(request, order)

    def post(self, request, pk):
        if request.POST.get('action') == 'submit':
            return self.place(request, pk)
        return self.cancel(request, pk)

    def place(self, request, pk):
        """Place the basket as an order to the delivery address on file, then pay for it by card."""
        from care.addresses import address_for
        from care.billing import start_order_payment
        from care.payment_providers import current_provider

        from .patient_shop import order_address

        form = OrderForm(request.POST)
        try:
            with transaction.atomic():
                self.patient_company = Company.objects.select_for_update().get(pk=self.patient_company.pk)
                order = get_object_or_404(self.orders().select_for_update(of=('self',)), pk=pk)
                if order.status != PharmacyOrder.Status.DRAFT:
                    # Already placed, from this page a moment ago or another tab.
                    return redirect('portal:patient-order-detail', pk=order.pk)
                validate_workflow_context(request, self.patient_company, 'patient-order', order, patient=self.patient)
                address = address_for(self.patient)
                if address is None:
                    raise ValidationError('Add your delivery address before you order.')
                prescribed = [item.product_name for item in order.items.select_related('product') if item.product.requires_authorisation]
                if prescribed:
                    raise ValidationError(f'Remove {", ".join(prescribed)} first: it comes with your subscription.')
                if not form.is_valid():
                    raise ValidationError('Check your note for the practice.')
                order = submit_basket(order=order, actor=request.user, delivery_address=order_address(address),
                                      note=form.cleaned_data['note'], confirm=True, expected_revision=order.revision,
                                      request=request)
        except ValidationError as error:
            _errors(request, form, error)
            return PatientBasketView.display(self, request, get_object_or_404(self.orders(), pk=pk), form, status=400)
        if current_provider() is None:
            messages.success(request, 'Order placed. The practice will tell you how to pay for it.')
            return redirect('portal:patient-order-detail', pk=order.pk)
        try:
            _, url = start_order_payment(order=order, actor=request.user, request=request)
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
            return redirect('portal:patient-order-detail', pk=order.pk)
        return redirect(url)

    @transaction.atomic
    def cancel(self, request, pk):
        self.patient_company = Company.objects.select_for_update().get(pk=self.patient_company.pk)
        order = get_object_or_404(self.orders().select_for_update(of=('self',)), pk=pk)
        form = ConfirmForm(request.POST)
        valid = form.is_valid()
        try:
            validate_workflow_context(request, self.patient_company, 'patient-order', order, patient=self.patient)
            if request.POST.get('action') != 'cancel':
                raise ValidationError('Choose cancel order.')
            if valid:
                cancel_order(order=order, actor=request.user, confirm=True, request=request)
                messages.success(request, 'Order cancelled.')
                return redirect('portal:patient-order-detail', pk=pk)
        except ValidationError as error:
            _errors(request, form, error)
        return self.display(request, order, form, status=400)
