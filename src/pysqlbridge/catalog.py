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

from . import aggregate, information_schema
from .http_source import (
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TTL_SECONDS,
    HttpSource,
    StaticSource,
)
from .predicate import PredicateError, matches
from .source import SourceError, Table, from_csv, from_json
from .sql import SqlError, parse_select
from .tds.result import Column, Query, QueryError, QueryResult

# SQL Server's "invalid object name". Clients already know how to present it,
# and a missing table here is the same thing to a user.
INVALID_OBJECT_NAME = 208

# Where SQL Server's user-defined range starts. Unsupported syntax is this
# project's own complaint rather than one of the server's.
UNSUPPORTED = 50000

# SQL Server's "could not find stored procedure". A client that asked for one
# and got silence has no way to tell that from an empty answer.
STORED_PROCEDURE_NOT_FOUND = 2812

# A source that exists but could not be read this time. Distinct from a
# missing table, because the fix is different: check the URL, not the
# spelling.
SOURCE_UNAVAILABLE = 50001


@dataclass
class Catalog:
    """The set of tables served to clients.

    Sources rather than tables, because an HTTP source refetches once its cache
    expires. Everything answers the same question, load(), so the catalog never
    has to know which kind it is holding.
    """

    sources: dict[str, object] = field(default_factory=dict)

    def add(self, table: Table) -> None:
        """Serve a table that was read once and will not change."""
        self.add_source(StaticSource(table))

    def add_source(self, source: object) -> None:
        key = source.name.lower()
        if key in self.sources:
            raise SourceError(
                f"two sources are both called '{source.name}'; table names must "
                f"be unique, so give one of them an explicit name"
            )
        self.sources[key] = source

    @property
    def tables(self) -> dict[str, Table]:
        """Every table, loading any that need it."""
        return {key: source.load() for key, source in self.sources.items()}

    def views(self) -> dict[str, Table]:
        """The catalog views, rebuilt from whatever is currently served.

        A source that cannot be reached is listed with no columns rather than
        failing the whole listing: one unreachable API should not hide every
        table that does work.
        """
        loaded: list[Table] = []
        for source in self.sources.values():
            try:
                loaded.append(source.load())
            except SourceError:
                loaded.append(Table(name=source.name, columns=[], rows=[]))
        return information_schema.build(loaded)

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
        source = self.sources.get(name.lower())
        if source is None:
            known = ", ".join(sorted(s.name for s in self.sources.values())) or "none"
            raise QueryError(
                f"invalid object name '{name}'. This server has: {known}",
                number=INVALID_OBJECT_NAME,
            )
        try:
            return source.load()
        except SourceError as exc:
            # Not a missing table. Saying so lets the user fix the URL rather
            # than hunt for a typo in the name.
            raise QueryError(
                f"table '{name}' could not be loaded: {exc}",
                number=SOURCE_UNAVAILABLE,
            ) from exc

    @property
    def names(self) -> list[str]:
        return sorted(source.name for source in self.sources.values())

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

        if select.has_aggregates:
            # One row out, so ordering the input cannot change the answer and
            # the sort is skipped rather than performed and discarded.
            try:
                columns, rows = aggregate.compute(table, rows, select.items)
            except SourceError as exc:
                raise QueryError(str(exc), number=INVALID_OBJECT_NAME) from exc
            return QueryResult(columns=columns, rows=rows)

        if select.order_by:
            try:
                rows = _sorted(rows, table.column_names, select.order_by)
            except SourceError as exc:
                raise QueryError(str(exc), number=INVALID_OBJECT_NAME) from exc

        filtered = Table(name=table.name, columns=table.columns, rows=rows)
        try:
            columns, rows = filtered.select(select.columns)
        except SourceError as exc:
            raise QueryError(str(exc), number=INVALID_OBJECT_NAME) from exc

        # A select list may rename what it selects.
        if select.items is not None:
            columns = [
                Column(item.output_name, column.type)
                for item, column in zip(select.items, columns)
            ]

        try:
            limit = select.row_limit(query.parameters)
        except SqlError as exc:
            raise QueryError(str(exc), number=UNSUPPORTED) from exc
        if limit is not None:
            # After the sort, not before: TOP 3 ... ORDER BY score DESC means
            # the three highest scores, not three arbitrary rows put in order.
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
        # utf-8-sig: a config written by Notepad or PowerShell carries a
        # byte order mark, and json.loads refuses one.
        document = json.loads(path.read_text(encoding="utf-8-sig"))
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
        kinds = sorted([*readers, "http"])
        given = [key for key in kinds if key in entry]
        if len(given) != 1:
            raise SourceError(
                f"'{path}' table {position} needs exactly one of "
                f"{', '.join(kinds)}, found {len(given)}"
            )

        kind = given[0]
        if kind == "http":
            catalog.add_source(_http_source(entry, position, path))
        else:
            source_path = (path.parent / entry[kind]).resolve()
            catalog.add(readers[kind](source_path, name=entry.get("name")))

    return catalog


def _http_source(entry: dict, position: int, config: Path) -> HttpSource:
    """Build an HTTP source from one configuration entry.

        {"name": "pokemon",
         "http": {"url": "https://pokeapi.co/api/v2/pokemon?limit=50",
                  "path": "results", "ttl": 300, "timeout": 30}}

    The URL may also be given as a bare string when nothing else is needed.
    """
    spec = entry["http"]
    if isinstance(spec, str):
        spec = {"url": spec}
    if not isinstance(spec, dict) or "url" not in spec:
        raise SourceError(
            f"{config} table {position}: http needs a url, either as a "
            f"string or as an object with a url key"
        )

    name = entry.get("name") or spec.get("name")
    if not name:
        raise SourceError(
            f"{config} table {position}: an http source needs a name, "
            f"because a URL has no obvious table name"
        )

    headers = spec.get("headers") or {}
    if not isinstance(headers, dict):
        raise SourceError(f"{config} table {position}: headers must be an object")

    return HttpSource(
        name=name,
        url=spec["url"],
        path=spec.get("path"),
        timeout=float(spec.get("timeout", DEFAULT_TIMEOUT_SECONDS)),
        ttl=float(spec.get("ttl", DEFAULT_TTL_SECONDS)),
        headers={str(k): str(v) for k, v in headers.items()},
    )


def _sorted(
    rows: list[list[object]], names: list[str], keys: tuple
) -> list[list[object]]:
    """Order rows by the ORDER BY keys.

    Applied before the projection, because a sort may name a column the SELECT
    list does not, and before TOP, because TOP takes the first rows of the
    sorted result rather than sorting whatever it happened to take.

    NULLs sort first ascending and last descending, which is what SQL Server
    does. The sort is stable and runs one key at a time from the last to the
    first, so each key's direction is honoured independently.
    """
    lookup = {name.lower(): index for index, name in enumerate(names)}
    ordered = list(rows)

    for key in reversed(keys):
        position = lookup.get(key.column.lower())
        if position is None:
            raise SourceError(
                f"invalid column name '{key.column}' in the ORDER BY"
            )
        # The first element of the tuple separates NULLs from values, so the
        # second is only ever compared between two values of the same column.
        ordered.sort(
            key=lambda row, i=position: (row[i] is not None, row[i]),
            reverse=key.descending,
        )
    return ordered
