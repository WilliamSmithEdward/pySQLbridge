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
    Column as ColumnRef,
    PredicateError,
    parse_expression,
    parse_predicate,
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
_TOP = re.compile(r"\s*TOP\s+(?:\(\s*)?(\d+|@[A-Za-z0-9_@#$]+)\s*\)?\s*", re.IGNORECASE)
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
_AS = re.compile(r"\s*AS\s+", re.IGNORECASE)
_JOIN = re.compile(
    r"\s*(?:(INNER|LEFT|RIGHT|FULL|CROSS)\s+(?:OUTER\s+)?)?JOIN\s+",
    re.IGNORECASE,
)
_ON = re.compile(r"\s*ON\s+", re.IGNORECASE)

# The joins this can perform. RIGHT and FULL are refused rather than
# approximated: a client given the wrong rows has no way to notice.
JOIN_KINDS = frozenset({"INNER", "LEFT", "CROSS"})

# Each of these collapses a set of rows to one value: the whole table
# without a GROUP BY, or one group with it. Defined with the expressions,
# because a function call has to be recognised as an aggregate before
# anything can say whether its name is known.
AGGREGATES = AGGREGATE_NAMES

# Words that end a select-list item rather than alias it. Without this a
# bare FROM would be read as the alias of the column before it.
_NOT_ALIASES = frozenset({
    "FROM", "WHERE", "ORDER", "GROUP", "HAVING", "AS", "JOIN", "INNER",
    "LEFT", "RIGHT", "FULL", "CROSS", "ON", "OFFSET", "UNION", "EXCEPT",
    "INTERSECT", "FOR", "OPTION", "COMPUTE", "PIVOT", "UNPIVOT", "APPLY",
    "WITH", "GO",
})


class SqlError(Exception):
    """A statement this project cannot answer."""


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

    @property
    def is_aggregate(self) -> bool:
        return self.function is not None

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
        """
        if self.alias:
            return self.alias
        if self.is_aggregate or self.is_computed:
            # SQL Server leaves both unnamed. A client renders that as a blank
            # heading, which is the faithful answer rather than an invented
            # one, and a query that wants a name says AS.
            return ""
        return self.expression or ""


@dataclass(frozen=True)
class Subquery:
    """A SELECT that stands where a value or a set of them was expected."""

    parameter: str
    sql: str
    kind: str          # "in", "exists" or "scalar"


@dataclass(frozen=True)
class Join:
    """One joined table, and the condition that matches its rows."""

    table: str
    schema: str | None = None
    alias: str | None = None
    kind: str = "INNER"
    on: object | None = None

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
    ctes: tuple = ()                        # (name, SELECT) from a WITH
    subqueries: tuple[Subquery, ...] = ()
    joins: tuple[Join, ...] = ()
    items: tuple[SelectItem, ...] | None = None   # None means every column
    distinct: bool = False
    top: int | None = None
    top_parameter: str | None = None   # TOP (@n), resolved at execution
    where: object | None = None
    group_by: tuple[str, ...] = ()
    having: object | None = None
    order_by: tuple[OrderKey, ...] = ()
    offset: int = 0
    fetch: int | None = None

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
        return bool(self.items) and any(item.is_aggregate for item in self.items)

    @property
    def is_projection(self) -> bool:
        """Whether every entry is a plain column, which projects by position."""
        return self.items is not None and all(
            not item.is_computed and not item.star for item in self.items
        )

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.table}" if self.schema else self.table

    def row_limit(self, parameters: dict | None = None) -> int | None:
        """How many rows to return, resolving TOP (@n) against the parameters."""
        if self.top_parameter is None:
            return self.top
        wanted = self.top_parameter.lstrip("@").lower()
        for key, value in (parameters or {}).items():
            if key.lstrip("@").lower() == wanted:
                return None if value is None else int(value)
        raise SqlError(f"TOP refers to {self.top_parameter}, which was not supplied")


def _read_identifier(text: str, at: int) -> tuple[str, int]:
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


def _find_order_by(text: str, start: int, *, ends=None) -> int | None:
    """Where the next top-level clause begins, or None.

    Scanned rather than matched with one expression, because a string literal
    or a bracketed name can contain the words and must not be split on them:
    WHERE note = 'order by tuesday' is a condition, not two clauses.
    """
    global _CLAUSE_ENDS
    _CLAUSE_ENDS = ends or (_ORDER_BY, _GROUP_BY, _HAVING, _OFFSET)
    at = start
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
        boundary = at == start or text[at - 1].isspace() or text[at - 1] == ")"
        if boundary and any(
            pattern.match(text, at) for pattern in _CLAUSE_ENDS
        ):
            return at
        at += 1
    return None


def _read_order_by(text: str, at: int) -> tuple[tuple[OrderKey, ...], int]:
    match = _ORDER_BY.match(text, at)
    if not match:
        raise SqlError("expected ORDER BY")
    at = match.end()

    keys: list[OrderKey] = []
    while True:
        body, at = _read_order_item(text, at)
        if not body:
            raise SqlError("ORDER BY needs a column, a position or an expression")
        direction = _DIRECTION.match(text, at)
        descending = False
        if direction:
            descending = direction.group(1).upper() == "DESC"
            at = direction.end()
        keys.append(_order_key(body, descending))
        at = _skip_space(text, at)
        if text[at:at + 1] == ",":
            at = _skip_space(text, at + 1)
            continue
        break
    return tuple(keys), at


def _read_order_item(text: str, at: int) -> tuple[str, int]:
    """Everything up to the comma, direction or clause that ends this item."""
    start = _skip_space(text, at)
    at = start
    depth = 0
    cases = 0
    while at < len(text):
        char = text[at]
        if char == "'":
            at = _skip_quoted(text, at, "'")
            continue
        if char == "[":
            found = text.find("]", at)
            at = len(text) if found < 0 else found + 1
            continue
        if char == '"':
            at = _skip_quoted(text, at, '"')
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
                elif cases == 0 and upper in _ORDER_ITEM_ENDS:
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

    try:
        name, consumed = _read_reference(body, 0)
    except SqlError:
        name, consumed = "", -1
    if consumed >= 0 and _skip_space(body, consumed) == len(body):
        return OrderKey(column=name, descending=descending)

    try:
        node = parse_expression(body)
    except PredicateError as exc:
        raise SqlError(f"cannot read {body!r} in the ORDER BY: {exc}") from exc
    return OrderKey(column=body, descending=descending, node=node)


def _read_select_item(text: str, at: int) -> tuple[SelectItem, int]:
    """One select-list entry: a star, a column, an aggregate or an expression."""
    probe = _skip_space(text, at)
    if text[probe:probe + 1] == "*" and not _continues_expression(text, probe + 1):
        return SelectItem(star=True), probe + 1

    if _starts_expression(text, probe):
        return _read_expression_item(text, at)

    call = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(").match(text, at)
    if call and call.group(1).upper() not in AGGREGATES:
        # Not an aggregate, so the whole entry is an expression.
        return _read_expression_item(text, at)

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

        if text[at:at + 1] == "*":
            if function != "COUNT":
                raise SqlError(f"{function}(*) is not a thing; {function} needs a column")
            if distinct:
                raise SqlError("COUNT(DISTINCT *) is not a thing")
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
    elif call:
        raise SqlError(
            f"'{call.group(1)}' is not a function this server knows; it has "
            f"{', '.join(sorted(AGGREGATES))}"
        )
    else:
        start = _skip_space(text, at)
        expression, at = _read_reference(text, at)
        after = _skip_space(text, at)
        if _continues_expression(text, after):
            return _read_expression_item(text, start)

    alias, at = _read_alias(text, at)
    return SelectItem(expression=expression, function=function, alias=alias,
                      argument=argument, distinct=distinct), at


# What can follow a value and mean the entry is not finished.
_OPERATOR_AHEAD = re.compile(r"[-+*/%]|\|\|")

# What can begin an entry that is not a column reference: a literal, a
# bracketed sub-expression, a leading sign, or a word that opens a construct.
_LITERAL_AHEAD = re.compile(r"""[-+(]|\d|N?'|@@""", re.VERBOSE)
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
            at = _skip_quoted(text, at, char)
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


def _read_expression_item(text: str, at: int) -> tuple[SelectItem, int]:
    """An entry that has to be evaluated rather than projected."""
    body, at = _read_expression_text(text, at)
    written, alias = _split_alias(body)
    try:
        node = parse_expression(written)
    except PredicateError as exc:
        raise SqlError(f"cannot read {written!r} in the select list: {exc}") from exc
    return SelectItem(expression=written, alias=alias, node=node), at


def _read_expression_text(text: str, at: int) -> tuple[str, int]:
    """Everything up to the top-level comma or FROM that ends this entry."""
    start = _skip_space(text, at)
    at = start
    depth = 0
    cases = 0
    while at < len(text):
        char = text[at]
        if char == "'":
            at = _skip_quoted(text, at, "'")
            continue
        if char == "[":
            found = text.find("]", at)
            at = len(text) if found < 0 else found + 1
            continue
        if char == '"':
            at = _skip_quoted(text, at, '"')
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
                elif upper == "FROM" and cases == 0:
                    break
                at = word.end()
                continue
            if char == "," and cases == 0:
                break
        at += 1
    return text[start:at].strip(), at


def _skip_quoted(text: str, at: int, quote: str) -> int:
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
        return body[:at].strip(), _bare(body[at + 4:].strip())

    match = re.compile(
        r"(.*[^\s])\s+((?:\[[^\]]*\])|(?:\"[^\"]*\")|(?:[A-Za-z_][A-Za-z0-9_]*))$",
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


def _top_level_as(body: str) -> int | None:
    """Where a top-level " AS " sits in an entry, or None."""
    depth = 0
    at = 0
    while at < len(body):
        char = body[at]
        if char in "'\"":
            at = _skip_quoted(body, at, char)
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
    if name.startswith("[") and name.endswith("]"):
        return name[1:-1].replace("]]", "]")
    if name.startswith('"') and name.endswith('"'):
        return name[1:-1].replace('""', '"')
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


def _read_select_list(text: str, at: int) -> tuple[tuple[SelectItem, ...], int]:
    items: list[SelectItem] = []
    while True:
        item, at = _read_select_item(text, at)
        items.append(item)
        at = _skip_space(text, at)
        if text[at:at + 1] == ",":
            at = _skip_space(text, at + 1)
            continue
        break
    return tuple(items), at


def _skip_space(text: str, at: int) -> int:
    while at < len(text) and text[at].isspace():
        at += 1
    return at


def parse_select(sql: str) -> Select:
    """Parse a SELECT, or say why it cannot be answered."""
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise SqlError("empty statement")

    ctes: tuple = ()
    with_match = _WITH.match(text)
    if with_match:
        ctes, at = _read_ctes(text, with_match.end())
        text = text[at:].strip()

    match = _SELECT.match(text)
    if not match:
        first = text.split(None, 1)[0]
        raise SqlError(f"only SELECT is supported, not {first.upper()}")
    at = match.end()

    distinct_match = _DISTINCT.match(text, at)
    distinct = bool(distinct_match)
    if distinct_match:
        at = distinct_match.end()

    top = top_parameter = None
    top_match = _TOP.match(text, at)
    if top_match:
        found = top_match.group(1)
        if found.startswith("@"):
            top_parameter = found
        else:
            top = int(found)
        at = top_match.end()

    at = _skip_space(text, at)
    items: tuple[SelectItem, ...] | None
    items, at = _read_select_list(text, at)
    if len(items) == 1 and items[0].star:
        items = None                       # a bare star is every column

    from_match = _FROM.match(text, at)
    if not from_match:
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
        # probe a connection, and answering is cheaper than refusing.
        return Select(table="", items=items, distinct=distinct, top=top,
                      top_parameter=top_parameter)

    derived = None
    probe = _skip_space(text, from_match.end())
    if text[probe:probe + 1] == "(":
        inner, at = _read_bracketed(text, probe)
        derived = parse_select(inner)
        schema, table = None, ""
        alias, at = _read_table_alias(text, at)
        if not alias:
            raise SqlError("a subquery used as a table needs an alias")
        table = alias
    else:
        schema, table, at = _read_qualified_name(text, from_match.end())
        alias, at = _read_table_alias(text, at)
    joins, at = _read_joins(text, at)

    subqueries: list[Subquery] = []
    where = None
    where_match = _WHERE.match(text, at)
    if where_match:
        start = where_match.end()
        # The condition ends where a top-level ORDER BY begins, or at the end
        # of the statement. Handing the whole tail to the predicate parser
        # would make "ORDER" look like a column name.
        end = _find_order_by(text, start)
        condition = text[start:end if end is not None else len(text)].strip()
        condition, subqueries = _lift_subqueries(condition)
        try:
            where = parse_predicate(condition)
        except PredicateError as exc:
            raise SqlError(f"cannot read the WHERE condition: {exc}") from exc
        at = end if end is not None else len(text)

    group_by: tuple[str, ...] = ()
    group_match = _GROUP_BY.match(text, at)
    if group_match:
        group_by, at = _read_group_by(text, group_match.end())

    having = None
    having_match = _HAVING.match(text, at)
    if having_match:
        if not group_by:
            raise SqlError("HAVING needs a GROUP BY to have anything to filter")
        start = having_match.end()
        end = _find_order_by(text, start, ends=(_ORDER_BY, _OFFSET))
        condition = text[start:end if end is not None else len(text)].strip()
        try:
            having = parse_predicate(condition)
        except PredicateError as exc:
            raise SqlError(f"cannot read the HAVING condition: {exc}") from exc
        at = end if end is not None else len(text)

    order_by: tuple[OrderKey, ...] = ()
    if _ORDER_BY.match(text, at):
        order_by, at = _read_order_by(text, at)

    offset, fetch, at = _read_offset_fetch(text, at, bool(order_by))

    trailing = text[at:].strip()
    if trailing:
        # Naming the clause is what makes this useful: a client whose ORDER BY
        # was dropped should learn that, not receive rows in another order.
        clause = trailing.split(None, 1)[0].upper()
        raise SqlError(
            f"{clause} is not supported; this server can read a table with a "
            f"column list, TOP, WHERE and ORDER BY"
        )

    if items is None and group_by:
        raise SqlError("SELECT * cannot be grouped; name the columns instead")

    if items is not None and (group_by or any(i.is_aggregate for i in items)):
        # Every column that is not aggregated has to be grouped on, or the
        # value it would report is one row's out of many.
        # Compared on the last part as well as the whole, the way every
        # other reference resolves: SELECT name beside GROUP BY c.name is one
        # column named two ways, and refusing it would be refusing the query
        # a real server answers.
        grouped = {name.lower() for name in group_by}
        grouped |= {name.lower().rsplit(".", 1)[-1] for name in group_by}
        for item in items:
            if item.is_aggregate or item.expression is None:
                continue
            written = item.expression.lower()
            if written in grouped or written.rsplit(".", 1)[-1] in grouped:
                continue
            raise SqlError(
                f"'{item.expression}' is in the select list beside an "
                f"aggregate but is neither aggregated nor named in the "
                f"GROUP BY"
            )

    return Select(
        table=table,
        schema=schema,
        alias=alias,
        derived=derived,
        ctes=ctes,
        subqueries=tuple(subqueries),
        joins=joins,
        items=items,
        distinct=distinct,
        top=top,
        top_parameter=top_parameter,
        where=where,
        group_by=group_by,
        having=having,
        order_by=order_by,
        offset=offset,
        fetch=fetch,
    )


def _read_ctes(text: str, at: int) -> tuple[tuple, int]:
    """The named queries of a WITH, in the order they were written.

    Each is parsed on its own, so a CTE that refers to an earlier one works
    and one that refers to itself is a table this does not have rather than a
    recursion this cannot bound.
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
        named.append((name, parse_select(inner)))
        at = _skip_space(text, at)
        if text[at:at + 1] == ",":
            at += 1
            continue
        return tuple(named), at


def _read_bracketed(text: str, at: int) -> tuple[str, int]:
    """The contents of a balanced bracket group, and where it ended."""
    depth = 0
    start = at
    while at < len(text):
        char = text[at]
        if char in "'\"":
            at = _skip_quoted(text, at, char)
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
_BEFORE_SUBQUERY = re.compile(r"(?:\b(IN|EXISTS)\s*|([<>=!]+)\s*)$", re.IGNORECASE)


def _lift_subqueries(condition: str) -> tuple[str, list]:
    """Replace every bracketed SELECT with a parameter, keeping its text aside.

    Done before the condition is parsed, so the predicate parser never has to
    know what a catalog is: it sees a parameter, and the value bound to it is
    whatever the subquery produced.
    """
    found: list[Subquery] = []
    out = []
    at = 0
    while at < len(condition):
        char = condition[at]
        if char in "'\"":
            end = _skip_quoted(condition, at, char)
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
            out.append(condition[at:end])
            at = end
            continue

        before = _BEFORE_SUBQUERY.search("".join(out))
        keyword = (before.group(1) or "").upper() if before else ""
        kind = {"IN": "in", "EXISTS": "exists"}.get(keyword, "scalar")
        name = f"{_SUBQUERY_NAME}{len(found)}"
        found.append(Subquery(parameter=name, sql=inner, kind=kind))

        if kind == "exists":
            # EXISTS takes no operand, so it becomes a test on what it found.
            written = "".join(out)
            out = [written[:before.start()], f"{name} = 1"]
        elif kind == "in":
            out.append(f"({name})")
        else:
            out.append(name)
        at = end
    return "".join(out), found


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


def _read_joins(text: str, at: int) -> tuple[tuple[Join, ...], int]:
    """Every JOIN clause after the first table."""
    joins: list[Join] = []
    while True:
        match = _JOIN.match(text, at)
        if not match:
            break
        kind = (match.group(1) or "INNER").upper()
        if kind not in JOIN_KINDS:
            raise SqlError(
                f"{kind} JOIN is not supported; this server can do INNER, "
                f"LEFT and CROSS joins"
            )
        schema, table, at = _read_qualified_name(text, match.end())
        alias, at = _read_table_alias(text, at)

        on = None
        on_match = _ON.match(text, at)
        if on_match:
            start = on_match.end()
            end = _find_join_end(text, start)
            condition = text[start:end].strip()
            try:
                on = parse_predicate(condition)
            except PredicateError as exc:
                raise SqlError(f"cannot read the ON condition: {exc}") from exc
            at = end
        elif kind != "CROSS":
            raise SqlError(f"the JOIN of '{table}' needs an ON condition")

        joins.append(Join(table=table, schema=schema, alias=alias, kind=kind,
                          on=on))
    return tuple(joins), at


def _find_join_end(text: str, start: int) -> int:
    """Where an ON condition stops: the next JOIN, or the next clause."""
    end = _find_order_by(
        text, start, ends=(_JOIN, _WHERE, _ORDER_BY, _GROUP_BY, _HAVING, _OFFSET)
    )
    return end if end is not None else len(text)


def _read_group_by(text: str, at: int) -> tuple[tuple[str, ...], int]:
    """The columns a GROUP BY names."""
    names: list[str] = []
    while True:
        name, at = _read_reference(text, at)
        names.append(name)
        at = _skip_space(text, at)
        if text[at:at + 1] == ",":
            at = _skip_space(text, at + 1)
            continue
        break
    return tuple(names), at


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
