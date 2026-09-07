"""The tables a connection can see, and the handler that answers from them.

This is where the three halves meet: sql.py decides what was asked, source.py
supplies the data, and tds.result puts it on the wire. Keeping the join here
means neither of the other two has to know about the third.

Table names match case-insensitively, because SQL Server's default collation
does and a client that round-trips a name through its own interface may not
preserve the case the file had.
"""

from __future__ import annotations

import concurrent.futures
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import aggregate, discover, information_schema, procedures
from .credentials import credential
from .http_source import (
    DEFAULT_MAX_PAGES,
    DEFAULT_MAX_ROWS,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TTL_SECONDS,
    FORMATS,
    STRATEGIES,
    HttpSource,
    Paging,
    StaticSource,
)
from .predicate import PredicateError, matches
from .source import SourceError, Table, from_csv, from_json, from_markup
from .sql import SqlError, parse_select
from .tds.result import Column, Query, QueryError, QueryResult

# SQL Server's "invalid object name". Clients already know how to present it,
# and a missing table here is the same thing to a user.
INVALID_OBJECT_NAME = 208

# Where SQL Server's user-defined range starts. Unsupported syntax is this
# project's own complaint rather than one of the server's.
UNSUPPORTED = 50000

# Loading sources is network wait, not work, so the pool can be wider than
# the machine has cores. Bounded anyway: a config with two hundred tables
# should not open two hundred sockets at once.
MAX_PARALLEL_LOADS = 12

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

    def warm(self) -> list[str]:
        """Fetch every source's shape now, so the first client does not wait.

        Returns the names that could not be loaded. They stay in the catalog:
        a source that is down at startup may be up by the first query.
        """
        failed = []
        for source, table in zip(self.sources.values(), self.load_all()):
            if not table.columns and source.name == table.name:
                failed.append(source.name)
        return failed

    def load_all(self) -> list[Table]:
        """Every table's shape, fetched at once rather than one after another.

        Measured on seven API sources: 1322 ms of fetching in sequence against
        724 ms for the slowest one alone. It is all network wait, so threads
        help even though the work is not CPU-bound.

        Shapes, not full tables. This feeds the catalog views and the startup
        warm, both of which want to know what exists and what its columns are.
        Reading every page of every source to answer that took 23 seconds on a
        catalog of 65 discovered tables, nearly all of it spent paginating
        collections nobody had asked for. A query goes through get(), which
        reads the whole table.

        A source that cannot be reached comes back as a table with no columns
        rather than raising, because one unreachable API should not hide every
        table that does work.
        """
        sources = list(self.sources.values())
        if len(sources) < 2:
            return [_safe_load(source) for source in sources]

        workers = min(len(sources), MAX_PARALLEL_LOADS)
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="pysqlbridge-source"
        ) as pool:
            return list(pool.map(_safe_load, sources))

    def views(self) -> dict[str, Table]:
        """The catalog views, rebuilt from whatever is currently served."""
        return information_schema.build(self.load_all())

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

    def shapes(self) -> list[Table]:
        """Every table's columns, in name order, for the catalog procedures."""
        return _sorted_by_name(self.load_all())

    def call(self, name: str, arguments: list, parameters: dict) -> QueryResult:
        """Answer a catalog procedure call, or say the procedure is unknown."""
        if not procedures.known(name):
            raise QueryError(
                f"could not find stored procedure '{name}'",
                number=STORED_PROCEDURE_NOT_FOUND,
            )
        return procedures.run(name, self, arguments, parameters)

    def answer(self, request: Query | str) -> QueryResult:
        """Handle one batch, as a query handler for a Connection.

        Anything that is not a SELECT completes without a result set. Clients
        open a session with setup batches, and a SET answered with columns
        makes them report an invalid cursor state on the real query.
        """
        query = Query(sql=request) if isinstance(request, str) else request
        statement = query.sql.lstrip()
        head = statement.upper()

        if query.procedure:
            return self.call(query.procedure, query.arguments, query.parameters)

        if head.startswith(("EXEC ", "EXECUTE ")):
            rest = statement.split(None, 1)[1] if " " in statement else ""
            name, _, written = rest.partition(" ")
            name = name.strip().strip(",")
            if procedures.known(name):
                return self.call(name, _arguments(written, query.parameters),
                                 query.parameters)
            # Completing this silently is what produced a NullReferenceException
            # in a client: it asked for a procedure's result set and received
            # nothing, with no error to explain it.
            raise QueryError(
                f"could not find stored procedure '{name or '?'}'",
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
            {"name": "cities", "json": "data/cities.json"},
            {"name": "pokemon", "http": "https://pokeapi.co/api/v2/pokemon"}
          ],
          "discover": [
            {"url": "https://pokeapi.co/api/v2/"}
          ]
        }

    Relative paths resolve against the configuration file's own directory, so
    a config and its data can be moved together.

    "discover" points at the base of an API and crawls it, which is the whole
    of the configuration for a server that describes itself. "tables" names
    sources one at a time, for the cases discovery cannot reach or gets wrong.
    Both may appear; a named table wins over a discovered one of the same name,
    because a person who wrote a name meant it.
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

    entries = document.get("tables") or []
    surfaces = document.get("discover") or []
    if not isinstance(entries, list):
        raise SourceError(f"'{path}': \"tables\" must be an array")
    if not isinstance(surfaces, list):
        raise SourceError(f"'{path}': \"discover\" must be an array")
    if not entries and not surfaces:
        raise SourceError(
            f"'{path}' needs a \"tables\" array, a \"discover\" array, or both"
        )

    catalog = Catalog()

    # Discovery runs first so that an explicitly named table overwrites a
    # discovered one rather than colliding with it.
    for position, surface in enumerate(surfaces, start=1):
        for discovered in _discovered_sources(surface, position, path):
            catalog.add_source(discovered)

    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise SourceError(f"'{path}' table {position} is not an object")

        readers = {
            "csv": from_csv,
            "json": from_json,
            "xml": lambda p, name=None: from_markup(p, "xml", name=name),
            "html": lambda p, name=None: from_markup(p, "html", name=name),
        }
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
    if isinstance(spec, list):
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

    records = spec.get("records", "auto")
    if records not in STRATEGIES:
        raise SourceError(
            f"{config} table {position}: '{records}' is not a records strategy; "
            f"use one of {', '.join(STRATEGIES)}"
        )

    document = spec.get("format", "auto")
    if document not in FORMATS:
        raise SourceError(
            f"{config} table {position}: '{document}' is not a format; use "
            f"one of {', '.join(FORMATS)}"
        )

    columns = spec.get("columns")
    if columns is not None and not (
        isinstance(columns, list) and all(isinstance(c, str) for c in columns)
    ):
        raise SourceError(
            f"{config} table {position}: columns must be a list of names"
        )

    paging = _paging_spec(spec.get("paging"), f"{config} table {position}")

    url = spec["url"]
    if isinstance(url, list):
        if not url or not all(isinstance(u, str) and u for u in url):
            raise SourceError(
                f"{config} table {position}: a list of urls must hold at least "
                f"one non-empty string"
            )
    elif not isinstance(url, str):
        raise SourceError(
            f"{config} table {position}: url must be a string, or a list of "
            f"them for a load-balanced set"
        )

    return HttpSource(
        name=name,
        url=url,
        path=spec.get("path"),
        records=records,
        format=document,
        next_key=spec.get("next"),
        paging=paging,
        max_pages=int(spec.get("max_pages", DEFAULT_MAX_PAGES)),
        max_rows=int(spec.get("max_rows", DEFAULT_MAX_ROWS)),
        flatten=bool(spec.get("flatten", True)),
        columns=columns,
        timeout=float(spec.get("timeout", DEFAULT_TIMEOUT_SECONDS)),
        ttl=float(spec.get("ttl", DEFAULT_TTL_SECONDS)),
        headers={str(k): str(v) for k, v in headers.items()},
        auth=credential(spec.get("auth"),
                        what=f"{config} table {position}: auth"),
    )


def _paging_spec(spec: object, where: str) -> Paging | None:
    """Read a paging rule from configuration.

        {"paging": {"key": "skip", "parameter": "skip", "step": 30}}

    Discovery writes this out for an API that reports its position instead of
    linking to the next page, so it has to read back in: a config anyone can
    regenerate is only useful if it can also be edited and reloaded.
    """
    if spec is None:
        return None
    if not isinstance(spec, dict) or "key" not in spec:
        raise SourceError(
            f'{where}: paging must be an object with a "key", and optionally '
            f'a "parameter" and a "step"'
        )
    key = str(spec["key"])
    try:
        step = int(spec.get("step", 1))
    except (TypeError, ValueError):
        raise SourceError(f"{where}: paging step must be a whole number") from None
    if step < 1:
        raise SourceError(f"{where}: paging step must be at least 1")
    return Paging(
        key=key,
        parameter=str(spec.get("parameter", key.rsplit(".", 1)[-1])),
        step=step,
    )


def _discovered_sources(spec: object, position: int, config: Path) -> list[HttpSource]:
    """Crawl one API surface and turn what it holds into sources.

        {"url": "https://pokeapi.co/api/v2/",
         "auth": {"bearer": "${API_TOKEN}"},
         "prefix": "poke", "max_requests": 60}

    This runs while the configuration is being read, which means starting the
    server costs one crawl. That is the right moment for it: a client asks for
    the table list immediately after connecting, and discovering the surface
    then would make the first query wait for a walk of somebody else API.
    """
    where = f"{config} discover {position}"
    if isinstance(spec, str):
        spec = {"url": spec}
    if not isinstance(spec, dict) or not isinstance(spec.get("url"), str):
        raise SourceError(
            f"{where}: needs a url, either as a string or as an object with "
            f"a url key"
        )

    headers = spec.get("headers") or {}
    if not isinstance(headers, dict):
        raise SourceError(f"{where}: headers must be an object")
    headers = {str(k): str(v) for k, v in headers.items()}

    auth = credential(spec.get("auth"), what=f"{where}: auth")
    prefix = str(spec.get("prefix", ""))
    found = discover.survey(
        spec["url"],
        auth=auth,
        headers=headers,
        max_requests=int(spec.get("max_requests", discover.DEFAULT_MAX_REQUESTS)),
        max_depth=int(spec.get("max_depth", discover.DEFAULT_MAX_DEPTH)),
        concurrency=int(spec.get("concurrency", discover.DEFAULT_CONCURRENCY)),
        timeout=float(spec.get("timeout", DEFAULT_TIMEOUT_SECONDS)),
        guess=bool(spec.get("guess", True)),
    )

    ttl = float(spec.get("ttl", DEFAULT_TTL_SECONDS))
    timeout = float(spec.get("timeout", DEFAULT_TIMEOUT_SECONDS))
    max_pages = int(spec.get("max_pages", DEFAULT_MAX_PAGES))
    max_rows = int(spec.get("max_rows", DEFAULT_MAX_ROWS))

    sources = []
    for resource in found.resources:
        source = HttpSource(
            name=f"{prefix}_{resource.name}" if prefix else resource.name,
            url=resource.url,
            path=resource.shape.path,
            records=resource.shape.records,
            next_key=resource.next_key,
            paging=resource.paging,
            max_pages=max_pages,
            max_rows=max_rows,
            headers=headers,
            auth=auth,
            timeout=timeout,
            ttl=ttl,
        )
        if resource.table is not None:
            # The crawl already fetched and shaped this one. Handing it over
            # saves the whole surface being fetched twice within a second.
            source.prime(resource.table)
        sources.append(source)
    return sources


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


def _sorted_by_name(tables: list[Table]) -> list[Table]:
    return sorted(tables, key=lambda t: t.name.lower())


def _arguments(written: str, bound: dict) -> list[object]:
    """The arguments of an EXEC written as text.

    A client that sends EXEC sp_columns 'people' rather than an RPC still has
    to be understood, and its arguments arrive as a comma-separated list of
    quoted strings, numbers and NULLs.

    A marker with nothing bound to it is no filter rather than a filter on the
    literal text: EXEC sp_columns @Table with no @Table supplied is asking for
    every table, and reading it as a table named "@Table" answers with none.
    """
    values: list[object] = []
    for piece in written.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "=" in piece and piece.lstrip().startswith("@"):
            piece = piece.split("=", 1)[1].strip()
        if piece.startswith("@"):
            values.append(_bound(piece, bound))
        elif piece.upper() == "NULL":
            values.append(None)
        elif piece[:1] in "'\"" or piece[:2].upper() == "N'":
            values.append(piece.lstrip("Nn").strip("'\""))
        else:
            try:
                values.append(int(piece))
            except ValueError:
                values.append(piece)
    return values


def _bound(marker: str, bound: dict) -> object:
    """What a client supplied for a parameter marker, if anything."""
    wanted = marker.lstrip("@").lower()
    for name, value in bound.items():
        if name.lstrip("@").lower() == wanted:
            return value
    return None


def _safe_load(source) -> Table:
    """Ask one source for its shape, turning a failure into an empty table."""
    try:
        return source.schema()
    except SourceError:
        return Table(name=source.name, columns=[], rows=[])
