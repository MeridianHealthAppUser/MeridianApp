"""Styled Excel downloads for tabular operational data."""
from datetime import date, datetime
from decimal import Decimal
from itertools import islice
from tempfile import SpooledTemporaryFile
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


def table_workbook(title, headings, rows, *, widths=None):
    rows = list(islice(iter(rows), 10001))
    if len(rows) > 10000:
        raise ValidationError('Narrow the filters to at most 10,000 exported rows.')
    headings = tuple(headings)
    if any(len(row) != len(headings) for row in rows):
        raise ValueError('Export rows must match their table headings.')
    widths = widths or tuple(min(48, max(17, len(str(heading)) + 4)) for heading in headings)
    workbook = Workbook(write_only=True, iso_dates=True)
    sheet = workbook.create_sheet(title[:31])
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = 'A2'
    sheet.print_title_rows = '1:1'
    sheet.row_dimensions[1].height = 32
    for column, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(column)].width = width

    def cell(value, header=False):
        if isinstance(value, datetime) and timezone.is_aware(value):
            value = timezone.localtime(value, ZoneInfo('Africa/Johannesburg')).replace(tzinfo=None)
        result = WriteOnlyCell(sheet, value=value)
        if isinstance(value, str):
            result.data_type = 's'
        elif isinstance(value, datetime):
            result.number_format = 'dd mmm yyyy hh:mm'
        elif isinstance(value, date):
            result.number_format = 'dd mmm yyyy'
        elif isinstance(value, (Decimal, float)):
            result.number_format = '#,##0.00'
        elif isinstance(value, int) and not isinstance(value, bool):
            result.number_format = '#,##0'
        result.font = Font(name='Arial', size=10, color='FFFFFF' if header else '153F44', bold=header)
        result.alignment = Alignment(vertical='center' if header else 'top', wrap_text=True)
        if header:
            result.fill = PatternFill('solid', fgColor='007F79')
        return result

    sheet.append([cell(value, True) for value in headings])
    for index, row in enumerate(rows, 2):
        lines = max((sum(max(1, (len(part) + max(1, int(widths[column]) - 3) - 1)
                             // max(1, int(widths[column]) - 3)) for part in str(value or '').split('\n'))
                     for column, value in enumerate(row)), default=1)
        sheet.row_dimensions[index].height = min(409, max(27, lines * 13 + 7))
        sheet.append([cell(value) for value in row])
    sheet.auto_filter.ref = f'A1:{get_column_letter(len(headings))}{len(rows) + 1}'
    output = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode='w+b')
    try:
        workbook.save(output)
        output.seek(0)
        return output, len(rows)
    except Exception:
        output.close()
        raise
    finally:
        workbook.close()
