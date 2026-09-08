import pytest

from pysqlbridge.catalog import Catalog
from pysqlbridge.source import from_records
from pysqlbridge.tds.result import Float, Integer, NVarChar, Query, QueryError

ROWS = [
    {"name": "ada", "score": 99.5, "rank": 1, "note": None},
    {"name": "grace", "score": 87.25, "rank": 2, "note": None},
    {"name": "edsger", "score": 78.0, "rank": 3, "note": "x"},
    {"name": "barbara", "score": 93.75, "rank": 4, "note": None},
]


def answer(sql: str, rows=None):
    c = Catalog()
    c.add(from_records(rows if rows is not None else ROWS, name="people"))
    return c.answer(Query(sql=sql))


def one(sql: str):
    """The single value an aggregated query returns."""
    result = answer(sql)
    assert len(result.rows) == 1, "an aggregate without GROUP BY returns one row"
    return result.rows[0][0]


class TestCount:
    def test_count_star_counts_rows(self):
        assert one("SELECT COUNT(*) FROM people") == 4

    def test_count_of_a_column_counts_non_nulls(self):
        assert one("SELECT COUNT(note) FROM people") == 1

    def test_count_respects_the_where(self):
        assert one("SELECT COUNT(*) FROM people WHERE score > 90") == 2

    def test_count_of_nothing_is_zero_not_null(self):
        assert one("SELECT COUNT(*) FROM people WHERE score > 999") == 0

    def test_count_is_an_int_column(self):
        column = answer("SELECT COUNT(*) FROM people").columns[0]
        assert isinstance(column.type, Integer)


class TestMinMaxSumAvg:
    def test_min_and_max(self):
        result = answer("SELECT MIN(score) AS lo, MAX(score) AS hi FROM people")
        assert result.rows[0] == [78.0, 99.5]

    def test_sum(self):
        assert one("SELECT SUM(score) FROM people") == pytest.approx(358.5)

    def test_avg_of_a_float_column(self):
        assert one("SELECT AVG(score) FROM people") == pytest.approx(89.625)

    def test_avg_of_an_integer_column_truncates(self):
        # SQL Server does this and it surprises people every time. Matched
        # rather than improved, so a client gets the same number from both.
        assert one("SELECT AVG(rank) FROM people") == 2

    def test_they_ignore_nulls(self):
        rows = [{"v": 1}, {"v": None}, {"v": 3}]
        assert answer("SELECT SUM(v) AS s, COUNT(v) AS c FROM people", rows).rows[0] == [4, 2]

    def test_over_no_rows_they_are_null_not_zero(self):
        result = answer(
            "SELECT SUM(score) AS s, MIN(score) AS lo, AVG(score) AS a "
            "FROM people WHERE score > 999")
        assert result.rows[0] == [None, None, None]

    def test_min_and_max_work_on_text(self):
        result = answer("SELECT MIN(name) AS first, MAX(name) AS last FROM people")
        assert result.rows[0] == ["ada", "grace"]


class TestHowFarTheValuesAreSpread:
    """STDEV and VAR over a sample, STDEVP and VARP over the population.

    The expected numbers are worked out from the fixture rather than written
    down, so the test says what the aggregate means rather than what it
    happened to answer. Checked against SQL Server for 1.5, 2.5 and 3.5,
    which give a sample variance of 1 and a population variance of 2/3.
    """

    @property
    def scores(self):
        return [row["score"] for row in ROWS]

    def spread(self, values, whole):
        mean = sum(values) / len(values)
        squares = sum((v - mean) ** 2 for v in values)
        return squares / (len(values) if whole else len(values) - 1)

    def test_var_is_over_a_sample(self):
        assert one("SELECT VAR(score) FROM people") == pytest.approx(
            self.spread(self.scores, whole=False))

    def test_varp_is_over_the_whole_population(self):
        assert one("SELECT VARP(score) FROM people") == pytest.approx(
            self.spread(self.scores, whole=True))

    def test_stdev_is_the_root_of_var(self):
        assert one("SELECT STDEV(score) FROM people") == pytest.approx(
            self.spread(self.scores, whole=False) ** 0.5)

    def test_stdevp_is_the_root_of_varp(self):
        assert one("SELECT STDEVP(score) FROM people") == pytest.approx(
            self.spread(self.scores, whole=True) ** 0.5)

    def test_one_value_has_no_sample_spread(self):
        # Nothing to divide by. SQL Server answers NULL rather than failing.
        assert one("SELECT VAR(score) FROM people WHERE rank = 1") is None
        assert one("SELECT STDEV(score) FROM people WHERE rank = 1") is None

    def test_but_it_has_a_population_spread_of_nothing(self):
        assert one("SELECT VARP(score) FROM people WHERE rank = 1") == 0.0

    def test_no_values_at_all_is_null(self):
        assert one("SELECT VARP(score) FROM people WHERE rank = 99") is None

    def test_nulls_are_left_out_rather_than_counted_as_zero(self):
        held = [{"v": 1.0}, {"v": 3.0}, {"v": None}]
        assert answer("SELECT VARP(v) FROM people", held).rows[0][0] == 1.0

    def test_the_answer_is_a_float_however_the_column_was_declared(self):
        # rank is an integer column and its spread is not a whole number.
        assert isinstance(answer("SELECT VAR(rank) FROM people").columns[0].type,
                          Float)

    def test_text_is_refused_by_name(self):
        with pytest.raises(QueryError, match="STDEV"):
            answer("SELECT STDEV(name) FROM people")

    def test_each_group_is_spread_on_its_own(self):
        result = answer("SELECT note, VARP(score) FROM people "
                        "GROUP BY note ORDER BY note")
        assert len(result.rows) == 2


class TestAWideCount:
    """COUNT_BIG, which is COUNT in a bigint.

    Nothing here needs the width for a file or an API page. A client written
    against a real table asks for it anyway.
    """

    def test_it_counts_rows(self):
        assert one("SELECT COUNT_BIG(*) FROM people") == 4

    def test_and_non_nulls_of_a_column(self):
        assert one("SELECT COUNT_BIG(note) FROM people") == 1

    def test_and_distinct_values(self):
        assert one("SELECT COUNT_BIG(DISTINCT note) FROM people") == 1

    def test_it_is_declared_wider_than_count(self):
        wide = answer("SELECT COUNT_BIG(*) FROM people").columns[0].type
        narrow = answer("SELECT COUNT(*) FROM people").columns[0].type
        assert (wide.width, narrow.width) == (8, 4)

    def test_a_star_still_belongs_to_the_counts_alone(self):
        with pytest.raises(QueryError, match="SUM"):
            answer("SELECT SUM(*) FROM people")

    def test_and_distinct_is_not_something_a_star_can_be(self):
        with pytest.raises(QueryError, match="COUNT_BIG"):
            answer("SELECT COUNT_BIG(DISTINCT *) FROM people")


class TestAllWrittenOut:
    """ALL before an argument, which is what an aggregate does anyway.

    A person writes it beside a DISTINCT elsewhere in the same statement.
    """

    @pytest.mark.parametrize("sql", [
        "SELECT COUNT(ALL note) FROM people",
        "SELECT SUM(ALL rank) FROM people",
        "SELECT AVG(ALL score) FROM people",
        "SELECT MAX(ALL name) FROM people",
        "SELECT VAR(ALL score) FROM people",
    ])
    def test_it_answers_what_the_plain_form_answers(self, sql):
        assert one(sql) == one(sql.replace("ALL ", ""))

    def test_a_column_whose_name_begins_with_it_is_still_a_column(self):
        held = [{"all_time": 3}, {"all_time": 4}]
        assert answer("SELECT MAX(all_time) FROM people", held).rows == [[4]]

    def test_all_on_its_own_is_not_a_column(self):
        with pytest.raises(QueryError, match="ALL"):
            answer("SELECT COUNT(ALL) FROM people")


class TestResultTypes:
    def test_sum_of_an_integer_column_is_widened(self):
        # SQL Server keeps the width and raises on overflow, which here would
        # surface as an encoding failure partway through a result set rather
        # than as a SQL error.
        column = answer("SELECT SUM(rank) FROM people").columns[0]
        assert isinstance(column.type, Integer) and column.type.width == 8

    def test_a_float_aggregate_stays_a_float(self):
        assert isinstance(answer("SELECT SUM(score) FROM people").columns[0].type, Float)

    def test_min_of_text_stays_text(self):
        assert isinstance(answer("SELECT MIN(name) FROM people").columns[0].type, NVarChar)


class TestNaming:
    def test_an_aliased_aggregate_uses_its_alias(self):
        assert answer("SELECT COUNT(*) AS n FROM people").columns[0].name == "n"

    def test_an_unaliased_aggregate_has_no_name(self):
        # SQL Server leaves it unnamed and clients render a blank heading.
        assert answer("SELECT COUNT(*) FROM people").columns[0].name == ""

    def test_a_plain_column_can_be_aliased(self):
        result = answer("SELECT name AS who FROM people")
        assert [c.name for c in result.columns] == ["who"]

    def test_a_bare_alias_works_too(self):
        assert answer("SELECT name who FROM people").columns[0].name == "who"

    def test_an_unaliased_column_keeps_its_own_name(self):
        assert answer("SELECT name FROM people").columns[0].name == "name"


class TestErrors:
    def test_summing_text_says_why(self):
        with pytest.raises(QueryError, match="needs a numeric column"):
            answer("SELECT SUM(name) FROM people")

    def test_an_unknown_column_names_itself_and_the_function(self):
        with pytest.raises(QueryError, match=r"invalid column name 'nope' in MAX\(\)"):
            answer("SELECT MAX(nope) FROM people")

    def test_mixing_an_aggregate_with_a_bare_column_is_refused(self):
        # Guessing the grouping would answer a question nobody asked.
        with pytest.raises(QueryError, match="is invalid in the select list"):
            answer("SELECT COUNT(*), name FROM people")

    def test_a_function_this_does_not_have_says_so(self):
        with pytest.raises(QueryError, match="not a function"):
            answer("SELECT NOSUCHTHING(name) FROM people")

    def test_star_is_only_valid_for_count(self):
        with pytest.raises(QueryError, match=r"SUM\(\*\) is not a thing"):
            answer("SELECT SUM(*) FROM people")
