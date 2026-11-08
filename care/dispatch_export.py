"""Streaming Excel rendering of authorised dispatch-history rows."""

from datetime import date, datetime
from tempfile import SpooledTemporaryFile
from zoneinfo import ZoneInfo

from django.utils import timezone
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


SAST = ZoneInfo('Africa/Johannesburg')
HEADERS = (
    'Shipment', 'Planned date', 'Dispatched', 'Practice', 'Patient', 'ID / passport', 'Doctor',
    'Product', 'Dose', 'Quantity', 'Batch and expiry', 'Status', 'Tracking', 'Hold reason', 'Note',
    'Deliver to', 'Contact number', 'Waybill',
)
COLUMN_WIDTHS = (12, 17, 23, 30, 30, 23, 30, 42, 28, 12, 32, 23, 28, 44, 50, 60, 23, 28)
TYPED_COLUMNS = frozenset((0, 1, 2, 9))


def dispatch_history_workbook(rows):
    """Return a rewound XLSX file using caller-supplied snapshot rows, without querying dispatch records.

    Rows contain the 18 HEADERS values. Shipment IDs and quantities stay numeric, dates stay typed, and identity,
    phone and reference fields are literal text. Aware dispatch timestamps are converted to SAST; naive timestamps
    are already considered local SAST values. No pagination or forecast logic belongs in this renderer.
    """
    workbook = Workbook(write_only=True, iso_dates=True)
    sheet = workbook.create_sheet('Dispatch history')
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = 'A2'
    sheet.row_dimensions[1].height = 32
    sheet.print_title_rows = '1:1'
    sheet.page_setup.orientation = 'landscape'
    for column, width in enumerate(COLUMN_WIDTHS, start=1):
        sheet.column_dimensions[get_column_letter(column)].width = width

    def cell(value, *, header=False):
        result = WriteOnlyCell(sheet, value=value)
        if isinstance(value, str):
            # Prefixing an apostrophe would change the exported value. Store every string as literal text instead.
            result.data_type = 's'
        elif isinstance(value, datetime):
            result.number_format = 'dd mmm yyyy hh:mm'
        elif isinstance(value, date):
            result.number_format = 'dd mmm yyyy'
        elif isinstance(value, (int, float)):
            result.number_format = '0'
        result.font = Font(name='Arial', size=10, color='153F44')
        result.alignment = Alignment(vertical='top', horizontal='left' if isinstance(value, (str, date)) else 'right',
                                     wrap_text=True)
        if header:
            result.font = Font(name='Arial', size=10, bold=True, color='FFFFFF')
            result.fill = PatternFill('solid', fgColor='007F79')
            result.alignment = Alignment(vertical='center', horizontal='center', wrap_text=True)
        return result

    output = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode='w+b')
    try:
        sheet.append([cell(label, header=True) for label in HEADERS])
        row_count = 1
        for row in rows:
            values = tuple(row)
            if len(values) != len(HEADERS):
                raise ValueError(f'Dispatch-history rows need {len(HEADERS)} values.')
            formatted = []
            lines = 1
            for column, value in enumerate(values):
                if column not in TYPED_COLUMNS and value is not None:
                    value = str(value)
                if isinstance(value, datetime) and timezone.is_aware(value):
                    value = timezone.localtime(value, SAST).replace(tzinfo=None)
                if isinstance(value, str):
                    width = int(COLUMN_WIDTHS[column]) - 3
                    lines = max(lines, sum(max(1, (len(part) + width - 1) // width) for part in value.split('\n')))
                formatted.append(cell(value))
            row_count += 1
            sheet.row_dimensions[row_count].height = min(409, max(26, lines * 13 + 7))
            sheet.append(formatted)
        sheet.auto_filter.ref = f'A1:{get_column_letter(len(HEADERS))}{row_count}'
        workbook.save(output)
        output.seek(0)
        return output
    except Exception:
        output.close()
        raise
    finally:
        workbook.close()
