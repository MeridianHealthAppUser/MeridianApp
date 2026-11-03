"""Purchase-order documents use recorded product wording and real receipt quantities."""

from datetime import date, datetime
from pathlib import Path
import re
from tempfile import SpooledTemporaryFile
from threading import Lock
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import MedicationProduct


SAST = ZoneInfo('Africa/Johannesburg')
HEADERS = ('Product', 'Strength', 'Category', 'Supplier code', 'Ordered', 'Received', 'Remaining')
TEAL = '007F79'
INK = '153F44'
DEMO_LABEL = 'DEMO – NOT FOR ORDERING'
_FONT_LOCK = Lock()
_ILLEGAL_TEXT = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')


def _text(value):
    return _ILLEGAL_TEXT.sub(' ', str(value if value is not None else ''))


def _created(order):
    value = order.created_at
    return timezone.localtime(value, SAST).replace(tzinfo=None) if timezone.is_aware(value) else value


def _lines(order):
    """Never render a malformed cross-practice relationship, including when called outside a view."""
    lines = list(order.items.all())
    categories = dict(MedicationProduct.Category.choices)
    result = []
    for item in lines:
        if (item.company_id != order.company_id or item.purchase_order_id != order.pk
                or item.product.company_id != order.company_id):
            raise ValidationError('The purchase order contains an item from another practice.')
        result.append((_text(item.product_name), _text(item.strength),
                       _text(categories.get(item.category, item.category)), _text(item.supplier_code), item.quantity,
                       item.quantity_received, item.quantity - item.quantity_received))
    return result


def purchase_order_workbook(order):
    """Return a rewound XLSX file with typed metadata, literal snapshots and receipt progress."""
    rows = _lines(order)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Purchase order'
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = 'A8'
    sheet.print_title_rows = '7:7'
    sheet.page_setup.orientation = 'landscape'
    widths = (42, 24, 25, 22, 14, 14, 14)
    for column, width in zip('ABCDEFG', widths):
        sheet.column_dimensions[column].width = width

    def write(row, column, value, *, bold=False, header=False):
        cell = sheet.cell(row, column, _text(value) if isinstance(value, str) else value)
        if isinstance(value, str):
            cell.data_type = 's'
        elif isinstance(value, datetime):
            cell.number_format = 'dd mmm yyyy hh:mm'
        elif isinstance(value, date):
            cell.number_format = 'dd mmm yyyy'
        elif isinstance(value, int):
            cell.number_format = '#,##0'
        cell.font = Font(name='Arial', size=10, color='FFFFFF' if header else INK, bold=bold or header)
        cell.alignment = Alignment(vertical='top', wrap_text=True,
                                   horizontal='right' if isinstance(value, int) and not header else 'left')
        if header:
            cell.fill = PatternFill('solid', fgColor=TEAL)
        return cell

    sheet.merge_cells('A1:G1')
    company_cell = write(1, 1, order.company.name, bold=True)
    company_cell.font = Font(name='Arial', size=18, bold=True, color=INK)
    sheet.row_dimensions[1].height = max(32, ((len(order.company.name) + 99) // 100) * 24)
    sheet.merge_cells('A2:G2')
    write(2, 1, 'Purchase order', bold=True).font = Font(name='Arial', size=14, bold=True, color=TEAL)
    sheet.row_dimensions[2].height = 27
    for row, label, value, other_label, other in (
        (3, 'Reference', order.reference, 'Status', order.get_status_display()),
        (4, 'Created (SAST)', _created(order), 'ETA', order.eta),
        (5, 'Supplier', order.supplier_name or 'Unassigned', 'Email', order.supplier_email),
    ):
        write(row, 1, label, bold=True)
        sheet.merge_cells(start_row=row, start_column=2, end_row=row, end_column=3)
        write(row, 2, value)
        write(row, 4, other_label, bold=True)
        sheet.merge_cells(start_row=row, start_column=5, end_row=row, end_column=7)
        write(row, 5, other)
        sheet.row_dimensions[row].height = 24
    sheet.row_dimensions[5].height = max(24, ((len(order.supplier_name) + 45) // 46) * 14 + 8,
                                           ((len(order.supplier_email) + 39) // 40) * 14 + 8)
    sheet.row_dimensions[6].height = 12
    if getattr(order, 'demo_data_set_id', None) is not None:
        sheet.merge_cells('A6:G6')
        notice = write(6, 1, DEMO_LABEL, bold=True)
        notice.font = Font(name='Arial', size=11, bold=True, color='9A5A00')
        notice.fill = PatternFill('solid', fgColor='FFF5DF')
        sheet.row_dimensions[6].height = 26
    for column, heading in enumerate(HEADERS, 1):
        write(7, column, heading, header=True)
    sheet.row_dimensions[7].height = 27
    for row_number, values in enumerate(rows, 8):
        for column, value in enumerate(values, 1):
            write(row_number, column, value)
        lines = max(sum(max(1, (len(part) + int(widths[column]) - 4) // (int(widths[column]) - 3))
                        for part in _text(value).split('\n')) for column, value in enumerate(values))
        sheet.row_dimensions[row_number].height = min(409, max(28, lines * 13 + 8))
    last = 7 + len(rows)
    sheet.auto_filter.ref = f'A7:G{last}'
    total_row = last + 2
    write(total_row, 1, 'Total units', bold=True)
    for column in (5, 6, 7):
        write(total_row, column, sum(row[column - 1] for row in rows), bold=True)
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


def _fonts():
    with _FONT_LOCK:
        if 'MeridianPurchase' not in pdfmetrics.getRegisteredFontNames():
            directory = Path(reportlab.__file__).parent / 'fonts'
            pdfmetrics.registerFont(TTFont('MeridianPurchase', str(directory / 'Vera.ttf')))
            pdfmetrics.registerFont(TTFont('MeridianPurchase-Bold', str(directory / 'VeraBd.ttf')))
            pdfmetrics.registerFontFamily('MeridianPurchase', normal='MeridianPurchase', bold='MeridianPurchase-Bold')


def purchase_order_pdf(order):
    """Return a real, paginated PDF; supplier and prices are never invented."""
    rows = _lines(order)
    _fonts()
    ink, teal = colors.HexColor('#' + INK), colors.HexColor('#' + TEAL)
    body = ParagraphStyle('PurchaseBody', fontName='MeridianPurchase', fontSize=9, leading=13, textColor=ink)
    small = ParagraphStyle('PurchaseSmall', parent=body, fontSize=8, leading=11, textColor=colors.HexColor('#607C87'))
    heading = ParagraphStyle('PurchaseHeading', parent=body, fontName='MeridianPurchase-Bold', fontSize=18, leading=24)
    title = ParagraphStyle('PurchaseTitle', parent=body, fontName='MeridianPurchase-Bold', fontSize=13, leading=18, textColor=teal)
    column = ParagraphStyle('PurchaseColumn', parent=body, fontName='MeridianPurchase-Bold', fontSize=8, leading=11,
                            textColor=colors.white)
    number = ParagraphStyle('PurchaseNumber', parent=body, alignment=TA_RIGHT)

    def paragraph(value, style=body):
        return Paragraph(escape(_text(value)).replace('\n', '<br/>'), style)

    output = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode='w+b')
    document = SimpleDocTemplate(output, pagesize=A4, leftMargin=42, rightMargin=42, topMargin=38, bottomMargin=46,
                                 title=f'Purchase order {order.reference}', author=_text(order.company.name))
    width = A4[0] - 84
    story = [paragraph(order.company.name, heading), Spacer(1, 6),
             paragraph(f'Purchase order {order.reference}', title), Spacer(1, 16)]
    demo = getattr(order, 'demo_data_set_id', None) is not None
    if demo:
        notice = ParagraphStyle('DemoNotice', parent=title, fontSize=11, textColor=colors.HexColor('#9A5A00'))
        story.extend([paragraph(DEMO_LABEL, notice), Spacer(1, 12)])
    created = _created(order).strftime('%d %b %Y, %H:%M') + ' SAST'
    metadata = Table([
        [paragraph('Created', small), paragraph(created), paragraph('Status', small), paragraph(order.get_status_display())],
        [paragraph('ETA', small), paragraph(order.eta.strftime('%d %b %Y') if order.eta else '-'),
         paragraph('Supplier', small), paragraph(order.supplier_name or 'Unassigned')],
        [paragraph('Email', small), paragraph(order.supplier_email or '-'), '', ''],
    ], colWidths=[width * .13, width * .42, width * .13, width * .32])
    metadata.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0),
                                  ('RIGHTPADDING', (0, 0), (-1, -1), 10), ('BOTTOMPADDING', (0, 0), (-1, -1), 9)]))
    story.extend([metadata, Spacer(1, 12)])
    headings = ('Product', 'Category', 'Ordered', 'Received', 'Remaining')
    data = [[paragraph(label, column) for label in headings]]
    for product, strength, category, supplier_code, ordered, received, remaining in rows:
        product_text = f'<b>{escape(product)}</b>' + (f'<br/>{escape(strength)}' if strength else '')
        if supplier_code:
            product_text += f'<br/>Code: {escape(supplier_code)}'
        data.append([Paragraph(product_text, body), paragraph(category), paragraph(ordered, number),
                     paragraph(received, number), paragraph(remaining, number)])
    totals = tuple(sum(row[index] for row in rows) for index in (4, 5, 6))
    data.append([paragraph('Total units', title), '', *(paragraph(value, number) for value in totals)])
    table = Table(data, colWidths=[width * .41, width * .21, width * .12, width * .12, width * .14], repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), teal), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 8), ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('ROWBACKGROUNDS', (0, 1), (-1, -2), [colors.white, colors.HexColor('#F6FAF9')]),
        ('LINEBELOW', (0, 0), (-1, -1), .4, colors.HexColor('#DCE8E6')),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#E4F5F1')),
    ]))
    story.append(table)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor('#DCE8E6'))
        canvas.line(42, 34, A4[0] - 42, 34)
        canvas.setFont('MeridianPurchase', 8)
        canvas.setFillColor(colors.HexColor('#607C87'))
        label = f'{DEMO_LABEL} · {order.reference}' if demo else f'Purchase order {order.reference}'
        canvas.drawString(42, 22, label)
        canvas.drawRightString(A4[0] - 42, 22, f'Page {doc.page}')
        canvas.restoreState()

    try:
        document.build(story, onFirstPage=footer, onLaterPages=footer)
        output.seek(0)
        return output
    except Exception:
        output.close()
        raise
