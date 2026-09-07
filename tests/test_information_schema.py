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
        with pytest.raises(QueryError, match="COLUMNS, SCHEMATA, TABLES"):
            catalog().answer(Query(sql="SELECT * FROM INFORMATION_SCHEMA.ROUTINES"))

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
