"""Turning files into tables a client can select from.

A source supplies a name, some columns and some rows. What it does not supply
is a schema, because CSV has none and JSON's is per-value, so the column types
are inferred by reading the data.

Inference is deliberately narrow: integer, float, or text, plus NULL. Those are
the types the result encoder can put on the wire today, and widening the
inference before widening the encoder would only produce columns that cannot be
sent. A column is integer only if every non-null value in it is an integer, and
one stray value drops the whole column to the next type out. That is stricter
than guessing per row, and it has to be, because the column type is declared
once in COLMETADATA and every row is then encoded against it.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from .tds.result import Column, ColumnType, Float, Integer, NVarChar

# TDS nvarchar tops out here before it needs the MAX form, which is a different
# encoding this project does not implement yet.
MAX_NVARCHAR_CHARS = 4000

# Values a CSV uses to mean "nothing". A file that means the literal text
# "NULL" is out of luck, which is why this is a short list rather than a
# generous one.
CSV_NULLS = frozenset({""})

# Nested keys are joined with this, so {"address": {"city": x}} becomes
# address.city. A client can select it as [address.city].
FLATTEN_SEPARATOR = "."

# Deep enough for every API surveyed; below it the remaining subtree is kept as
# its JSON text rather than exploding into columns nobody asked for.
MAX_FLATTEN_DEPTH = 6

# A flattened GitHub repository object runs to about a hundred columns. Past
# this a table is not useful and the config wants a projection, so failing here
# is more helpful than serving it.
MAX_COLUMNS = 250


class SourceError(Exception):
    """A source could not be read, or could not be turned into a table."""


@dataclass(frozen=True)
class Table:
    """One table a client can select from."""

    name: str
    columns: list[Column]
    rows: list[list[object]]

    @property
    def column_names(self) -> list[str]:
        return [column.name for column in self.columns]

    def select(self, names: list[str] | None = None) -> tuple[list[Column], list[list[object]]]:
        """Project the table onto the named columns, or all of them.

        Raises SourceError naming the column, because that message reaches the
        user as a SQL error and "invalid column name" without the name is not
        worth sending.
        """
        if names is None:
            return list(self.columns), [list(row) for row in self.rows]

        lookup = {column.name.lower(): index for index, column in enumerate(self.columns)}
        indexes = []
        for name in names:
            if name.lower() not in lookup:
                raise SourceError(f"invalid column name '{name}' in table '{self.name}'")
            indexes.append(lookup[name.lower()])

        return (
            [self.columns[i] for i in indexes],
            [[row[i] for i in indexes] for row in self.rows],
        )


def _looks_like_integer(value: str) -> bool:
    text = value.strip()
    if text.startswith(("-", "+")):
        text = text[1:]
    return text.isdigit() and text != ""


def _looks_like_float(value: str) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def infer_column(name: str, values: list[object]) -> tuple[Column, list[object]]:
    """Choose a type for a column and convert its values to match.

    Returns the column and the converted values together, because a type that
    nothing converts to is useless and the two decisions are one decision.
    """
    present = [v for v in values if v is not None]

    if present and all(isinstance(v, bool) for v in present):
        # bool is a subclass of int in Python, so it would otherwise silently
        # become an integer column. Text keeps True and False readable.
        return (
            Column(name, NVarChar(5)),
            [None if v is None else str(v) for v in values],
        )

    if not present:
        # Nothing to go on. Text accepts anything a later row might hold.
        return Column(name, NVarChar(1)), list(values)

    strings = [str(v) for v in present]

    if all(_looks_like_integer(s) for s in strings):
        converted = [None if v is None else int(str(v)) for v in values]
        widest = max(abs(v) for v in converted if v is not None)
        width = 4 if widest < 2**31 else 8
        return Column(name, Integer(width)), converted

    if all(_looks_like_float(s) for s in strings):
        return (
            Column(name, Float(8)),
            [None if v is None else float(str(v)) for v in values],
        )

    longest = max(len(s) for s in strings)
    if longest > MAX_NVARCHAR_CHARS:
        raise SourceError(
            f"column '{name}' holds a value of {longest} characters, beyond the "
            f"{MAX_NVARCHAR_CHARS} an nvarchar column can declare"
        )
    return (
        Column(name, NVarChar(max(longest, 1))),
        [None if v is None else str(v) for v in values],
    )


def _build(name: str, headers: list[str], records: list[list[object]]) -> Table:
    if not headers:
        raise SourceError(f"source '{name}' has no columns")

    columns: list[Column] = []
    converted: list[list[object]] = []
    for index, header in enumerate(headers):
        column, values = infer_column(header, [record[index] for record in records])
        columns.append(column)
        converted.append(values)

    # Transpose back: inference works down columns, the wire wants rows.
    rows = [list(row) for row in zip(*converted)] if converted else []
    return Table(name=name, columns=columns, rows=rows)


def from_csv(path: str | Path, *, name: str | None = None, encoding: str = "utf-8-sig") -> Table:
    """Read a CSV whose first line is its header.

    The default encoding tolerates a byte order mark, which Excel writes and
    which would otherwise become part of the first column's name.
    """
    path = Path(path)
    table_name = name or path.stem
    try:
        with path.open("r", encoding=encoding, newline="") as handle:
            reader = csv.reader(handle)
            try:
                headers = next(reader)
            except StopIteration:
                raise SourceError(f"'{path}' is empty, so it has no header row") from None
            records = [
                [None if cell in CSV_NULLS else cell for cell in row]
                for row in reader
                if row
            ]
    except OSError as exc:
        raise SourceError(f"could not read '{path}': {exc}") from exc

    width = len(headers)
    for number, record in enumerate(records, start=2):
        if len(record) != width:
            raise SourceError(
                f"'{path}' line {number} has {len(record)} fields but the header "
                f"declares {width}"
            )
    return _build(table_name, headers, records)


def from_json(path: str | Path, *, name: str | None = None) -> Table:
    """Read a JSON array of objects.

    Decoded as utf-8-sig, like the CSV reader, so a byte order mark is tolerated
    rather than rejected. Windows editors and PowerShell both write one by
    default, and json.loads refuses it outright.

    Keys are unioned across every record and ordered by first appearance, so a
    record missing a key contributes a NULL rather than shifting the row. An
    array of anything but objects is refused: there would be no column names.
    """
    path = Path(path)
    table_name = name or path.stem
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise SourceError(f"could not read '{path}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SourceError(f"'{path}' is not valid JSON: {exc}") from exc

    return from_records(payload, name=table_name, origin=str(path))


def from_records(
    payload: object,
    *,
    name: str,
    origin: str = "the supplied data",
    flatten: bool = True,
    columns: list[str] | None = None,
) -> Table:
    """Build a table from a list of dictionaries already in memory.

    Split out from from_json because an HTTP source hands over parsed records
    rather than a file, and the shaping rules are the same either way.

    Records are flattened by default, because most APIs nest. columns projects
    the result, which a wide record needs: a flattened GitHub repository object
    has about a hundred columns.
    """
    if not isinstance(payload, list):
        raise SourceError(f"{origin} is not a JSON array, so it is not a table")
    if not payload:
        raise SourceError(f"{origin} is an empty array, so it has no columns")

    shaped: list[dict] = []
    for record in payload:
        if not isinstance(record, dict):
            raise SourceError(
                f"{origin} contains a {type(record).__name__} where a JSON "
                f"object was needed, so its columns cannot be named"
            )
        shaped.append(flatten_record(record) if flatten else record)

    headers: list[str] = []
    for record in shaped:
        for key in record:
            if key not in headers:
                headers.append(key)

    if columns:
        wanted = {c.lower(): c for c in columns}
        missing = [
            c for c in columns if c.lower() not in {h.lower() for h in headers}
        ]
        if missing:
            available = ", ".join(headers[:12])
            raise SourceError(
                f"{origin} has no column {missing[0]!r}; it offers: {available}"
                + (" ..." if len(headers) > 12 else "")
            )
        headers = [h for h in headers if h.lower() in wanted]

    if len(headers) > MAX_COLUMNS:
        raise SourceError(
            f"{origin} flattens to {len(headers)} columns, past the "
            f"{MAX_COLUMNS} this serves; name the ones you want with "
            f'"columns" in the configuration'
        )

    records = [
        [
            record.get(header) if flatten
            else _scalar(record.get(header), header, origin)
            for header in headers
        ]
        for record in shaped
    ]
    return _build(name, headers, records)


def flatten_record(
    record: dict,
    *,
    separator: str = FLATTEN_SEPARATOR,
    max_depth: int = MAX_FLATTEN_DEPTH,
) -> dict:
    """Turn one nested record into a flat mapping of column name to scalar.

    Nested objects become dotted names. Arrays become their JSON text, which is
    lossless and keeps the column a scalar: an array of scalars has no column
    type, and an array of objects is really a second table, not a value. A
    subtree deeper than max_depth becomes JSON text for the same reason.

    Surveyed against nine public APIs; five of them need this to be servable at
    all. See docs/api-shapes.md.
    """
    flat: dict[str, object] = {}

    def walk(value: object, prefix: str, depth: int) -> None:
        if isinstance(value, dict) and depth < max_depth:
            if not value:
                flat[prefix] = None      # an empty object has no columns
                return
            for key, inner in value.items():
                name = f"{prefix}{separator}{key}" if prefix else str(key)
                walk(inner, name, depth + 1)
            return
        if isinstance(value, (dict, list)):
            flat[prefix] = json.dumps(value, ensure_ascii=False)
            return
        flat[prefix] = value

    walk(record, "", 0)
    return flat


def _scalar(value: object, header: str, origin: str) -> object:
    """Refuse what a column cannot hold.

    Only reached when flattening is off. A nested value has no scalar type to
    declare, and quietly stringifying it would produce a column of JSON
    fragments that looks like data and is not queryable.
    """
    if isinstance(value, (dict, list)):
        raise SourceError(
            f"{origin} has a nested {type(value).__name__} under '{header}', "
            f"which has no column type; serve it with flattening on, or select "
            f"a path that reaches the rows directly"
        )
    return value
