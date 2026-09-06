"""Just enough SELECT to serve a table.

Not a SQL engine. It recognises the shape clients actually send to read a
table, and refuses everything else clearly rather than half-executing it:

    SELECT * FROM people
    SELECT TOP 100 id, name FROM [dbo].[people]
    SELECT "id" FROM mydb.dbo.people

Three details are not optional even for something this small. Identifiers
arrive bracketed, because that is what Excel, Power BI and SSMS generate rather
than the bare names a person would type. Names can be qualified up to three
parts, and only the last is the table. And matching is case-insensitive,
because SQL Server's default collation is and a client that round-trips a name
through its own UI may not preserve case.

Refusing loudly matters more here than coverage. A WHERE clause that parsed and
was then ignored would return every row and look like a working filter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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
_TOP = re.compile(r"\s*TOP\s+(?:\(\s*)?(\d+)\s*\)?\s*", re.IGNORECASE)
_FROM = re.compile(r"\s*FROM\s+", re.IGNORECASE)


class SqlError(Exception):
    """A statement this project cannot answer."""


@dataclass(frozen=True)
class Select:
    """A parsed read of one table."""

    table: str
    columns: list[str] | None = None   # None means every column
    top: int | None = None

    @property
    def is_star(self) -> bool:
        return self.columns is None


def _read_identifier(text: str, at: int) -> tuple[str, int]:
    match = _IDENTIFIER.match(text, at)
    if not match:
        raise SqlError(f"expected a name at {text[at:at + 20]!r}")
    if match.group("bracketed") is not None:
        return match.group("bracketed").replace("]]", "]"), match.end()
    if match.group("quoted") is not None:
        return match.group("quoted").replace('""', '"'), match.end()
    return match.group("bare"), match.end()


def _read_qualified_name(text: str, at: int) -> tuple[str, int]:
    """Read up to database.schema.table and keep the last part."""
    name, at = _read_identifier(text, at)
    parts = 1
    while at < len(text) and text[at] == ".":
        if parts >= 3:
            raise SqlError("a table name has at most three parts")
        name, at = _read_identifier(text, at + 1)
        parts += 1
    return name, at


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

    top = None
    top_match = _TOP.match(text, at)
    if top_match:
        top = int(top_match.group(1))
        at = top_match.end()

    at = _skip_space(text, at)
    columns: list[str] | None
    if text[at:at + 1] == "*":
        columns = None
        at += 1
    else:
        columns = []
        while True:
            name, at = _read_qualified_name(text, at)
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

    table, at = _read_qualified_name(text, from_match.end())

    trailing = text[at:].strip()
    if trailing:
        # Naming the clause is what makes this useful: a client that sent a
        # WHERE should learn it was not applied, not receive every row.
        clause = trailing.split(None, 1)[0].upper()
        raise SqlError(
            f"{clause} is not supported; this server can only read a whole "
            f"table, optionally with TOP and a column list"
        )

    return Select(table=table, columns=columns, top=top)
