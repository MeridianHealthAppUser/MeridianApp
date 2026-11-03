"""Excel stock history with typed amounts/dates and literal user-entered text."""
from datetime import date, datetime
from tempfile import SpooledTemporaryFile
from zoneinfo import ZoneInfo

from django.utils import timezone
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


SAST = ZoneInfo('Africa/Johannesburg')
HEADERS = ('Date (SAST)', 'Product', 'Category', 'Quantity', 'Batch', 'Expires on',
           'Reference / reason', 'Shipment ID', 'Recorded by', 'Stock override')


def stock_history_workbook(movements):
    """Return a rewinded XLSX file, spilling to disk for larger histories.

    Callers provide the authorised queryset; pagination must never be applied.
    Write-only mode and iterator() avoid keeping the complete history in memory.
    """
    workbook = Workbook(write_only=True, iso_dates=True)
    sheet = workbook.create_sheet('Stock movements')
    sheet.freeze_panes = 'A2'
    for column, width in enumerate((23, 46, 24, 12, 25, 18, 58, 16, 42, 18), start=1):
        sheet.column_dimensions[get_column_letter(column)].width = width

    def cell(value, *, header=False):
        result = WriteOnlyCell(sheet, value=value)
        if isinstance(value, str):
            # openpyxl otherwise interprets leading '=' and Excel error names.
            result.data_type = 's'
        elif isinstance(value, datetime):
            result.number_format = 'dd mmm yyyy hh:mm'
        elif isinstance(value, date):
            result.number_format = 'dd mmm yyyy'
        result.alignment = Alignment(vertical='top', wrap_text=True)
        if header:
            result.font = Font(bold=True, color='FFFFFF')
            result.fill = PatternFill('solid', fgColor='007F79')
        return result

    sheet.append([cell(value, header=True) for value in HEADERS])
    row_count = 1
    for movement in movements.iterator(chunk_size=2000):
        values = (
            timezone.localtime(movement.created_at, SAST).replace(tzinfo=None),
            str(movement.product), movement.product.get_category_display(), movement.quantity, movement.batch, movement.expires_on,
            movement.reason, movement.shipment_id, str(movement.actor), movement.is_override,
        )
        sheet.append([cell(value) for value in values])
        row_count += 1
    sheet.auto_filter.ref = f'A1:J{row_count}'
    output = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode='w+b')
    try:
        workbook.save(output)
        output.seek(0)
        return output
    except Exception:
        output.close()
        raise
    finally:
        workbook.close()
