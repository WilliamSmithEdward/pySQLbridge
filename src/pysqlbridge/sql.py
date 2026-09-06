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

from .predicate import PredicateError, parse_predicate

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
_TOP = re.compile(r"\s*TOP\s+(?:\(\s*)?(\d+|@[A-Za-z0-9_@#$]+)\s*\)?\s*", re.IGNORECASE)
_FROM = re.compile(r"\s*FROM\s+", re.IGNORECASE)
_WHERE = re.compile(r"\s*WHERE\s+", re.IGNORECASE)
_ORDER_BY = re.compile(r"\s*ORDER\s+BY\s+", re.IGNORECASE)
_DIRECTION = re.compile(r"\s*(ASC|DESC)\b", re.IGNORECASE)
_AS = re.compile(r"\s*AS\s+", re.IGNORECASE)

# Whole-table aggregates only. GROUP BY is refused, so each of these
# collapses the result to a single row.
AGGREGATES = frozenset({"COUNT", "SUM", "MIN", "MAX", "AVG"})

# Words that end a select-list item rather than alias it. Without this a
# bare FROM would be read as the alias of the column before it.
_NOT_ALIASES = frozenset({"FROM", "WHERE", "ORDER", "GROUP", "HAVING", "AS"})


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

    @property
    def is_aggregate(self) -> bool:
        return self.function is not None

    @property
    def output_name(self) -> str:
        """What the client sees as the column name.

        SQL Server leaves an un-aliased aggregate unnamed, and clients render
        that as a blank heading, so an empty string is the faithful answer
        rather than an invented one.
        """
        if self.alias:
            return self.alias
        return "" if self.is_aggregate else (self.expression or "")


@dataclass(frozen=True)
class OrderKey:
    """One column of an ORDER BY, and which way it runs."""

    column: str
    descending: bool = False


@dataclass(frozen=True)
class Select:
    """A parsed read of one table."""

    table: str
    schema: str | None = None
    items: tuple[SelectItem, ...] | None = None   # None means every column
    top: int | None = None
    top_parameter: str | None = None   # TOP (@n), resolved at execution
    where: object | None = None
    order_by: tuple[OrderKey, ...] = ()

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


def _find_order_by(text: str, start: int) -> int | None:
    """Where a top-level ORDER BY begins, or None.

    Scanned rather than matched with one expression, because a string literal
    or a bracketed name can contain the words and must not be split on them:
    WHERE note = 'order by tuesday' is a condition, not two clauses.
    """
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
        match = _ORDER_BY.match(text, at)
        if match and (at == start or text[at - 1].isspace() or text[at - 1] == ")"):
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
        _, name, at = _read_qualified_name(text, at)
        direction = _DIRECTION.match(text, at)
        descending = False
        if direction:
            descending = direction.group(1).upper() == "DESC"
            at = direction.end()
        keys.append(OrderKey(column=name, descending=descending))
        at = _skip_space(text, at)
        if text[at:at + 1] == ",":
            at = _skip_space(text, at + 1)
            continue
        break
    return tuple(keys), at


def _read_select_item(text: str, at: int) -> tuple[SelectItem, int]:
    """One select-list entry: a column, or an aggregate, either optionally aliased."""
    call = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(").match(text, at)

    function = None
    expression = None
    if call and call.group(1).upper() in AGGREGATES:
        function = call.group(1).upper()
        at = _skip_space(text, call.end())
        if text[at:at + 1] == "*":
            if function != "COUNT":
                raise SqlError(f"{function}(*) is not a thing; {function} needs a column")
            at = _skip_space(text, at + 1)
        else:
            _, expression, at = _read_qualified_name(text, at)
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
        _, expression, at = _read_qualified_name(text, at)

    alias, at = _read_alias(text, at)
    return SelectItem(expression=expression, function=function, alias=alias), at


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
    """Parse a single-table SELECT, or say why it cannot be answered."""
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise SqlError("empty statement")

    match = _SELECT.match(text)
    if not match:
        first = text.split(None, 1)[0]
        raise SqlError(f"only SELECT is supported, not {first.upper()}")
    at = match.end()

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
    if text[at:at + 1] == "*":
        items = None
        at += 1
    else:
        items, at = _read_select_list(text, at)

    from_match = _FROM.match(text, at)
    if not from_match:
        rest = text[at:at + 30].strip()
        raise SqlError(
            f"expected FROM after the column list, found {rest!r}"
            if rest else "a SELECT here needs a FROM and a table"
        )

    schema, table, at = _read_qualified_name(text, from_match.end())

    where = None
    where_match = _WHERE.match(text, at)
    if where_match:
        start = where_match.end()
        # The condition ends where a top-level ORDER BY begins, or at the end
        # of the statement. Handing the whole tail to the predicate parser
        # would make "ORDER" look like a column name.
        end = _find_order_by(text, start)
        condition = text[start:end if end is not None else len(text)].strip()
        try:
            where = parse_predicate(condition)
        except PredicateError as exc:
            raise SqlError(f"cannot read the WHERE condition: {exc}") from exc
        at = end if end is not None else len(text)

    order_by: tuple[OrderKey, ...] = ()
    if _ORDER_BY.match(text, at):
        order_by, at = _read_order_by(text, at)

    trailing = text[at:].strip()
    if trailing:
        # Naming the clause is what makes this useful: a client whose ORDER BY
        # was dropped should learn that, not receive rows in another order.
        clause = trailing.split(None, 1)[0].upper()
        raise SqlError(
            f"{clause} is not supported; this server can read a table with a "
            f"column list, TOP, WHERE and ORDER BY"
        )

    if items is not None:
        aggregated = [item for item in items if item.is_aggregate]
        if aggregated and len(aggregated) != len(items):
            plain = next(i.expression for i in items if not i.is_aggregate)
            raise SqlError(
                f"'{plain}' is in the select list beside an aggregate but is not "
                f"aggregated itself, and GROUP BY is not supported"
            )

    return Select(
        table=table,
        schema=schema,
        items=items,
        top=top,
        top_parameter=top_parameter,
        where=where,
        order_by=order_by,
    )
