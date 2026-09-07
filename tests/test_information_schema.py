import pytest

from pysqlbridge.catalog import Catalog
from pysqlbridge.source import from_records
from pysqlbridge.tds.result import Query, QueryError


def catalog() -> Catalog:
    c = Catalog()
    c.add(from_records([{"id": 1, "name": "ada"}], name="people"))
    c.add(from_records([{"city": "Oslo", "population": 709037}], name="cities"))
    return c


class TestTablesView:
    def test_one_row_per_served_table(self):
        result = catalog().answer(Query(sql="SELECT * FROM INFORMATION_SCHEMA.TABLES"))
        assert [row[2] for row in result.rows] == ["cities", "people"]

    def test_column_names_match_sql_server(self):
        # Clients select these by name and then read them positionally.
        result = catalog().answer(Query(sql="SELECT * FROM INFORMATION_SCHEMA.TABLES"))
        assert [c.name for c in result.columns] == [
            "TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME", "TABLE_TYPE",
        ]

    def test_tables_are_reported_as_base_tables(self):
        result = catalog().answer(Query(sql="SELECT * FROM INFORMATION_SCHEMA.TABLES"))
        assert {row[3] for row in result.rows} == {"BASE TABLE"}

    def test_the_query_a_real_client_sends(self):
        # Measured from .NET's GetSchema("Tables"), parameters and all. Every
        # parameter is null, so each comparison is unknown and only the
        # IS NULL beside it makes the clause true.
        sql = (
            "select TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE "
            "from INFORMATION_SCHEMA.TABLES where "
            "(TABLE_CATALOG = @Catalog or (@Catalog is null)) and "
            "(TABLE_SCHEMA = @Owner or (@Owner is null)) and "
            "(TABLE_NAME = @Name or (@Name is null)) and "
            "(TABLE_TYPE = @TableType or (@TableType is null))"
        )
        params = {"@Catalog": None, "@Owner": None, "@Name": None, "@TableType": None}
        result = catalog().answer(Query(sql=sql, parameters=params))
        assert len(result.rows) == 2

    def test_the_same_query_filters_when_a_name_is_given(self):
        sql = ("select TABLE_NAME from INFORMATION_SCHEMA.TABLES where "
               "(TABLE_NAME = @Name or (@Name is null))")
        result = catalog().answer(Query(sql=sql, parameters={"@Name": "people"}))
        assert [row[0] for row in result.rows] == ["people"]


class TestColumnsView:
    def test_one_row_per_column_of_every_table(self):
        result = catalog().answer(Query(sql="SELECT * FROM INFORMATION_SCHEMA.COLUMNS"))
        assert len(result.rows) == 4   # two columns in each of two tables

    def test_reports_sql_type_names_not_tds_type_bytes(self):
        sql = ("SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
               "WHERE TABLE_NAME = 'cities'")
        result = catalog().answer(Query(sql=sql))
        assert dict(result.rows) == {"city": "nvarchar", "population": "int"}

    def test_ordinal_positions_start_at_one(self):
        sql = ("SELECT ORDINAL_POSITION FROM INFORMATION_SCHEMA.COLUMNS "
               "WHERE TABLE_NAME = 'people'")
        result = catalog().answer(Query(sql=sql))
        assert sorted(row[0] for row in result.rows) == [1, 2]

    def test_every_column_is_reported_nullable(self):
        # The sources carry no constraints, so claiming NO would be a promise
        # the data does not make.
        result = catalog().answer(
            Query(sql="SELECT IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS"))
        assert {row[0] for row in result.rows} == {"YES"}


class TestUnknownViews:
    def test_an_unknown_catalog_view_lists_the_known_ones(self):
        with pytest.raises(QueryError, match="CHECK_CONSTRAINTS, COLUMNS"):
            catalog().answer(Query(sql="SELECT * FROM INFORMATION_SCHEMA.NONSENSE"))

    def test_every_view_a_real_server_has_is_here(self):
        # Twenty-one of them. A client that asks about routines or
        # constraints and is told there is no such view gets an error about
        # a name it did not choose, where the answer it wanted was no rows.
        found = catalog().answer(Query(
            sql="SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES"))
        assert found.rows                    # the served tables, not the views
        for view in ("ROUTINES", "PARAMETERS", "VIEWS", "TABLE_CONSTRAINTS",
                     "KEY_COLUMN_USAGE", "REFERENTIAL_CONSTRAINTS",
                     "CHECK_CONSTRAINTS", "DOMAINS", "SEQUENCES",
                     "TABLE_PRIVILEGES", "COLUMN_PRIVILEGES"):
            answer = catalog().answer(
                Query(sql=f"SELECT * FROM INFORMATION_SCHEMA.{view}"))
            assert answer.rows == [], view
            assert answer.columns, f"{view} has no columns"

    def test_the_columns_view_is_as_wide_as_a_real_one(self):
        found = catalog().answer(Query(sql="SELECT * FROM INFORMATION_SCHEMA.COLUMNS"))
        assert len(found.columns) == 23

    def test_a_text_column_reports_what_describes_text(self):
        found = catalog().answer(Query(
            sql="SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, "
                "CHARACTER_OCTET_LENGTH, COLLATION_NAME, NUMERIC_PRECISION "
                "FROM INFORMATION_SCHEMA.COLUMNS WHERE COLUMN_NAME = 'name'"))
        kind, chars, octets, collation, precision = found.rows[0]
        assert kind == "nvarchar"
        assert octets == chars * 2       # two bytes a character
        assert collation and precision is None

    def test_a_number_reports_what_describes_a_number(self):
        found = catalog().answer(Query(
            sql="SELECT DATA_TYPE, NUMERIC_PRECISION, NUMERIC_PRECISION_RADIX, "
                "NUMERIC_SCALE, CHARACTER_MAXIMUM_LENGTH "
                "FROM INFORMATION_SCHEMA.COLUMNS WHERE COLUMN_NAME = 'id'"))
        kind, precision, radix, scale, chars = found.rows[0]
        assert (kind, precision, radix, scale) == ("int", 10, 10, 0)
        assert chars is None

    def test_a_user_table_named_tables_is_not_the_catalog_view(self):
        # The schema is what tells them apart, which is why the parser keeps it.
        c = catalog()
        c.add(from_records([{"x": 1}], name="TABLES"))
        assert c.answer(Query(sql="SELECT * FROM TABLES")).rows == [[1]]
        assert len(c.answer(
            Query(sql="SELECT * FROM INFORMATION_SCHEMA.TABLES")).rows) == 3


class TestStoredProcedures:
    def test_an_unimplemented_procedure_is_an_error_not_silence(self):
        # A client that asked for a procedure's result set and received nothing
        # raised a NullReferenceException, with no error to explain it.
        with pytest.raises(QueryError, match="could not find stored procedure") as caught:
            catalog().answer(Query(sql="EXEC sp_who2"))
        assert caught.value.number == 2812

    def test_a_catalog_procedure_answers_with_its_rowset(self):
        # sys.sp_columns_managed is what .NET SqlClient asks for, and it used
        # to be the example of a procedure this did not have.
        result = catalog().answer(
            Query(sql="EXEC sys.sp_columns_managed @Catalog, @Owner, @Table"))
        assert [c.name for c in result.columns][:4] == [
            "TABLE_QUALIFIER", "TABLE_OWNER", "TABLE_NAME", "COLUMN_NAME"
        ]
        assert result.rows

    def test_setup_batches_still_complete_quietly(self):
        result = catalog().answer(Query(sql="SET TEXTSIZE 4096"))
        assert result.columns == [] and result.rows == []


class TestTheDatabaseViews:
    """sys.databases and sys.database_mirroring, which the tree reads.

    Both are served whole rather than to the width of one client's query: a
    column that is missing is an error rather than a null, and finding them
    one refusal at a time is how the Databases node stayed empty.
    """

    def test_every_column_a_real_server_returns_is_there(self):
        # 98 of them, read off SQL Server 2025 rather than the documentation.
        found = catalog().answer(Query(sql="SELECT * FROM sys.databases"))
        assert len(found.columns) == 98

    def test_the_first_columns_are_in_the_order_a_client_reads_them(self):
        found = catalog().answer(Query(sql="SELECT * FROM sys.databases"))
        assert [c.name for c in found.columns][:6] == [
            "name", "database_id", "source_database_id", "owner_sid",
            "create_date", "compatibility_level",
        ]

    def test_the_one_database_is_online_and_read_only(self):
        found = catalog().answer(Query(
            sql="SELECT state_desc, is_read_only, user_access_desc "
                "FROM sys.databases"))
        assert found.rows == [["ONLINE", True, "MULTI_USER"]]

    def test_the_newer_columns_answer_too(self):
        # is_ledger_on is the last one the Databases node reads, and the one
        # a view built to the width of an older client did not have.
        assert catalog().answer(Query(
            sql="SELECT is_ledger_on, catalog_collation_type_desc, "
                "is_optimized_locking_on FROM sys.databases"
        )).rows == [[False, "DATABASE_DEFAULT", False]]

    def test_the_database_is_named_by_what_is_served(self):
        found = catalog().answer(Query(
            sql="SELECT name, physical_database_name FROM sys.databases"))
        assert found.rows[0][0] == found.rows[0][1]

    def test_mirroring_has_a_row_for_it_and_mirrors_nothing(self):
        # A LEFT JOIN to a view that is not there is an error, not a null,
        # so the view exists and every mirroring column in it is null.
        found = catalog().answer(Query(sql="SELECT * FROM sys.database_mirroring"))
        assert len(found.rows) == 1
        assert found.rows[0][1:] == [None] * 20

    def test_the_two_views_agree_on_which_database_this_is(self):
        one = catalog().answer(Query(sql="SELECT database_id FROM sys.databases"))
        other = catalog().answer(
            Query(sql="SELECT database_id FROM sys.database_mirroring"))
        assert one.rows == other.rows


class TestTheViewsThatDescribeTheTables:
    """sys.tables and what the tree joins to it.

    Object Explorer lists a database's tables out of these, and refused every
    one of them while the views were missing: an empty Tables node, and no
    error a person could see.
    """

    def test_one_row_per_table_served(self):
        found = catalog().answer(Query(
            sql="SELECT name FROM sys.tables ORDER BY name"))
        assert found.rows == [["cities"], ["people"]]

    def test_a_table_is_the_users_own_not_the_servers(self):
        # is_ms_shipped is how a client decides whether a table belongs under
        # Tables or under System Tables. These are the user's.
        assert catalog().answer(Query(
            sql="SELECT is_ms_shipped FROM sys.tables"
        )).rows == [[False], [False]]

    def test_the_number_a_table_has_is_the_one_object_id_gives(self):
        one = catalog().answer(Query(
            sql="SELECT object_id FROM sys.tables WHERE name = 'people'"))
        other = catalog().answer(Query(sql="SELECT OBJECT_ID('people') AS v"))
        assert one.rows == other.rows

    def test_every_column_of_every_table_is_in_all_columns(self):
        found = catalog().answer(Query(
            sql="SELECT COUNT(*) AS n FROM sys.all_columns"))
        assert found.rows == [[4]]        # two of people, two of cities

    def test_a_column_carries_the_type_number_sql_server_gives_it(self):
        # 56 is int and 231 is nvarchar, and a client reads the number
        # rather than the name.
        found = catalog().answer(Query(
            sql="SELECT name, system_type_id FROM sys.all_columns "
                "WHERE object_id = OBJECT_ID('people') ORDER BY column_id"))
        assert found.rows == [["id", 56], ["name", 231]]

    def test_a_table_with_no_index_still_has_a_heap(self):
        # index_id 0, type HEAP. The Tables node joins this and a table with
        # no row here does not appear at all.
        found = catalog().answer(Query(
            sql="SELECT COUNT(*) AS n FROM sys.indexes WHERE index_id = 0"))
        assert found.rows == [[2]]

    def test_the_schema_everything_is_in(self):
        assert catalog().answer(Query(
            sql="SELECT name, schema_id FROM sys.schemas")).rows == [["dbo", 1]]

    def test_the_views_it_has_none_of_are_empty_not_missing(self):
        for view in ("sys.all_views", "sys.views", "sys.extended_properties",
                     "sys.filetables", "sys.change_tracking_databases"):
            found = catalog().answer(Query(sql=f"SELECT * FROM {view}"))
            assert found.rows == [], view
            assert found.columns, f"{view} has no columns"

    def test_schema_name_resolves_the_id_the_tables_carry(self):
        assert catalog().answer(Query(
            sql="SELECT SCHEMA_NAME(tbl.schema_id) AS s FROM sys.tables AS tbl "
                "WHERE tbl.name = 'people'"
        )).rows == [["dbo"]]
