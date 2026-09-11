import pytest

from pysqlbridge.sql import (
    Select, SqlError, declarations, malformed, parse_select,
    select_assignments, unbound, without_comments,
)


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

    @pytest.mark.parametrize("written, kind", [
        ("JOIN b ON a.id = b.id", "INNER"),
        ("INNER JOIN b ON a.id = b.id", "INNER"),
        ("LEFT JOIN b ON a.id = b.id", "LEFT"),
        ("LEFT OUTER JOIN b ON a.id = b.id", "LEFT"),
        ("RIGHT JOIN b ON a.id = b.id", "RIGHT"),
        ("RIGHT OUTER JOIN b ON a.id = b.id", "RIGHT"),
        ("FULL JOIN b ON a.id = b.id", "FULL"),
        ("FULL OUTER JOIN b ON a.id = b.id", "FULL"),
        ("CROSS JOIN b", "CROSS"),
    ])
    def test_every_join_a_query_may_write(self, written, kind):
        assert parse_select(f"SELECT * FROM a {written}").joins[0].kind == kind

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
        with pytest.raises(SqlError, match="is invalid in the select list"):
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


def near(token):
    return (102, f"Incorrect syntax near '{token}'.")


def keyword(token):
    return (156, f"Incorrect syntax near the keyword '{token}'.")


def left_open(rest):
    return (105, f"Unclosed quotation mark after the character string '{rest}'.")


COMMENT_LEFT_OPEN = (113, "Missing end comment mark '*/'.")


class TestABatchThatCannotCompile:
    """What a real server says of text that cannot be T-SQL at all.

    Every case here was sent to SQL Server 2025 through SqlClient with every
    error of the answer recorded, and the list is what it said, in order.
    """

    def said(self, sql):
        return [(one.number, str(one)) for one in malformed(sql)]

    @pytest.mark.parametrize("sql, expected", [
        ("SELECT * FROM", [near("FROM")]),
        ("select * from", [near("from")]),
        ("SELECT", [near("SELECT")]),
        ("SELECT * FROM sys.objects WHERE object_id <=", [near("=")]),
        ("SELECT name FROM sys.objects WHERE object_id <>", [near(">")]),
        ("SELECT 1,", [near(",")]),
        ("SELECT o.", [near(".")]),
        ("SELECT ~", [near("~")]),
        ("SELECT name FROM sys.objects WHERE name IS", [near("IS")]),
        ("SELECT name FROM sys.objects LEFT OUTER", [near("OUTER")]),
        ("INSERT INTO #nowhere VALUES", [near("VALUES")]),
        ("UPDATE", [near("UPDATE")]),
        ("update", [near("update")]),
        ("DELETE", [near("DELETE")]),
        ("SELECT (1", [near("1")]),
        ("SELECT CASE WHEN 1 = 1 THEN 1", [near("1")]),
        ("IF 1 = 1 BEGIN SELECT 1 AS v", [near("v")]),
        ("DECLARE @p", [near("@p")]),
        ("SET @p", [near("@p")]),
        ("DECLARE @a int, @b", [near("@b")]),
        ("DECLARE @a int = 1, @b", [near("@b")]),
        ("DECLARE @a AS int, @b", [near("@b")]),
        ("SAVE TRAN", [near("TRAN")]),
        ("CREATE TABLE #t", [near("#t")]),
    ])
    def test_text_that_runs_out_is_102_near_its_last_token(self, sql, expected):
        assert self.said(sql) == expected

    @pytest.mark.parametrize("sql, expected", [
        # A SET that begins its statement is the one that answers otherwise,
        # in capitals however it was written.
        ("SET", [keyword("SET")]),
        ("set", [keyword("SET")]),
        ("DECLARE @p int SET", [keyword("SET")]),
        ("SELECT 1 AS v SET", [keyword("SET")]),
        ("BEGIN TRAN SET", [keyword("SET")]),
        ("SET; SELECT 1 AS v", [keyword("SET")]),
        ("SET\r\nSELECT 1 AS v", [keyword("SET")]),
        ("SET\r\nDELETE FROM #t", [keyword("SET")]),
        ("IF 1 = 1 BEGIN SET END", [keyword("SET")]),
        # And a SET that belongs to an UPDATE or an ALTER does not.
        ("UPDATE #t SET", [near("SET")]),
        ("update #t set", [near("set")]),
        ("UPDATE #t WITH (ROWLOCK) SET", [near("SET")]),
        ("ALTER DATABASE tempdb SET", [near("SET")]),
        ("MERGE #t AS t USING #s AS s ON t.a = s.a WHEN MATCHED THEN UPDATE SET",
         [near("SET")]),
        ("UPDATE #t SET; SELECT 1 AS v", [near(";")]),
    ])
    def test_a_set_stopped_short(self, sql, expected):
        assert self.said(sql) == expected

    @pytest.mark.parametrize("sql, expected", [
        ("SELECT name, FROM sys.objects", [keyword("FROM")]),
        ("select name, From sys.objects", [keyword("From")]),
        ("SELECT FROM sys.objects", [keyword("FROM")]),
        ("SELECT * FROM FROM", [keyword("FROM")]),
        ("SELECT * FROM sys.objects WHERE WHERE", [keyword("WHERE")]),
        ("SELECT COUNT( FROM sys.objects", [keyword("FROM")]),
        ("SELECT o.name FROM sys.objects o JOIN sys.columns c ON WHERE 1 = 1",
         [keyword("WHERE")]),
        ("SELECT name FROM sys.objects GROUP BY HAVING COUNT(*) > 1",
         [keyword("HAVING")]),
        ("SELECT 1 AS v UNION UNION SELECT 2", [keyword("UNION")]),
        ("SELECT o. FROM sys.objects o", [keyword("FROM")]),
        ("SET FROM", [keyword("FROM")]),
    ])
    def test_a_clause_word_where_a_value_goes_is_156(self, sql, expected):
        assert self.said(sql) == expected

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM sys.objects WHERE; SELECT 2 AS v",
        "SELECT 1,; SELECT 1 AS v",
        "IF 1 = 1 SELECT 1 AS v ELSE; SELECT 2 AS v",
        "UPDATE; SELECT 1 AS v",
        "SELECT (1; SELECT 2 AS v",
        "SELECT COUNT(*; SELECT 1 AS v",
        "SELECT CASE WHEN 1 = 1 THEN 1; SELECT 2 AS v",
    ])
    def test_a_semicolon_where_more_was_wanted_is_102_near_it(self, sql):
        assert self.said(sql) == [near(";")]

    @pytest.mark.parametrize("sql, expected", [
        ("SELECT 'unclosed", [left_open("unclosed"), near("unclosed")]),
        # A doubled quote is quoted back as the one quote it stands for.
        ("SELECT 'it''s", [left_open("it's"), near("it's")]),
        ("SELECT 'abc''", [left_open("abc'"), near("abc'")]),
        ("SELECT N'abc", [left_open("abc"), near("abc")]),
        ("SELECT 'a\r\nb  ", [left_open("a\r\nb  "), near("a\r\nb  ")]),
        ("SELECT 'abc FROM sys.objects WHERE",
         [left_open("abc FROM sys.objects WHERE"),
          near("abc FROM sys.objects WHERE")]),
        ("SELECT 'abc /* x", [left_open("abc /* x"), near("abc /* x")]),
        # A quoted name left open is the same error.
        ("SELECT [abc", [left_open("abc"), near("abc")]),
        ("SELECT [a]]b", [left_open("a]b"), near("a]b")]),
        ('SELECT "a""b', [left_open('a"b'), near('a"b')]),
        # Nothing before it runs, and anything before it that failed first
        # is reported first, with the 105 after it and no 102.
        ("SELECT 1 AS v; SELECT 'x", [left_open("x"), near("x")]),
        ("SELECT FROM sys.objects WHERE name = 'abc",
         [keyword("FROM"), left_open("abc")]),
        ("SELECT * FROM; SELECT 'abc", [near(";"), left_open("abc")]),
    ])
    def test_a_quote_left_open(self, sql, expected):
        assert self.said(sql) == expected

    def test_a_long_rest_is_cut_where_a_real_server_cuts_it(self):
        # Blocks of ten that say where they end, so the cut shows.
        marked = "".join(f"{n * 10:09d}|" for n in range(1, 231))
        opened, close = malformed("SELECT '" + marked[:200])
        assert str(opened) == (
            f"Unclosed quotation mark after the character string "
            f"'{marked[:200]}'.")
        assert str(close) == f"Incorrect syntax near '{marked[:129]}'."
        opened, close = malformed("SELECT '" + marked)
        assert len(str(opened)) == 2047
        assert str(opened).endswith("000001990|00...")
        assert str(close) == f"Incorrect syntax near '{marked[:129]}'."

    @pytest.mark.parametrize("sql, expected", [
        ("SELECT 1 /* open", [COMMENT_LEFT_OPEN]),
        # Comments nest, so this one is still open at the end.
        ("SELECT 1 /* a /* b */ AS v", [COMMENT_LEFT_OPEN]),
        # A quote inside a comment opens nothing.
        ("SELECT 'x' /* a ' b", [COMMENT_LEFT_OPEN]),
        # What the text before it lacked follows it; what failed before it
        # comes first.
        ("SELECT * FROM /* open", [COMMENT_LEFT_OPEN, near("FROM")]),
        ("SELECT FROM sys.objects /* open", [keyword("FROM"), COMMENT_LEFT_OPEN]),
        ("SELECT * FROM -- x", [near("FROM")]),
        ("SELECT * FROM /* c */", [near("FROM")]),
    ])
    def test_a_comment_left_open_is_113(self, sql, expected):
        assert self.said(sql) == expected

    @pytest.mark.parametrize("sql", [
        # Each answered by a real server, or parsed by one under SET
        # PARSEONLY ON where running it would have needed objects, and each
        # next to a rule above that could have caught it.
        "SET NOCOUNT ON; SELECT 1 AS v",
        "BEGIN TRAN; SELECT @@TRANCOUNT AS v; ROLLBACK;",
        "BEGIN TRANSACTION; ROLLBACK TRANSACTION;",
        "BEGIN; SELECT 1 AS v; END;",
        "SELECT 1 AS [FROM]",
        "SELECT name FROM sys.objects ORDER BY object_id OFFSET 2 ROWS",
        "SELECT name FROM sys.objects ORDER BY object_id "
        "OFFSET 2 ROWS FETCH NEXT 1 ROWS ONLY",
        "SELECT {fn LCASE('A')} AS v",
        "SELECT {d '2024-01-02'} AS v",
        "IF 1 = 1 BEGIN SELECT 1 AS v END ELSE BEGIN SELECT 2 AS v END",
        "IF 1 = 1 BEGIN SELECT 1 AS v END;",
        "SELECT CASE WHEN 1 = 1 THEN 1 END AS v",
        "BEGIN TRY SELECT 1 AS v END TRY BEGIN CATCH SELECT 2 AS v END CATCH",
        ";WITH c AS (SELECT 1 AS a) SELECT a FROM c",
        "SELECT a FROM (VALUES (1)) AS t(a)",
        "DECLARE @p int; SET @p = 1; SELECT @p AS v",
        "DECLARE @a int = 1, @b int = 2; SELECT @a, @b",
        "SELECT N'it''s' AS v",
        "SELECT 1 AS v FOR XML PATH",
        "SELECT TOP 1 name FROM sys.objects WITH (NOLOCK)",
        "SELECT TOP 1 name FROM sys.objects o WHERE NOT EXISTS (SELECT 1)",
        "SELECT 1 AS v UNION ALL SELECT 2",
        "CREATE TABLE #dv (a int DEFAULT 1); INSERT INTO #dv DEFAULT VALUES;",
        "SELECT 1 AS v;",
        "SELECT TOP 1 o.* FROM sys.objects o",
        "SELECT type, COUNT(*) AS n FROM sys.objects GROUP BY type WITH ROLLUP",
        "BEGIN TRY THROW 50001, 'x', 1; END TRY BEGIN CATCH SELECT 1 AS v; "
        "END CATCH",
        "SELECT a FROM t GROUP BY a OPTION (HASH GROUP)",
        "SELECT a FROM t GROUP BY a OPTION (ORDER GROUP)",
        "SELECT 1 AS v UNION SELECT 2 OPTION (MERGE UNION)",
        "SELECT 1 AS v UNION SELECT 2 OPTION (CONCAT UNION)",
        "DELETE FROM #d",
        "DECLARE c CURSOR FOR SELECT 1 AS v; OPEN c; FETCH FROM c;",
        "DECLARE c CURSOR FOR SELECT name FROM sys.objects FOR UPDATE",
        "SELECT TOP 1 name FROM sys.objects WHERE name NOT IN ('a') "
        "AND name NOT LIKE 'b' AND object_id NOT BETWEEN 1 AND 2",
        "SELECT TOP 1 name FROM sys.objects WHERE name IS NOT NULL",
        "UPDATE #u SET a = 1 FROM #u",
        "SELECT 1 /* a /* b */ c */ AS v",
        "SELECT 1 AS v -- trailing",
        "SELECT 1 AS v --",
        "SELECT 1 AS v -- it's\r\n",
        "SELECT '/* not a comment' AS v",
        "SELECT '-- not a comment' AS v",
        "SELECT 1 AS [a]]--b]",
        "SELECT 1 AS v WHERE 1 IS DISTINCT FROM 2",
        "SELECT 1 AS v WHERE 1 IS NOT DISTINCT FROM 1",
        "SELECT a FROM dbo.t FOR SYSTEM_TIME ALL WHERE a = 1",
        "SELECT a FROM dbo.t FOR SYSTEM_TIME ALL",
        "ALTER TABLE dbo.t NOCHECK CONSTRAINT ALL",
        "ALTER DATABASE d SET RECOVERY FULL",
        "CREATE VIEW dbo.v AS SELECT 1 AS a WITH CHECK OPTION",
        "MERGE dbo.t AS t USING dbo.s AS s ON t.a = s.a WHEN MATCHED THEN DELETE;",
        "CREATE SECURITY POLICY dbo.p ADD BLOCK PREDICATE dbo.f(a) ON dbo.t "
        "AFTER INSERT",
        "CREATE SECURITY POLICY dbo.p ADD BLOCK PREDICATE dbo.f(a) ON dbo.t "
        "BEFORE DELETE",
        "CREATE TRIGGER dbo.tr ON dbo.t FOR UPDATE AS SELECT 1 AS v",
        "SELECT STRING_AGG(a, ',') WITHIN GROUP (ORDER BY a) AS v FROM dbo.t",
        "SELECT TRIM('x' FROM a) AS v FROM dbo.t",
        "SELECT TRIM(LEADING 'x' FROM a) AS v FROM dbo.t",
        "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
        "SET STATISTICS IO ON",
        "SELECT ROW_NUMBER() OVER w AS v FROM dbo.t WINDOW w AS (ORDER BY a)",
        "SELECT 1 AS v FROM dbo.n1, dbo.e, dbo.n2 WHERE MATCH(n1-(e)->n2)",
        "",
        "   ",
    ])
    def test_good_t_sql_is_left_alone(self, sql):
        assert malformed(sql) == []


def scalar(name):
    return (137, 2, f'Must declare the scalar variable "{name}".')


def table_variable(name):
    return (1087, 2, f'Must declare the table variable "{name}".')


class TestAVariableNothingDeclared:
    """Msg 137 and 1087, which a real server settles while compiling.

    Every case sent to SQL Server 2025 with each error of the answer
    recorded; the list is what it said, in order.
    """

    def said(self, sql, known=()):
        return [(one.number, one.state, str(one))
                for one in unbound(without_comments(sql), known)]

    @pytest.mark.parametrize("sql, expected", [
        ("SELECT 'before' AS v; SELECT @zz AS v", [scalar("@zz")]),
        ("SELECT @zz = 1", [scalar("@zz")]),
        ("SET @zz = 1", [scalar("@zz")]),
        ("SELECT name FROM sys.objects WHERE object_id = @zz", [scalar("@zz")]),
        ("PRINT @zz", [scalar("@zz")]),
        ("RAISERROR('x %d', 16, 1, @zz)", [scalar("@zz")]),
        ("SELECT TOP (@zz) name FROM sys.objects", [scalar("@zz")]),
        ("SELECT (SELECT @zz) AS v", [scalar("@zz")]),
        ("WHILE @zz < 1 SELECT 1 AS v", [scalar("@zz")]),
        ("SELECT @x AS v; DECLARE @x int", [scalar("@x")]),
        # One for each statement, the first name in each.
        ("SELECT @zz AS a, @yy AS b", [scalar("@zz")]),
        ("SELECT @yy AS v; SELECT @zz AS v", [scalar("@yy"), scalar("@zz")]),
        ("IF @zz = 1 SELECT @yy AS v", [scalar("@zz"), scalar("@yy")]),
        ("IF 1 = 1 SELECT @yy AS v ELSE SELECT @xx AS v",
         [scalar("@yy"), scalar("@xx")]),
        ("BEGIN SELECT @zz AS v; SELECT @yy AS v END",
         [scalar("@zz"), scalar("@yy")]),
        ("BEGIN TRY SELECT @zz AS v END TRY BEGIN CATCH SELECT @yy AS v END CATCH",
         [scalar("@zz"), scalar("@yy")]),
        # A DECLARE that fails declares nothing, and its names are not yet
        # declared inside it.
        ("DECLARE @a int = 1, @b int = @a", [scalar("@a")]),
        ("DECLARE @a int = 1, @b int = @a + 1; SELECT @b AS b",
         [scalar("@a"), scalar("@b")]),
        ("DECLARE @a int = @b, @b int = 1", [scalar("@b")]),
        ("DECLARE @a int = @a", [scalar("@a")]),
        ("DECLARE @Abc int = 1, @b int = @abc", [scalar("@abc")]),
        ("IF 1 = 1 BEGIN DECLARE @a int = @zz; SELECT @a AS v END",
         [scalar("@zz"), scalar("@a")]),
        # An EXEC reads its return value's variable and every value it
        # passes, and not the names of the parameters it passes them to.
        ("EXEC @rc = sp_who", [scalar("@rc")]),
        ("EXEC sp_executesql N'SELECT @x AS v', N'@x int', @x", [scalar("@x")]),
        ("EXEC sp_executesql N'SELECT @x AS v', N'@x int', @x = @zz",
         [scalar("@zz")]),
        ("DECLARE c CURSOR LOCAL FOR SELECT 1 AS v; OPEN c; "
         "FETCH NEXT FROM c INTO @zz; CLOSE c; DEALLOCATE c", [scalar("@zz")]),
        ("FETCH NEXT FROM @c", [scalar("@c")]),
        # Where a table goes, the table variable's own number.
        ("SELECT COUNT(*) AS n FROM @t", [table_variable("@t")]),
        ("INSERT INTO @t VALUES (1)", [table_variable("@t")]),
        ("UPDATE @t SET a = 1", [table_variable("@t")]),
        ("DELETE FROM @t", [table_variable("@t")]),
        ("SELECT 1 AS v FROM sys.objects o JOIN @t t ON 1 = 1",
         [table_variable("@t")]),
    ])
    def test_a_variable_read_before_any_declare_made_it(self, sql, expected):
        assert self.said(sql) == expected

    @pytest.mark.parametrize("sql", [
        "DECLARE @a int; SELECT @a AS v",
        "DECLARE @Abc int = 3; SELECT @ABC AS v",
        "DECLARE @a int = 1; DECLARE @b int = @a + 1; SELECT @b AS b",
        # Declared where it is written, whether or not that branch runs.
        "IF 1 = 0 BEGIN DECLARE @x int END; SELECT @x AS v",
        "EXEC sp_executesql N'SELECT @x AS v', N'@x int', @x = 5",
        "DECLARE @rc int; EXEC @rc = sp_executesql N'SELECT 1 AS v'; "
        "SELECT @rc AS rc",
        "DECLARE @t TABLE (a int); SELECT COUNT(*) AS n FROM @t",
        "SELECT @@ROWCOUNT AS n",
        "SELECT '@zz' AS v",
        "SELECT 1 AS v -- @zz",
        # A module declares its parameters in its own header.
        "CREATE PROCEDURE p @x int AS SELECT @x AS v",
    ])
    def test_a_variable_that_was_declared_is_left_alone(self, sql):
        assert self.said(sql) == []

    def test_a_value_the_client_sent_declares_its_name(self):
        # How a parameterised statement arrives: its parameters beside it.
        assert self.said("SELECT @x AS v", known=["@x"]) == []
        assert self.said("SELECT @X AS v", known=["@x"]) == []

    def test_assigning_beside_reading_is_141(self):
        assert self.said("DECLARE @a int; SELECT @a = 1, 2 AS b") == [(
            141, 1, "A SELECT statement that assigns a value to a variable "
                    "must not be combined with data-retrieval operations.")]

    def test_a_variable_nothing_declared_is_said_before_141(self):
        # Measured, whichever of the two comes first in the statement.
        assert self.said("SELECT @zz = 1, 2 AS b") == [scalar("@zz")]
        assert self.said("DECLARE @a int; SELECT 2 AS b, @a = 1, @zz = 3") == [
            scalar("@zz")]


class TestWhatADeclareDeclares:
    @pytest.mark.parametrize("sql, expected", [
        ("DECLARE @a int = 1, @b int = 2", ["@a int = 1", "@b int = 2"]),
        ("DECLARE @s nvarchar(10) = 'a,b', @n int",
         ["@s nvarchar(10) = 'a,b'", "@n int"]),
        ("DECLARE @a int = COALESCE(NULL, 7), @b int",
         ["@a int = COALESCE(NULL, 7)", "@b int"]),
        ("DECLARE @a int = CASE WHEN 1 = 1 THEN 1 ELSE 2 END, @b int",
         ["@a int = CASE WHEN 1 = 1 THEN 1 ELSE 2 END", "@b int"]),
        ("DECLARE @t TABLE (a int, b int)", ["@t TABLE (a int, b int)"]),
        # A variable's name is one name, whatever word it spells.
        ("DECLARE @case int = 1, @end int = 2", ["@case int = 1", "@end int = 2"]),
    ])
    def test_it_divides_at_the_commas_between_variables(self, sql, expected):
        assert declarations(sql) == expected

    @pytest.mark.parametrize("sql", [
        "DECLARE c CURSOR FOR SELECT a, b FROM t",
        "SELECT @a = 1",
        "DECLARE @@x int",
    ])
    def test_anything_else_declares_no_variable(self, sql):
        assert declarations(sql) is None


class TestWhatASelectAssigns:
    def test_several_with_a_top_and_the_rest_kept(self):
        found = select_assignments(
            "SELECT TOP 1 @a = v, @b += w * 2 FROM t WHERE v > 1 ORDER BY v")
        assert [(one.variable, one.operator, one.expression)
                for one in found.assignments] == [
            ("@a", "", "v"), ("@b", "+", "w * 2")]
        assert found.as_a_read() == (
            "SELECT TOP 1 v, @b + (w * 2) FROM t WHERE v > 1 ORDER BY v")
        assert not found.mixed

    def test_one_that_also_reads_is_mixed(self):
        assert select_assignments("SELECT @a = 1, 2 AS b").mixed

    @pytest.mark.parametrize("sql", [
        "SELECT a = 1 FROM t",           # an alias, the other way round
        "SELECT @a AS v",                # reading a variable
        "SELECT @a >= 1 AS v",
        "SELECT name FROM t WHERE @a = 1",
    ])
    def test_a_select_that_reads_assigns_nothing(self, sql):
        assert select_assignments(sql) is None


class TestReadingPastComments:
    """The statement reader has to end a comment where the check does."""

    def test_a_comment_inside_a_comment_ends_with_its_own_close(self):
        # Measured: SELECT 1 /* a /* b */ c */ AS v answers 1. Stopping at
        # the first */ handed the reader ' c */ AS v'.
        assert without_comments("SELECT 1 /* a /* b */ c */ AS v").split() == [
            "SELECT", "1", "AS", "v"]

    def test_a_doubled_bracket_does_not_end_the_name(self):
        # [a]]--b] is one name. Ending it at the first ] read the rest as
        # text, and the -- in it as a comment that swallowed the line.
        assert without_comments("SELECT 1 AS [a]]--b]") == "SELECT 1 AS [a]]--b]"
