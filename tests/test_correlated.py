"""A correlated EXISTS answered by reading the inner table once.

WHERE EXISTS (SELECT 1 FROM t WHERE t.owner = p.id) asks the same question
of every outer row: is this value among the owners. Running the subquery
once per distinct value costs a pass over the inner table each time, which
is a pass per outer row where the values are all different. Reading the
owners once and looking each value up took a query over two thousand rows
against two thousand from 5.2 seconds to 0.004.

The shortcut only fits some shapes, so every test here runs its query twice:
once as it stands and once with the shortcut turned off. The two have to
agree, whichever route the query would have taken, because a shortcut that
answers differently is worse than no shortcut at all.
"""

import time

import pytest

from pysqlbridge.catalog import Catalog
from pysqlbridge.source import from_records
from pysqlbridge.tds.result import Query

PEOPLE = [
    {"id": 1, "name": "ada", "team": "red", "rank": 3},
    {"id": 2, "name": "Grace", "team": "RED", "rank": 1},
    {"id": 3, "name": "alan", "team": "blue", "rank": None},
    {"id": 4, "name": "", "team": None, "rank": 2},
]

TASKS = [
    {"tid": 10, "owner": 1, "state": "open", "hours": 3},
    {"tid": 11, "owner": 1, "state": "done", "hours": None},
    {"tid": 12, "owner": 2, "state": "open", "hours": 2},
    {"tid": 13, "owner": 9, "state": "done", "hours": 1},
]

# Every shape the shortcut has to answer or decline. The comment on each says
# which it should be; the test only requires that both routes agree.
SHAPES = [
    # Taken.
    "SELECT id FROM people p WHERE EXISTS "
    "(SELECT 1 FROM tasks t WHERE t.owner = p.id) ORDER BY id",
    "SELECT id FROM people p WHERE EXISTS "
    "(SELECT 1 FROM tasks t WHERE p.id = t.owner) ORDER BY id",
    "SELECT id FROM people p WHERE NOT EXISTS "
    "(SELECT 1 FROM tasks t WHERE t.owner = p.id) ORDER BY id",
    "SELECT id FROM people p WHERE EXISTS "
    "(SELECT 1 FROM tasks t WHERE t.owner = p.id AND t.state = 'open') "
    "ORDER BY id",
    "SELECT id FROM people p WHERE EXISTS "
    "(SELECT 1 FROM tasks t WHERE t.state = 'open' AND t.owner = p.id) "
    "ORDER BY id",
    "SELECT id FROM people p WHERE EXISTS "
    "(SELECT 1 FROM tasks t WHERE t.owner = p.rank) ORDER BY id",
    "SELECT id FROM people p WHERE EXISTS "
    "(SELECT 1 FROM tasks t WHERE t.hours = p.rank) ORDER BY id",
    "SELECT id FROM people p WHERE EXISTS "
    "(SELECT 1 FROM tasks t WHERE t.state = p.name) ORDER BY id",
    "SELECT id FROM people p WHERE EXISTS "
    "(SELECT 1 FROM tasks t WHERE t.owner + 1 = p.id) ORDER BY id",
    "SELECT COUNT(*) AS n FROM people p WHERE EXISTS "
    "(SELECT 1 FROM tasks t WHERE t.owner = p.id)",
    "SELECT id FROM people p WHERE p.id > 1 AND EXISTS "
    "(SELECT 1 FROM tasks t WHERE t.owner = p.id) ORDER BY id",
    "SELECT id FROM people p WHERE EXISTS "
    "(SELECT 1 FROM tasks t WHERE t.owner = p.id) AND NOT EXISTS "
    "(SELECT 1 FROM tasks u WHERE u.owner = p.id AND u.state = 'done') "
    "ORDER BY id",
    "SELECT id, CASE WHEN EXISTS "
    "(SELECT 1 FROM tasks t WHERE t.owner = p.id) THEN 1 ELSE 0 END AS has "
    "FROM people p ORDER BY id",
    # Declined: an OR, a comparison that is not equality, a grouping or a TOP
    # that decides which rows there are after the filter has run, two columns
    # matched at once, and an outer expression rather than an outer column.
    "SELECT id FROM people p WHERE EXISTS "
    "(SELECT 1 FROM tasks t WHERE t.owner = p.id OR t.state = 'done') "
    "ORDER BY id",
    "SELECT id FROM people p WHERE EXISTS "
    "(SELECT 1 FROM tasks t WHERE t.owner > p.id) ORDER BY id",
    "SELECT id FROM people p WHERE EXISTS "
    "(SELECT t.owner FROM tasks t WHERE t.owner = p.id "
    "GROUP BY t.owner HAVING COUNT(*) > 1) ORDER BY id",
    "SELECT id FROM people p WHERE EXISTS "
    "(SELECT TOP 1 1 FROM tasks t WHERE t.owner = p.id) ORDER BY id",
    "SELECT id FROM people p WHERE EXISTS "
    "(SELECT 1 FROM tasks t WHERE t.owner = p.id AND t.hours = p.rank) "
    "ORDER BY id",
    "SELECT id FROM people p WHERE EXISTS "
    "(SELECT 1 FROM tasks t WHERE t.owner = p.id + 1) ORDER BY id",
    "SELECT id FROM people p WHERE EXISTS "
    "(SELECT 1 FROM tasks t WHERE t.state = 'open') ORDER BY id",
]


@pytest.fixture
def catalog() -> Catalog:
    c = Catalog()
    c.add(from_records(PEOPLE, name="people"))
    c.add(from_records(TASKS, name="tasks"))
    return c


def answered(catalog, sql):
    return [list(row) for row in catalog.answer(Query(sql=sql, session={})).rows]


class TestBothRoutesAgree:
    @pytest.mark.parametrize("sql", SHAPES)
    def test_the_shortcut_answers_what_a_row_at_a_time_answers(
            self, catalog, sql, monkeypatch):
        quick = answered(catalog, sql)
        monkeypatch.setattr(Catalog, "_matching_once",
                            lambda *rest, **named: None)
        assert answered(catalog, sql) == quick

    def test_at_least_one_shape_takes_each_route(self, catalog, monkeypatch):
        # Otherwise the test above would pass with the shortcut never firing,
        # which would say nothing about it.
        taken = []
        original = Catalog._matching_once

        def watched(self, *rest):
            found = original(self, *rest)
            taken.append(found is not None)
            return found

        monkeypatch.setattr(Catalog, "_matching_once", watched)
        for sql in SHAPES:
            answered(catalog, sql)
        assert any(taken), "the shortcut never fired"
        assert not all(taken), "nothing fell back to a row at a time"


class TestWhatItAnswers:
    def test_who_has_a_task(self, catalog):
        found = answered(catalog, "SELECT id FROM people p WHERE EXISTS "
                                  "(SELECT 1 FROM tasks t WHERE t.owner = p.id) "
                                  "ORDER BY id")
        assert found == [[1], [2]]

    def test_and_who_has_none(self, catalog):
        found = answered(catalog, "SELECT id FROM people p WHERE NOT EXISTS "
                                  "(SELECT 1 FROM tasks t WHERE t.owner = p.id) "
                                  "ORDER BY id")
        assert found == [[3], [4]]

    def test_a_null_on_the_outside_matches_nothing(self, catalog):
        # rank is NULL for person 3, and NULL = anything is never true.
        found = answered(catalog, "SELECT id FROM people p WHERE EXISTS "
                                  "(SELECT 1 FROM tasks t WHERE t.hours = p.rank)"
                                  " ORDER BY id")
        assert 3 not in [row[0] for row in found]

    def test_a_null_on_the_inside_matches_nothing_either(self, catalog):
        # Task 11 has NULL hours, so no rank finds it.
        held = [{"id": 1, "rank": None}]
        c = Catalog()
        c.add(from_records(held, name="people"))
        c.add(from_records(TASKS, name="tasks"))
        assert answered(c, "SELECT id FROM people p WHERE EXISTS "
                           "(SELECT 1 FROM tasks t WHERE t.hours = p.rank)") == []

    def test_text_matches_without_regard_to_case(self, catalog):
        held = [{"id": 1, "word": "OPEN"}]
        c = Catalog()
        c.add(from_records(held, name="people"))
        c.add(from_records(TASKS, name="tasks"))
        assert answered(c, "SELECT id FROM people p WHERE EXISTS "
                           "(SELECT 1 FROM tasks t WHERE t.state = p.word)") == [[1]]


class TestItCostsWhatItShould:
    """Reading the inner table once rather than once per outer row.

    Timed rather than counted: what was wrong was the cost. Two thousand
    rows against two thousand took 5.2 seconds and takes 0.004, and doubling
    the rows used to quadruple the time.
    """

    def catalog_of(self, rows: int) -> Catalog:
        c = Catalog()
        c.add(from_records([{"id": n} for n in range(rows)], name="many"))
        c.add(from_records([{"id": n, "owner": n % 500} for n in range(rows)],
                           name="side"))
        return c

    def timed(self, rows: int) -> tuple[float, list]:
        catalog = self.catalog_of(rows)
        sql = ("SELECT COUNT(*) AS n FROM many m WHERE EXISTS "
               "(SELECT 1 FROM side s WHERE s.owner = m.id)")
        started = time.perf_counter()
        found = catalog.answer(Query(sql=sql, session={}))
        return time.perf_counter() - started, found.rows

    def test_the_answer_is_right(self):
        _, found = self.timed(2000)
        assert found == [[500]]

    def test_and_two_thousand_rows_do_not_take_a_second(self):
        taken, _ = self.timed(2000)
        assert taken < 1.0, f"a correlated EXISTS over 2000 rows took {taken:.2f}s"

    def test_doubling_the_rows_does_not_quadruple_the_time(self):
        small = min(self.timed(1000)[0] for _ in range(2))
        large = min(self.timed(2000)[0] for _ in range(2))
        assert large < small * 3, (
            f"1000 rows took {small:.3f}s and 2000 took {large:.3f}s, which "
            f"is the shape of a pass over the inner table per outer row"
        )
