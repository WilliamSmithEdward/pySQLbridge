"""The tables a connection can see, and the handler that answers from them.

This is where the three halves meet: sql.py decides what was asked, source.py
supplies the data, and tds.result puts it on the wire. Keeping the join here
means neither of the other two has to know about the third.

Table names match case-insensitively, because SQL Server's default collation
does and a client that round-trips a name through its own interface may not
preserve the case the file had.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import information_schema
from .predicate import PredicateError, matches
from .source import SourceError, Table, from_csv, from_json
from .sql import SqlError, parse_select
from .tds.result import Query, QueryError, QueryResult

# SQL Server's "invalid object name". Clients already know how to present it,
# and a missing table here is the same thing to a user.
INVALID_OBJECT_NAME = 208

# Where SQL Server's user-defined range starts. Unsupported syntax is this
# project's own complaint rather than one of the server's.
UNSUPPORTED = 50000

# SQL Server's "could not find stored procedure". A client that asked for one
# and got silence has no way to tell that from an empty answer.
STORED_PROCEDURE_NOT_FOUND = 2812


@dataclass
class Catalog:
    """The set of tables served to clients."""

    tables: dict[str, Table] = field(default_factory=dict)

    def add(self, table: Table) -> None:
        key = table.name.lower()
        if key in self.tables:
            raise SourceError(
                f"two sources are both called '{table.name}'; table names must "
                f"be unique, so give one of them an explicit name"
            )
        self.tables[key] = table

    def views(self) -> dict[str, Table]:
        """The catalog views, rebuilt from whatever is currently served."""
        return information_schema.build(list(self.tables.values()))

    def get(self, name: str, schema: str | None = None) -> Table:
        if schema and schema.upper() == information_schema.SCHEMA_PREFIX:
            view = self.views().get(name.lower())
            if view is not None:
                return view
            available = ", ".join(sorted(v.name for v in self.views().values()))
            raise QueryError(
                f"invalid object name '{information_schema.SCHEMA_PREFIX}.{name}'. "
                f"This server has: {available}",
                number=INVALID_OBJECT_NAME,
            )
        try:
            return self.tables[name.lower()]
        except KeyError:
            known = ", ".join(sorted(t.name for t in self.tables.values())) or "none"
            raise QueryError(
                f"invalid object name '{name}'. This server has: {known}",
                number=INVALID_OBJECT_NAME,
            ) from None

    @property
    def names(self) -> list[str]:
        return sorted(table.name for table in self.tables.values())

    def answer(self, request: Query | str) -> QueryResult:
        """Handle one batch, as a query handler for a Connection.

        Anything that is not a SELECT completes without a result set. Clients
        open a session with setup batches, and a SET answered with columns
        makes them report an invalid cursor state on the real query.
        """
        query = Query(sql=request) if isinstance(request, str) else request
        statement = query.sql.lstrip()
        head = statement.upper()

        if head.startswith(("EXEC ", "EXECUTE ")):
            # Completing this silently is what produced a NullReferenceException
            # in a client: it asked for a procedure's result set and received
            # nothing, with no error to explain it.
            name = statement.split(None, 1)[1].split(None, 1)[0] if " " in statement else "?"
            raise QueryError(
                f"could not find stored procedure '{name.strip(',')}'",
                number=STORED_PROCEDURE_NOT_FOUND,
            )

        if not head.startswith("SELECT"):
            return QueryResult(columns=[], rows=[])

        try:
            select = parse_select(query.sql)
        except SqlError as exc:
            raise QueryError(str(exc), number=UNSUPPORTED) from exc

        table = self.get(select.table, select.schema)

        rows = table.rows
        if select.where is not None:
            # Filtering happens before projection, so a condition can name a
            # column the SELECT list does not.
            names = table.column_names
            try:
                rows = [
                    row for row in rows
                    if matches(select.where, dict(zip(names, row)), query.parameters)
                ]
            except PredicateError as exc:
                raise QueryError(str(exc), number=INVALID_OBJECT_NAME) from exc

        filtered = Table(name=table.name, columns=table.columns, rows=rows)
        try:
            columns, rows = filtered.select(select.columns)
        except SourceError as exc:
            raise QueryError(str(exc), number=INVALID_OBJECT_NAME) from exc

        try:
            limit = select.row_limit(query.parameters)
        except SqlError as exc:
            raise QueryError(str(exc), number=UNSUPPORTED) from exc
        if limit is not None:
            rows = rows[:limit]

        return QueryResult(columns=columns, rows=rows)


def load(config_path: str | Path) -> Catalog:
    """Build a catalog from a configuration file.

        {
          "tables": [
            {"name": "people", "csv":  "data/people.csv"},
            {"name": "cities", "json": "data/cities.json"}
          ]
        }

    Relative paths resolve against the configuration file's own directory, so
    a config and its data can be moved together.
    """
    path = Path(config_path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SourceError(f"could not read '{path}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SourceError(f"'{path}' is not valid JSON: {exc}") from exc

    entries = document.get("tables")
    if not isinstance(entries, list) or not entries:
        raise SourceError(f"'{path}' needs a non-empty \"tables\" array")

    catalog = Catalog()
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise SourceError(f"'{path}' table {position} is not an object")

        readers = {"csv": from_csv, "json": from_json}
        given = [key for key in readers if key in entry]
        if len(given) != 1:
            raise SourceError(
                f"'{path}' table {position} needs exactly one of "
                f"{', '.join(sorted(readers))}, found {len(given)}"
            )

        kind = given[0]
        source_path = (path.parent / entry[kind]).resolve()
        catalog.add(readers[kind](source_path, name=entry.get("name")))

    return catalog
