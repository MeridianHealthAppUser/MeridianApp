"""The workbook is a typed, scoped report whose weekly totals reconcile to source dispatch lines."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from io import BytesIO
from zoneinfo import ZoneInfo
import zipfile

from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.utils import timezone
from openpyxl import load_workbook

from .consumption import consumption_report, weekly_items
from .consumption_export import (DETAIL_HEADERS, consumption_detail_workbook, consumption_dispatch_data,
                                 consumption_workbook)
from .models import Shipment, ShipmentItem
from .test_operations import OperationsFixture


class ConsumptionWorkbookTests(OperationsFixture):
    as_of = date(2026, 11, 3)
    last_week = date(2026, 10, 26)

    def supplied(self, quantity, day=None, **extra):
        day = day or self.last_week
        values = dict(status=Shipment.Status.DISPATCHED, scheduled_for=self.as_of + timedelta(days=30),
                      subscription=None,
                      dispatched_at=timezone.make_aware(datetime.combine(day, time(12)), ZoneInfo('Africa/Johannesburg')))
        values.update(extra)
        return self.shipment(quantity=quantity, **values)

    def read(self, stream):
        with stream:
            self.assertEqual(stream.tell(), 0)
            data = stream.read()
        with zipfile.ZipFile(BytesIO(data)) as archive:
            self.assertIsNone(archive.testzip())
            self.assertFalse(any('externalLinks' in name or 'vbaProject' in name for name in archive.namelist()))
        workbook = load_workbook(BytesIO(data), data_only=False)
        self.addCleanup(workbook.close)
        return workbook

    def report(self, **extra):
        return consumption_report(self.company, as_of=self.as_of, **extra)

    def test_weekly_sheet_averages_and_all_detail_lines_reconcile(self):
        self.supplied(6, self.last_week - timedelta(days=7))
        self.supplied(4, self.last_week, dispatch_snapshot={'patient_name': 'Patient at dispatch'}, waybill='WAY-10')
        self.supplied(2, self.last_week, product=self.open_product, status=Shipment.Status.DELIVERED)
        # No current week dispatch, receipt or starting AWC should enter the displayed completed weeks.
        self.supplied(900, self.as_of)
        self.product.starting_awc = Decimal('999.00')
        self.product.save(update_fields=('starting_awc',))
        report = self.report()
        workbook = self.read(consumption_workbook(self.company, report))
        self.assertEqual(workbook.sheetnames, ['Weekly consumption', 'Averages', 'Dispatch detail'])
        weekly = workbook['Weekly consumption']
        rows = {row[0].value: tuple(cell.value for cell in row) for row in weekly.iter_rows(min_row=8)}
        primary = rows[self.product.name]
        self.assertEqual(primary[3], 10)
        self.assertEqual(primary[-2:], (6, 4))
        self.assertEqual(rows[self.open_product.name][3], 2)
        self.assertEqual(weekly.freeze_panes, 'D8')
        self.assertEqual(weekly.auto_filter.ref, 'A7:L9')
        self.assertTrue(weekly['A7'].font.bold)
        self.assertTrue(weekly['A7'].fill.fgColor.rgb.endswith('007F79'))
        self.assertTrue(weekly.conditional_formatting)
        averages = workbook['Averages']
        row = next(values for values in averages.iter_rows(min_row=8, values_only=True) if values[0] == self.product.name)
        self.assertEqual((row[3], row[6], row[7]), (1.25, 10, 8))
        self.assertEqual((row[5], row[10], row[11]), (5, 10, 2))
        details = workbook['Dispatch detail']
        self.assertEqual(tuple(cell.value for cell in details[7]), DETAIL_HEADERS)
        sources = list(details.iter_rows(min_row=8, values_only=True))
        self.assertEqual(len(sources), 3)
        self.assertEqual(sum(row[5] for row in sources), sum(row[3] for row in rows.values()))
        self.assertTrue(any(row[6] == 'Patient at dispatch' and row[9] == 'WAY-10' for row in sources))
        self.assertTrue(all(isinstance(row[0], (date, datetime)) and isinstance(row[1], datetime) for row in sources))
        for name, values in rows.items():
            for index, week in enumerate(report['weeks']):
                from_sources = sum(row[5] for row in sources if row[2] == name and
                                   (row[0].date() if isinstance(row[0], datetime) else row[0]) == week['start'])
                self.assertEqual(values[4 + index], from_sources)

    def test_product_filters_and_zero_products_are_preserved_across_all_sheets(self):
        self.supplied(10)
        self.supplied(99, product=self.open_product)
        report = self.report(query='explicit dose')
        workbook = self.read(consumption_workbook(self.company, report))
        self.assertEqual(workbook['Weekly consumption'].max_row, 8)
        self.assertEqual(workbook['Averages'].max_row, 8)
        self.assertEqual(workbook['Dispatch detail'].max_row, 8)
        self.assertEqual(workbook['Dispatch detail']['F8'].value, 10)
        empty = self.read(consumption_workbook(self.company, self.report(query='no such product')))
        for sheet in empty:
            self.assertEqual(sheet.max_row, 7)
            self.assertIsNotNone(sheet.auto_filter.ref)

    def test_zero_history_is_distinct_from_an_all_time_average(self):
        workbook = self.read(consumption_workbook(self.company, self.report()))
        sheet = workbook['Averages']
        self.assertEqual(sheet['D8'].value, 0)
        self.assertEqual(sheet['E8'].value, 0)
        self.assertIsNone(sheet['F8'].value)
        self.assertIsNone(sheet['M8'].value)

    def test_week_detail_exports_every_line_beyond_any_ui_page_size(self):
        for index in range(29):
            self.supplied(index + 1, waybill=f'WB-{index:02d}')
        self.supplied(700, self.last_week - timedelta(days=1))
        self.supplied(800, product=self.open_product)
        workbook = self.read(consumption_detail_workbook(self.company, self.product, self.last_week,
                                                        as_of=self.as_of))
        self.assertEqual(workbook.sheetnames, ['Dispatch detail'])
        rows = list(workbook.active.iter_rows(min_row=8, values_only=True))
        self.assertEqual(len(rows), 29)
        self.assertEqual(sum(row[5] for row in rows), sum(range(1, 30)))
        self.assertEqual({row[9] for row in rows}, {f'WB-{index:02d}' for index in range(29)})

    def test_formula_like_product_patient_and_tracking_are_literal_text(self):
        self.product.name = '=HYPERLINK("https://example.invalid")'
        self.product.strength = 'One\x00two'
        self.product.save(update_fields=('name', 'strength'))
        self.supplied(1, tracking_number='=1+1', dispatch_snapshot={'patient_name': '+SUM(A1:A2)'})
        workbook = self.read(consumption_workbook(self.company, self.report()))
        weekly = workbook['Weekly consumption']
        row = next(row for row in weekly.iter_rows(min_row=8) if row[0].value == self.product.name)
        self.assertEqual(row[0].data_type, 's')
        self.assertEqual(row[1].value, 'One two')
        self.assertEqual(workbook['Dispatch detail']['G8'].value, '+SUM(A1:A2)')
        self.assertEqual(workbook['Dispatch detail']['K8'].value, '=1+1')
        self.assertFalse(any(cell.data_type == 'f' for sheet in workbook for row in sheet for cell in row))

    def test_detail_uses_sast_week_and_preserves_numeric_quantities(self):
        # 22:30 UTC Sunday belongs to Monday in South Africa.
        timestamp = datetime(2026, 10, 25, 22, 30, tzinfo=ZoneInfo('UTC'))
        self.supplied(3, dispatched_at=timestamp)
        workbook = self.read(consumption_detail_workbook(self.company, self.product, self.last_week,
                                                        as_of=self.as_of))
        row = tuple(cell.value for cell in workbook.active[8])
        self.assertEqual(row[0], self.last_week)
        self.assertEqual(row[1], datetime(2026, 10, 26, 0, 30))
        self.assertEqual(row[5], 3)
        self.assertEqual(workbook.active['F8'].data_type, 'n')

    def test_cross_practice_products_relationships_and_doctor_names_do_not_leak(self):
        other = self.supplied(77, company=self.beta, patient=self.beta_patient, product=self.beta_product)
        own = self.supplied(2)
        ShipmentItem.objects.create(company=self.company, shipment=other, product=self.product, quantity=88, dose='bad')
        ShipmentItem.objects.create(company=self.company, shipment=own, product=self.beta_product, quantity=99, dose='bad')
        foreign = get_user_model().objects.create_user('foreign-consumption@example.test', first_name='Private', last_name='Doctor')
        self.patient.assigned_doctor = foreign
        self.patient.save(update_fields=('assigned_doctor',))
        workbook = self.read(consumption_workbook(self.company, self.report()))
        sources = list(workbook['Dispatch detail'].iter_rows(min_row=8, values_only=True))
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0][5], 2)
        self.assertIsNone(sources[0][7])
        with self.assertRaises(ValidationError):
            consumption_detail_workbook(self.company, self.beta_product, self.last_week, as_of=self.as_of)
        report = self.report()
        report['rows'][0]['product'] = self.beta_product
        with self.assertRaises(ValidationError):
            consumption_workbook(self.company, report)

    def test_shared_display_helper_accepts_paginated_queryset(self):
        self.supplied(2)
        self.supplied(3)
        items = weekly_items(self.company, self.product, self.last_week, as_of=self.as_of)
        first = list(consumption_dispatch_data(self.company, items[:1]))
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]['quantity'], 3)
        self.assertEqual(first[0]['doctor'], self.doctor.titled_name)
