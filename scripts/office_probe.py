"""Say what this makes of a workbook or an Access database.

For a file that will not serve, or serves something unexpected. It reads with
the shipped readers and prints what came out: the tables, their columns with
the type each was given, and the first few rows.

    python scripts/office_probe.py budget.xlsx club.accdb
    python scripts/office_probe.py --rows 20 budget.xlsx

Nothing here needs Excel or Access installed, or Windows. That is the whole
point of both readers: an .xlsx is a zip of XML the standard library opens,
and an .accdb is read by pyOpenVBA in pure Python. If this prints what you
expect and a client does not see it, the problem is past the reader.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from pysqlbridge.source import SourceError, from_access, from_excel   # noqa: E402

WORKBOOKS = frozenset({".xlsx", ".xlsm", ".xltx", ".xltm"})
DATABASES = frozenset({".accdb", ".mdb", ".accde", ".mde"})

# How much of a value to show. A memo column runs to megabytes and a terminal
# does not.
WIDEST_VALUE = 40


def read(path: pathlib.Path):
    """The tables in a file, chosen by what the file is."""
    suffix = path.suffix.lower()
    if suffix in WORKBOOKS:
        return "workbook", from_excel(path)
    if suffix in DATABASES:
        return "database", from_access(path)
    raise SourceError(
        f"'{path}' is not a workbook or an Access database; this reads "
        f"{', '.join(sorted(WORKBOOKS | DATABASES))}"
    )


def shown(value: object) -> str:
    if value is None:
        return "NULL"
    text = str(value)
    return text if len(text) <= WIDEST_VALUE else text[:WIDEST_VALUE - 3] + "..."


def declared(kind: object) -> str:
    """A column type as a client would see it written."""
    name = type(kind).__name__
    if hasattr(kind, "max_chars"):
        return f"{name}(max)" if kind.max_chars is None else f"{name}({kind.max_chars})"
    if hasattr(kind, "width"):
        return f"{name}({kind.width})"
    return name


def describe(table, rows: int) -> None:
    print(f"  {table.name}  ({len(table.rows)} rows, "
          f"{len(table.columns)} columns)")
    for column in table.columns:
        print(f"      {column.name:<28} {declared(column.type)}")
    for row in table.rows[:rows]:
        print("      | " + " | ".join(shown(value) for value in row))
    if len(table.rows) > rows:
        print(f"      ... {len(table.rows) - rows} more rows")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", type=pathlib.Path)
    parser.add_argument("--rows", type=int, default=5,
                        help="how many rows of each table to show (default 5)")
    given = parser.parse_args(argv)

    failed = False
    for path in given.files:
        print(f"=== {path} ===")
        try:
            kind, tables = read(path)
        except SourceError as exc:
            print(f"  refused: {exc}\n")
            failed = True
            continue
        print(f"  read as a {kind}, {len(tables)} table(s)\n")
        for table in tables:
            describe(table, given.rows)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
