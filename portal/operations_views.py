"""Separate practice operations and patient pharmacy pages."""

import csv
from datetime import date, timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import pluralize
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt

from care.models import MedicationDose, MedicationProduct, PharmacyOrder, Shipment, ShipmentItem, TreatmentAuthorization
from care.operations import (
    AVERAGE_WEEKS, UNDISPATCHED, can_ship, change_parcel, dispatch_shipment, dispatch_week, hold_or_cancel_shipment, lock_reasons,
    lock_week, mark_delivered, prepare_shipment, prepare_week, product_allowance, require_operations_actor,
    set_shipment_note, shipment_hold_reason, week_order, week_stock,
)
from care.pharmacy import accept_order, cancel_order, set_basket_quantity, submit_basket
from care.services import record_audit
from care.treatment import dose_in_effect
from care.shipment_intake import create_manual_shipment
from practices.models import Company, CompanyMembership

from .filter_menus import fields_section, list_tools, ticked_sections, without_all
from .operations_forms import (
    BasketQuantityForm, CatalogueFilterForm, ConfirmForm, DeliveryAddressForm, DoseForm, HistoryFilterForm, OrderFilterForm,
    OrderReviewForm, ProductForm, ShipmentActionForm, ShipmentChangeForm, ShipmentCreateForm, ShippingFilterForm,
    SupplyRequestForm,
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
    return _search(queryset, query, 'patient__first_name', 'patient__last_name', 'tracking_number')


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


def _shipping_filter(request, queryset, default_week=None):
    # A shipping week runs Monday to Sunday: any day in ?week= means its week. One that isn't a date is reported.
    data = _form_data(request)
    try:
        day = date.fromisoformat(request.GET['week'][:10]) if request.GET.get('week') else (default_week or this_week())
        data['week'] = (day - timedelta(days=day.weekday())).isoformat()
    except ValueError:
        pass
    form = ShippingFilterForm(without_all(data, 'status'))
    if form.is_valid():
        week = form.cleaned_data['week']
        queryset = queryset.filter(scheduled_for__range=(week, week + timedelta(days=6)))
        if form.cleaned_data['status']:
            queryset = queryset.filter(status__in=form.cleaned_data['status'])
        queryset = _shipment_search(queryset, form.cleaned_data['q'])
    else:
        queryset = queryset.none()
    return form, queryset


def this_week():
    today = timezone.localdate()
    return today - timedelta(days=today.weekday())


def chosen_week(request, default=None):
    """The Monday of the week in ?week=, or of the default week (this week unless given)."""
    try:
        day = date.fromisoformat(request.GET.get('week', '')[:10])
    except ValueError:
        return default or this_week()
    return day - timedelta(days=day.weekday())


def last_week():
    return this_week() - timedelta(days=7)


def shipping_week(company):
    """The week Weekly shipping opens on: the one with the next shipping day (today's, on the day itself). After the
    courier's collection, that is next week's list, which is the one being set up."""
    from care.parcels import shipping_day_from, shipping_weekday

    day = shipping_day_from(timezone.localdate(), shipping_weekday(company))
    return day - timedelta(days=day.weekday())


def week_navigation(url_name, week, *, home=None, **query):
    """Week 43 · 19 Oct 2026 – 25 Oct 2026 with the weeks either side, as Calendar & Bookings' Week view, and a link
    back to the page's own week (home: this week unless given)."""
    link = lambda monday: f'{reverse(url_name)}?{urlencode({**query, "week": monday.isoformat()})}'
    home = home or this_week()
    return dict(week_start=week, week_end=week + timedelta(days=6), week_number=week.isocalendar().week,
                prev_week_url=link(week - timedelta(days=7)), next_week_url=link(week + timedelta(days=7)),
                this_week_url=link(home) if week != home else None,
                this_week_label='This week' if home == this_week() else 'Next week')


def shipment_doctor(shipment):
    """Who prescribed what's in the parcel, or else the patient's prescribing doctor."""
    authorization = shipment.authorization
    return authorization.prescribed_by if authorization and authorization.prescribed_by_id else shipment.patient.assigned_doctor


class ShippingListView(OperationsView):
    """Weekly shipping, one shipping week at a time: its parcels (drafted from prescriptions, or added by hand with
    Create shipment), where each goes and whether it's paid for, the steps that get them out (mark ready, check and
    lock, order the pens, book The Courier Guy, record the collection) and the week's pens. Dispatch stock was a page
    of its own until 24 Oct 2026."""
    shipping = True

    nav_section, page_title = 'shipping', 'Weekly shipping'
    http_method_names = ('get', 'head', 'options')
    per_page = 100

    def display(self, request, create_form=None, status=200):
        from care.billing import parcel_payment, practice_settings
        from care.courier import collection_problem, courier_enabled
        from care.models import DeliveryAddress
        from care.operations import delivery_problem

        home = shipping_week(self.company)
        week = chosen_week(request, home)
        form, queryset = _shipping_filter(request, self.shipments(), home)
        queryset = queryset.order_by('scheduled_for', 'patient__last_name', 'pk')
        fresh = lambda kind: make_workflow_context(request, self.company, kind)
        context = self.context(filter_form=form, lock_context=fresh('shipping-week'), **_paginate(request, queryset, self.per_page),
                               **week_navigation('portal:ops-shipping', week, home=home,
                                                 **{key: value for key, value in request.GET.items() if key in ('status', 'q')}))
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
            shipment.payment_state, shipment.payment_text = parcel_payment(shipment)
            # What Check and lock week would hold it back for, besides its prescription; payment is in its own column.
            shipment.lock_problems = [delivery_problem(shipment)] if open_parcel and delivery_problem(shipment) else []
            if self.can_edit and shipment.status not in (Shipment.Status.CANCELLED, Shipment.Status.DELIVERED):
                shipment.address_url = f'{reverse("portal:ops-delivery-address", args=[shipment.patient_id])}?{urlencode({"next": here})}'
            # Mark ready on its line, for a draft or held parcel.
            if self.can_edit and shipment.status in (Shipment.Status.DRAFT, Shipment.Status.HELD):
                shipment.ready_context = make_workflow_context(request, self.company, 'shipment-ready', shipment)
            # Its note, in the Notes column, until it's delivered or cancelled.
            if self.can_edit and shipment.status not in (Shipment.Status.CANCELLED, Shipment.Status.DELIVERED):
                shipment.note_context = make_workflow_context(request, self.company, 'shipment-note', shipment)
        context['list_query'] = request.GET.urlencode()
        tools = _list_tools(request, form, url_name='portal:ops-shipping', label='Filter shipments',
                            sections=ticked_sections(form, request.GET, ('status',)), placeholder='Patient or tracking',
                            count=context['page_obj'].paginator.count, noun='shipment')
        # The search and the Filters keep the week; clearing either stays on it.
        kept = {'week': week.isoformat()}
        tools['search_hidden'] = {**tools['search_hidden'], **kept}
        tools['search_clear_url'] = f"{reverse('portal:ops-shipping')}?{urlencode({**{k: v for k, v in tools['search_hidden'].items() if k != 'q'}})}"
        tools['filter_menu'].update(hidden=[*tools['filter_menu']['hidden'], ('week', week.isoformat())],
                                    clear_url=f"{reverse('portal:ops-shipping')}?{urlencode({**kept, 'q': tools['search']})}")
        context.update(tools)
        # The week's steps and pens, from what was Dispatch stock.
        order = week_order(self.company, week)
        week_parcels = Shipment.objects.filter(company=self.company, patient__company=self.company,
                                               scheduled_for__range=(week, week + timedelta(days=6)))
        locked = week_parcels.filter(status=Shipment.Status.READY, locked_at__isnull=False)
        context.update(
            week_order=order, stock_rows=week_stock(self.company, week), average_weeks=AVERAGE_WEEKS,
            show_stock_detail=self.membership.role == CompanyMembership.Role.SUPER_ADMIN,
            ready_unlocked=order['parcels'] - order['locked'],
            lock_blocked=sum(1 for shipment in week_parcels.filter(status=Shipment.Status.READY, locked_at__isnull=True)
                             if lock_reasons(shipment)),
            courier_enabled=courier_enabled(), courier_problem=collection_problem(self.company) if courier_enabled() else '',
            to_book=locked.filter(waybill='').count(), to_collect=locked.exclude(waybill='').count(),
            payment_required=payment_required,
            order_csv_url=f'{reverse("portal:ops-week-order")}?{urlencode({"week": week.isoformat()})}',
            manifest_url=f'{reverse("portal:ops-manifest")}?{urlencode({"week": week.isoformat()})}',
        )
        if self.can_edit:
            # A dialog reopened after a mistake keeps the token it was sent with.
            context.update(create_form=create_form or ShipmentCreateForm(company=self.company),
                           create_context=self.token('shipment-create') if create_form else fresh('shipment-create'),
                           prepare_context=fresh('shipping-prepare'), courier_context=fresh('shipping-courier'),
                           collect_context=fresh('shipping-collect'),
                           collect_products=MedicationProduct.objects.filter(
                               pk__in=ShipmentItem.objects.filter(shipment__in=locked.exclude(waybill='')).values('product')),
                           create_open=create_form is not None or request.GET.get('create') == '1')
        return render(request, 'portal/operations_shipping.html', context, status=status)

    def get(self, request):
        return self.display(request)


def _shipping_list_url(query):
    """Back to the Weekly shipping list as it was: its week, status and page only."""
    kept = {key: value for key, value in QueryDict(query or '').items() if key in ('week', 'status', 'page')}
    return f'{reverse("portal:ops-shipping")}?{urlencode(kept)}' if kept else reverse('portal:ops-shipping')


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


class DispatchStockView(OperationsView):
    """Dispatch stock is part of Weekly shipping now: its old address opens that page on the same week."""
    shipping = True

    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        week = chosen_week(request, shipping_week(self.company))
        return redirect(f'{reverse("portal:ops-shipping")}?{urlencode({"week": week.isoformat()})}#week-pens')


class ShippingPrepareView(OperationsView):
    """Mark drafts ready: every draft parcel of the week that passes its checks."""
    shipping = True

    http_method_names = ('post',)

    def post(self, request):
        require_operations_actor(self.company, request.user, shipping=True)
        form = ShippingFilterForm({'week': request.POST.get('week')})
        try:
            validate_workflow_context(request, self.company, 'shipping-prepare')
            if not form.is_valid():
                raise ValidationError('Choose a valid shipping week.')
            ready, left = prepare_week(company=self.company, actor=request.user, week_start=form.cleaned_data['week'], request=request)
            message = f'{len(ready)} draft parcel{"" if len(ready) == 1 else "s"} marked ready.'
            if left:
                message += f' {len(left)} could not be: see the hold reason beside {"it" if len(left) == 1 else "them"}.'
            (messages.warning if left else messages.success)(request, message)
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
        return redirect(f'{reverse("portal:ops-shipping")}?{urlencode({"week": request.POST.get("week", "")})}')


class WeekOrderDownloadView(OperationsView):
    """The week's pens to order, as a CSV for the supplier order."""
    shipping = True

    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        form, _ = _shipping_filter(request, self.shipments(), shipping_week(self.company))
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
                messages.success(request, f'Draft parcel added for {shipment.patient} on {day:%-d %b %Y}. Mark it ready to include it in the week’s order.')
                return redirect(f'{reverse("portal:ops-shipping")}?{urlencode({"week": (day - timedelta(days=day.weekday())).isoformat()})}')
        except ValidationError as error:
            for message in error.messages:
                form.add_error(None, message)
        return self.display(request, create_form=form, status=400)


class ShipmentDetailView(OperationsView):
    shipping = True
    nav_section, page_title = 'shipping', 'Shipment'

    def display(self, request, shipment, form=None, status=200, change_form=None):
        from .patient_action_context import patient_action_context

        items = list(shipment.items.select_related('product'))
        # Each form keeps the token it was sent with after a mistake; the other gets a current one.
        changing = change_form is not None
        context = self.context(shipment=shipment, items=items, change_form=None,
                               form=form if form is not None else ShipmentActionForm(items=self.batch_items(shipment, items)),
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
        form = ShipmentActionForm(request.POST, items=self.batch_items(shipment, list(shipment.items.select_related('product'))))
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
                    dispatch_shipment(shipment=shipment, actor=request.user, tracking_number=form.cleaned_data['tracking_number'],
                                      confirm=True, batch_notes=form.batch_notes(), request=request)
                elif action == 'deliver':
                    mark_delivered(shipment=shipment, actor=request.user, confirm=True, request=request)
                messages.success(request, 'Shipment action recorded. No courier API was called.')
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
                messages.success(request, 'Parcel changed. Mark it ready again to include it in the week’s order.')
                return redirect('portal:ops-shipment-detail', pk=pk)
        except ValidationError as error:
            _errors(request, form, error)
        return self.display(request, shipment, status=400, change_form=form)


def _week_redirect(request):
    return redirect(f'{reverse("portal:ops-shipping")}?{urlencode({"week": request.POST.get("week", "")})}')


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
            (messages.warning if held else messages.success)(request, message)
        except ValidationError as error:
            problems = error.messages
            messages.error(request, 'The week isn’t locked. Fix these, or hold them and lock the rest: ' + ' · '.join(problems)
                           if len(problems) > 1 or ':' in problems[0] else problems[0])
        return _week_redirect(request)


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
            booked, failed = book_waybills(company=self.company, actor=request.user, week_start=form.cleaned_data['week'],
                                           request=request)
            message = f'{len(booked)} waybill{pluralize(len(booked))} booked with The Courier Guy for collection.'
            if failed:
                message += f' {len(failed)} couldn’t be booked: see the reason beside {"it" if len(failed) == 1 else "them"}.'
            (messages.warning if failed else messages.success)(request, message)
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
        return _week_redirect(request)


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
            dispatched, failed = dispatch_week(company=self.company, actor=request.user,
                                               week_start=form.cleaned_data['week'], batch_notes=notes, request=request)
            message = f'{len(dispatched)} parcel{pluralize(len(dispatched))} recorded as collected.'
            if failed:
                message += ' Not recorded: ' + ' · '.join(f'{parcel.patient}: {reason}' for parcel, reason in failed)
            (messages.warning if failed else messages.success)(request, message)
        except ValidationError as error:
            messages.error(request, ' '.join(error.messages))
        return _week_redirect(request)


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
            monday = shipment.scheduled_for - timedelta(days=shipment.scheduled_for.weekday())
            return redirect(f'{reverse("portal:ops-shipping")}?{urlencode({"week": monday.isoformat()})}')


@method_decorator(csrf_exempt, name='dispatch')
class CourierWebhookView(View):
    """The Courier Guy's tracking webhook: each parcel's courier status, and delivery. Its address carries a secret
    token (TCG_WEBHOOK_TOKEN), as The Courier Guy doesn't sign webhooks; anything else is refused."""

    http_method_names = ('post',)

    def post(self, request, token):
        import json

        from care.courier import apply_tracking, webhook_token_ok

        if not webhook_token_ok(token):
            return HttpResponse(status=404)
        try:
            payload = json.loads(request.body)
        except ValueError:
            return HttpResponse(status=400)
        events = payload if isinstance(payload, list) else [payload]
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


def _history_filter(request, queryset, *, by_week=False):
    form = HistoryFilterForm(request.GET)
    if by_week:
        # Week view: what was dispatched in the chosen week, Monday to Sunday; last week unless another is chosen.
        week = chosen_week(request, last_week())
        form = HistoryFilterForm({'q': request.GET.get('q', ''), 'start': week, 'end': week + timedelta(days=6)})
    if form.is_valid():
        if form.cleaned_data.get('start'):
            queryset = queryset.filter(dispatched_at__date__gte=form.cleaned_data['start'])
        if form.cleaned_data.get('end'):
            queryset = queryset.filter(dispatched_at__date__lte=form.cleaned_data['end'])
        queryset = _shipment_search(queryset, form.cleaned_data['q'])
    else:
        queryset = queryset.none()
    return form, queryset


class DispatchHistoryView(OperationsView):
    """What was dispatched: one shipping week at a time (Week, the default, opening on last week), or a list with a
    date range (List, ?view=list). Export takes whichever is shown."""
    shipping = True

    nav_section, page_title = 'history', 'Dispatch history'
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        by_week = request.GET.get('view') != 'list'
        form, queryset = _history_filter(request, self.shipments().filter(status__in=('dispatched', 'delivered')).order_by('-dispatched_at', '-pk'),
                                         by_week=by_week)
        context = self.context(filter_form=form, by_week=by_week, **_paginate(request, queryset))
        context['shipments'] = list(context['page_obj'].object_list)
        for shipment in context['shipments']:
            shipment.doctor = shipment_doctor(shipment)
        count = context['page_obj'].paginator.count
        week_url = reverse('portal:ops-history')
        list_url = f'{week_url}?view=list'
        if by_week:
            week = chosen_week(request, last_week())
            context.update(week_navigation('portal:ops-history', week))
            # Only the search: the week is the date range. The search keeps the week.
            kept = {'week': week.isoformat()}
            search = request.GET.get('q', '')
            context.update(search=search, search_action=week_url, search_hidden=kept, search_placeholder='Patient or tracking',
                           search_clear_url=f'{week_url}?{urlencode(kept)}', filter_menu=None,
                           count_label=f'{count} shipment{pluralize(count)}',
                           export_query=urlencode({'start': week.isoformat(), 'end': (week + timedelta(days=6)).isoformat(), 'q': search}),
                           list_view_url=list_url, week_view_url=week_url)
        else:
            dated = int(bool(form['start'].value() or form['end'].value()))
            tools = _list_tools(request, form, url_name='portal:ops-history', label='Filter dispatches',
                                sections=[fields_section('dispatched', 'Dispatched', [form['start'], form['end']], count=dated)],
                                placeholder='Patient or tracking', count=count, noun='shipment')
            # The search and Filters, and clearing either, keep List.
            dates = tools['search_hidden']
            tools.update(search_hidden={**dates, 'view': 'list'}, search_clear_url=f'{list_url}&{urlencode(dates)}' if dates else list_url)
            tools['filter_menu'].update(hidden=[*tools['filter_menu']['hidden'], ('view', 'list')],
                                        clear_url=f"{list_url}&{urlencode({'q': tools['search']})}" if tools['search'] else list_url)
            export = request.GET.copy()
            for name in ('view', 'page'):
                export.pop(name, None)
            context.update(tools, export_query=export.urlencode(), list_view_url=list_url, week_view_url=week_url)
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


def batch_text(item):
    """A dispatched item's batch note. Parcels dispatched from stock saved the batch and its expiry instead."""
    if item.get('batch_note'):
        return item['batch_note']
    return ', expiry '.join(str(part) for part in (item.get('batch'), item.get('expires_on')) if part)


class ManifestDownloadView(OperationsView):
    shipping = True
    http_method_names = ('get', 'head', 'options')
    history = False

    def get(self, request):
        queryset = self.shipments()
        if self.history:
            form, queryset = _history_filter(request, queryset.filter(status__in=('dispatched', 'delivered')))
        else:
            form, queryset = _shipping_filter(request, queryset, shipping_week(self.company))
        if not form.is_valid():
            return HttpResponse('Correct the export date filters.', status=400)
        if queryset.count() > 10000:
            return HttpResponse('Narrow the export dates to at most 10,000 shipments.', status=400)
        from care.billing import parcel_payment
        from care.models import DeliveryAddress

        addresses = {address.patient_id: address for address in DeliveryAddress.objects.filter(
            company=self.company, patient__in=queryset.values('patient'))}
        rows = []
        for shipment in queryset.order_by('dispatched_at' if self.history else 'scheduled_for', 'pk'):
            snapshot = shipment.dispatch_snapshot or {}
            # Where it went: the address saved at dispatch, else the patient's address now.
            sent_to = snapshot.get('delivery_address') or {}
            address = addresses.get(shipment.patient_id)
            if sent_to.get('lines'):
                deliver_to, phone = ', '.join(sent_to['lines']), sent_to.get('phone', '')
            elif address is not None:
                deliver_to, phone = ', '.join(address.lines()), address.phone
            else:
                deliver_to, phone = '', ''
            paid = parcel_payment(shipment)[1] if not self.history else ''
            waybill = shipment.waybill or snapshot.get('waybill', '')
            patient_name = snapshot.get('patient_name', str(shipment.patient))
            id_number = snapshot.get('patient_id_number', shipment.patient.id_number)
            doctor = shipment_doctor(shipment)
            dispatched = ()
            if self.history:
                dispatched = (timezone.localtime(shipment.dispatched_at).strftime('%Y-%m-%d %H:%M') if shipment.dispatched_at else '',)
            items = snapshot.get('items') or [dict(product=str(item.product), dose=item.dose, quantity=item.quantity,
                                                       batch_note=item.batch_note) for item in shipment.items.all()]
            for item in items:
                rows.append((shipment.pk, shipment.scheduled_for, *dispatched, snapshot.get('company_name', self.company.name), patient_name,
                             id_number, doctor.titled_name if doctor else '', item['product'], item['dose'], item['quantity'],
                             batch_text(item), shipment.get_status_display(), shipment.tracking_number, shipment.hold_reason, shipment.note,
                             deliver_to, phone, *((paid,) if not self.history else ()), waybill))
        if request.method != 'HEAD':
            record_audit(company=self.company, actor=request.user, action='dispatch.history_exported' if self.history else 'shipping.manifest_exported',
                         request=request, metadata={'rows': len(rows)})
        dates = ('Planned date', 'Dispatched') if self.history else ('Planned date',)
        return _csv_response(self.filename(form) if self.history else 'shipping-manifest.csv',
                             ('Shipment', *dates, 'Practice', 'Patient', 'ID / passport', 'Doctor', 'Product', 'Dose', 'Quantity',
                              'Batch and expiry', 'Status', 'Tracking', 'Hold reason', 'Note', 'Deliver to', 'Contact number',
                              *(('Paid',) if not self.history else ()), 'Waybill'), rows)

    @staticmethod
    def filename(form):
        """dispatch-history-2026-10-19-to-2026-10-25.csv for a week or other date range."""
        start, end = form.cleaned_data.get('start'), form.cleaned_data.get('end')
        if not (start or end):
            return 'dispatch-history.csv'
        return f'dispatch-history-{start.isoformat() if start else "start"}-to-{end.isoformat() if end else "today"}.csv'


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
                messages.success(request, 'Unaccepted request cancelled.')
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
            product.plan_dose = dose_in_effect(product.plan) if product.plan else None
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
        from care.payment_providers import current_provider

        payable = (not order.paid_at and order.status in ('submitted', 'accepted') and order.subtotal > 0
                   and current_provider() is not None)
        return render(request, 'portal/pharmacy_order_detail.html', self.context(
            order=order, items=_order_items(order), shipment=order.shipment,
            cancel_form=form if form is not None else ConfirmForm(), can_cancel=order.status == 'submitted',
            patient_section='orders', payable=payable,
            pay_context=make_workflow_context(request, self.patient_company, 'order-pay', order, patient=self.patient) if payable else '',
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
                    messages.success(request, 'Request submitted. Pay for it below so the practice can send it.')
                else:
                    cancel_order(order=order, actor=request.user, confirm=True, request=request)
                    messages.success(request, 'Supply request cancelled.')
                return redirect('portal:patient-order-detail', pk=pk)
        except ValidationError as error:
            _errors(request, form, error)
        return self.display(request, order, form, status=400)
