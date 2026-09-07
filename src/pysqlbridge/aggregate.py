"""Whole-table aggregates.

COUNT, SUM, MIN, MAX and AVG over every row that survived the WHERE. There is
no GROUP BY, so an aggregated query always returns exactly one row, and the
parser refuses a select list that mixes an aggregate with a bare column rather
than guessing which grouping was meant.

Two type decisions, both deliberate and neither invisible.

SUM over an integer column returns a 64-bit integer rather than the column's
own width. SQL Server keeps the width and raises an arithmetic overflow, which
here would surface as an encoding failure partway through writing a result set
rather than as a SQL error. Widening cannot produce a wrong answer for any
source this project reads.

AVG over an integer column returns an integer, truncated, which is what SQL
Server does and surprises people every time. It is matched rather than
improved, because a client that computes against a real server and against this
one should get the same number.
"""

from __future__ import annotations

from .source import SourceError, Table
from .tds.result import Column, Float, Integer, NVarChar

# COUNT is int in SQL Server, not bigint. COUNT_BIG is the wider one, and
# nothing here needs it for a file or an API page.
COUNT_TYPE = Integer(4)

# See the note above: wide enough that a sum of a file's worth of integers
# cannot overflow the column it is declared in.
SUM_INTEGER_TYPE = Integer(8)


def _values(table: Table, rows: list[list[object]], name: str, function: str):
    """The non-null values of one column, and the column itself."""
    lookup = {c.name.lower(): i for i, c in enumerate(table.columns)}
    position = lookup.get(name.lower())
    if position is None:
        raise SourceError(
            f"invalid column name '{name}' in {function}(), "
            f"in table '{table.name}'"
        )
    column = table.columns[position]
    return column, [row[position] for row in rows if row[position] is not None]


def _numeric(column: Column, function: str) -> None:
    if isinstance(column.type, NVarChar):
        raise SourceError(
            f"{function}() needs a numeric column, and '{column.name}' is text"
        )


def group(
    table: Table, rows: list[list[object]], items, keys: list[str]
) -> tuple[list[Column], list[list[object]]]:
    """One row per distinct combination of the grouped columns.

    The partitions keep the order their first row appeared in, which is what
    a client sees when it asks for groups without an ORDER BY. Grouping is
    case-insensitive on text, because the collation this server declares is.
    """
    from .predicate import collated

    positions = [_position(table, name) for name in keys]
    partitions: dict[tuple, list[list[object]]] = {}
    for row in rows:
        signature = tuple(collated(row[at]) for at in positions)
        partitions.setdefault(signature, []).append(row)

    columns: list[Column] = []
    out: list[list[object]] = []
    for members in partitions.values():
        one, values = compute(table, members, items, group_row=members[0])
        columns = one
        out.append(values[0])
    if not columns:
        # No rows at all still has to declare the shape it would have had.
        columns, _ = compute(table, [], items, group_row=None)
    return columns, out


def _position(table: Table, name: str) -> int:
    at = table.index_of(name)
    if at is None:
        raise SourceError(
            f"cannot group by '{name}': the table has no such column"
        )
    return at


def compute(
    table: Table, rows: list[list[object]], items, group_row=None
) -> tuple[list[Column], list[list[object]]]:
    """Reduce the rows to the single row an aggregated select asks for.

    With a group_row, the non-aggregated entries in the select list are read
    from it: they are the columns the grouping was done on, so every row in
    the partition carries the same value and the first will do.
    """
    columns: list[Column] = []
    values: list[object] = []

    for item in items:
        function = item.function

        if function is None:
            # A grouped column. Its value is the one the whole partition
            # shares, and its type is whatever the table declared.
            at = _position(table, item.expression)
            columns.append(Column(item.output_name, table.columns[at].type))
            values.append(group_row[at] if group_row is not None else None)
            continue

        if function == "COUNT":
            if item.expression is None:
                count = len(rows)          # COUNT(*) counts rows
            else:
                _, present = _values(table, rows, item.expression, function)
                count = len(present)       # COUNT(col) counts non-nulls
            columns.append(Column(item.output_name, COUNT_TYPE))
            values.append(count)
            continue

        if item.expression is None:
            raise SourceError(f"{function}() needs a column")

        column, present = _values(table, rows, item.expression, function)

        if function in ("MIN", "MAX"):
            result_type = column.type
            result = None if not present else (
                min(present) if function == "MIN" else max(present)
            )

        elif function == "SUM":
            _numeric(column, function)
            result_type = (
                SUM_INTEGER_TYPE if isinstance(column.type, Integer) else column.type
            )
            result = None if not present else sum(present)

        elif function == "AVG":
            _numeric(column, function)
            result_type = column.type
            if not present:
                result = None
            elif isinstance(column.type, Integer):
                # Truncated, as SQL Server does it.
                result = int(sum(present) / len(present))
            else:
                result = sum(present) / len(present)

        else:
            raise SourceError(f"'{function}' is not an aggregate this server knows")

        columns.append(Column(item.output_name, result_type))
        values.append(result)

    return columns, [values]
