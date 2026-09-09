"""Reading an Access database as tables.

The .mdb and .accdb formats are undocumented and page-structured, so the
reading is not done here: pyOpenVBA implements the Jet storage engine in pure
Python, and this maps what it hands back onto the columns this serves.

Pure Python matters for the same reason it did for the workbook. The usual way
to read an Access file is the database engine Microsoft ships for it, reached
through COM or ODBC, and that engine is a separate download which installs in
one bit width and refuses to load into a process of the other. A bridge whose
promise is "point it at a file" cannot begin by asking for a driver, and it
should not stop working because the machine it runs on is Linux. Nothing here
touches COM, and an Access database is served on any machine the rest of this
runs on.

The file is read once into memory and never written. That is stronger than
opening it read-only: there is no handle held, no lock file beside it, and
somebody can have the same database open in Access while it is being served.

A saved query is served alongside the tables. Access calls one a view, people
who use Access build them constantly, and the whole point of a query somebody
saved is that it is the shape they wanted the data in. Only the ones that read
are served: an update or a delete query is a statement rather than a table, and
running it to find out what it returns would change the file.

Types come from the database, because unlike CSV or JSON it has some. Where
this serves the declared type, the declared type is what a client sees: a text
column stays text even where every value in it happens to be digits, which
inference alone would get wrong. Where it does not, the values decide, which
covers Yes/No, currency and the GUID. A saved query has no stored schema at
all, so all of its columns are read off its values.
"""

from __future__ import annotations

from pathlib import Path

from .source import SourceError, Table
from .tds.result import Column, DateTime, Float, Integer, NVarChar

# What to say when pyOpenVBA is not installed. It is a dependency, so this is
# a broken installation rather than a missing option, and the message says so
# instead of suggesting the file is at fault.
NO_READER = (
    "reading an Access database needs pyOpenVBA, which pysqlbridge installs "
    "and which is not importable here: {reason}. Reinstall pysqlbridge, or "
    "install it directly with: pip install pyopenvba"
)

# How Jet numbers the types it stores, from pyOpenVBA's own table
# definitions. Named again here rather than imported, because these are the
# numbers this maps from and a mapping that silently followed a rename would
# be worse than one that has to be updated on purpose.
JET_BOOLEAN = 0x01
JET_BYTE = 0x02
JET_INT = 0x03
JET_LONG = 0x04
JET_MONEY = 0x05
JET_SINGLE = 0x06
JET_DOUBLE = 0x07
JET_DATETIME = 0x08
JET_BINARY = 0x09
JET_TEXT = 0x0A
JET_OLE = 0x0B
JET_MEMO = 0x0C
JET_GUID = 0x0F
JET_NUMERIC = 0x10
JET_COMPLEX = 0x12
JET_BIGINT = 0x13
JET_EXTENDED_DATETIME = 0x14

_INTEGER_WIDTHS = {JET_BYTE: 1, JET_INT: 2, JET_LONG: 4, JET_BIGINT: 8}
_FLOATS = frozenset({JET_SINGLE, JET_DOUBLE})
_MOMENTS = frozenset({JET_DATETIME, JET_EXTENDED_DATETIME})

# The binary types. Nothing here can carry one: an OLE Object holds an
# embedded file and an attachment column holds several, and the honest
# answers are to send them or to say so. Saying so is what this does.
_BINARIES = frozenset({JET_BINARY, JET_OLE, JET_COMPLEX})

# Jet stores text as two bytes a character, and a column's declared length is
# in bytes, so TEXT(50) arrives as 100.
BYTES_PER_CHARACTER = 2

# Past what a sized nvarchar can declare there is no size to declare, so the
# column takes the MAX form. A memo declares no length at all and lands there
# by the same rule.
MAX_NVARCHAR_CHARS = 4000

# Which saved queries are worth serving. A select reads, and a union reads.
# Everything else is a statement: running an update query to find out what it
# returns would change the database, which this does not do.
QUERY_SELECT = 1
QUERY_UNION = 9
READING_QUERIES = frozenset({QUERY_SELECT, QUERY_UNION})

# Ceilings, as everywhere else a source is whatever a configuration pointed at.
MAX_TABLES = 256
MAX_ROWS_PER_TABLE = 5_000_000


def _opened(path: Path):
    """The database, read into memory, or why it could not be."""
    try:
        from pyopenvba.access import AccessDatabase
        from pyopenvba.access_read import AccessError
    except ImportError as exc:
        raise SourceError(NO_READER.format(reason=exc)) from exc

    try:
        return AccessDatabase(path), AccessError
    except AccessError as exc:
        raise SourceError(f"could not read '{path}': {exc}") from exc
    except OSError as exc:
        raise SourceError(f"could not read '{path}': {exc}") from exc


def _column_for(name: str, kind: int, size: int, table: str,
                path: Path) -> Column | None:
    """What this serves a declared type as, or None to read it off the values.

    None is not a failure. Yes/No, currency and the GUID have no type here
    that is theirs, and for those the values are better evidence than the
    declaration: a currency column becomes a float where every value in it
    survives one exactly, and text where one of them would lose digits, which
    is the rule this already applies to a number that arrived as text.
    """
    if kind in _BINARIES:
        raise SourceError(
            f"column '{name}' of table '{table}' in '{path}' holds binary "
            f"data, and there is no column type here that carries one. Serve "
            f'the tables you need by name with "table", or make a query in '
            f"Access that leaves this column out and point at that."
        )
    if kind in _INTEGER_WIDTHS:
        return Column(name, Integer(_INTEGER_WIDTHS[kind]))
    if kind in _FLOATS:
        return Column(name, Float(8))
    if kind in _MOMENTS:
        return Column(name, DateTime())
    if kind == JET_TEXT:
        chars = size // BYTES_PER_CHARACTER
        return Column(name, NVarChar(chars if 0 < chars <= MAX_NVARCHAR_CHARS
                                     else None))
    if kind == JET_MEMO:
        return Column(name, NVarChar(None))
    return None


def _value(value: object, where: str) -> object:
    """One value from the database, as something the rest of this can hold.

    Only two kinds need anything doing to them. A decimal is passed on as the
    digits it was written with, because nothing downstream types one and the
    inference that reads a number out of a CSV then decides whether a float
    holds it exactly. Binary is refused, having no column type here; the
    declared types are checked first, so reaching this means a column that
    did not say binary produced it anyway.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise SourceError(
            f"{where} holds binary data, and there is no column type here "
            f"that carries one"
        )
    if type(value).__name__ == "Decimal":
        return str(value)
    return value


def _typed(name: str, declared: list, rows: list[list[object]],
           path: Path) -> Table:
    """Give each column its declared type, or one read off its values."""
    from .source import infer_column, usable_names

    headers = [column_name for column_name, _ in declared]
    usable_names(name, headers)

    columns: list[Column] = []
    converted: list[list[object]] = []
    for at, (column_name, column) in enumerate(declared):
        where = f"column '{column_name}' of '{name}' in '{path}'"
        values = [_value(row[at], where) for row in rows]
        if column is None:
            column, values = infer_column(column_name, values)
        else:
            values = _as_declared(column, values)
        columns.append(column)
        converted.append(values)

    built = [list(row) for row in zip(*converted)] if converted else []
    return Table(name=name, columns=columns, rows=built)


def _as_declared(column: Column, values: list[object]) -> list[object]:
    """Values as the declared column holds them.

    The reader has already produced the right Python type for each of these,
    so this is a conversion only where a column that says integer holds a
    value written some other way.
    """
    kind = type(column.type)
    if kind is Integer:
        return [None if v is None else int(v) for v in values]
    if kind is Float:
        return [None if v is None else float(v) for v in values]
    if kind is NVarChar:
        return [None if v is None else str(v) for v in values]
    return list(values)


def _table(database, name: str, path: Path) -> Table:
    """One stored table, with the types the database declared for it."""
    stored = database.table(name)
    declared = [
        (column.name,
         _column_for(column.name, column.type_code, column.length, name, path))
        for column in stored.columns
    ]

    order = [column.name for column, _ in zip(stored.columns, declared)]
    rows: list[list[object]] = []
    for row in stored.rows():
        rows.append([row.get(one) for one in order])
        if len(rows) > MAX_ROWS_PER_TABLE:
            raise SourceError(
                f"table '{name}' of '{path}' holds more than "
                f"{MAX_ROWS_PER_TABLE} rows, which is more than this serves"
            )
    return _typed(name, declared, rows, path)


def _query(database, name: str, path: Path, failed) -> Table | None:
    """One saved query, as the rows it answers, or None where it has none.

    Run through pyOpenVBA's own Access SQL, which is the SQL the query was
    written in: an Access query says IIf and Nz and joins its strings with &,
    none of which is what a client sends this. So the query is answered where
    it was written and its rows arrive here already worked out.

    Its columns are read off those rows, because a query has no stored schema
    to read them off instead. A query that answers nothing therefore has no
    columns either, and there is nothing to serve.
    """
    try:
        answered = database.execute(f"SELECT * FROM [{name}]")
    except failed as exc:
        raise SourceError(
            f"could not run the saved query '{name}' of '{path}': {exc}"
        ) from exc

    if not isinstance(answered, list) or not answered:
        return None
    if len(answered) > MAX_ROWS_PER_TABLE:
        raise SourceError(
            f"the saved query '{name}' of '{path}' answered more than "
            f"{MAX_ROWS_PER_TABLE} rows, which is more than this serves"
        )

    headers = list(answered[0])
    declared = [(header, None) for header in headers]
    rows = [[row.get(header) for header in headers] for row in answered]
    return _typed(name, declared, rows, path)


def _reading_queries(database) -> dict[str, object]:
    """The saved queries worth serving, by name."""
    return {
        query.name: query
        for query in database.queries()
        if query.type in READING_QUERIES and query.name
    }


def tables(path: str | Path, *, only: str | None = None) -> list[Table]:
    """Every table and saved query of a database, or the one named."""
    path = Path(path)
    if not path.exists():
        raise SourceError(f"could not read '{path}': there is no such file")

    database, failed = _opened(path)
    try:
        stored = list(database.table_names())
        queries = _reading_queries(database)
    except failed as exc:
        raise SourceError(f"could not read '{path}': {exc}") from exc

    wanted = sorted(set(stored) | set(queries), key=str.lower)
    if len(wanted) > MAX_TABLES:
        raise SourceError(
            f"'{path}' has {len(wanted)} tables, and this serves up to "
            f'{MAX_TABLES}; name the one you want with "table"'
        )

    if only is not None:
        named = [name for name in wanted if name.lower() == only.lower()]
        if not named:
            available = ", ".join(f"'{name}'" for name in wanted)
            raise SourceError(
                f"'{path}' has no table called '{only}'; it has {available}"
            )
        wanted = named

    found: list[Table] = []
    for name in wanted:
        try:
            if name in queries:
                answered = _query(database, name, path, failed)
                if answered is None:
                    if only is not None:
                        raise SourceError(
                            f"the saved query '{name}' of '{path}' answers no "
                            f"rows, and a query has no stored columns to "
                            f"describe it by, so there is nothing to serve"
                        )
                    continue
                found.append(answered)
            else:
                found.append(_table(database, name, path))
        except failed as exc:
            raise SourceError(
                f"could not read '{name}' of '{path}': {exc}"
            ) from exc
    return found
