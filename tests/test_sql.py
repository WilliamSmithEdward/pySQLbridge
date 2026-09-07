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

    @pytest.mark.parametrize("clause", ["PIVOT (COUNT(a) FOR a IN ([1]))",
                                        "FOR XML AUTO"])
    def test_unsupported_clauses_name_themselves(self, clause):
        with pytest.raises(SqlError, match="is not supported"):
            parse_select(f"SELECT * FROM t {clause}")

    def test_an_unsupported_clause_after_where_is_still_refused(self):
        # The condition ends at the clauses this understands and runs to the
        # end of the statement otherwise, so this surfaces as a condition
        # error rather than as a named clause.
        with pytest.raises(SqlError, match="WHERE condition"):
            parse_select("SELECT * FROM t WHERE a = 1 COMPUTE SUM(a)")

    def test_where_followed_by_order_by_is_fine(self):
        select = parse_select("SELECT * FROM t WHERE a = 1 ORDER BY a")
        assert select.where is not None and select.order_by

    @pytest.mark.parametrize("statement", ["UPDATE t SET a=1", "DELETE FROM t",
                                           "INSERT INTO t VALUES (1)", "DROP TABLE t"])
    def test_non_select_statements_name_themselves(self, statement):
        with pytest.raises(SqlError, match="only SELECT is supported"):
            parse_select(statement)

    def test_a_select_with_no_from_cannot_read_columns(self):
        # SELECT 1 is answered, because clients probe with it. A column has
        # to come from somewhere.
        with pytest.raises(SqlError, match="can only compute values"):
            parse_select("SELECT id")

    def test_empty(self):
        with pytest.raises(SqlError, match="empty statement"):
            parse_select("   ")

    def test_a_join_this_cannot_do_is_refused_rather_than_approximated(self):
        # A client given the wrong rows has no way to notice.
        with pytest.raises(SqlError, match="RIGHT JOIN is not supported"):
            parse_select("SELECT * FROM a RIGHT JOIN b ON a.id = b.id")

    def test_a_join_without_a_condition_is_refused(self):
        with pytest.raises(SqlError, match="needs an ON condition"):
            parse_select("SELECT * FROM a JOIN b")


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


class TestSelectList:
    def test_a_plain_column(self):
        items = parse_select("SELECT name FROM t").items
        assert len(items) == 1
        assert items[0].expression == "name" and not items[0].is_aggregate

    def test_an_as_alias(self):
        assert parse_select("SELECT name AS who FROM t").items[0].alias == "who"

    def test_a_bare_alias(self):
        assert parse_select("SELECT name who FROM t").items[0].alias == "who"

    def test_from_is_not_read_as_an_alias(self):
        # Without the keyword check the statement loses its table.
        select = parse_select("SELECT name FROM t")
        assert select.items[0].alias is None and select.table == "t"

    def test_order_is_not_read_as_an_alias(self):
        select = parse_select("SELECT name FROM t ORDER BY name")
        assert select.items[0].alias is None and select.order_by

    def test_star_has_no_items(self):
        assert parse_select("SELECT * FROM t").items is None
        assert parse_select("SELECT * FROM t").is_star


class TestAggregateParsing:
    def test_count_star_has_a_function_and_no_column(self):
        item = parse_select("SELECT COUNT(*) FROM t").items[0]
        assert item.function == "COUNT" and item.expression is None
        assert item.is_aggregate

    def test_count_of_a_column(self):
        item = parse_select("SELECT COUNT(name) FROM t").items[0]
        assert item.function == "COUNT" and item.expression == "name"

    def test_several_aggregates(self):
        items = parse_select("SELECT MIN(a), MAX(a) FROM t").items
        assert [i.function for i in items] == ["MIN", "MAX"]

    def test_an_aggregate_can_be_aliased(self):
        item = parse_select("SELECT COUNT(*) AS n FROM t").items[0]
        assert item.alias == "n" and item.output_name == "n"

    def test_an_unaliased_aggregate_has_no_output_name(self):
        assert parse_select("SELECT COUNT(*) FROM t").items[0].output_name == ""

    def test_case_does_not_matter(self):
        assert parse_select("SELECT count(*) FROM t").items[0].function == "COUNT"

    def test_has_aggregates_reports_the_select_list(self):
        assert parse_select("SELECT COUNT(*) FROM t").has_aggregates
        assert not parse_select("SELECT name FROM t").has_aggregates
        assert not parse_select("SELECT * FROM t").has_aggregates

    def test_an_aggregate_beside_a_bare_column_is_refused(self):
        with pytest.raises(SqlError, match="neither aggregated nor named"):
            parse_select("SELECT COUNT(*), name FROM t")

    def test_a_scalar_function_is_read_as_an_expression(self):
        item = parse_select("SELECT LOWER(name) FROM t").items[0]
        assert item.is_computed and not item.is_aggregate

    def test_a_function_this_does_not_have_says_so(self):
        with pytest.raises(SqlError, match="not a function"):
            parse_select("SELECT NOSUCHTHING(name) FROM t")

    def test_star_is_only_valid_for_count(self):
        with pytest.raises(SqlError, match=r"SUM\(\*\) is not a thing"):
            parse_select("SELECT SUM(*) FROM t")

    def test_an_unclosed_call(self):
        with pytest.raises(SqlError, match="was opened and not closed"):
            parse_select("SELECT COUNT(* FROM t")

    def test_aggregates_combine_with_a_where(self):
        select = parse_select("SELECT COUNT(*) FROM t WHERE a = 1")
        assert select.has_aggregates and select.where is not None


class TestIdentifiersHaveALimit:
    """An alias is one name, and a name has a length a client can read.

    Found by sending a select list whose commas were missing: it parsed as
    one column aliased by the rest of the statement, 376 characters of it,
    and the wire writes a column name length in one byte. The connection did
    not fail, it dropped, which is the worst way for anything to go wrong.
    """

    def test_a_select_list_without_commas_is_a_syntax_error(self):
        with pytest.raises(SqlError, match="incorrect syntax near '2'"):
            parse_select("SELECT 1 AS c1 2 AS c2 FROM t")

    def test_an_alias_at_the_limit_is_fine(self):
        name = "a" * 128
        assert parse_select(f"SELECT 1 AS {name} FROM t").items[0].alias == name

    def test_one_past_it_says_so_the_way_sql_server_does(self):
        with pytest.raises(SqlError, match="Maximum length is 128"):
            parse_select("SELECT 1 AS " + "a" * 129 + " FROM t")

    def test_a_quoted_alias_is_measured_the_same_way(self):
        with pytest.raises(SqlError, match="Maximum length is 128"):
            parse_select("SELECT 1 AS [" + "a" * 129 + "] FROM t")

    def test_an_alias_may_still_be_quoted_and_hold_a_space(self):
        assert parse_select("SELECT 1 AS [a b] FROM t").items[0].alias == "a b"
