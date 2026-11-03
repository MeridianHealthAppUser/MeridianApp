from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from openpyxl import load_workbook

from .table_export import table_workbook


class TableWorkbookTests(SimpleTestCase):
    def workbook(self, headings, rows):
        output, count = table_workbook('Example', headings, rows)
        self.addCleanup(output.close)
        workbook = load_workbook(BytesIO(output.read()))
        self.addCleanup(workbook.close)
        return workbook.active, count

    def test_typed_dates_money_quantities_and_literal_identifiers(self):
        sheet, count = self.workbook(('Text', 'Phone', 'Quantity', 'Amount', 'Date', 'Time'), [
            ('=1+1', '00123', 3, Decimal('12.50'), date(2026, 11, 3),
             datetime(2026, 11, 2, 23, tzinfo=timezone.utc))])
        self.assertEqual(count, 1)
        self.assertEqual([(cell.value, cell.data_type) for cell in sheet[2]], [
            ('=1+1', 's'), ('00123', 's'), (3, 'n'), (12.5, 'n'), (date(2026, 11, 3), 'd'),
            (datetime(2026, 11, 3, 1), 'd')])
        self.assertEqual(sheet.freeze_panes, 'A2')
        self.assertEqual(sheet.auto_filter.ref, 'A1:F2')

    def test_empty_rows_still_have_filterable_headers(self):
        sheet, count = self.workbook(('Product', 'Category'), [])
        self.assertEqual((count, sheet.max_row, sheet.auto_filter.ref), (0, 1, 'A1:B1'))

    def test_large_export_rejects_before_a_partial_file_is_returned(self):
        with self.assertRaises(ValidationError):
            table_workbook('Large', ('Number',), ((number,) for number in range(10001)))
