"""Issued invoices are downloadable without leaking or changing another patient's billing records."""

from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfReader

from care.models import Appointment, Invoice, InvoiceLine, Payment
from practices.models import Company, Patient

from .test_billing import BillingPageFixture


class InvoiceDownloadTests(BillingPageFixture):
    def invoice(self, *, company=None, patient=None, number='IC-TEST-001'):
        company, patient = company or self.company, patient or self.patient
        invoice = Invoice.objects.create(company=company, patient=patient, invoice_number=number,
                                          issued_on=self.today, due_on=self.today, subtotal='345.00', total='345.00',
                                          status=Invoice.Status.PAID,
                                          billed_to={'name': 'Nádia Issued Name', 'id_number': 'ORIGINAL-ID'})
        InvoiceLine.objects.create(company=company, invoice=invoice, description='Initial consultation · 30 minutes',
                                   quantity=1, unit_amount='345.00', line_total='345.00')
        return invoice

    def test_initial_checkout_style_payment_has_its_real_description_and_pdf_link(self):
        invoice = self.invoice()
        appointment = Appointment.objects.create(company=self.company, patient=self.patient, clinician=self.doctor,
                                                   appointment_type=Appointment.Type.INITIAL, duration_minutes=30,
                                                   starts_at=timezone.now() - timedelta(days=1))
        # Existing checkout payments were created without a kind; they still need to appear.
        Payment.objects.create(company=self.company, patient=self.patient, appointment=appointment, invoice=invoice,
                               amount='345.00', due_on=self.today, status=Payment.Status.PAID)
        self.login(self.patient_user)
        response = self.client.get(reverse('portal:patient-billing'))
        self.assertContains(response, 'Initial consult (30 mins)')
        self.assertContains(response, reverse('portal:patient-invoice-download', args=[invoice.pk]))

    def test_pdf_preserves_issued_identity_and_amounts_without_writing_records(self):
        invoice = self.invoice()
        self.patient.first_name, self.patient.id_number = 'Changed current name', 'CHANGED-ID'
        self.patient.save(update_fields=('first_name', 'id_number'))
        before = (Invoice.objects.count(), InvoiceLine.objects.count(), Payment.objects.count())
        self.login(self.patient_user)
        url = reverse('portal:patient-invoice-download', args=[invoice.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response['Content-Disposition'].startswith('attachment;'))
        self.assertIn('private', response['Cache-Control'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        text = '\n'.join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages)
        for expected in ('IC-TEST-001', 'Nádia Issued Name', 'ORIGINAL-ID', '30 minutes', 'R345.00'):
            self.assertIn(expected, text)
        self.assertNotIn('CHANGED-ID', text)
        self.assertNotIn('Changed current name', text)
        self.assertEqual(self.client.head(url).content, b'')
        self.assertEqual(before, (Invoice.objects.count(), InvoiceLine.objects.count(), Payment.objects.count()))

    def test_download_checks_login_role_patient_and_selected_practice(self):
        invoice = self.invoice()
        url = reverse('portal:patient-invoice-download', args=[invoice.pk])
        self.assertEqual(self.client.get(url).status_code, 302)
        self.login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 403)
        other_user = get_user_model().objects.create_user(email='invoice-other@example.test')
        other = Patient.objects.create(company=self.company, user=other_user, first_name='Other', last_name='Patient')
        foreign_invoice = self.invoice(patient=other, number='PRIVATE-OTHER')
        beta = Company.objects.create(name='Invoice Beta', slug='invoice-beta')
        beta_patient = Patient.objects.create(company=beta, user=self.patient_user, first_name='Nadia', last_name='Beta')
        beta_invoice = self.invoice(company=beta, patient=beta_patient, number='PRIVATE-BETA')
        self.login(self.patient_user)
        for hidden in (foreign_invoice, beta_invoice):
            response = self.client.get(reverse('portal:patient-invoice-download', args=[hidden.pk]))
            self.assertEqual(response.status_code, 404)
            self.assertNotIn(hidden.invoice_number.encode(), response.content)

    def test_linked_foreign_invoice_does_not_leak_in_the_payment_table(self):
        other_user = get_user_model().objects.create_user(email='invoice-linked@example.test')
        other = Patient.objects.create(company=self.company, user=other_user, first_name='Other', last_name='Patient')
        invoice = self.invoice(patient=other, number='PRIVATE-LINKED-INVOICE')
        Payment.objects.create(company=self.company, patient=self.patient, invoice=invoice, kind=Payment.Kind.MANUAL,
                               amount='345.00', due_on=self.today)
        self.login(self.patient_user)
        response = self.client.get(reverse('portal:patient-billing'))
        self.assertNotContains(response, invoice.invoice_number)
        self.assertNotContains(response, reverse('portal:patient-invoice-download', args=[invoice.pk]))

    def test_long_invoice_wraps_over_multiple_pages_and_retains_the_total(self):
        invoice = self.invoice()
        InvoiceLine.objects.bulk_create([
            InvoiceLine(company=self.company, invoice=invoice, description=f'Line {number}: ' + 'Recorded service ' * 15,
                        quantity=1, unit_amount='0.00', line_total='0.00') for number in range(55)
        ])
        self.login(self.patient_user)
        response = self.client.get(reverse('portal:patient-invoice-download', args=[invoice.pk]))
        reader = PdfReader(BytesIO(response.content))
        self.assertGreater(len(reader.pages), 1)
        text = '\n'.join(page.extract_text() for page in reader.pages)
        self.assertIn('Line 54:', text)
        self.assertIn('Total', text)
        invoice.refresh_from_db()
        self.assertEqual(invoice.total, Decimal('345.00'))

    def test_content_navigation_cannot_prefetch_an_invoice_download(self):
        invoice = self.invoice()
        self.login(self.patient_user)
        url = reverse('portal:patient-invoice-download', args=[invoice.pk])
        with patch('portal.invoice_pdf.invoice_pdf') as render_pdf:
            response = self.client.get(url, HTTP_X_MERIDIAN_NAVIGATION='content')
            self.assertEqual(response.status_code, 409)
            render_pdf.assert_not_called()

    def test_draft_and_void_invoices_are_readable_but_cannot_be_downloaded_as_issued(self):
        invoice = self.invoice()
        Payment.objects.create(company=self.company, patient=self.patient, invoice=invoice, kind=Payment.Kind.MANUAL,
                               amount='345.00', due_on=self.today, status=Payment.Status.PAID)
        self.login(self.patient_user)
        download = reverse('portal:patient-invoice-download', args=[invoice.pk])
        view = reverse('portal:patient-invoice', args=[invoice.pk])
        for status, heading in ((Invoice.Status.DRAFT, 'Draft invoice'), (Invoice.Status.VOID, 'Void invoice')):
            invoice.status = status
            invoice.save()
            self.assertEqual(self.client.get(download).status_code, 404)
            html = self.client.get(view)
            self.assertContains(html, heading)
            self.assertNotContains(html, download)
            billing = self.client.get(reverse('portal:patient-billing'))
            self.assertContains(billing, view)
            self.assertNotContains(billing, download)
        self.assertContains(html, 'This invoice is void. It does not require payment.')
        for status in (Invoice.Status.ISSUED, Invoice.Status.PAID):
            invoice.status = status
            invoice.save()
            self.assertEqual(self.client.get(download).status_code, 200)

    def test_other_payment_amount_and_status_remain_visible_without_extra_columns(self):
        for number, status in enumerate((Payment.Status.PENDING, Payment.Status.FAILED, Payment.Status.PAID, Payment.Status.REFUNDED), 11):
            Payment.objects.create(company=self.company, patient=self.patient, kind=Payment.Kind.MANUAL,
                                   amount=number, due_on=self.today, status=status)
        self.login(self.patient_user)
        response = self.client.get(reverse('portal:patient-billing'))
        table = response.content.decode().split('aria-labelledby="billing-other-heading"', 1)[1].split('</table>', 1)[0]
        for amount, status in ((11, 'Pending'), (12, 'Failed'), (13, 'Paid'), (14, 'Refunded')):
            self.assertIn(f'R{amount} · {status}', table)
        self.assertNotIn('<th scope="col">Amount</th>', table)
        self.assertNotIn('<th scope="col">Status</th>', table)
