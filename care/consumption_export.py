"""Reconciled Excel reports of actual dispatch consumption, with the contributing parcel lines."""

from datetime import date, datetime, timedelta
from decimal import Decimal
import re
from tempfile import SpooledTemporaryFile
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from practices.models import CompanyMembership
from .consumption import period_items, weekly_items


SAST = ZoneInfo('Africa/Johannesburg')
TEAL, INK, MUTED = '007F79', '153F44', '607C87'
HEADER_ROW = 7
_ILLEGAL_TEXT = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
DETAIL_HEADERS = ('Week starting', 'Dispatched (SAST)', 'Product', 'Strength', 'Category', 'Quantity',
                  'Patient', 'Doctor', 'Shipment', 'Waybill', 'Tracking', 'Dose', 'Batch note', 'Parcel line')
DETAIL_WIDTHS = (19, 24, 36, 23, 25, 13, 29, 29, 14, 26, 26, 26, 33, 15)


def _text(value):
    return _ILLEGAL_TEXT.sub(' ', str(value if value is not None else ''))


def consumption_dispatch_data(company, queryset):
    """One safely scoped display record per source line, shared by downloads and drill-downs.

    Product identity and quantities come from the immutable dispatched line. Patient wording uses the saved
    dispatch name when present. A doctor must belong to this practice, including a former staff membership.
    """
    doctor_ids = set(CompanyMembership.objects.filter(company=company).values_list('user_id', flat=True))
    if hasattr(queryset, 'select_related'):
        items = queryset.select_related(
            'product', 'shipment__patient__assigned_doctor', 'shipment__authorization__prescribed_by',
        ).iterator(chunk_size=1000)
    else:
        items = iter(queryset)
    for item in items:
        parcel = item.shipment
        if (item.company_id != company.pk or item.product.company_id != company.pk
                or parcel.company_id != company.pk or parcel.patient.company_id != company.pk):
            continue
        snapshot = parcel.dispatch_snapshot if isinstance(parcel.dispatch_snapshot, dict) else {}
        authorization = parcel.authorization
        doctor = None
        if (authorization is not None and authorization.company_id == company.pk
                and authorization.patient_id == parcel.patient_id):
            doctor = authorization.prescribed_by
        if doctor is None or doctor.pk not in doctor_ids:
            doctor = parcel.patient.assigned_doctor
        if doctor is not None and doctor.pk not in doctor_ids:
            doctor = None
        dispatched = timezone.localtime(parcel.dispatched_at, SAST)
        day = dispatched.date()
        yield dict(item=item, shipment=parcel, product=item.product,
                   patient=snapshot.get('patient_name') or str(parcel.patient),
                   doctor=doctor.titled_name if doctor else '', quantity=item.quantity,
                   dispatched_at=dispatched, week=day - timedelta(days=day.weekday()),
                   waybill=parcel.waybill or snapshot.get('waybill', ''), tracking=parcel.tracking_number)


def _detail_rows(company, queryset):
    for row in consumption_dispatch_data(company, queryset):
        product, item, parcel = row['product'], row['item'], row['shipment']
        yield (row['week'], row['dispatched_at'], product.name, product.strength, product.get_category_display(),
               row['quantity'], row['patient'], row['doctor'], parcel.pk, row['waybill'], row['tracking'],
               item.dose, item.batch_note, item.pk)


def _sheet(workbook, name, *, title, subtitle, notes, headings, rows, widths, freeze='D8',
           average_columns=(), heatmap_start=None):
    sheet = workbook.create_sheet(name)
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = freeze
    sheet.print_title_rows = '7:7'
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = 'landscape'
    sheet.page_setup.paperSize = '8'  # ISO A3; WriteOnlyWorksheet does not expose paper-size constants.
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    for column, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(column)].width = width

    def cell(value, *, header=False, row=0, column=0):
        if isinstance(value, datetime) and timezone.is_aware(value):
            value = timezone.localtime(value, SAST).replace(tzinfo=None)
        result = WriteOnlyCell(sheet, value=_text(value) if isinstance(value, str) else value)
        if isinstance(value, str):
            result.data_type = 's'
        elif isinstance(value, datetime):
            result.number_format = 'dd mmm yyyy hh:mm'
        elif isinstance(value, date):
            result.number_format = 'dd mmm yyyy'
        elif column in average_columns or isinstance(value, (float, Decimal)):
            result.number_format = '#,##0.00'
        elif isinstance(value, int):
            result.number_format = '#,##0'
        result.font = Font(name='Arial', size=10, color='FFFFFF' if header else INK, bold=header)
        result.alignment = Alignment(vertical='center' if header else 'top', wrap_text=True,
                                     horizontal='left' if isinstance(value, (str, date)) else 'right')
        if header:
            result.fill = PatternFill('solid', fgColor=TEAL)
        elif row % 2 == 0:
            result.fill = PatternFill('solid', fgColor='F4F9F8')
        return result

    for index, (value, size, color, bold) in enumerate((
        (title, 18, INK, True), (subtitle, 11, TEAL, True),
        *((note, 10, MUTED, False) for note in tuple(notes)[:3]),
    ), 1):
        result = cell(value)
        result.font = Font(name='Arial', size=size, color=color, bold=bold)
        result.alignment = Alignment(vertical='center', wrap_text=False)
        sheet.row_dimensions[index].height = 30 if index == 1 else 22
        sheet.append([result])
    populated = 2 + min(3, len(notes))
    for _ in range(populated + 1, HEADER_ROW):
        sheet.append([])
    sheet.row_dimensions[HEADER_ROW].height = 42
    sheet.append([cell(label, header=True) for label in headings])
    count = 0
    for count, values in enumerate(rows, 1):
        values = tuple(values)
        if len(values) != len(headings):
            raise ValueError('Consumption export rows must match their headings.')
        if count + HEADER_ROW > 1048576:
            raise ValidationError('There are too many dispatch lines for Excel. Choose a shorter date range.')
        line_count = max((sum(max(1, (len(part) + int(widths[column]) - 4) // (int(widths[column]) - 3))
                              for part in _text(value).split('\n'))
                          for column, value in enumerate(values)), default=1)
        row_number = HEADER_ROW + count
        sheet.row_dimensions[row_number].height = min(409, max(29, line_count * 13 + 8))
        sheet.append([cell(value, row=count, column=column) for column, value in enumerate(values, 1)])
    last_row = max(HEADER_ROW, HEADER_ROW + count)
    sheet.auto_filter.ref = f'A{HEADER_ROW}:{get_column_letter(len(headings))}{last_row}'
    if heatmap_start is not None and count:
        sheet.conditional_formatting.add(
            f'{get_column_letter(heatmap_start)}{HEADER_ROW + 1}:{get_column_letter(len(headings))}{last_row}',
            ColorScaleRule(start_type='num', start_value=0, start_color='FFFFFF',
                           end_type='max', end_color='B8E6DD'))
    return sheet


def _save(workbook):
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


def consumption_workbook(company, report):
    """Export the entire filtered report, its current averages and every contributing dispatch line."""
    rows = report['rows']
    if any(row['product'].company_id != company.pk for row in rows):
        raise ValidationError('The consumption report contains a product from another practice.')
    workbook = Workbook(write_only=True, iso_dates=True)
    weeks = report['weeks']
    selected = f"{report['period_start']:%d %b %Y} – {report['period_end']:%d %b %Y}"
    _sheet(workbook, 'Weekly consumption', title=f'{company.name} · Weekly consumption',
           subtitle=f'{selected} · Units dispatched',
           notes=('Monday–Sunday weeks, using actual dispatch dates in South African time.',
                  'Receipts, reservations, cancelled parcels and starting AWC values are excluded.',
                  'Dispatch detail contains the parcel lines behind the selected weeks. Current weeks are partial.'),
           headings=('Product', 'Strength', 'Category', 'Period total',
                     *(f"{week['start']:%d %b %Y} – {week['end']:%d %b %Y}" +
                       (' (partial)' if week['is_partial'] else '') for week in weeks)),
           rows=((row['product'].name, row['product'].strength, row['product'].get_category_display(), row['total'],
                  *(cell['quantity'] for cell in row['cells'])) for row in rows),
           widths=(36, 23, 25, 17, *(26 for _ in weeks)), heatmap_start=5)
    _sheet(workbook, 'Averages', title=f'{company.name} · Average weekly consumption',
           subtitle=f"Actual dispatches through {report['average_end']:%d %b %Y} · Units per week",
           notes=('Last 8 weeks: total units ÷ 8 complete weeks, including weeks with no dispatches.',
                  'This year: units from 1 January to the last completed Sunday ÷ (elapsed days ÷ 7).',
                  'All time: units since each product’s first dispatch week ÷ complete weeks, including zero weeks. Blank means no history.'),
           headings=('Product', 'Strength', 'Category', 'AWC · Last 8 weeks', 'AWC · This year', 'AWC · All time',
                     '8-week units', '8-week denominator', 'Year units', 'Year weeks', 'All-time units',
                     'All-time weeks', 'First dispatch week'),
           rows=((row['product'].name, row['product'].strength, row['product'].get_category_display(),
                  row['avg_8'], row['avg_year'], row['avg_all'], row['units_8'], row['weeks_8'], row['units_year'],
                  row['weeks_year'], row['units_all'], row['weeks_all'], row['first_week']) for row in rows),
           widths=(36, 23, 25, 24, 23, 23, 17, 22, 17, 19, 19, 19, 23), average_columns=(4, 5, 6, 10, 12))
    items = period_items(company, report['period_start'], report['period_end'], as_of=report['as_of_at']).filter(
        product_id__in=[row['product'].pk for row in rows]).order_by('shipment__dispatched_at', 'shipment_id', 'pk')
    _sheet(workbook, 'Dispatch detail', title=f'{company.name} · Consumption detail',
           subtitle=f'{selected} · Every contributing parcel line',
           notes=('Sum Quantity by Product and Week starting to reconcile the Weekly consumption sheet.',
                  'Patient names use the saved dispatch record when available. Product and doctor labels use current records.',
                  'Only dispatched and delivered parcels with an actual dispatch timestamp contribute.'),
           headings=DETAIL_HEADERS, rows=_detail_rows(company, items), widths=DETAIL_WIDTHS, freeze='C8')
    return _save(workbook)


def consumption_detail_workbook(company, product, week, *, as_of=None):
    """Export all source lines for one product-week, including those beyond the modal's current page."""
    if product.company_id != company.pk:
        raise ValidationError('Choose a product from this practice.')
    start = week - timedelta(days=week.weekday())
    end = start + timedelta(days=6)
    workbook = Workbook(write_only=True, iso_dates=True)
    _sheet(workbook, 'Dispatch detail', title=f'{product.name} · {product.strength}'.rstrip(' ·'),
           subtitle=f'{start:%d %b %Y} – {end:%d %b %Y} · Units dispatched',
           notes=(company.name, 'Every contributing parcel line for this product and week.',
                  'Actual dispatch dates in South African time. Patient names use the saved dispatch record when available.'),
           headings=DETAIL_HEADERS, rows=_detail_rows(company, weekly_items(company, product, start, as_of=as_of).order_by(
               'shipment__dispatched_at', 'shipment_id', 'pk')),
           widths=DETAIL_WIDTHS, freeze='C8')
    return _save(workbook)
