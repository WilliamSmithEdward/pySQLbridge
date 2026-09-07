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

    def test_dividing_by_zero_is_null_rather_than_a_dead_query(self, catalog):
        assert one(catalog, "SELECT 1 / 0 AS oops") is None

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
