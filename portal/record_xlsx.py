"""Small streaming XML workbook writer: inline strings, no formulas or macros."""

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
