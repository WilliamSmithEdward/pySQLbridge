import json

import pathlib

import pytest

from pysqlbridge import procedures
from pysqlbridge.catalog import INVALID_OBJECT_NAME, UNSUPPORTED, Catalog, load
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
    def test_an_unknown_table_uses_the_invalid_object_name_number(self):
        # 208 is what SQL Server sends, and clients already present it well.
        with pytest.raises(QueryError, match="invalid object name 'nope'") as caught:
            catalog().answer("SELECT * FROM nope")
        assert caught.value.number == INVALID_OBJECT_NAME == 208

    def test_an_unknown_table_lists_what_is_available(self):
        with pytest.raises(QueryError, match="cities, people"):
            catalog().answer("SELECT * FROM nope")

    def test_an_unknown_column_is_also_an_invalid_object_name(self):
        with pytest.raises(QueryError, match="invalid column name 'nope'") as caught:
            catalog().answer("SELECT nope FROM people")
        assert caught.value.number == INVALID_OBJECT_NAME

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
        with pytest.raises(QueryError, match="supplies 2 columns"):
            self.run("INSERT #x SELECT id, name FROM people", here)

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

    def test_applying_something_that_is_not_values_is_refused(self):
        with pytest.raises(QueryError, match="written out with VALUES"):
            catalog().answer(
                "SELECT t.* FROM people CROSS APPLY ( SELECT 1 ) t(a)"
            )


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
        session = {"login": "DOMAIN\someone", "app": "a client", "host": "a machine"}
        session.update(extra)
        return Query(sql="", session=session)

    def one(self, sql, **extra):
        request = self.about(**extra)
        return catalog().answer(
            Query(sql=sql, session=request.session)
        ).rows[0][0]

    def test_it_reports_who_authenticated(self):
        assert self.one("SELECT suser_sname()") == "DOMAIN\someone"
        assert self.one("SELECT original_login()") == "DOMAIN\someone"

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

    def test_nothing_is_configured_and_the_view_says_so(self):
        found = catalog().answer(
            "select value_in_use from sys.configurations where configuration_id = 16384"
        )
        assert found.rows == []
        assert [c.name for c in found.columns] == ["value_in_use"]


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
