"""Private purchase documents and explicit, replay-safe PDF emails never receive stock."""

from datetime import timedelta
from decimal import Decimal
from io import BytesIO
import json
import re
from smtplib import SMTPException
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
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
                'to': 'purchasing@example.test', 'subject': f'Purchase order {self.order.reference}',
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
                if kind == 'excel':
                    workbook = load_workbook(BytesIO(content))
                    self.assertEqual(tuple(workbook.active.cell(8, column).value for column in (1, 8, 10, 11)),
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
            self.assertEqual(b''.join(response.streaming_content), b'')
        self.assertEqual(AuditEvent.objects.count(), before)
        self.assertFalse(mail.outbox)

    def test_email_popup_defaults_to_recorded_supplier_address_without_sending(self):
        Supplier.objects.filter(pk=self.order.supplier_id).update(email='later@example.test')
        response = self.client.get(reverse('portal:purchasing'), {'view': 'on-order', 'email': self.order.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['email_form']['to'].value(), 'saved-supplier@example.test')
        self.assertEqual(response.context['email_form']['subject'].value(),
                         f'Purchase order {self.order.reference} · {self.company.name}')
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
        self.assertTrue(b''.join(response.streaming_content).startswith(b'%PDF'))
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
        self.assertEqual(email.subject, f'Purchase order {self.order.reference}')  # as typed in the pop-up
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
        self.assertIn(f'Please find purchase order {self.order.reference} attached.', mail.outbox[0].body)
        response = self.client.post(self.url('email'), {**data, 'to': 'another@example.test'})
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
        # The supplier's PDF is built when it is sent; receipt progress stays in Meridian (and the Excel export).
        self.assertRegex(text, re.compile(r'SAVED-\d{3}\s+Saved purchase product 2\.0 mg.*?\b5\b', re.S))
        self.assertNotIn('Received', text)

    def test_invalid_recipient_subject_or_message_reopens_the_email_dialog_without_sending(self):
        for extra in ({'to': ''}, {'to': 'not an email'}, {'cc': 'nope'}, {'subject': ''},
                      {'subject': 'PO\nBcc: secret@example.test'}, {'message': 'x' * 5001}):
            with self.subTest(extra=extra):
                self.assert_email_error(self.client.post(self.url('email'), self.data(**extra)))
        self.assertFalse(mail.outbox)
        self.assertFalse(self.sent_audits().exists())
        form = PurchaseOrderEmailForm(order=self.order)
        self.assertEqual(form.initial['subject'], f'Purchase order {self.order.reference} · {self.company.name}')
        self.assertEqual(form.initial['to'], 'saved-supplier@example.test')
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

    def test_multiple_recipients_cc_bcc_and_reply_to_the_sender(self):
        response = self.client.post(self.url('email'), self.data(to='a@example.test; b@example.test, a@example.test',
                                                                  cc='c@example.test', bcc='d@example.test'))
        self.assertEqual(response.status_code, 302)
        sent = mail.outbox[0]
        self.assertEqual((sent.to, sent.cc, sent.bcc, sent.reply_to),
                         (['a@example.test', 'b@example.test'], ['c@example.test'], ['d@example.test'], [self.admin.email]))
        self.assertEqual(self.sent_audits().get().metadata['recipients'],
                         {'to': ['a@example.test', 'b@example.test'], 'cc': ['c@example.test'], 'bcc': ['d@example.test']})

    def test_one_invalid_address_names_it(self):
        response = self.client.post(self.url('email'), self.data(to='a@example.test, nope'))
        self.assert_email_error(response)
        self.assertIn('nope', str(response.context['email_form'].errors['to']))
        many = ', '.join(f'p{index}@example.test' for index in range(11))
        self.assert_email_error(self.client.post(self.url('email'), self.data(to=many)))

    def test_uploaded_files_are_attached_with_the_po_and_fingerprinted(self):
        upload = SimpleUploadedFile('their-form.pdf', b'%PDF-1.4 test', content_type='application/pdf')
        self.assertEqual(self.client.post(self.url('email'), {**self.data(), 'attachments': [upload]}).status_code, 302)
        attachments = mail.outbox[0].attachments
        self.assertEqual([name for name, *_ in attachments], [f'purchase-order-{self.order.reference}.pdf', 'their-form.pdf'])
        self.assertEqual(attachments[1][1], b'%PDF-1.4 test')
        meta = self.sent_audits().get().metadata
        self.assertEqual((meta['attachments'][0]['name'], meta['attachments'][0]['size']), ('their-form.pdf', 13))
        self.assertEqual(len(meta['attachments'][0]['sha256']), 64)
        self.assertEqual(len(meta['document_sha256']), 64)
        self.assertFalse(meta['uses_own_order_form'])

    def test_supplier_generated_sends_without_meridian_pdf_even_with_nothing_attached(self):
        Supplier.objects.filter(pk=self.order.supplier_id).update(uses_own_order_form=True)
        self.assertEqual(self.client.post(self.url('email'), self.data(message='')).status_code, 302)
        self.assertEqual(mail.outbox[0].attachments, [])
        self.assertIn(f'Our order reference is {self.order.reference}.', mail.outbox[0].body)
        self.assertNotIn('attached', mail.outbox[0].body)  # nothing is attached, so the email must not say so
        meta = self.sent_audits().get().metadata
        self.assertIsNone(meta['document_sha256'])
        self.assertTrue(meta['uses_own_order_form'])

    def test_supplier_generated_sends_their_uploaded_form_only(self):
        Supplier.objects.filter(pk=self.order.supplier_id).update(uses_own_order_form=True)
        upload = SimpleUploadedFile('Bio-Swiss order form.pdf', b'%PDF-1.4 form', content_type='application/pdf')
        self.client.post(self.url('email'), {**self.data(), 'attachments': [upload]})
        self.assertEqual([name for name, *_ in mail.outbox[0].attachments], ['Bio-Swiss order form.pdf'])

    def test_bad_uploads_reopen_the_dialog_and_send_nothing(self):
        exe = SimpleUploadedFile('tool.exe', b'MZ', content_type='application/octet-stream')
        big = SimpleUploadedFile('big.pdf', b'%PDF' + b'0' * (10 * 1024 * 1024), content_type='application/pdf')
        six = [SimpleUploadedFile(f'f{index}.pdf', b'%PDF', content_type='application/pdf') for index in range(6)]
        for files in ([exe], [big], six):
            with self.subTest(count=len(files), name=files[0].name):
                response = self.client.post(self.url('email'), {**self.data(), 'attachments': files})
                self.assert_email_error(response)
                self.assertEqual(list(response.context['email_form'].errors), ['attachments'])
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(self.sent_audits().exists())

    def test_dialog_defaults_to_supplier_orders_email_and_a_filled_message(self):
        response = self.client.get(reverse('portal:purchasing'), {'view': 'on-order', 'email': self.order.pk})
        form = response.context['email_form']
        self.assertEqual(form.initial['to'], 'saved-supplier@example.test')
        self.assertTrue(form.initial['message'].startswith('Dear Saved supplier name team,'))
        self.assertIn(f'Please find attached purchase order {self.order.reference}, for delivery by', form.initial['message'])
        self.assertIn('Please confirm receipt of this order', form.initial['message'])
        self.assertIn(self.company.name, form.initial['message'])
        self.assertFalse(response.context['email_own_form'])
        self.assertIn('?inline=1', response.context['email_pdf_url'])

    def email_dialog_html(self):
        response = self.client.get(reverse('portal:purchasing'), {'view': 'on-order', 'email': self.order.pk})
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_meridian_supplier_dialog_locks_the_pdf_and_frames_the_preview(self):
        html = self.email_dialog_html()
        self.assertIn('enctype="multipart/form-data"', html)
        self.assertIn(f'purchase-order-{self.order.reference}.pdf', html)
        self.assertIn('always attached', html)
        self.assertRegex(html, r'<iframe[^>]+src="[^"]*order\.pdf\?inline=1(#[^"]*)?"')
        self.assertIn('Open PDF in new tab', html)
        self.assertIn('1 product · 5 units', html)
        self.assertNotIn('uses their own order form', html)

    def test_supplier_generated_dialog_shows_the_notice_and_upload_zone(self):
        Supplier.objects.filter(pk=self.order.supplier_id).update(uses_own_order_form=True)
        html = self.email_dialog_html()
        self.assertIn('Saved supplier name uses their own order form.', html)
        self.assertIn('Upload their order form', html)
        self.assertNotIn('always attached', html)
        self.assertNotIn('<iframe', html)

    def test_dialog_shows_the_saved_total_or_a_dash(self):
        self.assertRegex(self.email_dialog_html(), r'Total incl VAT</dt>\s*<dd>–</dd>')  # created before prices
        PurchaseOrder.objects.filter(pk=self.order.pk).update(vat_rate=Decimal('15.00'))
        self.order.items.update(unit_price=Decimal('100.00'))
        self.assertRegex(self.email_dialog_html(), r'Total incl VAT</dt>\s*<dd>R 575\.00</dd>')

    def test_dialog_says_how_many_lines_use_the_reference_price(self):
        self.assertNotIn('reference price', self.email_dialog_html())  # created before prices: nothing to say
        PurchaseOrder.objects.filter(pk=self.order.pk).update(vat_rate=Decimal('15.00'))
        self.order.items.update(unit_price=Decimal('100.00'), price_source='reference')
        self.assertIn('1 of 1 line uses the catalogue reference price', self.email_dialog_html())

    def test_inline_pdf_is_framable_same_origin_and_not_audited(self):
        response = self.client.get(self.url('pdf') + '?inline=1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Disposition'].split(';')[0], 'inline')
        self.assertEqual(response['X-Frame-Options'], 'SAMEORIGIN')
        self.assertFalse(AuditEvent.objects.filter(action='purchase_order.pdf_exported').exists())
        download = self.client.get(self.url('pdf'))
        self.assertEqual(download['Content-Disposition'].split(';')[0], 'attachment')
        self.assertEqual(download['X-Frame-Options'], 'DENY')


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
