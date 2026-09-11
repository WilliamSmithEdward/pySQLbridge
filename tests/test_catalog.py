import datetime
import json
import pathlib
import re
import socket

import pytest

from pysqlbridge import procedures
from pysqlbridge.catalog import (
    INVALID_OBJECT_NAME,
    NO_SUCH_COLUMN,
    UNSUPPORTED,
    Catalog,
    load,
)
from pysqlbridge.source import SourceError, from_records
from pysqlbridge.tds.result import Query, QueryError


def catalog() -> Catalog:
    c = Catalog()
    c.add(from_records([{"id": 1, "name": "ada"}, {"id": 2, "name": "grace"}],
                       name="people"))
    c.add(from_records([{"city": "Oslo"}], name="cities"))
    return c


class TestAnswering:
    def test_select_star_returns_the_table(self):
        result = catalog().answer("SELECT * FROM people")
        assert [c.name for c in result.columns] == ["id", "name"]
        assert result.rows == [[1, "ada"], [2, "grace"]]

    def test_columns_are_projected(self):
        result = catalog().answer("SELECT name FROM people")
        assert [c.name for c in result.columns] == ["name"]
        assert result.rows == [["ada"], ["grace"]]

    def test_top_limits_the_rows(self):
        assert len(catalog().answer("SELECT TOP 1 * FROM people").rows) == 1

    def test_top_larger_than_the_table_is_not_an_error(self):
        assert len(catalog().answer("SELECT TOP 99 * FROM people").rows) == 2

    def test_table_names_match_case_insensitively(self):
        assert catalog().answer("SELECT * FROM PEOPLE").rows

    def test_bracketed_and_schema_qualified_names_resolve(self):
        assert catalog().answer("SELECT * FROM [dbo].[people]").rows


class TestSetupBatches:
    """Clients open a session before they ask anything."""

    @pytest.mark.parametrize("sql", [
        "SET QUOTED_IDENTIFIER OFF", "SET TEXTSIZE 4096", "USE master",
    ])
    def test_complete_without_a_result_set(self, sql):
        result = catalog().answer(sql)
        assert result.columns == [] and result.rows == []


class TestErrors:
    @pytest.mark.parametrize("sql", ["SELECT", "select", "SELECT  "])
    def test_a_select_with_nothing_after_it_is_a_syntax_error(self, sql):
        # Saying it is not a SELECT read as nonsense. A log truncated
        # mid-query is the usual way this arrives.
        with pytest.raises(QueryError, match="Incorrect syntax near 'SELECT'"):
            catalog().answer(sql)

    def test_a_named_query_with_nothing_reading_it_is_a_syntax_error(self):
        # Used to reach a split with nothing to split and raise an IndexError,
        # which is not an answer a client can do anything with.
        with pytest.raises(QueryError, match=r"Incorrect syntax near '\)'"):
            catalog().answer("WITH x AS (SELECT 1 AS a)")

    def test_an_unknown_table_uses_the_invalid_object_name_number(self):
        # 208 is what SQL Server sends, and clients already present it well.
        with pytest.raises(QueryError, match="invalid object name 'nope'") as caught:
            catalog().answer("SELECT * FROM nope")
        assert caught.value.number == INVALID_OBJECT_NAME == 208

    def test_an_unknown_table_lists_what_is_available(self):
        with pytest.raises(QueryError, match="cities, people"):
            catalog().answer("SELECT * FROM nope")

    def test_an_unknown_column_has_its_own_number(self):
        # Not the table's. Measured: SQL Server answers a bad column with msg
        # 207, Invalid column name, and keeps 208 for a name that is not an
        # object at all. Reporting 208 sends whoever reads it looking for a
        # table that was never the problem, which is the argument the note on
        # _number_of already makes about a divide by zero.
        with pytest.raises(QueryError, match="invalid column name 'nope'") as caught:
            catalog().answer("SELECT nope FROM people")
        assert caught.value.number == NO_SUCH_COLUMN == 207

    def test_and_a_column_in_a_join_condition_is_refused_the_same_way(self):
        # It used to travel out as a PredicateError and reach the client as
        # an internal error. Found by cutting every query in the differential
        # short, which is what a truncated ON condition looks like.
        with pytest.raises(QueryError, match="invalid column name") as caught:
            catalog().answer(
                "SELECT COUNT(*) AS n FROM people p "
                "JOIN cities c ON c.name = p.nope")
        assert caught.value.number == NO_SUCH_COLUMN

    def test_unsupported_sql_uses_the_user_defined_number(self):
        # This project's complaint, not one of SQL Server's.
        with pytest.raises(QueryError, match="PIVOT is not supported") as caught:
            catalog().answer("SELECT id FROM people PIVOT (COUNT(id) FOR id IN ([1]))")
        assert caught.value.number == UNSUPPORTED == 50000

    def test_duplicate_table_names_are_refused_when_added(self):
        c = catalog()
        with pytest.raises(SourceError, match="both called 'people'"):
            c.add(from_records([{"x": 1}], name="people"))


class TestConfig:
    @staticmethod
    def build(tmp_path):
        (tmp_path / "people.csv").write_text("id,name\n1,ada\n", encoding="utf-8")
        (tmp_path / "cities.json").write_text(
            json.dumps([{"city": "Oslo"}]), encoding="utf-8")
        config = tmp_path / "tables.json"
        config.write_text(json.dumps({"tables": [
            {"name": "people", "csv": "people.csv"},
            {"name": "cities", "json": "cities.json"},
        ]}), encoding="utf-8")
        return config

    def test_loads_both_source_kinds(self, tmp_path):
        assert load(self.build(tmp_path)).names == ["cities", "people"]

    def test_paths_resolve_against_the_config_not_the_working_directory(self, tmp_path):
        # So a config and its data can be moved together.
        nested = tmp_path / "deep"
        nested.mkdir()
        config = self.build(nested)
        assert load(config).answer("SELECT * FROM people").rows == [[1, "ada"]]

    def test_the_file_name_supplies_the_table_name_when_none_is_given(self, tmp_path):
        (tmp_path / "widgets.csv").write_text("a\n1\n", encoding="utf-8")
        config = tmp_path / "c.json"
        config.write_text(json.dumps({"tables": [{"csv": "widgets.csv"}]}),
                          encoding="utf-8")
        assert load(config).names == ["widgets"]

    def test_a_table_needs_exactly_one_source_kind(self, tmp_path):
        config = tmp_path / "c.json"
        config.write_text(json.dumps({"tables": [{"csv": "a.csv", "json": "a.json"}]}),
                          encoding="utf-8")
        with pytest.raises(SourceError, match="exactly one of"):
            load(config)

    def test_a_config_that_names_nothing_says_what_it_needs(self, tmp_path):
        config = tmp_path / "c.json"
        config.write_text(json.dumps({}), encoding="utf-8")
        with pytest.raises(SourceError, match='"tables" array, a "discover" array'):
            load(config)

    def test_a_missing_config_says_so(self, tmp_path):
        with pytest.raises(SourceError, match="could not read"):
            load(tmp_path / "absent.json")


class TestHttpConfig:
    """Configuration for a source that is fetched rather than read."""

    @staticmethod
    def write(tmp_path, entry):
        import json

        config = tmp_path / "tables.json"
        config.write_text(json.dumps({"tables": [entry]}), encoding="utf-8")
        return config

    def test_a_url_object(self, tmp_path):
        config = self.write(tmp_path, {
            "name": "pokemon",
            "http": {"url": "https://example.test/api", "path": "results",
                     "ttl": 60, "timeout": 5},
        })
        catalog = load(config)
        source = catalog.sources["pokemon"]
        assert source.url == "https://example.test/api"
        assert source.path == "results"
        assert source.ttl == 60 and source.timeout == 5

    def test_a_bare_url_string(self, tmp_path):
        config = self.write(tmp_path, {"name": "t", "http": "https://example.test/a"})
        assert load(config).sources["t"].url == "https://example.test/a"

    def test_defaults_are_used_when_not_given(self, tmp_path):
        from pysqlbridge.http_source import (
            DEFAULT_TIMEOUT_SECONDS,
            DEFAULT_TTL_SECONDS,
        )

        config = self.write(tmp_path, {"name": "t", "http": "https://example.test/a"})
        source = load(config).sources["t"]
        assert source.timeout == DEFAULT_TIMEOUT_SECONDS
        assert source.ttl == DEFAULT_TTL_SECONDS

    def test_an_http_source_needs_a_name(self, tmp_path):
        # A URL has no obvious table name the way a file path does.
        config = self.write(tmp_path, {"http": "https://example.test/a"})
        with pytest.raises(SourceError, match="needs a"):
            load(config)

    def test_an_http_entry_without_a_url(self, tmp_path):
        config = self.write(tmp_path, {"name": "t", "http": {"path": "results"}})
        with pytest.raises(SourceError, match="needs a url"):
            load(config)

    def test_a_source_cannot_be_both_a_file_and_a_url(self, tmp_path):
        config = self.write(tmp_path, {"name": "t", "csv": "a.csv",
                                       "http": "https://example.test/a"})
        with pytest.raises(SourceError, match="exactly one of"):
            load(config)


class TestUnreachableSources:
    """One broken source should not take the others down with it."""

    class Broken:
        name = "api"

        def load(self):
            raise SourceError("could not reach https://example.test")

        def schema(self):
            return self.load()

    def test_the_table_list_still_shows_the_working_tables(self):
        c = catalog()
        c.add_source(self.Broken())
        names = [row[2] for row in
                 c.answer(Query(sql="SELECT * FROM INFORMATION_SCHEMA.TABLES")).rows]
        assert names == ["api", "cities", "people"]

    def test_the_broken_one_is_listed_with_no_columns(self):
        c = catalog()
        c.add_source(self.Broken())
        rows = c.answer(Query(
            sql="SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME = 'api'")).rows
        assert rows == []

    def test_selecting_from_it_explains_why_rather_than_saying_it_is_missing(self):
        # The fix is to check the URL, not to hunt for a typo in the name.
        from pysqlbridge.catalog import SOURCE_UNAVAILABLE

        c = catalog()
        c.add_source(self.Broken())
        with pytest.raises(QueryError, match="could not be loaded") as caught:
            c.answer(Query(sql="SELECT * FROM api"))
        assert caught.value.number == SOURCE_UNAVAILABLE

    def test_a_genuinely_missing_table_still_says_so(self):
        c = catalog()
        c.add_source(self.Broken())
        with pytest.raises(QueryError, match="invalid object name"):
            c.answer(Query(sql="SELECT * FROM nope"))


class TestConfigTypos:
    """An option nobody reads is refused rather than ignored.

    A configuration is a decision, and a decision that vanishes is the one
    failure a config file cannot recover from on its own: the file looks
    right, the source behaves as though the line were not there, and nothing
    says which of the two is wrong.
    """

    @staticmethod
    def write(tmp_path, document):
        import json

        config = tmp_path / "tables.json"
        config.write_text(json.dumps(document), encoding="utf-8")
        return config

    def table(self, tmp_path, entry):
        return self.write(tmp_path, {"tables": [entry]})

    def test_an_http_option_written_beside_http_says_where_it_goes(self, tmp_path):
        config = self.table(tmp_path, {
            "name": "t", "format": "csv", "http": "https://example.test/a.csv",
        })
        with pytest.raises(SourceError, match='"format" belongs inside "http"'):
            load(config)

    def test_a_misspelled_key_offers_the_nearest_one(self, tmp_path):
        config = self.table(tmp_path, {
            "name": "t", "http": {"url": "https://example.test/a", "pathh": "results"},
        })
        with pytest.raises(SourceError, match='did you mean "path"'):
            load(config)

    def test_a_key_nothing_resembles_is_still_refused(self, tmp_path):
        config = self.table(tmp_path, {
            "name": "t", "http": {"url": "https://example.test/a", "banana": 1},
        })
        with pytest.raises(SourceError, match='"banana" is not an option here'):
            load(config)

    def test_a_paging_rule_is_checked_too(self, tmp_path):
        config = self.table(tmp_path, {
            "name": "t",
            "http": {"url": "https://example.test/a",
                     "paging": {"key": "offset", "steps": 20}},
        })
        with pytest.raises(SourceError, match='did you mean "step"'):
            load(config)

    def test_what_discovery_writes_still_loads(self, tmp_path):
        # A generated config nobody can reload is a report, not a config.
        config = self.table(tmp_path, {
            "name": "t",
            "http": {"url": "https://example.test/a", "path": "results",
                     "records": "entries", "next": "next",
                     "paging": {"key": "offset", "parameter": "offset",
                                "step": 20}},
        })
        assert load(config).sources["t"].path == "results"


class TestConfigEncoding:
    def test_a_config_with_a_byte_order_mark_loads(self, tmp_path):
        # PowerShell and Notepad both write one, and json.loads refuses it.
        (tmp_path / "people.csv").write_text("id\n1\n", encoding="utf-8")
        config = tmp_path / "c.json"
        body = '{"tables":[{"name":"people","csv":"people.csv"}]}'
        config.write_bytes(("\ufeff" + body).encode("utf-8"))
        assert load(config).names == ["people"]


class TestOrdering:
    @staticmethod
    def rows(sql: str):
        c = Catalog()
        c.add(from_records([
            {"name": "ada", "score": 99.5, "retired": None},
            {"name": "grace", "score": 87.25, "retired": None},
            {"name": "edsger", "score": 78.0, "retired": 1},
            {"name": "barbara", "score": 93.75, "retired": None},
        ], name="people"))
        return c.answer(Query(sql=sql)).rows

    def test_ascending(self):
        assert [r[0] for r in self.rows("SELECT name FROM people ORDER BY score")] == [
            "edsger", "grace", "barbara", "ada",
        ]

    def test_descending(self):
        assert [r[0] for r in self.rows("SELECT name FROM people ORDER BY score DESC")] == [
            "ada", "barbara", "grace", "edsger",
        ]

    def test_top_takes_the_first_rows_of_the_sorted_result(self):
        # Not three arbitrary rows put in order afterwards.
        assert [r[0] for r in self.rows(
            "SELECT TOP 2 name FROM people ORDER BY score DESC")] == ["ada", "barbara"]

    def test_sorting_by_a_column_the_select_list_does_not_name(self):
        # The sort happens before the projection, so this has to work.
        assert [r[0] for r in self.rows(
            "SELECT name FROM people ORDER BY score DESC")][0] == "ada"

    def test_nulls_sort_first_ascending(self):
        first = self.rows("SELECT retired FROM people ORDER BY retired")[0][0]
        assert first is None

    def test_nulls_sort_last_descending(self):
        last = self.rows("SELECT retired FROM people ORDER BY retired DESC")[-1][0]
        assert last is None

    def test_several_keys_are_applied_in_order(self):
        ordered = self.rows("SELECT name FROM people ORDER BY retired, score DESC")
        # The three NULL-retired rows first, highest score among them leading.
        assert [r[0] for r in ordered] == ["ada", "barbara", "grace", "edsger"]

    def test_ordering_combines_with_a_where(self):
        assert [r[0] for r in self.rows(
            "SELECT name FROM people WHERE score > 90 ORDER BY score")] == [
            "barbara", "ada",
        ]

    def test_an_unknown_order_by_column_names_itself(self):
        with pytest.raises(QueryError, match="invalid column name 'nope' in the ORDER BY"):
            self.rows("SELECT * FROM people ORDER BY nope")


class TestParallelLoading:
    """Sources load at once, because loading them is network wait."""

    class Slow:
        def __init__(self, name: str, delay: float = 0.08) -> None:
            self.name = name
            self.delay = delay

        def load(self):
            import time

            time.sleep(self.delay)
            return from_records([{"a": 1}], name=self.name)

        def schema(self):
            return self.load()

    def test_loading_many_sources_is_not_the_sum_of_their_waits(self):
        import time

        c = Catalog()
        for i in range(6):
            c.add_source(self.Slow(f"s{i}"))

        start = time.perf_counter()
        tables = c.load_all()
        elapsed = time.perf_counter() - start

        assert len(tables) == 6
        # Six sources at 80 ms each is 480 ms in sequence. Allowing generous
        # slack, anything under half of that proves they overlapped.
        assert elapsed < 0.24, f"took {elapsed:.2f}s, so they did not overlap"

    def test_a_single_source_does_not_start_a_pool(self):
        c = Catalog()
        c.add(from_records([{"a": 1}], name="only"))
        assert [t.name for t in c.load_all()] == ["only"]

    def test_one_broken_source_does_not_hide_the_others(self):
        class Broken:
            name = "api"

            def load(self):
                raise SourceError("could not reach it")

            def schema(self):
                return self.load()

        c = Catalog()
        c.add(from_records([{"a": 1}], name="good"))
        c.add_source(Broken())
        names = sorted(t.name for t in c.load_all())
        assert names == ["api", "good"]

    def test_warm_reports_what_it_could_not_load(self):
        class Broken:
            name = "api"

            def load(self):
                raise SourceError("could not reach it")

            def schema(self):
                return self.load()

        c = Catalog()
        c.add(from_records([{"a": 1}], name="good"))
        c.add_source(Broken())
        assert c.warm() == ["api"]

    def test_warm_keeps_a_failed_source_in_the_catalog(self):
        # It may be up by the first query.
        class Broken:
            name = "api"

            def load(self):
                raise SourceError("down")

            def schema(self):
                return self.load()

        c = Catalog()
        c.add_source(Broken())
        c.warm()
        assert c.names == ["api"]


# The batch SSMS sends before it will open Object Explorer, captured from a
# real connection rather than written from memory: the copy in SMO's own
# assembly is shorter than what it sends, and the part it leaves out is the
# part that made this fail.
PROBE = (
    "DECLARE @edition sysname; "
    "SET @edition = cast(SERVERPROPERTY(N'EDITION') as sysname); "
    "SELECT case when @edition = N'SQL Azure' then 2 else 1 end "
    "as 'DatabaseEngineType', "
    "SERVERPROPERTY('EngineEdition') AS DatabaseEngineEdition, "
    "SERVERPROPERTY('ProductVersion') AS ProductVersion, "
    "@@MICROSOFTVERSION AS MicrosoftVersion, "
    "case when serverproperty('EngineEdition') = 12 then 1 "
    "when serverproperty('EngineEdition') = 11 and @@version like "
    "'Microsoft Azure SQL Data Warehouse%' then 1 else 0 end as IsFabricServer, "
    "convert(sysname, SERVERPROPERTY(N'Collation')) AS Collation; "
    "select host_platform from sys.dm_os_host_info "
    "if @edition = N'SQL Azure' select 'TCP' as ConnectionProtocol "
    "else exec ('select CONVERT(nvarchar(40),CONNECTIONPROPERTY("
    "''net_transport'')) as ConnectionProtocol')"
)


# The second batch, kept as a file because it is 3,300 characters of someone
# else's SQL and rewriting it by hand would be rewriting the test.
SECOND_PROBE = (pathlib.Path(__file__).parent / "ssms_server_probe.sql").read_text(
    encoding="utf-8"
)

# What fills the Databases node, kept the same way and for the same reason.
DATABASES_QUERY = (pathlib.Path(__file__).parent / "ssms_databases.sql").read_text(
    encoding="utf-8"
)


class TestWhatAClientAsksFirst:
    """The batch SSMS opens a connection with, and what it needs to answer.

    It declares a variable, sets it from a server property, and selects
    something worked out from it. Answering nothing to that is what made a
    client report "Cannot find table 0" and refuse to connect: it asked for
    a result set and received none.
    """

    def test_the_edition_probe_answers(self):
        answer = catalog().answer(
            "DECLARE @edition sysname; "
            "SET @edition = cast(SERVERPROPERTY(N'EDITION') as sysname); "
            "SELECT case when @edition = N'SQL Azure' then 2 else 1 end "
            "as 'DatabaseEngineType', "
            "SERVERPROPERTY('EngineEdition') AS DatabaseEngineEdition, "
            "SERVERPROPERTY('ProductVersion') AS ProductVersion, "
            "@@MICROSOFTVERSION AS MicrosoftVersion"
        )
        assert [c.name for c in answer.columns] == [
            "DatabaseEngineType", "DatabaseEngineEdition",
            "ProductVersion", "MicrosoftVersion",
        ]
        assert answer.rows[0][0] == 1        # not Azure

    def test_a_variable_lives_for_the_batch(self):
        assert catalog().answer(
            "DECLARE @n int = 5; SELECT @n * 2 AS doubled"
        ).rows == [[10]]

    def test_a_set_of_something_that_is_not_a_variable_is_ignored(self):
        assert catalog().answer("SET LOCK_TIMEOUT 10000").rows == []

    def test_a_property_it_does_not_have_is_null(self):
        assert catalog().answer(
            "SELECT SERVERPROPERTY('nonsense') AS v"
        ).rows == [[None]]

    def test_the_version_agrees_with_the_one_it_reports(self):
        # A client that read both and found them different would be right to
        # complain: 17.0.1000 packed the way @@MICROSOFTVERSION packs it.
        version = catalog().answer("SELECT SERVERPROPERTY('ProductVersion') AS v")
        packed = catalog().answer("SELECT @@MICROSOFTVERSION AS v").rows[0][0]
        major, minor, build = [int(p) for p in version.rows[0][0].split(".")[:3]]
        assert packed == (major << 24) + (minor << 16) + build

    def test_a_batch_answers_with_every_result_set_it_made(self):
        answer = catalog().answer("SELECT 1 AS a; SELECT 2 AS b")
        assert answer.rows == [[1]]
        assert [one.rows for one in answer.following] == [[[2]]]

    def test_the_whole_probe_answers_with_three(self):
        # The batch SSMS opens with, as captured from a real connection. It
        # reads the third result set, so a server that sent one is a server
        # it reports "Cannot find table 2" about and will not connect to.
        answer = catalog().answer(PROBE)
        every = [answer, *answer.following]
        assert len(every) == 3
        assert [c.name for c in every[1].columns] == ["host_platform"]
        assert [c.name for c in every[2].columns] == ["ConnectionProtocol"]
        assert every[2].rows == [["TCP"]]

    def test_an_if_takes_the_branch_its_condition_chooses(self):
        answer = catalog().answer(
            "DECLARE @n int = 1; IF @n = 1 SELECT 'yes' AS v ELSE SELECT 'no' AS v"
        )
        assert answer.rows == [["yes"]]

    def test_and_the_other_one_when_it_does_not(self):
        answer = catalog().answer(
            "DECLARE @n int = 2; IF @n = 1 SELECT 'yes' AS v ELSE SELECT 'no' AS v"
        )
        assert answer.rows == [["no"]]

    def test_an_if_with_no_else_and_a_false_condition_answers_nothing(self):
        answer = catalog().answer("IF 1 = 2 SELECT 'yes' AS v")
        assert answer.columns == [] and answer.rows == []

    def test_exec_runs_the_statement_it_was_given_as_text(self):
        assert catalog().answer(
            "EXEC ('select ''run'' as v')"
        ).rows == [["run"]]

    def test_a_doubled_quote_inside_that_text_is_one_quote(self):
        assert catalog().answer(
            "EXEC ('select CONVERT(nvarchar(40),CONNECTIONPROPERTY(''net_transport'')) as v')"
        ).rows == [["TCP"]]

    def test_a_statement_may_follow_another_without_a_semicolon(self):
        # T-SQL does not need one, and the probe does not write one.
        answer = catalog().answer(
            "SELECT 1 AS a IF 1 = 1 SELECT 2 AS b"
        )
        assert answer.rows == [[1]]
        assert [one.rows for one in answer.following] == [[[2]]]

    def test_the_host_view_says_what_this_is_running_on(self):
        import platform

        answer = catalog().answer("SELECT host_platform FROM sys.dm_os_host_info")
        expected = "Windows" if platform.system() == "Windows" else platform.system()
        assert answer.rows == [[expected]]

    def test_a_sys_view_it_does_not_have_says_which_it_does(self):
        with pytest.raises(QueryError, match="dm_os_host_info"):
            catalog().answer("SELECT * FROM sys.dm_os_nonsense")

    def test_a_connection_property_it_does_not_have_is_null(self):
        assert catalog().answer(
            "SELECT CONNECTIONPROPERTY('nonsense') AS v"
        ).rows == [[None]]

    def test_a_quoted_alias_names_the_column_without_its_quotes(self):
        answer = catalog().answer("SELECT 1 as 'quoted', 2 as [bracketed]")
        assert [c.name for c in answer.columns] == ["quoted", "bracketed"]

    def test_a_semicolon_inside_a_string_does_not_split_a_statement(self):
        assert catalog().answer(
            "SELECT 'a;b' AS v"
        ).rows == [["a;b"]]


class TestWritesAreRefused:
    """A write is refused rather than passed over as though it had worked.

    Everything that is neither a read nor a write is still passed over: a
    client sends SET and USE by the dozen before it will talk to a server,
    and an error on those stops it before it starts. A write is different.
    Reporting that a DELETE succeeded tells a person something untrue about
    their data, and this reads its sources and never writes to them.
    """

    @pytest.mark.parametrize("sql", [
        "INSERT INTO people VALUES (1, 'x')",
        "INSERT people VALUES (1, 'x')",
        "UPDATE people SET name = 'x'",
        "DELETE FROM people",
        "DELETE people",
        "TRUNCATE TABLE people",
        "DROP TABLE people",
        "ALTER TABLE people ADD extra int",
        "MERGE people USING other ON 1 = 1",
        "CREATE TABLE somewhere (a int)",
        "CREATE PROCEDURE dbo.something AS SELECT 1",
    ])
    def test_a_write_says_so(self, sql):
        with pytest.raises(QueryError, match="never writes"):
            catalog().answer(sql)

    def test_the_source_is_untouched(self):
        c = catalog()
        before = len(c.get("people").rows)
        with pytest.raises(QueryError):
            c.answer("DELETE FROM people")
        assert len(c.get("people").rows) == before

    @pytest.mark.parametrize("sql", [
        "SET NOCOUNT ON", "USE master", "DECLARE @x int",
        "SET ANSI_NULLS ON; SET QUOTED_IDENTIFIER ON",
    ])
    def test_what_a_client_sends_to_open_a_session_still_passes(self, sql):
        assert catalog().answer(sql).rows == []

    def test_a_session_table_is_still_written(self):
        # The one thing this does write, and the reason the refusal looks at
        # what is named rather than at the word the statement starts with.
        here = {}
        catalog().answer(Query(sql="CREATE TABLE #x(ID int)", session=here))
        catalog().answer(Query(sql="DROP TABLE #x", session=here))

    def test_a_write_inside_a_batch_is_refused_too(self):
        with pytest.raises(QueryError, match="never writes"):
            catalog().answer("SET NOCOUNT ON; DELETE FROM people")

    @pytest.mark.parametrize("sql, named", [
        ("DELETE FROM [dbo].[people]", "[dbo].[people]"),
        ("DROP TABLE [my table]", "[my table]"),
        ("UPDATE [people] SET name = 'x'", "[people]"),
    ])
    def test_the_refusal_names_the_whole_bracketed_name(self, sql, named):
        # A bracketed name may hold a dot or a space. Reporting that 'my' is
        # unchanged names no table a person has.
        with pytest.raises(QueryError, match=re.escape(named)):
            catalog().answer(sql)


class TestPermissionChangesAreRefused:
    """GRANT, REVOKE and DENY, which used to complete without a word.

    They are not writes to the data, so the refusal above never looked at
    them, and the word they start with was not one the router knew, so they
    took the branch that quietly completes a SET. A person told their GRANT
    succeeded has been told something untrue about who can read their data.
    """

    @pytest.mark.parametrize("sql", [
        "GRANT SELECT ON people TO public",
        "REVOKE SELECT ON people FROM public",
        "DENY SELECT ON people TO guest",
        "GRANT VIEW DEFINITION TO public",
        "grant select on [dbo].[people] to [public]",
    ])
    def test_a_permission_change_says_so(self, sql):
        with pytest.raises(QueryError, match="no permissions to change"):
            catalog().answer(sql)

    def test_it_names_what_the_statement_named(self):
        with pytest.raises(QueryError, match="'people'"):
            catalog().answer("GRANT SELECT ON people TO public")

    def test_and_says_nothing_about_a_table_when_none_was_named(self):
        with pytest.raises(QueryError, match="every source it serves"):
            catalog().answer("GRANT VIEW DEFINITION TO public")

    def test_one_inside_a_batch_is_refused_too(self):
        with pytest.raises(QueryError, match="no permissions to change"):
            catalog().answer("SET NOCOUNT ON; GRANT SELECT ON people TO public")

    def test_a_column_called_grant_is_not_one(self):
        # The word only begins a statement. Anywhere else it is a name.
        assert catalog().answer("SELECT 1 AS grant_total").rows == [[1]]


class TestAStatementWrittenOutAsText:
    """EXEC('...') and EXEC sp_executesql N'...', which mean the same thing.

    A client sends the second one constantly, wrapped in a TRY so a server
    that cannot run it says nothing rather than failing. That is how it went
    unnoticed: the CATCH answered and the probe came back empty.
    """

    def answer(self, sql):
        return catalog().answer(Query(sql=sql, session={})).rows

    @pytest.mark.parametrize("sql", [
        "EXEC ('SELECT 4 AS v')",
        "EXEC sp_executesql N'SELECT 4 AS v'",
        "EXECUTE sp_executesql N'SELECT 4 AS v'",
        "EXEC master.dbo.sp_executesql N'SELECT 4 AS v'",
    ])
    def test_every_way_of_saying_it(self, sql):
        assert self.answer(sql) == [[4]]

    def test_a_quote_inside_it(self):
        assert self.answer("EXEC sp_executesql N'SELECT ''quoted'' AS v'"
                           ) == [["quoted"]]

    def test_it_reads_the_tables(self):
        assert self.answer("EXEC sp_executesql "
                           "N'SELECT COUNT(*) AS n FROM people'") == [[2]]

    def test_the_values_it_is_given_are_values(self):
        # Not decoration: answering NULL where a value was passed would be a
        # wrong answer rather than a missing feature.
        assert self.answer("EXEC sp_executesql N'SELECT @x AS v', "
                           "N'@x int', @x = 7") == [[7]]
        assert self.answer("EXEC sp_executesql N'SELECT @a + @b AS v', "
                           "N'@a int, @b int', @a = 2, @b = 3") == [[5]]

    def test_a_value_it_reads_by(self):
        assert self.answer(
            "EXEC sp_executesql N'SELECT COUNT(*) AS n FROM people "
            "WHERE id <= @n', N'@n int', @n = 1") == [[1]]

    def test_a_procedure_that_is_not_one_still_says_so(self):
        with pytest.raises(QueryError, match="could not find stored procedure"):
            self.answer("EXEC sp_nosuch")


class TestASelectThatMakesItsTable:
    """SELECT ... INTO #t, which builds the table out of the answer.

    The columns are the select list's, names and types alike, so the shape
    is whatever the answer was rather than something declared beforehand.
    """

    def session(self):
        return {}

    def run(self, sql, here):
        return catalog().answer(Query(sql=sql, session=here))

    def test_it_makes_the_table_and_fills_it(self):
        here = self.session()
        self.run("SELECT id, name INTO #c FROM people", here)
        found = self.run("SELECT id, name FROM #c ORDER BY id", here)
        assert found.rows == [[1, "ada"], [2, "grace"]]
        assert [c.name for c in found.columns] == ["id", "name"]

    def test_the_aliases_are_the_column_names(self):
        here = self.session()
        self.run("SELECT id AS a, name AS b INTO #c FROM people", here)
        assert [c.name for c in self.run("SELECT * FROM #c", here).columns
                ] == ["a", "b"]

    def test_a_star_brings_every_column(self):
        here = self.session()
        self.run("SELECT * INTO #c FROM people", here)
        assert len(self.run("SELECT * FROM #c", here).columns) == 2

    def test_an_aggregate_makes_a_table_of_one_row(self):
        here = self.session()
        self.run("SELECT COUNT(*) AS n INTO #c FROM people", here)
        assert self.run("SELECT n FROM #c", here).rows == [[2]]

    def test_no_rows_still_makes_the_table(self):
        here = self.session()
        self.run("SELECT id INTO #c FROM people WHERE 1 = 0", here)
        assert self.run("SELECT COUNT(*) AS n FROM #c", here).rows == [[0]]

    def test_a_column_it_could_not_name(self):
        # There would be nothing to read it back by.
        here = self.session()
        with pytest.raises(QueryError) as refused:
            self.run("SELECT id + 1 INTO #c FROM people", here)
        assert refused.value.number == 1038

    def test_a_name_already_taken(self):
        here = self.session()
        self.run("SELECT id INTO #c FROM people", here)
        with pytest.raises(QueryError) as refused:
            self.run("SELECT id INTO #c FROM people", here)
        assert refused.value.number == 2714

    def test_it_goes_when_it_is_dropped(self):
        here = self.session()
        self.run("SELECT id INTO #c FROM people", here)
        self.run("DROP TABLE #c", here)
        with pytest.raises(QueryError, match="invalid object name"):
            self.run("SELECT * FROM #c", here)


class TestTemporaryTables:
    """The one thing a read-only bridge writes: a table a session made.

    It lives on the connection that created it and goes when that connection
    does. Nothing a source holds is touched. SSMS builds one to collect what
    a server says about itself and reads the answer back out of it.
    """

    def session(self):
        return {}

    def run(self, sql, session):
        return catalog().answer(Query(sql=sql, session=session))

    def test_a_table_can_be_created_filled_and_read(self):
        here = self.session()
        self.run("CREATE TABLE #x(ID int, Name nvarchar(50))", here)
        self.run("INSERT #x SELECT id, name FROM people", here)
        found = self.run("SELECT Name FROM #x ORDER BY ID", here)
        assert [row[0] for row in found.rows] == ["ada", "grace"]

    def test_its_columns_are_the_ones_declared(self):
        here = self.session()
        self.run("CREATE TABLE #x(ID int, Name nvarchar(50))", here)
        found = self.run("SELECT * FROM #x", here)
        assert [c.name for c in found.columns] == ["ID", "Name"]
        assert found.columns[0].type.__class__.__name__ == "Integer"

    def test_another_connection_cannot_see_it(self):
        here = self.session()
        self.run("CREATE TABLE #x(ID int)", here)
        with pytest.raises(QueryError, match="invalid object name"):
            self.run("SELECT * FROM #x", self.session())

    def test_dropping_it_takes_it_away(self):
        here = self.session()
        self.run("CREATE TABLE #x(ID int)", here)
        self.run("DROP TABLE #x", here)
        with pytest.raises(QueryError, match="invalid object name"):
            self.run("SELECT * FROM #x", here)

    def test_inserting_the_wrong_shape_says_so(self):
        here = self.session()
        self.run("CREATE TABLE #x(ID int)", here)
        with pytest.raises(QueryError,
                           match="does not match table definition") as refused:
            self.run("INSERT #x SELECT id, name FROM people", here)
        assert refused.value.number == 213

    def test_an_insert_may_name_the_columns_it_fills(self):
        here = self.session()
        self.run("CREATE TABLE #x(a int, b nvarchar(50))", here)
        self.run("INSERT INTO #x (a, b) SELECT id, name FROM people", here)
        assert self.run("SELECT a, b FROM #x ORDER BY a", here).rows == [
            [1, "ada"], [2, "grace"],
        ]

    def test_it_fills_them_in_the_order_it_named_them(self):
        # (b, a) puts the first value in b, not in the table's first column.
        here = self.session()
        self.run("CREATE TABLE #x(a int, b nvarchar(50))", here)
        self.run("INSERT INTO #x (b, a) SELECT name, id FROM people", here)
        assert self.run("SELECT a, b FROM #x ORDER BY a", here).rows == [
            [1, "ada"], [2, "grace"],
        ]

    def test_the_columns_it_did_not_name_are_null(self):
        here = self.session()
        self.run("CREATE TABLE #x(a int, b nvarchar(50), c int)", here)
        self.run("INSERT INTO #x (a) SELECT id FROM people", here)
        assert self.run("SELECT a, b, c FROM #x ORDER BY a", here).rows == [
            [1, None, None], [2, None, None],
        ]

    @pytest.mark.parametrize("insert, number", [
        ("INSERT INTO #x (nosuch) SELECT id FROM people", 207),
        ("INSERT INTO #x (a, b) SELECT id FROM people", 120),
        ("INSERT INTO #x (a) SELECT id, name FROM people", 121),
    ])
    def test_an_insert_that_does_not_fit_says_which_way(self, insert, number):
        here = self.session()
        self.run("CREATE TABLE #x(a int, b nvarchar(50))", here)
        with pytest.raises(QueryError) as refused:
            self.run(insert, here)
        assert refused.value.number == number

    def test_inserting_into_one_that_was_never_created(self):
        with pytest.raises(QueryError, match="not created on this connection"):
            self.run("INSERT #nope SELECT 1", self.session())

    def test_a_procedure_can_fill_one(self):
        here = self.session()
        self.run("CREATE TABLE #v(ID int, Name sysname, Internal_Value int, "
                 "Value nvarchar(512))", here)
        self.run("INSERT #v EXEC master.dbo.xp_msver", here)
        found = self.run("SELECT Value FROM #v WHERE Name = 'ProductVersion'", here)
        assert found.rows == [["17.0.1000.0"]]


class TestCrossApply:
    """A table written into the query, worked out for each row beside it."""

    def test_values_may_read_the_row_they_are_applied_to(self):
        found = catalog().answer(
            "SELECT t.* FROM sys.dm_os_host_info CROSS APPLY "
            "( VALUES (1, 'platform', host_platform), (2, 'machine', host_architecture) ) "
            "t(id, [name], [value])"
        )
        assert [c.name for c in found.columns] == ["id", "name", "value"]
        assert [row[1] for row in found.rows] == ["platform", "machine"]

    def test_it_runs_once_for_every_row_of_the_left_side(self):
        found = catalog().answer(
            "SELECT t.n FROM people CROSS APPLY ( VALUES (id), (id * 10) ) t(n) "
            "ORDER BY t.n"
        )
        assert len(found.rows) == 4        # two rows, twice each

    def test_a_star_qualified_by_the_other_side_reads_that_one(self):
        found = catalog().answer(
            "SELECT people.* FROM people CROSS APPLY ( VALUES (1) ) t(n)"
        )
        assert [c.name for c in found.columns] == ["id", "name"]

    def test_a_row_of_the_wrong_width_is_refused(self):
        with pytest.raises(QueryError, match="names 2 columns"):
            catalog().answer(
                "SELECT t.* FROM people CROSS APPLY ( VALUES (1) ) t(a, b)"
            )

    def test_applying_something_that_is_neither_is_refused(self):
        with pytest.raises(QueryError, match="with VALUES or a SELECT"):
            catalog().answer("SELECT * FROM people CROSS APPLY (nonsense) t")

    def test_applying_a_select_runs_it_for_every_row(self):
        # Counted up to each row, so the answer differs per row: that is
        # what makes it an apply rather than a join.
        found = catalog().answer(
            "SELECT p.id, x.n FROM people p CROSS APPLY "
            "(SELECT COUNT(*) AS n FROM people q WHERE q.id <= p.id) x "
            "ORDER BY p.id"
        )
        assert found.rows == [[1, 1], [2, 2]]
        # Headed by their own names, not by what qualified them, which is
        # how every other qualified column is headed here.
        assert [c.name for c in found.columns] == ["id", "n"]

    def test_a_select_that_answers_nothing_drops_the_row(self):
        found = catalog().answer(
            "SELECT p.id, x.id FROM people p CROSS APPLY "
            "(SELECT id FROM people q WHERE q.id > p.id) x ORDER BY p.id"
        )
        assert found.rows == [[1, 2]]

    def test_outer_apply_keeps_it(self):
        found = catalog().answer(
            "SELECT p.id, x.id FROM people p OUTER APPLY "
            "(SELECT id FROM people q WHERE q.id > p.id) x ORDER BY p.id"
        )
        assert found.rows == [[1, 2], [2, None]]

    def test_a_select_that_reads_nothing_of_the_row(self):
        assert catalog().answer(
            "SELECT COUNT(*) AS n FROM people p CROSS APPLY "
            "(SELECT 1 AS one) x"
        ).rows == [[2]]

    def test_the_columns_come_from_the_select(self):
        answer = catalog().answer(
            "SELECT * FROM people p CROSS APPLY (SELECT 1 AS a, 2 AS b) x"
        )
        assert [c.name for c in answer.columns][-2:] == ["x.a", "x.b"]


class TestTheSecondProbe:
    """The batch SSMS sends after the first one, captured from a connection.

    It builds a temp table from a procedure and from a CROSS APPLY over the
    host view, skips a block meant for a managed instance, reads fifteen
    things about the server out of what it built, and drops the table.
    """

    def test_the_whole_of_it_answers(self):
        answer = catalog().answer(Query(sql=SECOND_PROBE, session={}))
        assert [c.name for c in answer.columns] == [
            "Server_Name", "Server_Urn", "Server_ServerType", "Server_Status",
            "Server_IsContainedAuthentication", "VersionMajor", "VersionMinor",
            "BuildNumber", "IsSingleUser", "Edition", "EngineEdition",
            "IsXTPSupported", "VersionString", "HostPlatform",
            "IsFullTextInstalled",
        ]

    def test_the_version_it_reads_is_the_one_reported_elsewhere(self):
        row = catalog().answer(Query(sql=SECOND_PROBE, session={})).rows[0]
        major, minor, build = row[5], row[6], row[7]
        assert f"{major}.{minor}.{build}" == row[12].rsplit(".", 1)[0]

    def test_the_platform_comes_back_out_of_the_table_it_built(self):
        import platform

        row = catalog().answer(Query(sql=SECOND_PROBE, session={})).rows[0]
        expected = "Windows" if platform.system() == "Windows" else platform.system()
        assert row[13] == expected

    def test_the_block_for_a_managed_instance_is_not_run(self):
        # Its condition is false here, and everything in it names views this
        # server does not have.
        assert catalog().answer(Query(sql=SECOND_PROBE, session={})).rows


class TestFunctionsAboutTheConnection:
    """What a client is told when it asks who it is and what it reached.

    Object Explorer asks all of these while opening. Everything answered is
    something this server actually knows: the login comes from the handshake
    Windows completed, the host and application from what the client sent,
    and the rest from what is served.
    """

    def about(self, **extra):
        session = {"login": r"DOMAIN\someone", "app": "a client", "host": "a machine"}
        session.update(extra)
        return Query(sql="", session=session)

    def one(self, sql, **extra):
        request = self.about(**extra)
        return catalog().answer(
            Query(sql=sql, session=request.session)
        ).rows[0][0]

    def test_it_reports_who_authenticated(self):
        assert self.one("SELECT suser_sname()") == r"DOMAIN\someone"
        assert self.one("SELECT original_login()") == r"DOMAIN\someone"

    def test_and_what_they_connected_with(self):
        assert self.one("SELECT app_name()") == "a client"
        assert self.one("SELECT host_name()") == "a machine"

    def test_the_database_is_the_one_this_serves(self):
        assert self.one("SELECT db_name()") == procedures.CATALOG
        assert self.one("SELECT db_id()") == 1

    def test_access_to_that_database_is_yes(self):
        assert self.one(f"SELECT has_dbaccess('{procedures.CATALOG}')") == 1

    def test_access_to_any_name_is_yes(self):
        # A login may name any database and is served the one catalog this
        # has, so saying no to a name the handshake accepts would be the
        # inconsistent answer.
        assert self.one("SELECT has_dbaccess('msdb')") == 1
        assert self.one("SELECT has_dbaccess('anything')") == 1

    def test_an_object_it_does_not_have_has_no_id(self):
        assert self.one("SELECT object_id('dbo.sysdac_instances')") is None

    def test_a_table_it_serves_has_one_and_keeps_it(self):
        first = self.one("SELECT object_id('people')")
        assert first is not None
        assert self.one("SELECT object_id('dbo.people')") == first

    def test_nobody_is_in_a_role_because_none_are_kept(self):
        assert self.one("SELECT is_srvrolemember('sysadmin')") == 0

    def test_the_probe_object_explorer_opens_with(self):
        assert self.one(
            "select case when object_id('dbo.sysdac_instances') is not null "
            "then 1 else 0 end"
        ) == 0


class TestSystemViews:
    """What Object Explorer reads to decide what to put in the tree."""

    def test_the_database_this_serves_is_listed(self):
        found = catalog().answer("SELECT name FROM master.sys.databases")
        assert [row[0] for row in found.rows] == [procedures.CATALOG]

    def test_it_says_it_is_read_only_because_it_is(self):
        found = catalog().answer("SELECT is_read_only FROM sys.databases")
        assert found.rows == [[True]]

    def test_the_status_object_explorer_builds_comes_out_normal(self):
        # Three CASEs and two bitwise ors, which is how it says online.
        found = catalog().answer(
            "SELECT case when dtb.collation_name is null then 0x200 else 0 end "
            "| case when 1 = dtb.is_in_standby then 0x40 else 0 end "
            "| case dtb.state when 1 then 0x2 when 2 then 0x8 when 3 then 0x4 "
            "when 4 then 0x10 when 5 then 0x100 when 6 then 0x20 else 1 end "
            "AS [Database_Status] FROM master.sys.databases AS dtb"
        )
        assert found.rows == [[1]]          # online, nothing unusual

    def test_the_filter_that_separates_user_databases_from_system_ones(self):
        found = catalog().answer(
            "SELECT dtb.name FROM master.sys.databases AS dtb WHERE "
            "(CAST(case when dtb.name in ('master','model','msdb','tempdb') "
            "then 1 else dtb.is_distributor end AS bit)=0)"
        )
        assert [row[0] for row in found.rows] == [procedures.CATALOG]

    def test_ordering_by_an_alias_over_a_qualified_column(self):
        found = catalog().answer(
            "SELECT dtb.name AS [Database_Name] FROM sys.databases AS dtb "
            "ORDER BY [Database_Name] ASC"
        )
        assert found.rows == [[procedures.CATALOG]]

    def test_the_setting_a_client_reads_first_has_a_row(self):
        # 16384 is Agent XPs, and it is the question Object Explorer asks
        # before it decides what to do about SQL Server Agent. An empty view
        # does not answer it: no row is not off, it is nothing, and a client
        # told nothing goes and finds out from the machine instead. That is
        # what the pause after connecting was.
        found = catalog().answer(
            "select value_in_use from sys.configurations where configuration_id = 16384"
        )
        assert found.rows == [[0]]
        assert [c.name for c in found.columns] == ["value_in_use"]

    def test_every_setting_a_real_server_reports_is_there(self):
        assert catalog().answer(
            "SELECT COUNT(*) AS n FROM sys.configurations"
        ).rows == [[101]]

    def test_the_features_it_does_not_have_are_off(self):
        found = catalog().answer(
            "SELECT name, value_in_use FROM sys.configurations "
            "WHERE name IN ('Agent XPs', 'clr enabled', 'xp_cmdshell') "
            "ORDER BY name"
        )
        assert found.rows == [
            ["Agent XPs", 0], ["clr enabled", 0], ["xp_cmdshell", 0]
        ]

    def test_a_setting_is_named_and_described_as_a_real_server_names_it(self):
        found = catalog().answer(
            "SELECT name, description FROM sys.configurations "
            "WHERE configuration_id = 16384"
        )
        assert found.rows == [["Agent XPs", "Enable or disable Agent XPs"]]


class TestBitwiseOperators:
    """How a client packs a version and a status into one number."""

    def test_and_or_and_xor(self):
        assert catalog().answer("SELECT 0xff & 0x0f AS v").rows == [[15]]
        assert catalog().answer("SELECT 0x200 | 0x40 AS v").rows == [[576]]
        assert catalog().answer("SELECT 5 ^ 3 AS v").rows == [[6]]

    def test_two_bars_are_still_a_concatenation(self):
        assert catalog().answer("SELECT 'a' || 'b' AS v").rows == [["ab"]]


class TestTheDefaultSchema:
    """dbo is the schema everything here is in."""

    def test_a_table_answers_to_its_qualified_name(self):
        assert catalog().answer("SELECT name FROM dbo.people").rows == [
            ["ada"], ["grace"]
        ]

    def test_and_to_its_bracketed_qualified_name(self):
        assert catalog().answer("SELECT name FROM [dbo].[people]").rows == [
            ["ada"], ["grace"]
        ]

    def test_the_policy_table_says_policies_are_off(self):
        # Rows rather than an empty table: the client reads each setting with
        # a scalar subquery, and off is an answer where nothing is not.
        found = catalog().answer(
            "SELECT CAST( (SELECT current_value FROM msdb.dbo.syspolicy_configuration "
            "WHERE name = 'Enabled') AS bit) AS [Enabled]"
        )
        assert found.rows == [[False]]


class TestSelectWithNoFrom:
    """A statement that computes a row, and decides whether to have one."""

    def test_a_where_keeps_the_row_when_it_holds(self):
        assert catalog().answer("SELECT 1 AS v WHERE 1 = 1").rows == [[1]]

    def test_and_drops_it_when_it_does_not(self):
        assert catalog().answer("SELECT 1 AS v WHERE 1 = 2").rows == []

    def test_the_columns_are_declared_either_way(self):
        found = catalog().answer("SELECT 1 AS v WHERE 1 = 2")
        assert [c.name for c in found.columns] == ["v"]

    def test_a_parameter_is_a_value_and_not_a_column(self):
        # No column can be called @p, so SELECT @p is something to compute
        # rather than a table this has never heard of.
        assert catalog().answer(
            Query(sql="SELECT @p AS v", parameters={"@p": "policy"})
        ).rows == [["policy"]]


class TestSubqueriesInsideOtherBrackets:
    """A select inside a cast, which is how a client reads one setting."""

    def test_one_nested_in_a_cast_is_still_lifted(self):
        found = catalog().answer(
            "SELECT CAST((SELECT COUNT(*) FROM people) AS int) AS n"
        )
        assert found.rows == [[2]]

    def test_two_of_them_in_one_entry(self):
        found = catalog().answer(
            "SELECT CAST((SELECT COUNT(*) FROM people) AS int) "
            "+ CAST((SELECT COUNT(*) FROM cities) AS int) AS n"
        )
        assert found.rows == [[3]]


class TestABatchThatBeginsWithIf:
    """An IF holds its branches whether or not anything precedes it."""

    def test_a_leading_if_is_one_statement(self):
        # It used to be split at BEGIN, DECLARE and EXECUTE, which left the
        # branches loose in the batch and ran them all.
        assert catalog().answer(
            "IF OBJECT_ID(N'sys.sp_MSIsContainedAGSession', N'P') IS NOT NULL "
            "BEGIN DECLARE @x int; SELECT @x END ELSE SELECT 0"
        ).rows == [[0]]

    def test_the_branch_that_holds_runs_its_whole_block(self):
        assert catalog().answer(
            "IF 1 = 1 BEGIN DECLARE @x int = 7; SELECT @x AS v END ELSE SELECT 0 AS v"
        ).rows == [[7]]


class TestWhatFillsTheDatabasesNode:
    """The batch Object Explorer sends to list databases.

    Read off the wire rather than written here: it builds four temporary
    tables, asks whether it may look at the server's own state, chooses
    between two nested branches on the answer, joins the result to two views
    and reads twenty columns out of one row. Anything it refuses is an empty
    Databases node, which is all the user sees.
    """

    def answer(self):
        # The two parameters it sends: keep the system databases out, and the
        # snapshots. Neither is this one, so the row survives both.
        return catalog().answer(Query(
            sql=DATABASES_QUERY,
            parameters={"@_msparam_0": "0", "@_msparam_1": "0"},
            session={},
        ))

    def test_it_answers_with_the_one_database(self):
        assert len(self.answer().rows) == 1

    def test_the_columns_are_the_ones_it_reads(self):
        assert [c.name for c in self.answer().columns] == [
            "Database_Name", "Database_Urn", "Database_ContainmentType",
            "Database_RecoveryModel", "Database_Owner", "Database_Status",
            "Database_CompatibilityLevel", "Database_MirroringRole",
            "Database_MirroringStatus",
            "Database_AvailabilityDatabaseSynchronizationState",
            "Database_HasMemoryOptimizedObjects",
            "Database_RemoteDataArchiveEnabled", "Database_IsSqlDw",
            "Database_IsFullTextEnabled", "Database_IsLedger",
            "RecoveryModel", "UserAccess", "ReadOnly",
            "Database_DatabaseName2", "Database_DatabaseName3",
        ]

    def test_the_name_is_the_one_served(self):
        row = self.answer().rows[0]
        assert row[0] == procedures.CATALOG

    def test_the_status_is_normal(self):
        # 1 is what the three CASEs and two ors come to for a database that
        # is online, has a collation and is not in standby. A client shows
        # anything else as a database it cannot open.
        assert self.answer().rows[0][5] == 1

    def test_it_is_read_only_and_says_so(self):
        assert self.answer().rows[0][17] is True

    def test_it_is_in_no_availability_group(self):
        # Null, which is what a LEFT JOIN to an empty table gives, and what
        # tells the client there is nothing to show about synchronization.
        assert self.answer().rows[0][9] is None


class TestIfInsideIf:
    """A nested IF, and which ELSE belongs to which.

    The batch that fills the Databases node has one, and the outer IF used to
    take the inner ELSE as its own. That handed the inner branch to the outer
    one along with the END that closed the block around it, and the statement
    that came out could not be read.
    """

    def test_the_outer_else_is_the_one_after_the_whole_block(self):
        assert catalog().answer(
            "IF 1 = 1 BEGIN IF 1 = 2 BEGIN SELECT 9 AS v END "
            "ELSE BEGIN SELECT 5 AS v END END ELSE SELECT 0 AS v"
        ).rows == [[5]]

    def test_and_the_outer_branch_that_does_not_hold_runs_nothing_of_it(self):
        assert catalog().answer(
            "IF 1 = 2 BEGIN IF 1 = 1 BEGIN SELECT 9 AS v END "
            "ELSE BEGIN SELECT 5 AS v END END ELSE SELECT 0 AS v"
        ).rows == [[0]]

    def test_a_case_inside_a_branch_keeps_its_own_end(self):
        assert catalog().answer(
            "IF 1 = 1 BEGIN SELECT CASE WHEN 1 = 1 THEN 3 ELSE 4 END AS v END "
            "ELSE SELECT 0 AS v"
        ).rows == [[3]]


class TestASelectThatAssigns:
    """SELECT @v = something, which gives a variable a value and no rows.

    SSMS declares a variable and selects into it with nothing between the
    two, and a batch that read the pair as one statement ran neither: the
    variable stayed null and the branches that depended on it all went the
    other way.
    """

    def test_it_sets_the_variable(self):
        assert catalog().answer(
            "declare @n int select @n = 7 select @n * 2 AS v"
        ).rows == [[14]]

    def test_it_produces_no_rows_of_its_own(self):
        assert catalog().answer("select @n = 7").rows == []

    def test_a_select_of_a_variable_is_still_a_read(self):
        assert catalog().answer("declare @n int = 4 select @n AS v").rows == [[4]]

    def test_a_union_whose_first_item_is_a_variable_is_one_statement(self):
        assert catalog().answer(
            "declare @n int = 1 select @n AS v union select 2 AS v"
        ).rows == [[1], [2]]

    def test_the_permission_it_asks_about_is_refused(self):
        # No, whatever is asked: this server keeps no server state to look
        # at, and a yes is followed by a question it cannot answer.
        assert catalog().answer(
            "select HAS_PERMS_BY_NAME(null, null, 'VIEW SERVER STATE') AS v"
        ).rows == [[0]]

    def test_no_statement_permission_is_held(self):
        # permissions() with nothing named is a bitmap of what may be done
        # to the database: create a table, a view, a procedure, back it up.
        # None of them, so every bit is off.
        assert catalog().answer("select permissions() AS v").rows == [[0]]

    def test_the_whole_probe_it_arrives_in_answers(self):
        # SSMS asks for seven things in one select, and refusing one of them
        # refused all seven. This is the query as it sends it.
        found = catalog().answer(
            "SELECT user_name(), @@MAX_PRECISION, is_member('db_owner'), "
            "permissions(), DatabasePropertyEx(db_name(), N'collation'), "
            "SERVERPROPERTY('IsFullTextInstalled'), schema_name()"
        )
        assert len(found.columns) == 7
        assert found.rows[0][0] == "dbo"
        assert found.rows[0][3] == 0

    def test_an_objects_permissions_are_refused_by_name(self):
        # Which bit means SELECT cannot be read off a server where the
        # caller is a sysadmin and every bit is set, so it is not guessed.
        with pytest.raises(QueryError, match="permissions.. of an object"):
            catalog().answer("select permissions(object_id('people')) AS v")


class TestWhatAStatementsValuesReach:
    """A parameter, and the details of the connection, inside a nesting.

    Each of these was answered with no parameters at all, so a variable read
    inside one came back null instead of its value and the functions that
    answer about the connection answered about nobody. Nothing said so: the
    query succeeded and the answer was wrong.
    """

    def asked(self, sql):
        return catalog().answer(Query(
            sql=sql,
            parameters={"@p": "given"},
            session={"login": r"DOMAIN\someone", "app": "a client",
                     "host": "a machine"},
        )).rows

    def test_a_parameter_reaches_a_subquery(self):
        assert self.asked("SELECT (SELECT @p) AS v") == [["given"]]

    def test_a_parameter_reaches_a_named_query(self):
        assert self.asked(
            "WITH one AS (SELECT @p AS v) SELECT v FROM one") == [["given"]]

    def test_a_parameter_reaches_a_derived_table(self):
        assert self.asked(
            "SELECT v FROM (SELECT @p AS v) AS d") == [["given"]]

    def test_the_connection_reaches_a_subquery(self):
        assert self.asked("SELECT (SELECT suser_sname()) AS v") == [
            [r"DOMAIN\someone"]]

    def test_the_connection_reaches_every_statement_of_a_batch(self):
        assert self.asked(
            "declare @n int = 1 select suser_sname() AS v") == [
            [r"DOMAIN\someone"]]

    def test_and_the_branch_a_batch_took(self):
        assert self.asked(
            "IF 1 = 1 SELECT app_name() AS v ELSE SELECT 0 AS v") == [
            ["a client"]]


class TestAVariableGivenARowsValue:
    """SELECT @v = something FROM a table, which reads before it assigns."""

    def test_it_takes_the_value_the_read_produced(self):
        assert catalog().answer(
            "declare @n int select @n = COUNT(*) FROM people select @n AS v"
        ).rows == [[2]]

    def test_it_takes_the_last_row_when_there_are_several(self):
        assert catalog().answer(
            "declare @n int select @n = id FROM people ORDER BY id "
            "select @n AS v"
        ).rows == [[2]]

    def test_no_rows_leaves_it_holding_what_it_had(self):
        assert catalog().answer(
            "declare @n int = 3 select @n = id FROM people WHERE id = 99 "
            "select @n AS v"
        ).rows == [[3]]

    def test_a_from_inside_a_subquery_is_not_this(self):
        # The FROM that decides belongs to the assignment, not to a scalar
        # subquery standing where the value goes.
        assert catalog().answer(
            "declare @n int select @n = (select COUNT(*) from people) "
            "select @n AS v"
        ).rows == [[2]]


class TestWhatADeclareSays:
    """The kind of column a variable makes, which it keeps when it is null.

    A value says what kind it is; a null one says nothing, and without the
    declaration a client was handed an int column as text.
    """

    def kind(self, sql):
        return type(catalog().answer(sql).columns[0].type).__name__

    def test_an_int_that_holds_nothing_is_still_an_int(self):
        assert self.kind("DECLARE @p int SELECT @p AS v") == "Integer"

    def test_and_text_is_text(self):
        assert self.kind("DECLARE @p nvarchar(50) SELECT @p AS v") == "NVarChar"

    def test_and_a_float_is_a_float(self):
        assert self.kind("DECLARE @p float SELECT @p AS v") == "Float"

    def test_and_a_bit_is_a_bit(self):
        assert self.kind("DECLARE @p bit SELECT @p AS v") == "Bit"

    def test_a_value_it_was_given_says_the_same_thing(self):
        assert self.kind("DECLARE @p int = 7 SELECT @p AS v") == "Integer"

    def test_a_set_from_a_subquery_that_matched_nothing_keeps_the_kind(self):
        # SET of a subquery with no rows is null, where SELECT ... FROM with
        # no rows leaves the variable alone. Either way the column is an int.
        found = catalog().answer(
            "DECLARE @p int = 3 SET @p = (SELECT id FROM people WHERE id = 99) "
            "SELECT @p AS v"
        )
        assert found.rows == [[None]]
        assert type(found.columns[0].type).__name__ == "Integer"

    def test_a_subquery_still_says_what_it_answered(self):
        # Its own answer is better evidence than a declaration, and wins.
        assert self.kind(
            "DECLARE @p nvarchar(50) SELECT (SELECT COUNT(*) FROM people) AS v"
        ) == "Integer"


class TestTheNameAQueryIsTold:
    """The name a client is given, and may open a connection to.

    SMO builds every urn out of SERVERPROPERTY('ServerName') and opens
    connections by it. On a real server that is the machine name and it
    works, because a real server listens on every address the name resolves
    to. This one usually listens on loopback alone, so the machine name is
    fifteen seconds of a client trying every address the machine has and
    finding nothing on any of them.
    """

    def asked(self, sql, server="127.0.0.1,1337"):
        return catalog().answer(Query(sql=sql, session={"server": server})).rows

    def test_the_server_name_is_where_the_client_reached_it(self):
        assert self.asked("SELECT SERVERPROPERTY('ServerName') AS v") == [
            ["127.0.0.1,1337"]]

    def test_the_variable_says_the_same_thing(self):
        assert self.asked("SELECT @@SERVERNAME AS v") == [["127.0.0.1,1337"]]

    def test_the_machine_name_is_still_the_machine(self):
        assert self.asked("SELECT SERVERPROPERTY('MachineName') AS v") == [
            [socket.gethostname()]]

    def test_the_urn_smo_builds_carries_it(self):
        assert self.asked(
            "SELECT 'Server[@Name=' + quotename(CAST("
            "serverproperty(N'Servername') AS sysname),'''') + ']' AS v"
        ) == [["Server[@Name='127.0.0.1,1337']"]]

    def test_with_nothing_to_say_it_falls_back_to_the_machine(self):
        found = catalog().answer(
            Query(sql="SELECT SERVERPROPERTY('ServerName') AS v", session={}))
        assert found.rows == [[socket.gethostname()]]

    def test_another_property_is_unaffected(self):
        assert self.asked("SELECT SERVERPROPERTY('ProductLevel') AS v") == [["RTM"]]

    def test_and_one_it_has_never_heard_of_is_still_null(self):
        assert self.asked("SELECT SERVERPROPERTY('nonsense') AS v") == [[None]]


class TestATryAndItsCatch:
    """BEGIN TRY ... END TRY BEGIN CATCH ... END CATCH.

    A client writes one around a question this server may not be able to
    answer, and the whole batch after it used to be lost: END CATCH begins
    with END, so the SELECT glued to it was run by nothing and the batch
    answered with no columns at all.
    """

    def test_the_try_runs_when_it_can(self):
        assert catalog().answer(
            "BEGIN TRY SELECT 1 AS v END TRY BEGIN CATCH SELECT 2 AS v END CATCH"
        ).rows == [[1]]

    def test_the_catch_runs_when_it_cannot(self):
        assert catalog().answer(
            "BEGIN TRY SELECT * FROM nope END TRY "
            "BEGIN CATCH SELECT 2 AS v END CATCH"
        ).rows == [[2]]

    def test_what_the_try_produced_before_failing_is_kept(self):
        # Measured with 8134 and 3902: a real server has already sent those
        # rows by the time the TRY fails, so they are the client's. This
        # dropped them, and said in its docstring that a real server dropped
        # them too, which was never true.
        found = catalog().answer(
            "BEGIN TRY SELECT 1 AS v SELECT * FROM nope END TRY "
            "BEGIN CATCH SELECT 2 AS v END CATCH"
        )
        assert [list(r) for r in found.rows] == [[1]]
        assert [[list(r) for r in one.rows] for one in found.following] == [[[2]]]

    def test_the_statement_after_it_is_its_own(self):
        found = catalog().answer(
            "BEGIN TRY SELECT 1 AS v END TRY BEGIN CATCH SELECT 2 AS v END CATCH "
            "SELECT 3 AS v"
        )
        assert found.rows == [[1]]
        assert found.following[0].rows == [[3]]

    def test_a_block_is_something_to_run(self):
        # A batch that is only a block used to answer nothing at all,
        # because nothing in it began with a word that runs.
        assert catalog().answer(
            "BEGIN TRY SELECT 7 AS v END TRY BEGIN CATCH SELECT 0 AS v END CATCH"
        ).columns[0].name == "v"


class TestABatchThatGoesOnPastAnError:
    """Some errors end only the statement that raised them.

    Measured on SQL Server 2025, one number at a time, each as
    `sqlcmd -Q "<the failing statement>; SELECT 'CONTINUED'"`. After 3902 and
    twelve others the rest of the batch runs and its answers reach the
    client behind the error, so the error is one of the answers rather than
    the whole of it. Before this, one error threw away everything a batch had
    already answered.
    """

    def answers(self, sql, session=None):
        """What each answer was: its rows, or the number it failed with."""
        found = catalog().answer(Query(
            sql=sql, parameters={},
            session={} if session is None else session,
        ))
        return [
            one.error.number if one.error is not None
            else [list(r) for r in one.rows]
            for one in (found, *found.following)
        ]

    def test_a_read_before_it_survives(self):
        assert self.answers("SELECT 1 AS v; COMMIT; SELECT 2 AS v") == [
            [[1]], 3902, [[2]]
        ]

    def test_a_failure_first_still_leaves_the_read(self):
        assert self.answers("COMMIT; SELECT 1 AS v") == [3902, [[1]]]

    def test_an_unknown_procedure_does_not_end_the_batch(self):
        # A batch that opens with EXEC is still a batch. Reading the whole of
        # it as one procedure name stopped it at the call, where a real
        # server runs everything after it.
        assert self.answers("EXEC no_such_proc; SELECT 'after' AS v") == [
            2812, [["after"]]
        ]

    def test_divide_by_zero_goes_on_too(self):
        assert self.answers("SELECT 1/0 AS v; SELECT 'after' AS v") == [
            8134, [["after"]]
        ]

    def test_an_error_that_ends_the_batch_keeps_what_ran_before_it(self):
        # 208 stops the batch on a real server, but only once it has run that
        # far: the read before it has already answered and is kept, and the
        # statement after it does not run.
        assert self.answers(
            "SELECT 1 AS v; SELECT * FROM nope; SELECT 'not reached' AS v"
        ) == [[[1]], 208]

    def test_and_still_raises_where_nothing_ran_before_it(self):
        with pytest.raises(QueryError) as caught:
            catalog().answer("SELECT * FROM nope")
        assert caught.value.number == 208

    def test_a_batch_that_is_only_an_error_still_raises(self):
        # The same bytes on the wire either way, and what every caller of
        # answer() already expects of a statement that could not be run.
        with pytest.raises(QueryError) as caught:
            catalog().answer("COMMIT")
        assert caught.value.number == 3902

    def test_inside_a_block_the_rest_of_the_block_runs(self):
        assert self.answers(
            "IF 1 = 1 BEGIN COMMIT; SELECT 1 AS v END; SELECT 2 AS v"
        ) == [3902, [[1]], [[2]]]

    def test_inside_a_statement_written_as_text(self):
        assert self.answers(
            "EXEC sp_executesql N'COMMIT; SELECT 1 AS v'; SELECT 2 AS v"
        ) == [3902, [[1]], [[2]]]

    def test_a_try_still_reaches_its_catch(self):
        # Nothing is caught inside a TRY, however deeply nested: catching it
        # first would leave the CATCH unreachable.
        assert self.answers(
            "BEGIN TRY COMMIT END TRY BEGIN CATCH SELECT 'caught' AS v END CATCH"
        ) == [[["caught"]]]

    def test_a_failure_leaves_the_row_count_at_nought(self):
        held = {}
        self.answers("SELECT 1 AS v UNION ALL SELECT 2; COMMIT", held)
        assert catalog().answer(Query(
            sql="SELECT @@ROWCOUNT AS n", parameters={}, session=held
        )).rows[0][0] == 0


class TestTheLastError:
    """@@ERROR, which a client reads to find out whether the last one worked.

    It answered NULL, so IF @@ERROR <> 0 was never true and a client's own
    error checking quietly never fired. Every case here was measured on SQL
    Server 2025 first. It matters more now that a batch outlives an error,
    because there is a statement after the failure to ask.
    """

    def last_row(self, sql):
        found = catalog().answer(Query(sql=sql, parameters={}, session={}))
        for one in reversed([found, *found.following]):
            if one.error is None and one.rows:
                return list(one.rows[-1])
        return None

    def test_nothing_has_failed_yet(self):
        assert self.last_row("SELECT @@ERROR AS e") == [0]

    def test_it_holds_the_number_of_the_last_failure(self):
        assert self.last_row("COMMIT; SELECT @@ERROR AS e") == [3902]

    def test_a_statement_that_worked_clears_it(self):
        assert self.last_row(
            "COMMIT; SELECT 1 AS one; SELECT @@ERROR AS e") == [0]

    def test_a_declare_with_no_value_leaves_it_standing(self):
        # The same exception @@ROWCOUNT makes for a bare DECLARE.
        assert self.last_row(
            "COMMIT; DECLARE @x int; SELECT @@ERROR AS e") == [3902]

    def test_a_declare_that_gives_a_value_clears_it(self):
        assert self.last_row(
            "COMMIT; DECLARE @e int = 1; SELECT @@ERROR AS e") == [0]

    def test_one_statement_reads_the_same_number_twice(self):
        assert self.last_row(
            "COMMIT; SELECT @@ERROR AS a, @@ERROR AS b") == [3902, 3902]

    def test_a_branch_that_failed_is_readable_after_the_if(self):
        assert self.last_row(
            "IF 1 = 1 BEGIN COMMIT END; SELECT @@ERROR AS e") == [3902]

    def test_an_if_that_ran_nothing_clears_it(self):
        assert self.last_row(
            "COMMIT; IF 1 = 0 SELECT 1 AS a; SELECT @@ERROR AS e") == [0]

    def test_a_statement_written_as_text_leaves_its_own(self):
        assert self.last_row(
            "EXEC sp_executesql N'COMMIT'; SELECT @@ERROR AS e") == [3902]

    def test_a_catch_reads_what_sent_it_there(self):
        assert self.last_row(
            "BEGIN TRY COMMIT END TRY "
            "BEGIN CATCH SELECT @@ERROR AS e END CATCH") == [3902]

    def test_and_the_try_clears_it_once_the_catch_is_done(self):
        assert self.last_row(
            "BEGIN TRY COMMIT END TRY BEGIN CATCH END CATCH; "
            "SELECT @@ERROR AS e") == [0]

    def test_divide_by_zero_leaves_its_own_number(self):
        assert self.last_row("SELECT 1/0 AS v; SELECT @@ERROR AS e") == [8134]

    def test_it_sits_beside_the_row_count(self):
        assert self.last_row(
            "COMMIT; SELECT @@ROWCOUNT AS r, @@ERROR AS e") == [0, 3902]


class TestReadingTheRegistry:
    """xp_instance_regread, which writes its answer into a variable.

    There is no registry. Refusing the call was worse than answering null,
    because the call sits in the middle of the one batch that carries the
    edition, the version and the server type, and a client that cannot run
    the batch loses all of them: SSMS then threw a null reference out of
    Server.ServerType and would not open a table.
    """

    @staticmethod
    def read(value):
        return catalog().answer(
            "declare @out sql_variant "
            "exec master.dbo.xp_instance_regread N'HKEY_LOCAL_MACHINE', "
            f"N'SOFTWARE', N'{value}', @out OUTPUT "
            "select @out AS v"
        ).rows

    def test_the_login_mode_is_windows_only(self):
        # Which is the whole of what this does, and what
        # SERVERPROPERTY('IsIntegratedSecurityOnly') already says.
        assert self.read("LoginMode") == [[1]]

    def test_nothing_is_audited_and_nothing_is_logged(self):
        assert self.read("AuditLevel") == [[0]]
        assert self.read("NumErrorLogs") == [[0]]

    def test_a_value_this_does_not_have_is_null(self):
        assert self.read("BackupDirectory") == [[None]]

    def test_the_sys_spelling_is_the_same_procedure(self):
        assert catalog().answer(
            "declare @out int "
            "EXEC master.sys.xp_instance_regread N'HKEY_LOCAL_MACHINE', "
            "N'SOFTWARE', N'LoginMode', @out OUTPUT "
            "select @out AS v"
        ).rows == [[1]]


class TestAFunctionCalledByItsWholeName:
    """msdb.dbo.fn_syspolicy_is_automation_enabled(), which is one function.

    A client writes where a function lives in front of what it is. The parts
    in front say where, and the last part says which.
    """

    def test_a_qualified_call_is_the_function_it_names(self):
        assert catalog().answer(
            "SELECT msdb.dbo.fn_syspolicy_is_automation_enabled() AS v"
        ).rows == [[0]]

    def test_it_agrees_with_the_table_that_says_the_same_thing(self):
        automation = catalog().answer(
            "SELECT msdb.dbo.fn_syspolicy_is_automation_enabled() AS v"
        ).rows[0][0]
        configured = catalog().answer(
            "SELECT current_value FROM msdb.dbo.syspolicy_configuration "
            "WHERE name = 'Enabled'"
        ).rows[0][0]
        assert automation == configured

    def test_a_qualified_name_that_is_not_a_function_says_so(self):
        with pytest.raises(QueryError, match="not a function"):
            catalog().answer("SELECT msdb.dbo.no_such_thing() AS v")

    def test_a_qualified_column_is_still_a_column(self):
        assert catalog().answer(
            "SELECT p.name FROM people AS p ORDER BY p.name"
        ).rows == [["ada"], ["grace"]]

    def test_a_sid_names_nobody_because_there_are_no_principals(self):
        assert catalog().answer("SELECT SID_BINARY('anyone') AS v").rows == [[None]]
        assert catalog().answer("SELECT suser_sname(0x01) AS v").rows == [[None]]

    def test_but_with_nothing_to_look_up_it_is_whoever_asked(self):
        found = catalog().answer(Query(
            sql="SELECT suser_sname() AS v",
            session={"login": r"DOMAIN\someone"}))
        assert found.rows == [[r"DOMAIN\someone"]]


class TestWhatAColumnIsCalled:
    """The heading a client reads, which it then looks the column up by.

    A qualified column is headed by its own name: SELECT t.name gives a
    column called name, not one called t.name. Keeping the qualifier made
    every heading in a joined query wrong by a prefix, and Power Query,
    which looks its columns up by name, was handed a null and stopped with
    "'column' argument cannot be null".
    """

    def test_a_qualified_column_is_headed_by_its_own_name(self):
        found = catalog().answer("SELECT p.name FROM people AS p")
        assert [c.name for c in found.columns] == ["name"]

    def test_qualified_by_the_table_rather_than_an_alias_as_well(self):
        found = catalog().answer("SELECT people.name FROM people")
        assert [c.name for c in found.columns] == ["name"]

    def test_an_alias_still_wins(self):
        found = catalog().answer("SELECT p.name AS who FROM people AS p")
        assert [c.name for c in found.columns] == ["who"]

    def test_the_values_are_the_column_it_named(self):
        assert catalog().answer(
            "SELECT p.name FROM people AS p ORDER BY p.name"
        ).rows == [["ada"], ["grace"]]


class TestAStarWithSomethingInFrontOfIt:
    """p.*, where p is what the query calls the table.

    Every column of p, whether p is the table's own name or the name the
    query gave it. Only the table's name was known, so the ordinary
    SELECT p.* FROM people AS p was refused outright.
    """

    def test_the_alias_names_the_table(self):
        found = catalog().answer("SELECT p.* FROM people AS p")
        assert [c.name for c in found.columns] == ["id", "name"]

    def test_and_so_does_the_table(self):
        found = catalog().answer("SELECT people.* FROM people")
        assert [c.name for c in found.columns] == ["id", "name"]

    def test_a_star_beside_a_column_of_the_same_table(self):
        found = catalog().answer("SELECT p.*, p.name FROM people AS p")
        assert [c.name for c in found.columns] == ["id", "name", "name"]

    def test_a_name_that_is_neither_is_refused(self):
        with pytest.raises(QueryError, match="names nothing this query reads"):
            catalog().answer("SELECT q.* FROM people AS p")

class TestValuesWrittenIntoAScratchTable:
    """INSERT INTO #t VALUES (...), which is the form everybody writes first.

    It did not work at all. Only INSERT ... SELECT did, and the VALUES form
    came back saying it had produced no rows to insert, because a VALUES
    list is not a statement and nothing else would answer it.

    Every case here was run against SQL Server and matches it, values and
    error numbers alike.
    """

    def session(self):
        c, held = catalog(), {}
        c.answer(Query(sql="CREATE TABLE #t (a int, b nvarchar(10))",
                       session=held))
        return c, held

    def rows(self, c, held):
        return [list(row) for row in c.answer(Query(
            sql="SELECT a, b FROM #t ORDER BY a", session=held)).rows]

    def test_one_row(self):
        c, held = self.session()
        c.answer(Query(sql="INSERT INTO #t VALUES (1, 'x')", session=held))
        assert self.rows(c, held) == [[1, "x"]]

    def test_several_rows_at_once(self):
        c, held = self.session()
        c.answer(Query(sql="INSERT INTO #t VALUES (1, 'x'), (2, 'y')",
                       session=held))
        assert self.rows(c, held) == [[1, "x"], [2, "y"]]

    def test_naming_the_columns(self):
        c, held = self.session()
        c.answer(Query(sql="INSERT INTO #t (a, b) VALUES (1, 'x')",
                       session=held))
        assert self.rows(c, held) == [[1, "x"]]

    def test_naming_only_some_leaves_the_rest_null(self):
        c, held = self.session()
        c.answer(Query(sql="INSERT INTO #t (a) VALUES (1)", session=held))
        assert self.rows(c, held) == [[1, None]]

    def test_a_row_may_be_worked_out(self):
        c, held = self.session()
        c.answer(Query(sql="INSERT INTO #t VALUES (6 + 1, UPPER('q'))",
                       session=held))
        assert self.rows(c, held) == [[7, "Q"]]

    def test_a_parameter_may_stand_in_a_row(self):
        c, held = self.session()
        c.answer(Query(sql="INSERT INTO #t VALUES (@n, @w)",
                       parameters={"@n": 4, "@w": "z"}, session=held))
        assert self.rows(c, held) == [[4, "z"]]

    @pytest.mark.parametrize("sql", [
        "INSERT INTO #t VALUES (1)",
        "INSERT INTO #t VALUES (1, 'x', 'extra')",
    ])
    def test_the_wrong_number_of_values_says_so(self, sql):
        c, held = self.session()
        with pytest.raises(QueryError) as bad:
            c.answer(Query(sql=sql, session=held))
        assert bad.value.number == 213

    def test_rows_of_different_widths_have_their_own_number(self):
        c, held = self.session()
        with pytest.raises(QueryError, match="must be the same") as bad:
            c.answer(Query(sql="INSERT INTO #t VALUES (1, 'x'), (2)",
                           session=held))
        assert bad.value.number == 10709

    def test_insert_from_a_select_still_works(self):
        c, held = self.session()
        c.answer(Query(sql="INSERT INTO #t (a) SELECT 1", session=held))
        assert self.rows(c, held) == [[1, None]]


class TestOneScratchTableOfEachName:
    """Making one twice, and dropping one that was never made.

    Both completed without a word. A client that has lost track of its own
    session is not helped by being told nothing, and a drop reported as
    having worked says a table went away that never existed.

    Measured: msg 2714 and msg 3701, in SQL Server's own words.
    """

    def test_making_it_twice_says_so(self):
        c, held = catalog(), {}
        c.answer(Query(sql="CREATE TABLE #t (a int)", session=held))
        with pytest.raises(QueryError, match="already an object") as bad:
            c.answer(Query(sql="CREATE TABLE #t (a int)", session=held))
        assert bad.value.number == 2714

    def test_and_the_first_one_is_left_alone(self):
        c, held = catalog(), {}
        c.answer(Query(sql="CREATE TABLE #t (a int)", session=held))
        c.answer(Query(sql="INSERT INTO #t VALUES (1)", session=held))
        with pytest.raises(QueryError):
            c.answer(Query(sql="CREATE TABLE #t (a int)", session=held))
        assert c.answer(Query(sql="SELECT a FROM #t", session=held)).rows == [[1]]

    def test_dropping_one_that_is_not_there_says_so(self):
        c, held = catalog(), {}
        with pytest.raises(QueryError, match="does not exist") as bad:
            c.answer(Query(sql="DROP TABLE #never", session=held))
        assert bad.value.number == 3701

    def test_dropping_it_twice_says_so_the_second_time(self):
        c, held = catalog(), {}
        c.answer(Query(sql="CREATE TABLE #t (a int)", session=held))
        c.answer(Query(sql="DROP TABLE #t", session=held))
        with pytest.raises(QueryError, match="does not exist"):
            c.answer(Query(sql="DROP TABLE #t", session=held))

    def test_another_session_cannot_drop_it(self):
        # It is one connection's scratch, and the refusal says the same
        # thing as if it had never been made, because to that session it
        # never was.
        c, mine = catalog(), {}
        c.answer(Query(sql="CREATE TABLE #t (a int)", session=mine))
        with pytest.raises(QueryError, match="does not exist"):
            c.answer(Query(sql="DROP TABLE #t", session={}))
        c.answer(Query(sql="SELECT a FROM #t", session=mine))

    def test_a_name_freed_by_a_drop_can_be_used_again(self):
        c, held = catalog(), {}
        c.answer(Query(sql="CREATE TABLE #t (a int)", session=held))
        c.answer(Query(sql="DROP TABLE #t", session=held))
        c.answer(Query(sql="CREATE TABLE #t (a int)", session=held))


class TestHowManyRowsTheLastStatementMade:
    """@@ROWCOUNT.

    It answered nought whatever had just happened, which is the answer a
    client reads as "nothing came back". Every case below was measured on
    SQL Server 2025 first; the comment on each says what it answered.
    """

    def answers(self, sql, session=None):
        result = catalog().answer(
            Query(sql=sql, parameters={}, session=session if session is not None else {})
        )
        return [list(row) for row in result.rows]

    def counted(self, sql, session=None):
        """The last answer of a batch, which is where @@ROWCOUNT is asked."""
        held = session if session is not None else {}
        result = catalog().answer(Query(sql=sql, parameters={}, session=held))
        last = result.following[-1] if result.following else result
        return [list(row) for row in last.rows]

    def test_a_read_counts_its_rows(self):
        assert self.counted("SELECT id FROM people; SELECT @@ROWCOUNT") == [[2]]

    def test_a_read_that_kept_nothing_counts_nought(self):
        assert self.counted(
            "SELECT id FROM people WHERE id > 99; SELECT @@ROWCOUNT") == [[0]]

    def test_asking_the_count_is_itself_a_read(self):
        # SQL Server answers 1 to the second one, because the first answered
        # a row. Nothing here is special-cased to avoid that.
        c, held = catalog(), {}
        c.answer(Query(sql="SELECT id FROM people", session=held))
        first = c.answer(Query(sql="SELECT @@ROWCOUNT", session=held))
        second = c.answer(Query(sql="SELECT @@ROWCOUNT", session=held))
        assert [list(r) for r in first.rows] == [[2]]
        assert [list(r) for r in second.rows] == [[1]]

    def test_it_is_kept_across_batches_on_one_connection(self):
        # A client sends the read and the question as two batches as often as
        # one, and the count belongs to the connection.
        c, held = catalog(), {}
        c.answer(Query(sql="SELECT id FROM people", session=held))
        answer = c.answer(Query(sql="SELECT @@ROWCOUNT", session=held))
        assert [list(r) for r in answer.rows] == [[2]]

    def test_a_bare_declare_leaves_it_alone(self):
        # Measured: 5 rows, then DECLARE, then the count, answers 5. It is
        # the one statement that does not settle a count of its own.
        assert self.counted(
            "SELECT id FROM people; DECLARE @v int; SELECT @@ROWCOUNT") == [[2]]

    def test_an_assignment_counts_one(self):
        assert self.counted(
            "SELECT id FROM people; DECLARE @v int; SET @v = 7; "
            "SELECT @@ROWCOUNT") == [[1]]

    def test_assigning_from_a_read_counts_what_the_read_made(self):
        assert self.counted(
            "DECLARE @n int; SELECT @n = COUNT(*) FROM people; "
            "SELECT @@ROWCOUNT") == [[1]]

    def test_making_a_table_counts_nought(self):
        assert self.counted(
            "SELECT id FROM people; CREATE TABLE #q (a int); "
            "SELECT @@ROWCOUNT") == [[0]]

    def test_a_statement_that_produces_nothing_counts_nought(self):
        # PRINT, measured, sets it to nought like everything else that made
        # no rows.
        assert self.counted(
            "SELECT id FROM people; PRINT 'x'; SELECT @@ROWCOUNT") == [[0]]

    def test_a_subquery_is_not_counted(self):
        # A working step inside a statement is not something the client saw.
        # The count is what the statement answered.
        assert self.counted(
            "SELECT id FROM people WHERE id IN (SELECT id FROM people); "
            "SELECT @@ROWCOUNT") == [[2]]

    def test_a_connection_that_has_run_nothing_says_nought(self):
        assert self.answers("SELECT @@ROWCOUNT") == [[0]]

    def test_two_connections_count_separately(self):
        c, mine, theirs = catalog(), {}, {}
        c.answer(Query(sql="SELECT id FROM people", session=mine))
        answer = c.answer(Query(sql="SELECT @@ROWCOUNT", session=theirs))
        assert [list(r) for r in answer.rows] == [[0]]


class TestTheTransactionCount:
    """@@TRANCOUNT, and the statements that move it.

    It answered nought whatever a batch did. Nothing here is written, so there
    is nothing to commit or roll back, but a client asks for the count and
    decides what to send next by the answer. Every case was measured on SQL
    Server 2025 first, and the comment on each says what it answered.
    """

    def run(self, sql, session):
        return catalog().answer(Query(sql=sql, parameters={}, session=session))

    def count(self, session):
        return self.run("SELECT @@TRANCOUNT AS n", session).rows[0][0]

    def refused(self, sql, session=None):
        with pytest.raises(QueryError) as caught:
            self.run(sql, session if session is not None else {})
        return caught.value.number

    def test_nesting_is_counted_and_unwound(self):
        # 0, then 1 and 2 as two are begun, 1 after a COMMIT, and 0 after a
        # ROLLBACK, which ends however many there are.
        held = {}
        seen = [self.count(held)]
        for sql in ("BEGIN TRAN", "BEGIN TRANSACTION", "COMMIT", "BEGIN TRAN",
                    "ROLLBACK"):
            self.run(sql, held)
            seen.append(self.count(held))
        assert seen == [0, 1, 2, 1, 2, 0]

    def test_it_is_kept_across_batches_and_per_connection(self):
        c, mine, theirs = catalog(), {}, {}
        c.answer(Query(sql="BEGIN TRAN", session=mine))
        assert self.count(mine) == 1
        assert self.count(theirs) == 0

    def test_one_batch_sees_its_own_begin(self):
        answer = self.run("BEGIN TRAN; SELECT @@TRANCOUNT AS n; COMMIT", {})
        assert [list(r) for r in answer.rows] == [[1]]

    def test_commit_ends_one_level_whatever_it_is_called(self):
        # COMMIT TRAN whatever after two BEGINs leaves one: COMMIT takes a
        # name and does nothing with it.
        held = {}
        self.run("BEGIN TRAN t1; BEGIN TRAN t2; COMMIT TRAN whatever", held)
        assert self.count(held) == 1

    def test_commit_and_rollback_with_nothing_open_are_refused(self):
        # 3902 and 3903, in SQL Server's words. A name makes no difference.
        assert self.refused("COMMIT") == 3902
        assert self.refused("COMMIT TRANSACTION") == 3902
        assert self.refused("ROLLBACK") == 3903
        assert self.refused("ROLLBACK TRAN anything") == 3903

    def test_a_savepoint_with_nothing_open_is_refused(self):
        assert self.refused("SAVE TRAN s1") == 628

    def test_rolling_back_to_a_savepoint_keeps_the_transaction(self):
        held = {}
        self.run("BEGIN TRAN; SAVE TRAN s1; ROLLBACK TRAN s1", held)
        assert self.count(held) == 1

    def test_rolling_back_to_a_savepoint_releases_it_and_those_after(self):
        # After ROLLBACK TRAN a, both a and the b marked after it are gone.
        held = {}
        self.run("BEGIN TRAN; SAVE TRAN a; SAVE TRAN b; ROLLBACK TRAN a", held)
        assert self.refused("ROLLBACK TRAN b", held) == 6401
        assert self.refused("ROLLBACK TRAN a", held) == 6401
        assert self.count(held) == 1

    def test_a_name_marked_twice_is_rolled_back_to_twice(self):
        held = {}
        self.run("BEGIN TRAN; SAVE TRAN a; SAVE TRAN a; ROLLBACK TRAN a; "
                 "ROLLBACK TRAN a", held)
        assert self.refused("ROLLBACK TRAN a", held) == 6401

    def test_an_unknown_name_is_refused_and_changes_nothing(self):
        held = {}
        self.run("BEGIN TRAN", held)
        assert self.refused("ROLLBACK TRAN nosuch", held) == 6401
        assert self.count(held) == 1

    def test_only_the_outermost_transaction_is_known_by_name(self):
        # Rolling back to an inner transaction's name is 6401, and to the
        # outer one's ends them both.
        held = {}
        self.run("BEGIN TRAN outer1; BEGIN TRAN inner1", held)
        assert self.refused("ROLLBACK TRAN inner1", held) == 6401
        self.run("ROLLBACK TRAN outer1", held)
        assert self.count(held) == 0

    def test_a_savepoint_is_found_before_a_transaction_of_its_name(self):
        held = {}
        self.run("BEGIN TRAN t; SAVE TRAN t; ROLLBACK TRAN t", held)
        assert self.count(held) == 1

    def test_savepoints_ignore_case_and_transactions_do_not(self):
        # On a server that ignores case: sp rolls back to Sp, and abc is not
        # the transaction begun as Abc.
        held = {}
        self.run("BEGIN TRAN Abc; SAVE TRAN Sp; ROLLBACK TRAN sp", held)
        assert self.refused("ROLLBACK TRAN abc", held) == 6401
        assert self.count(held) == 1

    def test_each_leaves_the_row_count_at_nought(self):
        held = {}
        self.run("SELECT id FROM people", held)
        self.run("BEGIN TRAN", held)
        answer = self.run("SELECT @@ROWCOUNT", held)
        assert [list(r) for r in answer.rows] == [[0]]

    def test_an_if_can_ask_it(self):
        # IF @@TRANCOUNT > 0 COMMIT TRAN is how a transaction is ended only
        # where one is open. It was refused outright, because nothing told
        # the IF where its condition stopped.
        held = {}
        self.run("BEGIN TRAN", held)
        self.run("IF @@TRANCOUNT > 0 COMMIT TRAN", held)
        assert self.count(held) == 0
        self.run("IF @@TRANCOUNT > 0 ROLLBACK TRAN", held)
        assert self.count(held) == 0

    def test_an_if_sees_the_connection_s_other_variables(self):
        # Measured before the change: IF @@SPID IS NULL was true. An IF was
        # given the batch's variables and none of the connection's, so every
        # @@ variable was null to it and IF @@ROWCOUNT = 0 never held.
        answer = self.run("IF @@SPID = 51 SELECT 1 AS one", {})
        assert [list(r) for r in answer.rows] == [[1]]
        answer = self.run("IF @@ROWCOUNT = 0 SELECT 1 AS one", {})
        assert [list(r) for r in answer.rows] == [[1]]

    def test_an_if_can_begin_one(self):
        held = {}
        self.run("IF @@TRANCOUNT = 0 BEGIN TRAN", held)
        assert self.count(held) == 1

    def test_an_if_that_begins_one_leaves_the_rest_of_the_batch_alone(self):
        # BEGIN TRAN has no END. Read as a block, it would take everything
        # after it in the batch into the branch, the SELECT included.
        answer = self.run(
            "IF @@TRANCOUNT = 0 BEGIN TRAN; SELECT @@TRANCOUNT AS n", {})
        assert [list(r) for r in answer.rows] == [[1]]

    def test_without_semicolons_the_read_still_comes_back(self):
        # Written a line apiece, the SELECT was taken for part of BEGIN TRAN
        # and the batch answered nothing, with nothing to say so.
        alone = self.run("SELECT id FROM people", {})
        answer = self.run("BEGIN TRAN\nSELECT id FROM people\nCOMMIT", {})
        assert alone.rows
        assert [list(r) for r in answer.rows] == [list(r) for r in alone.rows]

    def test_the_words_are_matched_whatever_their_case(self):
        answer = self.run(
            "begin transaction; select @@trancount as n; commit transaction", {})
        assert [list(r) for r in answer.rows] == [[1]]

    def test_a_bracketed_name_and_a_mark(self):
        held = {}
        self.run("BEGIN TRAN [my tran] WITH MARK 'deploy'", held)
        self.run("ROLLBACK TRAN [my tran]", held)
        assert self.count(held) == 0


class TestJoiningAcrossTwoKinds:
    """A join whose sides are different types, which the hash could not do.

    The join buckets rows by collated(value) and only compares the ones whose
    keys met. collated keeps the collation and nothing else, so a key never
    crossed a type: a table of numbers joined to the same numbers written as
    text answered nothing at all, where the same condition in a WHERE
    answered both rows. Measured against SQL Server 2025, which answers both.
    """

    def catalog(self):
        c = Catalog()
        c.add(from_records([{"n": 1}, {"n": 2}, {"n": 3}], name="numbers"))
        # "x" is what keeps the column text; a column of "1" and "2" alone is
        # read as integers and proves nothing.
        c.add(from_records([{"code": "1"}, {"code": "2"}, {"code": "x"}],
                           name="codes"))
        c.add(from_records([{"d": datetime.datetime(2024, 1, 15)}],
                           name="moments"))
        c.add(from_records([{"written": "2024-01-15"}], name="written"))
        return c

    def answered(self, sql):
        c = self.catalog()
        try:
            found = c.answer(Query(sql=sql, parameters={}, session={}))
        except QueryError as exc:
            return f"refused {exc.number}"
        return sorted([list(row) for row in found.rows], key=repr)

    @pytest.mark.parametrize("joined, filtered", [
        ("SELECT a.n FROM numbers a JOIN codes b ON a.n = b.code",
         "SELECT a.n FROM numbers a, codes b WHERE a.n = b.code"),
        ("SELECT a.n FROM numbers a JOIN codes b ON b.code = a.n",
         "SELECT a.n FROM numbers a, codes b WHERE b.code = a.n"),
        ("SELECT a.d FROM moments a JOIN written b ON a.d = b.written",
         "SELECT a.d FROM moments a, written b WHERE a.d = b.written"),
        ("SELECT a.d FROM moments a LEFT JOIN written b ON a.d = b.written",
         "SELECT a.d FROM moments a, written b WHERE a.d = b.written"),
    ])
    def test_a_join_answers_what_the_same_condition_answers(self, joined,
                                                            filtered):
        assert self.answered(joined) == self.answered(filtered)

    def test_a_join_of_one_kind_still_hashes(self):
        # The fast path is kept where it is faithful, which is where the two
        # sides are the same kind of thing. Integers and floats count as one.
        from pysqlbridge.catalog import _buckets_alike
        from pysqlbridge.tds.result import Column, DateTime, Float, Integer, NVarChar

        assert _buckets_alike(Column("a", Integer(4)), Column("b", Integer(8)))
        assert _buckets_alike(Column("a", Integer(4)), Column("b", Float(8)))
        assert _buckets_alike(Column("a", NVarChar(5)), Column("b", NVarChar(9)))
        assert not _buckets_alike(Column("a", Integer(4)), Column("b", NVarChar(5)))
        assert not _buckets_alike(Column("a", DateTime()), Column("b", NVarChar(5)))
        assert not _buckets_alike(Column("a", DateTime()), Column("b", Integer(4)))

    def test_a_join_refuses_text_that_is_not_a_number(self):
        # Measured: the 'x' in codes is message 245 even though it matches
        # nothing, because every candidate has to become an int first.
        assert self.answered(
            "SELECT a.n FROM numbers a JOIN codes b ON a.n = b.code"
        ) == "refused 245"

    def test_a_bare_name_in_the_select_list_after_a_join(self):
        # A join renames its columns to table.column, so a bare n found
        # nothing at all. Measured: a real server answers it.
        c = self.catalog()
        c.add(from_records([{"code": 1}, {"code": 2}], name="whole"))
        found = c.answer(Query(sql="SELECT n FROM numbers JOIN whole "
                                   "ON n = code", parameters={}, session={}))
        assert sorted([list(row) for row in found.rows], key=repr) == [[1], [2]]

    def test_a_bare_name_both_joined_tables_have(self):
        # Measured: message 209, which is not the 207 for a name that is not
        # there, because the name is spelled right.
        c = self.catalog()
        with pytest.raises(QueryError) as raised:
            c.answer(Query(sql="SELECT n FROM numbers a JOIN numbers b "
                               "ON a.n = b.n", parameters={}, session={}))
        assert raised.value.number == 209


class TestWhatAnIfLeavesTheCountAt:
    """@@ROWCOUNT around a branch, measured statement by statement.

    An IF produced no rows of its own whatever it went on to run, and the
    count said so. This left the last read's count standing instead, so a
    client that guarded a read with an IF and then asked how much came back
    was told about a different statement.
    """

    def counted(self, sql):
        c = catalog()
        result = c.answer(Query(sql=sql, parameters={}, session={}))
        last = result.following[-1] if result.following else result
        return [list(row) for row in last.rows]

    def test_a_branch_not_taken_counts_nought(self):
        assert self.counted("SELECT id FROM people; IF (1 = 0) SELECT 1; "
                            "SELECT @@ROWCOUNT") == [[0]]

    def test_a_branch_holding_only_a_declare_counts_nought(self):
        assert self.counted("SELECT id FROM people; "
                            "IF (1 = 1) BEGIN DECLARE @v int END; "
                            "SELECT @@ROWCOUNT") == [[0]]

    def test_a_read_inside_a_branch_counts_its_own_rows(self):
        assert self.counted("IF (1 = 1) SELECT id FROM people; "
                            "SELECT @@ROWCOUNT") == [[2]]

    def test_a_read_inside_a_try_counts_its_own_rows(self):
        assert self.counted(
            "BEGIN TRY SELECT id FROM people END TRY "
            "BEGIN CATCH SELECT 0 END CATCH; SELECT @@ROWCOUNT") == [[2]]

    def test_a_catch_counts_what_the_catch_read(self):
        assert self.counted(
            "BEGIN TRY SELECT nope FROM people END TRY "
            "BEGIN CATCH SELECT 1 END CATCH; SELECT @@ROWCOUNT") == [[1]]

    def test_a_read_run_from_a_string_counts_its_rows(self):
        assert self.counted("EXEC('SELECT id FROM people'); "
                            "SELECT @@ROWCOUNT") == [[2]]
