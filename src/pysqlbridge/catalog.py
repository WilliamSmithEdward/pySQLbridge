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
import difflib
import json
import datetime
import re
import socket
from dataclasses import dataclass, field, replace
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
    ChildSource,
    HttpSource,
    Paging,
    StaticSource,
)
from .predicate import (
    CONTEXT,
    Deferred,
    PredicateError,
    aggregates_in,
    as_parameters,
    collated,
    columns_in,
    is_constant,
    matches,
    parse_expression,
    parse_predicate,
    result_kind,
    with_deferred,
)
from .source import SourceError, Table, from_csv, from_json, from_markup
from .sql import (
    SelectItem,
    SqlError,
    end_of_branch,
    parse_select,
    skip_quoted as _skip_quoted,
    statements as _statements,
    without_comments,
)
from .tds.result import (
    Bit,
    Column,
    DateTime,
    Float,
    Integer,
    NVarChar,
    Query,
    QueryError,
    QueryResult,
)

# The words a statement can begin with, which is how the end of an IF
# condition is found: T-SQL needs no semicolon between a condition and the
# statement it guards, and no condition ends with one of these.
STATEMENT_WORDS = frozenset({
    "SELECT", "EXEC", "EXECUTE", "SET", "DECLARE", "PRINT", "RETURN",
    "BEGIN", "WITH", "INSERT", "UPDATE", "DELETE", "RAISERROR", "THROW",
})

# A statement that produces rows, and one that gives a variable a value.
# DECLARE with no assignment leaves the variable null, which is what an
# undeclared parameter already answers, so it needs no handling of its own.
_READS = re.compile(r"\s*(SELECT|WITH)\b", re.IGNORECASE)
_IF = re.compile(r"\s*IF\s+", re.IGNORECASE)
_BEGIN = re.compile(r"\s*BEGIN\b", re.IGNORECASE)
_CREATE_TEMP = re.compile(
    r"\s*CREATE\s+TABLE\s+(#[A-Za-z0-9_@#$]+)\s*\((.*)\)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_DROP_TEMP = re.compile(r"\s*DROP\s+TABLE\s+(#[A-Za-z0-9_@#$]+)\s*$", re.IGNORECASE)
_INSERT_TEMP = re.compile(
    r"\s*INSERT\s+(?:INTO\s+)?(#[A-Za-z0-9_@#$]+)\s+(.*)$",
    re.IGNORECASE | re.DOTALL,
)
# What has to be run, as against a setup statement that can be ignored.
# Not only the reads: a session builds a table of its own before it reads it.
_RUNS = re.compile(
    r"\s*(SELECT|WITH|IF|EXEC|EXECUTE|CREATE|INSERT|DROP|BEGIN)\b",
    re.IGNORECASE,
)
_ELSE = re.compile(r"\s*ELSE\b", re.IGNORECASE)
# A block that says what to do when something in it fails.
_TRY = re.compile(r"\s*BEGIN\s+TRY\b", re.IGNORECASE)
_END_TRY = re.compile(r"\s*END\s+TRY\b", re.IGNORECASE)
_BEGIN_CATCH = re.compile(r"\s*BEGIN\s+CATCH\b", re.IGNORECASE)
_END_CATCH = re.compile(r"\s*END\s+CATCH\b", re.IGNORECASE)
# EXEC of a procedure that writes its answer back into a variable, which is
# how a client reads the registry: the value comes out through the last
# argument rather than as a row.
_EXEC_OUTPUT = re.compile(
    r"\s*EXEC(?:UTE)?\s+(?:\[?[A-Za-z0-9_]+\]?\.){0,2}"
    r"\[?(xp_instance_regread|xp_regread)\]?\s+(.*?)"
    r",\s*(@[A-Za-z0-9_@#$]+)\s+OUTPUT\s*$",
    re.IGNORECASE | re.DOTALL,
)

# What this answers when a client reads a registry value. There is no
# registry: nothing here was installed, and none of these settings exist to
# be read. Answering nothing at all is still the wrong shape, because the
# batch that asks is one statement and a client that cannot run it loses
# every other value in it, including the edition and the version. So the
# procedure runs and gives back null, which is what the batch is written to
# cope with, except where this server does know the answer.
REGISTRY_VALUES = {
    # Windows authentication only, which is the whole of what this does and
    # what SERVERPROPERTY('IsIntegratedSecurityOnly') already says.
    "LOGINMODE": 1,
    # Nothing is audited and nothing is logged, so there are no logs to keep.
    "AUDITLEVEL": 0,
    "NUMERRORLOGS": 0,
}
# DECLARE @v <type>, with or without a value after it. The type is worth
# keeping on its own: a variable that ends up null has nothing else to say
# what kind of column it makes, and a real server still knows.
_DECLARES = re.compile(
    r"\s*DECLARE\s+(@[A-Za-z0-9_@#$]+)\s+(?:AS\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*(?:\s*\([^)]*\))?)",
    re.IGNORECASE,
)

# Where the kinds a batch declared are kept, beside the values themselves.
# One reserved parameter, for the same reason the connection's details are
# one: what travels with a statement should travel with its parameters.
DECLARED = "@@__declared"

_EXEC_NAME = re.compile(
    r"\s*EXEC(?:UTE)?\s+([A-Za-z0-9_@#$.\[\]]+)\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_EXEC_LITERAL = re.compile(
    r"\s*EXEC(?:UTE)?\s*\(\s*N?'(.*)'\s*\)\s*$", re.IGNORECASE | re.DOTALL
)
# SET, DECLARE and SELECT all give a variable a value, and a client uses
# whichever suits: SSMS declares one and selects into it in the same breath.
_ASSIGNMENT = re.compile(
    r"\s*(?:SET|DECLARE|SELECT)\s+(@[A-Za-z0-9_@#$]+)\s*(?:AS\s+)?"
    r"(?:[A-Za-z_][A-Za-z0-9_]*\s*(?:\([^)]*\))?\s*)?=\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)

# The schema everything this serves is in, and the one a client writes when
# it qualifies a name at all.
DEFAULT_SCHEMA = "dbo"

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

# How many distinct answers one correlated subquery may need. It runs once
# per distinct value it is asked about rather than once per row, so a
# thousand rows sharing twelve keys cost twelve; this is the bound on a query
# that genuinely asks for a million different ones.
MAX_CORRELATED_ANSWERS = 10_000

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
    # 17.0.1000 packed the way a client unpacks it: major, minor, build.
    "@@MICROSOFTVERSION": (17 << 24) + (0 << 16) + 1000,
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
            shapes = [_safe_load(source) for source in sources]
        else:
            workers = min(len(sources), MAX_PARALLEL_LOADS)
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="pysqlbridge-source"
            ) as pool:
                shapes = list(pool.map(_safe_load, sources))

        # A response has now been seen, so any arrays inside its rows are
        # known and the tables they make can join the catalog.
        added = self._register_children(sources)
        return shapes + [_safe_load(source) for source in added]

    def _register_children(self, sources: list) -> list:
        """Add a table for every array found inside a source's rows.

        Named parent_column. A name a person already gave to something else
        wins, because a configuration is a decision and this is an inference.
        """
        added = []
        for source in list(sources):
            try:
                found = source.children()
            except (SourceError, AttributeError):
                continue
            for column, table in found.items():
                if table.name.lower() in self.sources:
                    continue
                child = ChildSource(parent=source, column=column,
                                    name=table.name)
                self.sources[table.name.lower()] = child
                added.append(child)
        return added

    def views(self) -> dict[str, Table]:
        """The catalog views, rebuilt from whatever is currently served."""
        return information_schema.build(self.load_all())

    def get(self, name: str, schema: str | None = None,
            parameters: dict | None = None) -> Table:
        if schema and schema.upper() == information_schema.SYS_PREFIX:
            served = information_schema.system_views(procedures.CATALOG)
            view = served.get(name.lower())
            if view is not None:
                return view
            # The views that describe what is served rather than the server.
            # Built only when one is asked for, because building them loads
            # every source, and a client asking what edition this is should
            # not pull a CSV off disk to be told.
            about = (parameters or {}).get(CONTEXT) or {}
            built = information_schema.object_views(
                self.load_all(), about.get("login") or "")
            view = built.get(name.lower())
            if view is not None:
                return view
            raise QueryError(
                f"invalid object name 'sys.{name}'. This server has: "
                f"{', '.join(sorted(set(served) | set(built)))}",
                number=INVALID_OBJECT_NAME,
            )
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
        if schema and schema.lower() == DEFAULT_SCHEMA:
            # dbo is the schema everything here is in, so a name qualified
            # by it is the same name. A few system tables live there too,
            # and a client reads them while building its tree.
            served = information_schema.default_schema_views()
            if name.lower() in served:
                return served[name.lower()]

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

    def resolve(self, select, named=None, depth: int = 0,
                parameters=None) -> Table:
        """The table a SELECT reads from: joins, named queries and all."""
        if depth > MAX_NESTING:
            raise SourceError(
                f"this query nests more than {MAX_NESTING} deep; a named "
                f"query that refers to itself does that"
            )
        named = dict(named or {})

        if select.derived is not None:
            table = self.materialise(
                select.derived, named, depth + 1, select.table, parameters
            )
        else:
            table = self._named(select.table, select.schema, named,
                                parameters)

        if not select.joins:
            return _applied(table, select.applies)

        left = _renamed(table, select.alias or select.table)
        for join in select.joins:
            right = _renamed(
                self._named(join.table, join.schema, named, parameters),
                join.name
            )
            left = _join(left, right, join, parameters or {})
        return _unqualified(left)

    def _named(self, name: str, schema: str | None, named: dict,
               parameters: dict | None = None) -> Table:
        """A table by name, preferring one the query defined itself."""
        if not schema and name.lower() in named:
            return named[name.lower()]
        return self.get(name, schema, parameters)

    def materialise(self, select, named=None, depth: int = 0,
                    name: str = "", parameters=None) -> Table:
        """Run a SELECT and keep the answer as a table.

        This is what a CTE, a derived table and a subquery all reduce to. It
        goes through answer() so a named query is filtered, grouped and
        ordered exactly as the same text would be at the top level, and with
        the same parameters: a variable read inside one is the variable the
        statement around it declared.
        """
        result = self.answer(
            Query(sql="", parameters=dict(parameters or {})),
            select=select, named=named, depth=depth,
        )
        return Table(
            name=name or "subquery",
            columns=list(result.columns),
            rows=[list(row) for row in result.rows],
        )

    def _about(self, query) -> dict:
        """What a function that asks about the connection is told.

        The connection supplies who is asking; this supplies what they
        reached. Everything in it is something this server actually knows,
        so a client that asks is not told a story.
        """
        session = query.session or {}
        return {
            "login": session.get("login"),
            "app": session.get("app"),
            "host": session.get("host"),
            "server": session.get("server"),
            "database": procedures.CATALOG,
            "user": "dbo",
            "schema": "dbo",
            "tables": tuple(source.name for source in self.sources.values()),
        }

    def _combined(self, select, query, named, depth) -> QueryResult:
        """Answer a statement built from several SELECTs combined into one.

        Each part is answered on its own and the results are merged, which is
        what the operators mean. Three things belong to the statement rather
        than to any one part, and SQL Server puts all three at the end: the
        ORDER BY, the OFFSET and the FETCH. They are taken off the last part
        and applied to the whole.

        Column names come from the first part. UNION, EXCEPT and INTERSECT
        each drop repeated rows; only UNION ALL keeps them.
        """
        parts = _parts(select)
        last = parts[-1][1]
        answers = []
        for _, part in parts:
            alone = replace(part, combine=(), order_by=(), offset=0, fetch=None)
            answers.append(self._read(alone, query, named, depth + 1))

        columns = answers[0].columns
        rows = [list(row) for row in answers[0].rows]
        for (kind, _), answer in zip(parts[1:], answers[1:]):
            if len(answer.columns) != len(columns):
                raise QueryError(
                    f"all queries combined using a UNION, INTERSECT or EXCEPT "
                    f"operator must have an equal number of expressions in "
                    f"their target lists; this one has {len(columns)} and "
                    f"{len(answer.columns)}",
                    number=UNSUPPORTED,
                )
            other = [list(row) for row in answer.rows]
            if kind == "UNION ALL":
                rows = rows + other
            elif kind == "UNION":
                rows = _distinct(rows + other)
            else:
                theirs = {_signature(row) for row in other}
                wanted = kind == "INTERSECT"
                rows = _distinct(
                    [row for row in rows if (_signature(row) in theirs) == wanted]
                )

        if last.order_by:
            names = [column.name for column in columns]
            known = {name.lower() for name in names}
            for key in last.order_by:
                if key.position is None and key.column.lower() not in known:
                    raise QueryError(
                        f"ORDER BY items must appear in the select list if the "
                        f"statement contains a UNION, INTERSECT or EXCEPT "
                        f"operator; '{key.column}' does not",
                        number=UNSUPPORTED,
                    )
            try:
                rows = _sorted(rows, names, last.order_by,
                               parameters=query.parameters)
            except SourceError as exc:
                raise QueryError(str(exc), number=INVALID_OBJECT_NAME) from exc

        if last.offset:
            rows = rows[last.offset:]
        if last.fetch is not None:
            rows = rows[:last.fetch]
        return QueryResult(columns=columns, rows=rows)

    def _subqueries(self, select, named, depth, parameters=None):
        """Answer each subquery, or arrange for it to be answered per row.

        A subquery that names nothing outside itself has one answer for the
        whole statement, so it is run once here and bound to its parameter. A
        subquery that reads the row around it has an answer per row, so what
        goes into the expression is a value that will ask for one.

        Returns what was bound, what type each produced, and the per-row
        values still to be worked out.
        """
        bound: dict[str, object] = {}
        kinds: dict[str, type] = {}
        deferred: dict[str, object] = {}
        for subquery in select.subqueries:
            try:
                inner = parse_select(subquery.sql)
            except SqlError as exc:
                raise QueryError(str(exc), number=UNSUPPORTED) from exc

            outer = _reads_the_outer_row(inner)
            if outer:
                deferred[subquery.parameter.lstrip("@").lower()] = (
                    self._per_row(subquery, inner, outer, named, depth)
                )
                continue

            answer = self.answer(
                Query(sql=subquery.sql, parameters=dict(parameters or {})),
                select=inner, named=named, depth=depth + 1,
            )
            bound[subquery.parameter], kind = _one_answer(answer, subquery)
            if kind is not None:
                kinds[subquery.parameter] = kind
        return bound, kinds, deferred

    def _per_row(self, subquery, inner, outer, named, depth) -> Deferred:
        """A value that answers this subquery for whichever row it is shown.

        The references to the outer query are rewritten into parameters, so
        the subquery itself is an ordinary one against values handed to it.
        Answers are kept by the values they were asked about: a correlated
        subquery over a thousand rows with twelve distinct keys runs twelve
        times, and a bound stops a query that would run it a million.
        """
        names = {
            (column.qualified or column.name).lower(): f"@__outer_{at}"
            for at, column in enumerate(outer)
        }
        wanted = list(dict.fromkeys(names))          # in order, without repeats
        reading = replace(
            inner,
            where=as_parameters(inner.where, names),
            having=as_parameters(inner.having, names),
            items=as_parameters(inner.items, names),
            order_by=as_parameters(inner.order_by, names),
        )
        answers: dict[tuple, object] = {}

        def produce(row, parameters, _by=dict(zip(wanted, outer))):
            key = tuple(
                _by[name].evaluate(row, parameters or {}) for name in wanted
            )
            if key in answers:
                return answers[key]
            if len(answers) >= MAX_CORRELATED_ANSWERS:
                raise QueryError(
                    f"the subquery {subquery.sql!r} would be answered more "
                    f"than {MAX_CORRELATED_ANSWERS} times, once for each "
                    f"distinct value it was asked about",
                    number=UNSUPPORTED,
                )
            asked = dict(parameters or {})
            asked.update({names[name]: value for name, value in zip(wanted, key)})
            answer = self.answer(
                Query(sql=subquery.sql, parameters=asked), select=reading,
                named=named, depth=depth + 1,
            )
            answers[key] = _one_answer(answer, subquery)[0]
            return answers[key]

        return Deferred(name=subquery.parameter, produce=produce)

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

        statement = without_comments(query.sql).lstrip()
        head = statement.upper()

        if query.procedure:
            return self.call(query.procedure, query.arguments, query.parameters)

        if head.startswith(("EXEC ", "EXECUTE ")) and not _EXEC_LITERAL.match(statement):
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
        statements = _statements(statement)
        if not any(_RUNS.match(one) for one in statements):
            return QueryResult(columns=[], rows=[])
        if (len(statements) > 1 or not _READS.match(statements[0])
                or _ASSIGNMENT.match(statements[0])):
            # More than one statement, or one that has to be run rather than
            # read: an IF chooses between two, an EXEC of a string is a
            # statement written as text, and a SELECT into a variable begins
            # with the word a read begins with and produces no rows.
            return self._batch(statements, query)

        try:
            select = parse_select(statements[0])
        except SqlError as exc:
            raise QueryError(str(exc), number=UNSUPPORTED) from exc

        return self._read(select, query, _named(query.session), 0)

    def _batch(self, statements: list[str], query: Query) -> QueryResult:
        """Answer a batch of statements, of which one is the read.

        A client asks what it is talking to before it will show a table list,
        and it asks in one batch: declare a variable, set it from a server
        property, select something worked out from it. Nothing else in the
        batch produces rows, so the read is the answer.

        Variables live for the batch, which is as long as they live in a real
        server unless the connection declared them, and are handed to the
        read as parameters because that is what they are by then.
        """
        parameters = dict(query.parameters)
        answers: list[QueryResult] = []
        for one in statements:
            self._statement(one, parameters, answers, query.session)
        if not answers:
            return QueryResult(columns=[], rows=[])
        return replace(answers[0], following=tuple(answers[1:]))

    def _statement(self, written: str, parameters: dict,
                   answers: list, session: dict | None = None) -> None:
        """Run one statement of a batch, keeping what it produced.

        Everything a client sends before it will talk to a server: give a
        variable a value, read something, choose between two statements, or
        run one written as text.
        """
        declaration = _DECLARES.match(written)
        if declaration:
            kind = PYTHON_FOR.get(type(_declared_type(declaration.group(2))))
            if kind is not None:
                declared = dict(parameters.get(DECLARED) or {})
                declared[declaration.group(1)] = kind
                parameters[DECLARED] = declared

        assignment = _ASSIGNMENT.match(written)
        if assignment:
            name, expression = assignment.group(1), assignment.group(2)
            if self._assigned_from_a_read(name, expression, parameters, session):
                return
            try:
                parameters[name] = parse_expression(expression).evaluate(
                    {}, parameters
                )
            except PredicateError as exc:
                raise QueryError(str(exc), number=UNSUPPORTED) from exc
            return

        guarded = _TRY.match(written)
        if guarded:
            self._tried(written, guarded.end(), parameters, answers, session)
            return

        branch = _IF.match(written)
        if branch:
            taken = _branch_taken(
                written, branch.end(),
                lambda condition: self._condition_holds(
                    condition, parameters, session),
            )
            if taken is not None:
                for one in _block(taken):
                    self._statement(one, parameters, answers, session)
            return

        if session is not None and self._session_statement(written, parameters,
                                                           answers, session):
            return

        writes_back = _EXEC_OUTPUT.match(written)
        if writes_back:
            # The value goes into the variable named at the end. Which value
            # is asked for is the last argument before it.
            asked = _arguments(writes_back.group(2), parameters)
            wanted = _text_of(asked[-1]) if asked else ""
            parameters[writes_back.group(3)] = REGISTRY_VALUES.get(
                wanted.strip().upper()
            )
            return

        run = _EXEC_LITERAL.match(written)
        if run:
            # EXEC with a string rather than a procedure name: the statement
            # to run is the text, doubled quotes and all.
            inner = run.group(1).replace("''", "'")
            for one in _statements(inner):
                self._statement(one, parameters, answers, session)
            return

        called = _EXEC_NAME.match(written)
        if called and procedures.known(called.group(1)):
            answers.append(self.call(
                called.group(1),
                _arguments(called.group(2) or "", parameters),
                parameters,
            ))
            return

        if not _READS.match(written):
            return
        try:
            select = parse_select(written)
        except SqlError as exc:
            raise QueryError(str(exc), number=UNSUPPORTED) from exc
        answers.append(self._read(
            select,
            Query(sql=written, parameters=parameters, session=session or {}),
            _named(session), 0,
        ))

    def _tried(self, written: str, at: int, parameters: dict,
               answers: list, session: dict | None) -> None:
        """Run a TRY block, and its CATCH if the TRY could not finish.

        A client writes one around a question this server may not be able to
        answer, and the answer to a question that cannot be answered is the
        CATCH. Everything the TRY produced before it failed is dropped, the
        way a real server drops it.
        """
        body, at = _up_to(written, at, _END_TRY)
        caught, _ = _up_to(written, _BEGIN_CATCH.match(written, at).end(),
                           _END_CATCH) if _BEGIN_CATCH.match(written, at) else ("", at)

        so_far = len(answers)
        try:
            for one in _statements(body):
                self._statement(one, parameters, answers, session)
            return
        except QueryError:
            del answers[so_far:]
        for one in _statements(caught):
            self._statement(one, parameters, answers, session)

    def _condition_holds(self, condition: str, parameters: dict,
                         session: dict | None) -> bool | None:
        """Whether an IF's condition is true, reading something if it must.

        IF EXISTS (SELECT ...) asks a question of the tables rather than of
        the values to hand, and the parser is what says which kind this is:
        read as a select list, a condition holding a subquery is a read.
        """
        try:
            asked = parse_select(f"SELECT CASE WHEN {condition} THEN 1 ELSE 0 END AS v")
        except SqlError:
            asked = None
        if asked is not None and asked.subqueries:
            answer = self._read(
                asked,
                Query(sql=condition, parameters=parameters,
                      session=session or {}),
                _named(session), 0,
            )
            return bool(answer.rows and answer.rows[0][0] == 1)
        try:
            return matches(parse_predicate(condition), {}, parameters)
        except PredicateError as exc:
            raise QueryError(str(exc), number=UNSUPPORTED) from exc

    def _assigned_from_a_read(self, name: str, expression: str,
                              parameters: dict, session: dict | None) -> bool:
        """Give a variable a value a read produced, or say it is not one.

        SELECT @v = something FROM a table, and SET @v = (SELECT ...), both
        have to run a query before there is a value to assign. Whether this
        is one of those is the parser's answer rather than a guess from the
        text: read as a select list, an assignment that names a table or
        holds a subquery is a read, and anything else is an expression.

        The variable ends up holding the value from the last row the read
        produced, and keeps what it had when the read produced none. That is
        what makes the two forms differ where SQL Server has them differ:
        SELECT @v = c FROM t matching no rows leaves the variable alone,
        while SET @v = (SELECT c FROM t) matching none sets it to null,
        because the select around the subquery still has a row and null is
        what is in it.
        """
        try:
            select = parse_select(f"SELECT {expression}")
        except SqlError:
            return False                  # the expression route reports why
        if not (select.table or select.subqueries):
            return False
        answer = self._read(
            select,
            Query(sql=expression, parameters=parameters, session=session or {}),
            _named(session), 0,
        )
        if answer.rows:
            parameters[name] = answer.rows[-1][0]
        return True

    def _session_statement(self, written: str, parameters: dict,
                           answers: list, session: dict) -> bool:
        """A statement about a table this session made, or False for the rest.

        A temp table is the one thing a read-only bridge writes: it is the
        client's own scratch, it lives on the connection that made it, and it
        goes when that connection does. Nothing a source holds is touched.
        """
        made = _CREATE_TEMP.match(written)
        if made:
            session[made.group(1).lower()] = Table(
                name=made.group(1),
                columns=_declared_columns(made.group(2)),
                rows=[],
            )
            return True

        dropped = _DROP_TEMP.match(written)
        if dropped:
            session.pop(dropped.group(1).lower(), None)
            return True

        into = _INSERT_TEMP.match(written)
        if into:
            name, rest = into.group(1).lower(), into.group(2)
            table = session.get(name)
            if table is None:
                raise QueryError(
                    f"invalid object name '{into.group(1)}'; it was not "
                    f"created on this connection",
                    number=INVALID_OBJECT_NAME,
                )
            produced = self._rows_for(rest, parameters, session)
            session[name] = replace(
                table, rows=table.rows + _fitted(produced, table.columns)
            )
            return True
        return False

    def _rows_for(self, written: str, parameters: dict,
                  session: dict) -> QueryResult:
        """The rows a statement produces, for something else to keep."""
        gathered: list = []
        self._statement(written, parameters, gathered, session)
        if not gathered:
            raise QueryError(
                f"{written[:40]!r} produced no rows to insert",
                number=UNSUPPORTED,
            )
        return gathered[0]

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
        parameters = {
            name: value for name, value in SERVER_VARIABLES.items()
            if value is not None
        }
        parameters["@@SERVERNAME"] = (
            self._about(query).get("server") or socket.gethostname()
        )
        parameters[CONTEXT] = self._about(query)
        parameters.update(query.parameters)

        named = dict(named or {})
        for name, definition in select.ctes:
            try:
                named[name.lower()] = self.materialise(
                    definition, named, depth + 1, name, parameters
                )
            except SourceError as exc:
                raise QueryError(str(exc), number=INVALID_OBJECT_NAME) from exc

        if select.combine:
            return self._combined(select, query, named, depth)

        # What the batch declared comes first; a subquery's own answer is
        # better evidence than a declaration and overwrites it.
        produced: dict[str, type] = dict(parameters.get(DECLARED) or {})
        if select.subqueries:
            answers, produced, deferred = self._subqueries(
                select, named, depth, parameters
            )
            parameters.update(answers)
            if deferred:
                select = _asking_per_row(select, deferred)
        query = Query(sql=query.sql, parameters=parameters,
                      procedure=query.procedure,
                      arguments=list(query.arguments))

        if not select.table:
            # SELECT 1, or a function of nothing. One row, no columns to read.
            nothing = Table(name="", columns=[], rows=[[]])
            try:
                columns, rows = _evaluate(
                    nothing, select.items, query.parameters, produced,
                    select.alias or "",
                )
            except (SourceError, PredicateError) as exc:
                raise QueryError(str(exc), number=INVALID_OBJECT_NAME) from exc
            if select.where is not None:
                try:
                    if matches(select.where, {}, query.parameters) is not True:
                        rows = []
                except PredicateError as exc:
                    raise QueryError(str(exc), number=INVALID_OBJECT_NAME) from exc
            return QueryResult(columns=columns, rows=rows)

        try:
            table = self.resolve(select, named, depth, query.parameters)
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

        if select.is_grouped or select.has_aggregates or select.having is not None:
            try:
                # An aggregate the HAVING or the ORDER BY names is computed
                # for the group even when nothing asked to see it, and
                # dropped again below.
                asked = list(select.items)
                items = asked + _unlisted_aggregates(select, asked)
                if select.is_grouped:
                    columns, rows = aggregate.group(
                        table, rows, items, list(select.group_by),
                        parameters=query.parameters,
                    )
                else:
                    # No grouping means one group of everything, and one row
                    # out; ordering the input cannot change that.
                    columns, rows = aggregate.compute(
                        table, rows, items, parameters=query.parameters
                    )
            except SourceError as exc:
                raise QueryError(str(exc), number=INVALID_OBJECT_NAME) from exc

            # A grouped column answers to more than its heading, so a HAVING
            # and a sort can name it the way the query wrote it.
            lookup: dict[str, int] = {}
            for index, answers in enumerate(_group_names(items, columns)):
                for answer in answers:
                    lookup.setdefault(answer.lower(), index)

            if select.having is not None:
                rows = _having(select, items, columns, rows, query.parameters)
            if select.order_by:
                names = [column.name for column in columns]
                try:
                    rows = _sorted(rows, names, select.order_by,
                                   parameters=query.parameters, lookup=lookup)
                except SourceError as exc:
                    raise QueryError(
                        str(exc), number=INVALID_OBJECT_NAME
                    ) from exc
            rows = _page(select, rows, query.parameters)
            if len(items) > len(asked):
                columns = columns[:len(asked)]
                rows = [row[:len(asked)] for row in rows]
            return QueryResult(columns=columns, rows=rows)

        if select.order_by:
            try:
                rows = _sorted(rows, table.column_names, select.order_by,
                               items=select.items, parameters=query.parameters)
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
                columns, rows = _evaluate(
                    filtered, select.items, query.parameters, produced,
                    select.alias or "",
                )
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

        _only_known(
            entry, TABLE_KEYS, f"'{path}' table {position}",
            inside=(HTTP_KEYS, '"http"') if "http" in entry else None,
        )

        kind = given[0]
        if kind == "http":
            catalog.add_source(_http_source(entry, position, path))
        else:
            source_path = (path.parent / entry[kind]).resolve()
            catalog.add(readers[kind](source_path, name=entry.get("name")))

    return catalog


# What each part of a configuration reads. Anything else is refused rather
# than ignored: an option written one level too high, or spelled slightly
# wrong, is the one mistake a config file cannot recover from on its own,
# because the file looks right and the source behaves as though the line were
# not there.
TABLE_KEYS = frozenset({"name", "csv", "json", "xml", "html", "http"})
HTTP_KEYS = frozenset({
    "url", "name", "path", "records", "format", "expand", "flatten", "columns",
    "headers", "auth", "next", "paging", "max_pages", "max_rows", "timeout",
    "ttl",
})
PAGING_KEYS = frozenset({"key", "parameter", "step"})
DISCOVER_KEYS = frozenset({
    "url", "prefix", "headers", "auth", "guess", "concurrency", "max_requests",
    "max_depth", "max_pages", "max_rows", "expand", "timeout", "ttl",
})


def _only_known(spec: dict, known: frozenset, where: str,
                inside: tuple[frozenset, str] | None = None) -> None:
    """Refuse a key nobody reads, saying where the one meant would have gone.

    A key that is an option one level down is named as such, because writing
    an http option beside "http" rather than in it is the mistake this catches
    most often. Otherwise the nearest known key is offered.
    """
    for key in spec:
        if key in known:
            continue
        if inside and key in inside[0]:
            raise SourceError(
                f'{where}: "{key}" belongs inside {inside[1]}, not beside it'
            )
        near = difflib.get_close_matches(str(key), sorted(known), n=1)
        suggestion = f'; did you mean "{near[0]}"?' if near else ""
        raise SourceError(f'{where}: "{key}" is not an option here{suggestion}')


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

    _only_known(spec, HTTP_KEYS, f"{config} table {position}: http")

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

    expand = spec.get("expand", True)
    if not isinstance(expand, bool):
        raise SourceError(f"{config} table {position}: expand must be true or false")

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
        expand=expand,
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
    _only_known(spec, PAGING_KEYS, f"{where}: paging")

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

    _only_known(spec, DISCOVER_KEYS, where)

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
            expand=bool(spec.get("expand", True)),
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


def _branch_taken(written: str, at: int, holds) -> str | None:
    """Which statement an IF chooses, or None when it chooses neither.

    The condition runs to wherever the statement after it begins, which is a
    word no condition can end with. T-SQL needs no semicolon between the two,
    so nothing else marks the boundary.

    Whether the condition is true is decided by the caller, because deciding
    it may mean reading a table: IF EXISTS (SELECT ...) is a question about
    what is served rather than about the values to hand.

    The branch then runs to its own end, blocks and all, and only an ELSE
    directly after that belongs to this IF. Taking the first one instead
    handed the inner branch of a nested IF to the outer one, along with the
    END that closed the block around it.
    """
    start = _statement_start(written, at)
    if start is None:
        raise QueryError(
            f"cannot tell where the condition ends in {written[:40]!r}",
            number=UNSUPPORTED,
        )
    condition = written[at:start].strip()
    rest = written[start:]

    finish = end_of_branch(rest, 0)
    otherwise = _ELSE.match(rest, finish)
    if otherwise is None:
        taken, alternative = rest, None
    else:
        taken = rest[:finish]
        alternative = rest[otherwise.end():]

    if holds(condition) is True:
        return taken.strip()
    return alternative.strip() if alternative else None


def _up_to(written: str, at: int, ending) -> tuple[str, int]:
    """The text before a closing word, and where that word ended."""
    found = _statement_start(written, at, wanted=ending)
    if found is None:
        return written[at:], len(written)
    return written[at:found], ending.match(written, found).end()


def _statement_start(text: str, at: int, wanted=None) -> int | None:
    """Where the next statement begins, skipping anything quoted."""
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
            depth = max(0, depth - 1)
        elif depth == 0:
            word = re.compile(r"[A-Za-z_][A-Za-z0-9_]*").match(text, at)
            if word:
                if wanted is not None:
                    if wanted.match(text, at):
                        return at
                elif word.group(0).upper() in STATEMENT_WORDS:
                    return at
                at = word.end()
                continue
        at += 1
    return None


def _named(session: dict | None) -> dict:
    """The session's own tables, under the names a query calls them by."""
    return dict(session or {})


def _block(written: str) -> list:
    """The statements a branch holds, whether or not it is a BEGIN block."""
    stripped = written.strip()
    if _BEGIN.match(stripped) and stripped.upper().endswith("END"):
        inner = stripped[_BEGIN.match(stripped).end():-3]
        return _statements(inner)
    return [stripped]


def _declared_columns(written: str) -> list:
    """The columns a CREATE TABLE declared, in the order it declared them."""
    columns = []
    for one in _split_declarations(written):
        parts = one.split(None, 1)
        if not parts:
            continue
        name = parts[0].strip("[]\"")
        written_type = parts[1] if len(parts) > 1 else "nvarchar"
        columns.append(Column(name, _declared_type(written_type)))
    if not columns:
        raise QueryError("a table needs at least one column", number=UNSUPPORTED)
    return columns


def _split_declarations(written: str) -> list:
    """One column declaration per entry, ignoring commas inside brackets."""
    found, depth, start = [], 0, 0
    for at, char in enumerate(written):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            found.append(written[start:at])
            start = at + 1
    found.append(written[start:])
    return [one.strip() for one in found if one.strip()]


def _declared_type(written: str) -> object:
    """What a column declared as this holds, as far as this serves types."""
    name = written.strip().split("(")[0].strip().upper()
    size = re.search(r"\((\d+)\)", written)
    if name in ("INT", "INTEGER", "SMALLINT", "TINYINT"):
        return Integer(4)
    if name == "BIGINT":
        return Integer(8)
    if name in ("FLOAT", "REAL", "DECIMAL", "NUMERIC", "MONEY"):
        return Float(8)
    if name == "BIT":
        return Bit()
    if name == "SYSNAME":
        return NVarChar(128)
    return NVarChar(int(size.group(1)) if size else 4000)


def _fitted(produced, columns: list) -> list:
    """The rows a statement produced, laid against the columns they go into.

    By position, which is how INSERT works when it names no columns, and
    refused when the counts differ rather than padded with nulls.
    """
    if produced.columns and len(produced.columns) != len(columns):
        raise QueryError(
            f"the insert supplies {len(produced.columns)} columns and the "
            f"table has {len(columns)}",
            number=UNSUPPORTED,
        )
    return [list(row) for row in produced.rows]


def _reads_the_outer_row(inner) -> list:
    """The references a subquery makes to tables it does not read itself.

    A qualifier no table in the subquery answers to belongs to the query
    around it. Finding them is what makes a correlated subquery answerable:
    left alone the reference does not fail either, because a qualified name
    falls back to its bare form, and WHERE person_id = p.id would quietly
    become WHERE person_id = id and count the wrong thing.
    """
    scope = {name.lower() for name in (inner.table, inner.alias) if name}
    for join in inner.joins:
        scope |= {name.lower() for name in (join.table, join.alias) if name}

    found = []
    everywhere = columns_in(inner.where) + columns_in(inner.having) + [
        node for item in (inner.items or ()) for node in columns_in(item.node)
    ]
    for column in everywhere:
        if not column.qualified:
            continue
        qualifier = column.qualified.rsplit(".", 1)[0].lower()
        if qualifier and qualifier not in scope:
            found.append(column)
    return found


def _one_answer(answer, subquery) -> tuple:
    """What one answered subquery contributes, and the type it declared.

    IN takes the whole first column, a comparison takes one value, and
    EXISTS takes 1 or 0. A subquery standing where one value belongs and
    producing several is an error rather than a silent first row.
    """
    if subquery.kind == "exists":
        return (1 if answer.rows else 0), int
    if len(answer.columns) != 1:
        raise QueryError(
            f"a subquery used as a value must select one column, not "
            f"{len(answer.columns)}",
            number=UNSUPPORTED,
        )
    # What it declared, so a subquery that matched nothing still types the
    # column it stands in rather than leaving it text.
    kind = PYTHON_FOR.get(type(answer.columns[0].type))
    values = [row[0] for row in answer.rows]
    if subquery.kind == "in":
        return values, kind
    if len(values) > 1:
        raise QueryError(
            f"a subquery compared against one value returned {len(values)} rows",
            number=UNSUPPORTED,
        )
    return (values[0] if values else None), kind


def _asking_per_row(select, deferred: dict):
    """The same statement with its correlated subqueries left to be asked.

    Everywhere one can stand: a select-list entry, a condition, a sort key.
    """
    items = select.items
    if items is not None:
        items = tuple(
            replace(item, node=with_deferred(item.node, deferred))
            if item.node is not None else item
            for item in items
        )
    return replace(
        select,
        where=with_deferred(select.where, deferred),
        having=with_deferred(select.having, deferred),
        items=items,
        order_by=tuple(
            replace(key, node=with_deferred(key.node, deferred))
            if key.node is not None else key
            for key in select.order_by
        ),
    )


def _order_plan(keys: tuple, lookup: dict, items) -> list:
    """Where each ORDER BY key gets its value: a column, or an expression.

    Resolution follows SQL Server, measured against 2025 rather than recalled:

      * a bare number is a position in the select list, and one past its end
        is an error rather than a sort by that constant;
      * a bare name is looked for among the select list's aliases first, so
        SELECT c AS a, a AS other ORDER BY a sorts by c, and only then among
        the columns available to sort;
      * an expression is worked out per row and cannot see the aliases, which
        is why ORDER BY s + 'x' is an error where ORDER BY s is not;
      * an item that is the same for every row is refused, because a client
        that computed a constant into an ORDER BY meant something else.

    items is None for a grouped result, whose columns are already the select
    list: an alias is a column name there, and resolving it twice would let a
    grouped column shadow the aggregate beside it.

    Returns one entry per key: an index into the row, or a node to evaluate.
    """
    aliases = {}
    for item in items or ():
        if not item.star and item.alias:
            aliases.setdefault(item.alias.lower(), item)

    plans: list = []
    for at, key in enumerate(keys, start=1):
        if key.position is not None:
            plans.append(_position_plan(key, lookup, items))
            continue

        if key.node is not None:
            # An expression the result already holds as a column is that
            # column: a grouped result answers to COUNT(*) as written.
            written = lookup.get(key.column.lower())
            if written is not None:
                plans.append(written)
                continue
            if is_constant(key.node):
                raise SourceError(
                    f"a constant expression was encountered in the ORDER BY "
                    f"list, position {at}"
                )
            plans.append(key.node)
            continue

        item = aliases.get(key.column.lower())
        if item is not None:
            plans.append(_alias_plan(item, lookup, key))
            continue

        plans.append(_column_plan(key.column, lookup, key))
    return plans


def _position_plan(key, lookup: dict, items) -> object:
    """An ORDER BY that names a position rather than a column."""
    total = len(items) if items is not None else len(set(lookup.values()))
    if key.position < 1 or key.position > total:
        raise SourceError(
            f"the ORDER BY position number {key.position} is out of range of "
            f"the number of items in the select list"
        )
    if items is None:
        return key.position - 1
    return _alias_plan(items[key.position - 1], lookup, key)


def _alias_plan(item, lookup: dict, key) -> object:
    """How to get one select-list entry's value, given its own definition."""
    if item.is_computed:
        return item.node
    position = _found(lookup, item.expression)
    if position is None:
        # An aggregate has no source column to point at; its value is the
        # output column the alias named.
        position = _found(lookup, item.alias)
    if position is None:
        raise SourceError(f"invalid column name '{key.column}' in the ORDER BY")
    return position


def _found(lookup: dict, name: str | None) -> int | None:
    """Where a name lands, as written and then by its last part.

    A select list writes dtb.name where the table it reads has a column
    called name, and the qualifier is the alias it gave the table rather
    than part of the column.
    """
    if not name:
        return None
    wanted = name.lower()
    if wanted in lookup:
        return lookup[wanted]
    return lookup.get(wanted.rsplit(".", 1)[-1]) if "." in wanted else None


def _column_plan(name: str, lookup: dict, key) -> int:
    """A plain name, as written first and then by its last part.

    So a flattened team.name is found before anything is read as a table
    qualifier, and u.name still reaches the name column of a join.
    """
    wanted = name.lower()
    position = lookup.get(wanted)
    if position is None and "." in wanted:
        position = lookup.get(wanted.rsplit(".", 1)[-1])
    if position is None:
        raise SourceError(f"invalid column name '{key.column}' in the ORDER BY")
    return position


def _sorted(
    rows: list[list[object]], names: list[str], keys: tuple,
    items=None, parameters=None, lookup: dict | None = None,
) -> list[list[object]]:
    """Order rows by the ORDER BY keys.

    Applied before the projection, because a sort may name a column the SELECT
    list does not, and before TOP, because TOP takes the first rows of the
    sorted result rather than sorting whatever it happened to take.

    NULLs sort first ascending and last descending, which is what SQL Server
    does. The sort is stable and runs one key at a time from the last to the
    first, so each key's direction is honoured independently.
    """
    if lookup is None:
        lookup = {name.lower(): index for index, name in enumerate(names)}
    plans = _order_plan(keys, lookup, items)
    ordered = list(rows)

    for key, plan in reversed(list(zip(keys, plans))):
        if isinstance(plan, int):
            def value(row, i=plan):
                return row[i]
        else:
            def value(row, node=plan):
                try:
                    named = {name: row[index] for name, index in lookup.items()}
                    return node.evaluate(named, parameters or {})
                except PredicateError as exc:
                    raise SourceError(f"{exc} in the ORDER BY") from exc

        # The first element of the tuple separates NULLs from values, so the
        # second is only ever compared between two values of the same column.
        # Text sorts under the declared collation, which is case-insensitive:
        # a real server orders ada, alan, barbara, Edsger, Grace, where
        # sorting by code point puts the capitals first.
        ordered.sort(
            key=lambda row: (value(row) is not None, collated(value(row))),
            reverse=key.descending,
        )
    return ordered


def _applied(table: Table, applies: tuple) -> Table:
    """Each row of a table beside the rows an APPLY works out for it.

    CROSS APPLY over values written into the query: the values may name the
    columns of the row they are applied to, so they are worked out again for
    every row rather than once.
    """
    if not applies:
        return table
    for apply in applies:
        columns = list(table.columns) + [
            Column(f"{apply.alias}.{name}", NVarChar(1)) for name in apply.columns
        ]
        names = table.column_names
        built: list = []
        for row in table.rows:
            named = dict(zip(names, row))
            for written in apply.rows:
                try:
                    built.append(list(row) + [
                        one.evaluate(named, {}) for one in written
                    ])
                except PredicateError as exc:
                    raise SourceError(f"{exc} in {apply.alias}") from exc
        table = _typed(Table(name=table.name, columns=columns, rows=built),
                       len(table.columns))
    return table


def _typed(table: Table, from_column: int) -> Table:
    """The same table with the columns after this one typed from their values."""
    from .source import column_of

    columns = list(table.columns[:from_column])
    held = [list(row) for row in table.rows]
    for at in range(from_column, len(table.columns)):
        column, values = column_of(
            table.columns[at].name, [row[at] for row in held]
        )
        columns.append(column)
        for row, value in zip(held, values):
            row[at] = value
    return Table(name=table.name, columns=columns, rows=held)


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


def _join(left: Table, right: Table, join, parameters: dict | None = None) -> Table:
    """Match the two tables under the join's condition.

    The parameters go in because an ON condition may name one: a client
    compares a column against a value it bound, or against a subquery that
    was answered before the join ran. Evaluating the condition without them
    made every such comparison NULL and every row fall out.
    """
    parameters = parameters or {}
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
                if matches(join.on, dict(zip(names, combined)), parameters):
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
            if matches(join.on, dict(zip(names, combined)), parameters):
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


def _evaluate(
    table: Table, items, parameters, produced: dict | None = None,
    alias: str = "",
) -> tuple[list[Column], list[list[object]]]:
    """Work out a select list that is more than a projection.

    Stars expand to the table's own columns, plain names are read from the
    row, and anything computed is evaluated against it. Types come from the
    values produced, which is the same rule the sources are typed by: a
    column is whatever every value in it can be.
    """
    from .source import column_of

    names = table.column_names
    # What each column holds, so an expression over an empty table can still
    # be typed: score * 2 is a float whether or not a row survived the WHERE.
    holds = {
        column.name.lower(): PYTHON_FOR.get(type(column.type))
        for column in table.columns
    }
    headings: list[str] = []
    plans: list[object] = []
    for item in items:
        if item.star:
            wanted = _starred(names, item.expression, table.name, alias)
            # t.* names the columns of t, and a real server heads them with
            # the names they have there rather than with the qualifier.
            headings.extend(
                names[at].split(".", 1)[-1] if item.expression else names[at]
                for at in wanted
            )
            plans.extend(wanted)
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
            column, values = column_of(
                heading, [row[at] for row in built],
                kind=_kind_of(plans[at], produced or {}, holds),
            )
            columns.append(column)
            for row, value in zip(built, values):
                row[at] = value
    return columns, built


def _group_names(items, columns) -> list[list[str]]:
    """Every name one column of a grouped result answers to.

    Three per entry: what the query wrote for an aggregate, the alias if it
    gave one, and the column name for a grouped column. A client may write
    COUNT(*) or the alias it gave that count and mean the same thing, in a
    HAVING or in an ORDER BY.
    """
    names: list[list[str]] = []
    for item, column in zip(items, columns):
        answers = [column.name]
        if item.is_aggregate:
            answers.append(f"{item.function}({item.expression or '*'})")
        elif item.expression:
            answers.append(item.expression)
        names.append([answer for answer in answers if answer])
    return names


def _unlisted_aggregates(select, items: list) -> list:
    """The aggregates a HAVING or an ORDER BY names and the select list does not.

    SQL Server computes them for the group anyway: ORDER BY MAX(a) sorts by a
    value nobody asked to see, and HAVING MAX(a) > 3 keeps groups by one.
    They are appended to the select list, used, and dropped before the result
    goes out.
    """
    written = "{0}({1})"
    known = {
        written.format(item.function, item.expression or "*").lower()
        for item in items if item.is_aggregate
    }
    named = aggregates_in(select.having)
    for key in select.order_by:
        named += aggregates_in(key.node)

    extra = []
    for node in named:
        if node.key.lower() in known:
            continue
        known.add(node.key.lower())
        extra.append(SelectItem(
            function=node.function,
            expression=None if node.argument == "*" else node.argument,
        ))
    return extra


# What an expression produces, once a subquery has been answered, as the
# Python type the column builder speaks.
PYTHON_FOR = {Integer: int, Float: float, NVarChar: str, Bit: bool,
              DateTime: datetime.datetime}


def _kind_of(node, produced: dict, columns: dict | None = None) -> type | None:
    """What an expression produces, or None where it cannot be said.

    A subquery is a parameter by the time this runs, and result_kind cannot
    say what a parameter holds. This one can: it was answered a moment ago
    and its column said what it was.
    """
    kind = result_kind(node, columns)
    if kind not in (None, type(None)):
        return kind
    name = getattr(node, "name", None)
    return produced.get(name) if isinstance(name, str) else None


def _starred(names: list, qualifier: str | None, table: str = "",
             alias: str = "") -> list:
    """Which columns a star stands for: all of them, or one table's.

    t.* is every column t brought and nothing else, which is how a query
    reads one side of a join or the table an APPLY worked out. A join
    qualifies its columns and nothing else does, so the table this reads
    from answers to its own name with the columns that carry no qualifier.
    """
    if not qualifier:
        return list(range(len(names)))
    prefix = f"{qualifier.lower()}."
    found = [at for at, name in enumerate(names) if name.lower().startswith(prefix)]
    if found:
        return found
    # The name a query calls the table by, which is the alias where it gave
    # one and the table's own name where it did not. p.* is every column of
    # p either way.
    if qualifier.lower() in {(table or "").lower(), (alias or "").lower()}:
        return [at for at, name in enumerate(names) if "." not in name]
    raise SourceError(f"'{qualifier}.*' names nothing this query reads")


def _having(select, items, columns, rows, parameters):
    """Keep the groups the HAVING accepts.

    The condition is evaluated against the group's own row, under every name
    that column answers to, so HAVING COUNT(*) > 2 and HAVING n > 2 are the
    same condition when the query wrote COUNT(*) AS n.
    """
    keys = _group_names(items, columns)

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


def _parts(select) -> list:
    """The SELECTs a combined statement is made of, each with its operator.

    The first carries None, because nothing precedes it. A chain of three is
    parsed as a part holding a part, so this walks it flat.
    """
    parts = [(None, select)]
    while parts[-1][1].combine:
        kind, following = parts[-1][1].combine[0]
        parts.append((kind, following))
    return parts


def _signature(row: list) -> tuple:
    """What makes two rows the same row, under the declared collation."""
    return tuple(collated(value) for value in row)


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


def _text_of(value: object) -> str:
    """A value as the name it stands for, which is what a registry read asks."""
    return "" if value is None else str(value)


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
