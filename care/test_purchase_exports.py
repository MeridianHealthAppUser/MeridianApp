"""Purchase documents preserve typed snapshot data and real receipt progress without database writes."""

from datetime import date, datetime, timezone
from io import BytesIO
import re
from types import SimpleNamespace
import zipfile

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from openpyxl import load_workbook
from pypdf import PdfReader

from .purchase_exports import DEMO_LABEL, HEADERS, purchase_order_pdf, purchase_order_workbook


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
                         ('Semaglutide at order', '2.0 mg', 'Weight management', 'SUP-001', 5, 2, 3))
        self.assertEqual(tuple(sheet.cell(11, column).value for column in (5, 6, 7)), (8, 2, 6))
        self.assertEqual(sheet.freeze_panes, 'A8')
        self.assertEqual(sheet.auto_filter.ref, 'A7:G9')
        self.assertTrue(sheet['A7'].font.bold)
        self.assertTrue(sheet['A7'].fill.fgColor.rgb.endswith('007F79'))
        self.assertNotIn('Later catalogue name', str(list(sheet.values)))
        self.assertNotIn('Price', HEADERS)

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

    def test_pdf_contains_actual_metadata_snapshot_and_progress_without_prices(self):
        with purchase_order_pdf(purchase_order()) as output:
            content = output.read()
        self.assertTrue(content.startswith(b'%PDF-'))
        reader = PdfReader(BytesIO(content))
        self.assertEqual(len(reader.pages), 1)
        text = reader.pages[0].extract_text()
        for expected in ('Meridian Test & Partners', 'PO-00021', '03 Nov 2026, 14:15',
                         '12 Nov 2026', 'Supplier', 'Semaglutide at order', '2.0 mg',
                         'Recorded supplier & partners', 'orders@supplier.example', 'Code: SUP-001',
                         'Ordered', 'Received', 'Remaining', 'Total units', 'Page 1'):
            self.assertIn(expected, text)
        self.assertRegex(text, re.compile(r'Semaglutide at order.*?5\s+2\s+3', re.S))
        self.assertNotIn('Later catalogue name', text)
        self.assertNotIn('Price', text)

    def test_pdf_escapes_markup_and_handles_missing_eta_and_unicode_strength(self):
        order = purchase_order(eta=None)
        order.items.values[0].product_name = 'Product <B> & sample'
        order.items.values[0].strength = '250 µg'
        with purchase_order_pdf(order) as output:
            text = PdfReader(output).pages[0].extract_text()
        self.assertIn('Product <B> & sample', text)
        self.assertIn('250 µg', text)
        self.assertIn('ETA', text)

    def test_long_multi_page_orders_repeat_table_headers_and_reference_footers(self):
        order = purchase_order(count=55)
        for item in order.items.values:
            item.product_name += ' Long product description for packing and receiving.' * 3
        with purchase_order_pdf(order) as output:
            reader = PdfReader(output)
            self.assertGreater(len(reader.pages), 1)
            for number, page in enumerate(reader.pages, 1):
                text = page.extract_text()
                for expected in ('Product', 'Ordered', 'Received', 'Remaining', 'PO-00021', f'Page {number}'):
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
