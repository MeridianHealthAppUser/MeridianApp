"""Dispatch-history XLSX keeps snapshot values, types and unsafe-looking text intact."""

from datetime import date, datetime, timezone as datetime_timezone

from django.test import SimpleTestCase
from openpyxl import load_workbook

from .dispatch_export import HEADERS, dispatch_history_workbook


class DispatchHistoryWorkbookTests(SimpleTestCase):
    def row(self, **changes):
        values = dict(zip(HEADERS, (
            1234, date(2026, 11, 3), datetime(2026, 11, 3, 10, 15, tzinfo=datetime_timezone.utc),
            'Example Practice', 'Patient Example', '0012345678901', 'Dr Example', 'Semaglutide 1 mg',
            '1 mg weekly', 2, 'LOT-123, expiry 11/2027', 'Dispatched', '00012345', '', 'Keep chilled',
            '1 Example Road, Cape Town', '0820000000', '00056789',
        )))
        values.update(changes)
        return tuple(values[label] for label in HEADERS)

    def workbook(self, rows):
        output = dispatch_history_workbook(rows)
        self.addCleanup(output.close)
        self.assertEqual(output.tell(), 0)
        workbook = load_workbook(output)
        self.addCleanup(workbook.close)
        return workbook

    def test_eighteen_headers_and_snapshot_values_are_exported_in_the_supplied_order(self):
        row = self.row()
        sheet = self.workbook(iter((row,))).active

        self.assertEqual(sheet.title, 'Dispatch history')
        self.assertEqual(len(HEADERS), 18)
        self.assertEqual(tuple(cell.value for cell in sheet[1]), HEADERS)
        expected = list(row)
        expected[2] = datetime(2026, 11, 3, 12, 15)
        self.assertEqual(tuple(cell.value for cell in sheet[2]), tuple(value if value != '' else None for value in expected))

    def test_shipment_quantity_and_dates_remain_typed_and_dispatch_time_is_sast(self):
        sheet = self.workbook((self.row(),)).active

        self.assertEqual((sheet['A2'].value, sheet['A2'].data_type), (1234, 'n'))
        self.assertEqual((sheet['J2'].value, sheet['J2'].data_type), (2, 'n'))
        self.assertEqual((sheet['B2'].value, sheet['B2'].data_type), (date(2026, 11, 3), 'd'))
        self.assertEqual((sheet['C2'].value, sheet['C2'].data_type), (datetime(2026, 11, 3, 12, 15), 'd'))
        self.assertEqual(sheet['B2'].number_format, 'dd mmm yyyy')
        self.assertEqual(sheet['C2'].number_format, 'dd mmm yyyy hh:mm')
        self.assertEqual(sheet['C2'].value.tzinfo, None)

    def test_ids_phones_and_tracking_references_stay_text_with_leading_zeroes(self):
        sheet = self.workbook((self.row(),)).active

        for address, value in (('F2', '0012345678901'), ('M2', '00012345'), ('Q2', '0820000000'), ('R2', '00056789')):
            with self.subTest(cell=address):
                self.assertEqual((sheet[address].value, sheet[address].data_type), (value, 's'))
        numeric_identifiers = self.workbook((self.row(**{'ID / passport': 123, 'Contact number': 456,
                                                       'Tracking': 789, 'Waybill': 101}),)).active
        for address, value in (('F2', '123'), ('M2', '789'), ('Q2', '456'), ('R2', '101')):
            self.assertEqual((numeric_identifiers[address].value, numeric_identifiers[address].data_type), (value, 's'))

    def test_formula_and_error_prefixes_are_literal_without_added_apostrophes(self):
        names = ('=HYPERLINK("https://example.test")', '+SUM(A1)', '-1+2', '@SUM(A1)', '#REF!', '#N/A')
        rows = [self.row(**{'Patient': name, 'Practice': name, 'ID / passport': name, 'Product': name,
                           'Note': name, 'Deliver to': name, 'Contact number': name, 'Tracking': name, 'Waybill': name})
                for name in names]
        sheet = self.workbook(rows).active

        for number, name in enumerate(names, start=2):
            for column in (4, 5, 6, 8, 13, 15, 16, 17, 18):
                current = sheet.cell(number, column)
                self.assertEqual((current.value, current.data_type), (name, 's'))
        self.assertFalse(any(cell.data_type == 'f' for row in sheet for cell in row))

    def test_empty_history_has_headers_freeze_and_a_valid_filter(self):
        sheet = self.workbook(iter(())).active

        self.assertEqual(tuple(cell.value for cell in sheet[1]), HEADERS)
        self.assertEqual(sheet.max_row, 1)
        self.assertEqual(sheet.auto_filter.ref, 'A1:R1')
        self.assertEqual(sheet.freeze_panes, 'A2')

    def test_headers_wrapping_and_widths_are_readable_and_filter_covers_all_rows(self):
        note = 'A long operational note ' * 15
        sheet = self.workbook((self.row(), self.row(Note=note))).active

        self.assertEqual(sheet.auto_filter.ref, 'A1:R3')
        self.assertEqual(sheet['A1'].fill.fgColor.rgb, '00007F79')
        self.assertEqual(sheet['A1'].font.color.rgb, '00FFFFFF')
        self.assertTrue(sheet['A1'].font.bold)
        self.assertTrue(sheet['O3'].alignment.wrap_text)
        self.assertGreaterEqual(sheet.column_dimensions['O'].width, 45)
        self.assertGreater(sheet.row_dimensions[3].height, sheet.row_dimensions[2].height)
        self.assertFalse(sheet.sheet_view.showGridLines)

    def test_naive_dispatch_timestamp_is_kept_as_local_sast(self):
        local = datetime(2026, 11, 3, 9, 30)
        sheet = self.workbook((self.row(**{'Dispatched (SAST)': local}),)).active

        self.assertEqual(sheet['C2'].value, local)

    def test_every_supplied_item_line_is_written_without_grouping_or_pagination(self):
        first = self.row(Product='Pen', Quantity=2)
        second = self.row(Product='Supplement', Quantity=3)
        sheet = self.workbook(row for row in (first, second)).active

        self.assertEqual([sheet.cell(number, 1).value for number in (2, 3)], [1234, 1234])
        self.assertEqual([sheet.cell(number, 8).value for number in (2, 3)], ['Pen', 'Supplement'])
        self.assertEqual([sheet.cell(number, 10).value for number in (2, 3)], [2, 3])
