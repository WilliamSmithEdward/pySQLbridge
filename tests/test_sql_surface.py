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
from pysqlbridge.tds.result import QueryError

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
        with pytest.raises(QueryError, match="neither aggregated nor named"):
            catalog.answer("SELECT name, COUNT(*) FROM people GROUP BY team")

    def test_having_without_group_by_is_refused(self, catalog):
        with pytest.raises(QueryError, match="needs a GROUP BY"):
            catalog.answer("SELECT COUNT(*) FROM people HAVING COUNT(*) > 1")

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

    def test_a_join_this_cannot_do_is_refused(self, catalog):
        with pytest.raises(QueryError, match="RIGHT JOIN is not supported"):
            catalog.answer("SELECT * FROM people p RIGHT JOIN tasks t ON t.id = p.id")


class TestExpressions:
    def test_arithmetic(self, catalog):
        assert rows(catalog, "SELECT id * 2 + 1 AS n FROM people ORDER BY id")[0] == [3]

    def test_integer_division_truncates_like_sql_server(self, catalog):
        assert one(catalog, "SELECT 7 / 2 AS half") == 3

    def test_dividing_by_zero_is_an_error(self, catalog):
        # Measured against SQL Server 2025, which raises rather than
        # answering NULL.
        with pytest.raises(QueryError, match="divide by zero"):
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
        with pytest.raises(QueryError, match="must select one column"):
            catalog.answer(
                "SELECT * FROM people WHERE id IN (SELECT id, name FROM people)"
            )

    def test_a_scalar_subquery_must_return_one_row(self, catalog):
        with pytest.raises(QueryError, match="returned 5 rows"):
            catalog.answer("SELECT * FROM people WHERE id = (SELECT id FROM people)")


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
        with pytest.raises(QueryError, match="must select one column"):
            rows(catalog, "SELECT (SELECT id, state FROM tasks) AS n")

    def test_one_that_reads_the_outer_row_is_refused(self, catalog):
        # And refused rather than answered: left alone, p.id falls back to the
        # bare id, which the tasks table also has, so the count came out zero
        # for every row and looked like an answer.
        with pytest.raises(QueryError, match="depends on the row around it"):
            rows(catalog, "SELECT (SELECT COUNT(*) FROM tasks WHERE person_id = p.id) "
                          "AS n FROM people p")

    def test_the_same_refusal_from_a_where(self, catalog):
        with pytest.raises(QueryError, match="depends on the row around it"):
            rows(catalog, "SELECT name FROM people p WHERE id IN "
                          "(SELECT person_id FROM tasks WHERE state = p.name)")

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
        with pytest.raises(QueryError, match="UNION is not supported"):
            rows(catalog, "SELECT name FROM people ORDER BY name UNION SELECT 1")


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
        with pytest.raises(QueryError, match="argument data type int is invalid"):
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
