"""Just enough SELECT to serve a table.

Not a SQL engine. It recognises the shape clients actually send to read a
table, and refuses everything else clearly rather than half-executing it:

    SELECT * FROM people
    SELECT TOP 100 id, name FROM [dbo].[people] ORDER BY name
    SELECT "id" FROM mydb.dbo.people WHERE id = @id
    SELECT COUNT(*) AS n, MAX(score) FROM people

Three details are not optional even for something this small. Identifiers
arrive bracketed, because that is what Excel, Power BI and SSMS generate rather
than the bare names a person would type. Names can be qualified up to three
parts, and only the last is the table. And matching is case-insensitive,
because SQL Server's default collation is and a client that round-trips a name
through its own UI may not preserve case.

Refusing loudly matters more here than coverage. Anything past the WHERE, an
ORDER BY or a join, is refused by name rather than dropped, because a clause
that parsed and was then ignored would return the wrong rows and look right.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .predicate import (
    AGGREGATE_NAMES,
    COUNTS,
    Column as ColumnRef,
    PredicateError,
    GROUP_BY_NEEDS_A_COLUMN,
    NEEDS_AN_ORDER_BY,
    NEEDS_AN_OVER_CLAUSE,
    NO_DISTINCT_OVER,
    NOT_A_RECURSION,
    ROW_COUNT_CANNOT_BE_NEGATIVE,
    ROW_COUNT_MUST_BE_WHOLE,
    UNEVEN_VALUE_ROWS,
    NO_NAME_FOR_A_VALUES_COLUMN,
    MORE_VALUES_THAN_NAMES,
    FEWER_VALUES_THAN_NAMES,
    NOT_GROUPED_OR_AGGREGATED,
    ONLY_IN_SELECT_OR_ORDER_BY,
    SYNTAX_ERROR,
    UNCLOSED_QUOTATION,
    NEAR_A_KEYWORD,
    MISSING_END_COMMENT,
    UNDECLARED_VARIABLE,
    UNDECLARED_TABLE_VARIABLE,
    WINDOW_FUNCTIONS,
    aggregates_in,
    one_spelling,
    parse_expression,
    parse_predicate,
    reads_a_column,
)

# Bracketed, double-quoted, or bare. The bare form stops at anything that could
# start the next token.
_IDENTIFIER = re.compile(
    r"""
    \[ (?P<bracketed> (?: [^\]] | \]\] )* ) \]
  | " (?P<quoted>    (?: [^"]  | ""   )* ) "
  | (?P<bare> [A-Za-z_@#][A-Za-z0-9_@#$]* )
    """,
    re.VERBOSE,
)

# No leading ^ on these. Pattern.match(text, pos) already anchors at pos, and
# a ^ would additionally demand pos be the start of the string, so the two
# matched mid-statement never fire.
_SELECT = re.compile(r"\s*SELECT\s+", re.IGNORECASE)
_WITH = re.compile(r"\s*WITH\s+", re.IGNORECASE)
_SUBQUERY_NAME = "@__subquery_"
_DISTINCT = re.compile(r"\s*DISTINCT\s+", re.IGNORECASE)
_TOP = re.compile(
    r"\s*TOP\s+(?:\(\s*)?(\d+|@[A-Za-z0-9_@#$]+)\s*\)?\s*"
    r"(PERCENT\s*)?(WITH\s+TIES\s*)?",
    re.IGNORECASE,
)

# What SQL Server calls TOP ... WITH TIES with nothing to tie on.
TIES_NEED_AN_ORDER = 1062
_FROM = re.compile(r"\s*FROM\s+", re.IGNORECASE)
_WHERE = re.compile(r"\s*WHERE\s+", re.IGNORECASE)
_ORDER_BY = re.compile(r"\s*ORDER\s+BY\s+", re.IGNORECASE)
_GROUP_BY = re.compile(r"\s*GROUP\s+BY\s+", re.IGNORECASE)
_HAVING = re.compile(r"\s*HAVING\s+", re.IGNORECASE)
_OFFSET = re.compile(
    r"\s*OFFSET\s+(\d+|@[A-Za-z0-9_@#$]+)\s+ROWS?\b", re.IGNORECASE
)
_FETCH = re.compile(
    r"\s*FETCH\s+(?:FIRST|NEXT)\s+(\d+|@[A-Za-z0-9_@#$]+)\s+ROWS?\s+ONLY\b",
    re.IGNORECASE,
)
_DIRECTION = re.compile(r"\s*(ASC|DESC)\b", re.IGNORECASE)

# Words that end one ORDER BY item. ASC and DESC belong to the item; the rest
# begin whatever follows, and stopping on them is what lets a clause that
# cannot be served be refused by name rather than parsed as an expression.
# All are reserved, so a column called any of them arrives bracketed and is
# skipped before this is consulted.
_ORDER_ITEM_ENDS = frozenset({
    "ASC", "DESC", "OFFSET", "FOR", "OPTION",
    "GROUP", "HAVING", "WHERE", "UNION", "INTERSECT", "EXCEPT", "INTO",
})
# The same reader serves a GROUP BY entry, which ends at a different set of
# words: there is no direction on one, and an ORDER BY may follow it.
_GROUP_ITEM_ENDS = frozenset({
    "ORDER", "HAVING", "OFFSET", "FOR", "OPTION",
    "UNION", "INTERSECT", "EXCEPT", "INTO",
})
_AS = re.compile(r"\s*AS\s+", re.IGNORECASE)
_JOIN = re.compile(
    r"\s*(?:(INNER|LEFT|RIGHT|FULL|CROSS)\s+(?:OUTER\s+)?)?JOIN\s+",
    re.IGNORECASE,
)
# A second table listed after a comma, which is a cross join written the way
# it was written before JOIN existed. A bracket counts: the second table may
# be written out rather than named, and without it here the comma form broke
# out of the join reader and the rest of the FROM reached the clause check,
# which refused the whole query over a comma.
_ANOTHER_TABLE = re.compile(r"\s*,\s*(?=[A-Za-z_\[\"#@(])")
_ON = re.compile(r"\s*ON\s+", re.IGNORECASE)
_CROSS_APPLY = re.compile(r"\s*(CROSS|OUTER)\s+APPLY\s*\(", re.IGNORECASE)
_VALUES = re.compile(r"\s*VALUES\s*", re.IGNORECASE)
_SET_OPERATOR = re.compile(
    r"\s*(UNION\s+ALL|UNION|EXCEPT|INTERSECT)\s+", re.IGNORECASE
)

# The joins this can perform. RIGHT and FULL are refused rather than
# approximated: a client given the wrong rows has no way to notice.
JOIN_KINDS = frozenset({"INNER", "LEFT", "RIGHT", "FULL", "CROSS"})

# Each of these collapses a set of rows to one value: the whole table
# without a GROUP BY, or one group with it. Defined with the expressions,
# because a function call has to be recognised as an aggregate before
# anything can say whether its name is known.
AGGREGATES = AGGREGATE_NAMES
# ALL before an aggregate's argument, which changes nothing.
_ALL_OF_THEM = re.compile(r"ALL\s+(?=[^\s)])", re.IGNORECASE)

# An aggregate that takes more than the values it reduces: STRING_AGG is told
# what to put between them, and may be told what order to put them in. Read
# apart from the others because the others take one thing and stop.
WIDE_AGGREGATES = frozenset({"STRING_AGG"})

_WITHIN_GROUP = re.compile(r"\s*WITHIN\s+GROUP\s*\(", re.IGNORECASE)

# Words that end a select-list item rather than alias it. Without this a
# bare FROM would be read as the alias of the column before it.
_NOT_ALIASES = frozenset({
    "FROM", "WHERE", "ORDER", "GROUP", "HAVING", "AS", "JOIN", "INNER",
    "LEFT", "RIGHT", "FULL", "CROSS", "ON", "OFFSET", "UNION", "EXCEPT",
    "INTERSECT", "FOR", "OPTION", "COMPUTE", "PIVOT", "UNPIVOT", "APPLY",
    "WITH", "GO",
})


# What SQL Server allows an identifier to be. Longer than this is refused
# there with a message naming the first 128 characters, and refusing it here
# too is what stops a select list without commas from arriving as one column
# whose name is the rest of the statement.
MAX_IDENTIFIER_CHARS = 128


class SqlError(Exception):
    """A statement this project cannot answer.

    Carries a number where the complaint is one SQL Server has of its own,
    which happens when reading an expression fails for a reason it names.
    None means this project's own complaint, and the caller picks. The state
    is the one a real server sends with that number, which is 1 for nearly
    everything and 2 for a variable nothing declared.
    """

    def __init__(self, message: str, *, number: int | None = None,
                 state: int = 1) -> None:
        super().__init__(message)
        self.number = number
        self.state = state


def _as_written(exc: PredicateError, framed: str) -> SqlError:
    """The complaint about an expression, said the way it should be said.

    One SQL Server names is passed through as it stands: it already says
    what is wrong in the words a person will search for, and "cannot read
    'DATEPART(fortnight, x)' in the select list: 'fortnight' is not a
    recognized datepart option" says it twice. Anything else is this
    project's own and gets the frame that says where it was.
    """
    if getattr(exc, "number", None):
        return SqlError(str(exc), number=exc.number)
    return SqlError(framed)


def _checked(name: str) -> str:
    """One identifier, or the complaint SQL Server makes about its length."""
    if len(name) > MAX_IDENTIFIER_CHARS:
        raise SqlError(
            f"the identifier that starts with "
            f"'{name[:MAX_IDENTIFIER_CHARS]}' is too long. Maximum length is "
            f"{MAX_IDENTIFIER_CHARS}."
        )
    return name


@dataclass(frozen=True)
class SelectItem:
    """One entry in the select list.

    A plain column has an expression and no function. COUNT(*) has a function
    and no expression, because there is no column to name.
    """

    expression: str | None = None
    function: str | None = None
    alias: str | None = None
    # A parsed expression, when the entry is more than a column reference.
    node: object = None
    star: bool = False
    # An aggregate over something that has to be worked out per row, and
    # whether it counts each value once.
    argument: object = None
    distinct: bool = False
    # Set where the entry is a function over a window rather than over the
    # row or over a group. Everything else about the entry is then its.
    window: object = None
    # STRING_AGG's second argument, and the order it was told to run the
    # values together in.
    separator: object = None
    within: tuple = ()

    @property
    def is_aggregate(self) -> bool:
        """Whether this reduces the rows to one.

        A window function does not: it answers once per row, however much it
        reads to do it, so SELECT id, COUNT(*) OVER () is not grouped.
        """
        return self.function is not None and self.window is None

    @property
    def is_window(self) -> bool:
        return self.window is not None

    @property
    def is_computed(self) -> bool:
        """Whether this has to be worked out per row rather than projected."""
        return self.node is not None

    @property
    def output_name(self) -> str:
        """What the client sees as the column name.

        SQL Server leaves an un-aliased aggregate unnamed, and clients render
        that as a blank heading, so an empty string is the faithful answer
        rather than an invented one.

        A qualified column is headed by its own name and not by the qualifier:
        SELECT t.name gives a column called name. Keeping the whole of it
        made every heading in a joined query wrong by a prefix, and a client
        looking for one by name found nothing there.
        """
        if self.alias:
            return self.alias
        if self.is_aggregate or self.is_computed:
            # SQL Server leaves both unnamed. A client renders that as a blank
            # heading, which is the faithful answer rather than an invented
            # one, and a query that wants a name says AS.
            return ""
        return (self.expression or "").rsplit(".", 1)[-1]


@dataclass(frozen=True)
class Subquery:
    """A SELECT that stands where a value or a set of them was expected."""

    parameter: str
    sql: str
    kind: str          # "in", "exists" or "scalar"


@dataclass(frozen=True)
class Apply:
    """A table written out in the query, joined to each row of another.

    CROSS APPLY (VALUES (...), (...)) t(a, b), or CROSS APPLY (SELECT ...) t.
    Either may name the columns of the row it is applied to, which is what
    makes it an APPLY rather than a join: it is worked out again for every
    row.
    """

    alias: str
    columns: tuple = ()
    rows: tuple = ()       # one tuple of expressions per row
    # The other form: a select run again for every row, rather than values
    # worked out again for every row. Its columns are its own.
    sql: str | None = None
    # OUTER APPLY keeps the row that the select answered nothing for.
    keep_unmatched: bool = False


@dataclass(frozen=True)
class Join:
    """One joined table, and the condition that matches its rows."""

    table: str
    schema: str | None = None
    alias: str | None = None
    kind: str = "INNER"
    on: object | None = None
    # A joined table may be written in brackets rather than named, in
    # either of the two forms a FROM takes: a derived SELECT, or a table
    # written out with VALUES whose alias names its columns.
    derived: object = None
    values_rows: tuple = ()
    values_columns: tuple = ()

    @property
    def name(self) -> str:
        """What its columns are qualified by."""
        return self.alias or self.table


@dataclass(frozen=True)
class OrderKey:
    """One item of an ORDER BY, and which way it runs.

    A name, a position in the select list, or an expression worked out per
    row. SQL Server takes all three and clients write all three: Excel sorts
    by the alias it just declared, and a report orders by a CASE.
    """

    column: str
    descending: bool = False
    # Parsed, when the item is more than a name.
    node: object = None
    # A position in the select list, when the item is a bare number.
    position: int | None = None


@dataclass(frozen=True)
class Select:
    """A parsed read of one table."""

    table: str
    schema: str | None = None
    alias: str | None = None
    derived: object = None                  # a SELECT used as the table
    # The other bracketed form a FROM can take: a table written out with
    # VALUES. Its columns have no names of their own, so the alias gives
    # them some, and the rows are expressions until something works them out.
    values_rows: tuple = ()
    values_columns: tuple = ()
    ctes: tuple = ()                        # (name, SELECT) from a WITH
    subqueries: tuple[Subquery, ...] = ()
    joins: tuple[Join, ...] = ()
    applies: tuple = ()
    items: tuple[SelectItem, ...] | None = None   # None means every column
    distinct: bool = False
    top: int | None = None
    top_parameter: str | None = None
    # PERCENT makes the number a share of the rows rather than a count of
    # them, and WITH TIES keeps whatever ties with the last one taken.
    top_share: bool = False
    top_ties: bool = False   # TOP (@n), resolved at execution
    where: object | None = None
    group_by: tuple[str, ...] = ()
    having: object | None = None
    order_by: tuple[OrderKey, ...] = ()
    offset: int = 0
    fetch: int | None = None
    # (operator, SELECT) for each part after the first, when the statement
    # combines several. The last part carries the ORDER BY for all of them,
    # which is where SQL Server requires it to be written.
    combine: tuple = ()

    @property
    def is_grouped(self) -> bool:
        return bool(self.group_by)

    @property
    def is_star(self) -> bool:
        return self.items is None

    @property
    def columns(self) -> list[str] | None:
        """The plain column names, or None for a star.

        Only meaningful when nothing is aggregated; the aggregate path reads
        items directly.
        """
        if self.items is None:
            return None
        return [item.expression or "" for item in self.items]

    @property
    def has_aggregates(self) -> bool:
        """Whether anything here reduces the rows to one.

        An entry that is an aggregate, and one that computes over aggregates:
        SUM(a) / COUNT(*) is not itself an aggregate and still means the rows
        are being reduced.
        """
        return bool(self.items) and any(
            item.is_aggregate or aggregates_in(item.node) for item in self.items
        )

    @property
    def is_projection(self) -> bool:
        """Whether every entry is a plain column, which projects by position.

        A window function is not one, however much it looks like a name with
        brackets after it: it has to be worked out over all the rows.
        """
        return self.items is not None and all(
            not item.is_computed and not item.star and not item.is_window
            for item in self.items
        )

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.table}" if self.schema else self.table

    def row_limit(self, parameters: dict | None = None) -> int | None:
        """How many rows to return, resolving TOP (@n) against the parameters.

        A share rather than a count where the query said PERCENT; how many
        rows that is depends on how many there are, so _page works it out.

        What is bound to it has to be a whole number and not a negative one;
        see _row_count. Text used to reach int() and come back as a
        ValueError nobody had written a message for, and a negative silently
        returned every row.
        """
        if self.top_parameter is None:
            return self.top
        wanted = self.top_parameter.lstrip("@").lower()
        for key, value in (parameters or {}).items():
            if key.lstrip("@").lower() == wanted:
                return None if value is None else _row_count(value)
        raise SqlError(f"TOP refers to {self.top_parameter}, which was not supplied")


def _row_count(value: object) -> int:
    """A value bound to TOP or FETCH, as the count of rows it stands for.

    Measured. A real server wants an integer and says msg 1060 for anything
    else, including a float that happens to be whole and text that happens
    to read as a number, because it goes by the declared type. This goes by
    the value, which takes more than SQL Server does and refuses everything
    it could not have meant: 'ada' used to reach int() and come back as a
    ValueError nobody had written a message for.

    Below zero is msg 127 and its own sentence. It used to be taken as a
    slice bound, where -1 means all but the last row and every row came
    back but one, which is a wrong answer given quietly.
    """
    if isinstance(value, bool):
        whole = None
    elif isinstance(value, int):
        whole = value
    elif isinstance(value, float):
        whole = int(value) if value.is_integer() else None
    elif isinstance(value, str):
        text = value.strip()
        try:
            whole = int(text)
        except ValueError:
            whole = None
    else:
        whole = None

    if whole is None:
        raise SqlError(
            "The number of rows provided for a TOP or FETCH clauses row "
            "count parameter must be an integer.",
            number=ROW_COUNT_MUST_BE_WHOLE,
        )
    if whole < 0:
        raise SqlError(
            "A TOP N or FETCH rowcount value may not be negative.",
            number=ROW_COUNT_CANNOT_BE_NEGATIVE,
        )
    return whole


def _read_identifier(text: str, at: int) -> tuple[str, int]:  # noqa: D401
    match = _IDENTIFIER.match(text, at)
    if not match:
        raise SqlError(f"expected a name at {text[at:at + 20]!r}")
    if match.group("bracketed") is not None:
        return match.group("bracketed").replace("]]", "]"), match.end()
    if match.group("quoted") is not None:
        return match.group("quoted").replace('""', '"'), match.end()
    return match.group("bare"), match.end()


def _read_qualified_name(text: str, at: int) -> tuple[str | None, str, int]:
    """Read up to database.schema.table, keeping the schema and the table.

    The schema cannot be discarded the way the database can:
    INFORMATION_SCHEMA.TABLES and dbo.TABLES are different tables, and a
    catalog view would otherwise be indistinguishable from a user table that
    happens to be called TABLES.
    """
    collected = [_read_identifier(text, at)[0]]
    at = _read_identifier(text, at)[1]
    while at < len(text) and text[at] == ".":
        if len(collected) >= 3:
            raise SqlError("a table name has at most three parts")
        name, at = _read_identifier(text, at + 1)
        collected.append(name)

    table = collected[-1]
    schema = collected[-2] if len(collected) >= 2 else None
    return schema, table, at


def _read_reference(text: str, at: int) -> tuple[str, int]:
    """A column reference, qualifier and all.

    Unlike a table name, the qualifier is part of what identifies a column
    once a join is involved: u.id and o.id are two columns, and reading both
    as id answers with whichever came first.
    """
    parts = [_read_identifier(text, at)[0]]
    at = _read_identifier(text, at)[1]
    while at < len(text) and text[at] == ".":
        name, at = _read_identifier(text, at + 1)
        parts.append(name)
    # Only the last two can name a column; anything before is schema or
    # database and cannot help tell two columns apart.
    return ".".join(parts[-2:]), at


# What ends a clause: the clauses that can follow it, and the operator that
# ends the whole select. UNION is not a clause but it stops one all the same,
# because everything after it belongs to the select on the other side, and a
# WHERE handed the rest of the statement reads UNION as part of its
# condition and cannot make sense of it.
# OPTION is a hint rather than a clause, and it ends one all the same: a
# WHERE handed the rest of the statement reads it as part of its condition.
_QUERY_HINT = re.compile(r"\s*OPTION\s*(?=\()", re.IGNORECASE)
_ENDS_A_CLAUSE = (_ORDER_BY, _GROUP_BY, _HAVING, _OFFSET, _SET_OPERATOR,
                  _QUERY_HINT)
# What ends the last condition of a statement: no clause of the select can
# follow it, so only the tail and the hint can.
_ENDS_THE_LAST_CONDITION = (_ORDER_BY, _OFFSET, _SET_OPERATOR, _QUERY_HINT)


def _find_order_by(text: str, start: int, *, ends=None) -> int | None:
    """Where the next top-level clause begins, or None.

    Scanned rather than matched with one expression, because a string literal
    or a bracketed name can contain the words and must not be split on them:
    WHERE note = 'order by tuesday' is a condition, not two clauses.

    Only at the top level, for the same reason. A subquery brings its own
    clauses and they end it rather than the statement around it: the ON of a
    join whose condition holds a subquery ran to that subquery's WHERE, and
    what came out was half a condition with a bracket still open.
    """
    global _CLAUSE_ENDS
    _CLAUSE_ENDS = ends or _ENDS_A_CLAUSE
    at = start
    depth = 0
    while at < len(text):
        char = text[at]
        if char == "'":
            at += 1
            while at < len(text):
                if text[at] == "'":
                    if text[at + 1:at + 2] == "'":
                        at += 2
                        continue
                    break
                at += 1
            at += 1
            continue
        if char == "[":
            at = text.find("]", at)
            if at == -1:
                return None
            at += 1
            continue
        if char == '"':
            at = text.find('"', at + 1)
            if at == -1:
                return None
            at += 1
            continue
        if char == "(":
            depth += 1
            at += 1
            continue
        if char == ")":
            depth = max(0, depth - 1)
            at += 1
            continue
        boundary = at == start or text[at - 1].isspace() or text[at - 1] == ")"
        if depth == 0 and boundary and any(
            pattern.match(text, at) for pattern in _CLAUSE_ENDS
        ):
            return at
        at += 1
    return None


def _unbracketed(text: str) -> str:
    """A select written inside brackets, with the brackets taken off.

    A client that combines two selects often writes each in its own pair,
    and each part of that combination is read on its own here, so the
    brackets arrive at the front of a statement rather than in the middle of
    one. Taking them off leaves what follows them in place, because
    (SELECT ...) UNION (SELECT ...) ORDER BY id ends in a clause belonging
    to the whole statement rather than to the part it sits beside.

    Anything else in brackets is left alone, to be refused further down by
    whoever knows what it was meant to be, which is why the peeling only
    counts once it has reached a select.
    """
    peeled = text
    while peeled.startswith("("):
        try:
            inside, after = _read_bracketed(peeled, 0)
        except SqlError:
            return text
        rest = peeled[after:].strip()
        peeled = f"{inside} {rest}".strip() if rest else inside
        if _SELECT.match(peeled) or _WITH.match(peeled):
            return peeled
    return text


def _read_tail(text: str, at: int, subqueries: list):
    """The clauses that come after the rows are decided.

    ORDER BY, OFFSET and FETCH, and any set operator combining this statement
    with the next. Shared by the ordinary path and by a SELECT with no FROM,
    because the last part of a UNION may well be one.
    """
    order_by: tuple[OrderKey, ...] = ()
    if _ORDER_BY.match(text, at):
        order_by, at, sorted_by = _read_order_by(text, at, len(subqueries))
        subqueries.extend(sorted_by)

    offset, fetch, at = _read_offset_fetch(text, at, bool(order_by))

    combine: tuple = ()
    operator = _SET_OPERATOR.match(text, at)
    if operator:
        if order_by:
            # SQL Server takes one ORDER BY for the whole statement, written
            # at the end. One in the middle would order rows that are about
            # to be combined and reordered, which cannot mean anything.
            raise SqlError(
                f"an ORDER BY belongs after the last "
                f"{' '.join(operator.group(1).upper().split())}, not before it"
            )
        kind = " ".join(operator.group(1).upper().split())
        combine = ((kind, parse_select(text[operator.end():])),)
        at = len(text)
    return order_by, offset, fetch, combine, at


def _read_order_by(text: str, at: int, start: int = 0, ends=_ORDER_ITEM_ENDS):
    """Every ORDER BY item, where the clause ended, and what it lifted."""
    match = _ORDER_BY.match(text, at)
    if not match:
        raise SqlError("expected ORDER BY")
    at = match.end()

    keys: list[OrderKey] = []
    subqueries: list[Subquery] = []
    while True:
        body, at = _read_order_item(text, at, ends)
        if not body:
            raise SqlError("ORDER BY needs a column, a position or an expression")
        direction = _DIRECTION.match(text, at)
        descending = False
        if direction:
            descending = direction.group(1).upper() == "DESC"
            at = direction.end()
        body, lifted = _lift_subqueries(body, start + len(subqueries))
        subqueries.extend(lifted)
        keys.append(_order_key(body, descending))
        at = _skip_space(text, at)
        if text[at:at + 1] == ",":
            at = _skip_space(text, at + 1)
            continue
        break
    return tuple(keys), at, tuple(subqueries)


def _read_order_item(text: str, at: int, ends=_ORDER_ITEM_ENDS) -> tuple[str, int]:
    """Everything up to the comma or the clause that ends this item.

    ends says which words finish one, because an ORDER BY entry and a GROUP
    BY entry are the same shape and stop at different places.
    """
    start = _skip_space(text, at)
    at = start
    depth = 0
    cases = 0
    while at < len(text):
        char = text[at]
        if char == "'":
            at = skip_quoted(text, at, "'")
            continue
        if char == "[":
            found = text.find("]", at)
            at = len(text) if found < 0 else found + 1
            continue
        if char == '"':
            at = skip_quoted(text, at, '"')
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0:
            word = re.compile(r"[A-Za-z_][A-Za-z0-9_]*").match(text, at)
            if word:
                upper = word.group(0).upper()
                if upper == "CASE":
                    cases += 1
                elif upper == "END":
                    cases -= 1
                elif cases == 0 and upper in ends:
                    break
                at = word.end()
                continue
            if char == "," and cases == 0:
                break
        at += 1
    return text[start:at].strip(), at


def _order_key(body: str, descending: bool) -> OrderKey:
    """One ORDER BY item read as a position, a name, or an expression.

    A bare number is a position in the select list rather than the number
    itself, which is what SQL Server does and what a client generating
    ORDER BY 2 means.
    """
    if body.isdigit():
        return OrderKey(column=body, descending=descending, position=int(body))

    if body.startswith(_SUBQUERY_NAME):
        # A subquery already lifted out of this item. It reads as an
        # identifier, so without this it would be looked for among the
        # columns and not found.
        #
        # Guarded like the branch below, because the item can be a subquery
        # with something after it that belongs to nothing: ORDER BY (SELECT
        # ...) name. That reached the client as an internal error rather than
        # as a refusal, which is how a truncated query showed up.
        try:
            node = parse_expression(body)
        except PredicateError as exc:
            raise _as_written(
                exc, f"cannot read {body!r} in the ORDER BY: {exc}"
            ) from exc
        return OrderKey(column=body, descending=descending, node=node)

    try:
        name, consumed = _read_reference(body, 0)
    except SqlError:
        name, consumed = "", -1
    if consumed >= 0 and _skip_space(body, consumed) == len(body):
        return OrderKey(column=name, descending=descending)

    try:
        node = parse_expression(body)
    except PredicateError as exc:
        if getattr(exc, "number", None) == ONLY_IN_SELECT_OR_ORDER_BY:
            # SQL Server does take one here. This does not, and saying that
            # windows belong in the ORDER BY while refusing one in the ORDER
            # BY would be no help at all.
            raise SqlError(
                "a window function in the ORDER BY is not supported; name "
                "it in the select list and order by that name"
            ) from exc
        raise _as_written(
            exc, f"cannot read {body!r} in the ORDER BY: {exc}"
        ) from exc
    return OrderKey(column=body, descending=descending, node=node)


def _read_select_item(text: str, at: int, start: int = 0):
    """One select-list entry: a star, a column, an aggregate or an expression.

    Returns the entry, where it ended, and any subqueries lifted out of it.
    """
    probe = _skip_space(text, at)
    if text[probe:probe + 1] == "*" and not _continues_expression(text, probe + 1):
        return SelectItem(star=True), probe + 1, []

    qualified = _QUALIFIED_STAR.match(text, probe)
    if qualified:
        # t.* is every column of t and nothing else, which is how a query
        # reads one side of a join or an applied table.
        return (SelectItem(star=True, expression=_bare(qualified.group(1))),
                qualified.end(), [])

    if _starts_expression(text, probe):
        return _read_expression_item(text, at, start)

    # A call, whether or not it is written with where it lives in front of
    # it: msdb.dbo.fn_syspolicy_is_automation_enabled() is one function, and
    # reading the name as a column stops at the bracket and cannot go on.
    call = re.compile(
        r"\s*(?:[A-Za-z_][A-Za-z0-9_]*\s*\.\s*){0,2}([A-Za-z_][A-Za-z0-9_]*)\s*\("
    ).match(text, at)
    if call and call.group(1).upper() in WIDE_AGGREGATES:
        return _read_wide_aggregate(text, call, probe, start)

    if (call and call.group(1).upper() not in AGGREGATES
            and call.group(1).upper() not in WINDOW_FUNCTIONS):
        # Not an aggregate and not a window function, so the whole entry is
        # an expression.
        return _read_expression_item(text, at, start)

    if call and call.group(1).upper() in WINDOW_FUNCTIONS:
        function = call.group(1).upper()
        written, at = _read_window_arguments(text, call.end(), function)
        if text[at:at + 1] != ")":
            raise SqlError(f"{function}( was opened and not closed")
        at += 1
        window, at = _read_over(text, at, function, written)
        alias, at = _read_alias(text, at)
        return SelectItem(expression=function, alias=alias, window=window), at, []

    function = None
    expression = None
    argument = None
    distinct = False
    if call and call.group(1).upper() in AGGREGATES:
        function = call.group(1).upper()
        at = _skip_space(text, call.end())

        inner = _DISTINCT.match(text, at)
        if inner:
            distinct = True
            at = inner.end()
        else:
            # ALL is the default written out. A person writes it beside a
            # DISTINCT elsewhere in the same statement, for symmetry.
            every = _ALL_OF_THEM.match(text, at)
            if every:
                at = every.end()

        if text[at:at + 1] == "*":
            if function not in COUNTS:
                raise SqlError(f"{function}(*) is not a thing; {function} needs a column")
            if distinct:
                raise SqlError(f"{function}(DISTINCT *) is not a thing")
            at = _skip_space(text, at + 1)
        else:
            body, at = _read_aggregate_argument(text, at)
            try:
                node = parse_expression(body)
            except PredicateError as exc:
                raise SqlError(
                    f"cannot read {body!r} inside {function}(): {exc}"
                ) from exc
            # A bare column keeps the fast path; anything else is worked out
            # per row before the values are reduced. The name comes from the
            # parsed reference rather than the text, so COUNT([a.b]) looks up
            # a.b rather than a column called "[a.b]".
            if isinstance(node, ColumnRef):
                argument = None
                expression = node.qualified or node.name
            else:
                argument = node
                expression = body
            at = _skip_space(text, at)
        if text[at:at + 1] != ")":
            raise SqlError(f"{function}( was opened and not closed")
        at += 1
        if _OVER.match(text, at):
            # An aggregate over a window answers once per row rather than
            # reducing the rows, which is a different thing wearing the same
            # name.
            if distinct:
                raise SqlError(
                    "Use of DISTINCT is not allowed with the OVER clause.",
                    number=NO_DISTINCT_OVER,
                )
            window, at = _read_over(text, at, function, (),
                                    argument=expression, node=argument)
            alias, at = _read_alias(text, at)
            return SelectItem(expression=expression, function=function,
                              alias=alias, argument=argument,
                              window=window), at, []
        if _continues_expression(text, _skip_space(text, at)):
            # The aggregate is part of a larger value rather than the whole
            # entry: SUM(a) / COUNT(*), MAX(a) - MIN(a). Read again from the
            # start of the entry, as one expression.
            return _read_expression_item(text, probe, start)
    elif call:
        raise SqlError(
            f"'{call.group(1)}' is not a function this server knows; it has "
            f"{', '.join(sorted(AGGREGATES))}"
        )
    else:
        start_of_item = _skip_space(text, at)
        expression, at = _read_reference(text, at)
        after = _skip_space(text, at)
        if _continues_expression(text, after):
            return _read_expression_item(text, start_of_item, start)

    alias, at = _read_alias(text, at)
    return SelectItem(expression=expression, function=function, alias=alias,
                      argument=argument, distinct=distinct), at, []


# Every one of those needs to be told the order to work in; an aggregate does
# not, and means the whole partition when it is not told. Measured: LAG with
# an empty OVER is refused the same way ROW_NUMBER is.
_NEEDS_AN_ORDER = WINDOW_FUNCTIONS

# The two that read a row of the window may be told which rows to look at.
# Ranking cannot: its answer is about the whole window by construction.
_TAKES_A_FRAME = frozenset({"FIRST_VALUE", "LAST_VALUE"})

_OVER = re.compile(r"\s*OVER\s*\(", re.IGNORECASE)
_PARTITION_BY = re.compile(r"\s*PARTITION\s+BY\s+", re.IGNORECASE)
_FRAME = re.compile(r"\s*(ROWS|RANGE)\b", re.IGNORECASE)
_BETWEEN = re.compile(r"\s*BETWEEN\b", re.IGNORECASE)
_AND_THEN = re.compile(r"\s*AND\b", re.IGNORECASE)
_BOUND = re.compile(
    r"\s*(?:(UNBOUNDED)\s+(PRECEDING|FOLLOWING)"
    r"|(CURRENT)\s+ROW"
    r"|(\d+)\s+(PRECEDING|FOLLOWING))",
    re.IGNORECASE,
)

# What SQL Server calls a frame written wrongly, or asked of a function that
# cannot have one.
NO_FRAME_HERE = 10752
FRAME_BACKWARDS = 4193
RANGE_TAKES_NO_NUMBER = 4194
# A window argument ends at its comma or at the closing bracket, both of
# which _read_order_item stops on when nothing is nested.
_NOTHING_ENDS_IT = frozenset()
# Inside an OVER clause a frame may follow the order, and the words that
# start one are not part of the last key.
_WINDOW_ORDER_ENDS = _ORDER_ITEM_ENDS | {"ROWS", "RANGE"}
_WINDOW_PARTITION_ENDS = _GROUP_ITEM_ENDS | {"ROWS", "RANGE"}

@dataclass(frozen=True)
class Frame:
    """How much of the window one row sees.

    kind is ROWS, which counts rows, or RANGE, which counts them by what
    they tie on. start and end are each ("UNBOUNDED",), ("CURRENT",), or a
    direction and a distance: ("PRECEDING", 2).

    The frame a query does not write is RANGE from UNBOUNDED PRECEDING to
    CURRENT ROW when the OVER clause says an order, and the whole partition
    when it does not; window.py keeps that default rather than this.
    """

    kind: str
    start: tuple
    end: tuple


@dataclass(frozen=True)
class Window:
    """A function applied over a window of rows rather than to one row.

    argument is what it reduces or reads, written as the query wrote it, and
    node is that parsed when it is more than a column. arguments carries what
    follows it: NTILE's count, LAG's offset and its default.

    partition_by and order_by are kept as written and as order keys, the same
    shapes a GROUP BY and an ORDER BY are kept in, because that is what they
    are: the window is a group, ordered.
    """

    function: str
    argument: str | None = None
    node: object = None
    arguments: tuple = ()
    partition_by: tuple = ()
    order_by: tuple = ()
    frame: object = None


# What can follow a value and mean the entry is not finished.
_OPERATOR_AHEAD = re.compile(r"[-+*/%]|\|\|")

# What can begin an entry that is not a column reference: a literal, a
# bracketed sub-expression, a leading sign, or a word that opens a construct.
_QUALIFIED_STAR = re.compile(
    r"((?:\[[^\]]*\])|(?:\"[^\"]*\")|(?:[A-Za-z_][A-Za-z0-9_]*))\s*\.\s*\*"
)
# What can begin an entry that is not a column reference. A name starting
# with @ is among them: no column can be called that, so SELECT @p is a value
# and not a table this has never heard of.
_LITERAL_AHEAD = re.compile(r"""[-+(]|\d|N?'|@""", re.VERBOSE)
_EXPRESSION_WORDS = frozenset({"CASE", "CAST", "CONVERT", "NULL"})


def _starts_expression(text: str, at: int) -> bool:
    if _LITERAL_AHEAD.match(text, at):
        return True
    word = re.compile(r"[A-Za-z_][A-Za-z0-9_]*").match(text, at)
    return bool(word) and word.group(0).upper() in _EXPRESSION_WORDS


def _continues_expression(text: str, at: int) -> bool:
    at = _skip_space(text, at)
    return bool(_OPERATOR_AHEAD.match(text, at))


def _read_aggregate_argument(text: str, at: int) -> tuple[str, int]:
    """Everything up to the bracket that closes an aggregate."""
    start = _skip_space(text, at)
    at = start
    depth = 0
    while at < len(text):
        char = text[at]
        if char in "'\"":
            at = skip_quoted(text, at, char)
            continue
        if char == "[":
            found = text.find("]", at)
            at = len(text) if found < 0 else found + 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                break
            depth -= 1
        at += 1
    return text[start:at].strip(), at


def _read_wide_aggregate(text: str, call, probe: int, start: int):
    """STRING_AGG(value, separator) WITHIN GROUP (ORDER BY ...).

    The separator is kept as written and worked out once, since it is the
    same for every row; the order is kept the way an ORDER BY is kept,
    because that is what it is.
    """
    function = call.group(1).upper()
    at = _skip_space(text, call.end())
    body, at = _read_order_item(text, at, _NOTHING_ENDS_IT)
    if not body:
        raise SqlError(f"{function}() needs something to run together")
    if text[_skip_space(text, at):_skip_space(text, at) + 1] != ",":
        raise SqlError(
            f"{function}() needs a separator to put between the values"
        )
    at = _skip_space(text, _skip_space(text, at) + 1)
    written, at = _read_order_item(text, at, _NOTHING_ENDS_IT)
    if text[at:at + 1] != ")":
        raise SqlError(f"{function}( was opened and not closed")
    at += 1

    within: tuple = ()
    match = _WITHIN_GROUP.match(text, at)
    if match:
        if not _ORDER_BY.match(text, match.end()):
            raise SqlError("WITHIN GROUP needs an ORDER BY")
        within, at, _ = _read_order_by(text, match.end())
        at = _skip_space(text, at)
        if text[at:at + 1] != ")":
            raise SqlError("WITHIN GROUP( was opened and not closed")
        at += 1

    try:
        inner = parse_expression(body)
        separator = parse_expression(written)
    except PredicateError as exc:
        raise _as_written(
            exc, f"cannot read {body!r} inside {function}(): {exc}"
        ) from exc

    # A bare column keeps the fast path, the way the other aggregates do.
    argument = None if isinstance(inner, ColumnRef) else inner
    named = (inner.qualified or inner.name) if argument is None else body
    alias, at = _read_alias(text, at)
    return SelectItem(expression=named, function=function, alias=alias,
                      argument=argument, separator=separator,
                      within=within), at, []


def _read_window_arguments(text: str, at: int, function: str) -> tuple:
    """The arguments of a window function, of which there may be none.

    ROW_NUMBER takes none, NTILE takes a count, LAG takes what to read and
    optionally how far back and what to answer at the edge.
    """
    written: list[str] = []
    at = _skip_space(text, at)
    if text[at:at + 1] == ")":
        return (), at
    while True:
        body, at = _read_order_item(text, at, _NOTHING_ENDS_IT)
        if not body:
            raise SqlError(f"{function}() was given an empty argument")
        written.append(body)
        at = _skip_space(text, at)
        if text[at:at + 1] == ",":
            at = _skip_space(text, at + 1)
            continue
        break
    return tuple(written), at


def _read_over(text: str, at: int, function: str, arguments: tuple,
               argument: str | None = None, node: object = None):
    """The OVER clause, which says what the window is.

    A frame is refused rather than ignored: it changes which rows are in the
    window, and answering as though it were not there would be a different
    number reported as the one asked for.
    """
    over = _OVER.match(text, at)
    if not over:
        raise SqlError(
            f"The function '{function}' must have an OVER clause.",
            number=NEEDS_AN_OVER_CLAUSE,
        )
    at = over.end()

    partition_by: tuple = ()
    match = _PARTITION_BY.match(text, at)
    if match:
        partition_by, at = _read_partition_by(text, match.end())

    order_by: tuple = ()
    if _ORDER_BY.match(text, at):
        order_by, at, _ = _read_order_by(text, at, ends=_WINDOW_ORDER_ENDS)

    at = _skip_space(text, at)
    frame = None
    if _FRAME.match(text, at):
        if not order_by:
            # A frame counts from somewhere, and without an order there is
            # nowhere to count from. SQL Server calls it a syntax error.
            raise SqlError("Incorrect syntax near 'ROWS'.",
                           number=SYNTAX_ERROR)
        if function in WINDOW_FUNCTIONS - _TAKES_A_FRAME:
            raise SqlError(
                f"The function '{function}' may not have a window frame.",
                number=NO_FRAME_HERE,
            )
        frame, at = _read_frame(text, at)
    if text[at:at + 1] != ")":
        raise SqlError("OVER( was opened and not closed")
    at += 1

    if not order_by and function in _NEEDS_AN_ORDER:
        raise SqlError(
            f"The function '{function}' must have an OVER clause with "
            f"ORDER BY.",
            number=NEEDS_AN_ORDER_BY,
        )
    return Window(function=function, argument=argument, node=node,
                  arguments=arguments, partition_by=partition_by,
                  order_by=order_by, frame=frame), at


def _read_frame(text: str, at: int) -> tuple:
    """ROWS or RANGE, and the two ends of what this row sees."""
    kind = _FRAME.match(text, at)
    at = kind.end()
    written = kind.group(1).upper()

    between = _BETWEEN.match(text, at)
    if between:
        start, at = _read_bound(text, between.end(), written)
        joined = _AND_THEN.match(text, at)
        if not joined:
            raise SqlError("a window frame written BETWEEN needs an AND")
        end, at = _read_bound(text, joined.end(), written)
    else:
        # One bound on its own is where the frame starts, and it ends here.
        start, at = _read_bound(text, at, written)
        end = ("CURRENT",)

    if _backwards(start, end):
        raise SqlError(
            "'BETWEEN ... FOLLOWING AND ... PRECEDING' is not a valid window "
            "frame and cannot be used with the OVER clause.",
            number=FRAME_BACKWARDS,
        )
    return Frame(kind=written, start=start, end=end), at


def _read_bound(text: str, at: int, kind: str) -> tuple:
    """One end of a frame: unbounded, this row, or so many rows away."""
    match = _BOUND.match(text, at)
    if not match:
        raise SqlError("a window frame needs UNBOUNDED, CURRENT ROW, or a "
                       "number of rows before or after this one")
    if match.group(1):
        return ("UNBOUNDED", match.group(2).upper()), match.end()
    if match.group(3):
        return ("CURRENT",), match.end()
    if kind == "RANGE":
        raise SqlError(
            "RANGE is only supported with UNBOUNDED and CURRENT ROW window "
            "frame delimiters.",
            number=RANGE_TAKES_NO_NUMBER,
        )
    return (match.group(5).upper(), int(match.group(4))), match.end()


def _backwards(start: tuple, end: tuple) -> bool:
    """Whether a frame ends before it begins, which is no frame at all."""
    order = {("UNBOUNDED", "PRECEDING"): 0, ("CURRENT",): 2,
             ("UNBOUNDED", "FOLLOWING"): 4}
    began = order.get(start, 1 if start[0] == "PRECEDING" else 3)
    ended = order.get(end, 1 if end[0] == "PRECEDING" else 3)
    return began > ended


def _read_partition_by(text: str, at: int) -> tuple[tuple[str, ...], int]:
    """What a window is partitioned by, which is a GROUP BY of the window."""
    written: list[str] = []
    while True:
        body, at = _read_order_item(text, at, _WINDOW_PARTITION_ENDS)
        if not body:
            raise SqlError("PARTITION BY needs a column or an expression")
        written.append(body)
        at = _skip_space(text, at)
        if text[at:at + 1] == ",":
            at = _skip_space(text, at + 1)
            continue
        break
    return tuple(written), at


def _read_expression_item(text: str, at: int, start: int = 0):
    """An entry that has to be evaluated rather than projected.

    Returns the entry, where it ended, and any subqueries lifted out of it:
    SELECT (SELECT COUNT(*) FROM t) is one value standing in a select list,
    and it becomes a parameter the same way one in a WHERE does.
    """
    body, at = _read_expression_text(text, at)
    # Lifted before the alias is taken off, because deciding whether a
    # trailing word is an alias means parsing what comes before it, and a
    # subquery still written out is not something the expression parser can
    # read. CASE WHEN EXISTS (SELECT ...) THEN 1 ELSE 0 END _IS_SAAS lost
    # its alias that way and then failed on it.
    body, lifted = _lift_subqueries(body, start)
    written, alias = _split_alias(body)
    try:
        node = parse_expression(written)
    except PredicateError as exc:
        raise _as_written(
            exc, f"cannot read {written!r} in the select list: {exc}"
        ) from exc
    return SelectItem(expression=written, alias=alias, node=node), at, lifted


def _read_expression_text(text: str, at: int) -> tuple[str, int]:
    """Everything up to the top-level comma or FROM that ends this entry."""
    start = _skip_space(text, at)
    at = start
    depth = 0
    cases = 0
    while at < len(text):
        char = text[at]
        if char == "'":
            at = skip_quoted(text, at, "'")
            continue
        if char == "[":
            found = text.find("]", at)
            at = len(text) if found < 0 else found + 1
            continue
        if char == '"':
            at = skip_quoted(text, at, '"')
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0:
            word = re.compile(r"[A-Za-z_][A-Za-z0-9_]*").match(text, at)
            if word:
                upper = word.group(0).upper()
                if upper == "CASE":
                    cases += 1
                elif upper == "END":
                    cases -= 1
                elif cases == 0 and upper in _ITEM_ENDS:
                    break
                at = word.end()
                continue
            if char == "," and cases == 0:
                break
        at += 1
    return text[start:at].strip(), at


# What ends a select-list entry that is not followed by a comma. FROM is the
# usual one; the rest matter for a SELECT with no FROM at all, which is how
# the last part of a UNION can be written.
_ITEM_ENDS = frozenset({
    "FROM", "WHERE", "ORDER", "GROUP", "HAVING", "OFFSET", "FOR", "OPTION",
    "UNION", "INTERSECT", "EXCEPT", "INTO",
})


def skip_quoted(text: str, at: int, quote: str) -> int:
    """Past a quoted run, doubled quotes and all."""
    at += 1
    while at < len(text):
        if text[at] == quote:
            if text[at + 1:at + 2] == quote:
                at += 2
                continue
            return at + 1
        at += 1
    return at


def _split_alias(body: str) -> tuple[str, str | None]:
    """Take a trailing alias off an entry, if that is what it is.

    An AS only counts at the top level: CAST(id AS int) has one inside its
    brackets and is not aliased by "int". Without an AS, the last word is
    tried both ways, and an entry whose text still parses without it was
    aliased by it, which is what keeps "a + b" from losing its b.
    """
    at = _top_level_as(body)
    if at is not None:
        return body[:at].strip(), _one_name(body[at + 4:].strip())

    match = re.compile(
        r"(.*[^\s])\s+((?:\[[^\]]*\])|(?:\"[^\"]*\")|(?:'[^']*')"
        r"|(?:[A-Za-z_][A-Za-z0-9_]*))$",
        re.DOTALL,
    ).match(body)
    if not match:
        return body, None
    if match.group(2).upper() in _NOT_ALIASES:
        return body, None
    try:
        parse_expression(match.group(1).strip())
    except PredicateError:
        return body, None
    return match.group(1).strip(), _bare(match.group(2))


def _one_name(written: str) -> str:
    """The alias an AS introduces, which is one name and nothing else.

    A select list written without its commas parses as one entry aliased by
    the rest of the statement, and every name after the first disappears into
    it. SQL Server calls that incorrect syntax near the next thing it sees,
    and so does this.
    """
    match = re.compile(
        r"(?:\[(?:[^\]]|\]\])*\])|(?:\"(?:[^\"]|\"\")*\")|(?:'(?:[^']|'')*')"
        r"|(?:[A-Za-z_@#][A-Za-z0-9_@#$]*)"
    ).match(written)
    if not match:
        raise SqlError(f"incorrect syntax near {written[:20]!r}")
    rest = written[match.end():].strip()
    if rest:
        raise SqlError(f"incorrect syntax near {rest.split()[0]!r}")
    return _checked(_bare(match.group(0)))


def _top_level_as(body: str) -> int | None:
    """Where a top-level " AS " sits in an entry, or None."""
    depth = 0
    at = 0
    while at < len(body):
        char = body[at]
        if char in "'\"":
            at = skip_quoted(body, at, char)
            continue
        if char == "[":
            found = body.find("]", at)
            at = len(body) if found < 0 else found + 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and char.isspace():
            if body[at + 1:at + 4].upper() == "AS " or (
                body[at + 1:at + 3].upper() == "AS" and at + 3 == len(body)
            ):
                return at
        at += 1
    return None


def _bare(name: str) -> str:
    """A quoted name as the name itself, whichever way it was quoted.

    Single quotes included: AS 'DatabaseEngineType' is the old spelling of an
    alias and SQL Server still accepts it, naming the column without them.
    SSMS opens every connection with a query that uses it.
    """
    if name.startswith("[") and name.endswith("]"):
        return name[1:-1].replace("]]", "]")
    if name.startswith('"') and name.endswith('"'):
        return name[1:-1].replace('""', '"')
    if len(name) > 1 and name.startswith("'") and name.endswith("'"):
        return name[1:-1].replace("''", "'")
    return name


def _read_alias(text: str, at: int) -> tuple[str | None, int]:
    """An AS alias, or a bare one, or nothing.

    A bare alias is only an alias if it is not a keyword that ends the list;
    otherwise FROM becomes the alias of the last column and the statement loses
    its table.
    """
    as_match = _AS.match(text, at)
    if as_match:
        _, alias, at = _read_qualified_name(text, as_match.end())
        return alias, at

    probe = _skip_space(text, at)
    match = _IDENTIFIER.match(text, probe)
    if match:
        candidate = match.group("bare")
        if candidate is None or candidate.upper() not in _NOT_ALIASES:
            _, alias, at = _read_qualified_name(text, probe)
            return alias, at
    return None, at


def _read_select_list(text: str, at: int):
    """Every entry of a select list, and the subqueries standing inside it."""
    items: list[SelectItem] = []
    subqueries: list[Subquery] = []
    while True:
        item, at, lifted = _read_select_item(text, at, len(subqueries))
        items.append(item)
        subqueries.extend(lifted)
        at = _skip_space(text, at)
        if text[at:at + 1] == ",":
            at = _skip_space(text, at + 1)
            continue
        break
    return tuple(items), at, tuple(subqueries)


def _skip_space(text: str, at: int) -> int:
    while at < len(text) and text[at].isspace():
        at += 1
    return at


# The reserved words that ask for something after them, so that a batch can
# neither end on one nor put a semicolon straight after one. Reserved is
# what makes that safe: SELECT 1 AS <word> fails on a real server for
# exactly the reserved words, measured one at a time, so none of these can
# be a name a statement ends on. Each was measured as the last word of a
# batch, which is msg 102 near it, and twenty of them of every sort before a
# semicolon, which is 102 near ';'. SET is the one that answers differently:
# see _unfinished.
#
# Left out are the reserved words a statement can end on. Most are plain
# enough: ON, OFF, NULL, END, ASC, DESC, DEFAULT, KEY, PERCENT, RETURN,
# COMMIT, ROLLBACK, TRAN and TRANSACTION. Three are not, and each would have
# refused good T-SQL: FULL ends SET RECOVERY FULL, ALL ends NOCHECK
# CONSTRAINT ALL and FOR SYSTEM_TIME ALL, and OPTION ends WITH CHECK OPTION.
# Every word a real server lets be a name is left out as well, OFFSET, ROWS,
# PARTITION, APPLY and THROW among them: ORDER BY id OFFSET 2 ROWS really
# does end on one of those. The three statement verbs are here but not
# always: see _NAMES_AN_EVENT.
_WANTS_MORE = frozenset({
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "BY", "JOIN", "IN",
    "BETWEEN", "LIKE", "IS", "UNION", "EXCEPT", "INTERSECT", "HAVING",
    "GROUP", "ORDER", "AS", "TOP", "DISTINCT", "CASE", "WHEN", "THEN",
    "ELSE", "INTO", "SET", "DECLARE", "EXEC", "EXECUTE", "PRINT", "IF",
    "WHILE", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "WITH",
    "OVER", "CROSS", "LEFT", "RIGHT", "INNER", "OUTER", "ANY", "SOME",
    "EXISTS", "ESCAPE", "FETCH", "RAISERROR", "GOTO", "TABLE", "USE",
    "BEGIN", "VALUES", "FOR", "COLLATE", "TRUNCATE", "MERGE", "PROC",
    "PROCEDURE", "SAVE",
})

# Where INSERT, UPDATE and DELETE name an event or an action rather than
# begin a statement, and so can end one: a cursor's FOR UPDATE, measured to
# answer, a MERGE's WHEN MATCHED THEN DELETE;, and a security policy's AFTER
# INSERT or BEFORE DELETE. Anywhere else each wants more, measured: alone,
# each is msg 102 near it, and with a semicolon after it 102 near ';'.
_NAMES_AN_EVENT = frozenset({"FOR", "AFTER", "BEFORE", "THEN"})

# The marks that want an operand or a name after them, measured the same
# way. Not the star: SELECT * is a star rather than a multiplication.
_WANTS_MORE_MARKS = frozenset("=<>!+-/%&|^~,(.")

# The clause words. Each can only begin a clause, so none of them can be the
# name or the value that something before it was waiting for, and a real
# server names one found there with msg 156, "near the keyword", wherever
# that happens: measured after every token in the set below.
_ONLY_BEGINS_A_CLAUSE = frozenset({
    "FROM", "WHERE", "GROUP", "HAVING", "UNION", "EXCEPT", "INTERSECT",
})

# What waits for a name or a value, for the rule above. Narrower than
# _WANTS_MORE on purpose, because good T-SQL puts a clause word straight
# after each of the ones left out: DELETE FROM, FETCH FROM, a IS DISTINCT
# FROM b, FOR SYSTEM_TIME ALL WHERE, OPTION (ORDER GROUP) and OPTION (MERGE
# UNION). ORDER is not a clause word above for the same reason: OVER (ORDER
# BY ...) opens a bracket onto it.
_WAITS_FOR_AN_OPERAND = frozenset({
    ",", "(", "=", "<", ">", "+", "-", "/", "%", "&", "|", "^", "~", "!", ".",
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "ON", "BY", "IN",
    "BETWEEN", "LIKE", "IS", "JOIN", "CASE", "WHEN", "THEN", "ELSE", "UNION",
    "EXCEPT", "INTERSECT", "EXISTS", "HAVING", "GROUP", "TOP", "AS", "INTO",
    "COLLATE", "OVER", "ESCAPE", "CROSS", "LEFT", "RIGHT", "FULL", "INNER",
    "OUTER", "ANY", "SOME", "PRINT", "IF", "EXEC", "EXECUTE", "INSERT",
    "WITH", "DECLARE", "WHILE", "VALUES", "SET",
})

# The words a statement begins with, for finding where the one around a SET
# began. UPDATE and ALTER are not here, because a SET after either is that
# statement's own; neither is WITH, which UPDATE #t WITH (ROWLOCK) SET puts
# between an UPDATE and its SET.
_BEGINS_A_STATEMENT = frozenset({
    "SELECT", "INSERT", "DELETE", "MERGE", "DECLARE", "SET", "EXEC",
    "EXECUTE", "PRINT", "IF", "WHILE", "BEGIN", "END", "ELSE", "RETURN",
    "COMMIT", "ROLLBACK", "SAVE", "FETCH", "OPEN", "CLOSE", "DEALLOCATE",
    "TRUNCATE", "DROP", "CREATE", "USE", "GOTO", "RAISERROR", "THROW",
    "WAITFOR", "BREAK", "CONTINUE", "GRANT", "DENY", "REVOKE",
})
_SET_IS_ITS_OWN = frozenset({"UPDATE", "ALTER"})

# What a SET that begins its statement cannot have straight after it: any
# word that begins a statement, END and ELSE among them. Measured one at a
# time, SET and then each of them on the next line is msg 156 near the
# keyword 'SET', where this answered SET followed by a SELECT as though the
# SET were not there. THROW is the one left out, because it is not reserved:
# SET THROW is msg 195, THROW read as the name of an option.
_NOT_AFTER_A_SET = (_BEGINS_A_STATEMENT | _SET_IS_ITS_OWN | {"WITH"}) - {"THROW"}

# A BEGIN that opens a transaction or a conversation closes with no END.
_BEGIN_WITHOUT_AN_END = frozenset({
    "TRAN", "TRANSACTION", "DISTRIBUTED", "DIALOG", "CONVERSATION",
})

# How much of a long text a message keeps, both measured on a quote left
# open with thousands of characters after it: msg 105 stops at 2,047
# characters, the last three of them dots, and the 102 that follows it
# quotes only the first 129 characters of what it is near.
_LONGEST_MESSAGE = 2047
_LONGEST_NEAR = 129

# One token of a batch at a time, with the space before it, in the order the
# alternatives are tried. A string or a quoted name is whole only where its
# closing mark is not followed by another: the lookahead is what stops the
# pattern backing into an escaped '' and taking its first quote as the
# close, which would read 'abc'' as the string 'abc' and a quote left open
# after it. A comment that opens is left to _past_the_comment, because
# comments nest and a pattern cannot count. What none of the others takes
# is a mark of its own.
_TOKEN = re.compile(r"""
    \s*
    (?:
      (?P<line>   --[^\n]* )
    | (?P<block>  /\* )
    | (?P<string> [Nn]?'(?:[^']|'')*'(?!') )
    | (?P<name>   \[(?:[^\]]|\]\])*\](?!\]) | "(?:[^"]|"")*"(?!") )
    | (?P<open>   [Nn]?' | \[ | " )
    | (?P<word>   [^\W\d][\w@\#$]* | [@\#][\w@\#$]* )
    | (?P<number> 0[xX][0-9A-Fa-f]* | \d+\.?\d*(?:[eE][+-]?\d+)?
                  | \.\d+(?:[eE][+-]?\d+)? )
    | (?P<mark>   \S )
    )
""", re.VERBOSE)
_CLOSING_MARK = {"'": "'", "[": "]", "\"": "\""}
_NOT_A_TOKEN = frozenset({"line", "block", "open"})


@dataclass(frozen=True)
class _Lexed:
    """A batch as tokens, and what it ran off the end inside, if anything."""

    # Every token the text completes, as (kind, as written) pairs.
    tokens: list
    # The same tokens as the sets above hold them: a word in capitals and
    # anything else as written, which leaves a string its quotes and a name
    # its brackets, so that neither can pass for a keyword or a mark.
    words: list
    # "quote" where a string or a quoted name runs off the end of the batch,
    # "comment" where a comment does, and empty where nothing does.
    left_open: str = ""
    # What followed a quote left open, each doubled closing mark in it read
    # as the one mark it stands for, which is how a real server quotes it.
    rest: str = ""


def _closing(text: str, at: int, close: str) -> int:
    """Just past the mark closing a quoted run opened at `at`, or -1.

    The closing mark written twice is one of them inside the run, which is
    how a string and a bracketed name both escape it.
    """
    i = at + 1
    while i < len(text):
        if text[i] == close:
            if text[i + 1:i + 2] == close:
                i += 2
                continue
            return i + 1
        i += 1
    return -1


def _past_the_comment(text: str, at: int) -> int:
    """Just past the */ closing the comment opened at `at`, or -1.

    Comments nest: measured, SELECT 1 /* a /* b */ c */ AS v answers 1, and
    SELECT 1 /* a /* b */ AS v is msg 113.
    """
    depth = 0
    i = at
    while i < len(text):
        if text.startswith("/*", i):
            depth += 1
            i += 2
        elif text.startswith("*/", i):
            depth -= 1
            i += 2
            if not depth:
                return i
        else:
            i += 1
    return -1


def _lexed(text: str) -> _Lexed:
    """A batch read into tokens, each string and quoted name whole.

    Deliberately not the parser: this is read before anything is parsed, to
    ask only whether the text could be T-SQL at all, and it has to agree with
    itself on text no statement reader has seen.

    The walk stops at the last character that is not space. Past it the
    pattern's leading space would match and then find no token, and a search
    that fails is retried at every later place, which on a long run of
    trailing space is quadratic; a quote left open still quotes the space
    after it, because a real server does.
    """
    tokens: list = []
    words: list = []
    at, end = 0, len(text.rstrip())
    while True:
        for found in _TOKEN.finditer(text, at, end):
            kind = found.lastgroup
            written = found.group(kind)
            if kind in _NOT_A_TOKEN:
                if kind == "block":
                    at = _past_the_comment(text, found.start(kind))
                    if at < 0:
                        return _Lexed(tokens, words, "comment")
                    break
                if kind == "open":
                    close = _CLOSING_MARK[written[-1]]
                    return _Lexed(tokens, words, "quote",
                                  text[found.end():].replace(close * 2, close))
                continue
            tokens.append((kind, written))
            words.append(written.upper() if kind == "word" else written)
        else:
            return _Lexed(tokens, words)


def _wants_more(words: list, at: int, *, before_a_semicolon: bool = False
                ) -> bool:
    """Whether the token at `at` is asking for something that never came."""
    word = words[at]
    if word in _WANTS_MORE_MARKS:
        return True
    if word not in _WANTS_MORE:
        return False
    if before_a_semicolon and word == "BEGIN":
        # Measured: BEGIN; SELECT 1 AS v; END answers.
        return False
    before = words[at - 1] if at else ""
    if word in ("INSERT", "UPDATE", "DELETE") and before in _NAMES_AN_EVENT:
        return False
    return not (word == "VALUES" and before == "DEFAULT")


def _begins_its_statement(words: list, at: int) -> bool:
    """Whether the SET at `at` begins a statement of its own.

    A SET that stops a batch short is msg 156 near the keyword where it
    begins its own statement, and msg 102 near it where it belongs to an
    UPDATE or an ALTER: measured both ways, after SELECT, DECLARE, BEGIN
    TRAN and a semicolon for the first, and after UPDATE, UPDATE ... WITH
    (ROWLOCK), a MERGE's UPDATE and ALTER DATABASE for the second.
    """
    for back in range(at - 1, -1, -1):
        word = words[back]
        if word in _SET_IS_ITS_OWN:
            return False
        if word == ";" or word in _BEGINS_A_STATEMENT:
            return True
    return True


def _in_a_declare_list(words: list) -> bool:
    """Whether the last token is a variable a DECLARE list names and stops at.

    Walks back over the list to the DECLARE, stepping over bracketed
    values, and stops at whatever begins any other statement: SELECT @a, @b
    and EXEC p @a, @b both end on a variable after a comma and are good.
    """
    depth = 0
    for back in range(len(words) - 2, -1, -1):
        word = words[back]
        if word == ")":
            depth += 1
        elif word == "(":
            depth -= 1
            if depth < 0:
                return False
        elif depth:
            continue
        elif word == "DECLARE":
            return True
        elif word == ";" or word in _BEGINS_A_STATEMENT:
            return False
    return False


def _left_unfinished(tokens: list, words: list) -> bool:
    """The endings measured as unfinished that end on an ordinary word.

    DECLARE @p and SET @p are each msg 102 near the variable, and so is a
    DECLARE list stopped after a comma, as DECLARE @a int, @b. A CREATE
    TABLE that names its table and stops is 102 near the name, and SAVE
    TRAN with no name after it is 102 near TRAN.
    """
    last = words[-1]
    before = words[-2] if len(words) >= 2 else ""
    if last[:1] == "@" and last[:2] != "@@":
        if before in ("DECLARE", "SET"):
            return True
        if before == "," and _in_a_declare_list(words):
            return True
    if before == "SAVE" and last in ("TRAN", "TRANSACTION"):
        return True
    return (len(words) >= 3 and words[-3] == "CREATE" and before == "TABLE"
            and tokens[-1][0] in ("word", "name"))


_OPENS_OR_CLOSES = frozenset({"(", ")", "CASE", "BEGIN", "END"})


def _opening(opened: list, words: list, at: int) -> bool:
    """Carry the brackets and blocks still open past one token.

    False where the text does not close what it opened in order, an END
    with no BEGIN or CASE before it, say: there this cannot tell what the
    text is, and says nothing about it.
    """
    word = words[at]
    if word == "(":
        opened.append("(")
    elif word == ")":
        if not opened or opened[-1] != "(":
            return False
        opened.pop()
    elif word == "CASE":
        opened.append("CASE")
    elif word == "BEGIN":
        after = words[at + 1] if at + 1 < len(words) else ""
        if after not in _BEGIN_WITHOUT_AN_END:
            opened.append("BEGIN")
    elif word == "END":
        if not opened or opened[-1] == "(":
            return False
        opened.pop()
    return True


def _near(text: str) -> SqlError:
    return SqlError(f"Incorrect syntax near '{text}'.", number=SYNTAX_ERROR)


def _near_the_keyword(written: str) -> SqlError:
    return SqlError(f"Incorrect syntax near the keyword '{written}'.",
                    number=NEAR_A_KEYWORD)


def _first_misplaced(tokens: list, words: list
                     ) -> tuple[SqlError | None, list | None]:
    """The first token a real server cannot read where it stands, if any,
    and what the text leaves open at its end.

    Read left to right, because a real server reports what its parser
    cannot get past first. Three things are settled here, each measured:

    - a clause word straight after something waiting for a name or a value
      is 156 near that clause word, as written;
    - a semicolon straight after something that wants more, or inside a
      bracket or a CASE still open, is 102 near ';';
    - a SET that begins its statement, with a semicolon or the start of
      another statement straight after it, is 156 near the keyword 'SET',
      in capitals however it was written.

    What is left open is None where the text did not close what it opened
    in order, and then nothing is said about how it ends either.
    """
    opened: list = []
    before = ""
    for at, word in enumerate(words):
        if (before == "SET" and (word == ";" or word in _NOT_AFTER_A_SET)
                and _begins_its_statement(words, at - 1)):
            return _near_the_keyword("SET"), None
        if word == ";":
            if at and ((opened and opened[-1] in ("(", "CASE"))
                       or _wants_more(words, at - 1, before_a_semicolon=True)):
                return _near(";"), None
        elif word in _ONLY_BEGINS_A_CLAUSE and before in _WAITS_FOR_AN_OPERAND:
            return _near_the_keyword(tokens[at][1]), None
        if word in _OPENS_OR_CLOSES and not _opening(opened, words, at):
            return None, None
        before = word
    return None, opened


def _unfinished(tokens: list, words: list, opened: list | None
                ) -> SqlError | None:
    """What a real server says of a batch that runs out where it cannot.

    Msg 102 near the last token, where a bracket, a BEGIN or a CASE is still
    open, the last token asks for more, or the batch stops in one of the
    places _left_unfinished names. A SET that begins its statement is the
    exception, measured: 156 near the keyword 'SET'.
    """
    if opened is None:
        return None
    last = len(words) - 1
    if words[last] == "SET" and _begins_its_statement(words, last):
        return _near_the_keyword("SET")
    if opened or _wants_more(words, last) or _left_unfinished(tokens, words):
        return _near(tokens[last][1])
    return None


def _as_sent(message: str) -> str:
    """A message cut where a real server cuts one, measured on msg 105."""
    if len(message) <= _LONGEST_MESSAGE:
        return message
    return message[:_LONGEST_MESSAGE - 3] + "..."


def malformed(text: str) -> list[SqlError]:
    """The syntax errors a real server gives a batch that cannot be T-SQL.

    A real server compiles the whole batch before it runs any of it, and a
    batch that will not compile runs none of it: measured, CREATE TABLE #t
    (a int); SELECT * FROM is msg 102 and leaves no #t behind. This asks the
    same question of the text before anything is run, and answers only where
    the text itself settles it, so that good T-SQL this server cannot answer
    keeps its own refusal rather than being told its syntax is wrong.

    The errors come in the order a real server sends them, which is the
    order it reads the text in, and the first is the one a client raises.
    There are two when the lexer and the parser each find something, and
    both were measured for each case:

    - a quote left open is msg 105 quoting everything after it, then 102
      near the same text, unless something before it had already failed, in
      which case that is first and the 105 follows it alone;
    - a comment left open is msg 113, followed by whatever the text before
      it lacked, or preceded by whatever in that text failed first.

    Nothing where the text leaves the question open, so the statement reader
    decides as it always did.
    """
    lexed = _lexed(text)
    tokens, words = lexed.tokens, lexed.words
    misplaced, opened = _first_misplaced(tokens, words)
    if lexed.left_open == "quote":
        left_open = SqlError(
            _as_sent("Unclosed quotation mark after the character string "
                     f"'{lexed.rest}'."),
            number=UNCLOSED_QUOTATION,
        )
        if misplaced is not None:
            return [misplaced, left_open]
        return [left_open, _near(lexed.rest[:_LONGEST_NEAR])]
    comment = ([SqlError("Missing end comment mark '*/'.",
                         number=MISSING_END_COMMENT)]
               if lexed.left_open == "comment" else [])
    if misplaced is not None:
        return [misplaced, *comment]
    ended = _unfinished(tokens, words, opened) if tokens else None
    return [*comment, *([ended] if ended is not None else [])]


# What a batch that opens with one of these is: a module whose parameters
# are declared in its own header, and which a real server only accepts as
# the first statement of a batch. Nothing in one is checked here.
_DEFINES_A_MODULE = frozenset({"PROC", "PROCEDURE", "FUNCTION", "TRIGGER"})

# Where a name is a table rather than a value, measured: after FROM, JOIN,
# UPDATE, and INTO in an INSERT, an undeclared one is msg 1087, "table
# variable". A FETCH reads FROM a cursor and INTO a value, and there the
# same name is 137.
_A_TABLE_GOES_AFTER = frozenset({"FROM", "JOIN", "UPDATE", "INTO"})


def _a_variable(word: str) -> bool:
    """A variable's name, which @@ROWCOUNT and the like are not."""
    return word[:1] == "@" and word[:2] != "@@"


def _leaves(sql: str):
    """Every statement a batch holds, in the order they are written, with
    the ones inside an IF, a WHILE, a block, a TRY and a CATCH taken out.

    A real server compiles each of these as a statement of its own, which
    is what gives each its own error: measured, an IF's condition and the
    statement it guards are two, and so are the statements of a block, a
    TRY and its CATCH, and a branch and its ELSE.
    """
    for one in statements(sql):
        yield from _opened_up(one)


def _opened_up(one: str):
    """The statements one compound statement holds, or the statement."""
    head = _WORD.match(one)
    word = head.group(0).upper() if head else ""
    if word in ("IF", "WHILE"):
        guarded = _next_word_in(one, head.end(), STATEMENT_STARTS)
        if guarded is None:
            yield one
            return
        yield one[:guarded]
        end = end_of_branch(one, guarded)
        yield from _leaves(one[guarded:end])
        otherwise = _next_word_in(one, end, {"ELSE"})
        if otherwise is not None and not one[end:otherwise].strip():
            yield from _leaves(one[_WORD.match(one, otherwise).end():])
        return
    tried = _TRY.match(one)
    if tried:
        end = end_of_branch(one, 0)            # just past the END of END TRY
        yield from _leaves(_inside(one, tried.end(), end))
        at = _past_word(one, end, "TRY")
        caught = _CATCH.match(one, at)
        if caught:
            closing = end_of_branch(one, _skip_space(one, at))
            yield from _leaves(_inside(one, caught.end(), closing))
        return
    if word == "BEGIN" and not _BEGINS_A_TRANSACTION.match(one):
        end = end_of_branch(one, 0)
        yield from _leaves(_inside(one, head.end(), end))
        yield from _leaves(one[end:])
        return
    yield one


def _inside(one: str, start: int, end: int) -> str:
    """What a block holds, between its opening and the END that closes it."""
    held = one[start:end]
    return held[:-len("END")] if held.upper().endswith("END") else held


def _declarators(words: list) -> list:
    """Where a DECLARE names its variables, one place for each of them.

    A comma begins the next one only outside brackets and a CASE, so a
    value written as a call or a CASE, or a table variable's column list,
    stays inside the declaration it belongs to.
    """
    if len(words) < 2 or words[0] != "DECLARE" or not _a_variable(words[1]):
        return []
    places = [1]
    depth = 0
    for at in range(2, len(words)):
        word = words[at]
        if word in ("(", "CASE"):
            depth += 1
        elif word in (")", "END"):
            depth -= 1
        elif (word == "," and not depth and at + 1 < len(words)
                and _a_variable(words[at + 1])):
            places.append(at + 1)
    return places


def _named_arguments(words: list) -> set:
    """Where an EXEC names the parameter it gives a value to, as @p = 1.

    The name there is the procedure's rather than a variable of the batch:
    measured, EXEC sp_executesql N'SELECT @x AS v', N'@x int', @x = 5
    answers 5. The value after it is read like any other, and so is a
    variable straight after the EXEC, which is where the return value goes:
    EXEC @rc = sp_who is 137 when nothing declared @rc.
    """
    places = set()
    called = None
    for at, word in enumerate(words):
        if word in ("EXEC", "EXECUTE"):
            called = at
        elif (called is not None and at > called + 1 and _a_variable(word)
                and at + 1 < len(words) and words[at + 1] == "="):
            places.add(at)
    return places


def _first_unknown(tokens: list, words: list, known: set,
                   declaring: list) -> SqlError | None:
    """The error for the first variable a statement reads that is unknown.

    One per statement, measured: SELECT @zz AS a, @yy AS b names only @zz.
    The name is quoted as the statement wrote it.
    """
    passed = set(declaring) | _named_arguments(words)
    for at, word in enumerate(words):
        if not _a_variable(word) or at in passed:
            continue
        name = tokens[at][1]
        if name[1:].lower() in known:
            continue
        if (at and words[0] != "FETCH"
                and words[at - 1] in _A_TABLE_GOES_AFTER):
            return SqlError(f'Must declare the table variable "{name}".',
                            number=UNDECLARED_TABLE_VARIABLE, state=2)
        return SqlError(f'Must declare the scalar variable "{name}".',
                        number=UNDECLARED_VARIABLE, state=2)
    return None


def undeclared(sql: str, known=()) -> list[SqlError]:
    """Msg 137 or 1087 for each statement that reads a variable nothing
    declared before it, in the order they are written.

    A real server settles this while compiling, the way it settles a syntax
    error, so a batch that reads one runs none of itself: measured, SELECT
    'before' AS v; SELECT @zz AS v answers the error alone. A variable is
    known from the end of the DECLARE that names it, wherever that is
    written and whether or not it runs, so IF 1 = 0 BEGIN DECLARE @x int
    END; SELECT @x AS v answers. A DECLARE that fails declares nothing:
    DECLARE @a int = 1, @b int = @a is 137 on @a, which the same DECLARE
    has not made yet, and a SELECT @b after it is 137 as well.

    `known` is the names a client sent values for, which is how a
    parameterised statement declares them. The text should be free of
    comments, because a name inside one is not read.

    Text with no @ in it reads no variable, and is passed without being
    read at all: walking it cost a 38,000 character IN list 6 ms, three
    times what the syntax check costs it.
    """
    if "@" not in sql:
        return []
    known = {name.lstrip("@").lower() for name in known}
    found = []
    for place, leaf in enumerate(_leaves(sql)):
        lexed = _lexed(leaf)
        if (not place and lexed.words[:1] in (["CREATE"], ["ALTER"])
                and _DEFINES_A_MODULE.intersection(lexed.words[1:4])):
            return []
        declaring = _declarators(lexed.words)
        unknown = _first_unknown(lexed.tokens, lexed.words, known, declaring)
        if unknown is not None:
            found.append(unknown)
            continue
        known |= {lexed.tokens[at][1][1:].lower() for at in declaring}
    return found


def without_comments(sql: str) -> str:
    """The same statement with its comments replaced by a space.

    A comment cannot be left in: the batch SSMS sends has one between two
    statements, and a parser that has never heard of it reads the rest of
    that line as part of a query.

    The walk rebuilds a statement with no comment in it character by
    character to arrive at the same string, and returning it untouched where
    neither "--" nor "/*" appears makes this function 260 times quicker on a
    query of 3,600 characters. It is not worth having: measured end to end it
    was 0.6% of a small query, 0.3% of a long one, and 0.7% of a 39,000
    character IN list over fifty rows, because what a long query costs is
    parsing and answering it rather than reading past it. A profile says
    otherwise and a profile is wrong; see _local in workbook.py for the same
    lesson.
    """
    out: list[str] = []
    at = 0
    while at < len(sql):
        char = sql[at]
        if char in "\'\"":
            end = skip_quoted(sql, at, char)
            out.append(sql[at:end])
            at = end
            continue
        if char == "[":
            # Past the ]] a bracketed name writes one ] as, the way _lexed
            # reads it: stopping at the first ] read the rest of the name
            # as text, and a -- in it as a comment.
            found = _closing(sql, at, "]")
            end = len(sql) if found < 0 else found
            out.append(sql[at:end])
            at = end
            continue
        if sql.startswith("--", at):
            line = sql.find("\n", at)
            at = len(sql) if line < 0 else line
            out.append(" ")
            continue
        if sql.startswith("/*", at):
            # To the */ that closes this one rather than the first, because
            # comments nest; see _past_the_comment.
            close = _past_the_comment(sql, at)
            at = len(sql) if close < 0 else close
            out.append(" ")
            continue
        out.append(char)
        at += 1
    return "".join(out)


def statements(sql: str) -> list[str]:
    """One batch split into the statements it holds.

    Split on the semicolons that are actually between statements: one inside
    a string or a bracketed name is part of a value, and splitting there
    would cut a query in half. A trailing empty piece is dropped, so a single
    statement written with a semicolon is still one statement.
    """
    found: list[str] = []
    start = at = 0
    depth = 0
    while at < len(sql):
        char = sql[at]
        if char in "\'\"":
            at = skip_quoted(sql, at, char)
            continue
        if char == "[":
            found_at = sql.find("]", at)
            at = len(sql) if found_at < 0 else found_at + 1
            continue
        if char == ";":
            found.append(sql[start:at])
            start = at = at + 1
            continue
        if depth == 0:
            word = _WORD.match(sql, at)
            if word and word.group(0).upper() == "BEGIN" and _TRY.match(sql, at):
                # A TRY and its CATCH are one statement, and so is whatever
                # they guard. Split apart, the SELECT after END CATCH begins
                # with END and is run by nothing.
                if at > start:
                    found.append(sql[start:at])
                    start = at
                at = end_of_try(sql, at)
                found.append(sql[start:at])
                start = at
                continue
            if word and word.group(0).upper() == "IF":
                # An IF holds its branches, ELSE and all, and ends where they
                # do, whether it begins the batch or follows something else.
                # What comes after it is a statement of its own.
                if at > start:
                    found.append(sql[start:at])
                    start = at
                at = end_of_if(sql, at)
                found.append(sql[start:at])
                start = at
                continue
            if (word and at > start
                    and word.group(0).upper() in _STARTS_A_STATEMENT
                    and not _belongs_to_it(sql[start:at], word.group(0))):
                # T-SQL needs no semicolon between statements, so a word that
                # nothing else can be followed by is where the next one
                # begins: SSMS writes a read and an IF with only a space
                # between them.
                found.append(sql[start:at])
                start = at
            if word:
                at = word.end()
                continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        at += 1
    found.append(sql[start:])
    return [one for one in (part.strip() for part in found) if one]


def _belongs_to_it(so_far: str, word: str) -> bool:
    """Whether this word continues the statement rather than starting one.

    INSERT ... EXEC is one statement: the rows the procedure returns are what
    is inserted. Splitting there would leave an INSERT with nothing to put in
    the table and an EXEC nobody wanted the rows from.

    SELECT begins a statement after a variable has been declared or set,
    which are the two that end where their value does, and after a statement
    that already has a SELECT of its own. INSERT INTO t SELECT ... SELECT ...
    is two statements; SELECT ... UNION SELECT ... is one, so a set operator
    keeps them together.

    SET is the other way round: it begins a statement everywhere except in
    an UPDATE, which is the one statement built out of it.
    """
    before = so_far.strip()
    if word.upper() == "SELECT":
        if _JOINS_TWO_SELECTS.search(before):
            return True
        if _HOLDS_NO_SELECT.match(before):
            return False
        return _next_word_in(before, 0, {"SELECT"}) is None
    if word.upper() == "SET":
        return before.upper().startswith("UPDATE")
    return (word.upper() in ("EXEC", "EXECUTE")
            and before.upper().startswith("INSERT"))


# What one SELECT is joined to another by, so the second belongs to the same
# statement rather than beginning one.
_JOINS_TWO_SELECTS = re.compile(
    r"\b(?:UNION|EXCEPT|INTERSECT)(?:\s+ALL)?\s*$", re.IGNORECASE
)

# A statement no SELECT can be part of, so one after it begins another. The
# rest can hold one: INSERT INTO t SELECT is a single statement, and so is a
# CTE and a branch of an IF.
# Nor can beginning, ending or marking a transaction, which is what splits
# BEGIN TRAN from a SELECT written on the line after it.
_HOLDS_NO_SELECT = re.compile(
    r"(?:DECLARE|SET|EXEC|EXECUTE|DROP|CREATE|COMMIT|ROLLBACK|SAVE"
    r"|BEGIN\s+(?:TRAN|TRANSACTION|DISTRIBUTED))\b",
    re.IGNORECASE,
)


def end_of_if(sql: str, at: int) -> int:
    """Where an IF statement stops, branches and all.

    The condition runs to the statement it guards, that statement runs to an
    ELSE or to whatever begins next, and a branch written as BEGIN...END runs
    to its own END however many are nested inside it.
    """
    at = _WORD.match(sql, at).end()                      # past the IF itself
    at = _next_word_in(sql, at, STATEMENT_STARTS) or len(sql)
    at = end_of_branch(sql, at)
    otherwise = _next_word_in(sql, at, {"ELSE"})
    if otherwise is not None and not sql[at:otherwise].strip():
        at = _WORD.match(sql, otherwise).end()
        at = end_of_branch(sql, at)
    return at


# BEGIN TRY, which is a BEGIN that has to be seen before the word after it.
_TRY = re.compile(r"BEGIN\s+TRY\b", re.IGNORECASE)
_CATCH = re.compile(r"\s*BEGIN\s+CATCH\b", re.IGNORECASE)


def end_of_try(sql: str, at: int) -> int:
    """Where a BEGIN TRY stops, its CATCH included.

    The block finder counts BEGIN against END and stops on the END, which
    here is the first word of END TRY; the second word belongs to the block
    and is stepped over. Then the CATCH, the same way.
    """
    at = _past_word(sql, end_of_branch(sql, at), "TRY")
    if _CATCH.match(sql, at) is None:
        return at
    return _past_word(sql, end_of_branch(sql, _skip_space(sql, at)), "CATCH")


def _past_word(sql: str, at: int, wanted: str) -> int:
    """Past this word if it is the next one, and unchanged if it is not."""
    found = _WORD.match(sql, _skip_space(sql, at))
    if found and found.group(0).upper() == wanted:
        return found.end()
    return at


# A BEGIN that begins a transaction rather than a block, and so has no END.
_BEGINS_A_TRANSACTION = re.compile(
    r"BEGIN\s+(?:TRAN|TRANSACTION|DISTRIBUTED)\b", re.IGNORECASE
)


def end_of_branch(sql: str, at: int) -> int:
    """Where one branch of an IF stops, nesting and all.

    A BEGIN block runs to its own END however many blocks and CASEs are
    inside it, which is what tells this IF's ELSE from an inner one. BEGIN
    TRAN is a statement rather than a block: it has no END, and read as one
    it would take everything after it in the batch into the branch.
    """
    at = _skip_space(sql, at)
    word = _WORD.match(sql, at)
    if (word and word.group(0).upper() == "BEGIN"
            and not _BEGINS_A_TRANSACTION.match(sql, at)):
        depth = 0
        cases = 0
        while at < len(sql):
            word = _WORD.match(sql, at)
            if not word:
                at = _past_one(sql, at)
                continue
            upper = word.group(0).upper()
            if upper == "BEGIN":
                depth += 1
            elif upper == "CASE":
                # A CASE ends with END too, and that END closes no block.
                cases += 1
            elif upper == "END":
                if cases:
                    cases -= 1
                else:
                    depth -= 1
                    if depth == 0:
                        return word.end()
            at = word.end()
        return len(sql)
    # A single statement, which ends where the next one or an ELSE begins.
    if word:
        at = word.end()
    found = _next_word_in(sql, at, STATEMENT_STARTS | {"ELSE"})
    return found if found is not None else len(sql)


def _next_word_in(sql: str, at: int, wanted: set) -> int | None:
    """Where the next of these words begins, skipping anything quoted."""
    depth = 0
    while at < len(sql):
        char = sql[at]
        if char in "'\"" or char == "[":
            at = _past_one(sql, at)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            word = _WORD.match(sql, at)
            if word:
                if word.group(0).upper() in wanted:
                    return at
                at = word.end()
                continue
        at += 1
    return None


def _past_one(sql: str, at: int) -> int:
    """Past a quoted run or a bracketed name, or one character."""
    char = sql[at]
    if char in "'\"":
        return skip_quoted(sql, at, char)
    if char == "[":
        found = sql.find("]", at)
        return len(sql) if found < 0 else found + 1
    return at + 1


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# The words a statement can begin with, used to find where a condition ends.
# COMMIT and ROLLBACK are here because IF @@TRANCOUNT > 0 COMMIT TRAN is how
# a transaction is ended only when one is open, and without them the
# condition had no end and the batch was refused.
STATEMENT_STARTS = frozenset({
    "SELECT", "EXEC", "EXECUTE", "SET", "DECLARE", "PRINT", "RETURN",
    "BEGIN", "WITH", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP",
    "RAISERROR", "THROW", "IF", "COMMIT", "ROLLBACK", "SAVE",
})

# Words that can only begin a statement, so one of them mid-batch means the
# statement before it ended. SELECT is there for what it follows rather than
# for itself: it stands inside a statement as often as it begins one, so
# _belongs_to_it decides, and it belongs to everything but a variable and a
# transaction statement.
_STARTS_A_STATEMENT = frozenset({
    "IF", "DECLARE", "EXEC", "EXECUTE", "PRINT", "RETURN", "BEGIN",
    "CREATE", "DROP", "INSERT", "UPDATE", "DELETE", "SELECT", "SET",
    "COMMIT", "ROLLBACK", "SAVE",
})


def parse_select(sql: str) -> Select:
    """Parse a SELECT, or say why it cannot be answered."""
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise SqlError("empty statement")
    text = _unbracketed(text)

    ctes: tuple = ()
    with_match = _WITH.match(text)
    if with_match:
        ctes, at = _read_ctes(text, with_match.end())
        text = text[at:].strip()
        if not text:
            # A named query and nothing that reads it, which used to reach
            # the split below with nothing to split and raise an IndexError.
            raise SqlError("Incorrect syntax near ')'.", number=SYNTAX_ERROR)

    match = _SELECT.match(text)
    if not match:
        first = text.split(None, 1)[0]
        if first.upper() == "SELECT":
            # SELECT with nothing after it, which is what a log truncated
            # mid-query looks like. Saying it is not a SELECT reads as
            # nonsense; SQL Server calls it a syntax error, and so does this.
            raise SqlError("Incorrect syntax near 'SELECT'.",
                           number=SYNTAX_ERROR)
        raise SqlError(f"only SELECT is supported, not {first.upper()}")
    at = match.end()

    distinct_match = _DISTINCT.match(text, at)
    distinct = bool(distinct_match)
    if distinct_match:
        at = distinct_match.end()

    top = top_parameter = None
    top_share = top_ties = False
    top_match = _TOP.match(text, at)
    if top_match:
        found = top_match.group(1)
        if found.startswith("@"):
            top_parameter = found
        else:
            top = int(found)
        top_share = top_match.group(2) is not None
        top_ties = top_match.group(3) is not None
        at = top_match.end()

    at = _skip_space(text, at)
    items: tuple[SelectItem, ...] | None
    items, at, listed = _read_select_list(text, at)
    if len(items) == 1 and items[0].star and not items[0].expression:
        items = None                       # a bare star is every column

    from_match = _FROM.match(text, at)
    if not from_match:
        lifted = list(listed)
        # A WHERE with no FROM decides whether the one row a computed select
        # produces is there at all, which is what SQL Server does with it and
        # what a client writes when it wants a row only on some condition.
        where = None
        where_match = _WHERE.match(text, at)
        if where_match:
            start = where_match.end()
            end = _find_order_by(
                text, start, ends=_ENDS_THE_LAST_CONDITION)
            condition = text[start:end if end is not None else len(text)].strip()
            condition, found = _lift_subqueries(condition, len(lifted))
            lifted.extend(found)
            try:
                where = parse_predicate(condition)
            except PredicateError as exc:
                raise _as_written(
                    exc, f"cannot read the WHERE condition: {exc}"
                ) from exc
            at = end if end is not None else len(text)
        order_by, offset, fetch, combine, at = _read_tail(text, at, lifted)
        at = _skip_query_hint(text, at)
        rest = text[at:at + 30].strip()
        if rest:
            raise SqlError(f"expected FROM after the column list, found {rest!r}")
        if items is None or any(
            item.star or (item.node is None and not item.is_aggregate)
            for item in items
        ):
            raise SqlError(
                "a SELECT with no FROM can only compute values, not read "
                "columns from a table"
            )
        # SELECT 1, or SELECT a function of nothing. Clients send these to
        # probe a connection, and answering is cheaper than refusing. A
        # subquery counts as a value, so SELECT (SELECT COUNT(*) FROM t) is
        # one of these and has to carry what it lifted.
        return Select(table="", items=items, distinct=distinct, top=top,
                      top_parameter=top_parameter, top_share=top_share,
                      top_ties=top_ties, subqueries=tuple(lifted),
                      where=where, order_by=order_by, offset=offset,
                      fetch=fetch, combine=combine)

    derived = None
    values_rows: tuple = ()
    values_columns: tuple = ()
    probe = _skip_space(text, from_match.end())
    if text[probe:probe + 1] == "(":
        schema, table = None, ""
        (derived, values_rows, values_columns,
         alias, at) = _read_bracketed_table(text, probe)
        table = alias
    else:
        schema, table, at = _read_qualified_name(text, from_match.end())
        at = _skip_table_hint(text, at)
        alias, at = _read_table_alias(text, at)
        at = _skip_table_hint(text, at)
    # Built before the joins are read, because an ON condition may hold a
    # subquery and the numbering has to carry on from the select list's.
    subqueries: list[Subquery] = list(listed)
    joins, at = _read_joins(text, at, subqueries)
    applies, at = _read_applies(text, at)

    where = None
    where_match = _WHERE.match(text, at)
    if where_match:
        start = where_match.end()
        # The condition ends where a top-level ORDER BY begins, or at the end
        # of the statement. Handing the whole tail to the predicate parser
        # would make "ORDER" look like a column name.
        end = _find_order_by(text, start)
        condition = text[start:end if end is not None else len(text)].strip()
        condition, found = _lift_subqueries(condition, len(subqueries))
        subqueries.extend(found)
        try:
            where = parse_predicate(condition)
        except PredicateError as exc:
            raise _as_written(
                    exc, f"cannot read the WHERE condition: {exc}"
                ) from exc
        at = end if end is not None else len(text)

    group_by: tuple[str, ...] = ()
    group_match = _GROUP_BY.match(text, at)
    if group_match:
        group_by, at = _read_group_by(text, group_match.end())

    having = None
    having_match = _HAVING.match(text, at)
    if having_match:
        # With no GROUP BY the whole table is one group, and SQL Server takes
        # a HAVING over it. The select list still has to be aggregated, which
        # the check below enforces.
        start = having_match.end()
        end = _find_order_by(
            text, start, ends=_ENDS_THE_LAST_CONDITION)
        condition = text[start:end if end is not None else len(text)].strip()
        # Lifted the way a WHERE's are, because a client puts one here too:
        # HAVING COUNT(*) = (SELECT MAX(c) FROM ...) is how a report asks for
        # the biggest group. Without this the condition reached the parser
        # with a SELECT still written out in it and failed on the bracket.
        condition, found = _lift_subqueries(condition, len(subqueries))
        subqueries.extend(found)
        try:
            having = parse_predicate(condition)
        except PredicateError as exc:
            raise _as_written(
                exc, f"cannot read the HAVING condition: {exc}"
            ) from exc
        at = end if end is not None else len(text)

    order_by, offset, fetch, combine, at = _read_tail(text, at, subqueries)
    at = _skip_query_hint(text, at)

    trailing = text[at:].strip()
    if trailing:
        # Naming the clause is what makes this useful: a client whose ORDER BY
        # was dropped should learn that, not receive rows in another order.
        clause = trailing.split(None, 1)[0].upper()
        raise SqlError(
            f"{clause} is not supported; this server can read a table with a "
            f"column list, TOP, WHERE and ORDER BY"
        )

    if top_ties and not order_by:
        raise SqlError(
            "The TOP N WITH TIES clause is not allowed without a "
            "corresponding ORDER BY clause.",
            number=TIES_NEED_AN_ORDER,
        )

    if items is None and group_by:
        raise SqlError("SELECT * cannot be grouped; name the columns instead")

    for written in group_by:
        # A group has to be of something the rows decide. GROUP BY 1 or
        # GROUP BY GETDATE() is the same value for every row, so it is one
        # group of everything, and SQL Server refuses it rather than
        # answering the question nobody meant to ask.
        try:
            grouping = parse_expression(written)
        except PredicateError:
            continue          # not readable as one; the table will say so
        if not reads_a_column(grouping):
            raise SqlError(
                "Each GROUP BY expression must contain at least one column "
                "that is not an outer reference.",
                number=GROUP_BY_NEEDS_A_COLUMN,
            )

    if items is not None and (
        group_by or having is not None
        or any(i.is_aggregate or aggregates_in(i.node) for i in items)
    ):
        # Every column that is not aggregated has to be grouped on, or the
        # value it would report is one row's out of many.
        # Compared on the last part as well as the whole, the way every
        # other reference resolves: SELECT name beside GROUP BY c.name is one
        # column named two ways, and refusing it would be refusing the query
        # a real server answers.
        grouped = {one_spelling(name) for name in group_by}
        grouped |= {one_spelling(name).rsplit(".", 1)[-1] for name in group_by}
        for item in items:
            if item.is_window:
                raise SqlError(
                    "a window function beside a GROUP BY is not supported; "
                    "the window would be over the grouped rows, and this "
                    "works one out over the rows themselves"
                )
            if item.is_aggregate or item.expression is None:
                continue
            if item.node is not None and not reads_a_column(item.node):
                # A value that reads no column is the same for every row, so
                # there is nothing for a GROUP BY to decide: SELECT 1, a
                # lifted scalar subquery, and an expression whose columns an
                # aggregate has already reduced all stand beside an aggregate.
                continue
            written = one_spelling(item.expression)
            if written in grouped or written.rsplit(".", 1)[-1] in grouped:
                continue
            raise SqlError(
                f"Column '{table}.{item.expression}' is invalid in the "
                f"select list because it is not contained in either an "
                f"aggregate function or the GROUP BY clause.",
                number=NOT_GROUPED_OR_AGGREGATED,
            )

    if items is None and having is not None:
        raise SqlError("SELECT * cannot be filtered by a HAVING; name the columns")

    return Select(
        table=table,
        schema=schema,
        alias=alias,
        derived=derived,
        values_rows=values_rows,
        values_columns=values_columns,
        ctes=ctes,
        subqueries=tuple(subqueries),
        joins=joins,
        applies=applies,
        items=items,
        distinct=distinct,
        top=top,
        top_parameter=top_parameter,
        top_share=top_share,
        top_ties=top_ties,
        where=where,
        group_by=group_by,
        having=having,
        order_by=order_by,
        offset=offset,
        fetch=fetch,
        combine=combine,
    )


def _read_ctes(text: str, at: int) -> tuple[tuple, int]:
    """The named queries of a WITH, in the order they were written.

    Each is parsed on its own, so a CTE that refers to an earlier one works.
    One that refers to itself is a recursion, which this cannot bound and
    says so by name: left to reach the catalog it looked like a table nobody
    had, and sent whoever read that looking for it.
    """
    named = []
    while True:
        name, at = _read_identifier(text, _skip_space(text, at))
        after_as = _AS.match(text, at)
        if not after_as:
            raise SqlError(f"the WITH entry '{name}' needs AS before its query")
        probe = _skip_space(text, after_as.end())
        if text[probe:probe + 1] != "(":
            raise SqlError(f"the WITH entry '{name}' needs its query in brackets")
        inner, at = _read_bracketed(text, probe)
        query = parse_select(inner)
        if _reads_itself(name, query):
            # A name inside a named query is that query, not a table that
            # shares its name: measured, WITH folk AS (SELECT id FROM folk)
            # is recursive on a real server even where a table called folk
            # exists. Qualifying it, dbo.folk, reads the table instead.
            if not any(kind == "UNION ALL" for kind, _ in query.combine):
                raise SqlError(
                    f"Recursive common table expression '{name}' does not "
                    f"contain a top-level UNION ALL operator.",
                    number=NOT_A_RECURSION,
                )
            raise SqlError(
                f"the WITH entry '{name}' reads itself, which is a recursive "
                f"query; this server answers each named query once and cannot "
                f"repeat one until it stops producing rows"
            )
        named.append((name, query))
        at = _skip_space(text, at)
        if text[at:at + 1] == ",":
            at += 1
            continue
        return tuple(named), at


def _reads_itself(name: str, select) -> bool:
    """Whether a named query names itself where it reads a table.

    The FROM, the joins, the table a derived one is built from, and the
    branches it is combined with, which is where a recursive reference can
    be written: the recursive part of one is a select over the name itself,
    combined with the part that starts it off. A nested CTE that shares the
    name is somebody else's and stops the walk, because a name is looked up
    in what came before it. A qualified name is a table and never the query
    around it, which is how a person reads the table their CTE is named
    after.
    """
    if select is None:
        return False
    wanted = name.lower()
    if any(one.lower() == wanted for one, _ in select.ctes):
        return False
    # Only an unqualified name. dbo.folk names the table, whatever the
    # named query around it is called; measured.
    if not select.schema and (select.table or "").lower() == wanted:
        return True
    if any(not join.schema and (join.table or "").lower() == wanted
           for join in select.joins):
        return True
    inside = [select.derived]
    inside.extend(branch for _, branch in select.combine)
    return any(_reads_itself(name, one) for one in inside if one is not None)


def _read_bracketed(text: str, at: int) -> tuple[str, int]:
    """The contents of a balanced bracket group, and where it ended."""
    depth = 0
    start = at
    while at < len(text):
        char = text[at]
        if char in "'\"":
            at = skip_quoted(text, at, char)
            continue
        if char == "[":
            found = text.find("]", at)
            at = len(text) if found < 0 else found + 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:at].strip(), at + 1
        at += 1
    raise SqlError("a bracket was opened and not closed")


# What a subquery is standing in for, decided by the word in front of it.
_BEFORE_SUBQUERY = re.compile(
    r"(?:\b(IN|EXISTS|ANY|ALL|SOME)\s*|([<>=!]+)\s*)$", re.IGNORECASE)
# The words that ask about a whole column rather than about one value.
_ASKS_A_SET = frozenset({"IN", "ANY", "ALL", "SOME"})


def _lift_subqueries(condition: str, start: int = 0) -> tuple[str, list]:
    """Replace every bracketed SELECT with a parameter, keeping its text aside.

    Done before the text is parsed, so neither the predicate parser nor the
    expression parser has to know what a catalog is: each sees a parameter,
    and the value bound to it is whatever the subquery produced.

    start numbers the parameters, because a select list and a WHERE are lifted
    separately and two subqueries called @__subquery_0 would be one.
    """
    found: list[Subquery] = []
    out = []
    at = 0
    while at < len(condition):
        char = condition[at]
        if char in "'\"":
            end = skip_quoted(condition, at, char)
            out.append(condition[at:end])
            at = end
            continue
        if char == "[":
            end = condition.find("]", at)
            end = len(condition) if end < 0 else end + 1
            out.append(condition[at:end])
            at = end
            continue
        if char != "(":
            out.append(char)
            at += 1
            continue

        inner, end = _read_bracketed(condition, at)
        if not _SELECT.match(inner):
            # Not a subquery itself, but one may be inside it: SSMS reads a
            # setting with CAST((SELECT ...) AS bit), and a lifter that
            # stepped over the cast never saw the select in it.
            lifted, within = _lift_subqueries(inner, start + len(found))
            found.extend(within)
            out.append(f"({lifted})")
            at = end
            continue

        before = _BEFORE_SUBQUERY.search("".join(out))
        keyword = (before.group(1) or "").upper() if before else ""
        kind = ("exists" if keyword == "EXISTS"
                else "in" if keyword in _ASKS_A_SET else "scalar")
        name = f"{_SUBQUERY_NAME}{start + len(found)}"
        found.append(Subquery(parameter=name, sql=inner, kind=kind))

        if kind == "exists":
            # EXISTS takes no operand, so it becomes a test on what it found.
            written = "".join(out)
            out = [written[:before.start()], f"{name} = 1"]
        elif keyword == "IN":
            # IN wants its brackets back; ANY and ALL read a bare name, and
            # both bind the whole column either way.
            out.append(f"({name})")
        else:
            out.append(name)
        at = end
    return "".join(out), found


# A hint on a table: WITH (NOLOCK), or the older form with no WITH. After a
# table name a bracket can be nothing else, because a bracket where a table
# was expected is a derived table and is read before this.
_TABLE_HINT = re.compile(r"\s*(?:WITH\s*)?(?=\()", re.IGNORECASE)


def _skip_table_hint(text: str, at: int) -> int:
    """Past a hint on a table, which says nothing here.

    NOLOCK and the rest are about locking and isolation, and this holds no
    locks and reads a source that was loaded whole. Ignoring them is what
    they mean here rather than a shortcut, and people write them by habit.
    """
    match = _TABLE_HINT.match(text, at)
    if not match:
        return at
    _, at = _read_bracketed(text, match.end())
    return at


def _skip_query_hint(text: str, at: int) -> int:
    """Past an OPTION clause, which says nothing here either.

    RECOMPILE, MAXDOP and the rest are about how a real server builds a plan.
    There is no plan here: the source was loaded whole and the rows are
    walked. A hint that cannot change the answer should not change whether
    there is one, and a person who writes OPTION (RECOMPILE) by habit gets
    the same rows either way.
    """
    match = _QUERY_HINT.match(text, at)
    if not match:
        return at
    _, at = _read_bracketed(text, match.end())
    return at


def _read_table_alias(text: str, at: int) -> tuple[str | None, int]:
    """An alias after a table name, with or without AS."""
    after_as = _AS.match(text, at)
    if after_as:
        name, at = _read_identifier(text, after_as.end())
        return name, at

    probe = _skip_space(text, at)
    match = _IDENTIFIER.match(text, probe)
    if not match:
        return None, at
    word = (match.group("bracketed") or match.group("quoted")
            or match.group("bare") or "")
    if word.upper() in _NOT_ALIASES:
        return None, at
    return word, match.end()


def _read_applies(text: str, at: int) -> tuple[tuple, int]:
    """Every APPLY after the FROM clause, of values or of a select."""
    applies: list = []
    while True:
        match = _CROSS_APPLY.match(text, at)
        if not match:
            break
        body, after = _read_bracketed(text, match.end() - 1)
        keep = match.group(1).upper() == "OUTER"
        values = _VALUES.match(body)
        if not values:
            if not _SELECT.match(body) and not _WITH.match(body):
                raise SqlError(
                    "APPLY reads a table written out with VALUES or a "
                    f"SELECT; this one has {body.strip()[:30]!r}"
                )
            alias, columns, at = _read_apply_alias(text, after, named=False)
            applies.append(Apply(alias=alias, sql=body.strip(),
                                 keep_unmatched=keep))
            continue
        rows = tuple(_read_values(body[values.end():]))
        alias, columns, at = _read_apply_alias(text, after)
        for row in rows:
            if len(row) != len(columns):
                raise SqlError(
                    f"'{alias}' names {len(columns)} columns and a row of its "
                    f"values has {len(row)}"
                )
        applies.append(Apply(alias=alias, columns=columns, rows=rows,
                             keep_unmatched=keep))
    return tuple(applies), at


def values_written(written: str) -> list | None:
    """The rows of a VALUES list, or None where the text is not one.

    VALUES (1, 'a'), (2, 'b') as an INSERT writes it, read the same way the
    one after a CROSS APPLY is read. Returned as parsed expressions rather
    than values, because a row may say GETDATE() or @p and only the caller
    knows what those are worth.
    """
    match = _VALUES.match(written)
    if not match:
        return None
    rows = _read_values(written[match.end():])
    if not rows:
        raise SqlError("VALUES needs at least one bracketed row")
    widths = {len(row) for row in rows}
    if len(widths) > 1:
        # SQL Server's own words and number, measured.
        raise SqlError(
            "The number of columns for each row in a table value constructor "
            "must be the same.",
            number=UNEVEN_VALUE_ROWS,
        )
    return rows


def _read_values(body: str) -> list:
    """Each bracketed group of a VALUES list, as parsed expressions."""
    rows = []
    at = 0
    while at < len(body):
        at = _skip_space(body, at)
        if body[at:at + 1] == ",":
            at += 1
            continue
        if body[at:at + 1] != "(":
            break
        inner, at = _read_bracketed(body, at)
        rows.append(tuple(
            _parsed(one) for one in _split_top_level(inner)
        ))
    return rows


def _parsed(written: str):
    try:
        return parse_expression(written.strip())
    except PredicateError as exc:
        raise _as_written(
            exc, f"cannot read {written.strip()!r} in VALUES: {exc}"
        ) from exc


def _split_top_level(written: str) -> list:
    """One entry per top-level comma."""
    found, depth, start = [], 0, 0
    at = 0
    while at < len(written):
        char = written[at]
        if char in "'\"":
            at = skip_quoted(written, at, char)
            continue
        if char == "[":
            end = written.find("]", at)
            at = len(written) if end < 0 else end + 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            found.append(written[start:at])
            start = at + 1
        at += 1
    found.append(written[start:])
    return [one for one in found if one.strip()]


def _read_bracketed_table(text: str, at: int):
    """A table written in brackets, and the name it is given.

    Two forms share the brackets. A derived SELECT brings its own columns,
    and a table value constructor has none of its own, so its alias names
    them: measured on SQL Server 2025, an alias naming no columns is 8155,
    a row wider than the list is 8158, and a narrower one is 8159.

    Read in one place because a FROM and a JOIN both take either form, and
    writing it twice is how the two would drift apart.

    Returns (derived, values_rows, values_columns, alias, at).
    """
    inner, at = _read_bracketed(text, at)
    written = values_written(inner)
    if written is None:
        derived = parse_select(inner)
        alias, at = _read_table_alias(text, at)
        if not alias:
            raise SqlError("a subquery used as a table needs an alias")
        return derived, (), (), alias, at

    # A table value constructor. Every bracket here used to be handed to
    # parse_select, which refused this one for not beginning with SELECT,
    # so a client asking for (VALUES (1),(2)) AS t(n) was told the whole
    # query was unsupported rather than the one construct in it.
    rows = tuple(written)
    alias, columns, at = _read_apply_alias(text, at, named=False)
    if not columns:
        raise SqlError(
            f"No column name was specified for column 1 of '{alias}'.",
            number=NO_NAME_FOR_A_VALUES_COLUMN,
        )
    for row in rows:
        if len(row) > len(columns):
            raise SqlError(
                f"'{alias}' has more columns than were specified in "
                f"the column list.",
                number=MORE_VALUES_THAN_NAMES,
            )
        if len(row) < len(columns):
            raise SqlError(
                f"'{alias}' has fewer columns than were specified in "
                f"the column list.",
                number=FEWER_VALUES_THAN_NAMES,
            )
    return None, rows, columns, alias, at


def _read_apply_alias(text: str, at: int,
                      named: bool = True) -> tuple[str, tuple, int]:
    """The name an applied table is given, and the names of its columns.

    Values written into a query have no names of their own, so the alias has
    to give them some. A select brings its own, and naming them again is
    allowed but not required.
    """
    at = _skip_space(text, at)
    as_match = _AS.match(text, at)
    if as_match:
        at = as_match.end()
    alias, at = _read_identifier(text, at)
    at = _skip_space(text, at)
    if text[at:at + 1] != "(":
        if not named:
            return alias, (), at
        raise SqlError(f"'{alias}' has to name the columns of its values")
    inner, at = _read_bracketed(text, at)
    columns = tuple(
        _bare(one.strip()) for one in _split_top_level(inner)
    )
    return alias, columns, at


def _read_joins(text: str, at: int,
                subqueries: list | None = None) -> tuple[tuple[Join, ...], int]:
    """Every JOIN clause after the first table.

    An ON condition is lifted like a WHERE, because a client puts subqueries
    there too: SSMS joins the indexes of a table on a condition that asks for
    the smallest index_id of that same table.
    """
    joins: list[Join] = []
    while True:
        match = _JOIN.match(text, at)
        if match:
            # One of the five the pattern matches, or INNER where it named
            # none. There is no sixth to refuse.
            kind = (match.group(1) or "INNER").upper()
            after = match.end()
        else:
            listed = _ANOTHER_TABLE.match(text, at)
            if not listed:
                break
            # FROM a, b is the older way of writing a cross join, and a WHERE
            # relating the two is what makes it an inner one. Reading it as a
            # cross join is not an approximation: it is what it means.
            kind = "CROSS"
            after = listed.end()
        derived = None
        values_rows: tuple = ()
        values_columns: tuple = ()
        probe = _skip_space(text, after)
        if text[probe:probe + 1] == "(":
            # A JOIN takes a bracketed table exactly as a FROM does, in
            # both its forms. This read a name and nothing else, so a
            # derived select and a table written out with VALUES were
            # refused here while both worked in the FROM. The comma form
            # of a cross join arrives here too and gets it as well.
            schema, table = None, ""
            (derived, values_rows, values_columns,
             alias, at) = _read_bracketed_table(text, probe)
        else:
            schema, table, at = _read_qualified_name(text, after)
            at = _skip_table_hint(text, at)
            alias, at = _read_table_alias(text, at)
            at = _skip_table_hint(text, at)

        on = None
        on_match = _ON.match(text, at)
        if on_match:
            start = on_match.end()
            end = _find_join_end(text, start)
            condition = text[start:end].strip()
            if subqueries is not None:
                condition, found = _lift_subqueries(condition, len(subqueries))
                subqueries.extend(found)
            try:
                on = parse_predicate(condition)
            except PredicateError as exc:
                raise _as_written(
                    exc, f"cannot read the ON condition: {exc}"
                ) from exc
            at = end
        elif kind != "CROSS":
            raise SqlError(
                f"the JOIN of '{table or alias}' needs an ON condition")

        joins.append(Join(table=table, schema=schema, alias=alias, kind=kind,
                          on=on, derived=derived, values_rows=values_rows,
                          values_columns=values_columns))
    return tuple(joins), at


def _find_join_end(text: str, start: int) -> int:
    """Where an ON condition stops: the next JOIN, clause, or statement.

    A SELECT ends it too. An ON is an expression, and no expression has a
    bare SELECT in it at the top level, so one there is the next statement:
    INSERT INTO t SELECT ... JOIN u ON a = b SELECT ... is two statements
    with nothing between them, and the ON used to swallow the second. So
    does an APPLY, which may follow the joins and is not part of the last
    one's condition.
    """
    end = _find_order_by(
        text, start, ends=(_JOIN, _WHERE, _SELECT, _CROSS_APPLY) + _ENDS_A_CLAUSE,
    )
    return end if end is not None else len(text)


def _read_group_by(text: str, at: int) -> tuple[tuple[str, ...], int]:
    """What a GROUP BY groups on: a column, or an expression over one.

    An expression as much as a column, because that is what a report groups
    by: the year of a date, the first letter of a name, a column folded to
    one case. Kept as written, so the select list can be matched against it.

    A grouping that stands for several groupings at once is refused by name.
    Left as written it reached the select list as an expression nothing
    matched, and the answer was that the column was neither grouped nor
    aggregated, which is untrue: it is grouped, in a way this cannot answer.
    """
    written: list[str] = []
    while True:
        body, at = _read_order_item(text, at, _GROUP_ITEM_ENDS)
        if not body:
            raise SqlError("GROUP BY needs a column or an expression")
        several = _SEVERAL_GROUPINGS.match(body)
        if several:
            kind = " ".join(several.group(1).upper().split())
            raise SqlError(
                f"GROUP BY {kind} asks for several groupings at once and the "
                f"subtotal rows between them; this server groups on the "
                f"columns it is given and answers one row per group. Ask for "
                f"the totals as their own query and combine them with UNION "
                f"ALL."
            )
        written.append(body)
        at = _skip_space(text, at)
        if text[at:at + 1] == ",":
            at = _skip_space(text, at + 1)
            continue
        break
    return tuple(written), at


# A GROUP BY that stands for several groupings at once, each with its own
# subtotal row.
_SEVERAL_GROUPINGS = re.compile(
    r"\s*(ROLLUP|CUBE|GROUPING\s+SETS)\s*\(", re.IGNORECASE
)


def _read_offset_fetch(text: str, at: int, ordered: bool) -> tuple[int, int | None, int]:
    """OFFSET n ROWS [FETCH NEXT m ROWS ONLY], which SQL Server pages with."""
    match = _OFFSET.match(text, at)
    if not match:
        return 0, None, at
    if not ordered:
        raise SqlError("OFFSET needs an ORDER BY, because otherwise there is "
                       "no defined order to skip through")
    if match.group(1).startswith("@"):
        raise SqlError("OFFSET must be a number here, not a parameter")
    offset = int(match.group(1))
    at = match.end()

    fetch = None
    fetch_match = _FETCH.match(text, at)
    if fetch_match:
        if fetch_match.group(1).startswith("@"):
            raise SqlError("FETCH must be a number here, not a parameter")
        fetch = int(fetch_match.group(1))
        at = fetch_match.end()
    return offset, fetch, at
