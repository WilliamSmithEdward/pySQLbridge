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

from .predicate import collated, reads_the_row
from .source import SourceError, Table, column_of
from .tds.result import Column, Float, Integer, NVarChar

# COUNT is int in SQL Server, not bigint. COUNT_BIG is the wider one, and
# nothing here needs it for a file or an API page.
COUNT_TYPE = Integer(4)

# See the note above: wide enough that a sum of a file's worth of integers
# cannot overflow the column it is declared in.
SUM_INTEGER_TYPE = Integer(8)


def _values(table: Table, rows: list[list[object]], name: str, function: str,
            item=None):
    """The non-null values an aggregate reduces, and the column they came from.

    A plain column is read by position. Anything else is evaluated per row,
    and its column stands in for a type: an expression has no declared one,
    so what it produced decides, the same way a source's own columns are
    typed.
    """
    if item is not None and item.argument is not None:
        from .predicate import PredicateError
        from .source import infer_column

        names = table.column_names
        produced = []
        for row in rows:
            try:
                produced.append(item.argument.evaluate(dict(zip(names, row)), {}))
            except PredicateError as exc:
                raise SourceError(str(exc)) from exc
        column, converted = infer_column(name, produced)
        present = [value for value in converted if value is not None]
        return column, _once(present) if item.distinct else present

    at = table.index_of(name)
    if at is None:
        raise SourceError(
            f"invalid column name '{name}' in {function}(), "
            f"in table '{table.name}'"
        )
    column = table.columns[at]
    present = [row[at] for row in rows if row[at] is not None]
    if item is not None and item.distinct:
        present = _once(present)
    return column, present


def _once(values: list) -> list:
    """Each distinct value once, in the order it first appeared.

    Distinct under the declared collation, which is case-insensitive, so red
    and RED are one value.
    """
    from .predicate import collated

    seen = set()
    kept = []
    for value in values:
        key = collated(value)
        if key in seen:
            continue
        seen.add(key)
        kept.append(value)
    return kept


def _numeric(column: Column, function: str) -> None:
    if isinstance(column.type, NVarChar):
        raise SourceError(
            f"{function}() needs a numeric column, and '{column.name}' is text"
        )


def group(
    table: Table, rows: list[list[object]], items, keys: list[str],
    parameters=None,
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
        one, values = compute(table, members, items, group_row=members[0],
                              parameters=parameters)
        columns = one
        out.append(values[0])
    if not columns:
        # No rows at all still has to declare the shape it would have had.
        columns, _ = compute(table, [], items, group_row=None,
                             parameters=parameters)
    return columns, out


def _position(table: Table, name: str) -> int:
    at = table.index_of(name)
    if at is None:
        raise SourceError(
            f"cannot group by '{name}': the table has no such column"
        )
    return at


def compute(
    table: Table, rows: list[list[object]], items, group_row=None,
    parameters=None,
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
            if item.node is not None and not reads_the_row(item.node):
                # A value the rows do not decide: a literal, or a subquery
                # that was answered before this ran. Worked out once and
                # repeated, because every group carries the same one.
                value = item.node.evaluate({}, parameters or {})
                column, converted = column_of(item.output_name, [value])
                columns.append(column)
                values.append(converted[0])
                continue
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
                _, present = _values(table, rows, item.expression, function, item)
                count = len(present)       # COUNT(col) counts non-nulls
            columns.append(Column(item.output_name, COUNT_TYPE))
            values.append(count)
            continue

        if item.expression is None:
            raise SourceError(f"{function}() needs a column")

        column, present = _values(table, rows, item.expression, function, item)

        if function in ("MIN", "MAX"):
            # Ordered under the declared collation, which is case-insensitive:
            # a real server answers MAX over ada, Grace, barbara with Grace,
            # where comparing by code point answers barbara. The value handed
            # back is the original, not the folded one used to compare.
            result_type = column.type
            chosen = min if function == "MIN" else max
            result = None if not present else chosen(present, key=collated)

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
