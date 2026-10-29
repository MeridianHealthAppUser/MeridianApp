"""Downloadable invoices rendered from issued records without changing them."""

from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

import reportlab
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from care.billing import money


def invoice_pdf(*, invoice, company, recipient, lines):
    """Use saved line amounts and billing identity, with wrapped text and automatic page breaks."""
    fonts = Path(reportlab.__file__).parent / 'fonts'
    for name, filename in (('Invoice', 'Vera.ttf'), ('InvoiceBold', 'VeraBd.ttf')):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(fonts / filename)))
    ink = colors.HexColor('#1D4A5E')
    pale = colors.HexColor('#A5E3F0')
    text = ParagraphStyle('InvoiceText', fontName='Invoice', fontSize=10, leading=15, textColor=ink)
    heading = ParagraphStyle('InvoiceHeading', parent=text, fontName='InvoiceBold', fontSize=20, leading=26)
    label = ParagraphStyle('InvoiceLabel', parent=text, fontName='InvoiceBold')

    def paragraph(value, style=text):
        return Paragraph(escape(str(value)), style)

    output = BytesIO()
    document = SimpleDocTemplate(output, pagesize=A4, rightMargin=42, leftMargin=42,
                                 topMargin=42, bottomMargin=42, title=f'Invoice {invoice.invoice_number}',
                                 author=company.name)
    content = [paragraph(company.name, heading), Spacer(1, 14),
               paragraph(f'Invoice {invoice.invoice_number}', label),
               paragraph(f'Issued {invoice.issued_on:%d %B %Y}'),
               paragraph(f'Due {invoice.due_on:%d %B %Y}'),
               paragraph(f'Status: {invoice.get_status_display()}'), Spacer(1, 18),
               paragraph('Billed to', label), paragraph(recipient.get('name', ''))]
    if recipient.get('id_number'):
        content.append(paragraph(f'ID / passport: {recipient["id_number"]}'))
    if invoice.subscription_cycle_id:
        cycle = invoice.subscription_cycle
        content.append(paragraph(f'Period: {cycle.starts_on:%d %B %Y} – {(cycle.ends_on or cycle.starts_on):%d %B %Y}'))
    content.append(Spacer(1, 22))
    rows = [[paragraph('Description', label), paragraph('Amount', label)]]
    for line in lines:
        description = line.description + (f' × {line.quantity}' if line.quantity > 1 else '')
        rows.append([paragraph(description), paragraph(money(line.line_total))])
    if len(rows) == 1:
        rows.append([paragraph('Services'), paragraph(money(invoice.subtotal))])
    rows.append([paragraph('Total', label), paragraph(money(invoice.total), label)])
    table = Table(rows, colWidths=[document.width * .73, document.width * .27], repeatRows=1, hAlign='LEFT')
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), pale),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LINEABOVE', (0, -1), (-1, -1), 1, ink),
    ]))
    content.append(table)
    document.build(content)
    return output.getvalue()
