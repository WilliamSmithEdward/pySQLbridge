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
import socket
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
from .predicate import PredicateError, collated, matches
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

# A join can multiply its inputs. This is the ceiling on what one will build
# before refusing, so a condition that matches everything against everything
# fails with a message rather than by exhausting memory.
MAX_JOIN_ROWS = 1_000_000

# How deep a query may nest its subqueries and named queries. A WITH that
# names itself is the case this catches, and it catches it with a message
# rather than with a stack overflow.
MAX_NESTING = 16

# What the @@ variables answer. Clients send these to work out what they are
# talking to, and refusing them makes a connection look broken over something
# that costs nothing to answer. The banner is SQL Server shaped because that
# is what a client parses, and names this project because a person reading it
# should not be misled.
SERVER_VARIABLES = {
    "@@VERSION": (
        "Microsoft SQL Server 2025 - 17.0.1000.0 (X64), served by pysqlbridge"
    ),
    "@@SERVERNAME": None,          # filled in per server below
    "@@SPID": 51,
    "@@LANGUAGE": "us_english",
    "@@MAX_PRECISION": 38,
    "@@NESTLEVEL": 0,
    "@@ROWCOUNT": 0,
    "@@TRANCOUNT": 0,
    "@@OPTIONS": 0,
}

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

    def resolve(self, select, named=None, depth: int = 0) -> Table:
        """The table a SELECT reads from: joins, named queries and all."""
        if depth > MAX_NESTING:
            raise SourceError(
                f"this query nests more than {MAX_NESTING} deep; a named "
                f"query that refers to itself does that"
            )
        named = dict(named or {})

        if select.derived is not None:
            table = self.materialise(
                select.derived, named, depth + 1, select.table
            )
        else:
            table = self._named(select.table, select.schema, named)

        if not select.joins:
            return table

        left = _renamed(table, select.alias or select.table)
        for join in select.joins:
            right = _renamed(
                self._named(join.table, join.schema, named), join.name
            )
            left = _join(left, right, join)
        return _unqualified(left)

    def _named(self, name: str, schema: str | None, named: dict) -> Table:
        """A table by name, preferring one the query defined itself."""
        if not schema and name.lower() in named:
            return named[name.lower()]
        return self.get(name, schema)

    def materialise(self, select, named=None, depth: int = 0,
                    name: str = "") -> Table:
        """Run a SELECT and keep the answer as a table.

        This is what a CTE, a derived table and a subquery all reduce to. It
        goes through answer() so a named query is filtered, grouped and
        ordered exactly as the same text would be at the top level.
        """
        result = self.answer(Query(sql=""), select=select, named=named,
                             depth=depth)
        return Table(
            name=name or "subquery",
            columns=list(result.columns),
            rows=[list(row) for row in result.rows],
        )

    def _subqueries(self, select, named, depth) -> dict:
        """Run each subquery and bind what it produced to its parameter.

        IN gets the whole first column, a comparison gets one value, and
        EXISTS gets 1 or 0. A subquery standing where one value belongs and
        producing several is an error rather than a silent first row.
        """
        bound: dict[str, object] = {}
        for subquery in select.subqueries:
            try:
                inner = parse_select(subquery.sql)
            except SqlError as exc:
                raise QueryError(str(exc), number=UNSUPPORTED) from exc
            answer = self.answer(Query(sql=subquery.sql), select=inner,
                                 named=named, depth=depth + 1)

            if subquery.kind == "exists":
                bound[subquery.parameter] = 1 if answer.rows else 0
                continue
            if len(answer.columns) != 1:
                raise QueryError(
                    f"a subquery used as a value must select one column, not "
                    f"{len(answer.columns)}",
                    number=UNSUPPORTED,
                )
            values = [row[0] for row in answer.rows]
            if subquery.kind == "in":
                bound[subquery.parameter] = values
            elif len(values) > 1:
                raise QueryError(
                    "a subquery compared against one value returned "
                    f"{len(values)} rows",
                    number=UNSUPPORTED,
                )
            else:
                bound[subquery.parameter] = values[0] if values else None
        return bound

    def call(self, name: str, arguments: list, parameters: dict) -> QueryResult:
        """Answer a catalog procedure call, or say the procedure is unknown."""
        if not procedures.known(name):
            raise QueryError(
                f"could not find stored procedure '{name}'",
                number=STORED_PROCEDURE_NOT_FOUND,
            )
        return procedures.run(name, self, arguments, parameters)

    def answer(self, request: Query | str, *, select=None, named=None,
               depth: int = 0) -> QueryResult:
        """Handle one batch, as a query handler for a Connection.

        Anything that is not a SELECT completes without a result set. Clients
        open a session with setup batches, and a SET answered with columns
        makes them report an invalid cursor state on the real query.
        """
        query = Query(sql=request) if isinstance(request, str) else request
        if select is not None:
            # Already parsed, because this is a named query or a subquery
            # being run on behalf of the statement that contains it.
            return self._read(select, query, named, depth)

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

        # WITH begins a read as much as SELECT does. Anything else is a
        # setup batch, and a SET answered with columns makes a client report
        # an invalid cursor state on the real query.
        if not head.startswith(("SELECT", "WITH ")):
            return QueryResult(columns=[], rows=[])

        try:
            select = parse_select(query.sql)
        except SqlError as exc:
            raise QueryError(str(exc), number=UNSUPPORTED) from exc

        return self._read(select, query, None, 0)

    def _read(self, select, query, named, depth) -> QueryResult:
        """Answer one parsed SELECT.

        The named queries are built first, because everything after can refer
        to them: a subquery in the WHERE as much as the FROM.
        """
        if depth > MAX_NESTING:
            raise QueryError(
                f"this query nests more than {MAX_NESTING} deep; a named "
                f"query that refers to itself does that",
                number=UNSUPPORTED,
            )
        named = dict(named or {})
        for name, definition in select.ctes:
            try:
                named[name.lower()] = self.materialise(
                    definition, named, depth + 1, name
                )
            except SourceError as exc:
                raise QueryError(str(exc), number=INVALID_OBJECT_NAME) from exc

        parameters = {
            name: value for name, value in SERVER_VARIABLES.items()
            if value is not None
        }
        parameters["@@SERVERNAME"] = socket.gethostname()
        parameters.update(query.parameters)
        if select.subqueries:
            parameters.update(self._subqueries(select, named, depth))
        query = Query(sql=query.sql, parameters=parameters,
                      procedure=query.procedure,
                      arguments=list(query.arguments))

        if not select.table:
            # SELECT 1, or a function of nothing. One row, no columns to read.
            nothing = Table(name="", columns=[], rows=[[]])
            try:
                columns, rows = _evaluate(nothing, select.items, query.parameters)
            except (SourceError, PredicateError) as exc:
                raise QueryError(str(exc), number=INVALID_OBJECT_NAME) from exc
            return QueryResult(columns=columns, rows=rows)

        try:
            table = self.resolve(select, named, depth)
        except SourceError as exc:
            raise QueryError(str(exc), number=INVALID_OBJECT_NAME) from exc

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

        if select.is_grouped or select.has_aggregates:
            try:
                if select.is_grouped:
                    columns, rows = aggregate.group(
                        table, rows, select.items, list(select.group_by)
                    )
                else:
                    # No grouping means one group of everything, and one row
                    # out; ordering the input cannot change that.
                    columns, rows = aggregate.compute(table, rows, select.items)
            except SourceError as exc:
                raise QueryError(str(exc), number=INVALID_OBJECT_NAME) from exc

            if select.having is not None:
                rows = _having(select, columns, rows, query.parameters)
            if select.order_by:
                names = [column.name for column in columns]
                try:
                    rows = _sorted(rows, names, select.order_by)
                except SourceError as exc:
                    raise QueryError(
                        str(exc), number=INVALID_OBJECT_NAME
                    ) from exc
            rows = _page(select, rows, query.parameters)
            return QueryResult(columns=columns, rows=rows)

        if select.order_by:
            try:
                rows = _sorted(rows, table.column_names, select.order_by)
            except SourceError as exc:
                raise QueryError(str(exc), number=INVALID_OBJECT_NAME) from exc

        filtered = Table(name=table.name, columns=table.columns, rows=rows)
        try:
            if select.items is None or select.is_projection:
                columns, rows = filtered.select(select.columns)
                if select.items is not None:
                    # A select list may rename what it selects.
                    columns = [
                        Column(item.output_name, column.type)
                        for item, column in zip(select.items, columns)
                    ]
            else:
                columns, rows = _evaluate(filtered, select.items, query.parameters)
        except SourceError as exc:
            raise QueryError(str(exc), number=INVALID_OBJECT_NAME) from exc
        except PredicateError as exc:
            raise QueryError(str(exc), number=INVALID_OBJECT_NAME) from exc

        if select.distinct:
            rows = _distinct(rows)

        rows = _page(select, rows, query.parameters)
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
        # As written first, then its last part, so a flattened team.name is
        # found before anything is read as a table qualifier and u.name still
        # reaches the name column of a join.
        wanted = key.column.lower()
        position = lookup.get(wanted)
        if position is None and "." in wanted:
            position = lookup.get(wanted.rsplit(".", 1)[-1])
        if position is None:
            raise SourceError(
                f"invalid column name '{key.column}' in the ORDER BY"
            )
        # The first element of the tuple separates NULLs from values, so the
        # second is only ever compared between two values of the same column.
        # Text sorts under the declared collation, which is case-insensitive:
        # a real server orders ada, alan, barbara, Edsger, Grace, where
        # sorting by code point puts the capitals first.
        ordered.sort(
            key=lambda row, i=position: (row[i] is not None, collated(row[i])),
            reverse=key.descending,
        )
    return ordered


def _renamed(table: Table, qualifier: str) -> Table:
    """A copy whose columns are all qualified by the table's name or alias."""
    return Table(
        name=qualifier,
        columns=[
            Column(f"{qualifier}.{column.name}", column.type)
            for column in table.columns
        ],
        rows=table.rows,
    )


def _unqualified(table: Table) -> Table:
    """Drop the qualifier from every column name that only one table has.

    A joined table keeps u.name and o.name apart, but a name only one side
    carries reads better as itself, and that is what a client writing
    SELECT status after joining expects to work.
    """
    bare: dict[str, int] = {}
    for column in table.columns:
        _, _, name = column.name.partition(".")
        bare[name.lower()] = bare.get(name.lower(), 0) + 1

    columns = []
    for column in table.columns:
        _, _, name = column.name.partition(".")
        columns.append(
            Column(name, column.type) if bare[name.lower()] == 1 else column
        )
    return Table(name=table.name, columns=columns, rows=table.rows)


def _equalities(condition, left: Table, right: Table):
    """The (left index, right index) pairs an ON condition joins on.

    Only top-level ANDs of column-to-column equality count. Anything else is
    left to be checked row by row, which is correct but slower, so the pairs
    found here are what make the join a hash rather than a loop.
    """
    from .predicate import And, Column as ColumnRef, Comparison

    pairs = []
    pending = [condition]
    while pending:
        node = pending.pop()
        if isinstance(node, And):
            pending.extend((node.left, node.right))
            continue
        if not isinstance(node, Comparison) or node.operator != "=":
            continue
        if not (isinstance(node.left, ColumnRef) and isinstance(node.right, ColumnRef)):
            continue
        for a, b in ((node.left, node.right), (node.right, node.left)):
            at_left = _index_of(left, a)
            at_right = _index_of(right, b)
            if at_left is not None and at_right is not None:
                pairs.append((at_left, at_right))
                break
    return pairs


def _index_of(table: Table, reference) -> int | None:
    """Where a reference lands in a table, or None if it is not this one."""
    for wanted in (reference.qualified, reference.name):
        if not wanted:
            continue
        for at, column in enumerate(table.columns):
            if column.name.lower() == wanted.lower():
                return at
            _, _, bare = column.name.partition(".")
            if bare.lower() == wanted.lower() and reference.qualified is None:
                return at
    return None


def _join(left: Table, right: Table, join) -> Table:
    """Match the two tables under the join's condition."""
    from .predicate import collated

    columns = list(left.columns) + list(right.columns)
    names = [column.name for column in columns]
    empty = [None] * len(right.columns)
    keep_unmatched = join.kind == "LEFT"

    if join.kind == "CROSS" or join.on is None:
        _check_size(len(left.rows) * len(right.rows), join)
        rows = [a + b for a in left.rows for b in right.rows]
        return Table(name=left.name, columns=columns, rows=rows)

    pairs = _equalities(join.on, left, right)
    rows: list[list[object]] = []

    if pairs:
        buckets: dict[tuple, list[list[object]]] = {}
        for row in right.rows:
            key = tuple(collated(row[at]) for _, at in pairs)
            if None in key:
                continue        # NULL never matches, not even itself
            buckets.setdefault(key, []).append(row)

        for row in left.rows:
            key = tuple(collated(row[at]) for at, _ in pairs)
            found = buckets.get(key, ()) if None not in key else ()
            matched = False
            for other in found:
                combined = row + other
                if matches(join.on, dict(zip(names, combined)), {}):
                    rows.append(combined)
                    matched = True
            if keep_unmatched and not matched:
                rows.append(row + empty)
            _check_size(len(rows), join)
        return Table(name=left.name, columns=columns, rows=rows)

    # No equality to hash on, so every pair is tried.
    _check_size(len(left.rows) * len(right.rows), join)
    for row in left.rows:
        matched = False
        for other in right.rows:
            combined = row + other
            if matches(join.on, dict(zip(names, combined)), {}):
                rows.append(combined)
                matched = True
        if keep_unmatched and not matched:
            rows.append(row + empty)
    return Table(name=left.name, columns=columns, rows=rows)


def _check_size(size: int, join) -> None:
    if size > MAX_JOIN_ROWS:
        raise SourceError(
            f"the join of '{join.table}' would produce more than "
            f"{MAX_JOIN_ROWS} rows; narrow it with a WHERE or a tighter ON"
        )


def _evaluate(table: Table, items, parameters) -> tuple[list[Column], list[list[object]]]:
    """Work out a select list that is more than a projection.

    Stars expand to the table's own columns, plain names are read from the
    row, and anything computed is evaluated against it. Types come from the
    values produced, which is the same rule the sources are typed by: a
    column is whatever every value in it can be.
    """
    from .source import infer_column

    names = table.column_names
    headings: list[str] = []
    plans: list[object] = []
    for item in items:
        if item.star:
            headings.extend(names)
            plans.extend(range(len(names)))
            continue
        if item.is_computed:
            headings.append(item.output_name)
            plans.append(item.node)
            continue
        at = table.index_of(item.expression or "")
        if at is None:
            raise SourceError(
                f"invalid column name '{item.expression}' in table '{table.name}'"
            )
        headings.append(item.output_name)
        plans.append(at)

    built: list[list[object]] = []
    for row in table.rows:
        named = None
        values = []
        for plan in plans:
            if isinstance(plan, int):
                values.append(row[plan])
                continue
            if named is None:
                named = dict(zip(names, row))
            values.append(plan.evaluate(named, parameters))
        built.append(values)

    columns = []
    for at, heading in enumerate(headings):
        if isinstance(plans[at], int):
            columns.append(Column(heading, table.columns[plans[at]].type))
        else:
            column, values = infer_column(heading, [row[at] for row in built])
            columns.append(column)
            for row, value in zip(built, values):
                row[at] = value
    return columns, built


def _having(select, columns, rows, parameters):
    """Keep the groups the HAVING accepts.

    The condition is evaluated against the group's own row, under three names
    for each entry: what the query wrote for an aggregate, the alias if it
    gave one, and the column name for a grouped column. A client may write
    HAVING COUNT(*) > 2 or HAVING n > 2 and mean the same thing.
    """
    keys: list[list[str]] = []
    for item, column in zip(select.items, columns):
        names = [column.name]
        if item.is_aggregate:
            names.append(f"{item.function}({item.expression or '*'})")
        elif item.expression:
            names.append(item.expression)
        keys.append([name for name in names if name])

    kept = []
    for row in rows:
        seen: dict[str, object] = {}
        for names, value in zip(keys, row):
            for name in names:
                seen.setdefault(name, value)
        try:
            if matches(select.having, seen, parameters):
                kept.append(row)
        except PredicateError as exc:
            raise QueryError(str(exc), number=INVALID_OBJECT_NAME) from exc
    return kept


def _distinct(rows: list[list[object]]) -> list[list[object]]:
    """Drop repeated rows, keeping the order they first appeared in.

    Compared under the declared collation, which is case-insensitive, so two
    rows differing only in case are one row.
    """
    from .predicate import collated

    seen: set[tuple] = set()
    kept = []
    for row in rows:
        signature = tuple(collated(value) for value in row)
        if signature in seen:
            continue
        seen.add(signature)
        kept.append(row)
    return kept


def _page(select, rows: list[list[object]], parameters) -> list[list[object]]:
    """Apply TOP and OFFSET/FETCH, after the sort rather than before.

    TOP 3 ... ORDER BY score DESC means the three highest scores, not three
    arbitrary rows put in order.
    """
    try:
        limit = select.row_limit(parameters)
    except SqlError as exc:
        raise QueryError(str(exc), number=UNSUPPORTED) from exc
    if limit is not None:
        rows = rows[:limit]
    if select.offset:
        rows = rows[select.offset:]
    if select.fetch is not None:
        rows = rows[:select.fetch]
    return rows


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
