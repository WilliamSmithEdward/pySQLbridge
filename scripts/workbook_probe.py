"""Have a real Excel write a workbook, then read it back through the reader.

A test aid, run by hand on Windows with Excel installed, and never shipped:
COM belongs here and in nothing under src/. tests/workbooks.py writes the
fixtures the suite reads, and this is what says those fixtures still look like
what Excel writes. It prints the parts Excel wrote, so the builder can be
compared against them, and then what the shipped reader makes of the file,
including the reader's own account of any tab it did not serve.

    python scripts/workbook_probe.py
    python scripts/workbook_probe.py --keep excel_wrote.xlsx

A separate Excel process is started with DispatchEx, so an instance somebody
has open is never attached to, and it is quit however this ends.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import re
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from pysqlbridge.source import SourceError, from_excel   # noqa: E402
from pysqlbridge.workbook import sheets                  # noqa: E402

# Excel's own constants, which late-bound COM does not bring with it.
SOURCE_RANGE = 1            # xlSrcRange
HAS_HEADERS = 1             # xlYes
OPEN_XML_WORKBOOK = 51      # xlOpenXMLWorkbook
TOTALS_SUM = 1              # xlTotalsCalculationSum

RELATIONSHIP_ID = ("{http://schemas.openxmlformats.org/officeDocument/2006/"
                   "relationships}id")

ROOMS = (("code", "seats"), ("A1", 30), ("B2", 12))


def _sheet(book, name: str, first: bool = False):
    made = (book.Worksheets(1) if first else
            book.Worksheets.Add(None, book.Worksheets(book.Worksheets.Count)))
    made.Name = name
    return made


def _table(tab, address: str, rows, name: str):
    tab.Range(address).Value = rows
    made = tab.ListObjects.Add(SOURCE_RANGE, tab.Range(address), None,
                               HAS_HEADERS)
    made.Name = name
    return made


def _people(book) -> None:
    """Each kind of cell, with a gap: what the builder's cell shapes are from.

    Dates go in as serials with a number format, because pywin32 cannot pass
    a datetime from before 1970 across COM, and one of these is.
    """
    tab = _sheet(book, "People", first=True)
    tab.Range("A1:F1").Value = (("id", "name", "score", "hired", "active",
                                 "team"),)
    tab.Range("A2:C2").Value = ((1, "ada", 99.5),)
    tab.Range("D2").Value2 = 45306
    tab.Range("E2").Value = True
    tab.Range("F2").Value = "core"
    tab.Range("A4:B4").Value = ((3, "grace"),)
    tab.Range("D4").Value2 = 23803
    tab.Range("D2:D4").NumberFormat = "yyyy-mm-dd"


def _tables(book) -> None:
    """Every table layout the reader decides something about."""
    # A calculated column and a totals row, which are what a person adds.
    rooms = _table(_sheet(book, "Totals"), "A1:B3", ROOMS, "Rooms")
    doubled = rooms.ListColumns.Add()
    doubled.Name = "doubled"
    doubled.DataBodyRange.Formula = "=[@seats]*2"
    rooms.ShowTotals = True
    rooms.ListColumns("seats").TotalsCalculation = TOTALS_SUM

    # The header row switched off after the table was made.
    _table(_sheet(book, "NoHeader"), "A1:B3", ROOMS, "Bare").ShowHeaders = False

    # A title, a gap, and a table that starts at column D.
    tab = _sheet(book, "Offset")
    tab.Range("A1").Value = "Room bookings, Q3"
    _table(tab, "D3:E5", ROOMS, "Report")

    # Two tables side by side.
    tab = _sheet(book, "Two")
    _table(tab, "A1:B3", ROOMS, "Left")
    _table(tab, "D1:E3", (("day", "booked"), ("Mon", 4), ("Tue", 7)), "Right")

    # A table given its tab's name, which Excel allows.
    _table(_sheet(book, "Stock"), "A1:B3", ROOMS, "Stock")


def write(path: pathlib.Path) -> None:
    """Have Excel make the workbook and save it at path."""
    # Imported here so the module still imports where there is no COM.
    import win32com.client

    excel = win32com.client.DispatchEx("Excel.Application")
    excel.Visible = False
    excel.DisplayAlerts = False
    try:
        book = excel.Workbooks.Add()
        _people(book)
        _tables(book)
        book.SaveAs(str(path), OPEN_XML_WORKBOOK)
        book.Close(False)
    finally:
        excel.Quit()


def _without_noise(text: str) -> str:
    """A part with the namespace and revision attributes taken out, which say
    nothing about how it is read."""
    text = re.sub(r'\s+xmlns(:\w+)?="[^"]*"', "", text)
    return re.sub(r'\s+(mc:Ignorable|xr\d*:uid|x14ac:dyDescent)="[^"]*"', "",
                  text)


def _part_of(archive: zipfile.ZipFile, sheet_name: str) -> str:
    """Where a named sheet is kept, found through the workbook's own index."""
    index = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    reference = next(one.get(RELATIONSHIP_ID) for one in index.iter()
                     if one.tag.endswith("}sheet")
                     and one.get("name") == sheet_name)
    relationships = ElementTree.fromstring(
        archive.read("xl/_rels/workbook.xml.rels"))
    target = next(one.get("Target") for one in relationships
                  if one.get("Id") == reference)
    return target[1:] if target.startswith("/") else f"xl/{target}"


def show(path: pathlib.Path) -> None:
    """What Excel wrote, then what the reader serves from it."""
    with zipfile.ZipFile(path) as archive:
        people = _part_of(archive, "People")
        text = archive.read(people).decode("utf-8")
        print(f"{people}, the People sheet:")
        for row in re.findall(r"<row .*?</row>", text)[:4]:
            print(f"  {_without_noise(row)}")
        for part in sorted(name for name in archive.namelist()
                           if re.fullmatch(r"xl/tables/table\d+\.xml", name)):
            text = _without_noise(archive.read(part).decode("utf-8"))
            print(f"\n{part}\n  {text[text.index('<table'):]}")

    print("\nthrough the reader:")
    for one in sheets(path):
        print(f"  {one.kind:<5} {one.name!r:<10} from {one.sheet!r:<10} "
              f"{one.headers}  {one.rows}")

    # Read a second time to build the tables, which would say everything in
    # the log again. Once is enough to explain what was served.
    logging.disable(logging.WARNING)
    try:
        served = from_excel(path)
    except SourceError as exc:
        print(f"\nrefused: {exc}")
    else:
        print(f"\nserved {len(served)} tables: "
              f"{', '.join(one.name for one in served)}")
    finally:
        logging.disable(logging.NOTSET)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", type=pathlib.Path,
                        help="save the workbook here rather than in a "
                             "temporary directory")
    arguments = parser.parse_args()
    path = (arguments.keep or
            pathlib.Path(tempfile.mkdtemp()) / "excel_wrote.xlsx").resolve()
    if path.exists():
        path.unlink()

    # The reader says in its log why a tab was not served, which is half of
    # what this is for. To stdout, so it lands beside the rows it explains.
    logging.basicConfig(level=logging.INFO, format="  log: %(message)s",
                        stream=sys.stdout)
    write(path)
    print(f"Excel wrote {path}\n")
    show(path)


if __name__ == "__main__":
    main()
