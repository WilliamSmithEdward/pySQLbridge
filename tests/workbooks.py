"""Writing .xlsx packages to read back, in the shape Excel writes them.

Built here rather than committed as a file, because a workbook is a zip and a
zip in the tree is a fixture nobody can read, edit or review. What is in the
tree is the XML, which is the thing being parsed.

The shape is not invented. It was taken from a workbook Excel 16.0 saved,
whose People sheet held these cells:

    <row r="2" spans="1:6"><c r="A2"><v>1</v></c>
      <c r="B2" t="s"><v>6</v></c>
      <c r="C2"><v>99.5</v></c>
      <c r="D2" s="1"><v>45306</v></c>
      <c r="E2" t="b"><v>1</v></c>
      <c r="F2" t="s"><v>7</v></c></row>
    <row r="4" spans="1:6"><c r="A4"><v>3</v></c>
      <c r="B4" t="s"><v>9</v></c>
      <c r="D4" s="1"><v>23803</v></c> ...

so: text is an index into the shared table, a number is written as typed, a
date is a number whose style says it is one, a cell holding nothing is not
written at all, and a formula keeps its last value in a <v> beside it. The
namespaces below are Excel's own, because stripping them is one of the things
being tested.

scripts/workbook_probe.py drives a real Excel and reads what it saves back
through the same reader, which is what says this fixture still resembles what
Excel does.
"""

import datetime
import zipfile

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELATIONSHIPS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE = "http://schemas.openxmlformats.org/package/2006/relationships"

# The one style index this writes, pointing at the one number format that
# makes a number a date. Excel numbers custom formats from 164.
DATE_STYLE = 1
DATE_FORMAT_ID = 164

# Excel's day nought, so a datetime can be written as the number Excel would
# have written for it.
SERIAL_EPOCH = datetime.datetime(1899, 12, 30)


class Formula:
    """A cell holding a formula, and the value Excel last worked out for it."""

    def __init__(self, formula: str, cached, kind: str | None = None) -> None:
        self.formula = formula
        self.cached = cached
        self.kind = kind


class Error:
    """A cell Excel could not work out, such as #DIV/0!."""

    def __init__(self, text: str = "#DIV/0!") -> None:
        self.text = text


class Inline:
    """A cell whose text is in the sheet rather than the shared table."""

    def __init__(self, text: str) -> None:
        self.text = text


class Raw:
    """A cell written exactly as given, for shapes nothing else produces."""

    def __init__(self, attributes: str, body: str) -> None:
        self.attributes = attributes
        self.body = body


def serial(moment: datetime.datetime) -> float:
    """Excel's number of days for a moment, in the 1900 workbook."""
    delta = moment - SERIAL_EPOCH
    return delta.days + delta.seconds / 86400.0


def _letters(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _escaped(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _cell(reference: str, value, strings: list[str]) -> str:
    """One cell, as Excel would have written it."""
    if isinstance(value, Raw):
        return f'<c r="{reference}"{value.attributes}>{value.body}</c>'
    if isinstance(value, Error):
        return f'<c r="{reference}" t="e"><v>{_escaped(value.text)}</v></c>'
    if isinstance(value, Inline):
        return (f'<c r="{reference}" t="inlineStr">'
                f"<is><t>{_escaped(value.text)}</t></is></c>")
    if isinstance(value, Formula):
        kind = f' t="{value.kind}"' if value.kind else ""
        return (f'<c r="{reference}"{kind}>'
                f"<f>{_escaped(value.formula)}</f>"
                f"<v>{_escaped(str(value.cached))}</v></c>")
    if isinstance(value, datetime.datetime):
        # repr rather than a format: %g keeps six significant digits, which
        # is enough for the day and loses the time of day inside it.
        return (f'<c r="{reference}" s="{DATE_STYLE}">'
                f"<v>{serial(value)!r}</v></c>")
    if isinstance(value, bool):
        return f'<c r="{reference}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{reference}"><v>{value}</v></c>'
    if value is None:
        return ""
    if value not in strings:
        strings.append(value)
    return f'<c r="{reference}" t="s"><v>{strings.index(value)}</v></c>'


def _sheet(rows: list[list], strings: list[str], first_row: int = 1) -> str:
    """A worksheet part. A row holding nothing at all is left out, as Excel
    leaves it out, so the reader has to take a row's number from its own
    reference rather than from how many came before it."""
    written = []
    for offset, row in enumerate(rows):
        number = first_row + offset
        cells = "".join(
            _cell(f"{_letters(at)}{number}", value, strings)
            for at, value in enumerate(row)
        )
        if cells:
            written.append(f'<row r="{number}">{cells}</row>')
    body = "".join(written)
    inside = f"<sheetData>{body}</sheetData>" if body else "<sheetData/>"
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{MAIN}" xmlns:r="{RELATIONSHIPS}">{inside}'
            f"</worksheet>")


def workbook(path, sheets: dict, *, date1904: bool = False,
             date_format: str = r"yyyy\-mm\-dd\ hh:mm:ss",
             styles: bool = True) -> object:
    """Write a workbook holding these sheets, and answer where it was written.

    Each sheet is a list of rows, each row a list of values. A str becomes a
    shared string, a number a number, a datetime a styled serial, and None a
    cell that is simply not written.
    """
    strings: list[str] = []
    parts: dict[str, str] = {}
    entries = []
    for position, (name, rows) in enumerate(sheets.items(), start=1):
        part = f"xl/worksheets/sheet{position}.xml"
        parts[part] = _sheet(rows, strings)
        entries.append((name, position, part))

    setting = ' date1904="1"' if date1904 else ""
    listed = "".join(
        f'<sheet name="{_escaped(name)}" sheetId="{position}" '
        f'r:id="rId{position}"/>'
        for name, position, _ in entries
    )
    parts["xl/workbook.xml"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{MAIN}" xmlns:r="{RELATIONSHIPS}">'
        f"<workbookPr{setting}/><sheets>{listed}</sheets></workbook>"
    )
    parts["xl/_rels/workbook.xml.rels"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{PACKAGE}">'
        + "".join(
            f'<Relationship Id="rId{position}" Type="{RELATIONSHIPS}/worksheet"'
            f' Target="worksheets/sheet{position}.xml"/>'
            for _, position, _ in entries
        )
        + "</Relationships>"
    )
    parts["xl/sharedStrings.xml"] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="{MAIN}" count="{len(strings)}" '
        f'uniqueCount="{len(strings)}">'
        + "".join(f"<si><t>{_escaped(one)}</t></si>" for one in strings)
        + "</sst>"
    )
    if styles:
        parts["xl/styles.xml"] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<styleSheet xmlns="{MAIN}">'
            f'<numFmts count="1"><numFmt numFmtId="{DATE_FORMAT_ID}" '
            f'formatCode="{_escaped(date_format)}"/></numFmts>'
            f'<cellXfs count="2"><xf numFmtId="0" xfId="0"/>'
            f'<xf numFmtId="{DATE_FORMAT_ID}" xfId="0" '
            f'applyNumberFormat="1"/></cellXfs></styleSheet>'
        )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts.items():
            archive.writestr(name, text)
    return path
