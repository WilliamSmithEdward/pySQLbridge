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

from .aggregate import SPREAD, named_row, sums_of, totalled
from .predicate import (
    CONVERSION_ERROR,
    COUNTS,
    NEGATIVE_OFFSET,
    NOT_A_TILE_COUNT,
    NTILE_READS_A_ROW,
    Literal,
    PredicateError,
    collated,
    columns_in,
    converted,
    parse_expression,
)
from .source import SourceError, Table, column_of, holdings
from .tds.result import Column, Integer

# ROW_NUMBER and its like count rows, and SQL Server counts them in a bigint
# however few there are. NTILE too. Measured.
COUNTED = Integer(8)

# What each of these needs told before it can answer.
RANKING = frozenset({"ROW_NUMBER", "RANK", "DENSE_RANK", "NTILE"})
READS_ANOTHER_ROW = frozenset({"LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE"})
REDUCES = frozenset({"SUM", "MIN", "MAX", "AVG"}) | COUNTS | set(SPREAD)


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

    if window.function in RANKING or window.function == "COUNT_BIG":
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

    Returned as the first and last position each one ties with, which is what
    a frame counting by RANGE reaches to. With no order every row is a peer
    of every other, which is the whole partition.
    """
    if not window.order_by:
        last = len(members) - 1
        return members, ([0] * len(members), [last] * len(members))

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
    lasts = [0] * len(ordered)
    end = len(ordered) - 1
    for at in range(len(ordered) - 1, -1, -1):
        if at < len(ordered) - 1 and marks[at] != marks[at + 1]:
            end = at
        lasts[at] = end
    firsts = [0] * len(ordered)
    start = 0
    for at in range(len(ordered)):
        if at and marks[at] != marks[at - 1]:
            start = at
        firsts[at] = start
    return ordered, (firsts, lasts)


def _frame(place: int, window, ordered: list, peers: tuple) -> tuple:
    """Which rows of the ordered partition this one sees, as a first and last.

    A first past the last means the frame is empty, which is a legitimate
    thing for it to be: two rows before this one, at the first row, is
    nothing at all, and an aggregate over nothing is NULL.
    """
    firsts, lasts = peers
    last = len(ordered) - 1
    frame = getattr(window, "frame", None)
    if frame is None:
        # What SQL Server uses when a query does not say: the whole partition
        # with no order, and everything through this row's ties with one.
        return (0, last) if not window.order_by else (0, lasts[place])

    def edge(bound, beginning):
        if bound[0] == "UNBOUNDED":
            return 0 if bound[1] == "PRECEDING" else last
        if bound[0] == "CURRENT":
            if frame.kind == "RANGE":
                return firsts[place] if beginning else lasts[place]
            return place
        step = bound[1] if bound[0] == "FOLLOWING" else -bound[1]
        return place + step

    return max(0, edge(frame.start, True)), min(last, edge(frame.end, False))


def _answer(answers: list, table: Table, rows: list, ordered: list,
            peers: list, window, parameters) -> None:
    """Fill in this partition's answers, one per row."""
    function = window.function
    if function in RANKING:
        _rank(answers, table, ordered, peers, window, parameters)
        return
    if function in READS_ANOTHER_ROW:
        _read_another(answers, table, rows, ordered, peers, window, parameters)
        return
    _reduce(answers, table, rows, ordered, peers, window, parameters)


def _rank(answers: list, table: Table, ordered: list, peers: list, window,
          parameters) -> None:
    if window.function == "ROW_NUMBER":
        for place, at in enumerate(ordered, start=1):
            answers[at] = place
        return
    if window.function == "NTILE":
        _ntile(answers, ordered, _tile_count(table, window, parameters))
        return

    # RANK counts the rows before this one's peers; DENSE_RANK counts the
    # peer groups. Both start at one.
    dense = window.function == "DENSE_RANK"
    _, lasts = peers
    groups = 0
    start = 0
    while start < len(ordered):
        groups += 1
        finish = lasts[start]
        for at in ordered[start:finish + 1]:
            answers[at] = groups if dense else start + 1
        start = finish + 1


def _tile_count(table: Table, window, parameters) -> int:
    """How many tiles NTILE was asked for, refused where a real server would.

    Worked out with the statement's variables, which it was not: NTILE(@n)
    was refused as though @n were null. Measured on SQL Server 2025, the
    count has to be an integer type and above nought, so a decimal 2.0 is
    refused as surely as a null or a nought, and it may not read the rows it
    is tiling, which a real server settles before running anything.
    """
    if len(window.arguments) != 1:
        raise SourceError("NTILE() needs to be told how many tiles")
    try:
        node = parse_expression(window.arguments[0])
    except PredicateError as exc:
        raise SourceError.carrying(exc) from exc
    for column in columns_in(node):
        if table.index_of(column.qualified or column.name) is not None:
            raise SourceError(
                f'The reference to column "{column.name}" is not allowed in '
                f"an argument to the NTILE function. Only references to "
                f"columns at an outer scope or standalone expressions and "
                f"subqueries are allowed here.",
                number=NTILE_READS_A_ROW, severity=15)
    try:
        tiles = node.evaluate({}, parameters or {})
    except PredicateError as exc:
        raise SourceError.carrying(exc) from exc
    if not isinstance(tiles, int) or isinstance(tiles, bool) or tiles < 1:
        raise SourceError(
            "The function 'ntile' takes only a positive int or bigint "
            "expression as its input.", number=NOT_A_TILE_COUNT, severity=15)
    return tiles


def _ntile(answers: list, ordered: list, tiles: int) -> None:
    """Split the partition into so many tiles, the bigger ones first.

    Six rows into four tiles is two, two, one, one. Measured; the remainder
    goes to the tiles at the front rather than being spread out.
    """
    each, spare = divmod(len(ordered), tiles)
    place = 0
    for tile in range(1, tiles + 1):
        size = each + (1 if tile <= spare else 0)
        for at in ordered[place:place + size]:
            answers[at] = tile
        place += size


def _read_another(answers: list, table: Table, rows: list, ordered: list,
                  peers: list, window, parameters) -> None:
    """LAG, LEAD, FIRST_VALUE and LAST_VALUE, which read one row of the window.

    LAG and LEAD's offset and default are read from the row asking, and from
    the statement's variables; the offset was worked out once with neither,
    so LAG(id, @n) and LAG(id, rank) both reached back one row, and LAG(id,
    -1) read the row after. Measured on SQL Server 2025: an offset is read
    per row, is converted to a bigint, may not be below nought, and when it
    is null every row answers its default.
    """
    if not window.arguments:
        raise SourceError(f"{window.function}() needs something to read")
    read = _reader(table, window.arguments[0], parameters)
    reach = (_reach(table, window.arguments[1], parameters)
             if len(window.arguments) > 1 else lambda row: 1)
    missing = (_reader(table, window.arguments[2], parameters)
               if len(window.arguments) > 2 else lambda row: None)

    for place, at in enumerate(ordered):
        if window.function in ("FIRST_VALUE", "LAST_VALUE"):
            # The two ends of the frame. With no frame written, the end is
            # this row's last tie, which is why LAST_VALUE over a plain
            # ORDER BY is this row and not the last one.
            start, finish = _frame(place, window, ordered, peers)
            if start > finish:
                answers[at] = None
                continue
            wanted = start if window.function == "FIRST_VALUE" else finish
            answers[at] = read(rows[ordered[wanted]])
            continue
        step = reach(rows[at])
        wanted = (None if step is None
                  else place - step if window.function == "LAG"
                  else place + step)
        answers[at] = (read(rows[ordered[wanted]])
                       if wanted is not None and 0 <= wanted < len(ordered)
                       else missing(rows[at]))


def _reduce(answers: list, table: Table, rows: list, ordered: list,
            peers: list, window, parameters) -> None:
    """An aggregate over the window, which is the frame ending at this row.

    With no order the frame is the whole partition and every row of it gets
    the same answer. With one it reaches to the end of this row's peers,
    which is what makes it a running total when the order is unique and the
    partition's total when it is not.
    """
    counting_rows = window.function in COUNTS and window.argument is None
    read = None if counting_rows else _reader(
        table, window.argument, parameters, node=window.node
    )
    held = [None if counting_rows else read(rows[at]) for at in ordered]

    bounds = [_frame(place, window, ordered, peers)
              for place in range(len(ordered))]
    last = len(ordered) - 1
    if all(start == 0 for start, _ in bounds):
        # The frame begins where the partition does, so each row's answer is
        # the one before it with more added.
        _accumulate(answers, ordered, held, bounds, window)
        return
    if all(finish == last for _, finish in bounds):
        # And the same the other way, for a frame that runs to the end of
        # the partition: the last row's answer is the smallest, and every
        # row before it adds one more value to what the row after it saw.
        # Measured, this is the order a real server works in too, which the
        # slice below is not: SUM over 1, 1 and -1e16 is -1e16 there and
        # -9999999999999998 from adding them the other way about.
        _accumulate(answers, ordered, held, bounds, window, backwards=True)
        return

    for place, at in enumerate(ordered):
        start, finish = bounds[place]
        answers[at] = _reduced(held[start:finish + 1] if start <= finish else [],
                               window)


def _accumulate(answers: list, ordered: list, held: list, bounds: list,
                window, backwards: bool = False) -> None:
    """Answers for a frame that always reaches one end of the partition.

    Each row's answer is the one before it with more added, which is what
    makes this worth keeping apart from the general case: a running total
    over a partition of n rows costs n additions rather than n totals.

    It used to keep every value seen so far and total the lot again each
    time a row was added, so the answer was right and the cost was n
    squared: measured at 0.01 seconds over a thousand rows and 0.51 over
    eight thousand, doubling the rows quadrupling the time. A file with
    fifty thousand rows in it is an ordinary thing to point this at.

    The frame can be empty at the start, which is what ROWS BETWEEN
    UNBOUNDED PRECEDING AND 2 PRECEDING asks for at the first two rows, and
    an aggregate over nothing is NULL except COUNT, which is 0. Measured.
    That case used to read an answer that had not been worked out yet and
    fail with an UnboundLocalError.
    """
    running = _running(window)
    answer = running.answer()               # the frame before anything is in it
    if backwards:
        reached = len(ordered)
        for place in range(len(ordered) - 1, -1, -1):
            start = bounds[place][0]
            if start <= reached - 1:
                for value in reversed(held[start:reached]):
                    running.add(value)
                reached = start
                answer = running.answer()
            answers[ordered[place]] = answer
        return
    reached = 0
    for place, at in enumerate(ordered):
        finish = bounds[place][1]
        if finish >= reached:
            for value in held[reached:finish + 1]:
                running.add(value)
            reached = finish + 1
            answer = running.answer()
        answers[at] = answer


def _running(window):
    """Something to add the values to one at a time, for this aggregate.

    Every one of them takes the values in the order they arrive and needs to
    know nothing else. Which way the frame is growing decides that order and
    nothing beyond it: MIN and MAX keep the first value they meet, and a
    total and a spread are built from the values as they come. Measured,
    that is what a real server answers going either way.
    """
    function = window.function
    if function in COUNTS:
        return _Counting(window.argument is None)
    if function == "SUM":
        return _Totalling(False)
    if function == "AVG":
        return _Totalling(True)
    if function in ("MIN", "MAX"):
        return _Furthest(function == "MAX")
    return _Spread(window.function)


class _Counting:
    """How many rows, or how many of them held a value."""

    def __init__(self, every_row: bool) -> None:
        self.every_row = every_row
        self.so_far = 0

    def add(self, value) -> None:
        if self.every_row or value is not None:
            self.so_far += 1

    def answer(self):
        return self.so_far


class _Totalling:
    """The values added up, and divided by how many there were for an AVG."""

    def __init__(self, mean: bool) -> None:
        self.mean = mean
        self.total = 0
        self.counted = 0

    def add(self, value) -> None:
        # One after another, which is what totalled does over a whole frame
        # and what a real server does; see the note there.
        if value is None:
            return
        try:
            self.total = self.total + value
        except TypeError as exc:
            raise SourceError(
                f"{'AVG' if self.mean else 'SUM'}() over a window needs "
                f"numbers, and this column holds text"
            ) from exc
        self.counted += 1

    def answer(self):
        if not self.counted:
            return None
        return self.total / self.counted if self.mean else self.total


class _Furthest:
    """The biggest or smallest so far, under the declared collation.

    The first of several values the collation calls equal is kept, meaning
    the first one met rather than the first one in the frame. Measured, and
    the distinction is real: over a, A, b, B a real server answers MIN as a
    where the frame grows forwards and A where it grows backwards, because
    it meets them in the other order.
    """

    def __init__(self, biggest: bool) -> None:
        self.biggest = biggest
        self.best = None
        self.mark = None

    def add(self, value) -> None:
        if value is None:
            return
        mark = collated(value)
        if self.mark is None or (mark > self.mark if self.biggest
                                 else mark < self.mark):
            self.best, self.mark = value, mark

    def answer(self):
        return self.best


class _Spread:
    """How far the values are spread, from how many, their total and squares.

    Worked out from running sums rather than from the mean, which is what a
    real server does and what lets this be kept up to date at all: a mean
    moves as rows arrive and every value would have to be read again.
    """

    def __init__(self, function: str) -> None:
        self.function = function
        self.count = 0
        self.total = 0
        self.squares = 0

    def add(self, value) -> None:
        if value is None:
            return
        try:
            self.total = self.total + value
            self.squares = self.squares + value * value
        except TypeError as exc:
            raise SourceError(
                f"{self.function}() over a window needs numbers, and this "
                f"column holds text"
            ) from exc
        self.count += 1

    def answer(self):
        if not self.count:
            return None
        return SPREAD[self.function](self.count, self.total, self.squares)


def _reduced(values: list, window):
    """One aggregate over the values in the frame, NULLs left out."""
    function = window.function
    if function in COUNTS:
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
            return totalled(present)
        if function in SPREAD:
            return SPREAD[function](*sums_of(present))
        return totalled(present) / len(present)
    except TypeError as exc:
        raise SourceError(
            f"{function}() over a window needs numbers, and this column "
            f"holds text"
        ) from exc


def _reduced_kind(table: Table, window):
    """What the answer is, for a window that produced only NULLs.

    A count is a count and a mean is a float whatever they were given. The
    rest are the type of the column they reduced, which is why the table is
    here: a frame that is empty for every row of the partition answers NULL
    every time, and the values cannot say what the column was. Measured, a
    SUM over score is a float column there even where it answered nothing.

    None where the argument is an expression rather than a column, which is
    the answer that lets the values decide and costs a text column of NULLs.
    """
    if window.function in COUNTS:
        return int
    if window.function == "AVG" or window.function in SPREAD:
        return float
    if window.node is None and window.argument:
        return holdings(table.columns).get(window.argument.lower())
    if window.function in READS_ANOTHER_ROW and window.arguments:
        # The column read, the same way: LAG(id, @n) with @n null is an int
        # column of NULLs on a real server, measured, and so is a LAG that
        # reaches past every row.
        return holdings(table.columns).get(window.arguments[0].lower())
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
            raise SourceError.carrying(exc) from exc

    return worked_out


def _written(key) -> str:
    """What one ORDER BY key of a window names."""
    return key.column


def _sortable(value):
    """A value as something sortable, with NULLs first the way SQL Server has
    them. The pair keeps a NULL away from every value rather than comparing
    with it."""
    return (value is not None, collated(value))


def _reach(table: Table, written: str, parameters):
    """How far LAG or LEAD reaches from one row, or None for not at all.

    A bigint, the way a real server converts it: 1.9 is 1, '2' is 2, and
    'x' is msg 8114. Below nought is 8730, with state 1 where the query
    wrote the number and 2 where a variable or a column held it.
    """
    read = _reader(table, written, parameters)
    try:
        written_out = isinstance(parse_expression(written), Literal)
    except PredicateError:
        written_out = False

    def reached(row):
        value = read(row)
        if value is None:
            return None
        try:
            step = converted(value, "BIGINT")
        except PredicateError:
            raise SourceError("Error converting data type nvarchar to bigint.",
                              number=CONVERSION_ERROR, state=5) from None
        if step < 0:
            raise SourceError(
                "Offset parameter for Lag and Lead functions cannot be a "
                "negative value.", number=NEGATIVE_OFFSET,
                state=1 if written_out else 2)
        return step

    return reached
