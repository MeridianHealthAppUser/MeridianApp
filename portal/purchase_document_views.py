"""Private purchase-order documents and explicitly submitted emails with PDF attachments."""

from hashlib import sha256
import json
from smtplib import SMTPException
from urllib.parse import urlencode

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.db import transaction
from django.db.models import Prefetch
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse

from care.models import AuditEvent, PurchaseOrder, PurchaseOrderItem
from care.operations import _lock_company, require_operations_actor
from care.purchase_exports import purchase_order_pdf, purchase_order_workbook
from care.services import record_audit
from .operations_views import OperationsView
from .purchasing_views import PurchasingView
from .workflow_context import validate_workflow_context


class PurchaseOrderEmailForm(forms.Form):
    recipient = forms.EmailField(label='Recipient', max_length=254)
    subject = forms.CharField(label='Subject', max_length=255)
    message = forms.CharField(label='Message', required=False, max_length=5000,
                              widget=forms.Textarea(attrs={'rows': 4}))

    def __init__(self, *args, order=None, **kwargs):
        initial = dict(kwargs.pop('initial', {}) or {})
        if order is not None:
            initial.setdefault('subject', f'Purchase order {order.reference}')
            initial.setdefault('recipient', order.supplier_email)
        super().__init__(*args, initial=initial, **kwargs)

    def clean_subject(self):
        value = self.cleaned_data['subject']
        if '\n' in value or '\r' in value:
            raise ValidationError('Use a single line for the subject.')
        return value


def _order(company, pk, *, lock=False):
    items = PurchaseOrderItem.objects.filter(company=company, product__company=company).select_related('product').order_by('pk')
    queryset = PurchaseOrder.objects.filter(company=company).select_related('company', 'created_by').prefetch_related(
        Prefetch('items', queryset=items))
    if lock:
        queryset = queryset.select_for_update(of=('self',))
    return get_object_or_404(queryset, pk=pk)


class _PurchaseOrderDocumentView(OperationsView):
    nav_section, page_title = 'purchasing', 'Purchase order'
    shipping = False
    http_method_names = ('get', 'head', 'options')

    def get(self, request, pk):
        require_operations_actor(self.company, request.user)
        order = _order(self.company, pk)
        try:
            output = self.build(order)
        except ValidationError as error:
            return HttpResponse(' '.join(error.messages), status=400, content_type='text/plain')
        try:
            if request.method != 'HEAD':
                record_audit(company=self.company, actor=request.user, action=f'purchase_order.{self.extension}_exported',
                             target=order, request=request)
            response = FileResponse(output, as_attachment=True, filename=f'purchase-order-{order.reference}.{self.extension}',
                                    content_type=self.content_type)
            response['Cache-Control'] = 'private, no-store, max-age=0'
            response['X-Content-Type-Options'] = 'nosniff'
            return response
        except Exception:
            output.close()
            raise


class PurchaseOrderExcelView(_PurchaseOrderDocumentView):
    extension = 'xlsx'
    content_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    build = staticmethod(purchase_order_workbook)


class PurchaseOrderPDFView(_PurchaseOrderDocumentView):
    extension = 'pdf'
    content_type = 'application/pdf'
    build = staticmethod(purchase_order_pdf)


@transaction.atomic
def _send_order_email(*, request, company, pk, data):
    """Serialise manual sends with the practice lock and make a known successful submission replay-safe."""
    company = _lock_company(company, request.user)
    order = _order(company, pk, lock=True)
    if order.demo_data_set_id is not None:
        raise ValidationError('Demo purchase orders cannot be emailed. Download a preview instead.')
    context = validate_workflow_context(request, company, 'purchase-order-email', order, check_version=False)
    fingerprint = sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    previous = AuditEvent.objects.filter(company=company, actor=request.user, action='purchase_order.emailed',
        target_type=PurchaseOrder._meta.label_lower, target_id=str(order.pk),
        metadata__submission_key=context['key']).first()
    if previous is not None:
        if previous.metadata.get('payload_sha256') != fingerprint:
            raise ValidationError('This email form was already used. Reload before sending another copy.')
        return order, False
    if not settings.EMAIL_ENABLED:
        raise ValidationError('Email is not configured. Set up the practice email service before sending purchase orders.')
    with purchase_order_pdf(order) as output:
        attachment = output.read()
    text = data['message'] or 'Please find the purchase order attached.'
    body = f'{text}\n\nPurchase order {order.reference}\n{company.name}\n'
    email = EmailMessage(subject=data['subject'], body=body, from_email=settings.DEFAULT_FROM_EMAIL,
                         to=[data['recipient']])
    email.attach(f'purchase-order-{order.reference}.pdf', attachment, 'application/pdf')
    # Write before SMTP so an audit failure cannot send an unrecorded email. A delivery failure rolls this back.
    record_audit(company=company, actor=request.user, action='purchase_order.emailed', target=order, request=request,
                 metadata={'submission_key': context['key'], 'payload_sha256': fingerprint,
                           'recipient': data['recipient'], 'document_sha256': sha256(attachment).hexdigest(),
                           'delivery_backend': settings.EMAIL_BACKEND})
    if email.send(fail_silently=False) != 1:
        raise ValidationError('The email was not sent. Check the email service and try again.')
    return order, True


def _return_url(order, data):
    query = {'view': 'history' if order.status == PurchaseOrder.Status.RECEIVED else 'on-order', 'order': order.pk}
    if data.get('group') in ('orders', 'products'):
        query['group'] = data['group']
    return f'{reverse("portal:purchasing")}?{urlencode(query)}'


def _email_success_message(order, recipient, sent):
    prepared = {
        'django.core.mail.backends.console.EmailBackend': 'the development server console',
        'django.core.mail.backends.filebased.EmailBackend': 'the development email files',
    }.get(settings.EMAIL_BACKEND)
    if prepared:
        action = 'prepared' if sent else 'already prepared from this submission'
        return f'{order.reference} {action} in {prepared}; email delivery requires SMTP.'
    return (f'{order.reference} emailed to {recipient}.' if sent else
            f'{order.reference} was already emailed from this submission.')


class PurchaseOrderEmailView(PurchasingView):
    http_method_names = ('post', 'options')

    def post(self, request, pk):
        require_operations_actor(self.company, request.user)
        order = _order(self.company, pk)
        form = PurchaseOrderEmailForm(request.POST, order=order)
        if form.is_valid():
            try:
                order, sent = _send_order_email(request=request, company=self.company, pk=pk, data=form.cleaned_data)
                messages.success(request, _email_success_message(order, form.cleaned_data['recipient'], sent))
                return redirect(_return_url(order, request.POST))
            except ValidationError as error:
                form.add_error(None, error)
            except (SMTPException, OSError, ValueError):
                form.add_error(None, 'The purchase order could not be emailed. Check the email service and try again.')
        return self.display(request, email_order=order, email_form=form, status=400)
