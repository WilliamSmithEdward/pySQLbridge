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

from .predicate import (
    COUNTS,
    NOT_GROUPED_OR_AGGREGATED,
    ONLY_IN_SELECT_OR_ORDER_BY,
    converted,
    PredicateError,
    aggregates_in,
    collated,
    grouping_expressions,
    parse_expression,
    reads_the_row,
    result_kind,
    ungrouped,
)
from .source import DECLARED_FOR, SourceError, Table, column_of, holdings
from .tds.result import Column, Float, Integer, NVarChar

# COUNT is int in SQL Server, not bigint. COUNT_BIG is the wider one, which
# nothing here needs for a file or an API page and which a client written
# against a real table asks for anyway.
COUNT_TYPE = Integer(4)
WIDE_COUNT_TYPE = Integer(8)

# How far the values are spread, always float however they were declared.
# STDEV and VAR are over a sample and divide by one fewer than there are;
# STDEVP and VARP are over the whole population. One value gives a sample
# nothing to divide by, and SQL Server answers NULL rather than failing.
#
# Worked out from how many there are, what they add up to and what their
# squares add up to, rather than from their mean. That is the shakier of the
# two formulas where the values are large and close together, and it is the
# one a real server uses: measured, STDEV over 1 and -1e16 is
# 7071067811865475 there, which is what the sums give, and 7071067811865476
# from subtracting the mean from each value. Reading the values once is also
# what lets a window keep the answer up to date as rows arrive.
def _variance(count: int, total: float, squares: float, sample: bool):
    """The variance of values with these sums, or None where there is none."""
    if sample and count < 2:
        return None
    spread = squares - total * total / count
    # Cancellation can take it a hair below zero where every value is the
    # same; there is no negative spread and no square root of one.
    return max(spread, 0.0) / (count - 1 if sample else count)


def _deviation(count: int, total: float, squares: float, sample: bool):
    found = _variance(count, total, squares, sample)
    return None if found is None else found ** 0.5


SPREAD = {
    "VARP": lambda count, total, squares: _variance(count, total, squares, False),
    "VAR": lambda count, total, squares: _variance(count, total, squares, True),
    "STDEVP": lambda count, total, squares: _deviation(count, total, squares, False),
    "STDEV": lambda count, total, squares: _deviation(count, total, squares, True),
}


def sums_of(values) -> tuple:
    """How many there are, their total, and the total of their squares."""
    count = total = squares = 0
    for value in values:
        count += 1
        total = total + value
        squares = squares + value * value
    return count, total, squares

# See the note above: wide enough that a sum of a file's worth of integers
# cannot overflow the column it is declared in.
SUM_INTEGER_TYPE = Integer(8)


def totalled(values) -> object:
    """The values added one after another, in the order they came.

    Not sum(), which since 3.12 keeps a running correction and answers what
    the arithmetic would have given with no rounding at all. That is the
    better number and it is not the one a real server gives: measured,
    SUM over 1e16, 1, 1 and -1e16 is 0 on SQL Server and 2 from sum(),
    because a float at 1e16 has a gap of 2 either side of it and the two
    ones fall into it. A bridge that answers 2 where the server it stands in
    for answers 0 has changed somebody's report.

    Ordinary data never notices. Values far enough apart do, and a file has
    whatever is in it.
    """
    total = 0
    for value in values:
        total = total + value
    return total


def _run_together(table: Table, rows: list, item, parameters):
    """STRING_AGG: a group's values with something written between them.

    A NULL is left out entirely, so the separator around it goes too, and a
    group holding nothing but NULLs is NULL rather than the empty string. An
    empty string is a value and stays. Measured, all three.

    WITHIN GROUP says what order to run them in; without it they keep the
    order they arrived in, which is what a real server does with nothing
    else to go on.
    """
    ordered = _in_told_order(table, rows, item.within, parameters)
    column, present = _values(table, ordered, item.expression, "STRING_AGG",
                              item, parameters)
    if not present:
        return column_of(item.output_name, [None], str)[0], None
    try:
        between = "" if item.separator is None else item.separator.evaluate(
            {}, parameters or {}
        )
    except PredicateError as exc:
        raise SourceError.carrying(exc) from exc
    # Written out the way a cast to text writes it, so a moment among them
    # is the same string a client would have been shown on its own.
    joined = ("" if between is None else converted(between, "NVARCHAR")).join(
        converted(value, "NVARCHAR") for value in present
    )
    return column_of(item.output_name, [joined])[0], joined


def _in_told_order(table: Table, rows: list, keys: tuple, parameters) -> list:
    """A group's rows in the order WITHIN GROUP asked for."""
    if not keys:
        return rows
    ordered = list(rows)
    for key in reversed(keys):
        read = _read_key(table, key.column, parameters)
        ordered.sort(key=lambda row, read=read: (read(row) is not None,
                                                 collated(read(row))),
                     reverse=key.descending)
    return ordered


def _values(table: Table, rows: list[list[object]], name: str, function: str,
            item=None, parameters=None):
    """The non-null values an aggregate reduces, and the column they came from.

    A plain column is read by position. Anything else is evaluated per row,
    and its column stands in for a type: an expression has no declared one,
    so what it produced decides, the same way a source's own columns are
    typed.

    Evaluated with the statement's variables, which it used to be without:
    MAX(id + @r) read @r as null and answered NULL where a real server
    answers the largest id plus @r, and a COUNT over a CASE naming one
    counted nothing. A parameterised statement puts its values exactly there.
    """
    if item is not None and item.argument is not None:
        from .predicate import PredicateError
        from .source import infer_column

        names = table.column_names
        produced = []
        for row in rows:
            try:
                produced.append(item.argument.evaluate(dict(zip(names, row)),
                                                       parameters or {}))
            except PredicateError as exc:
                raise SourceError.carrying(exc) from exc
        column, values = infer_column(name, produced)
        present = [value for value in values if value is not None]
        if not present:
            # Nothing to read a type off, so the argument says what it is:
            # measured, MAX(5) over no rows at all is an int column, where
            # this declared the text a column of nothing else gets.
            declared = DECLARED_FOR.get(
                result_kind(item.argument, holdings(table.columns)))
            if declared is not None:
                column = Column(name, declared)
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

    reading = [_read_key(table, written, parameters) for written in keys]
    groupings = grouping_expressions(keys)
    partitions: dict[tuple, list[list[object]]] = {}
    for row in rows:
        signature = tuple(collated(read(row)) for read in reading)
        partitions.setdefault(signature, []).append(row)

    columns: list[Column] = []
    out: list[list[object]] = []
    for members in partitions.values():
        one, values = compute(table, members, items, group_row=members[0],
                              parameters=parameters, groupings=groupings)
        columns = one
        out.append(values[0])
    if not columns:
        # No rows at all still has to declare the shape it would have had.
        columns, _ = compute(table, [], items, group_row=None,
                             parameters=parameters, groupings=groupings)
        return columns, out

    for at, item in enumerate(items):
        if item.is_aggregate or item.node is None:
            continue
        if aggregates_in(item.node) or ungrouped(item.node, groupings):
            continue
        # Declared from every group's value rather than the last group's.
        # compute() sees one group at a time and a group that worked out
        # NULL would have the whole column declared as text, which is the
        # one case values cannot decide; result_kind covers it when they
        # are all NULL.
        column, converted = column_of(
            item.output_name, [row[at] for row in out],
            result_kind(item.node, holdings(table.columns)),
        )
        columns[at] = column
        for row, value in zip(out, converted):
            row[at] = value
    return columns, out


def _read_key(table: Table, written: str, parameters):
    """How to work out what one GROUP BY entry groups on, for a row.

    A column is read by position, which is what nearly every GROUP BY names.
    Anything else is an expression over the row and is worked out per row:
    GROUP BY YEAR(created) is a year for each one and a group for each year.
    """
    at = table.index_of(written)
    if at is not None:
        return lambda row: row[at]

    try:
        node = parse_expression(written)
    except PredicateError as exc:
        if exc.number == ONLY_IN_SELECT_OR_ORDER_BY:
            # It already says what is wrong and where windows may go.
            raise SourceError.carrying(exc) from exc
        raise SourceError(f"cannot group by '{written}': {exc}",
                          number=exc.number) from exc

    def worked_out(row):
        try:
            return node.evaluate(named_row(table, row), parameters or {})
        except PredicateError as exc:
            raise SourceError(f"cannot group by '{written}': {exc}",
                              number=exc.number) from exc

    return worked_out


def named_row(table: Table, row: list) -> dict:
    """A row under every name its columns answer to.

    A join qualifies its columns, so p.team is also team where nothing else
    is called that, which is how a GROUP BY may name either.
    """
    named: dict[str, object] = {}
    for column, value in zip(table.columns, row):
        named.setdefault(column.name, value)
        named.setdefault(column.name.rsplit(".", 1)[-1], value)
    return named


def _position(table: Table, name: str) -> int:
    at = table.index_of(name)
    if at is None:
        raise SourceError(
            f"cannot group by '{name}': the table has no such column"
        )
    return at


def compute(
    table: Table, rows: list[list[object]], items, group_row=None,
    parameters=None, groupings=(),
) -> tuple[list[Column], list[list[object]]]:
    """Reduce the rows to the single row an aggregated select asks for.

    With a group_row, the non-aggregated entries in the select list are read
    from it: they read only what the grouping fixes, so every row in the
    partition works them out the same and the first will do.
    """
    columns: list[Column] = []
    values: list[object] = []
    # Entries that compute over aggregates cannot be worked out until the
    # aggregates have been, so they keep their place and are filled in below.
    later: list[tuple[int, object]] = []

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
            if item.node is not None and aggregates_in(item.node):
                later.append((len(columns), item))
                columns.append(None)
                values.append(None)
                continue
            if item.node is not None:
                if ungrouped(item.node, groupings) is None:
                    # Reads only what the grouping fixes, the grouped
                    # expression itself or rank + 1 beside GROUP BY rank.
                    # Every row of the partition works it out the same, so
                    # the first will do: the same reason a plain grouped
                    # column is read off one row below.
                    value = (None if group_row is None else
                             item.node.evaluate(named_row(table, group_row),
                                                parameters or {}))
                    column, converted = column_of(item.output_name, [value])
                    columns.append(column)
                    values.append(converted[0])
                    continue
                # It reads the row, and a group is many rows. Reached only
                # after the value is known to change per row, which the
                # parser cannot see when the value is a subquery it has not
                # run yet.
                # Said in this project's own words rather than SQL Server's,
                # because what is left of a lifted subquery has no name a
                # person would know, and naming the placeholder would be
                # worse than describing what is wrong with it.
                raise SourceError(
                    "a value that changes from row to row is in the select "
                    "list beside an aggregate, and is neither aggregated nor "
                    "named in the GROUP BY",
                    number=NOT_GROUPED_OR_AGGREGATED,
                )
            # A grouped column. Its value is the one the whole partition
            # shares, and its type is whatever the table declared.
            at = _position(table, item.expression)
            columns.append(Column(item.output_name, table.columns[at].type))
            values.append(group_row[at] if group_row is not None else None)
            continue

        if function == "STRING_AGG":
            column, joined = _run_together(table, rows, item, parameters)
            columns.append(column)
            values.append(joined)
            continue

        if function in COUNTS:
            if item.expression is None:
                count = len(rows)          # COUNT(*) counts rows
            else:
                _, present = _values(table, rows, item.expression, function,
                                     item, parameters)
                count = len(present)       # COUNT(col) counts non-nulls
            wide = function == "COUNT_BIG"
            columns.append(Column(item.output_name,
                                  WIDE_COUNT_TYPE if wide else COUNT_TYPE))
            values.append(count)
            continue

        if item.expression is None:
            raise SourceError(f"{function}() needs a column")

        column, present = _values(table, rows, item.expression, function,
                                  item, parameters)

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
            result = None if not present else totalled(present)

        elif function == "AVG":
            _numeric(column, function)
            result_type = column.type
            if not present:
                result = None
            elif isinstance(column.type, Integer):
                # Truncated, as SQL Server does it.
                result = int(totalled(present) / len(present))
            else:
                result = totalled(present) / len(present)

        elif function in SPREAD:
            _numeric(column, function)
            # Float whatever the column was declared, because the answer is
            # not a count of the thing the column holds.
            result_type = Float()
            result = (None if not present
                      else SPREAD[function](*sums_of(present)))

        else:
            raise SourceError(f"'{function}' is not an aggregate this server knows")

        columns.append(Column(item.output_name, result_type))
        values.append(result)

    if later:
        # Every aggregate this group worked out, under the name an expression
        # naming it uses, beside whatever the group's own row holds. An entry
        # may reach for both: UPPER(team) + CAST(COUNT(*) AS nvarchar(4)).
        named = named_row(table, group_row) if group_row is not None else {}
        for item, value in zip(items, values):
            if not item.is_aggregate:
                continue
            written = item.expression or "*"
            for said in (written, written.rsplit(".", 1)[-1]):
                inside = f"DISTINCT {said}" if item.distinct else said
                named.setdefault(f"{item.function}({inside})", value)
        for at, item in later:
            try:
                worked_out = item.node.evaluate(named, parameters or {})
            except PredicateError as exc:
                raise SourceError.carrying(exc) from exc
            column, converted = column_of(
                item.output_name, [worked_out],
                # What the columns hold, so a group whose every row was NULL
                # is still declared by what its aggregates reduce.
                result_kind(item.node, holdings(table.columns)),
            )
            columns[at] = column
            values[at] = converted[0]

    return columns, [values]
