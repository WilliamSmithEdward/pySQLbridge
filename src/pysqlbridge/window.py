"""A function over a window of rows: FUNC(...) OVER (PARTITION BY ... ORDER BY ...).

Not an aggregate, which reduces many rows to one, and not an ordinary
expression, which reads one row. A window function answers once per row and
reads a set of rows to do it, so it is worked out after the rows are settled,
over all of them, and each answer put back beside the row it belongs to.

The window is a group, ordered. PARTITION BY says which rows are in it and is
the same thing a GROUP BY says; ORDER BY says the order to work in and is the
same thing an ORDER BY says. What is in the window past that is the frame,
and the frame this implements is the one SQL Server uses when a query does not
say: everything in the partition when there is no order, and everything up to
and including this row's peers when there is. Peers being included is what
makes SUM(x) OVER (ORDER BY team) the same number for every row of a team
rather than a running total within it. Measured.

An explicit frame is refused rather than ignored; see sql._read_over.
"""

from __future__ import annotations

from .aggregate import named_row
from .predicate import PredicateError, collated, parse_expression
from .source import SourceError, Table, column_of
from .tds.result import Column, Integer

# ROW_NUMBER and its like count rows, and SQL Server counts them in a bigint
# however few there are. NTILE too. Measured.
COUNTED = Integer(8)

# What each of these needs told before it can answer.
RANKING = frozenset({"ROW_NUMBER", "RANK", "DENSE_RANK", "NTILE"})
READS_ANOTHER_ROW = frozenset({"LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE"})
REDUCES = frozenset({"COUNT", "SUM", "MIN", "MAX", "AVG"})


def over(name: str, table: Table, rows: list, window, parameters) -> tuple:
    """One value for every row, and the column to declare them as.

    Returned in the order the rows came in, whatever order the window worked
    in, because they are going back into those rows.
    """
    if window.function not in RANKING | READS_ANOTHER_ROW | REDUCES:
        raise SourceError(
            f"'{window.function}' is not a function this server knows over a "
            f"window; it has "
            f"{', '.join(sorted(RANKING | READS_ANOTHER_ROW | REDUCES))}"
        )

    answers: list = [None] * len(rows)
    for members in _partitions(table, rows, window, parameters):
        ordered, peers = _in_window_order(table, rows, members, window, parameters)
        _answer(answers, table, rows, ordered, peers, window, parameters)

    if window.function in RANKING:
        return Column(name, COUNTED), answers
    return column_of(name, answers, _reduced_kind(table, window))


def _partitions(table: Table, rows: list, window, parameters) -> list:
    """The rows of each partition, as positions, in the order they arrived."""
    if not window.partition_by:
        return [list(range(len(rows)))]
    readers = [_reader(table, written, parameters)
               for written in window.partition_by]
    found: dict = {}
    for at, row in enumerate(rows):
        signature = tuple(collated(read(row)) for read in readers)
        found.setdefault(signature, []).append(at)
    return list(found.values())


def _in_window_order(table: Table, rows: list, members: list, window,
                     parameters) -> tuple[list, list]:
    """The partition in the window's order, and where each peer group ends.

    peers[i] is the last position in the ordered partition that this one ties
    with, which is what the default frame reaches to. With no order every row
    is a peer of every other, which is the whole partition.
    """
    if not window.order_by:
        return members, [len(members) - 1] * len(members)

    readers = [_reader(table, _written(key), parameters)
               for key in window.order_by]
    ordered = list(members)
    # One key at a time from the last to the first, so each direction is
    # honoured on its own; the same way an ORDER BY is applied.
    for key, read in reversed(list(zip(window.order_by, readers))):
        ordered.sort(
            key=lambda at, read=read: _sortable(read(rows[at])),
            reverse=key.descending,
        )

    marks = [tuple(collated(read(rows[at])) for read in readers)
             for at in ordered]
    peers = [0] * len(ordered)
    end = len(ordered) - 1
    for at in range(len(ordered) - 1, -1, -1):
        if at < len(ordered) - 1 and marks[at] != marks[at + 1]:
            end = at
        peers[at] = end
    return ordered, peers


def _answer(answers: list, table: Table, rows: list, ordered: list,
            peers: list, window, parameters) -> None:
    """Fill in this partition's answers, one per row."""
    function = window.function
    if function in RANKING:
        _rank(answers, ordered, peers, window)
        return
    if function in READS_ANOTHER_ROW:
        _read_another(answers, table, rows, ordered, peers, window, parameters)
        return
    _reduce(answers, table, rows, ordered, peers, window, parameters)


def _rank(answers: list, ordered: list, peers: list, window) -> None:
    if window.function == "ROW_NUMBER":
        for place, at in enumerate(ordered, start=1):
            answers[at] = place
        return
    if window.function == "NTILE":
        _ntile(answers, ordered, window)
        return

    # RANK counts the rows before this one's peers; DENSE_RANK counts the
    # peer groups. Both start at one.
    dense = window.function == "DENSE_RANK"
    groups = 0
    start = 0
    while start < len(ordered):
        groups += 1
        finish = peers[start]
        for at in ordered[start:finish + 1]:
            answers[at] = groups if dense else start + 1
        start = finish + 1


def _ntile(answers: list, ordered: list, window) -> None:
    """Split the partition into so many tiles, the bigger ones first.

    Six rows into four tiles is two, two, one, one. Measured; the remainder
    goes to the tiles at the front rather than being spread out.
    """
    if len(window.arguments) != 1:
        raise SourceError("NTILE() needs to be told how many tiles")
    try:
        tiles = int(parse_expression(window.arguments[0]).evaluate({}, {}))
    except (PredicateError, TypeError, ValueError) as exc:
        raise SourceError(
            "The function 'ntile' takes only a positive int or bigint "
            "expression as its input."
        ) from exc
    if tiles < 1:
        raise SourceError(
            "The function 'ntile' takes only a positive int or bigint "
            "expression as its input."
        )
    each, spare = divmod(len(ordered), tiles)
    place = 0
    for tile in range(1, tiles + 1):
        size = each + (1 if tile <= spare else 0)
        for at in ordered[place:place + size]:
            answers[at] = tile
        place += size


def _read_another(answers: list, table: Table, rows: list, ordered: list,
                  peers: list, window, parameters) -> None:
    """LAG, LEAD, FIRST_VALUE and LAST_VALUE, which read one row of the window."""
    if not window.arguments:
        raise SourceError(f"{window.function}() needs something to read")
    read = _reader(table, window.arguments[0], parameters)
    step = _a_number(window.arguments[1], 1) if len(window.arguments) > 1 else 1
    missing = (_a_value(window.arguments[2], parameters)
               if len(window.arguments) > 2 else None)

    for place, at in enumerate(ordered):
        if window.function == "FIRST_VALUE":
            answers[at] = read(rows[ordered[0]])
            continue
        if window.function == "LAST_VALUE":
            # The end of the frame, which is the end of this row's peers.
            # That is why LAST_VALUE with a plain ORDER BY is this row.
            answers[at] = read(rows[ordered[peers[place]]])
            continue
        wanted = place - step if window.function == "LAG" else place + step
        answers[at] = (read(rows[ordered[wanted]])
                       if 0 <= wanted < len(ordered) else missing)


def _reduce(answers: list, table: Table, rows: list, ordered: list,
            peers: list, window, parameters) -> None:
    """An aggregate over the window, which is the frame ending at this row.

    With no order the frame is the whole partition and every row of it gets
    the same answer. With one it reaches to the end of this row's peers,
    which is what makes it a running total when the order is unique and the
    partition's total when it is not.
    """
    counting_rows = window.function == "COUNT" and window.argument is None
    read = None if counting_rows else _reader(
        table, window.argument, parameters, node=window.node
    )

    if not window.order_by:
        answer = _reduced([None] * len(ordered) if counting_rows
                          else [read(rows[at]) for at in ordered], window)
        for at in ordered:
            answers[at] = answer
        return

    seen: list = []
    start = 0
    while start < len(ordered):
        finish = peers[start]
        seen.extend(None if counting_rows else read(rows[at])
                    for at in ordered[start:finish + 1])
        answer = _reduced(seen, window)
        for at in ordered[start:finish + 1]:
            answers[at] = answer
        start = finish + 1


def _reduced(values: list, window):
    """One aggregate over the values in the frame, NULLs left out."""
    function = window.function
    if function == "COUNT":
        return (len(values) if window.argument is None
                else sum(1 for v in values if v is not None))
    present = [v for v in values if v is not None]
    if not present:
        return None
    if function == "MIN":
        return min(present, key=collated)
    if function == "MAX":
        return max(present, key=collated)
    try:
        if function == "SUM":
            return sum(present)
        return sum(present) / len(present)
    except TypeError as exc:
        raise SourceError(
            f"{function}() over a window needs numbers, and this column "
            f"holds text"
        ) from exc


def _reduced_kind(table: Table, window):
    """What the answer is, for a window that produced only NULLs."""
    if window.function == "COUNT":
        return int
    if window.function == "AVG":
        return float
    return None


def _reader(table: Table, written: str | None, parameters, node=None):
    """How to read what an expression says, for one row of the table."""
    if written is None and node is None:
        raise SourceError("a window function was given nothing to read")
    if node is None:
        at = table.index_of(written)
        if at is not None:
            return lambda row: row[at]
        try:
            node = parse_expression(written)
        except PredicateError as exc:
            raise SourceError(f"cannot read '{written}' in the OVER clause: "
                              f"{exc}", number=exc.number) from exc

    def worked_out(row, node=node):
        try:
            return node.evaluate(named_row(table, row), parameters or {})
        except PredicateError as exc:
            raise SourceError(str(exc), number=exc.number) from exc

    return worked_out


def _written(key) -> str:
    """What one ORDER BY key of a window names."""
    return key.column


def _sortable(value):
    """A value as something sortable, with NULLs first the way SQL Server has
    them. The pair keeps a NULL away from every value rather than comparing
    with it."""
    return (value is not None, collated(value))


def _a_number(written: str, otherwise: int) -> int:
    try:
        return int(parse_expression(written).evaluate({}, {}))
    except (PredicateError, TypeError, ValueError):
        return otherwise


def _a_value(written: str, parameters):
    try:
        return parse_expression(written).evaluate({}, parameters or {})
    except PredicateError:
        return None
