"""Purchase documents preserve typed snapshot data and real receipt progress without database writes."""

from datetime import date, datetime, timezone
from io import BytesIO
import re
from types import SimpleNamespace
import zipfile

from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from openpyxl import load_workbook
from pypdf import PdfReader

from .models import MedicationProduct, PracticeSettings, PurchaseOrder, Supplier, SupplierProduct
from .purchase_exports import DEMO_LABEL, HEADERS, purchase_order_pdf, purchase_order_workbook
from .purchasing import create_purchase_order
from .test_purchase_orders import PurchaseOrderFixture


class Items:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


def purchase_order(*, count=2, eta=date(2026, 11, 12)):
    items = [SimpleNamespace(company_id=1, purchase_order_id=42,
        product=SimpleNamespace(company_id=1, name='Later catalogue name'),
        product_name='Semaglutide at order' if index == 0 else f'Product {index + 1}',
        strength='2.0 mg', category='weight_management', supplier_code=f'SUP-{index + 1:03d}', quantity=5 if index == 0 else 3,
        quantity_received=2 if index == 0 else 0) for index in range(count)]
    return SimpleNamespace(pk=42, company_id=1, company=SimpleNamespace(name='Meridian Test & Partners'),
        reference='PO-00021', created_at=datetime(2026, 11, 3, 12, 15, tzinfo=timezone.utc),
        supplier_name='Recorded supplier & partners', supplier_email='orders@supplier.example',
        eta=eta, status='open', get_status_display=lambda: 'Open', items=Items(items))


class PurchaseExportTests(SimpleTestCase):
    def test_demo_documents_are_clearly_marked_in_excel_and_on_every_pdf_page(self):
        order = purchase_order(count=70)
        order.demo_data_set_id = 'owned-demo'
        workbook = self.workbook(order)
        self.addCleanup(workbook.close)
        self.assertEqual(workbook.active['A6'].value, DEMO_LABEL)
        with purchase_order_pdf(order) as output:
            reader = PdfReader(output)
            self.assertGreater(len(reader.pages), 1)
            for page in reader.pages:
                self.assertIn(DEMO_LABEL, page.extract_text())

    def workbook(self, order):
        with purchase_order_workbook(order) as output:
            content = output.read()
        with zipfile.ZipFile(BytesIO(content)) as archive:
            self.assertIsNone(archive.testzip())
            self.assertFalse(any('vbaProject' in name or 'externalLinks' in name for name in archive.namelist()))
        return load_workbook(BytesIO(content), data_only=False)

    def test_excel_has_typed_metadata_snapshot_lines_and_receipt_totals(self):
        workbook = self.workbook(purchase_order())
        self.addCleanup(workbook.close)
        sheet = workbook['Purchase order']
        self.assertEqual(sheet['A1'].value, 'Meridian Test & Partners')
        self.assertEqual((sheet['B3'].value, sheet['E3'].value), ('PO-00021', 'Open'))
        self.assertEqual(sheet['B4'].value, datetime(2026, 11, 3, 14, 15))
        self.assertEqual(sheet['E4'].value.date(), date(2026, 11, 12))
        self.assertEqual((sheet['A5'].value, sheet['B5'].value), ('Supplier', 'Recorded supplier & partners'))
        self.assertEqual((sheet['D5'].value, sheet['E5'].value), ('Email', 'orders@supplier.example'))
        self.assertEqual(tuple(cell.value for cell in sheet[7]), HEADERS)
        self.assertEqual(tuple(cell.value for cell in sheet[8]),
                         ('Semaglutide at order', '2.0 mg', 'Weight management', 'SUP-001', None, None, None, 5, None, 2, 3))
        self.assertEqual(tuple(sheet.cell(11, column).value for column in (8, 10, 11)), (8, 2, 6))
        self.assertEqual(sheet.freeze_panes, 'A8')
        self.assertEqual(sheet.auto_filter.ref, 'A7:K9')
        self.assertTrue(sheet['A7'].font.bold)
        self.assertTrue(sheet['A7'].fill.fgColor.rgb.endswith('007F79'))
        self.assertNotIn('Later catalogue name', str(list(sheet.values)))
        self.assertNotIn('Subtotal (excl VAT)', str(list(sheet.values)))  # no saved prices, no money totals

    def test_excel_omits_unset_eta_and_stores_formula_like_text_as_literal_strings(self):
        order = purchase_order(eta=None)
        order.items.values[0].product_name = '=HYPERLINK("https://example.invalid")'
        order.items.values[0].strength = '#N/A'
        order.items.values[0].supplier_code = '=SUM(A1:A2)'
        order.supplier_name = '=UNTRUSTED_SUPPLIER'
        workbook = self.workbook(order)
        self.addCleanup(workbook.close)
        sheet = workbook.active
        self.assertIsNone(sheet['E4'].value)
        self.assertEqual(sheet['A8'].value, order.items.values[0].product_name)
        self.assertEqual(sheet['A8'].data_type, 's')
        self.assertEqual(sheet['B8'].value, '#N/A')
        self.assertEqual(sheet['B8'].data_type, 's')
        for cell, value in (('D8', '=SUM(A1:A2)'), ('B5', '=UNTRUSTED_SUPPLIER')):
            self.assertEqual(sheet[cell].value, value)
            self.assertEqual(sheet[cell].data_type, 's')
        self.assertFalse(any(cell.data_type == 'f' for row in sheet for cell in row))

    def test_xml_control_characters_are_safely_replaced(self):
        order = purchase_order()
        order.items.values[0].product_name = 'One\x00two\x0bthree'
        workbook = self.workbook(order)
        self.addCleanup(workbook.close)
        self.assertEqual(workbook.active['A8'].value, 'One two three')
        with purchase_order_pdf(order) as output:
            self.assertIn('One two three', PdfReader(output).pages[0].extract_text())

    def test_pdf_contains_order_facts_snapshot_lines_and_no_receipt_progress(self):
        with purchase_order_pdf(purchase_order()) as output:
            content = output.read()
        self.assertTrue(content.startswith(b'%PDF-'))
        reader = PdfReader(BytesIO(content))
        self.assertEqual(len(reader.pages), 1)
        text = reader.pages[0].extract_text()
        flat = re.sub(r'\s+', ' ', text)  # the party boxes are narrow, so values wrap
        for expected in ('PURCHASE ORDER', 'Meridian Test & Partners', 'PO-00021', '03 Nov 2026', '12 Nov 2026',
                         'SUPPLIER', 'BILL TO', 'SHIP TO', 'Semaglutide at order 2.0 mg', 'Recorded supplier & partners',
                         'orders @supplier.example', 'SUP-001', 'Product code', 'Qty', 'Page 1'):
            self.assertIn(expected, flat)
        self.assertRegex(text, re.compile(r'SUP-001.*?Semaglutide at order 2\.0 mg.*?5', re.S))
        for absent in ('Later catalogue name', 'Received', 'Remaining', 'Total (incl VAT)'):
            self.assertNotIn(absent, text)

    def test_pdf_escapes_markup_and_handles_missing_eta_and_unicode_strength(self):
        order = purchase_order(eta=None)
        order.items.values[0].product_name = 'Product <B> & sample'
        order.items.values[0].strength = '250 µg'
        with purchase_order_pdf(order) as output:
            text = PdfReader(output).pages[0].extract_text()
        self.assertIn('Product <B> & sample', text)
        self.assertIn('250 µg', text)
        self.assertIn('DELIVERY REQUESTED', text)

    def test_long_multi_page_orders_repeat_table_headers_and_reference_footers(self):
        order = purchase_order(count=55)
        for item in order.items.values:
            item.product_name += ' Long product description for packing and receiving.' * 3
        with purchase_order_pdf(order) as output:
            reader = PdfReader(output)
            self.assertGreater(len(reader.pages), 1)
            for number, page in enumerate(reader.pages, 1):
                text = page.extract_text()
                self.assertIn('PO-00021', text)
                self.assertIn(f'Page {number}', text)
                if 'SUP-' in text:  # every page with order lines repeats the column headings
                    for expected in ('Product code', 'Description', 'Qty'):
                        self.assertIn(expected, text)

    def test_both_document_formats_reject_cross_practice_lines(self):
        for field in ('company_id', 'purchase_order_id', 'product'):
            order = purchase_order()
            setattr(order.items.values[0], field, SimpleNamespace(company_id=2) if field == 'product' else 99)
            for build in (purchase_order_workbook, purchase_order_pdf):
                with self.subTest(field=field, build=build.__name__), self.assertRaises(ValidationError):
                    build(order)

    def test_legacy_order_without_supplier_remains_exportable_as_unassigned(self):
        order = purchase_order()
        order.supplier_name = order.supplier_email = ''
        for item in order.items.values:
            item.supplier_code = ''
        workbook = self.workbook(order)
        self.addCleanup(workbook.close)
        self.assertEqual(workbook.active['B5'].value, 'Unassigned')
        self.assertIsNone(workbook.active['D8'].value)
        with purchase_order_pdf(order) as output:
            text = PdfReader(output).pages[0].extract_text()
        self.assertIn('Unassigned', text)
        self.assertNotIn('Code:', text)


class PricedPurchaseOrderDocumentTests(PurchaseOrderFixture):
    """The supplier-facing PDF and the Excel export of an order created with prices and saved details."""

    def priced_order(self, *, products=None, vat_registered=True):
        Supplier.objects.filter(pk=self.supplier.pk).update(
            name='Bio-Swiss', registered_name='Bio-Swiss (Pty) Ltd', vat_registered=vat_registered,
            vat_number='4350263226' if vat_registered else '', address_street='1 Road', address_city='Midrand',
            address_postal_code='1685', payment_terms='30 days from statement', account_number='MER-0042',
            sahpra_licence_number='22C-1')
        get_user_model().objects.filter(pk=self.doctor.pk).update(title='DR', first_name='Jo', last_name='Example',
                                                                   hpcsa_number='MP0000000')
        PracticeSettings.objects.update_or_create(company=self.company, defaults=dict(
            po_bill_to_name='Dr Jo Example', po_billing_street='6 Harfield Rd', po_billing_city='Cape Town',
            po_billing_postal_code='7708', po_doctor=self.doctor, collection_business='Meridian Health',
            collection_street='6 Harfield Rd', collection_city='Cape Town', collection_postal_code='7708',
            collection_contact='Alison'))
        items = []
        if products is None:
            for code, description, price, quantity in (('T5/0.6/2.4 PFS', 'Tirzepatide 5mg/0,6ml PFS', '1600.00', 10),
                                                       ('T10/0.6/2.4 PFS', 'Tirzepatide 10mg/0,6ml PFS', '2500.00', 6),
                                                       ('T15/0.6/2.4 PFS', 'Tirzepatide 15mg/0,6ml PFS', '3300.00', 4)):
                product = MedicationProduct.objects.create(company=self.company, name=description, price=Decimal('1.00'))
                SupplierProduct.objects.create(company=self.company, supplier=self.supplier, product=product,
                    supplier_code=code, description=description, pack_size='2,4ml', unit_price=Decimal(price))
                items.append({'product': product, 'quantity': quantity})
        else:
            items = [{'product': product, 'quantity': 1} for product in products]
        return create_purchase_order(company=self.company, actor=self.admin, supplier=Supplier.objects.get(pk=self.supplier.pk),
                                     items=items, submission_key=uuid4())

    def pdf_text(self, order):
        order = PurchaseOrder.objects.select_related('company', 'created_by').get(pk=order.pk)
        with purchase_order_pdf(order) as output:
            return '\n'.join(page.extract_text() for page in PdfReader(output).pages)

    def test_pdf_has_header_parties_lines_and_totals(self):
        text = self.pdf_text(self.priced_order())
        for expected in ('PURCHASE ORDER', 'PO-00001', 'Bio-Swiss (Pty) Ltd', '4350263226', 'BILL TO', 'SHIP TO',
                         'Dr Jo Example', 'MP0000000', 'T5/0.6/2.4 PFS', 'Tirzepatide 5mg/0,6ml PFS', '2,4ml',
                         'R 1 600.00', 'R 16 000.00', 'Subtotal (excl VAT)', 'R 44 200.00', 'VAT 15%', 'R 6 630.00',
                         'Total (incl VAT)', 'R 50 830.00', 'Payment terms', '30 days from statement', 'AUTHORISED BY',
                         'MER-0042', 'Alison'):
            self.assertIn(expected, text)

    def test_cold_chain_note_only_with_cold_chain_products(self):
        self.assertIn('Cold-chain products: deliver refrigerated', self.pdf_text(self.priced_order()))
        self.assertNotIn('Cold-chain', self.pdf_text(self.priced_order(products=[self.open_product])))

    def test_non_vat_supplier_pdf_shows_vat_zero(self):
        text = self.pdf_text(self.priced_order(vat_registered=False))
        self.assertIn('VAT 0%', text)
        self.assertIn('Not VAT-registered', text)
        self.assertRegex(text, re.compile(r'Total \(incl VAT\)\s*R 44 200\.00'))

    def test_legacy_order_without_prices_renders_dashes_and_no_totals(self):
        order = self.priced_order()
        PurchaseOrder.objects.filter(pk=order.pk).update(vat_rate=None, supplier_details={}, buyer_details={})
        order.items.update(unit_price=None, price_source='')
        text = self.pdf_text(order)
        self.assertIn('–', text)
        self.assertIn('Bio-Swiss', text)  # the supplier name saved on the order
        self.assertNotIn('Total (incl VAT)', text)
        self.assertNotIn('R 1 600.00', text)

    def test_workbook_adds_prices_and_keeps_receipt_progress(self):
        order = PurchaseOrder.objects.select_related('company').get(pk=self.priced_order().pk)
        with purchase_order_workbook(order) as output:
            workbook = load_workbook(BytesIO(output.read()))
        self.addCleanup(workbook.close)
        sheet = workbook.active
        headings = [cell.value for cell in sheet[7]]
        for heading in ('Description', 'Size', 'Unit price (excl VAT)', 'Line total (excl VAT)', 'Received', 'Remaining'):
            self.assertIn(heading, headings)
        values = {row[0]: row for row in sheet.iter_rows(min_row=8, values_only=True) if row[0]}
        labels = [label for label in values if isinstance(label, str)]
        for label in ('Subtotal (excl VAT)', 'VAT 15%', 'Total (incl VAT)'):
            self.assertIn(label, labels)
        price_column = headings.index('Line total (excl VAT)')
        self.assertEqual(values['Total (incl VAT)'][price_column], Decimal('50830.00'))
        cell = sheet.cell(8, headings.index('Unit price (excl VAT)') + 1)
        self.assertEqual((cell.value, cell.number_format), (1600, '#,##0.00'))
