import pytest

from pysqlbridge.sql import Select, SqlError, parse_select


class TestShapesClientsSend:
    def test_select_star(self):
        assert parse_select("SELECT * FROM people") == Select("people", None, None)

    def test_case_and_trailing_semicolon(self):
        assert parse_select("select * from people;").table == "people"

    def test_named_columns(self):
        assert parse_select("SELECT id, name FROM t").columns == ["id", "name"]

    def test_top(self):
        assert parse_select("SELECT TOP 100 * FROM t").top == 100

    def test_top_in_parentheses(self):
        # Clients generate both forms.
        assert parse_select("SELECT TOP (10) * FROM t").top == 10

    def test_newlines_and_padding(self):
        assert parse_select("\n  SELECT\n  *\n  FROM\n  t\n").table == "t"


class TestWhere:
    def test_a_condition_is_parsed(self):
        assert parse_select("SELECT * FROM t WHERE id = 1").where is not None

    def test_no_condition_leaves_it_unset(self):
        assert parse_select("SELECT * FROM t").where is None

    def test_a_broken_condition_says_so(self):
        with pytest.raises(SqlError, match="cannot read the WHERE condition"):
            parse_select("SELECT * FROM t WHERE id =")


class TestTopParameter:
    def test_a_literal_top(self):
        select = parse_select("SELECT TOP 5 * FROM t")
        assert select.row_limit() == 5

    def test_a_parameterised_top_resolves_at_execution(self):
        select = parse_select("SELECT TOP (@n) * FROM t")
        assert select.top is None and select.top_parameter == "@n"
        assert select.row_limit({"@n": 3}) == 3

    def test_a_missing_top_parameter_is_an_error(self):
        select = parse_select("SELECT TOP (@n) * FROM t")
        with pytest.raises(SqlError, match="was not supplied"):
            select.row_limit({})


class TestSchema:
    def test_the_schema_is_kept_not_discarded(self):
        # INFORMATION_SCHEMA.TABLES and dbo.TABLES are different tables.
        select = parse_select("SELECT * FROM INFORMATION_SCHEMA.TABLES")
        assert select.schema == "INFORMATION_SCHEMA"
        assert select.table == "TABLES"
        assert select.qualified_name == "INFORMATION_SCHEMA.TABLES"

    def test_a_database_prefix_is_dropped_but_the_schema_is_not(self):
        select = parse_select("SELECT * FROM db.INFORMATION_SCHEMA.COLUMNS")
        assert select.schema == "INFORMATION_SCHEMA"

    def test_an_unqualified_name_has_no_schema(self):
        assert parse_select("SELECT * FROM people").schema is None


class TestIdentifiers:
    def test_bracketed(self):
        # What Excel, Power BI and SSMS actually generate.
        assert parse_select("SELECT * FROM [dbo].[people]").table == "people"

    def test_double_quoted(self):
        assert parse_select('SELECT "id" FROM "people"').columns == ["id"]

    def test_brackets_with_spaces_inside(self):
        assert parse_select("SELECT * FROM [my table]").table == "my table"

    def test_a_doubled_bracket_is_an_escaped_bracket(self):
        assert parse_select("SELECT [a]]b] FROM t").columns == ["a]b"]

    def test_a_doubled_quote_is_an_escaped_quote(self):
        assert parse_select('SELECT "a""b" FROM t').columns == ['a"b']

    def test_three_part_names_keep_only_the_table(self):
        assert parse_select("SELECT * FROM mydb.dbo.people").table == "people"

    def test_three_part_bracketed_names(self):
        assert parse_select("SELECT * FROM [my db].[dbo].[my table]").table == "my table"

    def test_four_parts_is_too_many(self):
        with pytest.raises(SqlError, match="at most three parts"):
            parse_select("SELECT * FROM a.b.c.d")


class TestRefusals:
    """Refusing loudly beats half-executing.

    A WHERE that parsed and was then ignored would return every row and look
    like a filter that worked.
    """

    @pytest.mark.parametrize("clause", ["ORDER BY id", "GROUP BY id"])
    def test_unsupported_clauses_name_themselves(self, clause):
        with pytest.raises(SqlError, match="is not supported"):
            parse_select(f"SELECT * FROM t {clause}")

    def test_a_clause_after_where_is_still_refused(self):
        # The condition runs to the end of the statement, so this surfaces as
        # a condition error rather than as unexplained trailing text.
        with pytest.raises(SqlError, match="WHERE condition"):
            parse_select("SELECT * FROM t WHERE a = 1 ORDER BY a")

    @pytest.mark.parametrize("statement", ["UPDATE t SET a=1", "DELETE FROM t",
                                           "INSERT INTO t VALUES (1)", "DROP TABLE t"])
    def test_non_select_statements_name_themselves(self, statement):
        with pytest.raises(SqlError, match="only SELECT is supported"):
            parse_select(statement)

    def test_a_select_with_no_from(self):
        with pytest.raises(SqlError, match="needs a FROM"):
            parse_select("SELECT id")

    def test_empty(self):
        with pytest.raises(SqlError, match="empty statement"):
            parse_select("   ")

    def test_a_join_is_refused_rather_than_silently_reading_one_table(self):
        with pytest.raises(SqlError, match="is not supported"):
            parse_select("SELECT * FROM a JOIN b ON a.id = b.id")
