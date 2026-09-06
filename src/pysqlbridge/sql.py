"""Just enough SELECT to serve a table.

Not a SQL engine. It recognises the shape clients actually send to read a
table, and refuses everything else clearly rather than half-executing it:

    SELECT * FROM people
    SELECT TOP 100 id, name FROM [dbo].[people]
    SELECT "id" FROM mydb.dbo.people WHERE id = @id

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


class SqlError(Exception):
    """A statement this project cannot answer."""


@dataclass(frozen=True)
class Select:
    """A parsed read of one table."""

    table: str
    schema: str | None = None
    columns: list[str] | None = None   # None means every column
    top: int | None = None
    top_parameter: str | None = None   # TOP (@n), resolved at execution
    where: object | None = None

    @property
    def is_star(self) -> bool:
        return self.columns is None

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
    columns: list[str] | None
    if text[at:at + 1] == "*":
        columns = None
        at += 1
    else:
        columns = []
        while True:
            _, name, at = _read_qualified_name(text, at)
            columns.append(name)
            at = _skip_space(text, at)
            if text[at:at + 1] == ",":
                at = _skip_space(text, at + 1)
                continue
            break

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
        condition = text[where_match.end():].strip()
        # The condition runs to the end of the statement, so anything this
        # parser does not support inside it surfaces as a condition error
        # rather than as unexplained trailing text.
        try:
            where = parse_predicate(condition)
        except PredicateError as exc:
            raise SqlError(f"cannot read the WHERE condition: {exc}") from exc
        at = len(text)

    trailing = text[at:].strip()
    if trailing:
        # Naming the clause is what makes this useful: a client whose ORDER BY
        # was dropped should learn that, not receive rows in another order.
        clause = trailing.split(None, 1)[0].upper()
        raise SqlError(
            f"{clause} is not supported; this server can read a table with a "
            f"column list, TOP and WHERE"
        )

    return Select(
        table=table,
        schema=schema,
        columns=columns,
        top=top,
        top_parameter=top_parameter,
        where=where,
    )
