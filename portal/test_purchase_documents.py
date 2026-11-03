"""Private purchase documents and explicit, replay-safe PDF emails never receive stock."""

from datetime import timedelta
from io import BytesIO
import json
import re
from smtplib import SMTPException
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from django.core import mail
from django.test import Client, RequestFactory, SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook
from pypdf import PdfReader

from care.models import AuditEvent, PurchaseOrder, PurchaseOrderItem, StockMovement, Supplier, SupplierProduct
from practices.models import CompanyMembership
from .purchase_document_views import PurchaseOrderEmailForm, _email_success_message
from .test_operations import OperationsPortalFixture
from .workflow_context import make_workflow_context


@override_settings(EMAIL_ENABLED=True, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class PurchaseDocumentTests(OperationsPortalFixture):
    def setUp(self):
        super().setUp()
        self.order = self.order_for(self.company, self.product, 1)
        self.other_order = self.order_for(self.company, self.open_product, 2)
        self.foreign_order = self.order_for(self.beta, self.beta_product, 1)
        self.movements = list(StockMovement.objects.order_by('pk').values())

    def order_for(self, company, product, number):
        supplier, _ = Supplier.objects.get_or_create(company=company, name='Saved supplier name',
                                                     defaults={'email': 'saved-supplier@example.test'})
        code = f'SAVED-{product.pk:03d}'
        SupplierProduct.objects.get_or_create(company=company, supplier=supplier, product=product,
                                              defaults={'supplier_code': code})
        order = PurchaseOrder.objects.create(company=company, created_by=self.admin, number=number,
            submission_key=uuid4(), eta=self.today + timedelta(days=7), supplier=supplier,
            supplier_name=supplier.name, supplier_email=supplier.email)
        PurchaseOrderItem.objects.create(company=company, purchase_order=order, product=product,
            product_name='Saved purchase product', strength='2.0 mg', category=product.category,
            supplier_code=code, quantity=5, quantity_received=2)
        return order

    def url(self, kind, order=None):
        return reverse(f'portal:purchase-order-{kind}', args=[(order or self.order).pk])

    def email_token(self, *, order=None, actor=None, company=None, kind='purchase-order-email'):
        request = RequestFactory().get('/')
        request.user = actor or self.admin
        return make_workflow_context(request, company or self.company, kind, order or self.order)

    def data(self, *, token=None, **extra):
        return {'workflow_context': token if token is not None else self.email_token(),
                'recipient': 'purchasing@example.test', 'subject': f'Purchase order {self.order.reference}',
                'message': 'Please arrange this delivery.', 'view': 'on-order', 'group': 'orders', **extra}

    def sent_audits(self):
        return AuditEvent.objects.filter(action='purchase_order.emailed')

    def assert_email_error(self, response):
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context['email_form'].errors)
        self.assertEqual(response.context['email_order'].pk, self.order.pk)
        self.assertRegex(response.content.decode(),
            rf'<dialog[^>]*id="purchase-order-email-{self.order.pk}"[^>]*\bopen\b')

    def test_downloads_are_real_private_files_with_snapshots_and_progress(self):
        self.product.name = 'Later catalogue wording'
        self.product.save(update_fields=('name', 'updated_at'))
        Supplier.objects.filter(pk=self.order.supplier_id).update(name='Later supplier name', email='later@example.test')
        SupplierProduct.objects.filter(supplier_id=self.order.supplier_id, product=self.product).update(supplier_code='LATER-CODE')
        for kind, content_type in (('excel', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
                                    ('pdf', 'application/pdf')):
            with self.subTest(kind=kind):
                response = self.client.get(self.url(kind))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Content-Type'], content_type)
                self.assertIn(self.order.reference, response['Content-Disposition'])
                self.assertIn('no-store', response['Cache-Control'])
                self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
                content = b''.join(response.streaming_content)
                response.close()
                if kind == 'excel':
                    workbook = load_workbook(BytesIO(content))
                    self.assertEqual(tuple(workbook.active.cell(8, column).value for column in (1, 5, 6, 7)),
                                     ('Saved purchase product', 5, 2, 3))
                    self.assertEqual(workbook.active['B5'].value, 'Saved supplier name')
                    self.assertEqual(workbook.active['E5'].value, 'saved-supplier@example.test')
                    self.assertEqual(workbook.active['D8'].value, f'SAVED-{self.product.pk:03d}')
                    workbook.close()
                else:
                    self.assertTrue(content.startswith(b'%PDF-'))
                    text = PdfReader(BytesIO(content)).pages[0].extract_text()
                    self.assertIn('Saved purchase product', text)
                    self.assertIn('Saved supplier name', text)
                    self.assertIn(f'SAVED-{self.product.pk:03d}', text)
                    self.assertNotIn('Later catalogue wording', text)
                    self.assertNotIn('Later supplier name', text)
                    self.assertNotIn('LATER-CODE', text)
        self.assertEqual(list(StockMovement.objects.order_by('pk').values()), self.movements)
        self.assertEqual(self.order.items.get().quantity_received, 2)

    def test_head_does_not_record_download_or_email_events(self):
        before = AuditEvent.objects.count()
        for kind in ('excel', 'pdf'):
            response = self.client.head(self.url(kind))
            self.assertEqual(response.status_code, 200)
            response.close()
        self.assertEqual(AuditEvent.objects.count(), before)
        self.assertFalse(mail.outbox)

    def test_email_popup_defaults_to_recorded_supplier_address_without_sending(self):
        Supplier.objects.filter(pk=self.order.supplier_id).update(email='later@example.test')
        response = self.client.get(reverse('portal:purchasing'), {'view': 'on-order', 'email': self.order.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['email_form']['recipient'].value(), 'saved-supplier@example.test')
        self.assertEqual(response.context['email_form']['subject'].value(), f'Purchase order {self.order.reference}')
        self.assertFalse(mail.outbox)
        self.assertFalse(self.sent_audits().exists())

    def test_documents_and_email_are_admin_only_and_foreign_records_are_not_found(self):
        for kind in ('excel', 'pdf'):
            self.assertEqual(self.client.get(self.url(kind, self.foreign_order)).status_code, 404)
        self.assertEqual(self.client.post(self.url('email', self.foreign_order), self.data()).status_code, 404)
        for actor in (self.doctor, self.patient_user):
            self.login(actor)
            for kind in ('excel', 'pdf'):
                self.assertEqual(self.client.get(self.url(kind)).status_code, 403)
            self.assertEqual(self.client.post(self.url('email'), self.data()).status_code, 403)
        self.login(self.super_admin)
        response = self.client.get(self.url('pdf'))
        self.assertEqual(response.status_code, 200)
        response.close()
        self.login()
        CompanyMembership.objects.filter(company=self.company, user=self.admin).update(is_active=False)
        self.assertEqual(self.client.get(self.url('pdf')).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(self.url('pdf')).status_code, 302)
        self.assertFalse(mail.outbox)

    def test_wrong_methods_cannot_send_or_modify_an_order(self):
        self.assertEqual(self.client.get(self.url('email')).status_code, 405)
        self.assertEqual(self.client.head(self.url('email')).status_code, 405)
        for kind in ('excel', 'pdf'):
            self.assertEqual(self.client.post(self.url(kind), {}).status_code, 405)
        self.assertFalse(mail.outbox)

    def test_email_attaches_a_pdf_and_only_records_the_explicit_submission(self):
        original_updated = self.order.updated_at
        response = self.client.post(self.url('email'), self.data(group='products'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(parse_qs(urlsplit(response['Location']).query),
                         {'view': ['on-order'], 'order': [str(self.order.pk)], 'group': ['products']})
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, ['purchasing@example.test'])
        self.assertEqual(email.subject, f'Purchase order {self.order.reference}')
        self.assertIn('Please arrange this delivery.', email.body)
        self.assertEqual(len(email.attachments), 1)
        name, content, mimetype = email.attachments[0]
        self.assertEqual((name, mimetype), (f'purchase-order-{self.order.reference}.pdf', 'application/pdf'))
        self.assertIn('Saved purchase product', PdfReader(BytesIO(content)).pages[0].extract_text())
        audit = self.sent_audits().get()
        self.assertEqual((audit.company, audit.actor, audit.target_id),
                         (self.company, self.admin, str(self.order.pk)))
        self.assertNotIn('Please arrange this delivery.', json.dumps(audit.metadata))
        self.order.refresh_from_db()
        self.assertEqual((self.order.updated_at, self.order.status), (original_updated, PurchaseOrder.Status.OPEN))
        self.assertEqual(list(StockMovement.objects.order_by('pk').values()), self.movements)

    def test_same_submission_is_idempotent_and_cannot_be_reused_for_another_recipient(self):
        data = self.data(message='')
        self.assertEqual(self.client.post(self.url('email'), data).status_code, 302)
        self.assertEqual(self.client.post(self.url('email'), data).status_code, 302)
        self.assertEqual((len(mail.outbox), self.sent_audits().count()), (1, 1))
        self.assertIn('Please find the purchase order attached.', mail.outbox[0].body)
        response = self.client.post(self.url('email'), {**data, 'recipient': 'another@example.test'})
        self.assert_email_error(response)
        self.assertEqual(len(mail.outbox), 1)

    def test_signed_context_is_bound_to_company_actor_order_and_email_kind(self):
        tokens = ('', 'tampered', self.email_token(order=self.other_order), self.email_token(actor=self.super_admin),
                  self.email_token(company=self.beta), self.email_token(kind='purchase-order-receive'))
        for token in tokens:
            with self.subTest(token=token):
                self.assert_email_error(self.client.post(self.url('email'), self.data(token=token)))
        self.assertFalse(mail.outbox)
        self.assertFalse(self.sent_audits().exists())

    def test_email_uses_latest_receipt_data_with_a_previously_rendered_signed_form(self):
        data = self.data()
        PurchaseOrder.objects.filter(pk=self.order.pk).update(updated_at=timezone.now() + timedelta(seconds=1))
        self.order.items.update(quantity_received=3)
        response = self.client.post(self.url('email'), data)
        self.assertEqual(response.status_code, 302)
        text = PdfReader(BytesIO(mail.outbox[0].attachments[0][1])).pages[0].extract_text()
        self.assertRegex(text, re.compile(r'Saved purchase product.*?5\s+3\s+2', re.S))

    def test_invalid_recipient_subject_or_message_reopens_the_email_dialog_without_sending(self):
        for extra in ({'recipient': ''}, {'recipient': 'not an email'}, {'subject': ''},
                      {'subject': 'PO\nBcc: secret@example.test'}, {'message': 'x' * 5001}):
            with self.subTest(extra=extra):
                self.assert_email_error(self.client.post(self.url('email'), self.data(**extra)))
        self.assertFalse(mail.outbox)
        self.assertFalse(self.sent_audits().exists())
        form = PurchaseOrderEmailForm(order=self.order)
        self.assertEqual(form.initial['subject'], f'Purchase order {self.order.reference}')
        self.assertEqual(form.initial['recipient'], 'saved-supplier@example.test')
        self.assertFalse(form.fields['message'].required)

    def test_delivery_failure_rolls_back_audit_and_allows_explicit_retry(self):
        data = self.data()
        with patch('portal.purchase_document_views.EmailMessage.send', side_effect=SMTPException('Unavailable')):
            self.assert_email_error(self.client.post(self.url('email'), data))
        self.assertFalse(self.sent_audits().exists())
        self.assertFalse(mail.outbox)
        self.assertEqual(self.client.post(self.url('email'), data).status_code, 302)
        self.assertEqual((len(mail.outbox), self.sent_audits().count()), (1, 1))

    def test_disabled_email_and_zero_deliveries_are_not_recorded_as_sent(self):
        with override_settings(EMAIL_ENABLED=False):
            self.assert_email_error(self.client.post(self.url('email'), self.data()))
        with patch('portal.purchase_document_views.EmailMessage.send', return_value=0):
            self.assert_email_error(self.client.post(self.url('email'), self.data()))
        self.assertFalse(self.sent_audits().exists())
        self.assertFalse(mail.outbox)

    def test_audit_failure_cannot_send_an_unrecorded_email(self):
        with patch('portal.purchase_document_views.record_audit', side_effect=RuntimeError('Unavailable')):
            with self.assertRaises(RuntimeError):
                self.client.post(self.url('email'), self.data())
        self.assertFalse(mail.outbox)
        self.assertFalse(self.sent_audits().exists())

    def test_email_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        self.login(client=client)
        response = client.post(self.url('email'), self.data())
        self.assertEqual(response.status_code, 403)
        self.assertFalse(mail.outbox)
        response = client.get(reverse('portal:purchasing'), {'view': 'on-order', 'email': self.order.pk})
        self.assertEqual(response.status_code, 200)
        response = client.post(self.url('email'), {**self.data(), 'csrfmiddlewaretoken': client.cookies['csrftoken'].value})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)

    def test_received_order_email_returns_to_history(self):
        PurchaseOrder.objects.filter(pk=self.order.pk).update(status=PurchaseOrder.Status.RECEIVED, received_at=timezone.now())
        self.order.items.update(quantity_received=5)
        self.order.refresh_from_db()
        response = self.client.post(self.url('email'), self.data(group='products'))
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlsplit(response['Location']).query)
        self.assertEqual((query['view'], query['group']), (['history'], ['products']))


class PurchaseEmailConfirmationTests(SimpleTestCase):
    order = SimpleNamespace(reference='PO-00001')

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.console.EmailBackend')
    def test_console_output_is_not_reported_as_email_delivery(self):
        for sent in (True, False):
            text = _email_success_message(self.order, 'recipient@example.test', sent)
            self.assertIn('prepared', text)
            self.assertIn('development server console', text)
            self.assertIn('email delivery requires SMTP', text)
            self.assertNotIn('emailed', text)

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.filebased.EmailBackend')
    def test_file_output_is_reported_as_prepared(self):
        text = _email_success_message(self.order, 'recipient@example.test', True)
        self.assertIn('prepared in the development email files', text)
        self.assertIn('email delivery requires SMTP', text)

    def test_smtp_and_in_memory_test_backend_keep_the_sent_confirmation(self):
        for backend in ('smtp', 'locmem'):
            with self.subTest(backend=backend), override_settings(EMAIL_BACKEND=f'django.core.mail.backends.{backend}.EmailBackend'):
                self.assertEqual(_email_success_message(self.order, 'recipient@example.test', True),
                                 'PO-00001 emailed to recipient@example.test.')
                self.assertEqual(_email_success_message(self.order, 'recipient@example.test', False),
                                 'PO-00001 was already emailed from this submission.')
