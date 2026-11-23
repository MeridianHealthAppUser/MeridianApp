"""Private purchase-order documents and explicitly submitted emails with PDF attachments."""

from hashlib import sha256
import json
import re
from smtplib import SMTPException
from urllib.parse import urlencode

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Prefetch
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse

from care.models import AuditEvent, PurchaseOrder, PurchaseOrderItem
from care.operations import _lock_company, require_operations_actor
from care.purchase_exports import purchase_order_pdf, purchase_order_workbook
from care.purchasing import person_name
from care.services import record_audit
from .operations_views import OperationsView
from .purchasing_views import PurchasingView
from .workflow_context import validate_workflow_context


ATTACHMENT_EXTENSIONS = ('pdf', 'xlsx', 'xls', 'csv', 'docx', 'doc', 'png', 'jpg', 'jpeg')
ATTACHMENT_TYPES = {
    'pdf': 'application/pdf', 'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'xls': 'application/vnd.ms-excel', 'csv': 'text/csv', 'doc': 'application/msword',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
}
MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # in total, including the purchase order PDF
MAX_RECIPIENTS = 10
TOO_LARGE = 'Attachments must total 10 MB or less, including the purchase order PDF.'


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """Several files from one input (Django's documented pattern); cleans to a list."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('widget', MultipleFileInput(attrs={'accept': ','.join(f'.{ext}' for ext in ATTACHMENT_EXTENSIONS)}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        values = data if isinstance(data, (list, tuple)) else [data]
        return [file for file in (super(MultipleFileField, self).clean(value, initial) for value in values) if file]


def _addresses(value, *, required):
    """Email addresses separated by commas, semicolons or new lines: each checked, duplicates dropped."""
    addresses, seen = [], set()
    for part in re.split(r'[,;\n]', value or ''):
        address = part.strip()
        if not address:
            continue
        try:
            validate_email(address)
        except ValidationError:
            raise ValidationError(f'“{address}” is not an email address.') from None
        if address.casefold() not in seen:
            seen.add(address.casefold())
            addresses.append(address)
    if required and not addresses:
        raise ValidationError('Enter at least one email address.')
    if len(addresses) > MAX_RECIPIENTS:
        raise ValidationError(f'Send to {MAX_RECIPIENTS} addresses or fewer.')
    return addresses


def uses_own_order_form(order):
    """The supplier's current setting: they only accept their own completed order form, so no PDF is attached."""
    return bool(order.supplier_id and order.supplier.uses_own_order_form)


def default_message(order, user=None):
    """The message the pop-up starts with; the sender edits it freely."""
    details = order.supplier_details or {}
    greeting = details.get('contact_name') or f"{details.get('name') or order.supplier_name or 'supplier'} team"
    when = f', for delivery by {order.eta:%A} {order.eta.day} {order.eta:%B %Y}' if order.eta else ''
    opening = (f'Please find our completed order form attached{when}.' if uses_own_order_form(order)
               else f'Please find attached purchase order {order.reference}{when}.')
    sign_off = [person_name(user)] if user is not None and user.is_authenticated else []
    return '\n'.join([f'Dear {greeting},', '', opening, '',
                      'Please confirm receipt of this order and let us know the delivery date and any tracking details.',
                      '', 'Kind regards,', *sign_off, order.company.name])


class PurchaseOrderEmailForm(forms.Form):
    to = forms.CharField(label='To', max_length=2000, help_text='Separate several addresses with commas.')
    cc = forms.CharField(label='CC', max_length=2000, required=False)
    bcc = forms.CharField(label='BCC', max_length=2000, required=False)
    subject = forms.CharField(label='Subject', max_length=255)
    message = forms.CharField(label='Message', required=False, max_length=5000,
                              widget=forms.Textarea(attrs={'rows': 9}))
    attachments = MultipleFileField(label='Attachments', required=False)

    def __init__(self, *args, order=None, user=None, **kwargs):
        initial = dict(kwargs.pop('initial', {}) or {})
        if order is not None:
            initial.setdefault('subject', f'Purchase order {order.reference} · {order.company.name}')
            initial.setdefault('to', order.supplier_email)
            initial.setdefault('message', default_message(order, user))
        super().__init__(*args, initial=initial, **kwargs)

    def clean_to(self):
        return _addresses(self.cleaned_data['to'], required=True)

    def clean_cc(self):
        return _addresses(self.cleaned_data['cc'], required=False)

    def clean_bcc(self):
        return _addresses(self.cleaned_data['bcc'], required=False)

    def clean_subject(self):
        value = self.cleaned_data['subject']
        if '\n' in value or '\r' in value:
            raise ValidationError('Use a single line for the subject.')
        return value

    def clean_attachments(self):
        files = self.cleaned_data['attachments']
        if len(files) > MAX_ATTACHMENTS:
            raise ValidationError(f'Attach up to {MAX_ATTACHMENTS} files.')
        for file in files:
            extension = file.name.rsplit('.', 1)[-1].lower() if '.' in file.name else ''
            if extension not in ATTACHMENT_EXTENSIONS:
                raise ValidationError(f'“{file.name}” can’t be attached. Use a PDF, Excel, Word, CSV, PNG or JPG file.')
        if sum(file.size for file in files) > MAX_ATTACHMENT_BYTES:
            raise ValidationError(TOO_LARGE)
        return files


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
        # The email pop-up previews the PDF in a same-origin frame. It holds no patient data and the email itself
        # records the document's fingerprint, so a preview is not an export.
        inline = self.extension == 'pdf' and request.GET.get('inline') == '1'
        try:
            output = self.build(order)
        except ValidationError as error:
            return HttpResponse(' '.join(error.messages), status=400, content_type='text/plain')
        try:
            if request.method != 'HEAD' and not inline:
                record_audit(company=self.company, actor=request.user, action=f'purchase_order.{self.extension}_exported',
                             target=order, request=request)
            response = FileResponse(output, as_attachment=not inline,
                                    filename=f'purchase-order-{order.reference}.{self.extension}',
                                    content_type=self.content_type)
            response['Cache-Control'] = 'private, no-store, max-age=0'
            response['X-Content-Type-Options'] = 'nosniff'
            if inline:
                response['X-Frame-Options'] = 'SAMEORIGIN'
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
def _send_order_email(*, request, company, pk, data, files=()):
    """Serialise manual sends with the practice lock and make a known successful submission replay-safe."""
    company = _lock_company(company, request.user)
    order = _order(company, pk, lock=True)
    if order.demo_data_set_id is not None:
        raise ValidationError('Demo purchase orders cannot be emailed. Download a preview instead.')
    context = validate_workflow_context(request, company, 'purchase-order-email', order, check_version=False)
    uploads = []
    for file in files:
        file.seek(0)
        content = file.read()
        extension = file.name.rsplit('.', 1)[-1].lower()
        uploads.append({'name': file.name, 'content': content, 'type': ATTACHMENT_TYPES[extension],
                        'size': len(content), 'sha256': sha256(content).hexdigest()})
    payload = {key: data[key] for key in ('to', 'cc', 'bcc', 'subject', 'message')}
    payload['attachments'] = [upload['sha256'] for upload in uploads]
    fingerprint = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    previous = AuditEvent.objects.filter(company=company, actor=request.user, action='purchase_order.emailed',
        target_type=PurchaseOrder._meta.label_lower, target_id=str(order.pk),
        metadata__submission_key=context['key']).first()
    if previous is not None:
        if previous.metadata.get('payload_sha256') != fingerprint:
            raise ValidationError('This email form was already used. Reload before sending another copy.')
        return order, False
    if not settings.EMAIL_ENABLED:
        raise ValidationError('Email is not configured. Set up the practice email service before sending purchase orders.')
    own_form = uses_own_order_form(order)
    document = None
    if not own_form:
        with purchase_order_pdf(order) as output:
            document = output.read()
    if sum(upload['size'] for upload in uploads) + len(document or b'') > MAX_ATTACHMENT_BYTES:
        raise ValidationError(TOO_LARGE)
    if document is None and not uploads:
        fallback = f'Our order reference is {order.reference}.'
    elif own_form:
        fallback = 'Please find our order attached.'
    else:
        fallback = f'Please find purchase order {order.reference} attached.'
    body = (data['message'].strip() or fallback) + '\n'
    email = EmailMessage(subject=data['subject'], body=body, from_email=settings.DEFAULT_FROM_EMAIL,
                         to=data['to'], cc=data['cc'], bcc=data['bcc'],
                         reply_to=[request.user.email] if request.user.email else None)
    if document is not None:
        email.attach(f'purchase-order-{order.reference}.pdf', document, 'application/pdf')
    for upload in uploads:
        email.attach(upload['name'], upload['content'], upload['type'])
    # Write before SMTP so an audit failure cannot send an unrecorded email. A delivery failure rolls this back.
    record_audit(company=company, actor=request.user, action='purchase_order.emailed', target=order, request=request,
                 metadata={'submission_key': context['key'], 'payload_sha256': fingerprint,
                           'recipient': ', '.join(data['to']),
                           'recipients': {'to': data['to'], 'cc': data['cc'], 'bcc': data['bcc']},
                           'uses_own_order_form': own_form,
                           'document_sha256': sha256(document).hexdigest() if document is not None else None,
                           'attachments': [{key: upload[key] for key in ('name', 'size', 'sha256')} for upload in uploads],
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
        form = PurchaseOrderEmailForm(request.POST, request.FILES, order=order, user=request.user)
        if form.is_valid():
            try:
                order, sent = _send_order_email(request=request, company=self.company, pk=pk, data=form.cleaned_data,
                                                files=form.cleaned_data['attachments'])
                messages.success(request, _email_success_message(order, ', '.join(form.cleaned_data['to']), sent))
                return redirect(_return_url(order, request.POST))
            except ValidationError as error:
                form.add_error(None, error)
            except (SMTPException, OSError, ValueError):
                form.add_error(None, 'The purchase order could not be emailed. Check the email service and try again.')
        return self.display(request, email_order=order, email_form=form, status=400)
