"""Small dependency-free XLSX reader/writer for Contact import templates.

The packaged Windows runtime is offline and does not bundle an Excel library.
This module deliberately covers the ordinary spreadsheet interchange used by
Contacts: first worksheet values, shared/inline strings, and a styled template.
"""
from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape


_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _tag(name: str) -> str:
    return f"{{{_MAIN_NS}}}{name}"


def _column_index(reference: str) -> int:
    letters = "".join(char for char in reference if char.isalpha()).upper()
    value = 0
    for char in letters:
        value = value * 26 + ord(char) - ord("A") + 1
    return max(value - 1, 0)


def _column_name(index: int) -> str:
    result = ""
    current = index + 1
    while current:
        current, remainder = divmod(current - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext())


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [_text(item) for item in root.findall(_tag("si"))]


def _first_sheet_path(archive: zipfile.ZipFile) -> str:
    fallback = "xl/worksheets/sheet1.xml"
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        first_sheet = workbook.find(f"{_tag('sheets')}/{_tag('sheet')}")
        if first_sheet is None:
            return fallback
        relationship_id = first_sheet.attrib.get(f"{{{_REL_NS}}}id")
        if not relationship_id:
            return fallback
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        for relationship in relationships.findall(f"{{{_PACKAGE_REL_NS}}}Relationship"):
            if relationship.attrib.get("Id") == relationship_id:
                target = relationship.attrib.get("Target", "")
                return target.lstrip("/") if target.startswith("xl/") else f"xl/{target.lstrip('/')}"
    except (KeyError, ET.ParseError):
        return fallback
    return fallback


def read_xlsx_rows(content: bytes) -> list[list[str]]:
    """Read values from the first worksheet of a standard XLSX workbook."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        shared = _shared_strings(archive)
        try:
            root = ET.fromstring(archive.read(_first_sheet_path(archive)))
        except (KeyError, ET.ParseError) as exc:
            raise ValueError("invalid_xlsx") from exc

    output: list[list[str]] = []
    for row in root.findall(f"{_tag('sheetData')}/{_tag('row')}"):
        cells: dict[int, str] = {}
        for cell in row.findall(_tag("c")):
            index = _column_index(cell.attrib.get("r", "A1"))
            cell_type = cell.attrib.get("t")
            if cell_type == "inlineStr":
                value = _text(cell.find(_tag("is")))
            else:
                raw = _text(cell.find(_tag("v")))
                if cell_type == "s" and raw.isdigit() and int(raw) < len(shared):
                    value = shared[int(raw)]
                else:
                    value = raw
            cells[index] = value.strip()
        if cells:
            output.append([cells.get(index, "") for index in range(max(cells) + 1)])
    return output


def _inline_cell(reference: str, value: str, *, style: int = 0) -> str:
    return f'<c r="{reference}" t="inlineStr" s="{style}"><is><t>{escape(value)}</t></is></c>'


def _worksheet_xml(rows: list[list[str]], widths: list[int], *, freeze: bool = False, filter_ref: str | None = None) -> str:
    columns = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, start=1)
    )
    row_xml = []
    for row_number, values in enumerate(rows, start=1):
        cells = "".join(
            _inline_cell(f"{_column_name(column)}{row_number}", str(value), style=1 if row_number == 1 else 0)
            for column, value in enumerate(values)
        )
        row_xml.append(f'<row r="{row_number}">{cells}</row>')
    pane = '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>' if freeze else '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
    auto_filter = f'<autoFilter ref="{filter_ref}"/>' if filter_ref else ""
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{_MAIN_NS}">{pane}<cols>{columns}</cols><sheetData>{''.join(row_xml)}</sheetData>{auto_filter}</worksheet>'''


def contacts_template_xlsx() -> bytes:
    """Create the English-first Contact import workbook returned by the API."""
    headers = [
        "email", "first_name", "last_name", "company", "title", "phone", "website",
        "segments", "tags", "timezone", "notes", "custom_fields", "source",
    ]
    contact_rows = [
        headers,
        [
            "alex@example.com", "Alex", "Chen", "Example Company", "Founder", "+1 555 0100",
            "https://example.com", "Finance; IT", "priority; conference", "Asia/Shanghai",
            "Met at the 2026 summit", '{"account_tier":"enterprise"}', "event_import",
        ],
    ]
    guide_rows = [
        ["Field", "Required", "Description", "Example"],
        ["email", "Yes", "A valid email address.", "alex@example.com"],
        ["first_name", "Name required", "Given name. Fill first_name or last_name at minimum.", "Alex"],
        ["last_name", "Name required", "Family name. Fill first_name or last_name at minimum.", "Chen"],
        ["company", "No", "Company or organization name.", "Example Company"],
        ["title", "No", "Job title.", "Founder"],
        ["phone", "No", "Phone number.", "+1 555 0100"],
        ["website", "No", "Company or contact website.", "https://example.com"],
        ["segments", "No", "Industry/customer groups. Separate multiple values with commas or semicolons.", "Finance; IT"],
        ["tags", "No", "Additional user tags. Separate multiple values with commas or semicolons.", "priority; conference"],
        ["timezone", "No", "IANA timezone name.", "Asia/Shanghai"],
        ["notes", "No", "Free-text CRM notes.", "Met at the 2026 summit"],
        ["custom_fields", "No", "A JSON object for extra attributes.", '{"account_tier":"enterprise"}'],
        ["source", "No", "Import source label. Defaults to file_import when blank.", "event_import"],
    ]
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>'''
    package_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'''
    workbook = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{_MAIN_NS}" xmlns:r="{_REL_NS}"><sheets><sheet name="Contacts" sheetId="1" r:id="rId1"/><sheet name="Field Guide" sheetId="2" r:id="rId2"/></sheets></workbook>'''
    workbook_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'''
    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Arial"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Arial"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs></styleSheet>'''
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", package_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", styles)
        archive.writestr("xl/worksheets/sheet1.xml", _worksheet_xml(contact_rows, [28, 18, 18, 24, 20, 18, 30, 24, 24, 18, 32, 34, 20], freeze=True, filter_ref="A1:M2"))
        archive.writestr("xl/worksheets/sheet2.xml", _worksheet_xml(guide_rows, [20, 18, 72, 36], freeze=True))
    return output.getvalue()
