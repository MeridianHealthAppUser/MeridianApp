"""Small streaming XML workbook writer: inline strings, no formulas or macros."""

from decimal import Decimal
from io import BytesIO
import re
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from django.utils import timezone

from .record_views import RECORD_TIMEZONE


_INVALID_XML = re.compile(r'[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]')
_NAMESPACE = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_HEADERS = ('Date and time (SAST)', 'Practice', 'Category', 'Event', 'Detail', 'Recorded by',
            'Patient record', 'Source type', 'Source record', 'Patient-provided profile details', 'Part')


def _chunks(value):
    text = _INVALID_XML.sub('\ufffd', '' if value is None else str(value))
    # 15,000 Unicode codepoints remain below Excel's 32,767 UTF-16 unit limit
    # even when every character is a supplementary character (two units).
    return [text[start:start + 15000] for start in range(0, len(text), 15000)] or ['']


def _column(number):
    label = ''
    while number:
        number, remainder = divmod(number - 1, 26)
        label = chr(65 + remainder) + label
    return label


def _write_sheet(archive, name, rows):
    with archive.open(name, 'w') as stream:
        stream.write(f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{_NAMESPACE}"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><sheetData>'.encode())
        for index, values in enumerate(rows, 1):
            cells = ''.join(f'<c r="{_column(column)}{index}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>' for column, value in enumerate(values, 1))
            stream.write(f'<row r="{index}">{cells}</row>'.encode('utf-8'))
        stream.write(b'</sheetData></worksheet>')


def _history_rows(entries):
    yield _HEADERS
    for entry in entries:
        values = (timezone.localtime(entry['at'], RECORD_TIMEZONE).isoformat(), entry['company_name'],
                  entry['category_label'], entry['title'], entry['detail'], entry['actor'],
                  entry['patient_id'], entry['kind'], entry['id'],
                  '\n'.join(f"{answer['label']}: {answer['answer']}" for answer in entry.get('answers', [])))
        chunks = [_chunks(value) for value in values]
        parts = max(map(len, chunks))
        for part in range(parts):
            # Identity/source columns repeat on continued rows; long text chunks
            # retain all content rather than silently truncating clinical text.
            yield tuple(value[part] if part < len(value) else value[0] if column in (0, 1, 2, 5, 6, 7, 8) else ''
                        for column, value in enumerate(chunks)) + (f'{part + 1}/{parts}',)


def _summary_rows(summary):
    yield ('Field', 'Value')
    for label, value in summary:
        for index, chunk in enumerate(_chunks(value), 1):
            yield (_chunks(label)[0] if index == 1 else f'{label} (continued)', chunk)


def build_history_workbook(entries, *, summary):
    output = BytesIO()
    with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        archive.writestr('_rels/.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{_NAMESPACE}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="History" sheetId="1" r:id="rId1"/><sheet name="Export details" sheetId="2" r:id="rId2"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/></Relationships>')
        _write_sheet(archive, 'xl/worksheets/sheet1.xml', _history_rows(entries))
        _write_sheet(archive, 'xl/worksheets/sheet2.xml', _summary_rows(summary))
    return output.getvalue()


# Workbooks with several sheets: rows are (style, values), style '' for data, 'title' for a section
# heading and 'header' for column names. Numbers are number cells; everything else is text.
_STYLES = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="{_NAMESPACE}">'
    '<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="13"/><color rgb="FF0B4F4A"/><name val="Calibri"/></font></fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFE4F5F1"/><bgColor indexed="64"/></patternFill></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
    '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
)
_STYLE_INDEX = {'': 0, 'header': 1, 'title': 2}
_SHEET_NAME = re.compile(r'[\\/?*\[\]:]')


def _cell(reference, value, style):
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return f'<c r="{reference}" s="{style}"><v>{value}</v></c>'
    return f'<c r="{reference}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'


def _expand(values):
    """Very long text continues on the rows below, so nothing is cut off."""
    chunks = [[value] if value is None or isinstance(value, (int, float, Decimal)) else _chunks(value) for value in values]
    for part in range(max(map(len, chunks), default=1)):
        yield tuple(value[part] if part < len(value) else None for value in chunks)


def _write_table_sheet(archive, name, rows, widths, freeze):
    pane = '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>' if freeze else ''
    cols = ''.join(f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>' for index, width in enumerate(widths, 1))
    with archive.open(name, 'w') as stream:
        stream.write(f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{_NAMESPACE}"><sheetViews><sheetView workbookViewId="0">{pane}</sheetView></sheetViews>{"<cols>" + cols + "</cols>" if cols else ""}<sheetData>'.encode())
        index = 0
        for style, values in rows:
            for values_part in _expand(values):
                index += 1
                cells = ''.join(_cell(f'{_column(column)}{index}', value, _STYLE_INDEX[style])
                                for column, value in enumerate(values_part, 1) if value not in (None, ''))
                stream.write(f'<row r="{index}">{cells}</row>'.encode('utf-8'))
        stream.write(b'</sheetData></worksheet>')


def build_workbook(sheets):
    """sheets: (name, rows, column widths, freeze the first row) in the order they appear."""
    output = BytesIO()
    sheets = list(sheets)
    with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
        overrides = ''.join(f'<Override PartName="/xl/worksheets/sheet{number}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                            for number in range(1, len(sheets) + 1))
        archive.writestr('[Content_Types].xml', f'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>{overrides}</Types>')
        archive.writestr('_rels/.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        names = ''.join(f'<sheet name="{escape(_SHEET_NAME.sub("-", name)[:31])}" sheetId="{number}" r:id="rId{number}"/>'
                        for number, (name, *_rest) in enumerate(sheets, 1))
        archive.writestr('xl/workbook.xml', f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{_NAMESPACE}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{names}</sheets></workbook>')
        relationships = ''.join(f'<Relationship Id="rId{number}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{number}.xml"/>'
                                for number in range(1, len(sheets) + 1))
        styles = f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        archive.writestr('xl/_rels/workbook.xml.rels', f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{relationships}{styles}</Relationships>')
        archive.writestr('xl/styles.xml', _STYLES)
        for number, (_name, rows, widths, freeze) in enumerate(sheets, 1):
            _write_table_sheet(archive, f'xl/worksheets/sheet{number}.xml', rows, widths, freeze)
    return output.getvalue()
