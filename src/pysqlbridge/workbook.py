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
import re
import xml.etree.ElementTree as ElementTree
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .source import SourceError

# Where the package keeps the parts this reads. The workbook is the index; the
# relationships turn its r:id references into paths.
WORKBOOK_PART = "xl/workbook.xml"
RELATIONSHIP_PART = "xl/_rels/workbook.xml.rels"
SHARED_STRINGS_PART = "xl/sharedStrings.xml"
STYLES_PART = "xl/styles.xml"

# Relationship types, matched on the last segment so that both the strict and
# the transitional namespaces resolve.
WORKSHEET_RELATIONSHIP = "worksheet"

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
    """One sheet, as a header row and the rows under it."""

    name: str
    headers: list[str]
    rows: list[list[object]]


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
    if len(found) > MAX_SHEETS:
        raise SourceError(
            f"'{path}' has {len(found)} sheets, and this serves up to "
            f'{MAX_SHEETS}; name the one you want with "sheet"'
        )
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
             path: Path) -> list[dict[int, object]]:
    """Every row of a sheet that holds anything, by column index.

    Sparse both ways: a row that holds nothing is not in the list, and a cell
    that holds nothing is not in its row. Turning that into rectangular rows
    needs the header first, so it is done a step later.

    In the order the rows say they are in rather than the order they were
    written. Excel writes them in order and the two are the same, but the
    number is what a sheet means: a file whose rows ran 3, 1, 2 took the
    third row as the heading and served a column called '3' holding the rest,
    which is the wrong answer with nothing to say so. Only sorted where they
    actually arrive out of order, so the ordinary file pays one comparison a
    row and nothing else.
    """
    where = f"sheet '{sheet_name}' of '{path}'"
    found: list[dict[int, object]] = []
    numbers: list[int] = []
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
                    found.append(row)
                    numbers.append(number)
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
        found = [row for _, row in sorted(zip(numbers, found),
                                          key=lambda pair: pair[0])]
    return found


def _row_number(written: str | None, last: int) -> int:
    """What row this is, or the one after the last where it does not say."""
    if written is None:
        return last + 1
    try:
        return int(written)
    except ValueError:
        return last + 1


def _squared(rows: list[dict[int, object]], sheet_name: str,
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

    heading, *body = rows
    width = max(heading) + 1
    headers = [str(heading.get(at) or "").strip() for at in range(width)]

    where = f"sheet '{sheet_name}' of '{path}'"
    squared: list[list[object]] = []
    for row in body:
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


def _letters(index: int) -> str:
    """A column index as the letters a spreadsheet shows for it."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def sheets(path: str | Path, *, only: str | None = None) -> list[Sheet]:
    """Every sheet of a workbook, or the one named.

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

        wanted = _sheet_paths(index, archive, path)
        if only is not None:
            named = [pair for pair in wanted if pair[0].lower() == only.lower()]
            if not named:
                available = ", ".join(f"'{name}'" for name, _ in wanted)
                raise SourceError(
                    f"'{path}' has no sheet called '{only}'; it has {available}"
                )
            wanted = named

        found: list[Sheet] = []
        for name, part in wanted:
            rows = _rows_of(archive, part, name, strings, dated, since_1904, path)
            headers, body = _squared(rows, name, path)
            if not headers:
                if only is not None:
                    raise SourceError(
                        f"sheet '{name}' of '{path}' is empty, so it has no "
                        f"columns to serve"
                    )
                continue
            found.append(Sheet(name=name, headers=headers, rows=body))
        return found
    finally:
        archive.close()


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
