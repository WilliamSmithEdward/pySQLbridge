"""Reading a workbook as tables, one per sheet.

An .xlsx is a zip of XML, so this needs nothing that is not in the standard
library. That matters more than it sounds: the usual way to read a workbook on
Windows is the Access database engine, which is a separate download, is
installed in one bit width, and refuses to load into a process of the other.
A bridge whose whole promise is "point it at a file" cannot start by asking
the user to install a driver, so the file is read directly.

What the format actually stores is worth knowing before reading further,
because three of its choices decide most of this module:

  Text is not in the sheet. A cell of type "s" holds an index into a shared
  string table, so the same word written a thousand times is stored once.

  A date is not a date. It is a number of days, and the only thing that makes
  45306 a date rather than the number 45306 is the number format its style
  points at. So the styles have to be read to know what a column holds.

  A cell that is empty is not there at all. Rows and cells carry their own
  references and both skip, so position comes from the reference and never
  from the count of what came before it.

  A table is not in the sheet either. What Excel calls a table and the object
  model calls a ListObject is its own part, holding the range it covers, the
  name a person gave it, and its column names; the sheet only points at it.
  So a sheet is read twice over: once whole, and once per table on it. Both
  are served, because both are things a person put there, and a table is the
  only one of the two that carries its own name.

Numbers are passed on as the text the file spelled them with, not as floats.
Excel keeps every number as a double, but it writes 1000 rather than 1000.0,
and handing that text to the same inference the CSV reader uses gets an
integer column for whole numbers rather than a float column full of values
ending .0.

The file may be one nobody here wrote, so the reading is bounded: a zip that
expands to more than it should, a sheet with more rows than could be served,
and a document with more sheets than a catalog should hold are all refused
rather than swallowed.
"""

from __future__ import annotations

import datetime
import logging
import posixpath
import re
import xml.etree.ElementTree as ElementTree
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .source import SourceError, missing_names, usable_names, wanted_names

log = logging.getLogger(__name__)

# Where the package keeps the parts this reads. The workbook is the index; the
# relationships turn its r:id references into paths.
WORKBOOK_PART = "xl/workbook.xml"
RELATIONSHIP_PART = "xl/_rels/workbook.xml.rels"
SHARED_STRINGS_PART = "xl/sharedStrings.xml"
STYLES_PART = "xl/styles.xml"

# Relationship types, matched on the last segment so that both the strict and
# the transitional namespaces resolve.
WORKSHEET_RELATIONSHIP = "worksheet"
TABLE_RELATIONSHIP = "table"

# What a served table was read from. A tab and a table on it are two names for
# overlapping rows, and a message that cannot say which it means is no help.
FROM_A_SHEET = "sheet"
FROM_A_TABLE = "table"

# Number formats built into the format, by id. 14 to 22 are the dates and
# times; 45 to 47 are elapsed times, which measure a duration rather than
# name a moment and so stay numbers.
BUILTIN_DATE_FORMATS = frozenset(range(14, 23))

# A custom format is a format code, and what makes one a date is a date field
# in it. Everything that could hold a letter without meaning one is taken out
# first: a quoted literal, a backslash escape, a bracketed condition, colour
# or locale, and the currency and percent signs.
_NOT_A_FIELD = re.compile(r'"[^"]*"|\\.|\[[^\]]*\]|_.|\*.')
_A_DATE_FIELD = re.compile(r"[ymdhs]", re.IGNORECASE)

# Excel writes a serial number of days. The 1900 workbook counts from
# 1900-01-01 as day 1 and believes 1900 was a leap year, so day 60 is a
# 29th of February that did not happen and every day after it is one too
# many. Two epochs, either side of the fiction, are what put the real dates
# back; the day itself has no date to be read as.
_LEAP_YEAR_FICTION = 60
EPOCH_BEFORE_THE_FICTION = datetime.datetime(1899, 12, 31)
EPOCH_AFTER_THE_FICTION = datetime.datetime(1899, 12, 30)
EPOCH_1904 = datetime.datetime(1904, 1, 1)

# A serial is a fraction of a day, and rounding it to the second is what
# stops 13:30 arriving as 13:29:59.999998.
SECONDS_IN_A_DAY = 86400

# Ceilings. The file is whatever a configuration pointed at, and a zip is a
# format that can be small on disk and enormous in memory.
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_SHEETS = 256
MAX_ROWS_PER_SHEET = 1_048_576          # what a sheet itself can hold
MAX_CELLS_PER_SHEET = 8_000_000

# Tables are counted separately from sheets, because a sheet can carry any
# number of them and a workbook that carries thousands is one this should
# refuse rather than serve a catalog nobody can read.
MAX_TABLES = 1024

# What a cell's type attribute can say. Anything else is a value this does not
# know how to read, and is refused rather than guessed at.
KNOWN_CELL_TYPES = frozenset({"n", "s", "str", "b", "e", "inlineStr", "d"})


class _Nothing:
    """A cell with nothing in it, as against one holding a value that is null.

    The difference decides whether a row exists. A row Excel wrote because
    somebody put #DIV/0! in it is a row, and every value in it is null; a row
    Excel wrote because somebody coloured it is not a row at all, and serving
    one would add a record to every count over the sheet.
    """

    def __repr__(self) -> str:                     # pragma: no cover - a debug aid
        return "NOTHING"


NOTHING = _Nothing()


@dataclass(frozen=True)
class Sheet:
    """One table to serve, as a header row and the rows under it.

    Named for what it usually is. It is also what a table on a sheet reads
    as, and `kind` says which, so that a caller filtering on one or the other
    does not have to guess from the name.
    """

    name: str
    headers: list[str]
    rows: list[list[object]]
    kind: str = FROM_A_SHEET
    sheet: str = ""             # the tab it was read from, table or not


@dataclass(frozen=True)
class _ListObject:
    """What a table part says about the range it covers.

    `headers` is the table's own column names rather than the cells of its
    header row. Excel keeps the two in step, and the part is the only one of
    them that exists when a table is written with no header row at all.
    """

    name: str
    first_row: int
    last_row: int
    first_column: int
    last_column: int
    headers: list[str]
    header_rows: int
    totals_rows: int


def _local(tag: str) -> str:
    """An element's name without its namespace.

    Matched on the local name throughout, because a workbook can be written
    in the transitional namespace or the strict one, and every producer that
    is not Excel picks for itself.

    Not remembered, though it is called once per element and a sheet holds
    only a handful of distinct tags. A profile said it was a sixth of the
    read and the clock said it was two per cent: cProfile charges about a
    microsecond to every call, and this is called 661,871 times on a sheet
    of 20,000 rows, so the profile was mostly measuring itself. An lru_cache
    here bought 2% and was taken back out.
    """
    return tag.rpartition("}")[2]


def _opened(path: Path) -> zipfile.ZipFile:
    """The workbook as a zip, or why it is not one."""
    try:
        archive = zipfile.ZipFile(path)
    except FileNotFoundError as exc:
        raise SourceError(f"could not read '{path}': {exc}") from exc
    except OSError as exc:
        raise SourceError(f"could not read '{path}': {exc}") from exc
    except zipfile.BadZipFile as exc:
        # The commonest one by far is an .xls: the old binary format shares
        # neither the extension's promise nor a single byte of this one, and
        # "not a zip file" does not tell anybody that.
        raise SourceError(
            f"'{path}' is not a workbook this can read: {exc}. An .xls saved "
            f"by an old Excel is a different format; open it and save it as "
            f".xlsx, or point at the sheet through \"access\" instead."
        ) from exc

    total = sum(item.file_size for item in archive.infolist())
    if total > MAX_UNCOMPRESSED_BYTES:
        archive.close()
        raise SourceError(
            f"'{path}' expands to {total} bytes, and this reads up to "
            f"{MAX_UNCOMPRESSED_BYTES}"
        )
    return archive


def _part(archive: zipfile.ZipFile, name: str, path: Path) -> bytes | None:
    """One part of the package, or None where the package has none."""
    try:
        return archive.read(name)
    except KeyError:
        return None
    except (OSError, zipfile.BadZipFile) as exc:
        raise SourceError(f"could not read {name} of '{path}': {exc}") from exc


def _parsed(raw: bytes, name: str, path: Path) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise SourceError(f"{name} of '{path}' is not valid XML: {exc}") from exc


def _shared_strings(archive: zipfile.ZipFile, path: Path) -> list[str]:
    """The workbook's string table, in the order cells index it.

    A string can be split into runs where part of it is formatted
    differently, and the runs are joined: the cell holds one value however
    many colours it was typed in.
    """
    raw = _part(archive, SHARED_STRINGS_PART, path)
    if raw is None:
        return []
    table: list[str] = []
    for item in _parsed(raw, SHARED_STRINGS_PART, path):
        if _local(item.tag) != "si":
            continue
        table.append("".join(
            piece.text or ""
            for piece in item.iter()
            if _local(piece.tag) == "t"
        ))
    return table


def _dated_styles(archive: zipfile.ZipFile, path: Path) -> set[int]:
    """Which style indexes mean the number under them is a moment.

    A cell points at a style, a style points at a number format, and a number
    format is what makes a count of days into a date. Nothing else in the
    file says so.
    """
    raw = _part(archive, STYLES_PART, path)
    if raw is None:
        return set()
    root = _parsed(raw, STYLES_PART, path)

    codes: dict[int, str] = {}
    for element in root.iter():
        if _local(element.tag) != "numFmt":
            continue
        try:
            codes[int(element.get("numFmtId", ""))] = element.get("formatCode", "")
        except ValueError:
            continue

    dated: set[int] = set()
    for group in root:
        if _local(group.tag) != "cellXfs":
            continue
        for index, style in enumerate(group):
            if _local(style.tag) != "xf":
                continue
            try:
                format_id = int(style.get("numFmtId", "0"))
            except ValueError:
                continue
            if format_id in BUILTIN_DATE_FORMATS or _is_a_date(codes.get(format_id)):
                dated.add(index)
    return dated


def _is_a_date(code: str | None) -> bool:
    """Whether a custom number format names a moment.

    Only the first section is read. A format can carry up to four, for
    positive, negative, zero and text, and the first is what a date is
    written with.
    """
    if not code:
        return False
    first = code.split(";")[0]
    return bool(_A_DATE_FIELD.search(_NOT_A_FIELD.sub("", first)))


def _moment(serial: float, since_1904: bool, where: str) -> datetime.datetime:
    """A serial number of days as the moment it stands for."""
    if since_1904:
        epoch = EPOCH_1904
    elif serial < _LEAP_YEAR_FICTION:
        epoch = EPOCH_BEFORE_THE_FICTION
    elif serial < _LEAP_YEAR_FICTION + 1:
        raise SourceError(
            f"{where} holds day {serial}, which Excel shows as 1900-02-29. "
            f"That day did not happen, so there is no date to serve."
        )
    else:
        epoch = EPOCH_AFTER_THE_FICTION

    whole = int(serial)
    seconds = round((serial - whole) * SECONDS_IN_A_DAY)
    try:
        return epoch + datetime.timedelta(days=whole, seconds=seconds)
    except (OverflowError, ValueError) as exc:
        raise SourceError(
            f"{where} holds day {serial}, which is not a date: {exc}"
        ) from exc


def _workbook_part(archive: zipfile.ZipFile, path: Path) -> ElementTree.Element:
    """The index of the package, parsed. Read once: it says both which sheets
    there are and which year the workbook counts days from."""
    raw = _part(archive, WORKBOOK_PART, path)
    if raw is None:
        raise SourceError(
            f"'{path}' has no {WORKBOOK_PART}, so it is not a workbook"
        )
    return _parsed(raw, WORKBOOK_PART, path)


def _sheet_paths(root: ElementTree.Element, archive: zipfile.ZipFile,
                 path: Path) -> list[tuple[str, str]]:
    """Each sheet's name and the part holding it, in the workbook's own order.

    The order is the workbook's rather than the archive's: sheet1.xml is
    whichever sheet was made first, and a person who moved a tab expects the
    order they see.
    """
    targets: dict[str, str] = {}
    relationships = _part(archive, RELATIONSHIP_PART, path)
    if relationships is not None:
        for element in _parsed(relationships, RELATIONSHIP_PART, path):
            if _local(element.tag) != "Relationship":
                continue
            kind = (element.get("Type") or "").rpartition("/")[2]
            if kind != WORKSHEET_RELATIONSHIP:
                continue
            identifier = element.get("Id")
            target = element.get("Target") or ""
            if identifier:
                targets[identifier] = _resolved(target)

    found: list[tuple[str, str]] = []
    for group in root:
        if _local(group.tag) != "sheets":
            continue
        for sheet in group:
            if _local(sheet.tag) != "sheet":
                continue
            name = sheet.get("name")
            reference = next(
                (value for key, value in sheet.attrib.items()
                 if _local(key) == "id"),
                None,
            )
            part = targets.get(reference or "")
            if name and part and part in archive.namelist():
                found.append((name, part))

    if not found:
        raise SourceError(f"'{path}' has no worksheets in it")
    return found


def _resolved(target: str) -> str:
    """A relationship target as a path inside the package.

    Targets are written relative to the part that declared them, which for the
    workbook means relative to xl/. An absolute one is already a package path
    apart from its leading slash.
    """
    if target.startswith("/"):
        return target[1:]
    return f"xl/{target}"


def _resolved_beside(part: str, target: str) -> str:
    """A relationship target as a package path, relative to the declaring part.

    A sheet's relationships live beside the sheet, so a table written as
    ../tables/table1.xml from xl/worksheets/sheet1.xml is xl/tables/table1.xml.
    """
    if target.startswith("/"):
        return target[1:]
    return posixpath.normpath(posixpath.join(posixpath.dirname(part), target))


def _relationships_of(part: str) -> str:
    """Where one part's own relationships are kept: _rels beside it."""
    directory, _, name = part.rpartition("/")
    return f"{directory}/_rels/{name}.rels" if directory else f"_rels/{name}.rels"


def _column_index(reference: str, where: str) -> int:
    """The column a cell reference names, counting A as nought."""
    letters = reference.rstrip("0123456789")
    if not letters:
        raise SourceError(f"{where} is not a cell reference")
    index = 0
    for letter in letters.upper():
        if not "A" <= letter <= "Z":
            raise SourceError(f"'{reference}' in {where} is not a cell reference")
        index = index * 26 + (ord(letter) - ord("A") + 1)
    return index - 1


def _reference(reference: str | None, fallback: int) -> str:
    """What to call a cell in a message: its own reference where it has one."""
    return reference or f"cell {fallback + 1}"


def _corner(reference: str, where: str) -> tuple[int, int]:
    """The row and column a cell reference names, the column counting A as 0."""
    column = _column_index(reference, where)
    digits = reference[len(reference.rstrip("0123456789")):]
    try:
        row = int(digits)
    except ValueError as exc:
        raise SourceError(f"'{reference}' in {where} names no row") from exc
    if row < 1:
        raise SourceError(f"'{reference}' in {where} names no row")
    return row, column


def _counted(element: ElementTree.Element, attribute: str, fallback: int,
             where: str) -> int:
    """One of a table's counts, which the format leaves out when it is usual."""
    written = element.get(attribute)
    if written is None:
        return fallback
    try:
        count = int(written)
    except ValueError as exc:
        raise SourceError(
            f"{where} has {attribute}='{written}', which is not a number"
        ) from exc
    if count < 0:
        raise SourceError(f"{where} has {attribute}='{written}'")
    return count


def _a_list_object(root: ElementTree.Element, where: str) -> _ListObject:
    """One table part, read.

    Three of its attributes decide what gets served and two of them are
    usually absent. headerRowCount is 1 unless it says otherwise, and the
    header row is inside the range rather than above it. totalsRowCount is 0
    unless it says otherwise, and a totals row is also inside the range: serve
    it and a workbook of ten rooms has eleven, one of them called Total.
    """
    name = (root.get("displayName") or root.get("name") or "").strip()
    if not name:
        raise SourceError(f"{where} is a table with no name")

    ref = root.get("ref")
    if not ref:
        raise SourceError(f"table '{name}' in {where} covers no cells")
    first, _, last = ref.partition(":")
    first_row, first_column = _corner(first, f"table '{name}' of {where}")
    last_row, last_column = (
        _corner(last, f"table '{name}' of {where}") if last
        else (first_row, first_column)
    )
    if last_row < first_row or last_column < first_column:
        raise SourceError(
            f"table '{name}' in {where} covers '{ref}', which runs backwards"
        )

    headers = [
        (column.get("name") or "").strip()
        for column in root.iter()
        if _local(column.tag) == "tableColumn"
    ]
    width = last_column - first_column + 1
    if len(headers) != width:
        raise SourceError(
            f"table '{name}' in {where} covers {width} columns and names "
            f"{len(headers)} of them"
        )

    header_rows = _counted(root, "headerRowCount", 1, f"table '{name}'")
    totals_rows = _counted(root, "totalsRowCount", 0, f"table '{name}'")
    if header_rows + totals_rows > last_row - first_row + 1:
        raise SourceError(
            f"table '{name}' in {where} is all heading and total, with no "
            f"rows between them"
        )

    return _ListObject(
        name=name,
        first_row=first_row,
        last_row=last_row,
        first_column=first_column,
        last_column=last_column,
        headers=headers,
        header_rows=header_rows,
        totals_rows=totals_rows,
    )


def _list_objects(archive: zipfile.ZipFile, part: str, sheet_name: str,
                  path: Path) -> list[_ListObject]:
    """Every table on one sheet, in the order they sit on it.

    Read from the sheet's own relationships, which is the only place that says
    a table exists. Cheap: the parts hold a range and some names, no cells, so
    this can run over a whole workbook before deciding which sheets to read.
    """
    where = f"sheet '{sheet_name}' of '{path}'"
    relationships = _relationships_of(part)
    raw = _part(archive, relationships, path)
    if raw is None:
        return []

    found: list[_ListObject] = []
    for element in _parsed(raw, relationships, path):
        if _local(element.tag) != "Relationship":
            continue
        if (element.get("Type") or "").rpartition("/")[2] != TABLE_RELATIONSHIP:
            continue
        target = _resolved_beside(part, element.get("Target") or "")
        body = _part(archive, target, path)
        if body is None:
            raise SourceError(
                f"{where} points at a table in {target}, which '{path}' does "
                f"not contain"
            )
        found.append(_a_list_object(_parsed(body, target, path), where))

    # The order tables were made in is the order they are related in, which is
    # not the order they are read in. Top to bottom, then left to right, is.
    found.sort(key=lambda one: (one.first_row, one.first_column))
    return found


def _cell_value(cell: ElementTree.Element, strings: list[str],
                dated: set[int], since_1904: bool, where: str) -> object:
    """What one cell holds: a value, None for null, or NOTHING for empty.

    An error cell holds null. #DIV/0! is not a value SQL has, and the thing it
    stands for is the absence of one, so it arrives as NULL: this is also what
    every other reader of the format does, so a workbook read here and the
    same workbook read through the Access engine agree about it.
    """
    kind = cell.get("t", "n")
    if kind not in KNOWN_CELL_TYPES:
        raise SourceError(f"{where} has cell type '{kind}', which this cannot read")

    if kind == "inlineStr":
        return "".join(
            piece.text or ""
            for piece in cell.iter()
            if _local(piece.tag) == "t"
        ) or NOTHING

    text = next(
        (child.text for child in cell if _local(child.tag) == "v"), None
    )
    if text is None or text == "":
        return NOTHING

    if kind == "e":
        return None
    if kind == "s":
        try:
            return strings[int(text)]
        except (ValueError, IndexError) as exc:
            raise SourceError(
                f"{where} points at string {text}, which the workbook's string "
                f"table does not have"
            ) from exc
    if kind == "str":
        return text
    if kind == "b":
        return text not in ("0", "false", "FALSE")
    if kind == "d":
        try:
            return datetime.datetime.fromisoformat(text)
        except ValueError as exc:
            raise SourceError(f"{where} holds '{text}', which is not a date") from exc

    # A number, which is a date only if its style says so.
    style = cell.get("s")
    if style is not None and dated:
        try:
            index = int(style)
        except ValueError:
            index = -1
        if index in dated:
            try:
                serial = float(text)
            except ValueError as exc:
                raise SourceError(
                    f"{where} is formatted as a date and holds '{text}'"
                ) from exc
            return _moment(serial, since_1904, where)
    return text


def _rows_of(archive: zipfile.ZipFile, part: str, sheet_name: str,
             strings: list[str], dated: set[int], since_1904: bool,
             path: Path) -> list[tuple[int, dict[int, object]]]:
    """Every row of a sheet that holds anything, by row number and column index.

    Sparse both ways: a row that holds nothing is not in the list, and a cell
    that holds nothing is not in its row. Turning that into rectangular rows
    needs the header first, so it is done a step later.

    The row number comes back with the row because a table on the sheet is a
    range of them, and which rows are inside it cannot be worked out from a
    position in a list that skipped whatever was blank.

    In the order the rows say they are in rather than the order they were
    written. Excel writes them in order and the two are the same, but the
    number is what a sheet means: a file whose rows ran 3, 1, 2 took the
    third row as the heading and served a column called '3' holding the rest,
    which is the wrong answer with nothing to say so. Only sorted where they
    actually arrive out of order, so the ordinary file pays one comparison a
    row and nothing else.
    """
    where = f"sheet '{sheet_name}' of '{path}'"
    found: list[tuple[int, dict[int, object]]] = []
    last = 0
    in_order = True
    cells = 0
    try:
        handle = archive.open(part)
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        raise SourceError(f"could not read {where}: {exc}") from exc

    with handle:
        try:
            for event, element in ElementTree.iterparse(handle, ("end",)):
                if _local(element.tag) != "row":
                    continue
                # A row with no number of its own follows the one before it,
                # which is what its position already said.
                number = _row_number(element.get("r"), last)
                in_order = in_order and number > last
                last = number
                row: dict[int, object] = {}
                for cell in element:
                    if _local(cell.tag) != "c":
                        continue
                    reference = cell.get("r")
                    at = (_column_index(reference, where) if reference
                          else len(row))
                    value = _cell_value(
                        cell, strings, dated, since_1904,
                        f"{_reference(reference, at)} of {where}",
                    )
                    if value is not NOTHING:
                        row[at] = value
                cells += len(row)
                if cells > MAX_CELLS_PER_SHEET:
                    raise SourceError(
                        f"{where} holds more than {MAX_CELLS_PER_SHEET} cells, "
                        f"which is more than this serves"
                    )
                if row:
                    found.append((number, row))
                    if len(found) > MAX_ROWS_PER_SHEET:
                        raise SourceError(
                            f"{where} holds more than {MAX_ROWS_PER_SHEET} rows"
                        )
                # The row is finished with, and a sheet held entirely in
                # parsed elements costs several times what its values do.
                element.clear()
        except ElementTree.ParseError as exc:
            raise SourceError(f"{where} is not valid XML: {exc}") from exc
    if not in_order:
        found.sort(key=lambda pair: pair[0])
    return found


def _row_number(written: str | None, last: int) -> int:
    """What row this is, or the one after the last where it does not say."""
    if written is None:
        return last + 1
    try:
        return int(written)
    except ValueError:
        return last + 1


def _squared(rows: list[tuple[int, dict[int, object]]], sheet_name: str,
             path: Path) -> tuple[list[str], list[list[object]]]:
    """A header row and rows of the same width, from the sparse cells.

    The first row holding anything is the header, which is a rule rather than
    a reading: a sheet with a title above its headings gets the title, and
    that is visible immediately and fixable by naming the sheet's real
    heading row, where guessing which row looked most like headings would be
    wrong occasionally and silently.

    A value to the right of the last heading is refused. There is no name for
    it, so nothing could ever select it, and a stray note in column J would
    otherwise be dropped without a word.
    """
    if not rows:
        return [], []

    (_, heading), *body = rows
    width = max(heading) + 1
    headers = [str(heading.get(at) or "").strip() for at in range(width)]

    where = f"sheet '{sheet_name}' of '{path}'"
    squared: list[list[object]] = []
    for _, row in body:
        beyond = [at for at in row if at >= width]
        if beyond:
            raise SourceError(
                f"{where} has a value in column {_letters(min(beyond))}, which "
                f"is past the last heading in column {_letters(width - 1)}. "
                f"Give it a heading, or take it out; it cannot be served "
                f"without a name."
            )
        squared.append([row.get(at) for at in range(width)])
    return headers, squared


def _within(rows: list[tuple[int, dict[int, object]]],
            table: _ListObject) -> list[list[object]]:
    """The rows of one table, squared to the columns its range covers.

    Only the range's own cells. A note typed beside a table is not in it,
    which is most of the reason a table is worth serving apart from the sheet
    it sits on.

    A record with nothing in it is not a record, the same rule the sheet
    reading uses: Excel will keep a blank row inside a range, and a row of
    nulls is not something anybody put there.
    """
    first = table.first_row + table.header_rows
    last = table.last_row - table.totals_rows
    width = table.last_column - table.first_column + 1

    body: list[list[object]] = []
    for number, row in rows:
        if not first <= number <= last:
            continue
        record = [row.get(table.first_column + at) for at in range(width)]
        if any(value is not None for value in record):
            body.append(record)
    return body


def _letters(index: int) -> str:
    """A column index as the letters a spreadsheet shows for it."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _serving_the_sheet(name: str, sheets_wanted: dict[str, str] | None,
                       tables_wanted: dict[str, str] | None,
                       objects: list[_ListObject]) -> bool:
    """Whether the tab itself is served, beside whatever tables are on it.

    Named, it is served whatever is on it: somebody who asked for a sheet by
    name wants the sheet. Unnamed, it is served unless the file says its own
    reading would be wrong, which _instead_of_the_sheet finds in four ways.
    """
    if sheets_wanted is not None:
        return name.lower() in sheets_wanted
    if tables_wanted is not None:
        return False
    return _instead_of_the_sheet(name, objects) is None


def _tables_named(objects: list[_ListObject], verb: str) -> str:
    """The tables on a sheet, for a sentence about what is served in its place."""
    named = ", ".join(f"'{one.name}'" for one in objects)
    word = "table" if len(objects) == 1 else "tables"
    return f"{word} {named} {verb}"


def _instead_of_the_sheet(name: str,
                          objects: list[_ListObject]) -> str | None:
    """What a tab is served as in place of itself, or None where it is served.

    Each reason is something the file states rather than a judgement about
    how the tab looks, and each turned up in a workbook Excel wrote. The text
    goes to the log, so it says what was served and why.

    A table named after its tab would give two tables one name, which the
    catalog refuses. Naming the table after the sheet is what people do, so
    this cannot be an error; the table wins, because it is the one that knows
    its own columns and where it stops.

    More than one table means more than one header row, and no reading of the
    tab as one table is right: the second table's headings would arrive as a
    record and its numbers would drag the column over to text.

    A totals row is inside its table's range, so the tab read whole serves it
    as a record called Total: a count over the tab is one too many, and a sum
    over a totalled column counts every value twice.

    A table with no header row leaves the tab nothing to name its columns
    with, so the tab read whole takes its first record for the headings.
    """
    if any(one.name.lower() == name.lower() for one in objects):
        return "the table of the same name on it, rather than as the tab"
    if len(objects) > 1:
        named = ", ".join(f"'{one.name}'" for one in objects)
        return (f"its {len(objects)} tables ({named}), because a sheet "
                f"holding more than one has no single row of headings")
    for one in objects:
        if one.totals_rows:
            return (f"its table '{one.name}', because the table ends in a "
                    f"totals row that the tab read whole would serve as a "
                    f"record")
        if not one.header_rows:
            return (f"its table '{one.name}', because the table has no row "
                    f"of headings and the tab read whole would take its "
                    f"first record for one")
    return None


def _serving_the_tables(objects: list[_ListObject],
                        sheets_wanted: dict[str, str] | None,
                        tables_wanted: dict[str, str] | None) -> list[_ListObject]:
    """Which tables on a sheet are served.

    Naming sheets and nothing else serves no tables, the same way naming
    tables and nothing else serves no sheets. Either filter means "this is
    what I want", and a filter that quietly brought something else along
    would be no filter at all.
    """
    if tables_wanted is not None:
        return [one for one in objects if one.name.lower() in tables_wanted]
    if sheets_wanted is not None:
        return []
    return objects


def sheets(path: str | Path, *, only: str | list[str] | None = None,
           table: str | list[str] | None = None) -> list[Sheet]:
    """Every table in a workbook: one per sheet, and one per table on a sheet.

    Both, because both are things a person made. A sheet is what the tabs
    along the bottom show, and a table is a named range somebody drew on one,
    which is the only one of the two that carries a name of its own and knows
    where it stops.

    "only" names sheets and "table" names tables, either as one name or as a
    list of them. Given neither, the whole workbook is served. Given either,
    only what is named is: naming tables alone serves no sheets, which is how
    to say "the tables, not the tabs they sit on".

    A sheet with nothing on it produces nothing: there are no headings, so
    there is no table to describe. Asked for by name it is an error instead,
    because somebody who named it expected rows from it.
    """
    path = Path(path)
    archive = _opened(path)
    try:
        index = _workbook_part(archive, path)
        strings = _shared_strings(archive, path)
        dated = _dated_styles(archive, path)
        since_1904 = _counts_from_1904(index)

        tabs = _sheet_paths(index, archive, path)
        sheets_wanted = wanted_names(only, "sheet")
        tables_wanted = wanted_names(table, "table")

        # Checked after the filters rather than before, because the advice in
        # it has to be true: the check used to run over the whole workbook, so
        # a document of 300 sheets refused with a message saying to name the
        # one wanted, and naming it refused in exactly the same way.
        if (sheets_wanted is None and tables_wanted is None
                and len(tabs) > MAX_SHEETS):
            raise SourceError(
                f"'{path}' has {len(tabs)} sheets, and this serves up to "
                f'{MAX_SHEETS}; name the ones you want with "sheet"'
            )

        on_each = {name: _list_objects(archive, part, name, path)
                   for name, part in tabs}
        carried = sum(len(objects) for objects in on_each.values())
        if tables_wanted is None and carried > MAX_TABLES:
            raise SourceError(
                f"'{path}' has {carried} tables on its sheets, and this serves "
                f'up to {MAX_TABLES}; name the ones you want with "table"'
            )

        if sheets_wanted is not None:
            missing_names(sheets_wanted, [name for name, _ in tabs],
                          "sheet", f"'{path}'")
        if tables_wanted is not None:
            missing_names(tables_wanted,
                          [one.name for objects in on_each.values()
                           for one in objects], "table", f"'{path}'")

        found: list[Sheet] = []
        left_out: list[str] = []
        for name, part in tabs:
            objects = on_each[name]
            whole = _serving_the_sheet(name, sheets_wanted, tables_wanted,
                                       objects)
            wanted = _serving_the_tables(objects, sheets_wanted, tables_wanted)
            if not whole and not wanted:
                continue
            if (not whole and objects and sheets_wanted is None
                    and tables_wanted is None):
                # Said once, at load, because the alternative is a client
                # asking for a tab by name and being told there is no such
                # object, with nothing anywhere saying why.
                log.info("sheet '%s' of '%s' is served as %s", name, path,
                         _instead_of_the_sheet(name, objects))

            rows = _rows_of(archive, part, name, strings, dated, since_1904,
                            path)
            if whole:
                served, unreadable = _the_whole_sheet(
                    rows, name, path, named=sheets_wanted is not None)
                found.extend(served)
                if unreadable is not None:
                    # The reader's own message first, because it is the part
                    # that says what to change on the tab; somebody looking
                    # for one that is not there needs that rather than the
                    # bare fact that it is gone. It already names the tab and
                    # the file, so this does not again.
                    left_out.append(str(unreadable))
                    log.warning("%s %s", unreadable, _in_its_place(objects))
            for one in wanted:
                found.append(Sheet(name=one.name, headers=one.headers,
                                   rows=_within(rows, one),
                                   kind=FROM_A_TABLE, sheet=name))

        # One tab left out is a warning, and the rest is served. Every tab
        # left out makes the warnings the whole answer, and saying instead
        # that the workbook had nothing on it would be untrue.
        if not found and left_out:
            raise SourceError(
                f"'{path}' has nothing that can be served. "
                + " ".join(left_out)
            )
        return found
    finally:
        archive.close()


def _in_its_place(objects: list[_ListObject]) -> str:
    """What is served where a tab could not be, to end a warning with."""
    if not objects:
        return "It is left out, and the rest of the workbook is served."
    verb = "is" if len(objects) == 1 else "are"
    return f"Its {_tables_named(objects, verb)} served instead."


def _the_whole_sheet(rows: list[tuple[int, dict[int, object]]], name: str,
                     path: Path, *,
                     named: bool) -> tuple[list[Sheet], SourceError | None]:
    """The tab as one table, or why it cannot be one.

    Where it cannot and nobody named it, the reason comes back rather than
    being raised, and the tab is left out rather than taking the workbook
    down with it. A title above a table is the commonest layout there is,
    and a stray note beside a list is not far behind; before this, one such
    tab refused a whole workbook, including every other sheet in it.

    Only the layout is forgiven: a value past the last heading, a heading
    that cannot be a column name, or more columns than a result can carry.
    A cell that cannot be read at all has been refused already, where the
    rows are read, because a file damaged in one place is not one to trust
    in the others.

    Named explicitly, it raises as it always did. Somebody who asked for that
    sheet is owed the reason it cannot be served, not a line in a log.
    """
    try:
        headers, body = _squared(rows, name, path)
        # The names are checked here as well as where every table is built,
        # because that is too late to spare the rest of the workbook. A table
        # drawn at D1 leaves the tab three columns with no heading over them,
        # which squares cleanly and is refused only when the names are.
        if headers:
            usable_names(name, headers)
    except SourceError as exc:
        if named:
            raise
        return [], exc

    if not headers:
        if named:
            raise SourceError(
                f"sheet '{name}' of '{path}' is empty, so it has no columns "
                f"to serve"
            )
        return [], None
    return [Sheet(name=name, headers=headers, rows=body, kind=FROM_A_SHEET,
                  sheet=name)], None


def _counts_from_1904(root: ElementTree.Element) -> bool:
    """Whether this workbook's day nought is 1904 rather than 1900.

    A workbook saved by Excel for Mac before 2011 counts from 1904, and a
    date read against the wrong epoch is out by four years and a day without
    anything looking wrong.
    """
    for element in root:
        if _local(element.tag) != "workbookPr":
            continue
        setting = element.get("date1904") or element.get("dateCompatibility") or ""
        return setting.lower() in ("1", "true")
    return False
