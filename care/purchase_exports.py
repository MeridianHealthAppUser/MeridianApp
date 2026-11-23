"""Purchase-order documents use recorded product wording and real receipt quantities."""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import re
from tempfile import SpooledTemporaryFile
from threading import Lock
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from django.contrib.staticfiles import finders
from django.core.exceptions import ValidationError
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import MedicationProduct
from .purchasing import format_rand, order_amounts, person_name


SAST = ZoneInfo('Africa/Johannesburg')
HEADERS = ('Product', 'Strength', 'Category', 'Supplier code', 'Description', 'Size', 'Unit price (excl VAT)', 'Ordered',
           'Line total (excl VAT)', 'Received', 'Remaining')
NAVY = '1D4A5E'
DASH = '–'
COLD_CHAIN_NOTE = 'Cold-chain products: deliver refrigerated (2–8 °C).'

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


def _items(order):
    """Never render a malformed cross-practice relationship, including when called outside a view."""
    items = list(order.items.all())
    for item in items:
        if (item.company_id != order.company_id or item.purchase_order_id != order.pk
                or item.product.company_id != order.company_id):
            raise ValidationError('The purchase order contains an item from another practice.')
    return items


def _amounts(order):
    """Saved prices and totals; None for an order created before prices were saved (or a bare test double)."""
    if getattr(order, 'vat_rate', None) is None:
        return None
    return order_amounts(order)


def _price(item):
    return getattr(item, 'unit_price', None)


def _description(item):
    """The supplier's wording saved on the line, else the catalogue wording saved at ordering."""
    return _text(getattr(item, 'description', '')) or ' '.join(
        part for part in (_text(item.product_name), _text(item.strength)) if part)


def _lines(order):
    """Excel rows in HEADERS order: snapshot wording, saved prices and real receipt progress."""
    items = _items(order)
    amounts = _amounts(order)
    categories = dict(MedicationProduct.Category.choices)
    return [(_text(item.product_name), _text(item.strength), _text(categories.get(item.category, item.category)),
             _text(item.supplier_code), _text(getattr(item, 'description', '')), _text(getattr(item, 'pack_size', '')),
             _price(item) if amounts else None, item.quantity, amounts['lines'][item.pk] if amounts else None,
             item.quantity_received, item.quantity - item.quantity_received) for item in items]


def _vat_label(rate):
    return f'VAT {rate.normalize():f}%'


def purchase_order_workbook(order):
    """Return a rewound XLSX file with typed metadata, literal snapshots, saved prices and receipt progress."""
    rows = _lines(order)
    amounts = _amounts(order)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Purchase order'
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = 'A8'
    sheet.print_title_rows = '7:7'
    sheet.page_setup.orientation = 'landscape'
    widths = (36, 14, 20, 18, 34, 10, 16, 11, 18, 11, 12)
    last_column = 'K'
    for column, width in zip('ABCDEFGHIJK', widths):
        sheet.column_dimensions[column].width = width

    def write(row, column, value, *, bold=False, header=False):
        cell = sheet.cell(row, column, _text(value) if isinstance(value, str) else value)
        if isinstance(value, str):
            cell.data_type = 's'
        elif isinstance(value, datetime):
            cell.number_format = 'dd mmm yyyy hh:mm'
        elif isinstance(value, date):
            cell.number_format = 'dd mmm yyyy'
        elif isinstance(value, Decimal):
            cell.number_format = '#,##0.00'
        elif isinstance(value, int):
            cell.number_format = '#,##0'
        numeric = isinstance(value, (int, Decimal)) and not isinstance(value, bool)
        cell.font = Font(name='Arial', size=10, color='FFFFFF' if header else INK, bold=bold or header)
        cell.alignment = Alignment(vertical='top', wrap_text=True, horizontal='right' if numeric and not header else 'left')
        if header:
            cell.fill = PatternFill('solid', fgColor=TEAL)
        return cell

    sheet.merge_cells(f'A1:{last_column}1')
    company_cell = write(1, 1, order.company.name, bold=True)
    company_cell.font = Font(name='Arial', size=18, bold=True, color=INK)
    sheet.row_dimensions[1].height = max(32, ((len(order.company.name) + 99) // 100) * 24)
    sheet.merge_cells(f'A2:{last_column}2')
    write(2, 1, 'Purchase order', bold=True).font = Font(name='Arial', size=14, bold=True, color=TEAL)
    sheet.row_dimensions[2].height = 27
    for row, label, value, other_label, other in (
        (3, 'Reference', order.reference, 'Status', order.get_status_display()),
        (4, 'Created', _created(order), 'ETA', order.eta),
        (5, 'Supplier', order.supplier_name or 'Unassigned', 'Email', order.supplier_email),
    ):
        write(row, 1, label, bold=True)
        sheet.merge_cells(start_row=row, start_column=2, end_row=row, end_column=3)
        write(row, 2, value)
        write(row, 4, other_label, bold=True)
        sheet.merge_cells(start_row=row, start_column=5, end_row=row, end_column=len(HEADERS))
        write(row, 5, other)
        sheet.row_dimensions[row].height = 24
    sheet.row_dimensions[5].height = max(24, ((len(order.supplier_name) + 45) // 46) * 14 + 8,
                                           ((len(order.supplier_email) + 39) // 40) * 14 + 8)
    sheet.row_dimensions[6].height = 12
    if getattr(order, 'demo_data_set_id', None) is not None:
        sheet.merge_cells(f'A6:{last_column}6')
        notice = write(6, 1, DEMO_LABEL, bold=True)
        notice.font = Font(name='Arial', size=11, bold=True, color='9A5A00')
        notice.fill = PatternFill('solid', fgColor='FFF5DF')
        sheet.row_dimensions[6].height = 26
    for column, heading in enumerate(HEADERS, 1):
        write(7, column, heading, header=True)
    sheet.row_dimensions[7].height = 40
    for row_number, values in enumerate(rows, 8):
        for column, value in enumerate(values, 1):
            if value is not None:
                write(row_number, column, value)
        lines = max(sum(max(1, (len(part) + int(widths[column]) - 4) // (int(widths[column]) - 3))
                        for part in _text(value).split('\n')) for column, value in enumerate(values))
        sheet.row_dimensions[row_number].height = min(409, max(28, lines * 13 + 8))
    last = 7 + len(rows)
    sheet.auto_filter.ref = f'A7:{last_column}{last}'
    total_row = last + 2
    write(total_row, 1, 'Total units', bold=True)
    for column in (8, 10, 11):
        write(total_row, column, sum(row[column - 1] for row in rows), bold=True)
    if amounts:
        for offset, (label, value) in enumerate((('Subtotal (excl VAT)', amounts['subtotal']),
                                                 (_vat_label(amounts['vat_rate']), amounts['vat']),
                                                 ('Total (incl VAT)', amounts['total'])), 1):
            write(total_row + offset, 1, label, bold=True)
            write(total_row + offset, 9, value, bold=label.startswith('Total'))
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


def _logo(height):
    """The Meridian logo at the given height, or None when the image isn't available."""
    path = finders.find('images/meridian-logo.png')
    if not path:
        return None
    width, natural = ImageReader(path).getSize()
    return Image(path, width=height * width / natural, height=height)


def purchase_order_pdf(order):
    """Return a real, paginated purchase order PDF from the details and prices saved on the order.

    Nothing is looked up again: a supplier, price or practice change after the order was created never changes it.
    Orders from before prices were saved print dashes for prices and no totals; nothing is ever invented.
    """
    items = _items(order)
    amounts = _amounts(order)
    supplier = getattr(order, 'supplier_details', None) or {}
    buyer = getattr(order, 'buyer_details', None) or {}
    bill_to, ship_to = buyer.get('bill_to') or {}, buyer.get('ship_to') or {}
    _fonts()
    ink, navy = colors.HexColor('#' + INK), colors.HexColor('#' + NAVY)
    line, panel, muted = colors.HexColor('#B9C7CE'), colors.HexColor('#E9EEF1'), colors.HexColor('#5B6B73')
    body = ParagraphStyle('PurchaseBody', fontName='MeridianPurchase', fontSize=8.5, leading=11.5, textColor=ink)
    bold = ParagraphStyle('PurchaseBold', parent=body, fontName='MeridianPurchase-Bold')
    small = ParagraphStyle('PurchaseSmall', parent=body, fontSize=7, leading=9, textColor=muted)
    label = ParagraphStyle('PurchaseLabel', parent=small, fontName='MeridianPurchase-Bold')
    band = ParagraphStyle('PurchaseBand', parent=body, fontName='MeridianPurchase-Bold', fontSize=22, leading=26,
                          textColor=colors.white)
    column = ParagraphStyle('PurchaseColumn', parent=body, fontName='MeridianPurchase-Bold', fontSize=7.5, leading=9.5)
    column_right = ParagraphStyle('PurchaseColumnRight', parent=column, alignment=TA_RIGHT)
    number = ParagraphStyle('PurchaseNumber', parent=body, alignment=TA_RIGHT)
    number_bold = ParagraphStyle('PurchaseNumberBold', parent=bold, alignment=TA_RIGHT)
    grand = ParagraphStyle('PurchaseGrand', parent=bold, fontSize=9.5, leading=12, textColor=colors.white)
    grand_right = ParagraphStyle('PurchaseGrandRight', parent=grand, alignment=TA_RIGHT)
    warning = ParagraphStyle('PurchaseWarning', parent=bold, textColor=colors.HexColor('#B3261E'))

    def paragraph(value, style=body):
        text = _text(value)
        if not text:
            return Paragraph(DASH, style)
        markup = escape(text).replace('\n', '<br/>')
        if '@' in text and ' ' not in text and len(text) > 18:
            markup = markup.replace('@', '<br/>@', 1)  # a long email breaks before its domain, not mid-word
        return Paragraph(markup, style)

    def lines(values):
        return '\n'.join(_text(value) for value in values if value) if values else ''

    output = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode='w+b')
    document = SimpleDocTemplate(output, pagesize=A4, leftMargin=36, rightMargin=36, topMargin=34, bottomMargin=44,
                                 title=f'Purchase order {order.reference}', author=_text(order.company.name))
    width = A4[0] - 72
    demo = getattr(order, 'demo_data_set_id', None) is not None
    story = []

    # The band and the logo.
    logo = _logo(30) or paragraph(order.company.name, bold)
    header = Table([[Paragraph('PURCHASE ORDER', band), logo]], colWidths=[width - 170, 170], rowHeights=[58])
    header.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, 0), navy), ('BOX', (0, 0), (-1, -1), 1.4, navy),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('ALIGN', (1, 0), (1, 0), 'CENTER'),
        ('LEFTPADDING', (0, 0), (0, 0), 16),
    ]))
    story.append(header)

    # Order facts.
    created_by = getattr(order, 'created_by', None)
    facts = [('PO number', order.reference), ('Date of order', _created(order).strftime('%d %b %Y')),
             ('Delivery requested', order.eta.strftime('%d %b %Y') if order.eta else ''),
             ('Ordered by', person_name(created_by) if created_by else '')]
    facts_table = Table([[[paragraph(name.upper(), label), paragraph(value, bold)] for name, value in facts]],
                        colWidths=[width / 4] * 4)
    facts_table.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), .6, line), ('LINEAFTER', (0, 0), (-2, -1), .6, line),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 7), ('RIGHTPADDING', (0, 0), (-1, -1), 7),
    ]))
    story.append(facts_table)
    if demo:
        notice = ParagraphStyle('DemoNotice', parent=bold, fontSize=10, textColor=colors.HexColor('#9A5A00'))
        story.extend([Spacer(1, 8), paragraph(DEMO_LABEL, notice)])
    story.append(Spacer(1, 12))

    # Supplier, Bill to, Ship to.
    if supplier:
        vat = supplier.get('vat_number') if supplier.get('vat_registered', True) else 'Not VAT-registered'
        supplier_rows = [('Name', supplier.get('registered_name') or supplier.get('name') or order.supplier_name),
                         ('Reg. no.', supplier.get('registration_number')), ('VAT reg. no.', vat),
                         ('SAHPRA', supplier.get('sahpra_licence_number')),
                         ('Address', lines(supplier.get('address'))),
                         ('Orders email', supplier.get('email') or order.supplier_email),
                         ('Our account', supplier.get('account_number'))]
    else:
        supplier_rows = [('Name', order.supplier_name or 'Unassigned'), ('Orders email', order.supplier_email)]
    doctor = bill_to.get('doctor_name', '')
    bill_rows = [('Entity', bill_to.get('name')), ('Practice', bill_to.get('practice') or order.company.name),
                 ('Address', lines(bill_to.get('address')))]
    if doctor and doctor != bill_to.get('name'):
        bill_rows.append(('Doctor', doctor))
    bill_rows += [('HPCSA', bill_to.get('doctor_hpcsa')), ('Pharmacy Y', bill_to.get('pharmacy_number')),
                  ('VAT reg. no.', bill_to.get('vat_number') or ('N/A' if bill_to else '')),
                  ('Accounts', bill_to.get('accounts_email'))]
    ship_rows = [('Deliver to', ship_to.get('name')), ('Address', lines(ship_to.get('address'))),
                 ('Contact', ship_to.get('contact')), ('Phone', ship_to.get('phone')),
                 ('Instructions', ship_to.get('instructions'))]
    box_width = (width - 16) / 3

    party_value = ParagraphStyle('PurchasePartyValue', parent=body, fontSize=8, leading=10.5)
    party_name = ParagraphStyle('PurchasePartyName', parent=party_value, fontName='MeridianPurchase-Bold')
    party_label = ParagraphStyle('PurchasePartyLabel', parent=small, fontSize=6.8, leading=10.5)

    def party(title, rows):
        data = [[Paragraph(title.upper(), column), '']]
        data += [[paragraph(name, party_label), paragraph(value, party_name if index == 0 else party_value)]
                 for index, (name, value) in enumerate(rows)]
        table = Table(data, colWidths=[62, box_width - 62])
        table.setStyle(TableStyle([
            ('SPAN', (0, 0), (-1, 0)), ('BACKGROUND', (0, 0), (-1, 0), panel), ('BOX', (0, 0), (-1, -1), .6, line),
            ('LINEBELOW', (0, 0), (-1, 0), .6, line), ('LINEBELOW', (0, 1), (-1, -2), .3, colors.HexColor('#E3E9EC')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('TOPPADDING', (0, 0), (-1, -1), 3.5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3.5), ('LEFTPADDING', (0, 0), (-1, -1), 5), ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ]))
        return table

    parties = Table([[party('Supplier', supplier_rows), '', party('Bill to', bill_rows), '', party('Ship to', ship_rows)]],
                    colWidths=[box_width, 8, box_width, 8, box_width])
    parties.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0),
                                 ('RIGHTPADDING', (0, 0), (-1, -1), 0), ('TOPPADDING', (0, 0), (-1, -1), 0),
                                 ('BOTTOMPADDING', (0, 0), (-1, -1), 0)]))
    story.extend([parties, Spacer(1, 12)])

    # The lines.
    headings = [Paragraph('Product code', column), Paragraph('Description', column), Paragraph('Size', column),
                Paragraph('Unit price<br/>(excl VAT)', column_right), Paragraph('Qty', column_right),
                Paragraph('Line total<br/>(excl VAT)', column_right)]
    data = [headings]
    for item in items:
        price = _price(item) if amounts else None
        data.append([paragraph(item.supplier_code, bold), paragraph(_description(item)),
                     paragraph(getattr(item, 'pack_size', '')),
                     paragraph(format_rand(price) if price is not None else '', number),
                     paragraph(item.quantity, number),
                     paragraph(format_rand(amounts['lines'][item.pk]) if amounts else '', number)])
    table = Table(data, colWidths=[width * .19, width * .33, width * .09, width * .15, width * .07, width * .17], repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), panel), ('BOX', (0, 0), (-1, -1), .6, line),
        ('LINEBELOW', (0, 0), (-1, 0), .6, line), ('LINEBELOW', (0, 1), (-1, -1), .3, colors.HexColor('#E3E9EC')),
        ('BACKGROUND', (0, 1), (0, -1), colors.HexColor('#F4F7F8')), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 6), ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ]))
    story.extend([table, Spacer(1, 10)])

    # Terms on the left, totals on the right.
    terms = [paragraph('Payment terms', bold), paragraph(supplier.get('payment_terms', '')), Spacer(1, 5),
             paragraph('Notes', bold), paragraph('Please confirm receipt of this order and the delivery date by email.')]
    terms_table = Table([[terms]], colWidths=[width - 228])
    terms_table.setStyle(TableStyle([('BOX', (0, 0), (-1, -1), .6, line), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                                     ('LEFTPADDING', (0, 0), (-1, -1), 8), ('TOPPADDING', (0, 0), (-1, -1), 6),
                                     ('BOTTOMPADDING', (0, 0), (-1, -1), 7)]))
    if amounts:
        totals = Table([[paragraph('Subtotal (excl VAT)'), paragraph(format_rand(amounts['subtotal']), number)],
                        [paragraph(_vat_label(amounts['vat_rate'])), paragraph(format_rand(amounts['vat']), number)],
                        [Paragraph('Total (incl VAT)', grand), Paragraph(format_rand(amounts['total']), grand_right)]],
                       colWidths=[118, 102])
        totals.setStyle(TableStyle([
            ('BOX', (0, 0), (-1, -1), .6, line), ('LINEBELOW', (0, 0), (-1, -2), .3, colors.HexColor('#E3E9EC')),
            ('BACKGROUND', (0, -1), (-1, -1), navy), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ]))
    else:
        totals = ''
    summary = Table([[terms_table, totals]], colWidths=[width - 220, 220])
    summary.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0),
                                 ('RIGHTPADDING', (0, 0), (-1, -1), 0), ('ALIGN', (1, 0), (1, 0), 'RIGHT')]))
    story.extend([summary, Spacer(1, 12)])

    # Authorised by and the supplier's acknowledgement.
    def signature(title, value, caption):
        box = Table([[paragraph(title.upper(), label)], [paragraph(value, bold)], [''], [paragraph(caption, small)]],
                    colWidths=[(width - 12) / 2], rowHeights=[None, None, 16, None])
        box.setStyle(TableStyle([('BOX', (0, 0), (-1, -1), .6, line), ('LINEABOVE', (0, 3), (-1, 3), .5, muted),
                                 ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                                 ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3)]))
        return box

    signatures = Table([[signature('Authorised by', doctor, 'Signature'), '',
                         signature('Date', _created(order).strftime('%d %b %Y'), 'Supplier acknowledgement')]],
                       colWidths=[(width - 12) / 2, 12, (width - 12) / 2])
    signatures.setStyle(TableStyle([('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                                    ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0)]))
    story.append(signatures)
    if any(getattr(item.product, 'requires_cold_chain', False) for item in items):
        story.extend([Spacer(1, 9), Paragraph(COLD_CHAIN_NOTE, warning)])

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor('#DCE3E6'))
        canvas.line(36, 32, A4[0] - 36, 32)
        canvas.setFont('MeridianPurchase', 7.5)
        canvas.setFillColor(muted)
        label_text = f'{DEMO_LABEL} · {order.reference}' if demo else f'{_text(order.company.name)} · {order.reference}'
        canvas.drawString(36, 21, label_text)
        canvas.drawRightString(A4[0] - 36, 21, f'Page {doc.page}')
        canvas.restoreState()

    try:
        document.build(story, onFirstPage=footer, onLaterPages=footer)
        output.seek(0)
        return output
    except Exception:
        output.close()
        raise
