"""Billing pages (care.billing): the patient's Billing and payments, the sandbox approval pages, the patient's
delivery address, and the practice's Operations > Billing with each subscription's payments."""

import logging
from decimal import Decimal

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import pluralize
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from care.addresses import address_for, save_delivery_address
from care.billing import (
    apply_notice, approve_mandate, ask_for_new_debit_order, billed_to, billing_state, complete_card_payment, current_mandate,
    decline_mandate, money, outstanding, patient_summary, record_manual_payment, retry_now, run_debits, set_next_debit,
    settle_mandate, settle_payment, start_cycle_payment, start_debit_order, start_order_payment,
)
from care.models import (
    DebitMandate, Invoice, PatientSubscription, Payment, PharmacyOrder, SubscriptionCycle,
)
from care.payment_providers import HOSTED, SandboxProvider, current_provider, provider_named
from care.stitch import debicheck_in_use, debicheck_waiting
from practices.models import CompanyMembership

from .filter_menus import TickedChoiceField, list_tools, ticked_sections, without_all
from .operations_forms import DeliveryAddressForm
from .operations_views import OperationsView
from .patient_care_views import PatientCarePage
from .treatment_forms import limit_methods
from .workflow_context import make_workflow_context, validate_workflow_context

logger = logging.getLogger(__name__)
OPEN_CYCLES = (SubscriptionCycle.Status.PENDING, SubscriptionCycle.Status.FAILED)


def _messages(error):
    return ' '.join(error.messages)


def _current_plan(company, patient):
    plans = PatientSubscription.objects.filter(company=company, patient=patient).select_related('authorization__dose')
    return plans.exclude(status=PatientSubscription.Status.CANCELLED).order_by('-created_at', '-pk').first()


class DebitOrderForm(forms.Form):
    method = forms.ChoiceField(label='Pay by', widget=forms.RadioSelect, initial=DebitMandate.Method.DEBICHECK, choices=(
        (DebitMandate.Method.DEBICHECK, 'DebiCheck debit order: you approve it in your banking app'),
        (DebitMandate.Method.CARD, 'Card: kept safely by our payment provider'),
    ))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        limit_methods(self.fields['method'], current_provider())


# The patient

class PatientBillingView(PatientCarePage):
    """Billing and payments: the debit order, what's outstanding with Pay now, and each cycle with its invoice."""

    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        company, patient = self.patient_company, self.patient
        plan = _current_plan(company, patient)
        cycles = SubscriptionCycle.objects.filter(company=company, patient=patient).select_related('invoice').order_by('-due_on', '-pk')
        page = Paginator(cycles, 20).get_page(request.GET.get('page'))
        due = [cycle for cycle in cycles.filter(status__in=OPEN_CYCLES) if cycle.attempts or cycle.status == SubscriptionCycle.Status.FAILED]
        for cycle in due:
            cycle.pay_context = make_workflow_context(request, company, 'cycle-pay', cycle, patient=patient)
        once_off = Payment.objects.filter(company=company, patient=patient, kind__in=(Payment.Kind.ONCE_OFF, Payment.Kind.CONSULTATION,
                                                                                         Payment.Kind.MANUAL)).order_by('-created_at')[:20]
        context = self.context('billing', 'Billing and payments', plan=plan,
                               summary=patient_summary(plan) if plan else None, mandate=current_mandate(plan) if plan else None,
                               due=due, page_obj=page, once_off=once_off, provider=current_provider(),
                               debit_form=DebitOrderForm(),
                               debit_context=make_workflow_context(request, company, 'debit-order', plan, patient=patient) if plan else '',
                               address=address_for(patient))
        return render(request, 'portal/patient_billing.html', context)


class PatientDebitOrderView(PatientCarePage):
    """Set up, approve again or change the debit order: then on to the provider's page to approve it."""

    http_method_names = ('post',)

    def post(self, request):
        plan = _current_plan(self.patient_company, self.patient)
        if plan is None:
            raise Http404
        form = DebitOrderForm(request.POST)
        try:
            validate_workflow_context(request, self.patient_company, 'debit-order', plan, patient=self.patient, check_version=False)
            if not form.is_valid():
                raise ValidationError('Choose a DebiCheck debit order or a card.')
            _, url = start_debit_order(subscription=plan, actor=request.user, method=form.cleaned_data['method'], request=request)
            return redirect(url)
        except ValidationError as error:
            messages.error(request, _messages(error))
        return redirect('portal:patient-billing')


class PatientCyclePayView(PatientCarePage):
    """Pay a failed debit now by card."""

    http_method_names = ('post',)

    def post(self, request, pk):
        cycle = get_object_or_404(SubscriptionCycle.objects.filter(company=self.patient_company, patient=self.patient), pk=pk)
        try:
            validate_workflow_context(request, self.patient_company, 'cycle-pay', cycle, patient=self.patient, check_version=False)
            _, url = start_cycle_payment(cycle=cycle, actor=request.user, request=request)
            return redirect(url)
        except ValidationError as error:
            messages.error(request, _messages(error))
        return redirect('portal:patient-billing')


class PatientOrderPayView(PatientCarePage):
    """Pay for a non-prescription request by card."""

    http_method_names = ('post',)

    def post(self, request, pk):
        order = get_object_or_404(PharmacyOrder.objects.filter(company=self.patient_company, patient=self.patient), pk=pk)
        try:
            validate_workflow_context(request, self.patient_company, 'order-pay', order, patient=self.patient, check_version=False)
            _, url = start_order_payment(order=order, actor=request.user, request=request)
            return redirect(url)
        except ValidationError as error:
            messages.error(request, _messages(error))
        return redirect('portal:patient-order-detail', pk=pk)


class PatientInvoiceView(PatientCarePage):
    """One invoice, to print or save as PDF."""

    http_method_names = ('get', 'head', 'options')

    def get(self, request, pk):
        invoice = get_object_or_404(Invoice.objects.filter(company=self.patient_company, patient=self.patient).select_related(
            'subscription_cycle__subscription'), pk=pk)
        return render(request, 'portal/patient_invoice.html', self.context('billing', f'Invoice {invoice.invoice_number}',
                                                                           invoice=invoice, lines=invoice.lines.all(),
                                                                           billed_to=invoice.billed_to or billed_to(self.patient),
                                                                           paid=invoice.status == Invoice.Status.PAID))


class PatientDeliveryAddressView(PatientCarePage):
    """The patient's own delivery address and contact number for parcels."""

    def display(self, request, form, status=200):
        return render(request, 'portal/patient_delivery_address.html', self.context(
            'account', 'Delivery address', form=form, address=address_for(self.patient)), status=status)

    def get(self, request):
        return self.display(request, DeliveryAddressForm(instance_address=address_for(self.patient), patient=self.patient,
                                                         for_patient=True))

    def post(self, request):
        form = DeliveryAddressForm(request.POST, patient=self.patient, for_patient=True)
        if form.is_valid():
            try:
                _, changed = save_delivery_address(company=self.patient_company, patient=self.patient, actor=request.user,
                                                   data=form.address(), request=request)
                messages.success(request, 'Your delivery address is saved.' if changed else 'Your delivery address is already up to date.')
                return redirect(f"{reverse('portal:patient-profile')}#profile-delivery")
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
        return self.display(request, form, status=400)


# Back from the provider's page, and its webhooks

@method_decorator(csrf_exempt, name='dispatch')
class PatientPaymentReturnView(PatientCarePage):
    """Where the provider's page sends the patient back, after approving a debit order or paying by card. What it
    sends back isn't trusted: the provider is asked how it went, for the record kept here. (Some providers post back,
    so this isn't CSRF-checked; it changes nothing the provider didn't confirm.)"""

    http_method_names = ('get', 'post', 'head', 'options')

    def get(self, request, kind, pk):
        params = request.GET if request.method in ('GET', 'HEAD') else request.POST
        if kind == 'mandate':
            mandate = get_object_or_404(DebitMandate.objects.filter(company=self.patient_company, patient=self.patient,
                                                                    provider__in=HOSTED), pk=pk)
            provider = provider_named(mandate.provider)
            try:
                approval = provider.finish_mandate(mandate, params)
                settle_mandate(mandate=mandate, approval=approval, actor=request.user, request=request)
                if approval.status == 'approved':
                    messages.success(request, 'Your debit order is approved.')
                elif approval.status == 'declined':
                    messages.warning(request, f'Your debit order wasn’t approved. {approval.reason}'.strip())
                else:
                    messages.info(request, 'Your bank is still confirming your debit order. We’ll email you if anything is needed.')
            except ValidationError as error:
                messages.error(request, _messages(error))
            return redirect('portal:patient-billing')
        if kind != 'payment':
            raise Http404
        payment = get_object_or_404(Payment.objects.filter(company=self.patient_company, patient=self.patient,
                                                           provider__in=HOSTED, checkout=True), pk=pk)
        provider = provider_named(payment.provider)
        try:
            collection = provider.finish_checkout(payment, params)
            settle_payment(payment=payment, collection=collection, actor=request.user, request=request)
            if collection.status == 'paid':
                messages.success(request, f'{money(payment.amount)} paid. Thank you.')
            elif collection.status == 'failed':
                messages.warning(request, f'The card payment didn’t go through. Nothing was taken. {collection.reason}'.strip())
            else:
                messages.info(request, 'Your payment is still being confirmed. This page updates once it is.')
        except ValidationError as error:
            messages.error(request, _messages(error))
        if payment.pharmacy_order_id:
            return redirect('portal:patient-order-detail', pk=payment.pharmacy_order_id)
        return redirect('portal:patient-billing')

    post = get


class PatientCardFormView(PatientCarePage):
    """The provider's card form, on the app's page (Peach's COPYandPAY): to keep a card for the subscription, or to
    pay once. Each visit prepares a fresh form; it sends the patient on to the return page."""

    http_method_names = ('get', 'head', 'options')

    def get(self, request, kind, pk):
        if kind == 'mandate':
            record = get_object_or_404(DebitMandate.objects.filter(company=self.patient_company, patient=self.patient,
                                                                   provider__in=HOSTED, status=DebitMandate.Status.PENDING), pk=pk)
            title, amount = 'Add your card', record.amount
        elif kind == 'payment':
            record = get_object_or_404(Payment.objects.filter(company=self.patient_company, patient=self.patient, checkout=True,
                                                              provider__in=HOSTED, status=Payment.Status.PENDING), pk=pk)
            title, amount = 'Pay by card', record.amount
        else:
            raise Http404
        provider = provider_named(record.provider)
        if not hasattr(provider, 'card_form'):
            raise Http404
        try:
            form = provider.card_form(kind, record)
        except ValidationError as error:
            messages.error(request, _messages(error))
            return redirect('portal:patient-billing')
        return render(request, 'portal/patient_card_form.html', self.context(
            'billing', title, provider=provider, form=form, amount=money(amount),
            mandate=record if kind == 'mandate' else None, payment=record if kind == 'payment' else None))


@method_decorator(csrf_exempt, name='dispatch')
class PaymentWebhookView(View):
    """The provider tells the app about a debit order or payment (approved, paid, failed). Each provider signs or
    encrypts its webhooks; read_webhook refuses one it can't verify. Telling the app twice changes nothing."""

    http_method_names = ('post',)

    def post(self, request, name):
        # Each provider has its own address, so the one being moved away from can still settle what it holds.
        if name not in HOSTED:
            raise Http404
        provider = provider_named(name)
        try:
            notices = provider.read_webhook(request)
        except ValidationError as error:
            logger.warning('Refused a %s webhook: %s', name, _messages(error))
            return HttpResponse(status=400)
        for notice in notices:
            apply_notice(provider, notice)
        return JsonResponse({'received': len(notices)})


# The sandbox: approving and paying inside the app, for local testing

def _require_sandbox():
    if settings.PAYMENT_PROVIDER != SandboxProvider.name:
        raise Http404


class PatientSandboxMandateView(PatientCarePage):
    """Stands in for the bank's or card provider's approval page."""

    def mandate(self, pk):
        _require_sandbox()
        return get_object_or_404(DebitMandate.objects.filter(company=self.patient_company, patient=self.patient,
                                                             provider=SandboxProvider.name), pk=pk)

    def get(self, request, pk):
        mandate = self.mandate(pk)
        return render(request, 'portal/patient_billing_sandbox.html', self.context(
            'billing', 'Approve your debit order', mandate=mandate, amount=money(mandate.amount),
            workflow_context=make_workflow_context(request, self.patient_company, 'sandbox-mandate', mandate, patient=self.patient)))

    def post(self, request, pk):
        mandate = self.mandate(pk)
        try:
            validate_workflow_context(request, self.patient_company, 'sandbox-mandate', mandate, patient=self.patient)
            choice = request.POST.get('choice')
            if choice == 'decline':
                decline_mandate(mandate=mandate, reason='You declined it.', actor=request.user, request=request)
                messages.warning(request, 'You declined the debit order. Set it up again when you’re ready.')
            else:
                label = 'Visa ···4242' if mandate.method == DebitMandate.Method.CARD else 'Sandbox Bank ···1234'
                approve_mandate(mandate=mandate, account_label=label, fail_debits=choice == 'fail', actor=request.user, request=request)
                messages.success(request, 'Your debit order is approved. Your first payment has been collected.'
                                 if choice != 'fail' else 'Your debit order is approved. (Sandbox: its debits will fail.)')
        except ValidationError as error:
            messages.error(request, _messages(error))
        return redirect('portal:patient-billing')


class PatientSandboxCheckoutView(PatientCarePage):
    """Stands in for the card provider's payment page."""

    def payment(self, pk):
        _require_sandbox()
        return get_object_or_404(Payment.objects.filter(company=self.patient_company, patient=self.patient,
                                                        provider=SandboxProvider.name, status=Payment.Status.PENDING), pk=pk)

    def get(self, request, pk):
        payment = self.payment(pk)
        return render(request, 'portal/patient_billing_sandbox.html', self.context(
            'billing', 'Pay by card', payment=payment, amount=money(payment.amount),
            workflow_context=make_workflow_context(request, self.patient_company, 'sandbox-checkout', payment, patient=self.patient)))

    def post(self, request, pk):
        payment = self.payment(pk)
        try:
            validate_workflow_context(request, self.patient_company, 'sandbox-checkout', payment, patient=self.patient)
            paid = request.POST.get('choice') == 'pay'
            complete_card_payment(payment=payment, paid=paid, reference=f'sandbox-card-{payment.pk}',
                                  reason='Card declined (sandbox).', actor=request.user, request=request)
            (messages.success if paid else messages.warning)(request, f'{money(payment.amount)} paid. Thank you.' if paid
                                                             else 'The card payment didn’t go through. Nothing was taken.')
        except ValidationError as error:
            messages.error(request, _messages(error))
        if payment.pharmacy_order_id:
            return redirect('portal:patient-order-detail', pk=payment.pharmacy_order_id)
        return redirect('portal:patient-billing')


# The practice: Operations > Billing

class BillingFilterForm(forms.Form):
    q = forms.CharField(label='Search billing', required=False, max_length=100)
    state = TickedChoiceField(label='Billing', choices=(
        ('failed', 'Failed (failure queue)'), ('retrying', 'Retrying'), ('due', 'Due'), ('update', 'New amount to confirm'),
        ('pending', 'Waiting for approval'), ('no_mandate', 'No debit order'), ('paid', 'Paid up'),
    ))


class BillingListView(OperationsView):
    """Every subscription's billing: paid up, due, retrying, failed (the failure queue), or waiting on the patient.
    Practice administrators and Super Admins; totals owed are for Super Admins."""

    nav_section, page_title = 'billing', 'Billing'
    http_method_names = ('get', 'head', 'options')

    def get(self, request):
        form = BillingFilterForm(without_all(request.GET, 'state'))
        plans = PatientSubscription.objects.filter(company=self.company, patient__company=self.company).exclude(
            status=PatientSubscription.Status.CANCELLED).select_related('patient', 'authorization__dose__pen').order_by(
            'patient__last_name', 'patient__first_name', 'pk')
        states = []
        if form.is_valid():
            for term in form.cleaned_data['q'].split():
                plans = plans.filter(Q(patient__first_name__icontains=term) | Q(patient__last_name__icontains=term))
            states = form.cleaned_data['state']
        else:
            plans = plans.none()
        rows = []
        for plan in plans:
            plan.state, plan.state_text = billing_state(plan)
            if states and plan.state not in states:
                continue
            plan.mandate = current_mandate(plan)
            plan.owed = outstanding(plan)
            plan.last_paid = plan.cycles.filter(status=SubscriptionCycle.Status.PAID).order_by('-paid_at').first()
            rows.append(plan)
        # The failure queue first, then what's waiting on someone, then the rest.
        order = {'failed': 0, 'retrying': 1, 'update': 2, 'pending': 3, 'no_mandate': 4, 'due': 5, 'paid': 6}
        rows.sort(key=lambda plan: order.get(plan.state, 9))
        page = Paginator(rows, 50).get_page(request.GET.get('page'))
        query = request.GET.copy()
        query.pop('page', None)
        super_admin = self.membership.role == CompanyMembership.Role.SUPER_ADMIN
        provider = current_provider()
        debicheck = None
        if super_admin and debicheck_in_use(self.company):
            debicheck = {'waiting': debicheck_waiting(self.company).count(),
                         'context': make_workflow_context(request, self.company, 'debicheck-files')}
        context = self.context(
            filter_form=form, page_obj=page, is_paginated=page.has_other_pages(), pagination_query=query.urlencode(),
            failed_count=sum(1 for plan in rows if plan.state == 'failed'), show_totals=super_admin,
            total_owed=sum((plan.owed for plan in rows), Decimal('0')), provider=provider, debicheck=debicheck,
            run_context=make_workflow_context(request, self.company, 'debit-run') if super_admin else '',
            count_label=f'{len(rows)} subscription{pluralize(len(rows))}',
            **list_tools(action=reverse('portal:ops-billing'), data=request.GET, form=form, label='Filter billing',
                         sections=ticked_sections(form, request.GET, ('state',)), placeholder='Patient name'))
        return render(request, 'portal/operations_billing.html', context)


class DebitRunView(OperationsView):
    """Run today's debits now (Super Admins), as the daily `manage.py run_debits` does."""

    http_method_names = ('post',)

    def post(self, request):
        if self.membership.role != CompanyMembership.Role.SUPER_ADMIN:
            raise PermissionDenied('Only a Super Admin can run the debits by hand.')
        try:
            validate_workflow_context(request, self.company, 'debit-run')
            counts = run_debits(self.company)
            messages.success(request, f'Debit run done: {counts["tried"]} tried, {counts["paid"]} paid, {counts["failed"]} failed.')
        except ValidationError as error:
            messages.error(request, _messages(error))
        return redirect('portal:ops-billing')


class DebiCheckFilesView(OperationsView):
    """Stitch's DebiCheck collection files, by hand until Stitch gives the practice SFTP access (Super Admins): download
    the next collection file to send to Stitch, or upload a REPLY or OUTPUT file Stitch sent back."""

    http_method_names = ('post',)
    MAX_UPLOAD = 2 * 1024 * 1024

    def post(self, request):
        from care.stitch import apply_results_file, collection_file

        if self.membership.role != CompanyMembership.Role.SUPER_ADMIN:
            raise PermissionDenied('Only a Super Admin can send or read collection files.')
        if not debicheck_in_use(self.company):
            raise Http404
        try:
            validate_workflow_context(request, self.company, 'debicheck-files')
            upload = request.FILES.get('results')
            if request.POST.get('action') == 'download':
                made = collection_file(self.company, actor=request.user)
                if made is None:
                    messages.info(request, 'No DebiCheck collections are waiting to be sent.')
                    return redirect('portal:ops-billing')
                name, text, count = made
                response = HttpResponse(text, content_type='text/csv; charset=utf-8')
                response['Content-Disposition'] = f'attachment; filename="{name}"'
                return response
            if upload is None:
                raise ValidationError('Choose the REPLY or OUTPUT file Stitch sent.')
            if upload.size > self.MAX_UPLOAD:
                raise ValidationError('That file is too big to be a Stitch collection file.')
            try:
                text = upload.read().decode('utf-8-sig')
            except UnicodeDecodeError:
                raise ValidationError('That file isn’t a CSV file.') from None
            counts = apply_results_file(self.company, text, actor=request.user)
            messages.success(request, f'Stitch’s file read: {counts["paid"]} paid, {counts["failed"]} failed, '
                                      f'{counts["pending"]} still with the bank, {counts["ignored"]} already settled or not found.')
        except ValidationError as error:
            messages.error(request, _messages(error))
        return redirect('portal:ops-billing')


class ManualPaymentForm(forms.Form):
    amount = forms.DecimalField(label='Amount received (R)', min_value=Decimal('0.01'), max_digits=10, decimal_places=2)
    method = forms.ChoiceField(label='Paid by', choices=(
        (Payment.Method.EFT, 'EFT'), (Payment.Method.CASH, 'Cash'), (Payment.Method.CARD, 'Card machine'),
        (Payment.Method.DEBICHECK, 'Debit order outside the platform'), (Payment.Method.OTHER, 'Other')))
    reference = forms.CharField(label='Reference', required=False, max_length=120, help_text='Such as the EFT reference.')
    note = forms.CharField(label='Note', required=False, max_length=300)


class NextDebitForm(forms.Form):
    day = forms.DateField(label='Next debit on', widget=forms.DateInput(attrs={'type': 'date'}))


class BillingDetailView(OperationsView):
    """One subscription's billing, for the practice: its debit orders, every cycle and payment attempt, and what can be
    done: retry now, record a payment received another way, move the next debit, ask for a new debit order."""

    nav_section, page_title = 'billing', 'Billing'

    def plan(self, pk):
        return get_object_or_404(PatientSubscription.objects.filter(company=self.company, patient__company=self.company)
                                 .select_related('patient', 'authorization__dose__pen'), pk=pk)

    def display(self, request, plan, *, payment_form=None, payment_cycle=None, next_form=None, status=200):
        cycles = list(plan.cycles.select_related('invoice').order_by('-cycle_number'))
        for cycle in cycles:
            cycle.attempt_rows = list(cycle.payments.order_by('attempt', 'pk'))
            if self.can_edit and cycle.status in OPEN_CYCLES:
                cycle.retry_context = make_workflow_context(request, self.company, 'cycle-retry', cycle)
                cycle.record_context = make_workflow_context(request, self.company, 'cycle-record', cycle)
        state, state_text = billing_state(plan)
        return render(request, 'portal/operations_billing_detail.html', self.context(
            plan=plan, cycles=cycles, mandates=plan.mandates.order_by('-created_at')[:10], state=state, state_text=state_text,
            owed=outstanding(plan), provider=current_provider(), address=address_for(plan.patient),
            payment_form=payment_form or ManualPaymentForm(), payment_cycle=payment_cycle,
            next_form=next_form or NextDebitForm(initial={'day': plan.next_debit_on}),
            next_context=make_workflow_context(request, self.company, 'next-debit', plan),
            ask_context=make_workflow_context(request, self.company, 'ask-debit-order', plan),
            other_payments=Payment.objects.filter(company=self.company, patient=plan.patient, subscription_cycle__isnull=True)
            .order_by('-created_at')[:20]), status=status)

    def get(self, request, pk):
        return self.display(request, self.plan(pk))

    def post(self, request, pk):
        plan = self.plan(pk)
        action = request.POST.get('action')
        try:
            if action == 'next_debit':
                form = NextDebitForm(request.POST)
                validate_workflow_context(request, self.company, 'next-debit', plan, check_version=False)
                if not form.is_valid():
                    return self.display(request, plan, next_form=form, status=400)
                set_next_debit(subscription=plan, actor=request.user, day=form.cleaned_data['day'], request=request)
                messages.success(request, f'Next debit moved to {form.cleaned_data["day"]:%-d %B %Y}.')
            elif action == 'ask':
                validate_workflow_context(request, self.company, 'ask-debit-order', plan, check_version=False)
                ask_for_new_debit_order(subscription=plan, actor=request.user, request=request)
                messages.success(request, f'{plan.patient} has been emailed to set up their debit order again.')
            elif action in ('retry', 'record'):
                cycle = get_object_or_404(plan.cycles, pk=request.POST.get('cycle'))
                validate_workflow_context(request, self.company, f'cycle-{action}', cycle, check_version=False)
                if action == 'retry':
                    payment = retry_now(cycle=cycle, actor=request.user, request=request)
                    payment.refresh_from_db()
                    (messages.success if payment.status == Payment.Status.PAID else messages.warning)(
                        request, f'{money(cycle.amount)} collected.' if payment.status == Payment.Status.PAID
                        else f'The debit failed again: {payment.failure_reason or "no reason given"}.')
                else:
                    form = ManualPaymentForm(request.POST)
                    if not form.is_valid():
                        return self.display(request, plan, payment_form=form, payment_cycle=cycle, status=400)
                    record_manual_payment(company=self.company, actor=request.user, cycle=cycle, request=request,
                                          **form.cleaned_data)
                    messages.success(request, f'Payment of {money(form.cleaned_data["amount"])} recorded for cycle {cycle.cycle_number}.')
            else:
                raise ValidationError('Choose what to do.')
        except ValidationError as error:
            messages.error(request, _messages(error))
        return redirect('portal:ops-billing-detail', pk=pk)


class ParcelPaymentView(OperationsView):
    """Record a payment received another way for one parcel (or its non-prescription request), from Weekly shipping."""

    nav_section, page_title = 'shipping', 'Record a payment'
    shipping = True

    def shipment(self, pk):
        return get_object_or_404(self.shipments(), pk=pk)

    def display(self, request, shipment, form, status=200):
        from care.billing import parcel_payment

        state, text = parcel_payment(shipment)
        return render(request, 'portal/operations_parcel_payment.html', self.context(
            shipment=shipment, form=form, payment_state=state, payment_text=text,
            next_url=request.POST.get('next') or request.GET.get('next') or reverse('portal:ops-shipping')), status=status)

    def get(self, request, pk):
        from care.operations import require_operations_actor

        shipment = self.shipment(pk)
        require_operations_actor(self.company, request.user, shipping=True)
        order = getattr(shipment, 'pharmacy_order', None)
        return self.display(request, shipment, ManualPaymentForm(initial={'amount': order.subtotal if order else None}))

    def post(self, request, pk):
        shipment = self.shipment(pk)
        form = ManualPaymentForm(request.POST)
        if form.is_valid():
            order = getattr(shipment, 'pharmacy_order', None)
            try:
                record_manual_payment(company=self.company, actor=request.user, request=request, from_shipping=True,
                                      **({'order': order} if order is not None else {'shipment': shipment}), **form.cleaned_data)
                messages.success(request, f'Payment recorded for {shipment.patient}’s parcel.')
                target = request.POST.get('next', '')
                return redirect(target if target.startswith('/') and not target.startswith('//') else reverse('portal:ops-shipping'))
            except ValidationError as error:
                for message in error.messages:
                    form.add_error(None, message)
        return self.display(request, shipment, form, status=400)
