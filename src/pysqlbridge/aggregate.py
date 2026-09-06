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


def compute(
    table: Table, rows: list[list[object]], items
) -> tuple[list[Column], list[list[object]]]:
    """Reduce the rows to the single row an aggregated select asks for."""
    columns: list[Column] = []
    values: list[object] = []

    for item in items:
        function = item.function

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
