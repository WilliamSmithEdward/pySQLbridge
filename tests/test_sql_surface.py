"""The SQL a client or a person actually sends, end to end.

Written against a catalog rather than the parser, because these features only
mean anything together: a GROUP BY over a JOIN filtered by a HAVING that
names an aggregate is four things at once, and each of them parsing is not
the same as the four of them answering correctly.

The expected values are computed from the fixture data rather than written
down, so a change to the fixture cannot quietly make a wrong answer look
right.
"""

import json
import pathlib
import tempfile
from collections import Counter

import pytest

from pysqlbridge.catalog import Catalog
from pysqlbridge.source import from_records
from pysqlbridge.tds.result import Integer, QueryError

PEOPLE = [
    {"id": 1, "name": "ada", "team": "red", "score": 10.5},
    {"id": 2, "name": "Grace", "team": "blue", "score": 20.0},
    {"id": 3, "name": "alan", "team": "red", "score": 30.5},
    {"id": 4, "name": "edsger", "team": "green", "score": None},
    {"id": 5, "name": "barbara", "team": "blue", "score": 40.0},
]

TASKS = [
    {"id": 100, "person_id": 1, "state": "open"},
    {"id": 101, "person_id": 1, "state": "done"},
    {"id": 102, "person_id": 2, "state": "open"},
    {"id": 103, "person_id": 3, "state": "done"},
    {"id": 104, "person_id": 9, "state": "open"},
]


@pytest.fixture
def catalog() -> Catalog:
    c = Catalog()
    c.add(from_records(PEOPLE, name="people"))
    c.add(from_records(TASKS, name="tasks"))
    return c


def rows(catalog, sql):
    return catalog.answer(sql).rows


def one(catalog, sql):
    return catalog.answer(sql).rows[0][0]


class TestWhereOperators:
    def test_like_matches_a_run(self, catalog):
        assert one(catalog, "SELECT COUNT(*) FROM people WHERE name LIKE 'a%'") == 2

    def test_like_matches_one_character(self, catalog):
        assert one(catalog, "SELECT COUNT(*) FROM people WHERE name LIKE '_da'") == 1

    def test_like_matches_a_set(self, catalog):
        assert one(catalog, "SELECT COUNT(*) FROM people WHERE name LIKE '[ab]%'") == 3

    def test_like_is_case_insensitive_like_the_declared_collation(self, catalog):
        # Every text column is declared SQL_Latin1_General_CP1_CI_AS.
        assert one(catalog, "SELECT COUNT(*) FROM people WHERE name LIKE 'GR%'") == 1

    def test_comparison_is_case_insensitive_too(self, catalog):
        assert one(catalog, "SELECT COUNT(*) FROM people WHERE name = 'ADA'") == 1

    def test_not_like_excludes_nulls(self, catalog):
        # Unknown is not true, so a NULL is in neither LIKE nor NOT LIKE.
        both = (one(catalog, "SELECT COUNT(*) FROM people WHERE team LIKE 'r%'")
                + one(catalog, "SELECT COUNT(*) FROM people WHERE team NOT LIKE 'r%'"))
        assert both == len(PEOPLE)

    def test_like_with_an_escape(self, catalog):
        assert one(
            catalog, r"SELECT COUNT(*) FROM people WHERE name LIKE '!%' ESCAPE '!'"
        ) == 0

    def test_in(self, catalog):
        assert one(catalog, "SELECT COUNT(*) FROM people WHERE id IN (1, 3, 5)") == 3

    def test_not_in(self, catalog):
        assert one(catalog, "SELECT COUNT(*) FROM people WHERE id NOT IN (1, 3)") == 3

    def test_between_includes_both_ends(self, catalog):
        assert one(catalog, "SELECT COUNT(*) FROM people WHERE id BETWEEN 2 AND 4") == 3

    def test_not_between(self, catalog):
        assert one(catalog, "SELECT COUNT(*) FROM people WHERE id NOT BETWEEN 2 AND 4") == 2

    def test_a_null_is_never_in_a_list(self, catalog):
        assert one(catalog, "SELECT COUNT(*) FROM people WHERE score IN (10.5)") == 1


class TestGrouping:
    def test_one_row_per_group(self, catalog):
        got = {row[0]: row[1] for row in
               rows(catalog, "SELECT team, COUNT(*) AS n FROM people GROUP BY team")}
        assert got == dict(Counter(p["team"] for p in PEOPLE))

    def test_aggregates_are_per_group(self, catalog):
        got = {row[0]: row[1] for row in rows(
            catalog, "SELECT team, MAX(score) AS best FROM people GROUP BY team"
        )}
        assert got["red"] == 30.5 and got["blue"] == 40.0

    def test_a_group_of_only_nulls_aggregates_to_null(self, catalog):
        got = {row[0]: row[1] for row in rows(
            catalog, "SELECT team, SUM(score) AS total FROM people GROUP BY team"
        )}
        assert got["green"] is None

    def test_having_reads_the_aggregate_by_what_was_written(self, catalog):
        got = rows(catalog, "SELECT team, COUNT(*) AS n FROM people "
                            "GROUP BY team HAVING COUNT(*) > 1")
        assert sorted(row[0] for row in got) == ["blue", "red"]

    def test_having_reads_it_by_alias_too(self, catalog):
        got = rows(catalog, "SELECT team, COUNT(*) AS n FROM people "
                            "GROUP BY team HAVING n > 1")
        assert sorted(row[0] for row in got) == ["blue", "red"]

    def test_grouping_is_case_insensitive(self, catalog):
        c = Catalog()
        c.add(from_records([{"k": "a"}, {"k": "A"}, {"k": "b"}], name="t"))
        assert len(rows(c, "SELECT k, COUNT(*) AS n FROM t GROUP BY k")) == 2

    def test_a_column_neither_grouped_nor_aggregated_is_refused(self, catalog):
        with pytest.raises(QueryError, match="is invalid in the select list"):
            catalog.answer("SELECT name, COUNT(*) FROM people GROUP BY team")

    def test_having_without_group_by_filters_the_one_group(self, catalog):
        # The whole table is one group, and SQL Server takes a HAVING over it.
        assert one(catalog, "SELECT COUNT(*) AS n FROM people "
                            "HAVING COUNT(*) > 1") == len(PEOPLE)

    def test_and_can_exclude_that_group(self, catalog):
        assert rows(catalog, "SELECT COUNT(*) AS n FROM people "
                             "HAVING COUNT(*) > 1000") == []

    def test_a_bare_column_beside_that_having_is_still_refused(self, catalog):
        with pytest.raises(QueryError, match="is invalid in the select list"):
            catalog.answer("SELECT name FROM people HAVING COUNT(*) > 1")

    def test_a_star_beside_it_too(self, catalog):
        with pytest.raises(QueryError, match="cannot be filtered by a HAVING"):
            catalog.answer("SELECT * FROM people HAVING COUNT(*) > 1")

    def test_an_aggregate_only_the_having_names(self, catalog):
        # Computed for the group even though nothing asked to see it.
        highest = max(p["score"] for p in PEOPLE if p["score"] is not None)
        assert one(catalog, f"SELECT COUNT(*) AS n FROM people "
                            f"HAVING MAX(score) = {highest}") == len(PEOPLE)

    def test_an_aggregate_only_the_order_by_names(self, catalog):
        found = rows(catalog, "SELECT team FROM people GROUP BY team "
                              "ORDER BY MAX(score) DESC")
        highest = {}
        for person in PEOPLE:
            if person["score"] is not None:
                highest[person["team"]] = max(
                    highest.get(person["team"], 0), person["score"]
                )
        assert [r[0] for r in found][:1] == [
            max(highest, key=lambda team: highest[team])
        ]

    def test_it_is_dropped_before_the_result_goes_out(self, catalog):
        answer = catalog.answer("SELECT team FROM people GROUP BY team "
                                "ORDER BY MAX(score) DESC")
        assert [c.name for c in answer.columns] == ["team"]

    def test_an_expression_over_two_of_them(self, catalog):
        # Neither MAX nor MIN is in the select list.
        found = rows(catalog, "SELECT team FROM people GROUP BY team "
                              "ORDER BY MAX(score) - MIN(score) DESC, team")
        assert len(found) == len({p["team"] for p in PEOPLE})

    def test_ordering_a_grouped_result(self, catalog):
        got = rows(catalog, "SELECT team, COUNT(*) AS n FROM people "
                            "GROUP BY team ORDER BY n DESC, team")
        assert [row[0] for row in got] == ["blue", "red", "green"]


class TestDistinctAndPaging:
    def test_distinct_drops_repeats(self, catalog):
        assert len(rows(catalog, "SELECT DISTINCT team FROM people")) == 3

    def test_distinct_uses_the_declared_collation(self, catalog):
        c = Catalog()
        c.add(from_records([{"k": "a"}, {"k": "A"}], name="t"))
        assert len(rows(c, "SELECT DISTINCT k FROM t")) == 1

    def test_offset_skips_and_fetch_takes(self, catalog):
        got = rows(catalog, "SELECT id FROM people ORDER BY id "
                            "OFFSET 1 ROWS FETCH NEXT 2 ROWS ONLY")
        assert [row[0] for row in got] == [2, 3]

    def test_offset_without_fetch_takes_the_rest(self, catalog):
        got = rows(catalog, "SELECT id FROM people ORDER BY id OFFSET 3 ROWS")
        assert [row[0] for row in got] == [4, 5]

    def test_offset_without_an_order_is_refused(self, catalog):
        # There is no defined order to skip through.
        with pytest.raises(QueryError, match="needs an ORDER BY"):
            catalog.answer("SELECT id FROM people OFFSET 1 ROWS")


class TestJoins:
    def test_an_inner_join_keeps_only_matches(self, catalog):
        want = sum(1 for t in TASKS for p in PEOPLE if t["person_id"] == p["id"])
        assert one(
            catalog,
            "SELECT COUNT(*) FROM people p JOIN tasks t ON t.person_id = p.id",
        ) == want

    def test_a_left_join_keeps_the_unmatched(self, catalog):
        matched = sum(1 for t in TASKS for p in PEOPLE if t["person_id"] == p["id"])
        lonely = sum(
            1 for p in PEOPLE if not any(t["person_id"] == p["id"] for t in TASKS)
        )
        assert one(
            catalog,
            "SELECT COUNT(*) FROM people p LEFT JOIN tasks t ON t.person_id = p.id",
        ) == matched + lonely

    def test_a_cross_join_is_every_pair(self, catalog):
        assert one(catalog, "SELECT COUNT(*) FROM people CROSS JOIN tasks") == \
            len(PEOPLE) * len(TASKS)

    def test_columns_are_reached_by_their_alias(self, catalog):
        got = rows(catalog, "SELECT p.name, t.state FROM people p "
                            "JOIN tasks t ON t.person_id = p.id ORDER BY t.id")
        assert got[0] == ["ada", "open"]

    def test_a_name_only_one_side_has_needs_no_alias(self, catalog):
        got = rows(catalog, "SELECT state FROM people p "
                            "JOIN tasks t ON t.person_id = p.id ORDER BY t.id")
        assert got[0] == ["open"]

    def test_a_shared_name_keeps_both_apart(self, catalog):
        got = rows(catalog, "SELECT p.id, t.id FROM people p "
                            "JOIN tasks t ON t.person_id = p.id ORDER BY t.id")
        assert got[0] == [1, 100]

    def test_a_join_can_be_grouped(self, catalog):
        got = {row[0]: row[1] for row in rows(
            catalog,
            "SELECT p.team, COUNT(*) AS n FROM people p "
            "JOIN tasks t ON t.person_id = p.id GROUP BY p.team",
        )}
        assert got == {"red": 3, "blue": 1}

    def test_a_join_can_be_filtered(self, catalog):
        assert one(
            catalog,
            "SELECT COUNT(*) FROM people p JOIN tasks t ON t.person_id = p.id "
            "WHERE t.state = 'done'",
        ) == 2

    def test_a_right_join_keeps_the_rows_the_left_matched_nothing_of(self, catalog):
        # task 104 is owned by nobody, so a RIGHT join keeps it with the
        # person's columns empty, and a LEFT one does not.
        right = rows(catalog, "SELECT p.id, t.id FROM people p RIGHT JOIN "
                              "tasks t ON t.person_id = p.id ORDER BY t.id")
        assert right == [[1, 100], [1, 101], [2, 102], [3, 103], [None, 104]]

    def test_a_full_join_keeps_both(self, catalog):
        found = rows(catalog, "SELECT p.id, t.id FROM people p FULL JOIN "
                              "tasks t ON t.person_id = p.id "
                              "ORDER BY p.id, t.id")
        # Everything an inner join has, plus the people with no task and the
        # task with no person.
        assert sorted(found, key=lambda r: (r[0] is None, r)) == [
            [1, 100], [1, 101], [2, 102], [3, 103], [4, None], [5, None],
            [None, 104],
        ]

    def test_the_columns_stay_where_they_were_written(self, catalog):
        # A RIGHT join is not a LEFT one with the tables swapped: the swap
        # would move the columns and the select list names them by position.
        right = catalog.answer("SELECT * FROM people p RIGHT JOIN tasks t "
                               "ON t.person_id = p.id")
        inner = catalog.answer("SELECT * FROM people p JOIN tasks t "
                               "ON t.person_id = p.id")
        assert [c.name for c in right.columns] == [c.name for c in inner.columns]


class TestExpressions:
    def test_arithmetic(self, catalog):
        assert rows(catalog, "SELECT id * 2 + 1 AS n FROM people ORDER BY id")[0] == [3]

    def test_integer_division_truncates_like_sql_server(self, catalog):
        assert one(catalog, "SELECT 7 / 2 AS half") == 3

    def test_dividing_by_zero_is_an_error(self, catalog):
        # Measured against SQL Server 2025, which raises rather than
        # answering NULL.
        with pytest.raises(QueryError, match="Divide by zero"):
            catalog.answer("SELECT 1 / 0 AS oops")

    def test_division_truncates_toward_zero(self, catalog):
        # Python floors, so it would say -4.
        assert one(catalog, "SELECT -7 / 2 AS q") == -3

    def test_the_remainder_takes_the_sign_of_the_dividend(self, catalog):
        # Python takes the sign of the divisor, so it would say 2.
        assert one(catalog, "SELECT -7 % 3 AS m") == -1

    def test_text_beside_a_number_is_added_not_joined(self, catalog):
        # int outranks varchar in SQL Server's type precedence.
        assert one(catalog, "SELECT '1' + 2 AS n") == 3

    def test_two_strings_still_join(self, catalog):
        assert one(catalog, "SELECT 'a' + 'b' AS s") == "ab"

    def test_round_sends_a_half_away_from_zero(self, catalog):
        # Python rounds to even, so it would say 2.
        assert one(catalog, "SELECT ROUND(2.5, 0) AS n") == 3

    def test_substring_before_the_start_still_spends_its_length(self, catalog):
        assert one(catalog, "SELECT SUBSTRING('abc', 0, 2) AS s") == "a"

    def test_a_string_function_is_null_in_every_argument(self, catalog):
        assert one(catalog, "SELECT REPLACE('abc', 'b', NULL) AS s") is None

    def test_sorting_text_uses_the_declared_collation(self, catalog):
        got = rows(catalog, "SELECT name FROM people ORDER BY name")
        assert [row[0] for row in got] == [
            "ada", "alan", "barbara", "edsger", "Grace"
        ]

    def test_text_concatenation(self, catalog):
        got = rows(catalog, "SELECT name + '!' AS shout FROM people ORDER BY id")
        assert got[0] == ["ada!"]

    def test_case_with_conditions(self, catalog):
        got = rows(catalog, "SELECT CASE WHEN id > 3 THEN 'late' ELSE 'early' END "
                            "AS era FROM people ORDER BY id")
        assert [row[0] for row in got] == ["early"] * 3 + ["late"] * 2

    def test_case_against_a_value(self, catalog):
        got = rows(catalog, "SELECT CASE team WHEN 'red' THEN 1 ELSE 0 END AS r "
                            "FROM people ORDER BY id")
        assert [row[0] for row in got] == [1, 0, 1, 0, 0]

    def test_case_with_nothing_matching_is_null(self, catalog):
        assert one(catalog, "SELECT CASE WHEN 1 = 2 THEN 'x' END AS nothing") is None

    def test_cast(self, catalog):
        assert one(catalog, "SELECT CAST('42' AS int) AS n") == 42

    def test_convert_says_it_the_other_way_round(self, catalog):
        assert one(catalog, "SELECT CONVERT(int, '42') AS n") == 42

    @pytest.mark.parametrize("sql,want", [
        ("SELECT UPPER('ada') AS x", "ADA"),
        ("SELECT LOWER('ADA') AS x", "ada"),
        ("SELECT LEN('ada ') AS x", 3),
        ("SELECT LEFT('grace', 2) AS x", "gr"),
        ("SELECT RIGHT('grace', 2) AS x", "ce"),
        ("SELECT SUBSTRING('grace', 2, 3) AS x", "rac"),
        ("SELECT REPLACE('a-b', '-', '+') AS x", "a+b"),
        ("SELECT TRIM('  a  ') AS x", "a"),
        ("SELECT CHARINDEX('a', 'grace') AS x", 3),
        ("SELECT CONCAT('a', 1, 'b') AS x", "a1b"),
        ("SELECT ISNULL(NULL, 'fallback') AS x", "fallback"),
        ("SELECT COALESCE(NULL, NULL, 'third') AS x", "third"),
        ("SELECT NULLIF(1, 1) AS x", None),
        ("SELECT ABS(-3) AS x", 3),
        ("SELECT FLOOR(2.7) AS x", 2),
        ("SELECT CEILING(2.1) AS x", 3),
        ("SELECT ROUND(2.44, 1) AS x", 2.4),
        ("SELECT IIF(1 = 1, 'yes', 'no') AS x", "yes"),
    ])
    def test_the_functions(self, catalog, sql, want):
        assert one(catalog, sql) == want

    def test_a_function_this_does_not_have_says_so(self, catalog):
        with pytest.raises(QueryError, match="not a function"):
            catalog.answer("SELECT NOSUCHTHING(name) FROM people")

    def test_an_expression_is_unnamed_unless_aliased(self, catalog):
        # SQL Server leaves it blank, and clients render that as a blank
        # heading, so inventing a name would be the wrong answer.
        assert catalog.answer("SELECT id + 1 FROM people").columns[0].name == ""

    def test_a_star_can_sit_beside_a_column(self, catalog):
        result = catalog.answer("SELECT *, id AS again FROM people")
        assert [c.name for c in result.columns][-1] == "again"
        assert len(result.columns) == len(PEOPLE[0]) + 1

    def test_a_select_with_no_from_computes(self, catalog):
        assert one(catalog, "SELECT 1 + 1 AS two") == 2

    def test_a_select_with_no_from_cannot_read_a_column(self, catalog):
        with pytest.raises(QueryError, match="can only compute values"):
            catalog.answer("SELECT name")


class TestNestedQueries:
    def test_in_a_subquery(self, catalog):
        want = len({p["id"] for p in PEOPLE
                    if any(t["person_id"] == p["id"] for t in TASKS)})
        assert one(
            catalog,
            "SELECT COUNT(*) FROM people WHERE id IN (SELECT person_id FROM tasks)",
        ) == want

    def test_not_in_a_subquery(self, catalog):
        assert one(
            catalog,
            "SELECT COUNT(*) FROM people WHERE id NOT IN (SELECT person_id FROM tasks)",
        ) == 2

    def test_compared_against_a_subquery(self, catalog):
        assert one(
            catalog,
            "SELECT COUNT(*) FROM people WHERE id > (SELECT MIN(id) FROM people)",
        ) == 4

    def test_exists(self, catalog):
        assert one(
            catalog,
            "SELECT COUNT(*) FROM people WHERE EXISTS (SELECT id FROM tasks)",
        ) == len(PEOPLE)

    def test_exists_of_nothing_keeps_no_rows(self, catalog):
        assert one(
            catalog,
            "SELECT COUNT(*) FROM people WHERE EXISTS "
            "(SELECT id FROM tasks WHERE person_id = 999)",
        ) == 0

    def test_a_named_query(self, catalog):
        assert one(
            catalog,
            "WITH busy AS (SELECT person_id, COUNT(*) AS n FROM tasks "
            "GROUP BY person_id) SELECT COUNT(*) FROM busy WHERE n > 1",
        ) == 1

    def test_a_named_query_can_be_joined(self, catalog):
        got = rows(
            catalog,
            "WITH busy AS (SELECT person_id, COUNT(*) AS n FROM tasks "
            "GROUP BY person_id) "
            "SELECT p.name, b.n FROM people p JOIN busy b ON b.person_id = p.id "
            "ORDER BY b.n DESC, p.name",
        )
        assert got[0] == ["ada", 2]

    def test_a_named_query_can_use_an_earlier_one(self, catalog):
        assert one(
            catalog,
            "WITH a AS (SELECT id FROM people WHERE id < 4), "
            "b AS (SELECT id FROM a WHERE id > 1) SELECT COUNT(*) FROM b",
        ) == 2

    def test_a_subquery_can_use_a_named_query(self, catalog):
        assert one(
            catalog,
            "WITH t AS (SELECT person_id FROM tasks) "
            "SELECT COUNT(*) FROM people WHERE id IN (SELECT person_id FROM t)",
        ) == 3

    def test_a_derived_table(self, catalog):
        assert one(
            catalog,
            "SELECT COUNT(*) FROM (SELECT id FROM people WHERE id > 2) AS big",
        ) == 3

    def test_a_derived_table_needs_a_name(self, catalog):
        with pytest.raises(QueryError, match="needs an alias"):
            catalog.answer("SELECT COUNT(*) FROM (SELECT id FROM people)")

    def test_a_query_that_names_itself_is_refused(self, catalog):
        with pytest.raises(QueryError, match="invalid object name"):
            catalog.answer("WITH loop AS (SELECT id FROM loop) SELECT * FROM loop")

    def test_a_value_subquery_must_select_one_column(self, catalog):
        with pytest.raises(QueryError, match="Only one expression can be specified"):
            catalog.answer(
                "SELECT * FROM people WHERE id IN (SELECT id, name FROM people)"
            )

    def test_a_scalar_subquery_must_return_one_row(self, catalog):
        with pytest.raises(QueryError, match="returned 5 rows"):
            catalog.answer("SELECT * FROM people WHERE id = (SELECT id FROM people)")


class TestWhatARefusalIsCalled:
    """The number beside the message, which a client shows.

    SSMS prints "Msg 8134" next to the words. A divide by zero reported as
    msg 208, invalid object name, sends whoever reads it looking for a table
    that was never the problem. Every number here measured against SQL
    Server 2025, and the comparison in scripts/differential.ps1 now reads
    them too, which is how the nine that were wrong were found.
    """

    def number_of(self, catalog, sql):
        with pytest.raises(QueryError) as refused:
            rows(catalog, sql)
        return refused.value.number

    @pytest.mark.parametrize("sql, number", [
        ("SELECT 1 / 0 AS v", 8134),
        ("SELECT 1 % 0 AS v", 8134),
        ("SELECT COUNT(*) AS n FROM people WHERE id / 0 = 1", 8134),
        ("SELECT CAST('abc' AS int) AS v", 245),
        ("SELECT CAST('abc' AS bit) AS v", 245),
        ("SELECT CAST('abc' AS bigint) AS v", 8114),
        ("SELECT CAST('abc' AS float) AS v", 8114),
        ("SELECT CAST('abc' AS datetime) AS v", 241),
        ("SELECT 'a' + 1 AS v", 245),
        ("SELECT 'a' * 2 AS v", 245),
        ("SELECT 'a' + CAST(1 AS float) AS v", 8114),
        ("SELECT CAST(300 AS tinyint) AS v", 220),
        ("SELECT CAST('300' AS tinyint) AS v", 244),
        ("SELECT CAST('3000000000' AS int) AS v", 248),
        ("SELECT CAST(3000000000 AS int) AS v", 8115),
        ("SELECT LOG(0) AS v", 3623),
        ("SELECT EXP(1000) AS v", 8115),
        ("SELECT SUBSTRING(12345, 2, 2) AS v", 8116),
        ("SELECT TRANSLATE('abc', 'ab', 'x') AS v", 9828),
        ("SELECT CONCAT_WS('-', 'a') AS v", 189),
        ("SELECT DATEPART(fortnight, GETDATE()) AS v", 155),
        ("SELECT DATEADD(day, NULL, GETDATE()) AS v", 8116),
        ("SELECT DATEADD(day, -1, CAST('1753-01-01' AS datetime)) AS v", 517),
        ("SELECT DATEDIFF(second, CAST('1900-01-01' AS datetime), "
         "CAST('2026-01-01' AS datetime)) AS v", 535),
        ("SELECT COUNT(*) AS n FROM people GROUP BY 1", 164),
        ("SELECT name, COUNT(*) AS n FROM people GROUP BY team", 8120),
        ("SELECT id FROM people UNION ALL SELECT name FROM people", 245),
        ("SELECT nosuch FROM people", 208),
        ("SELECT * FROM nope", 208),
        ("DELETE FROM people", 50000),
    ])
    def test_the_number_a_client_is_shown(self, catalog, sql, number):
        assert self.number_of(catalog, sql) == number

    def test_one_sql_server_names_is_not_framed_again(self, catalog):
        # "cannot read 'DATEPART(fortnight, x)' in the select list:
        # 'fortnight' is not a recognized datepart option" says it twice.
        with pytest.raises(QueryError) as refused:
            rows(catalog, "SELECT DATEPART(fortnight, GETDATE()) AS v")
        assert str(refused.value) == (
            "'fortnight' is not a recognized datepart option."
        )

    def test_one_of_this_project_s_own_still_says_where_it_was(self, catalog):
        with pytest.raises(QueryError, match="in the select list"):
            rows(catalog, "SELECT NOSUCHFUNCTION(1) AS v")


class TestMoreScalarFunctions:
    """The string and maths functions a report reaches for.

    Every expected value measured against SQL Server 2025. Several are not
    what a language would do: ASCII of an empty string is NULL rather than
    nought, STUFF from before the first character is NULL rather than the
    first, and a character code outside its type's range is NULL rather than
    an error.
    """

    @pytest.mark.parametrize("expression, expected", [
        # PATINDEX is LIKE anchored at both ends, saying where the match began.
        ("PATINDEX('%a%', 'bad')", 2),
        ("PATINDEX('%z%', 'bad')", 0),
        ("PATINDEX('a%', 'abc')", 1),
        ("PATINDEX('%[0-9]%', 'ab3cd')", 3),
        ("PATINDEX('abc', 'abcy')", 0),
        ("PATINDEX('abc', 'abc')", 1),
        ("PATINDEX('%abc', 'xabc')", 2),
        ("PATINDEX('', 'abc')", 0),
        ("PATINDEX('%', 'abc')", 1),
        ("PATINDEX('_b%', 'abc')", 1),
        ("PATINDEX('%a%', 'BAD')", 2),
        ("PATINDEX(NULL, 'bad')", None),
        # STUFF, whose edges are all NULL rather than a clamp.
        ("STUFF('abcdef', 2, 3, 'XY')", "aXYef"),
        ("STUFF('abcdef', 2, 0, 'XY')", "aXYbcdef"),
        ("STUFF('abcdef', 0, 2, 'X')", None),
        ("STUFF('abcdef', 9, 2, 'X')", None),
        ("STUFF('abcdef', 2, 99, 'X')", "aX"),
        ("STUFF('abcdef', 2, 3, NULL)", "aef"),
        ("STUFF('abcdef', 2, -1, 'X')", None),
        ("STUFF(NULL, 2, 3, 'X')", None),
        ("REPLICATE('ab', 3)", "ababab"),
        ("REPLICATE('ab', 0)", ""),
        ("REPLICATE('ab', -1)", None),
        ("REPLICATE(12, 2)", "1212"),
        ("REPLICATE(NULL, 3)", None),
        # A code and a character, in both widths.
        ("ASCII('A')", 65),
        ("ASCII('abc')", 97),
        ("ASCII(' ')", 32),
        ("ASCII('')", None),
        ("ASCII(65)", 54),
        ("CHAR(65)", "A"),
        ("CHAR('65')", "A"),
        ("CHAR(256)", None),
        ("CHAR(-1)", None),
        ("UNICODE('A')", 65),
        ("UNICODE('')", None),
        ("NCHAR(65)", "A"),
        ("NCHAR(9731)", "\u2603"),
        ("NCHAR(65536)", None),
        ("NCHAR(-1)", None),
        # The values that are NULL are left out, and their separator with them.
        ("CONCAT_WS('-', 'a', 'b', 'c')", "a-b-c"),
        ("CONCAT_WS('-', 'a', NULL, 'c')", "a-c"),
        ("CONCAT_WS('-', NULL, NULL)", ""),
        ("CONCAT_WS(NULL, 'a', 'b')", "ab"),
        ("CONCAT_WS('-', 1, 2)", "1-2"),
        ("CHOOSE(2, 'a', 'b', 'c')", "b"),
        ("CHOOSE(0, 'a', 'b')", None),
        ("CHOOSE(9, 'a', 'b')", None),
        ("CHOOSE(2.9, 'a', 'b', 'c')", "b"),
        ("CHOOSE(NULL, 'a')", None),
        ("TRANSLATE('abcdef', 'abc', 'xyz')", "xyzdef"),
        ("TRANSLATE(NULL, 'ab', 'xy')", None),
    ])
    def test_what_it_answers(self, catalog, expression, expected):
        assert one(catalog, f"SELECT {expression} AS v") == expected

    @pytest.mark.parametrize("expression, expected", [
        ("LOG(1)", 0.0),
        ("LOG(10, 10)", 1.0),
        ("LOG10(100)", 2.0),
        ("EXP(0)", 1.0),
        ("SQUARE(3)", 9.0),
        ("SQUARE(2.5)", 6.25),
        ("SQUARE(-3)", 9.0),
    ])
    def test_the_maths(self, catalog, expression, expected):
        assert one(catalog, f"SELECT {expression} AS v") == expected

    def test_pi(self, catalog):
        assert round(one(catalog, "SELECT PI() AS v"), 10) == 3.1415926536

    def test_square_is_always_a_float(self, catalog):
        # SQUARE(3) is 9.0 rather than 9, and a client reading the column
        # type sees the difference even where the rendered number does not.
        answer = catalog.answer("SELECT SQUARE(3) AS v")
        assert answer.columns[0].type.__class__.__name__ == "Float"

    @pytest.mark.parametrize("expression, said", [
        ("LOG(0)", "invalid floating point operation"),
        ("LOG(-1)", "invalid floating point operation"),
        ("EXP(1000)", "overflow"),
        ("TRANSLATE('abc', 'ab', 'x')", "equal number of characters"),
        ("CONCAT_WS('-', 'a')", "requires 3 to 254 arguments"),
    ])
    def test_what_it_refuses(self, catalog, expression, said):
        with pytest.raises(QueryError, match=said):
            rows(catalog, f"SELECT {expression} AS v")

    def test_patindex_over_a_column(self, catalog):
        found = rows(catalog, "SELECT PATINDEX('%a%', name) AS v FROM people "
                              "ORDER BY id")
        assert [row[0] for row in found] == [
            (name.lower().find("a") + 1) for name in
            [p["name"] for p in PEOPLE]
        ]


class TestWhatElseAFromClauseMaySay:
    """Two things people write out of habit and older tools still generate.

    Tables listed with a comma, which is a cross join written the way it was
    written before JOIN existed, and a hint about locking, which a server
    holding no locks has nothing to do with.
    """

    def test_tables_listed_with_a_comma_are_a_cross_join(self, catalog):
        assert one(catalog, "SELECT COUNT(*) AS n FROM people, tasks") == (
            len(PEOPLE) * len(TASKS)
        )

    def test_a_where_relating_them_makes_it_an_inner_join(self, catalog):
        listed = rows(catalog, "SELECT p.name, t.state FROM people p, tasks t "
                               "WHERE t.person_id = p.id ORDER BY p.id, t.id")
        joined = rows(catalog, "SELECT p.name, t.state FROM people p "
                               "JOIN tasks t ON t.person_id = p.id "
                               "ORDER BY p.id, t.id")
        assert listed == joined

    def test_three_of_them(self, catalog):
        assert one(catalog, "SELECT COUNT(*) AS n FROM people p, tasks t, people q "
                            "WHERE t.person_id = p.id AND q.id = p.id") == 4

    def test_a_join_may_follow_them(self, catalog):
        assert one(catalog, "SELECT COUNT(*) AS n FROM people p, tasks t "
                            "JOIN people q ON q.id = t.person_id") == (
            len(PEOPLE) * 4
        )

    @pytest.mark.parametrize("written", [
        "FROM people WITH (NOLOCK)",
        "FROM people (NOLOCK)",
        "FROM people WITH(NOLOCK)",
        "FROM people AS p WITH (NOLOCK)",
        "FROM people p WITH (NOLOCK)",
        "FROM people WITH (INDEX(ix_name), NOLOCK)",
        "FROM people WITH (NOLOCK), tasks WITH (NOLOCK)",
    ])
    def test_a_hint_says_nothing_here(self, catalog, written):
        # Whatever it says about locking and isolation, this holds no locks
        # and reads a source that was loaded whole.
        assert rows(catalog, f"SELECT COUNT(*) AS n {written}")

    def test_a_hint_on_each_side_of_a_join(self, catalog):
        assert one(catalog, "SELECT COUNT(*) AS n FROM people p WITH (NOLOCK) "
                            "JOIN tasks t WITH (NOLOCK) "
                            "ON t.person_id = p.id") == 4

    def test_a_derived_table_is_still_a_derived_table(self, catalog):
        # The bracket after FROM that is not a hint, and is read before one
        # could be looked for.
        assert one(catalog, "SELECT COUNT(*) AS n FROM "
                            "(SELECT id FROM people) AS x") == len(PEOPLE)


class TestRunningAGroupsValuesTogether:
    """STRING_AGG(value, separator) WITHIN GROUP (ORDER BY ...).

    What a report writes when it wants the names in each team on one line.
    A NULL is left out entirely and the separator around it with it, an
    empty string is a value and stays, and a group holding nothing but
    NULLs is NULL rather than the empty string. Measured, all three.
    """

    def joined(self, catalog, sql):
        return one(catalog, sql)

    def test_in_the_order_it_was_told(self, catalog):
        assert self.joined(catalog, "SELECT STRING_AGG(name, ',') WITHIN "
                                    "GROUP (ORDER BY id) AS v FROM people"
                           ) == "ada,Grace,alan,edsger,barbara"

    def test_in_another_order(self, catalog):
        assert self.joined(catalog, "SELECT STRING_AGG(name, ',') WITHIN "
                                    "GROUP (ORDER BY name DESC) AS v "
                                    "FROM people"
                           ) == "Grace,edsger,barbara,alan,ada"

    def test_per_group(self, catalog):
        assert rows(catalog, "SELECT team, STRING_AGG(name, ',') WITHIN GROUP "
                             "(ORDER BY id) AS v FROM people GROUP BY team "
                             "ORDER BY team") == [
            ["blue", "Grace,barbara"], ["green", "edsger"], ["red", "ada,alan"],
        ]

    def test_a_group_of_nothing_is_null(self, catalog):
        assert self.joined(catalog, "SELECT STRING_AGG(name, ',') AS v "
                                    "FROM people WHERE 1 = 0") is None

    def test_a_group_of_only_nulls_is_null(self, catalog):
        assert self.joined(catalog, "SELECT STRING_AGG(score, ',') AS v "
                                    "FROM people WHERE score IS NULL") is None

    def test_a_null_among_them_is_left_out_with_its_separator(self, catalog):
        # Four people have a score and one does not, so there are three
        # separators and not four.
        joined = self.joined(catalog, "SELECT STRING_AGG(score, ',') WITHIN "
                                      "GROUP (ORDER BY id) AS v FROM people")
        assert joined.count(",") == 3

    def test_numbers_are_written_out(self, catalog):
        assert self.joined(catalog, "SELECT STRING_AGG(id, '-') WITHIN GROUP "
                                    "(ORDER BY id) AS v FROM people"
                           ) == "1-2-3-4-5"

    def test_no_separator_at_all(self, catalog):
        assert self.joined(catalog, "SELECT STRING_AGG(id, NULL) WITHIN GROUP "
                                    "(ORDER BY id) AS v FROM people") == "12345"

    def test_over_an_expression(self, catalog):
        assert self.joined(catalog, "SELECT STRING_AGG(name + '!', ',') WITHIN "
                                    "GROUP (ORDER BY id) AS v FROM people"
                           ).startswith("ada!,Grace!")

    def test_beside_another_aggregate(self, catalog):
        assert rows(catalog, "SELECT team, COUNT(*) AS n, "
                             "STRING_AGG(name, ',') WITHIN GROUP (ORDER BY id) "
                             "AS v FROM people GROUP BY team HAVING COUNT(*) > 1 "
                             "ORDER BY team") == [
            ["blue", 2, "Grace,barbara"], ["red", 2, "ada,alan"],
        ]

    def test_it_is_declared_as_text(self, catalog):
        answer = catalog.answer("SELECT STRING_AGG(id, '-') AS v FROM people")
        assert answer.columns[0].type.__class__.__name__ == "NVarChar"

    def test_it_needs_something_to_put_between_them(self, catalog):
        with pytest.raises(QueryError, match="needs a separator"):
            rows(catalog, "SELECT STRING_AGG(name) AS v FROM people")


class TestComparingAgainstEveryRow:
    """x > ANY (...) and x > ALL (...), and SOME, which is ANY.

    ALL holds where the comparison holds for every row and ANY where it
    holds for one, so an empty set is true for ALL and false for ANY: there
    is no row to break the promise, and none to keep it. Measured, along
    with how the unknowns carry.
    """

    def count(self, catalog, condition):
        return one(catalog, f"SELECT COUNT(*) AS n FROM people WHERE {condition}")

    def test_equal_to_any_is_in(self, catalog):
        assert self.count(catalog, "id = ANY (SELECT person_id FROM tasks)") == (
            self.count(catalog, "id IN (SELECT person_id FROM tasks)")
        )

    def test_unequal_to_all_is_not_in(self, catalog):
        assert self.count(catalog, "id <> ALL (SELECT person_id FROM tasks)") == (
            self.count(catalog, "id NOT IN (SELECT person_id FROM tasks)")
        )

    def test_greater_than_every_one_of_them(self, catalog):
        # Only ids above every task id, of which there are none.
        assert self.count(catalog, "id > ALL (SELECT id FROM tasks)") == 0

    def test_greater_than_one_of_them(self, catalog):
        assert self.count(catalog, "id > ANY (SELECT person_id FROM tasks)") == 4

    def test_some_is_any_under_another_name(self, catalog):
        assert self.count(catalog, "id > SOME (SELECT person_id FROM tasks)") == (
            self.count(catalog, "id > ANY (SELECT person_id FROM tasks)")
        )

    def test_every_one_of_nothing_is_true(self, catalog):
        assert self.count(catalog, "id > ALL (SELECT id FROM tasks WHERE 1 = 0)"
                          ) == len(PEOPLE)

    def test_one_of_nothing_is_false(self, catalog):
        assert self.count(catalog, "id > ANY (SELECT id FROM tasks WHERE 1 = 0)"
                          ) == 0

    def test_a_null_operand_settles_nothing(self, catalog):
        assert self.count(catalog, "score > ANY (SELECT id FROM tasks)") == 0

    def test_it_reads_more_than_one_column_as_an_error(self, catalog):
        with pytest.raises(QueryError) as refused:
            rows(catalog, "SELECT COUNT(*) AS n FROM people "
                          "WHERE id = ANY (SELECT id, person_id FROM tasks)")
        assert refused.value.number == 116


class TestTheOtherThingsTopMaySay:
    """TOP n PERCENT, and TOP n WITH TIES.

    A share of the rows rounded up, and whatever ties with the last row
    taken. Both measured against SQL Server 2025; the rounding is the part
    worth naming, since one percent of six rows is one row and not none.
    """

    def ids(self, catalog, sql):
        return [row[0] for row in rows(catalog, sql)]

    @pytest.mark.parametrize("share, kept", [
        (0, 0), (1, 1), (20, 1), (21, 2), (40, 2), (50, 3), (100, 5),
    ])
    def test_a_share_of_the_rows_rounds_up(self, catalog, share, kept):
        assert len(self.ids(catalog, f"SELECT TOP {share} PERCENT id "
                                     f"FROM people ORDER BY id")) == kept

    def test_a_share_is_of_what_survived_the_where(self, catalog):
        assert self.ids(catalog, "SELECT TOP 50 PERCENT id FROM people "
                                 "WHERE id > 2 ORDER BY id") == [3, 4]

    def test_ties_come_with_the_last_row_taken(self, catalog):
        # Two people are on team red, so asking for one row of an ordering
        # by team gets both of them.
        assert self.ids(catalog, "SELECT TOP 1 WITH TIES id FROM people "
                                 "ORDER BY team") == [2, 5]

    def test_no_ties_is_the_plain_answer(self, catalog):
        assert self.ids(catalog, "SELECT TOP 2 WITH TIES id FROM people "
                                 "ORDER BY id") == [1, 2]

    def test_everything_ties_on_something_constant(self, catalog):
        # The sort key is read the way the sort read it, so a key that is an
        # expression ties on what it works out to.
        assert self.ids(catalog, "SELECT TOP 1 WITH TIES id FROM people "
                                 "ORDER BY id - id") == [1, 2, 3, 4, 5]

    def test_more_than_there_are(self, catalog):
        assert len(self.ids(catalog, "SELECT TOP 9 WITH TIES id FROM people "
                                     "ORDER BY id")) == len(PEOPLE)

    def test_ties_over_groups(self, catalog):
        # Two teams have two people each, so the top group by count is two
        # groups.
        found = rows(catalog, "SELECT TOP 1 WITH TIES team, COUNT(*) AS n "
                              "FROM people GROUP BY team ORDER BY n DESC")
        assert len(found) == 2
        assert {row[1] for row in found} == {2}

    def test_ties_need_something_to_tie_on(self, catalog):
        with pytest.raises(QueryError) as refused:
            rows(catalog, "SELECT TOP 2 WITH TIES id FROM people")
        assert refused.value.number == 1062

    def test_ties_beside_distinct_says_so(self, catalog):
        # A real server takes it. This does not, because DISTINCT decides
        # which rows there are and the ties are on what the sort said.
        with pytest.raises(QueryError, match="beside DISTINCT is not supported"):
            rows(catalog, "SELECT DISTINCT TOP 1 WITH TIES team FROM people "
                          "ORDER BY team")


class TestAWindowFunction:
    """FUNC(...) OVER (PARTITION BY ... ORDER BY ...).

    Neither an aggregate, which reduces many rows to one, nor an ordinary
    expression, which reads one row: it answers once per row and reads a set
    of rows to do it. Every expected value measured against SQL Server 2025.
    """

    def column(self, catalog, sql):
        return [row[-1] for row in rows(catalog, sql)]

    def test_row_number(self, catalog):
        assert self.column(catalog, "SELECT id, ROW_NUMBER() OVER (ORDER BY id) "
                                    "AS r FROM people ORDER BY id") == [1, 2, 3, 4, 5]

    def test_row_number_in_the_windows_own_order(self, catalog):
        # The window works in its order and the statement sorts in its own.
        assert self.column(catalog, "SELECT id, ROW_NUMBER() OVER (ORDER BY id "
                                    "DESC) AS r FROM people ORDER BY id") == [5, 4, 3, 2, 1]

    def test_nulls_sort_first_in_a_window_too(self, catalog):
        # score is NULL for edsger, id 4, so that row is first in the window.
        assert self.column(catalog, "SELECT id, ROW_NUMBER() OVER (ORDER BY "
                                    "score) AS r FROM people ORDER BY id"
                           ) == [2, 3, 4, 1, 5]

    def test_rank_counts_the_rows_before_the_ties(self, catalog):
        # Two rows share team red, and RANK leaves a gap after them where
        # DENSE_RANK does not.
        ranked = self.column(catalog, "SELECT id, RANK() OVER (ORDER BY team) "
                                      "AS r FROM people ORDER BY id")
        dense = self.column(catalog, "SELECT id, DENSE_RANK() OVER (ORDER BY "
                                     "team) AS r FROM people ORDER BY id")
        assert ranked == [4, 1, 4, 3, 1]
        assert dense == [3, 1, 3, 2, 1]

    def test_ntile_puts_the_bigger_tiles_first(self, catalog):
        # Five rows into three tiles is two, two, one.
        assert self.column(catalog, "SELECT id, NTILE(3) OVER (ORDER BY id) "
                                    "AS r FROM people ORDER BY id") == [1, 1, 2, 2, 3]

    def test_a_partition_is_a_window_of_its_own(self, catalog):
        assert self.column(catalog, "SELECT id, ROW_NUMBER() OVER (PARTITION BY "
                                    "team ORDER BY id) AS r FROM people "
                                    "ORDER BY id") == [1, 1, 2, 1, 2]

    def test_an_aggregate_over_a_window_answers_per_row(self, catalog):
        # Not a reduction: every row keeps its place and is told the count.
        assert self.column(catalog, "SELECT id, COUNT(*) OVER () AS n "
                                    "FROM people ORDER BY id") == [len(PEOPLE)] * len(PEOPLE)

    def test_an_aggregate_over_a_partition(self, catalog):
        assert self.column(catalog, "SELECT id, SUM(score) OVER (PARTITION BY "
                                    "team) AS s FROM people ORDER BY id"
                           ) == [41.0, 60.0, 41.0, None, 60.0]

    def test_an_order_makes_it_run(self, catalog):
        # The frame reaches from the start of the partition to this row, so
        # an aggregate over an ordered window is a running one.
        assert self.column(catalog, "SELECT id, SUM(score) OVER (ORDER BY id) "
                                    "AS s FROM people ORDER BY id"
                           ) == [10.5, 30.5, 61.0, 61.0, 101.0]

    def test_and_ties_share_what_it_reaches(self, catalog):
        # The frame ends at the last row this one ties with, which is why
        # ordering by something with ties is not a running total within them.
        assert self.column(catalog, "SELECT id, COUNT(*) OVER (ORDER BY team) "
                                    "AS n FROM people ORDER BY id") == [5, 2, 5, 3, 2]

    def test_lag_and_lead_read_a_neighbour(self, catalog):
        assert self.column(catalog, "SELECT id, LAG(id) OVER (ORDER BY id) AS a "
                                    "FROM people ORDER BY id") == [None, 1, 2, 3, 4]
        assert self.column(catalog, "SELECT id, LEAD(id) OVER (ORDER BY id) AS a "
                                    "FROM people ORDER BY id") == [2, 3, 4, 5, None]

    def test_lag_takes_a_distance_and_something_for_the_edge(self, catalog):
        assert self.column(catalog, "SELECT id, LAG(id, 2, -1) OVER (ORDER BY id) "
                                    "AS a FROM people ORDER BY id") == [-1, -1, 1, 2, 3]

    def test_first_value_and_last_value(self, catalog):
        # LAST_VALUE with a plain order is this row, because the frame ends
        # here. It is the classic surprise and it is what SQL Server does.
        assert self.column(catalog, "SELECT id, FIRST_VALUE(id) OVER (ORDER BY "
                                    "id) AS a FROM people ORDER BY id") == [1] * 5
        assert self.column(catalog, "SELECT id, LAST_VALUE(id) OVER (ORDER BY "
                                    "id) AS a FROM people ORDER BY id") == [1, 2, 3, 4, 5]

    def test_it_is_counted_in_a_bigint(self, catalog):
        answer = catalog.answer("SELECT ROW_NUMBER() OVER (ORDER BY id) AS r "
                                "FROM people")
        assert answer.columns[0].type == Integer(8)

    def test_it_runs_over_what_the_where_kept(self, catalog):
        assert self.column(catalog, "SELECT id, ROW_NUMBER() OVER (ORDER BY id) "
                                    "AS r FROM people WHERE id > 2 "
                                    "ORDER BY id") == [1, 2, 3]

    def test_the_statement_may_order_by_its_name(self, catalog):
        found = rows(catalog, "SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS r "
                              "FROM people ORDER BY r DESC")
        assert [row[0] for row in found] == [5, 4, 3, 2, 1]

    def test_top_takes_the_rows_after_the_window_is_worked_out(self, catalog):
        found = rows(catalog, "SELECT TOP 2 id, ROW_NUMBER() OVER (ORDER BY id "
                              "DESC) AS r FROM people ORDER BY id")
        assert found == [[1, 5], [2, 4]]

    def test_a_star_does_not_reach_it(self, catalog):
        answer = catalog.answer("SELECT *, ROW_NUMBER() OVER (ORDER BY id) AS r "
                                "FROM people")
        assert len(answer.columns) == len(PEOPLE[0]) + 1

    def test_over_no_rows_at_all(self, catalog):
        assert rows(catalog, "SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS r "
                             "FROM people WHERE id > 99") == []

    @pytest.mark.parametrize("sql, number", [
        ("SELECT id FROM people WHERE ROW_NUMBER() OVER (ORDER BY id) = 1", 4108),
        ("SELECT id FROM people GROUP BY id HAVING COUNT(*) OVER () = 1", 4108),
        ("SELECT COUNT(*) AS n FROM people GROUP BY ROW_NUMBER() OVER (ORDER BY id)", 4108),
        ("SELECT ROW_NUMBER() AS r FROM people", 10753),
        ("SELECT ROW_NUMBER() OVER () AS r FROM people", 4112),
        ("SELECT SUM(DISTINCT score) OVER () AS s FROM people", 10759),
    ])
    def test_where_a_window_may_not_be(self, catalog, sql, number):
        with pytest.raises(QueryError) as refused:
            rows(catalog, sql)
        assert refused.value.number == number

    @pytest.mark.parametrize("sql, said", [
        ("SELECT id FROM people ORDER BY ROW_NUMBER() OVER (ORDER BY id)",
         "name it in the select list"),
        ("SELECT team, COUNT(*) AS n, ROW_NUMBER() OVER (ORDER BY team) AS r "
         "FROM people GROUP BY team", "beside a GROUP BY is not supported"),
    ])
    def test_what_is_refused_by_name(self, catalog, sql, said):
        # Two things a real server takes and this one does not. Each says so,
        # because a wrong number is worse than no number.
        with pytest.raises(QueryError, match=said):
            rows(catalog, sql)


class TestAWindowFrame:
    """How much of the window one row sees.

    Written as ROWS, which counts rows, or RANGE, which counts them by what
    they tie on. The frame a query does not write is RANGE from the start of
    the partition to the end of this row's ties, which is what makes an
    ordered SUM a running total. Measured against SQL Server 2025.
    """

    def column(self, catalog, over):
        return [row[-1] for row in rows(
            catalog, f"SELECT id, SUM(id) OVER ({over}) AS s FROM people "
                     f"ORDER BY id")]

    @pytest.mark.parametrize("over, expected", [
        ("ORDER BY id ROWS UNBOUNDED PRECEDING", [1, 3, 6, 10, 15]),
        ("ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
         [1, 3, 6, 10, 15]),
        ("ORDER BY id ROWS BETWEEN 1 PRECEDING AND CURRENT ROW",
         [1, 3, 5, 7, 9]),
        ("ORDER BY id ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING",
         [3, 6, 9, 12, 9]),
        ("ORDER BY id ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING",
         [15, 14, 12, 9, 5]),
        ("ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING",
         [15, 15, 15, 15, 15]),
        ("ORDER BY id ROWS CURRENT ROW", [1, 2, 3, 4, 5]),
        # Wider than the partition is the whole partition; there is no error
        # in asking for rows that are not there.
        ("ORDER BY id ROWS BETWEEN 9 PRECEDING AND 9 FOLLOWING",
         [15, 15, 15, 15, 15]),
    ])
    def test_what_rows_it_covers(self, catalog, over, expected):
        assert self.column(catalog, over) == expected

    def test_a_frame_that_holds_nothing(self, catalog):
        # Two rows before this one, at the first row, is no rows at all, and
        # an aggregate over none of them is NULL rather than an error.
        assert self.column(
            catalog, "ORDER BY id ROWS BETWEEN 2 PRECEDING AND 1 PRECEDING"
        ) == [None, 1, 3, 5, 7]

    def test_it_stays_inside_the_partition(self, catalog):
        found = rows(catalog, "SELECT id, SUM(id) OVER (PARTITION BY team "
                              "ORDER BY id ROWS BETWEEN 1 PRECEDING AND "
                              "CURRENT ROW) AS s FROM people ORDER BY id")
        assert [row[-1] for row in found] == [1, 2, 4, 4, 7]

    def test_counting_over_a_frame(self, catalog):
        found = rows(catalog, "SELECT id, COUNT(*) OVER (ORDER BY id ROWS "
                              "BETWEEN 1 PRECEDING AND 1 FOLLOWING) AS n "
                              "FROM people ORDER BY id")
        assert [row[-1] for row in found] == [2, 3, 3, 3, 2]

    def test_last_value_told_to_look_ahead(self, catalog):
        # The classic surprise undone: with the frame reaching the end of
        # the partition, LAST_VALUE is the last value.
        found = rows(catalog, "SELECT id, LAST_VALUE(id) OVER (ORDER BY id "
                              "ROWS BETWEEN UNBOUNDED PRECEDING AND "
                              "UNBOUNDED FOLLOWING) AS l FROM people "
                              "ORDER BY id")
        assert [row[-1] for row in found] == [5] * len(PEOPLE)

    def test_first_value_over_a_moving_frame(self, catalog):
        found = rows(catalog, "SELECT id, FIRST_VALUE(id) OVER (ORDER BY id "
                              "ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS f "
                              "FROM people ORDER BY id")
        assert [row[-1] for row in found] == [1, 1, 2, 3, 4]

    def test_range_reaches_the_end_of_the_ties(self, catalog):
        # RANGE counts by what rows tie on rather than by rows, so every row
        # of a team is told the same number.
        found = rows(catalog, "SELECT id, COUNT(*) OVER (ORDER BY team RANGE "
                              "BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) "
                              "AS n FROM people ORDER BY id")
        assert [row[-1] for row in found] == [5, 2, 5, 3, 2]

    @pytest.mark.parametrize("sql, number", [
        ("SELECT SUM(id) OVER (ORDER BY id RANGE BETWEEN 1 PRECEDING AND "
         "CURRENT ROW) AS s FROM people", 4194),
        ("SELECT SUM(id) OVER (ORDER BY id ROWS BETWEEN 1 FOLLOWING AND "
         "1 PRECEDING) AS s FROM people", 4193),
        ("SELECT ROW_NUMBER() OVER (ORDER BY id ROWS UNBOUNDED PRECEDING) "
         "AS r FROM people", 10752),
    ])
    def test_a_frame_written_wrongly(self, catalog, sql, number):
        with pytest.raises(QueryError) as refused:
            rows(catalog, sql)
        assert refused.value.number == number

    def test_a_frame_needs_an_order_to_count_from(self, catalog):
        with pytest.raises(QueryError, match="Incorrect syntax near 'ROWS'"):
            rows(catalog, "SELECT SUM(id) OVER (ROWS UNBOUNDED PRECEDING) "
                          "AS s FROM people")


class TestAnAggregateInsideAnExpression:
    """MAX(a) - MIN(a), SUM(a) / COUNT(*), COUNT(*) * 100.0 / n.

    The shape of every spread and every percentage in every report. An
    aggregate could be a whole select-list entry and nothing more, so the
    parser stopped at its closing bracket and the rest of the entry read as
    a mistake: "expected FROM after the column list, found '- MIN(score)'".
    """

    def test_a_spread(self, catalog):
        scores = [p["score"] for p in PEOPLE if p["score"] is not None]
        assert one(catalog, "SELECT MAX(score) - MIN(score) AS v FROM people") == (
            max(scores) - min(scores)
        )

    def test_a_count_scaled(self, catalog):
        assert one(catalog, "SELECT COUNT(*) * 2 AS v FROM people") == 2 * len(PEOPLE)

    def test_an_average_the_long_way(self, catalog):
        scores = [p["score"] for p in PEOPLE if p["score"] is not None]
        assert one(catalog, "SELECT SUM(score) / COUNT(score) AS v FROM people") == (
            sum(scores) / len(scores)
        )

    def test_an_aggregate_inside_a_function(self, catalog):
        scores = [p["score"] for p in PEOPLE if p["score"] is not None]
        assert one(catalog, "SELECT ABS(MIN(score)) AS v FROM people") == abs(min(scores))

    def test_an_aggregate_inside_a_cast(self, catalog):
        assert one(catalog, "SELECT CAST(COUNT(*) AS nvarchar(10)) AS v FROM people"
                   ) == str(len(PEOPLE))

    def test_an_aggregate_inside_a_case(self, catalog):
        assert one(catalog, "SELECT CASE WHEN COUNT(*) > 3 THEN 'many' ELSE 'few' "
                            "END AS v FROM people") == "many"

    def test_an_aggregate_of_an_expression(self, catalog):
        # The aggregate reduces something worked out per row, and does it
        # inside a larger value. Its name is what it says, put back together
        # from the tokens it took.
        scores = [p["score"] for p in PEOPLE if p["score"] is not None]
        assert one(catalog, "SELECT MAX(score * 2) - MIN(score * 2) AS v "
                            "FROM people") == 2 * max(scores) - 2 * min(scores)

    def test_a_count_of_distinct(self, catalog):
        teams = {p["team"] for p in PEOPLE}
        assert one(catalog, "SELECT COUNT(DISTINCT team) * 10 AS v FROM people"
                   ) == 10 * len(teams)

    def test_per_group(self, catalog):
        found = rows(catalog, "SELECT team, MAX(score) - MIN(score) AS v "
                              "FROM people GROUP BY team ORDER BY team")
        # green holds one row and its score is NULL, so both aggregates are
        # NULL and so is what they make.
        assert found == [["blue", 20.0], ["green", None], ["red", 20.0]]

    def test_beside_the_aggregates_it_computes_over(self, catalog):
        found = rows(catalog, "SELECT team, COUNT(*) AS n, "
                              "MAX(score) - MIN(score) AS spread FROM people "
                              "GROUP BY team ORDER BY team")
        assert found == [["blue", 2, 20.0], ["green", 1, None], ["red", 2, 20.0]]

    def test_with_a_having(self, catalog):
        found = rows(catalog, "SELECT team, MAX(score) - MIN(score) AS v "
                              "FROM people GROUP BY team HAVING COUNT(*) > 1 "
                              "ORDER BY team")
        assert found == [["blue", 20.0], ["red", 20.0]]

    def test_ordered_by_the_same_expression(self, catalog):
        found = rows(catalog, "SELECT team FROM people GROUP BY team "
                              "ORDER BY MAX(score) - MIN(score), team")
        assert found == [["green"], ["blue"], ["red"]]

    def test_a_column_beside_it_still_has_to_be_grouped(self, catalog):
        # The entry is not itself an aggregate, and the rows are still being
        # reduced, so a plain column beside it is as wrong as ever.
        with pytest.raises(QueryError, match="is invalid in the select list"):
            rows(catalog, "SELECT name, MAX(score) - MIN(score) AS v FROM people")

    def test_the_column_is_declared_by_what_it_produced(self, catalog):
        answer = catalog.answer("SELECT COUNT(*) * 2 AS v FROM people")
        assert answer.columns[0].type.__class__.__name__ == "Integer"


class TestAHavingThatHoldsASubquery:
    """A HAVING may ask a question of its own, the way a WHERE may.

    Lifted the same way, and it was not: the condition reached the parser
    with a SELECT still written out in it and failed on the bracket, which
    said nothing about what was wrong.
    """

    def test_a_subquery_that_is_just_a_value(self, catalog):
        assert rows(catalog, "SELECT team FROM people GROUP BY team "
                             "HAVING COUNT(*) = (SELECT 2) ORDER BY team"
                    ) == [["blue"], ["red"]]

    def test_a_subquery_over_another_table(self, catalog):
        assert rows(catalog, "SELECT team FROM people GROUP BY team "
                             "HAVING COUNT(*) > (SELECT MIN(id) FROM tasks) "
                             "ORDER BY team") == []

    def test_the_biggest_group(self, catalog):
        # The shape a report writes when it wants whichever group is largest.
        assert rows(catalog, "SELECT team FROM people GROUP BY team "
                             "HAVING COUNT(*) = (SELECT MAX(c) FROM "
                             "(SELECT COUNT(*) AS c FROM people GROUP BY team) "
                             "AS x) ORDER BY team") == [["blue"], ["red"]]

    def test_a_having_with_no_subquery_still_works(self, catalog):
        assert rows(catalog, "SELECT team FROM people GROUP BY team "
                             "HAVING COUNT(*) > 1 ORDER BY team"
                    ) == [["blue"], ["red"]]


class TestGroupingOnAnExpression:
    """GROUP BY what a value works out to, not only what a column holds.

    Which is what a report groups by: the year of a date, the first letter
    of a name, a column folded to one case, a number put in a band. Only a
    bare column name was accepted before, and everything else was refused
    with a complaint about reading a table.
    """

    def test_a_column_folded_to_one_case(self, catalog):
        found = rows(catalog, "SELECT UPPER(team) AS t, COUNT(*) AS n "
                              "FROM people GROUP BY UPPER(team) ORDER BY t")
        assert found == [["BLUE", 2], ["GREEN", 1], ["RED", 2]]

    def test_a_first_letter(self, catalog):
        found = rows(catalog, "SELECT LEFT(name, 1) AS c, COUNT(*) AS n "
                              "FROM people GROUP BY LEFT(name, 1) ORDER BY c")
        # The value a group reports is the first row's, and the order is the
        # declared collation's, so G sorts after e and is still written G.
        assert found == [["a", 2], ["b", 1], ["e", 1], ["G", 1]]

    def test_arithmetic(self, catalog):
        found = rows(catalog, "SELECT id * 0 AS z, COUNT(*) AS n FROM people "
                              "GROUP BY id * 0")
        assert found == [[0, len(PEOPLE)]]

    def test_a_case(self, catalog):
        found = rows(catalog,
                     "SELECT CASE WHEN score > 20 THEN 'high' ELSE 'low' END "
                     "AS band, COUNT(*) AS n FROM people "
                     "GROUP BY CASE WHEN score > 20 THEN 'high' ELSE 'low' END "
                     "ORDER BY band")
        assert found == [["high", 2], ["low", 3]]

    def test_spacing_and_case_do_not_have_to_match(self, catalog):
        # The select list is matched against the GROUP BY by what it says,
        # so the two spellings have to come out the same.
        found = rows(catalog, "SELECT UPPER(team) AS t, COUNT(*) AS n "
                              "FROM people GROUP BY upper( team ) ORDER BY t")
        assert found == [["BLUE", 2], ["GREEN", 1], ["RED", 2]]

    def test_beside_a_column(self, catalog):
        found = rows(catalog, "SELECT UPPER(team) AS t, id, COUNT(*) AS n "
                              "FROM people GROUP BY UPPER(team), id "
                              "ORDER BY t, id")
        assert len(found) == len(PEOPLE)

    def test_with_a_having(self, catalog):
        found = rows(catalog, "SELECT LEFT(name, 1) AS c, COUNT(*) AS n "
                              "FROM people GROUP BY LEFT(name, 1) "
                              "HAVING COUNT(*) > 1 ORDER BY c")
        assert found == [["a", 2]]

    def test_ordered_by_the_expression_itself(self, catalog):
        found = rows(catalog, "SELECT UPPER(team) AS t FROM people "
                              "GROUP BY UPPER(team) ORDER BY UPPER(team)")
        assert found == [["BLUE"], ["GREEN"], ["RED"]]

    def test_over_a_join(self, catalog):
        found = rows(catalog, "SELECT UPPER(p.team) AS t, COUNT(*) AS n "
                              "FROM people AS p JOIN tasks AS k "
                              "ON k.person_id = p.id GROUP BY UPPER(p.team) "
                              "ORDER BY t")
        assert found == [["BLUE", 1], ["RED", 3]]

    def test_the_select_list_need_not_show_it(self, catalog):
        found = rows(catalog, "SELECT COUNT(*) AS n FROM people "
                              "GROUP BY LEFT(name, 1)")
        assert sorted(row[0] for row in found) == [1, 1, 1, 2]

    def test_a_grouped_expression_is_declared_by_what_it_produced(self, catalog):
        # One group working out NULL would otherwise decide the whole column
        # was text, because compute sees a group at a time.
        answer = catalog.answer(
            "SELECT YEAR(DATEADD(day, score, CAST('2026-01-01' AS datetime))) "
            "AS y, COUNT(*) AS n FROM people "
            "GROUP BY YEAR(DATEADD(day, score, CAST('2026-01-01' AS datetime)))"
        )
        assert answer.columns[0].type.__class__.__name__ == "Integer"

    def test_a_column_that_is_neither_grouped_nor_aggregated_is_refused(self, catalog):
        with pytest.raises(QueryError, match="is invalid in the select list"):
            rows(catalog, "SELECT name, COUNT(*) AS n FROM people GROUP BY team")

    def test_an_expression_that_is_not_the_grouped_one_is_refused(self, catalog):
        with pytest.raises(QueryError, match="is invalid in the select list"):
            rows(catalog, "SELECT LOWER(team) AS t, COUNT(*) AS n FROM people "
                          "GROUP BY UPPER(team)")


class TestDates:
    """The date functions, all measured against SQL Server 2025.

    The moment used throughout is a Tuesday afternoon in the third quarter,
    in week 37, so that every part of it is a different number and a part
    read as the wrong one shows up as a wrong answer rather than a coincidence.
    """

    MOMENT = "CAST('2026-09-08T14:35:47.123' AS datetime)"

    def value(self, catalog, expression):
        return one(catalog, f"SELECT {expression} AS v".replace("@", self.MOMENT))

    @pytest.mark.parametrize("expression, expected", [
        ("YEAR(@)", 2026), ("MONTH(@)", 9), ("DAY(@)", 8),
        ("DATEPART(year, @)", 2026),
        ("DATEPART(quarter, @)", 3),
        ("DATEPART(dayofyear, @)", 251),
        ("DATEPART(day, @)", 8),
        ("DATEPART(week, @)", 37),
        ("DATEPART(weekday, @)", 3),
        ("DATEPART(hour, @)", 14),
        ("DATEPART(minute, @)", 35),
        ("DATEPART(second, @)", 47),
        ("DATEPART(millisecond, @)", 123),
    ])
    def test_a_part_of_a_date(self, catalog, expression, expected):
        assert self.value(catalog, expression) == expected

    @pytest.mark.parametrize("short, full", [
        ("yy", "year"), ("yyyy", "year"), ("qq", "quarter"), ("q", "quarter"),
        ("mm", "month"), ("m", "month"), ("dy", "dayofyear"), ("y", "dayofyear"),
        ("dd", "day"), ("d", "day"), ("wk", "week"), ("ww", "week"),
        ("dw", "weekday"), ("w", "weekday"), ("hh", "hour"), ("mi", "minute"),
        ("n", "minute"), ("ss", "second"), ("s", "second"), ("ms", "millisecond"),
    ])
    def test_the_abbreviations_mean_what_they_are_short_for(
            self, catalog, short, full):
        # y is the day of the year and d is the day of the month. One letter
        # apart, different numbers, and easy to write the wrong one.
        assert (self.value(catalog, f"DATEPART({short}, @)")
                == self.value(catalog, f"DATEPART({full}, @)"))

    def test_a_week_starts_on_sunday_and_the_first_one_holds_january(self, catalog):
        # 2026 opens on a Thursday, so the 4th is the first Sunday and is
        # already week two, and the year runs to week fifty-three.
        assert self.value(catalog, "DATEPART(week, CAST('2026-01-01' AS datetime))") == 1
        assert self.value(catalog, "DATEPART(week, CAST('2026-01-04' AS datetime))") == 2
        assert self.value(catalog, "DATEPART(week, CAST('2026-12-31' AS datetime))") == 53

    def test_a_weekday_counts_sunday_as_one(self, catalog):
        assert self.value(catalog, "DATEPART(weekday, CAST('2026-09-06' AS datetime))") == 1
        assert self.value(catalog, "DATEPART(weekday, CAST('2026-09-07' AS datetime))") == 2
        assert self.value(catalog, "DATEPART(weekday, CAST('2026-01-01' AS datetime))") == 5

    def test_only_two_parts_are_written_as_words(self, catalog):
        assert self.value(catalog, "DATENAME(month, @)") == "September"
        assert self.value(catalog, "DATENAME(weekday, @)") == "Tuesday"
        assert self.value(catalog, "DATENAME(year, @)") == "2026"
        assert self.value(catalog, "DATENAME(quarter, @)") == "3"

    @pytest.mark.parametrize("expression, expected", [
        ("DATEADD(day, 1, @)", "2026-09-09T14:35:47.123000"),
        ("DATEADD(day, -1, @)", "2026-09-07T14:35:47.123000"),
        ("DATEADD(week, 2, @)", "2026-09-22T14:35:47.123000"),
        ("DATEADD(month, 1, @)", "2026-10-08T14:35:47.123000"),
        ("DATEADD(quarter, 1, @)", "2026-12-08T14:35:47.123000"),
        ("DATEADD(year, -1, @)", "2025-09-08T14:35:47.123000"),
        ("DATEADD(hour, 10, @)", "2026-09-09T00:35:47.123000"),
        ("DATEADD(minute, -90, @)", "2026-09-08T13:05:47.123000"),
        ("DATEADD(second, 30, @)", "2026-09-08T14:36:17.123000"),
    ])
    def test_moving_a_date_along(self, catalog, expression, expected):
        assert self.value(catalog, expression).isoformat() == expected

    def test_the_number_of_parts_is_truncated_toward_zero(self, catalog):
        forward = self.value(catalog, "DATEADD(day, 1.9, @)")
        back = self.value(catalog, "DATEADD(day, -1.9, @)")
        assert (forward.day, back.day) == (9, 7)

    @pytest.mark.parametrize("expression, expected", [
        ("DATEADD(month, 1, CAST('2026-01-31' AS datetime))", "2026-02-28"),
        ("DATEADD(month, -1, CAST('2026-03-31' AS datetime))", "2026-02-28"),
        ("DATEADD(month, 1, CAST('2026-08-31' AS datetime))", "2026-09-30"),
        ("DATEADD(year, 1, CAST('2024-02-29' AS datetime))", "2025-02-28"),
    ])
    def test_a_month_is_held_back_rather_than_spilling(
            self, catalog, expression, expected):
        # A month after the 31st of January is the 28th of February, not the
        # 3rd of March.
        assert self.value(catalog, expression).date().isoformat() == expected

    def test_past_what_a_datetime_holds_is_an_overflow(self, catalog):
        with pytest.raises(QueryError, match="overflow"):
            rows(catalog, "SELECT DATEADD(day, -1, CAST('1753-01-01' AS datetime)) AS v")

    @pytest.mark.parametrize("expression, expected", [
        # Boundaries crossed, not elapsed time: a minute either side of
        # midnight is a day, and a whole day inside one date is none.
        ("DATEDIFF(day, CAST('2026-01-01 23:59' AS datetime), "
         "CAST('2026-01-02 00:01' AS datetime))", 1),
        ("DATEDIFF(day, CAST('2026-01-01 00:00' AS datetime), "
         "CAST('2026-01-01 23:59' AS datetime))", 0),
        ("DATEDIFF(year, CAST('2026-12-31' AS datetime), "
         "CAST('2027-01-01' AS datetime))", 1),
        ("DATEDIFF(month, CAST('2026-01-31' AS datetime), "
         "CAST('2026-02-01' AS datetime))", 1),
        ("DATEDIFF(quarter, CAST('2026-03-31' AS datetime), "
         "CAST('2026-04-01' AS datetime))", 1),
        ("DATEDIFF(week, CAST('2026-01-03' AS datetime), "
         "CAST('2026-01-04' AS datetime))", 1),
        ("DATEDIFF(hour, CAST('2026-01-01 00:59' AS datetime), "
         "CAST('2026-01-01 01:00' AS datetime))", 1),
        ("DATEDIFF(second, CAST('2026-01-01' AS datetime), "
         "CAST('2026-01-02' AS datetime))", 86400),
        ("DATEDIFF(day, CAST('2026-01-02' AS datetime), "
         "CAST('2026-01-01' AS datetime))", -1),
    ])
    def test_counting_between_two_moments(self, catalog, expression, expected):
        assert self.value(catalog, expression) == expected

    def test_a_count_that_does_not_fit_an_int_is_an_overflow(self, catalog):
        with pytest.raises(QueryError, match="datediff function resulted in an overflow"):
            rows(catalog, "SELECT DATEDIFF(second, CAST('1900-01-01' AS datetime), "
                          "CAST('2026-01-01' AS datetime)) AS v")

    def test_the_end_of_a_month(self, catalog):
        assert self.value(catalog, "EOMONTH(@)").date().isoformat() == "2026-09-30"
        assert self.value(catalog, "EOMONTH(@, 1)").date().isoformat() == "2026-10-31"
        assert self.value(catalog, "EOMONTH(@, -1)").date().isoformat() == "2026-08-31"
        assert one(catalog, "SELECT EOMONTH(CAST('2024-02-10' AS datetime)) AS v"
                   ).date().isoformat() == "2024-02-29"

    @pytest.mark.parametrize("expression", [
        "YEAR(NULL)", "DATEADD(day, 1, NULL)", "DATEDIFF(day, NULL, @)",
        "EOMONTH(NULL)", "DATEPART(year, NULL)", "DATENAME(month, NULL)",
    ])
    def test_null_in_is_null_out(self, catalog, expression):
        assert self.value(catalog, expression) is None

    def test_the_word_null_has_no_type_to_count_in(self, catalog):
        # A complaint about the type of an untyped literal, not about a
        # value, so it is the written word that is refused.
        with pytest.raises(QueryError, match="invalid for argument 2"):
            rows(catalog, f"SELECT DATEADD(day, NULL, {self.MOMENT}) AS v")

    def test_but_a_null_that_has_a_type_gives_null(self, catalog):
        # Measured: CAST(NULL AS int) is fine and so is a column holding one,
        # and both give NULL rather than the error above.
        assert self.value(catalog, "DATEADD(day, CAST(NULL AS int), @)") is None
        found = rows(catalog, f"SELECT DATEADD(day, CAST(score AS int), "
                              f"{self.MOMENT}) AS v FROM people WHERE score IS NULL")
        assert found == [[None]]

    def test_a_part_that_is_not_one_says_so(self, catalog):
        with pytest.raises(QueryError, match="not a recognized datepart option"):
            rows(catalog, f"SELECT DATEPART(fortnight, {self.MOMENT}) AS v")

    def test_text_and_numbers_are_read_as_dates(self, catalog):
        assert one(catalog, "SELECT YEAR('2026-09-08') AS v") == 2026
        assert one(catalog, "SELECT DATEDIFF(day, '2026-09-08', '2026-09-10') AS v") == 2
        # A number is days counted from 1900, which is what CAST does too.
        assert one(catalog, "SELECT YEAR(0) AS v") == 1900

    def test_now_is_one_moment_for_the_whole_statement(self, catalog):
        # A real server evaluates GETDATE() once. Every row of one answer
        # holding a different one would be a filter that moved while it ran.
        found = rows(catalog, "SELECT DISTINCT GETDATE() AS moment FROM people")
        assert len(found) == 1

    def test_the_spellings_of_now_agree_with_each_other(self, catalog):
        assert one(catalog, "SELECT DATEDIFF(second, GETDATE(), CURRENT_TIMESTAMP) AS v") == 0
        assert one(catalog, "SELECT DATEDIFF(second, GETDATE(), SYSDATETIME()) AS v") == 0

    def test_a_column_of_dates_is_declared_as_dates(self, catalog):
        answer = catalog.answer(f"SELECT DATEADD(day, 1, {self.MOMENT}) AS v")
        assert answer.columns[0].type.__class__.__name__ == "DateTime"

    def test_a_filter_a_person_would_write(self, catalog):
        # The reason any of this is here.
        found = rows(catalog, "SELECT COUNT(*) AS n FROM people "
                              "WHERE GETDATE() > DATEADD(day, -7, GETDATE())")
        assert found == [[len(PEOPLE)]]


class TestAUnionOfDifferentTypes:
    """The one type a column gets when the branches do not agree on it.

    Measured against SQL Server 2025. Serving the first branch's type is the
    obvious thing to do and it is wrong in both directions: a union of id and
    score declared int and handed 10.5 back as 10, and a union of id and name
    declared int and then could not encode a name at all.
    """

    def kind(self, catalog, sql):
        return catalog.answer(sql).columns[0].type.__class__.__name__

    def test_a_float_beside_an_int_wins(self, catalog):
        sql = "SELECT id FROM people UNION ALL SELECT score FROM people"
        assert self.kind(catalog, sql) == "Float"
        assert 10.5 in [row[0] for row in rows(catalog, sql)]

    def test_and_wins_whichever_branch_it_is(self, catalog):
        sql = "SELECT score FROM people UNION ALL SELECT id FROM people"
        assert self.kind(catalog, sql) == "Float"

    def test_every_value_is_the_type_that_won(self, catalog):
        sql = "SELECT id FROM people UNION ALL SELECT score FROM people"
        held = [row[0] for row in rows(catalog, sql) if row[0] is not None]
        assert all(isinstance(value, float) for value in held)

    def test_an_int_beside_text_wins_and_the_text_is_converted(self, catalog):
        sql = "SELECT id FROM people UNION ALL SELECT '7'"
        assert self.kind(catalog, sql) == "Integer"
        assert 7 in [row[0] for row in rows(catalog, sql)]

    def test_text_that_is_not_a_number_is_refused(self, catalog):
        # int outranks nvarchar, so the names are what has to convert, and
        # the message is SQL Server's own: a person searching for it should
        # find the documentation for it.
        with pytest.raises(QueryError, match="Conversion failed"):
            rows(catalog, "SELECT id FROM people UNION ALL SELECT name FROM people")

    def test_the_refusal_carries_the_number_a_client_expects(self, catalog):
        with pytest.raises(QueryError) as refused:
            rows(catalog, "SELECT id FROM people UNION ALL SELECT name FROM people")
        assert refused.value.number == 245

    def test_a_branch_with_no_rows_still_decides(self, catalog):
        # Nothing came out of it, and it is still an int column, so the names
        # still have to convert and still cannot. Deciding from the values
        # would call this one text and answer it.
        with pytest.raises(QueryError, match="Conversion failed"):
            rows(catalog, "SELECT id FROM people WHERE 1 = 0 "
                          "UNION ALL SELECT name FROM people")

    def test_converting_happens_before_repeats_are_dropped(self, catalog):
        # 1 and '1' are one row, not two, because by the time UNION compares
        # them they are both the number.
        both = rows(catalog, "SELECT id FROM people UNION SELECT '1'")
        assert len(both) == len(PEOPLE)

    def test_three_branches_take_the_highest(self, catalog):
        sql = ("SELECT id FROM people UNION ALL SELECT '7' "
               "UNION ALL SELECT score FROM people")
        assert self.kind(catalog, sql) == "Float"

    def test_a_branch_of_nulls_decides_nothing(self, catalog):
        sql = "SELECT NULL AS v FROM people UNION ALL SELECT name FROM people"
        assert self.kind(catalog, sql) == "NVarChar"

    def test_branches_that_agree_are_left_alone(self, catalog):
        # The ordinary union, which must behave exactly as it did before any
        # of this: one type throughout, and nothing converted.
        sql = "SELECT id FROM people UNION ALL SELECT id FROM people"
        assert self.kind(catalog, sql) == "Integer"
        assert all(isinstance(row[0], int) for row in rows(catalog, sql))

    def test_text_is_widened_to_hold_what_it_was_given(self, catalog):
        # score written out is longer than any name, and the column has to be
        # declared wide enough for it or the value arrives cut short.
        answer = catalog.answer(
            "SELECT name FROM people UNION ALL "
            "SELECT CAST(score AS nvarchar(30)) FROM people"
        )
        longest = max(len(row[0]) for row in answer.rows if row[0] is not None)
        assert answer.columns[0].type.max_chars >= longest


class TestTextReadAsANumber:
    """When text becomes a number, and when it refuses to.

    Measured against SQL Server 2025, because none of it is guessable. Blank
    text is zero. Text spelling a whole number becomes one, with a sign and
    surrounding space allowed. Anything else is refused for an integer, even
    though it names a number a person would round: '2.0' is an error where
    the number 2.0 is 2. The float rule is the loose one, and reads
    everything Python's own float() reads.
    """

    def test_blank_text_is_zero(self, catalog):
        assert one(catalog, "SELECT CAST('' AS int) AS v") == 0
        assert one(catalog, "SELECT CAST('   ' AS int) AS v") == 0
        assert one(catalog, "SELECT CAST('' AS float) AS v") == 0

    def test_blank_text_is_zero_in_arithmetic_too(self, catalog):
        # The same rule, which is why '' + 1 is 1 rather than an error.
        assert one(catalog, "SELECT '' + 1 AS v") == 1

    def test_a_whole_number_in_text_converts(self, catalog):
        assert one(catalog, "SELECT CAST('42' AS int) AS v") == 42
        assert one(catalog, "SELECT CAST(' -2 ' AS int) AS v") == -2
        assert one(catalog, "SELECT CAST('+2' AS int) AS v") == 2

    @pytest.mark.parametrize("text", ["2.0", "2.9", "2e2", "1,000", "0x10", "ada"])
    def test_anything_else_is_not_a_whole_number(self, catalog, text):
        with pytest.raises(QueryError):
            rows(catalog, f"SELECT CAST('{text}' AS int) AS v")

    def test_a_number_truncates_toward_zero(self, catalog):
        # The split worth remembering: the number 2.9 casts to 2, and the
        # text '2.9' is refused. A cast is not a rounding instruction, and
        # text is not a number until it spells one.
        assert one(catalog, "SELECT CAST(CAST(2.9 AS float) AS int) AS v") == 2
        assert one(catalog, "SELECT CAST(CAST(-2.9 AS float) AS int) AS v") == -2

    def test_a_float_reads_text_loosely(self, catalog):
        assert one(catalog, "SELECT CAST('2.5' AS float) AS v") == 2.5
        assert one(catalog, "SELECT CAST('2e2' AS float) AS v") == 200.0


class TestConvertingWithoutFailing:
    """TRY_CAST and TRY_CONVERT, which answer NULL where CAST refuses.

    The reason they matter here more than on a real server: a source read
    off a CSV or an API holds whatever it holds, and one value that will not
    convert should not cost the whole answer.
    """

    def test_a_value_that_converts_is_unchanged(self, catalog):
        assert one(catalog, "SELECT TRY_CAST('12' AS int) AS v") == 12
        assert one(catalog, "SELECT TRY_CONVERT(int, '12') AS v") == 12

    @pytest.mark.parametrize("expression", [
        "TRY_CAST('x' AS int)", "TRY_CAST('2.0' AS int)",
        "TRY_CAST('nope' AS datetime)", "TRY_CONVERT(int, 'x')",
        "TRY_CAST(NULL AS int)",
    ])
    def test_a_value_that_does_not_is_null(self, catalog, expression):
        assert one(catalog, f"SELECT {expression} AS v") is None

    def test_the_same_conversion_otherwise(self, catalog):
        # Blank text is still zero, and text too long for its size is still
        # truncated, because shortening text is what a sized cast is for and
        # is not a failure. A number that will not fit is a failure.
        assert one(catalog, "SELECT TRY_CAST('' AS int) AS v") == 0
        assert one(catalog, "SELECT TRY_CAST('abcdef' AS nvarchar(3)) AS v") == "abc"
        assert one(catalog, "SELECT TRY_CAST(123456 AS nvarchar(3)) AS v") is None

    def test_the_column_is_declared_by_the_type_asked_for(self, catalog):
        answer = catalog.answer("SELECT TRY_CAST('x' AS int) AS v")
        assert answer.columns[0].type.__class__.__name__ == "Integer"

    def test_it_keeps_the_rows_a_cast_would_cost(self, catalog):
        # The whole point: every name refuses to be an int, and the query
        # still answers.
        found = rows(catalog, "SELECT COUNT(*) AS n FROM people "
                              "WHERE TRY_CAST(name AS int) IS NULL")
        assert found == [[len(PEOPLE)]]
        with pytest.raises(QueryError):
            rows(catalog, "SELECT CAST(name AS int) AS v FROM people")


class TestAnIntegerCastIsHeldToItsRange:
    """CAST(300 AS tinyint) is an error, not 300.

    Every integer type is served as an integer here, which made the type a
    cast named decoration. It is not: it says what the value has to fit in,
    and a value that does not fit reaches the wire as something the column
    cannot encode. All five wordings measured against SQL Server 2025.
    """

    @pytest.mark.parametrize("expression, expected", [
        ("CAST(255 AS tinyint)", 255),
        ("CAST(0 AS tinyint)", 0),
        ("CAST(2.9 AS tinyint)", 2),
        ("CAST(3000000000 AS bigint)", 3000000000),
        ("CAST(-32768 AS smallint)", -32768),
    ])
    def test_a_value_that_fits(self, catalog, expression, expected):
        assert one(catalog, f"SELECT {expression} AS v") == expected

    @pytest.mark.parametrize("expression, said", [
        # A number names its type and quotes itself; an int gets the plain
        # arithmetic wording; text names the encoding or the column.
        ("CAST(300 AS tinyint)", "for data type tinyint, value = 300"),
        ("CAST(-1 AS tinyint)", "for data type tinyint, value = -1"),
        ("CAST(99999 AS smallint)", "for data type smallint, value = 99999"),
        ("CAST(3000000000 AS int)", "converting expression to data type int"),
        ("CAST('300' AS tinyint)", "overflowed an INT1 column"),
        ("CAST('99999' AS smallint)", "overflowed an INT2 column"),
        ("CAST('3000000000' AS int)", "overflowed an int column"),
    ])
    def test_a_value_that_does_not(self, catalog, expression, said):
        with pytest.raises(QueryError, match=said):
            rows(catalog, f"SELECT {expression} AS v")

    def test_and_the_try_form_answers_null_for_all_of_them(self, catalog):
        assert one(catalog, "SELECT TRY_CAST(300 AS tinyint) AS v") is None
        assert one(catalog, "SELECT TRY_CAST(3000000000 AS int) AS v") is None
        assert one(catalog, "SELECT TRY_CAST('99999999999999999999' AS int) AS v") is None


class TestTheStyleConvertWritesAMomentIn:
    """CONVERT's third argument, which was read and thrown away.

    A report formats a date with it, and throwing it away did not refuse:
    it answered in the default style and cut the result to the column,
    so CONVERT(nvarchar(10), d, 101) came back 'Sep  8 202'. Every style
    below measured against SQL Server 2025.
    """

    MOMENT = "CAST('2026-09-08T14:35:47.123' AS datetime)"

    def written(self, catalog, style):
        return one(catalog, f"SELECT CONVERT(nvarchar(50), {self.MOMENT}, "
                            f"{style}) AS v")

    @pytest.mark.parametrize("style, expected", [
        (0, "Sep  8 2026  2:35PM"), (100, "Sep  8 2026  2:35PM"),
        (1, "09/08/26"), (101, "09/08/2026"),
        (2, "26.09.08"), (102, "2026.09.08"),
        (3, "08/09/26"), (103, "08/09/2026"),
        (4, "08.09.26"), (104, "08.09.2026"),
        (5, "08-09-26"), (105, "08-09-2026"),
        (6, "08 Sep 26"), (106, "08 Sep 2026"),
        (7, "Sep 08, 26"), (107, "Sep 08, 2026"),
        (8, "14:35:47"), (108, "14:35:47"), (24, "14:35:47"),
        (9, "Sep  8 2026  2:35:47:123PM"),
        (109, "Sep  8 2026  2:35:47:123PM"),
        (10, "09-08-26"), (110, "09-08-2026"),
        (11, "26/09/08"), (111, "2026/09/08"),
        (12, "260908"), (112, "20260908"),
        (13, "08 Sep 2026 14:35:47:123"),
        (113, "08 Sep 2026 14:35:47:123"),
        (14, "14:35:47:123"), (114, "14:35:47:123"),
        (20, "2026-09-08 14:35:47"), (120, "2026-09-08 14:35:47"),
        (21, "2026-09-08 14:35:47.123"), (25, "2026-09-08 14:35:47.123"),
        (121, "2026-09-08 14:35:47.123"),
        (22, "09/08/26  2:35:47 PM"),
        (23, "2026-09-08"),
        (126, "2026-09-08T14:35:47.123"),
        (127, "2026-09-08T14:35:47.123"),
    ])
    def test_each_style(self, catalog, style, expected):
        assert self.written(catalog, style) == expected

    def test_the_same_shape_takes_a_short_year_below_a_hundred(self, catalog):
        # 6 and 106 are the same shape with a different year, which is the
        # rule the whole table is built on.
        assert self.written(catalog, 6) == "08 Sep 26"
        assert self.written(catalog, 106) == "08 Sep 2026"

    def test_no_style_is_the_default_one(self, catalog):
        assert one(catalog, f"SELECT CONVERT(nvarchar(50), {self.MOMENT}) AS v"
                   ) == self.written(catalog, 0)

    def test_the_size_still_cuts_it(self, catalog):
        assert one(catalog, f"SELECT CONVERT(nvarchar(5), {self.MOMENT}, 101) "
                            f"AS v") == "09/08"

    def test_a_style_on_something_that_is_not_a_moment(self, catalog):
        assert one(catalog, "SELECT CONVERT(nvarchar(10), 12345, 1) AS v") == "12345"

    def test_a_number_that_is_not_a_style(self, catalog):
        with pytest.raises(QueryError) as refused:
            rows(catalog, f"SELECT CONVERT(nvarchar(50), {self.MOMENT}, 999) AS v")
        assert refused.value.number == 281


class TestCastSize:
    """A cast says how wide, and that is part of what it means.

    Measured against SQL Server 2025. The two ways a value can fail to fit
    are different, and both matter: text is truncated quietly, which is what
    makes a cast a way of shortening a column, and a number that will not fit
    is an overflow, because nobody asks for the first three digits of a
    number by casting it.
    """

    def test_text_is_truncated(self, catalog):
        assert one(catalog, "SELECT CAST('abcdef' AS nvarchar(3)) AS s") == "abc"

    def test_text_that_fits_is_untouched(self, catalog):
        assert one(catalog, "SELECT CAST('abc' AS nvarchar(3)) AS s") == "abc"

    def test_a_number_that_does_not_fit_is_an_overflow(self, catalog):
        with pytest.raises(QueryError, match="arithmetic overflow"):
            rows(catalog, "SELECT CAST(123456 AS nvarchar(3)) AS s")

    def test_no_size_means_thirty(self, catalog):
        # Easy to hit by accident: a 50 character name cast to nvarchar comes
        # back with 30 of it.
        assert one(catalog, "SELECT CAST('" + "abcdefghij" * 5 + "' AS nvarchar) AS s")             == "abcdefghij" * 3

    def test_a_char_is_padded_to_its_width(self, catalog):
        assert one(catalog, "SELECT CAST('ab' AS nchar(5)) + '|' AS s") == "ab   |"

    def test_convert_says_it_the_other_way_round(self, catalog):
        assert one(catalog, "SELECT CONVERT(nvarchar(3), 'abcdef') AS s") == "abc"

    def test_a_cast_to_text_stays_text(self, catalog):
        # The values read as numbers, and inferring the column from them
        # would undo the cast the query asked for.
        answer = catalog.answer("SELECT CAST(id AS nvarchar(10)) AS s FROM people")
        assert answer.columns[0].type.__class__.__name__ == "NVarChar"

    def test_a_function_returning_text_stays_text(self, catalog):
        answer = catalog.answer("SELECT LEFT(12345, 2) AS s FROM people")
        assert answer.columns[0].type.__class__.__name__ == "NVarChar"

    def test_floor_and_ceiling_keep_the_type_they_were_given(self, catalog):
        # SQL Server declares FLOOR(a float) as float and answers 10.0.
        floats = catalog.answer("SELECT FLOOR(score) AS n FROM people")
        assert floats.columns[0].type.__class__.__name__ == "Float"
        wholes = catalog.answer("SELECT FLOOR(id) AS n FROM people")
        assert wholes.columns[0].type.__class__.__name__ == "Integer"


class TestColumnsWithNoValues:
    """What a column is declared when every value in it came back NULL.

    The one case the values cannot decide. A real server declares
    CAST(NULL AS int) an int because the cast says so, and clients build
    their own model from what the server declares, so a column of NULLs
    arriving as text is a difference a Power BI model would keep.

    Found by comparing the declared type of all 292 differential queries
    against SQL Server 2025, which is now part of that comparison.
    """

    def kind(self, catalog, sql):
        return catalog.answer(sql).columns[0].type.__class__.__name__

    def test_arithmetic_beside_a_null(self, catalog):
        assert self.kind(catalog, "SELECT 1 + NULL AS v") == "Integer"

    def test_a_function_that_has_one_type(self, catalog):
        assert self.kind(catalog, "SELECT LEN(NULL) AS v") == "Integer"

    def test_a_cast_says_its_own(self, catalog):
        assert self.kind(catalog, "SELECT CAST(NULL AS int) AS v") == "Integer"
        assert self.kind(catalog, "SELECT CAST(NULL AS nvarchar(10)) AS v") == "NVarChar"

    def test_a_function_that_takes_its_argument_type(self, catalog):
        assert self.kind(catalog, "SELECT NULLIF(1, 1) AS v") == "Integer"
        assert self.kind(catalog, "SELECT ISNULL(NULL, 1) AS v") == "Integer"

    def test_a_case_where_one_branch_is_null(self, catalog):
        assert self.kind(catalog, "SELECT CASE WHEN 1 = 1 THEN NULL ELSE 2 END AS v") \
            == "Integer"

    def test_a_subquery_that_matched_nothing(self, catalog):
        # It said what it was when it was answered, even with no rows.
        assert self.kind(catalog, "SELECT (SELECT id FROM tasks WHERE person_id = 999) "
                                  "AS v") == "Integer"

    def test_an_expression_over_a_table_with_no_rows(self, catalog):
        # Nothing survived the WHERE, so there is not even a NULL to read.
        # score is a float column and score * 2 is a float either way.
        assert self.kind(catalog, "SELECT score * 2 AS v FROM people WHERE 1 = 0") \
            == "Float"
        assert self.kind(catalog, "SELECT UPPER(name) AS v FROM people WHERE 1 = 0") \
            == "NVarChar"

    def test_a_qualified_column_reaches_the_same_answer(self, catalog):
        assert self.kind(catalog, "SELECT p.score + 1 AS v FROM people p WHERE 1 = 0") \
            == "Float"

    def test_a_nothing_that_says_nothing_stays_text(self, catalog):
        assert self.kind(catalog, "SELECT NULL AS v") == "NVarChar"


class TestCombining:
    """UNION, UNION ALL, EXCEPT and INTERSECT, measured against SQL Server.

    Three things belong to the statement rather than to any one part, and
    SQL Server writes all three at the end: the ORDER BY, the OFFSET and the
    FETCH. Column names come from the first part.
    """

    def test_union_drops_repeats(self, catalog):
        both = rows(catalog, "SELECT team FROM people UNION SELECT state FROM tasks "
                             "ORDER BY 1")
        expected = {p["team"].lower() for p in PEOPLE}
        expected |= {t["state"].lower() for t in TASKS}
        assert [str(r[0]).lower() for r in both] == sorted(expected)

    def test_union_all_keeps_them(self, catalog):
        both = rows(catalog, "SELECT team FROM people UNION ALL SELECT state FROM tasks")
        assert len(both) == len(PEOPLE) + len(TASKS)

    def test_the_names_come_from_the_first_part(self, catalog):
        answer = catalog.answer("SELECT team AS grouping FROM people "
                                "UNION SELECT state FROM tasks")
        assert [c.name for c in answer.columns] == ["grouping"]

    def test_a_repeat_differing_only_in_case_is_one_row(self, catalog):
        # The collation is case-insensitive, so it decides this too. Which of
        # the two spellings survives is not defined by either server; this
        # keeps the one that appeared first.
        both = rows(catalog, "SELECT 'RED' AS team UNION SELECT team FROM people")
        assert len(both) == len({p["team"].lower() for p in PEOPLE})

    def test_order_by_applies_to_the_whole(self, catalog):
        both = rows(catalog, "SELECT name FROM people UNION SELECT state FROM tasks "
                             "ORDER BY name")
        assert [r[0] for r in both] == sorted((r[0] for r in both), key=str.lower)

    def test_order_by_a_position_too(self, catalog):
        by_position = rows(catalog, "SELECT id, name FROM people "
                                    "UNION SELECT id, state FROM tasks ORDER BY 2, 1")
        by_name = rows(catalog, "SELECT id, name FROM people "
                                "UNION SELECT id, state FROM tasks ORDER BY name, id")
        assert by_position == by_name

    def test_offset_and_fetch_apply_to_the_whole(self, catalog):
        every = rows(catalog, "SELECT name FROM people UNION SELECT state FROM tasks "
                              "ORDER BY name")
        window = rows(catalog, "SELECT name FROM people UNION SELECT state FROM tasks "
                               "ORDER BY name OFFSET 1 ROWS FETCH NEXT 2 ROWS ONLY")
        assert window == every[1:3]

    def test_except_removes_what_the_other_side_has(self, catalog):
        left = rows(catalog, "SELECT team FROM people EXCEPT SELECT state FROM tasks")
        assert {str(r[0]).lower() for r in left} == (
            {p["team"].lower() for p in PEOPLE} - {t["state"].lower() for t in TASKS}
        )

    def test_intersect_keeps_only_what_both_have(self, catalog):
        shared = rows(catalog, "SELECT state FROM tasks INTERSECT SELECT state FROM tasks")
        assert {str(r[0]).lower() for r in shared} == {t["state"].lower() for t in TASKS}

    def test_three_parts_chain(self, catalog):
        three = rows(catalog, "SELECT team FROM people UNION SELECT state FROM tasks "
                              "UNION SELECT 'zed' ORDER BY 1")
        assert "zed" in [r[0] for r in three]

    def test_a_part_may_be_a_literal_with_no_table(self, catalog):
        assert rows(catalog, "SELECT 1 AS n UNION SELECT 2 ORDER BY n") == [[1], [2]]

    def test_a_mismatched_column_count_is_refused(self, catalog):
        with pytest.raises(QueryError, match="equal number of expressions"):
            rows(catalog, "SELECT id FROM people UNION SELECT id, name FROM people")

    def test_ordering_by_something_not_in_the_list_is_refused(self, catalog):
        with pytest.raises(QueryError, match="must appear in the select list"):
            rows(catalog, "SELECT name FROM people UNION SELECT state FROM tasks "
                          "ORDER BY id")

    def test_an_order_by_before_the_operator_is_refused(self, catalog):
        # There is one ORDER BY for the statement and it goes at the end.
        with pytest.raises(QueryError, match="belongs after the last UNION"):
            rows(catalog, "SELECT name FROM people ORDER BY name "
                          "UNION SELECT state FROM tasks")


class TestScalarSubqueries:
    """A SELECT standing where one value belongs, in every clause it can.

    Answered once, before the rows, and bound to a parameter: the expression
    parser never learns what a catalog is. That is also the limit, and it is
    a real one: a subquery that reads the outer row cannot be answered ahead
    of the rows, and is refused by the column it could not resolve.
    """

    def test_in_the_select_list_beside_a_column(self, catalog):
        found = rows(catalog, "SELECT name, (SELECT COUNT(*) FROM tasks) AS n "
                              "FROM people ORDER BY name")
        assert [r[1] for r in found] == [len(TASKS)] * len(PEOPLE)

    def test_on_its_own_with_no_table(self, catalog):
        assert one(catalog, "SELECT (SELECT COUNT(*) FROM tasks) AS n") == len(TASKS)

    def test_inside_an_expression(self, catalog):
        assert one(catalog, "SELECT (SELECT COUNT(*) FROM tasks) * 2 AS n")             == len(TASKS) * 2

    def test_two_of_them_in_one_entry(self, catalog):
        assert one(catalog, "SELECT (SELECT COUNT(*) FROM tasks) "
                            "- (SELECT COUNT(*) FROM people) AS d")             == len(TASKS) - len(PEOPLE)

    def test_beside_an_aggregate(self, catalog):
        # It reads no column, so there is nothing for a GROUP BY to decide.
        assert rows(catalog, "SELECT COUNT(*) AS c, (SELECT COUNT(*) FROM tasks) AS n "
                             "FROM people") == [[len(PEOPLE), len(TASKS)]]

    def test_beside_a_group(self, catalog):
        found = rows(catalog, "SELECT team, COUNT(*) AS c, "
                              "(SELECT COUNT(*) FROM tasks) AS n "
                              "FROM people GROUP BY team ORDER BY team")
        assert [r[2] for r in found] == [len(TASKS)] * len(set(p["team"] for p in PEOPLE))

    def test_a_literal_beside_an_aggregate_too(self, catalog):
        assert rows(catalog, "SELECT 1 AS one, COUNT(*) AS c FROM people")             == [[1, len(PEOPLE)]]

    def test_in_an_order_by(self, catalog):
        # Every row gets the same key, so the second key decides the order.
        found = rows(catalog, "SELECT name FROM people "
                              "ORDER BY (SELECT COUNT(*) FROM tasks), name")
        assert [r[0] for r in found] == sorted(
            (p["name"] for p in PEOPLE), key=str.lower
        )

    def test_in_the_select_list_the_where_and_the_order_by_at_once(self, catalog):
        # Three subqueries in one statement, each numbered apart from the rest.
        found = rows(catalog, "SELECT (SELECT COUNT(*) FROM tasks) AS n FROM people "
                              "WHERE id IN (SELECT person_id FROM tasks) "
                              "ORDER BY (SELECT MIN(id) FROM tasks), id")
        owners = {t["person_id"] for t in TASKS}
        assert found == [[len(TASKS)]] * len([p for p in PEOPLE if p["id"] in owners])

    def test_matching_nothing_is_null(self, catalog):
        assert one(catalog, "SELECT (SELECT id FROM tasks WHERE person_id = 99) AS n") is None

    def test_more_than_one_row_is_refused(self, catalog):
        with pytest.raises(QueryError, match="returned"):
            rows(catalog, "SELECT (SELECT id FROM tasks) AS n")

    def test_more_than_one_column_is_refused(self, catalog):
        with pytest.raises(QueryError, match="Only one expression can be specified"):
            rows(catalog, "SELECT (SELECT id, state FROM tasks) AS n")

    def test_one_that_reads_the_outer_row_is_answered_per_row(self, catalog):
        found = rows(catalog, "SELECT p.name, (SELECT COUNT(*) FROM tasks t "
                              "WHERE t.person_id = p.id) AS n "
                              "FROM people p ORDER BY p.id")
        counted = Counter(t["person_id"] for t in TASKS)
        assert found == [[p["name"], counted.get(p["id"], 0)] for p in PEOPLE]

    def test_the_outer_table_may_be_named_rather_than_aliased(self, catalog):
        found = rows(catalog, "SELECT name, (SELECT COUNT(*) FROM tasks "
                              "WHERE person_id = people.id) AS n "
                              "FROM people ORDER BY id")
        counted = Counter(t["person_id"] for t in TASKS)
        assert [r[1] for r in found] == [counted.get(p["id"], 0) for p in PEOPLE]

    def test_one_that_matches_nothing_for_a_row_is_null_there(self, catalog):
        found = rows(catalog, "SELECT p.id, (SELECT MAX(t.id) FROM tasks t "
                              "WHERE t.person_id = p.id) AS n "
                              "FROM people p ORDER BY p.id")
        owners = {t["person_id"] for t in TASKS}
        assert [r[1] is None for r in found] == [p["id"] not in owners for p in PEOPLE]

    def test_a_correlated_exists_filters(self, catalog):
        found = rows(catalog, "SELECT name FROM people p WHERE EXISTS "
                              "(SELECT 1 FROM tasks t WHERE t.person_id = p.id) "
                              "ORDER BY p.id")
        owners = {t["person_id"] for t in TASKS}
        assert [r[0] for r in found] == [
            p["name"] for p in PEOPLE if p["id"] in owners
        ]

    def test_and_not_exists_keeps_the_rest(self, catalog):
        found = rows(catalog, "SELECT name FROM people p WHERE NOT EXISTS "
                              "(SELECT 1 FROM tasks t WHERE t.person_id = p.id) "
                              "ORDER BY p.id")
        owners = {t["person_id"] for t in TASKS}
        assert [r[0] for r in found] == [
            p["name"] for p in PEOPLE if p["id"] not in owners
        ]

    def test_a_correlated_comparison_filters(self, catalog):
        found = rows(catalog, "SELECT name FROM people p WHERE "
                              "(SELECT COUNT(*) FROM tasks t WHERE t.person_id = p.id) "
                              "> 1 ORDER BY p.id")
        counted = Counter(t["person_id"] for t in TASKS)
        assert [r[0] for r in found] == [
            p["name"] for p in PEOPLE if counted.get(p["id"], 0) > 1
        ]

    def test_a_correlated_in_filters(self, catalog):
        found = rows(catalog, "SELECT name FROM people p WHERE p.id IN "
                              "(SELECT t.person_id FROM tasks t WHERE t.id > p.id) "
                              "ORDER BY p.id")
        wanted = [
            p["name"] for p in PEOPLE
            if p["id"] in {t["person_id"] for t in TASKS if t["id"] > p["id"]}
        ]
        assert [r[0] for r in found] == wanted

    def test_one_may_sort_the_rows(self, catalog):
        found = rows(catalog, "SELECT p.name FROM people p ORDER BY "
                              "(SELECT COUNT(*) FROM tasks t WHERE t.person_id = p.id) "
                              "DESC, p.id")
        counted = Counter(t["person_id"] for t in TASKS)
        assert [r[0] for r in found] == [
            p["name"] for p in sorted(
                PEOPLE, key=lambda one: (-counted.get(one["id"], 0), one["id"])
            )
        ]

    def test_one_beside_an_aggregate_is_refused(self, catalog):
        # It changes per row, and a group is many rows.
        with pytest.raises(QueryError, match="changes from row to row"):
            rows(catalog, "SELECT COUNT(*) AS c, (SELECT COUNT(*) FROM tasks t "
                          "WHERE t.person_id = p.id) AS n FROM people p")

    def test_it_is_answered_once_for_each_value_it_is_asked_about(self, catalog):
        # Three people share two distinct team values, so a subquery
        # correlated on team runs twice rather than three times.
        counted = {"calls": 0}
        original = catalog.answer

        def counting(request, **kwargs):
            if kwargs.get("select") is not None:
                counted["calls"] += 1
            return original(request, **kwargs)

        catalog.answer = counting
        try:
            rows(catalog, "SELECT p.name, (SELECT COUNT(*) FROM tasks t "
                          "WHERE t.state = p.team) AS n FROM people p")
        finally:
            catalog.answer = original
        assert counted["calls"] == len({p["team"] for p in PEOPLE})

    def test_a_subquery_may_still_qualify_its_own_table(self, catalog):
        assert one(catalog, "SELECT (SELECT COUNT(*) FROM tasks "
                            "WHERE tasks.person_id = 1) AS n") == len(
            [t for t in TASKS if t["person_id"] == 1]
        )

    def test_and_its_own_alias(self, catalog):
        assert one(catalog, "SELECT (SELECT COUNT(*) FROM tasks t "
                            "WHERE t.person_id = 1) AS n") == len(
            [t for t in TASKS if t["person_id"] == 1]
        )


class TestOrderByResolution:
    """What an ORDER BY item may name, measured against SQL Server 2025.

    Every case below was run against a real server first and this project
    made to agree with it. The interesting ones are the two that are refused:
    an alias is usable as the whole item and not as an operand inside one,
    and an item that is the same for every row is an error rather than a sort
    by nothing.
    """

    def test_an_alias_may_be_sorted_by(self, catalog):
        # Under the declared collation, which is case-insensitive.
        found = rows(catalog, "SELECT name AS who FROM people ORDER BY who")
        assert [r[0] for r in found] == sorted(
            (p["name"] for p in PEOPLE), key=str.lower
        )

    def test_an_alias_over_an_expression_may_be_sorted_by(self, catalog):
        found = rows(catalog, "SELECT UPPER(name) AS s FROM people ORDER BY s")
        assert [r[0] for r in found] == sorted(p["name"].upper() for p in PEOPLE)

    def test_an_expression_the_select_list_does_not_have(self, catalog):
        found = rows(catalog, "SELECT name FROM people ORDER BY LEN(name), name")
        assert [r[0] for r in found] == sorted(
            (p["name"] for p in PEOPLE), key=lambda n: (len(n), n)
        )

    def test_an_alias_wins_over_a_column_of_the_same_name(self, catalog):
        # SELECT team AS name sorts by team, not by the name column.
        found = rows(catalog, "SELECT team AS name FROM people ORDER BY name")
        assert [r[0] for r in found] == sorted(
            (p["team"] for p in PEOPLE), key=lambda t: (t is not None, t or "")
        )

    def test_a_number_is_a_position_in_the_select_list(self, catalog):
        by_position = rows(catalog, "SELECT name, team FROM people ORDER BY 2, 1")
        by_name = rows(catalog, "SELECT name, team FROM people ORDER BY team, name")
        assert by_position == by_name

    def test_a_position_past_the_select_list_is_refused(self, catalog):
        with pytest.raises(QueryError, match="position number 3 is out of range"):
            rows(catalog, "SELECT name, team FROM people ORDER BY 3")

    def test_an_alias_is_not_visible_inside_an_expression(self, catalog):
        with pytest.raises(QueryError, match="invalid column name 's'"):
            rows(catalog, "SELECT UPPER(name) AS s FROM people ORDER BY s + 'x'")

    def test_a_constant_is_refused(self, catalog):
        with pytest.raises(QueryError, match="constant expression"):
            rows(catalog, "SELECT name FROM people ORDER BY 1 + 1")

    def test_a_constant_string_is_refused(self, catalog):
        with pytest.raises(QueryError, match="constant expression"):
            rows(catalog, "SELECT name FROM people ORDER BY 'x'")

    def test_a_case_may_be_sorted_by(self, catalog):
        found = rows(
            catalog,
            "SELECT name FROM people "
            "ORDER BY CASE WHEN team = 'red' THEN 0 ELSE 1 END, name",
        )
        assert [r[0] for r in found] == sorted(
            (p["name"] for p in PEOPLE),
            key=lambda n: (0 if next(p for p in PEOPLE if p["name"] == n)["team"]
                           == "red" else 1, n.lower()),
        )

    def test_top_takes_the_first_rows_of_the_alias_sort(self, catalog):
        found = rows(catalog, "SELECT TOP 2 UPPER(name) AS s FROM people ORDER BY s DESC")
        assert [r[0] for r in found] == sorted(
            (p["name"].upper() for p in PEOPLE), reverse=True
        )[:2]

    def test_an_aggregate_may_be_written_out_in_a_grouped_sort(self, catalog):
        found = rows(
            catalog,
            "SELECT team, COUNT(*) AS n FROM people GROUP BY team "
            "ORDER BY COUNT(*) DESC, team",
        )
        counted = Counter(p["team"] for p in PEOPLE)
        assert found == sorted(
            ([team, n] for team, n in counted.items()),
            key=lambda pair: (-pair[1], (pair[0] or "").lower()),
        )

    def test_the_alias_of_that_aggregate_sorts_the_same_way(self, catalog):
        written = rows(
            catalog,
            "SELECT team, COUNT(*) AS n FROM people GROUP BY team "
            "ORDER BY COUNT(*) DESC, team",
        )
        aliased = rows(
            catalog,
            "SELECT team, COUNT(*) AS n FROM people GROUP BY team "
            "ORDER BY n DESC, team",
        )
        assert written == aliased

    def test_a_clause_that_cannot_be_served_is_still_named(self, catalog):
        # Reading the item as an expression must not swallow what follows it.
        with pytest.raises(QueryError, match="FOR is not supported"):
            rows(catalog, "SELECT name FROM people ORDER BY name FOR XML AUTO")


class TestMatchesSqlServer:
    """Behaviours measured against SQL Server 2025 rather than assumed.

    Each was found by running the same query against a real server and this
    one with the same rows, and each was wrong here before it was measured.
    scripts/differential.py runs that comparison; these are what it found.
    """

    def test_charindex_of_nothing_is_nowhere(self, catalog):
        # Python finds an empty string at the position it started looking.
        assert one(catalog, "SELECT CHARINDEX('', 'abc') AS n") == 0
        assert one(catalog, "SELECT CHARINDEX('', '') AS n") == 0
        assert one(catalog, "SELECT CHARINDEX('', 'abc', 2) AS n") == 0

    def test_replacing_nothing_changes_nothing(self, catalog):
        # Python replaces an empty string at every position: xaxbxcx.
        assert one(catalog, "SELECT REPLACE('abc', '', 'x') AS s") == "abc"

    def test_replacing_with_nothing_still_removes(self, catalog):
        assert one(catalog, "SELECT REPLACE('abc', 'b', '') AS s") == "ac"

    def test_substring_refuses_a_number(self, catalog):
        # Alone among the string functions: LEFT, LEN, UPPER and CHARINDEX
        # all take a number, and SUBSTRING is an error on one.
        with pytest.raises(QueryError, match="Argument data type int is invalid"):
            rows(catalog, "SELECT SUBSTRING(12345, 2, 2) AS s")

    def test_substring_of_a_cast_number_is_fine(self, catalog):
        # The value matches. The type does not: a computed column is typed
        # from the values it produced, so digits come back as an integer
        # where SQL Server declares the varchar its function returns.
        assert str(one(catalog, "SELECT SUBSTRING(CAST(12345 AS nvarchar(10)), 2, 2) AS s")) == "23"

    def test_the_other_string_functions_still_take_a_number(self, catalog):
        assert str(one(catalog, "SELECT LEFT(12345, 2) AS s")) == "12"
        assert one(catalog, "SELECT LEN(12345) AS n") == 5
        assert one(catalog, "SELECT CHARINDEX('2', 12345) AS n") == 2

    def test_trailing_spaces_do_not_count_in_a_comparison(self, catalog):
        # SQL Server pads the shorter side, so 'a' = 'a  ' is true.
        assert one(catalog, "SELECT COUNT(*) FROM people WHERE name = 'ada  '") == 1

    def test_trailing_spaces_do_count_in_like(self, catalog):
        assert one(catalog, "SELECT COUNT(*) FROM people WHERE name LIKE 'ada '") == 0

    def test_text_beside_a_number_converts_to_a_number(self, catalog):
        # int outranks varchar, so the text is converted, not the number.
        assert one(catalog, "SELECT COUNT(*) FROM people WHERE id IN (1, '2')") == 2

    def test_min_and_max_on_text_use_the_collation(self, catalog):
        # By code point 'edsger' beats 'Grace'; under CI_AS it does not.
        assert one(catalog, "SELECT MAX(name) FROM people") == "Grace"

    def test_count_distinct(self, catalog):
        assert one(catalog, "SELECT COUNT(DISTINCT team) FROM people") == 3

    def test_count_distinct_uses_the_collation(self):
        c = Catalog()
        c.add(from_records([{"k": "a"}, {"k": "A"}, {"k": "b"}], name="t"))
        assert one(c, "SELECT COUNT(DISTINCT k) FROM t") == 2

    def test_an_aggregate_over_an_expression(self, catalog):
        want = sum(p["id"] + 1 for p in PEOPLE)
        assert one(catalog, "SELECT SUM(id + 1) FROM people") == want

    def test_count_of_an_expression_skips_nulls(self, catalog):
        present = sum(1 for p in PEOPLE if p["score"] is not None)
        assert one(catalog, "SELECT COUNT(score + 1) FROM people") == present

    def test_grouping_by_two_columns(self, catalog):
        got = rows(catalog, "SELECT team, id, COUNT(*) AS n FROM people "
                            "GROUP BY team, id ORDER BY team, id")
        assert len(got) == len(PEOPLE)

    def test_power_keeps_the_scale_of_what_it_raised(self, catalog):
        # SQL Server types 2.0 as decimal(2,1) and POWER returns that type.
        assert one(catalog, "SELECT POWER(2.0, 0.5) AS n") == 1.4

    def test_power_of_a_float_literal_does_not(self, catalog):
        assert round(one(catalog, "SELECT POWER(2.0E0, 0.5) AS n"), 6) == 1.414214

    def test_a_bracketed_name_inside_an_aggregate_is_unquoted(self, catalog):
        # A flattened column really is called team.name, and COUNT of it has
        # to look up that name rather than one with the brackets still on.
        c = Catalog()
        c.add(from_records([{"a.b": 1}, {"a.b": 2}, {"a.b": None}], name="t"))
        assert one(c, "SELECT COUNT([a.b]) FROM t") == 2

    def test_a_qualified_name_inside_an_aggregate_resolves(self, catalog):
        assert one(catalog, "SELECT SUM(p.id) FROM people p") == sum(
            row["id"] for row in PEOPLE
        )
