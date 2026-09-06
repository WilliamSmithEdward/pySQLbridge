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

    @pytest.mark.parametrize("clause", ["GROUP BY id", "HAVING x > 1"])
    def test_unsupported_clauses_name_themselves(self, clause):
        with pytest.raises(SqlError, match="is not supported"):
            parse_select(f"SELECT * FROM t {clause}")

    def test_an_unsupported_clause_after_where_is_still_refused(self):
        # The condition ends at a top-level ORDER BY but runs to the end of
        # the statement otherwise, so this surfaces as a condition error.
        with pytest.raises(SqlError, match="WHERE condition"):
            parse_select("SELECT * FROM t WHERE a = 1 GROUP BY a")

    def test_where_followed_by_order_by_is_fine(self):
        select = parse_select("SELECT * FROM t WHERE a = 1 ORDER BY a")
        assert select.where is not None and select.order_by

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


class TestOrderBy:
    def test_a_single_key_defaults_to_ascending(self):
        keys = parse_select("SELECT * FROM t ORDER BY name").order_by
        assert len(keys) == 1
        assert keys[0].column == "name" and keys[0].descending is False

    def test_desc(self):
        assert parse_select("SELECT * FROM t ORDER BY name DESC").order_by[0].descending

    def test_asc_is_accepted_explicitly(self):
        assert not parse_select("SELECT * FROM t ORDER BY name ASC").order_by[0].descending

    def test_several_keys_keep_their_own_directions(self):
        keys = parse_select("SELECT * FROM t ORDER BY a, b DESC, [c] ASC").order_by
        assert [(k.column, k.descending) for k in keys] == [
            ("a", False), ("b", True), ("c", False),
        ]

    def test_it_follows_a_where(self):
        select = parse_select("SELECT * FROM t WHERE id = 1 ORDER BY name DESC")
        assert select.where is not None
        assert select.order_by[0].column == "name"

    def test_a_literal_containing_the_words_is_not_split_on(self):
        # WHERE note = 'order by tuesday' is a condition, not two clauses.
        select = parse_select("SELECT * FROM t WHERE note = 'order by tuesday'")
        assert select.order_by == ()
        assert select.where is not None

    def test_a_bracketed_name_containing_the_words_is_not_split_on(self):
        select = parse_select("SELECT * FROM [order by] ORDER BY a")
        assert select.table == "order by"
        assert select.order_by[0].column == "a"

    def test_no_order_by_leaves_it_empty(self):
        assert parse_select("SELECT * FROM t").order_by == ()

    def test_a_clause_after_order_by_is_still_refused(self):
        with pytest.raises(SqlError, match="GROUP is not supported"):
            parse_select("SELECT * FROM t ORDER BY a GROUP BY a")
