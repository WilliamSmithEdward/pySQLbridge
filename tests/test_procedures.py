import pytest

from pysqlbridge import procedures
from pysqlbridge.catalog import STORED_PROCEDURE_NOT_FOUND, Catalog
from pysqlbridge.source import from_records
from pysqlbridge.tds.result import Query, QueryError


def catalog() -> Catalog:
    c = Catalog()
    c.add(from_records(
        [{"id": 1, "name": "ada", "score": 1.5}, {"id": 2, "name": "grace", "score": 2.0}],
        name="people",
    ))
    c.add(from_records([{"city": "Oslo"}], name="cities"))
    return c


def call(sql: str) -> object:
    return catalog().answer(Query(sql=sql))


class TestNaming:
    @pytest.mark.parametrize("written", [
        "sp_tables", "sys.sp_tables", "[sys].sp_tables", "master.dbo.sp_tables",
        "SP_TABLES", " sp_tables ", "sp_tables;",
    ])
    def test_every_way_a_client_qualifies_it(self, written):
        # Drivers write these differently and mean the same call.
        assert procedures.normalise(written) == "sp_tables"

    def test_something_else_is_not_a_catalog_procedure(self):
        assert not procedures.known("sp_who2")


class TestTables:
    def test_it_lists_every_table(self):
        result = call("EXEC sp_tables")
        assert [row[2] for row in result.rows] == ["cities", "people"]

    def test_the_columns_are_the_ones_odbc_reads_by_position(self):
        # A driver finding SCALE where it expects RADIX does not report a
        # mismatch, it reports the wrong type.
        assert [c.name for c in call("EXEC sp_tables").columns] == [
            "TABLE_QUALIFIER", "TABLE_OWNER", "TABLE_NAME", "TABLE_TYPE", "REMARKS"
        ]

    def test_each_row_is_qualified_and_typed(self):
        row = call("EXEC sp_tables").rows[0]
        assert row[0] == "pysqlbridge" and row[1] == "dbo" and row[3] == "TABLE"

    def test_a_name_filter_narrows_it(self):
        assert [r[2] for r in call("EXEC sp_tables 'people'").rows] == ["people"]

    def test_a_wildcard_filter_narrows_it(self):
        assert [r[2] for r in call("EXEC sp_tables 'c%'").rows] == ["cities"]

    def test_a_percent_means_everything(self):
        assert len(call("EXEC sp_tables '%'").rows) == 2

    def test_an_unbound_marker_is_not_a_filter(self):
        # EXEC sp_tables @Table with nothing bound to @Table is asking for
        # every table. Reading it as a table named "@Table" answers with none.
        # A client sends it parameterised, with @Table declared and null;
        # sent with no parameter at all, a real server calls it msg 137.
        found = catalog().answer(Query(sql="EXEC sp_tables @Table",
                                       parameters={"@Table": None}))
        assert len(found.rows) == 2

    def test_asking_only_for_views_gets_none(self):
        assert call("EXEC sp_tables NULL, NULL, NULL, \"'VIEW'\"").rows == []


class TestColumns:
    def test_it_lists_every_column_of_every_table(self):
        assert len(call("EXEC sp_columns").rows) == 4

    def test_the_columns_are_odbc_s_layout(self):
        assert [c.name for c in call("EXEC sp_columns").columns] == [
            "TABLE_QUALIFIER", "TABLE_OWNER", "TABLE_NAME", "COLUMN_NAME",
            "DATA_TYPE", "TYPE_NAME", "PRECISION", "LENGTH", "SCALE", "RADIX",
            "NULLABLE", "REMARKS", "COLUMN_DEF", "SQL_DATA_TYPE",
            "SQL_DATETIME_SUB", "CHAR_OCTET_LENGTH", "ORDINAL_POSITION",
            "IS_NULLABLE", "SS_DATA_TYPE",
        ]

    def test_a_table_filter_narrows_it(self):
        rows = call("EXEC sp_columns 'people'").rows
        assert {r[2] for r in rows} == {"people"}
        assert [r[3] for r in rows] == ["id", "name", "score"]

    def test_an_integer_is_reported_as_one(self):
        row = [r for r in call("EXEC sp_columns 'people'").rows if r[3] == "id"][0]
        assert row[4] == procedures.SQL_INTEGER and row[5] == "int"

    def test_a_float_is_reported_as_one(self):
        row = [r for r in call("EXEC sp_columns 'people'").rows if r[3] == "score"][0]
        assert row[4] == procedures.SQL_FLOAT and row[5] == "float"

    def test_text_is_reported_as_unicode(self):
        # Reported as SQL_WVARCHAR because every string this serves is
        # nvarchar on the wire; a driver told otherwise mangles non-ASCII.
        row = [r for r in call("EXEC sp_columns 'people'").rows if r[3] == "name"][0]
        assert row[4] == procedures.SQL_WVARCHAR and row[5] == "nvarchar"

    def test_ordinal_positions_start_at_one_per_table(self):
        rows = call("EXEC sp_columns 'people'").rows
        assert [r[16] for r in rows] == [1, 2, 3]

    def test_everything_is_nullable(self):
        # A JSON record missing a key contributes a NULL, and nothing here
        # declares a column cannot.
        assert all(r[10] == 1 and r[17] == "YES" for r in call("EXEC sp_columns").rows)

    @pytest.mark.parametrize("name", ["sp_columns_100", "sp_columns_90",
                                      "sys.sp_columns_managed"])
    def test_the_variants_clients_actually_ask_for(self, name):
        # ODBC 17 and 18 ask for sp_columns_100; .NET asks for the managed one.
        assert len(call(f"EXEC {name} NULL, NULL, NULL, NULL, 0").rows) == 4


class TestDatabases:
    def test_it_names_the_one_database(self):
        result = call("EXEC sp_databases")
        assert [c.name for c in result.columns] == [
            "DATABASE_NAME", "DATABASE_SIZE", "REMARKS"
        ]
        assert result.rows == [["pysqlbridge", 0, None]]


class TestUnknownProcedures:
    def test_it_still_says_so_rather_than_completing_quietly(self):
        # Silence produced a NullReferenceException in a client: it asked for
        # a result set and got nothing, with no error to explain it.
        with pytest.raises(QueryError, match="could not find stored procedure") as caught:
            call("EXEC sp_who2")
        assert caught.value.number == STORED_PROCEDURE_NOT_FOUND


class TestCalledAsRpc:
    def test_a_procedure_call_carrying_no_sql_is_answered(self):
        # ODBC sends these as RPC with positional arguments rather than as
        # text, which used to be refused in the protocol layer before the
        # catalog ever saw it.
        result = catalog().answer(
            Query(sql="", procedure="[sys].sp_tables", arguments=[None, None, None, None])
        )
        assert [row[2] for row in result.rows] == ["cities", "people"]

    def test_positional_arguments_filter(self):
        result = catalog().answer(
            Query(sql="", procedure="sp_columns", arguments=["people"])
        )
        assert {row[2] for row in result.rows} == {"people"}

    def test_named_arguments_filter_too(self):
        result = catalog().answer(
            Query(sql="", procedure="sp_columns", parameters={"@table_name": "cities"})
        )
        assert {row[2] for row in result.rows} == {"cities"}


class TestOleDbRowsets:
    """MSOLEDBSQL asks for a different family with different layouts.

    Every layout was read off SQL Server 2025 by running the procedure and
    taking its result metadata. The types matter as much as the order: a
    provider handed nvarchar where it expects uniqueidentifier reports a cast
    error naming no column.
    """

    def test_the_tables_rowset_has_the_layout_the_provider_reads(self):
        result = call("EXEC [pysqlbridge].[sys].sp_tables_rowset2")
        assert [c.name for c in result.columns] == [
            "TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME", "TABLE_TYPE",
            "TABLE_GUID", "DESCRIPTION", "TABLE_PROPID", "DATE_CREATED",
            "DATE_MODIFIED",
        ]
        assert [row[2] for row in result.rows] == ["cities", "people"]

    def test_the_columns_rowset_reports_ole_db_types_not_odbc_ones(self):
        # nvarchar is -9 to ODBC and 130 to OLE DB, and -9 is not a DBTYPE at
        # all. This was the whole of "invalid character value for cast
        # specification".
        rows = call("EXEC sys.sp_columns_100_rowset2").rows
        types = {row[3]: row[11] for row in rows if row[2] == "people"}
        assert types["id"] == procedures.DBTYPE_I4
        assert types["name"] == procedures.DBTYPE_WSTR
        assert types["score"] == procedures.DBTYPE_R8

    def test_the_columns_rowset_has_all_forty_two_columns(self):
        assert len(call("EXEC sys.sp_columns_100_rowset2").columns) == 42

    def test_a_fixed_length_column_says_so_in_its_flags(self):
        rows = call("EXEC sys.sp_columns_rowset2").rows
        flags = {row[3]: row[9] for row in rows if row[2] == "people"}
        assert flags["id"] & procedures.DBCOLUMNFLAGS_ISFIXEDLENGTH
        assert not flags["name"] & procedures.DBCOLUMNFLAGS_ISFIXEDLENGTH

    def test_the_ninety_spellings_carry_one_column_more(self):
        # sp_tables_info_90_rowset2 has fifteen columns ending in TABLE_FLAGS;
        # the others have fourteen. Answering the first with fourteen is what
        # MSOLEDBSQL reports as a catastrophic failure.
        plain = call("EXEC sys.sp_tables_info_rowset2")
        ninety = call("EXEC sys.sp_tables_info_90_rowset2_64")
        assert len(plain.columns) == 14
        assert len(ninety.columns) == 15
        assert ninety.columns[-1].name == "TABLE_FLAGS"

    def test_the_empty_rowsets_still_have_their_shape(self):
        # An empty rowset with the wrong columns is not an empty answer, it is
        # a broken one: the provider reads by position.
        assert len(call("EXEC sys.sp_indexes_100_rowset2").columns) == 26
        assert len(call("EXEC sys.sp_primary_keys_rowset2").columns) == 8
        assert len(call("EXEC sys.sp_foreign_keys_rowset;3").columns) == 18
        assert len(call("EXEC sys.sp_procedures_rowset2").columns) == 8

    def test_provider_types_names_what_this_server_can_produce(self):
        result = call("EXEC sys.sp_provider_types_100_rowset")
        assert [row[0] for row in result.rows] == [
            "smallint", "int", "bigint", "float", "bit", "nvarchar"
        ]

    def test_privileges_are_read_only(self):
        rows = call("EXEC sys.sp_table_privileges_rowset2").rows
        assert {row[5] for row in rows} == {"SELECT"}
        assert all(row[6] is False for row in rows)


class TestNumberedProcedures:
    """A trailing ;N is a group number, and the number picks the variant."""

    def test_a_group_number_selects_the_variant(self):
        assert procedures.normalise("sp_foreign_keys_rowset;3") == \
            "sp_foreign_keys_rowset3"

    def test_group_one_is_the_bare_name(self):
        assert procedures.normalise("sp_tables;1") == "sp_tables"

    def test_it_survives_qualification(self):
        assert procedures.normalise("[db].[sys].sp_foreign_keys_rowset;3") == \
            "sp_foreign_keys_rowset3"


class TestArgumentBinding:
    """The variants disagree about what argument 0 means.

    sp_columns_rowset starts with @table_name; sp_columns_100_rowset2 starts
    with @table_schema and has no @table_name at all. Reading argument 0 as a
    table name filters the whole catalog by the string "dbo".
    """

    def test_the_base_variant_takes_a_table_name_first(self):
        assert procedures.bind("sp_columns_rowset", ["people", "dbo"], {}) == {
            "table_name": "people", "table_schema": "dbo"
        }

    def test_the_schema_variant_takes_a_schema_first(self):
        assert procedures.bind("sp_columns_100_rowset2", ["dbo"], {}) == {
            "table_schema": "dbo"
        }

    def test_named_arguments_win_over_position(self):
        bound = procedures.bind("sp_tables", ["people"], {"@table_type": "'TABLE'"})
        assert bound == {"table_name": "people", "table_type": "'TABLE'"}

    def test_the_schema_variant_does_not_filter_by_table(self):
        # Given "dbo" positionally, the rowset must still list every table.
        result = catalog().answer(
            Query(sql="", procedure="sys.sp_columns_100_rowset2", arguments=["dbo"])
        )
        assert {row[2] for row in result.rows} == {"cities", "people"}
