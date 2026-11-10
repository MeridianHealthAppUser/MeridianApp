"""Read-only subscription and billing tables for the shared Excel endpoint."""
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.utils import timezone

from care.models import SubscriptionCycle
from .billing_views import billing_cycle_rows, billing_detail_plan, billing_listing, billing_other_payments
from .export_support import ExportTable
from .treatment_views import prepare_subscription_rows, subscription_listing


TABLES = frozenset(('subscriptions', 'billing', 'billing-cycles', 'billing-payments'))
SAST = ZoneInfo('Africa/Johannesburg')
BILLING_LABELS = {'paid': 'Paid up', 'failed': 'Failed', 'retrying': 'Retrying', 'due': 'Due',
                  'update': 'New amount', 'pending': 'Awaiting approval', 'no_mandate': 'No debit order'}


def billing_table(table, request, company, membership):
    """Return every matching row; pagination and presentation controls never restrict a download."""
    if table not in TABLES:
        return None
    if (membership is None or not membership.is_admin or not membership.is_active
            or membership.company_id != company.pk or membership.user_id != request.user.pk):
        raise PermissionDenied('This export is for practice administrators.')
    if table == 'subscriptions':
        return _subscriptions(request, company)
    if table == 'billing':
        return _billing(request, company)
    try:
        pk = int(request.GET.get('pk', ''))
    except (TypeError, ValueError):
        raise Http404('Choose a subscription from this practice.') from None
    plan = billing_detail_plan(company, pk)
    return _cycles(plan, company) if table == 'billing-cycles' else _payments(plan, company)


def _date(value):
    return timezone.localdate(value, SAST) if value is not None else None


def _subscriptions(request, company):
    form, plans = subscription_listing(request.GET, company)
    if not form.is_valid():
        raise ValidationError('Correct the subscription filters before exporting.')

    def rows():
        for plan in prepare_subscription_rows(plans.iterator(chunk_size=500), company):
            dose = plan.dose
            amount = dose.price if dose is not None else None
            yield (str(plan.patient), plan.plan_name, plan.get_status_display(),
                   f'{plan.supply_dispatched} dispatched · {plan.supply_remaining} remaining',
                   plan.next_shipment.scheduled_for if plan.next_shipment else None,
                   plan.next_shipment.get_status_display() if plan.next_shipment else 'None scheduled',
                   plan.starts_on, plan.review_due_on, amount,
                   dose.billing_text if amount is not None else 'Not in the catalogue',
                   plan.payment_progress['paid_count'], plan.payment_progress['total_count'], plan.payment_progress['paid_total'])

    return ExportTable('Subscriptions',
        ('Patient', 'Plan', 'Status', 'Supply progress', 'Next shipment', 'Next shipment status',
         'Started', 'Review due', 'Reference amount', 'Reference billing interval',
         'Payments settled', 'Scheduled payments', 'Paid to date'), rows(),
        widths=(30, 42, 18, 36, 20, 25, 18, 18, 22, 28, 20, 20, 22))


def _billing(request, company):
    form, plans = billing_listing(request.GET, company)
    if not form.is_valid():
        raise ValidationError('Correct the billing filters before exporting.')

    def rows():
        for plan in plans:
            mandate, paid = plan.mandate, plan.last_paid
            yield (str(plan.patient), plan.get_status_display(), BILLING_LABELS.get(plan.state, plan.state), plan.state_text,
                   mandate.get_method_display() if mandate else '', mandate.get_status_display() if mandate else '',
                   mandate.account_label if mandate else '', mandate.amount if mandate else None,
                   paid.amount if paid else None, _date(paid.paid_at) if paid else None,
                   plan.next_debit_on, plan.owed)

    return ExportTable('Billing',
        ('Patient', 'Subscription status', 'Billing', 'Billing detail', 'Debit order', 'Debit order status',
         'Account', 'Debit order limit', 'Last paid', 'Last paid date', 'Next debit', 'Outstanding'), rows(),
        widths=(30, 22, 24, 42, 28, 28, 30, 22, 18, 20, 20, 20))


def _cycle_status(cycle):
    if cycle.status == SubscriptionCycle.Status.PAID:
        return 'Paid', '', _date(cycle.paid_at), None
    if cycle.status == SubscriptionCycle.Status.FAILED:
        return 'Failed', 'In the failure queue', None, None
    if cycle.attempts:
        return 'Retrying', '', None, cycle.next_attempt_on
    if cycle.status == SubscriptionCycle.Status.CANCELLED:
        return 'Cancelled', '', None, None
    return 'Due', cycle.failure_reason, None, None


def _attempt_text(payment):
    method = payment.get_method_display() or payment.get_kind_display()
    text = f'{payment.attempt}. {method} · {payment.get_status_display()}'
    if payment.failure_reason:
        text += f': {payment.failure_reason}'
    if payment.recorded_by:
        text += f' · by {payment.recorded_by.full_name}'
    return text


def _cycles(plan, company):
    def rows():
        for cycle in billing_cycle_rows(plan, company):
            invoice = getattr(cycle, 'invoice', None)
            label, detail, paid_on, next_attempt = _cycle_status(cycle)
            yield (cycle.cycle_number,
                   invoice.invoice_number if invoice and invoice.company_id == company.pk and invoice.patient_id == plan.patient_id else '',
                   cycle.starts_on, cycle.ends_on or cycle.starts_on, cycle.amount, label, paid_on, next_attempt, detail,
                   '\n'.join(_attempt_text(payment) for payment in cycle.attempt_rows))

    return ExportTable('Billing cycles',
        ('Cycle', 'Invoice', 'Period start', 'Period end', 'Amount', 'Status', 'Paid date', 'Next attempt',
         'Status details', 'Attempts'), rows(), widths=(12, 24, 20, 20, 18, 18, 20, 20, 42, 70))


def _payments(plan, company):
    rows = ((_date(payment.paid_at or payment.created_at), payment.get_kind_display() or 'Payment', payment.note,
             payment.amount, payment.get_status_display())
            for payment in billing_other_payments(plan, company).iterator(chunk_size=500))
    return ExportTable('Other payments', ('Date', 'For', 'Note', 'Amount', 'Status'), rows,
                       widths=(20, 32, 60, 18, 18))
