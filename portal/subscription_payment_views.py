"""Practice subscription payment schedules and auditable discounts."""
from decimal import Decimal
from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit
from django.utils import timezone
from django.views import View
from care.billing import require_staff
from care.models import PatientSubscription, SubscriptionDiscount
from care.subscription_discounts import apply_discount, payment_schedule
from .views import StaffCompanyRequiredMixin
from .workflow_context import make_workflow_context, validate_workflow_context


class SubscriptionDiscountForm(forms.Form):
    scope = forms.ChoiceField(label='Apply to', choices=(('one', 'One unpaid payment'), ('selected', 'Selected payments'), ('future', 'From an effective date')))
    payment = forms.ChoiceField(label='Payment', required=False)
    payments = forms.MultipleChoiceField(label='Selected payments', required=False)
    effective_on = forms.DateField(label='Effective date', required=False, initial=timezone.localdate,
        widget=forms.DateInput(attrs={'type': 'date'}), help_text='Applies to payment due dates on or after this date.')
    kind = forms.ChoiceField(label='Discount type', choices=SubscriptionDiscount.Kind.choices)
    value = forms.DecimalField(label='Discount', min_value=Decimal('0'), max_digits=10, decimal_places=2)
    reason = forms.CharField(label='Reason (optional)', required=False, max_length=300)

    def __init__(self, *args, plan, schedule, **kwargs):
        kwargs.setdefault('auto_id', f'bulk_discount_{plan.pk}_%s')
        super().__init__(*args, **kwargs)
        self.rows = {row['key']: row for row in schedule['rows'] if row['editable']}
        self.fields['payment'].choices = [('', 'Choose a payment'), *[
            (key, f"{row['due_on']:%d %b %Y} · R{row['amount']:.2f}") for key, row in self.rows.items()]]
        self.fields['payments'].choices = [(key, key) for key in self.rows]

    def clean(self):
        data = super().clean()
        if data.get('kind') == 'percent' and data.get('value') is not None and data['value'] > 100:
            self.add_error('value', 'Enter a percentage from 0 to 100.')
        if data.get('scope') == 'one':
            row = self.rows.get(data.get('payment'))
            if row is None:
                self.add_error('payment', 'This payment is no longer editable. Refresh the schedule.')
            else:
                data['effective_on'] = row['due_on']
        elif data.get('scope') == 'selected':
            if not data.get('payments'):
                self.add_error('payments', 'Select at least one unpaid payment in the table.')
        elif data.get('scope') == 'future' and not data.get('effective_on'):
            self.add_error('effective_on', 'Choose the effective date.')
        return data


def payment_popup(request, company, plan, *, form=None, schedule=None):
    schedule = schedule or payment_schedule(plan)
    base = reverse('portal:treatment-subscriptions')
    candidate = (request.get_full_path() if request.resolver_match.url_name == 'treatment-subscriptions'
                 else request.POST.get('return_to') or request.GET.get('return_to') or base)
    parts = urlsplit(candidate)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != 'payments']
    return_url = urlunsplit(('', '', base, urlencode(query), '')) if not parts.scheme and not parts.netloc and parts.path == base else base
    discount_form = form if form is not None else SubscriptionDiscountForm(plan=plan, schedule=schedule)
    single_error = form is not None and request.POST.get('scope') == 'one'
    if single_error:
        form.auto_id = f'line_discount_{plan.pk}_error_%s'
    bulk_form = SubscriptionDiscountForm(plan=plan, schedule=schedule, initial={'scope': 'selected'}) if single_error or form is None else form
    bulk_form.fields['scope'].choices = [('selected', 'Selected payments'), ('future', 'From an effective date')]
    for index, row in enumerate(schedule['rows']):
        row['editor_id'] = f'payment-edit-{plan.pk}-{index}'
        selected_error = single_error and request.POST.get('payment') == row['key']
        row['edit_open'] = selected_error or request.GET.get('edit') == row['key']
        if row['editable'] or selected_error:
            row['discount_form'] = form if selected_error else SubscriptionDiscountForm(plan=plan, schedule=schedule,
                initial={'kind': row['discount'].kind if row['discount'] else 'amount',
                         'value': row['discount'].value if row['discount'] else None},
                auto_id=f'line_discount_{plan.pk}_{index}_%s')
    return dict(payment_plan=plan, payment_schedule=schedule, discount_form=discount_form,
        bulk_discount_form=bulk_form, bulk_open=(form is not None and not single_error) or (form is None and request.GET.get('bulk') == '1'),
        payment_errors=form.errors if single_error and not any(row['edit_open'] for row in schedule['rows']) else None,
        discount_token=make_workflow_context(request, company, 'subscription-discount', plan), payment_return=return_url)



class SubscriptionPaymentsView(LoginRequiredMixin, StaffCompanyRequiredMixin, View):
    admin_only = True
    http_method_names = ('get', 'post')

    def plan(self, pk):
        return get_object_or_404(PatientSubscription.objects.filter(company=self.company, patient__company=self.company), pk=pk)

    def display(self, request, plan, *, form=None, schedule=None, status=200):
        return render(request, 'portal/subscription_payments.html', dict(company=self.company,
            active_membership=self.membership, nav_section='subscriptions', payment_open=True,
            **payment_popup(request, self.company, plan, form=form, schedule=schedule)), status=status)

    def get(self, request, pk):
        require_staff(self.company, request.user)
        return self.display(request, self.plan(pk))

    @transaction.atomic
    def post(self, request, pk):
        require_staff(self.company, request.user)
        plan = self.plan(pk)
        plan = PatientSubscription.objects.select_for_update(of=('self',)).get(pk=plan.pk)
        schedule = payment_schedule(plan)
        form = SubscriptionDiscountForm(request.POST, plan=plan, schedule=schedule)
        try:
            validate_workflow_context(request, self.company, 'subscription-discount', plan)
            if form.is_valid():
                data = form.cleaned_data
                targets = ([form.rows[data['payment']]] if data['scope'] == 'one' else
                           [form.rows[key] for key in data['payments']] if data['scope'] == 'selected' else [None])
                skipped = 0
                with transaction.atomic():
                    for row in targets:
                        rule, count = apply_discount(subscription=plan, actor=request.user, kind=data['kind'], value=data['value'],
                            effective_on=row['due_on'] if row else data['effective_on'], payment_on=row['due_on'] if row else None,
                            target_cycle=row['cycle'] if row else None, reason=data['reason'], request=request)
                        skipped += count
                messages.success(request, 'Discount saved. Future collections will use the updated amounts.' +
                    (f' {skipped} payment(s) already processing were left unchanged.' if skipped else ''))
                target = request.POST.get('return_to', '')
                parts = urlsplit(target)
                if parts.scheme or parts.netloc or parts.path != reverse('portal:treatment-subscriptions'):
                    return redirect('portal:subscription-payments', pk=plan.pk)
                query = [(key, value) for key, value in parse_qsl(parts.query) if key != 'payments']
                query.append(('payments', str(plan.pk)))
                return redirect(urlunsplit(('', '', parts.path, urlencode(query), f'subscription-payments-{plan.pk}')))
        except ValidationError as error:
            form.add_error(None, error)
        return self.display(request, plan, form=form, schedule=schedule, status=400)
