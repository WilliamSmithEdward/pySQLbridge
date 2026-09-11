import datetime
import itertools

import pytest

from pysqlbridge.predicate import (
    PredicateError,
    Unknown,
    _as_datetime,
    _Candidates,
    _days_since_1900,
    _rounded_away,
    compare,
    matches,
    parse_expression,
    parse_predicate,
    result_kind,
)
from pysqlbridge.source import SourceError

ROW = {"id": 1, "name": "ada", "score": 99.5, "note": None}


def check(expr: str, params: dict | None = None, row: dict | None = None) -> bool:
    return matches(parse_predicate(expr), row or ROW, params or {})


def value(expr: str, params: dict | None = None):
    """The raw ternary, so unknown can be told apart from false."""
    return parse_predicate(expr).evaluate(ROW, params or {})


class TestComparisons:
    @pytest.mark.parametrize("expr,expected", [
        ("id = 1", True), ("id = 2", False),
        ("id <> 2", True), ("id != 1", False),
        ("id > 0", True), ("id >= 1", True), ("id < 1", False), ("id <= 1", True),
        ("name = 'ada'", True), ("name = 'grace'", False),
        ("score = 99.5", True),
    ])
    def test_operators(self, expr, expected):
        assert check(expr) is expected

    def test_a_doubled_quote_is_an_escaped_quote(self):
        assert check("name = 'ada''s'", row={"name": "ada's"})

    def test_unicode_string_literals(self):
        assert check("name = N'ada'")


class TestThreeValuedLogic:
    """SQL has three truth values and Python has two.

    Treating unknown as false is not merely imprecise: it is what makes the
    standard catalog clause return nothing at all.
    """

    def test_a_comparison_against_null_is_unknown_not_false(self):
        assert value("note = 'x'") is None

    def test_unknown_does_not_pass_the_filter(self):
        assert check("note = 'x'") is False

    def test_negating_unknown_stays_unknown(self):
        assert value("NOT (note = 'x')") is None

    def test_false_and_unknown_is_false(self):
        assert value("id = 2 AND note = 'x'") is False

    def test_true_and_unknown_is_unknown(self):
        assert value("id = 1 AND note = 'x'") is None

    def test_true_or_unknown_is_true(self):
        assert value("id = 1 OR note = 'x'") is True

    def test_false_or_unknown_is_unknown(self):
        assert value("id = 2 OR note = 'x'") is None

    def test_is_null_is_never_unknown(self):
        assert value("note IS NULL") is True
        assert value("note IS NOT NULL") is False


class TestParameters:
    def test_a_bound_parameter(self):
        assert check("id = @id", {"@id": 1})
        assert not check("id = @id", {"@id": 2})

    def test_the_at_sign_is_optional_in_the_supplied_name(self):
        assert check("id = @id", {"id": 1})

    def test_an_unsupplied_parameter_is_null(self):
        # Clients guard clauses with IS NULL precisely so an absent value is
        # harmless rather than an error.
        assert value("id = @nope") is None
        assert check("@nope IS NULL")

    def test_the_catalog_clause_with_everything_null_matches(self):
        clause = ("(name = @Name or (@Name is null)) and "
                  "(id = @Id or (@Id is null))")
        assert check(clause, {"@Name": None, "@Id": None})

    def test_the_catalog_clause_filters_when_a_value_is_given(self):
        clause = ("(name = @Name or (@Name is null)) and "
                  "(id = @Id or (@Id is null))")
        assert check(clause, {"@Name": "ada", "@Id": None})
        assert not check(clause, {"@Name": "grace", "@Id": None})


class TestStructure:
    def test_and_binds_tighter_than_or(self):
        # id=2 AND id=3 is false, so the OR decides.
        assert check("id = 1 OR id = 2 AND id = 3")

    def test_brackets_override_precedence(self):
        assert not check("(id = 1 OR id = 2) AND id = 3")

    def test_bracketed_and_quoted_column_names(self):
        assert check("[id] = 1") and check('"id" = 1')

    def test_a_table_qualifier_is_ignored(self):
        assert check("people.id = 1")


class TestErrors:
    @pytest.mark.parametrize("expr", ["id =", "id = = 1", "(id = 1", "id 1", ""])
    def test_broken_expressions_are_refused(self, expr):
        with pytest.raises(PredicateError):
            parse_predicate(expr)

    def test_an_unknown_column_names_itself(self):
        with pytest.raises(PredicateError, match="invalid column name 'nope'"):
            check("nope = 1")

    def test_the_level_and_state_are_the_usual_ones_unless_told(self):
        plain = PredicateError("x", number=8134)
        assert (plain.severity, plain.state) == (16, 1)

    def test_wrapped_as_a_sources_error_it_keeps_all_of_itself(self):
        # Wrapping by the words and the number alone lost the level, so a
        # complaint a real server sends at 15 went out at 16.
        wrapped = SourceError.carrying(
            PredicateError("x", number=4116, severity=15, state=2))
        assert (str(wrapped), wrapped.number, wrapped.severity,
                wrapped.state) == ("x", 4116, 15, 2)


class TestResultKind:
    """What an expression produces, said without running it.

    Deliberately partial: it answers where it is sure and says nothing
    otherwise. A wrong answer here would declare a column the values then
    contradict, and None costs only a text column of NULLs.
    """

    def kind(self, text):
        found = result_kind(parse_expression(text))
        if found is type(None):
            return "null"
        return getattr(found, "__name__", "unknown")

    def test_a_cast_states_it(self):
        assert self.kind("CAST(x AS int)") == "int"
        assert self.kind("CAST(x AS nvarchar(10))") == "str"

    def test_a_function_with_one_return_type(self):
        assert self.kind("LEN(anything)") == "int"
        assert self.kind("UPPER(anything)") == "str"

    def test_a_function_that_takes_its_argument(self):
        assert self.kind("ABS(-3)") == "int"
        assert self.kind("ABS(score)") == "unknown"

    def test_arithmetic_over_numbers(self):
        assert self.kind("1 + 2") == "int"
        assert self.kind("1 + 2.5") == "float"

    def test_a_null_takes_the_other_side(self):
        assert self.kind("1 + NULL") == "int"
        assert self.kind("1.5 + NULL") == "float"

    def test_an_unknown_side_makes_it_unknown(self):
        # A column times two is whatever that column holds.
        assert self.kind("id * 2") == "unknown"

    def test_a_written_null_is_a_null_rather_than_unknown(self):
        assert self.kind("NULL") == "null"

    def test_a_case_agrees_or_says_nothing(self):
        assert self.kind("CASE WHEN 1 = 1 THEN 1 ELSE 2 END") == "int"
        assert self.kind("CASE WHEN 1 = 1 THEN 1 ELSE 2.5 END") == "float"
        assert self.kind("CASE WHEN 1 = 1 THEN 1 ELSE name END") == "unknown"

    def test_a_column_says_nothing_on_its_own(self):
        assert self.kind("name") == "unknown"

    def test_but_says_what_it_holds_when_the_columns_are_given(self):
        holds = {"name": str, "score": float, "p.score": float}
        assert result_kind(parse_expression("name"), holds) is str
        assert result_kind(parse_expression("score * 2"), holds) is float
        assert result_kind(parse_expression("p.score + 1"), holds) is float
        assert result_kind(parse_expression("missing * 2"), holds) is None

    def test_count_is_a_count_whatever_it_counted(self):
        assert self.kind("COUNT(*)") == "int"
        assert self.kind("MAX(score)") == "unknown"

class TestWhatAnInHolds:
    """IN, which looks a value up in its candidates rather than walking them.

    Equality here is not Python's: text is compared without regard to case
    and with trailing spaces ignored, and a number beside text converts the
    text rather than the other way round, so 2 is in ('2') and '2' is in
    (2). A set that missed any of that would answer a query wrongly and say
    nothing, so the whole grid is checked against the rule it replaced.
    """

    VALUES = [
        1, 2, 0, -1, 2.0, 2.5, "2", "2.0", " 2", "2 ", "a", "A", "a ", "",
        "abc", True, False, datetime.datetime(2026, 1, 1), b"a", None,
        "1e3", 1000.0, "0x2",
    ]

    def walked(self, value, candidates, negated=False):
        """What In.evaluate did before: compare each candidate in turn.

        A match wins over a candidate that cannot be read against the value,
        whichever of the two was written first. Measured: 'a' IN ('a', a
        datetime) and 'a' IN (that datetime, 'a') are both yes, while 'b' IN
        either of those lists is message 241. So the refusal is kept and
        raised only once nothing has matched, rather than stopping the walk
        where it happened.

        A datetime on the left does not go through here at all. The whole
        list is read as datetimes at once for that, which is why a datetime
        column IN ('2024-01-15', 'nonsense') is 241 even though the first of
        them matches; _Candidates.holds is where that lives.
        """
        if value is None:
            return Unknown
        unknown = False
        refusal = None
        for other in candidates:
            if other is None:
                unknown = True
                continue
            try:
                if compare("=", value, other):
                    return not negated
            except PredicateError as exc:
                refusal = refusal or exc
        if refusal is not None:
            raise refusal
        return Unknown if unknown else negated

    def looked_up(self, value, candidates, negated=False):
        if value is None:
            return Unknown
        known = _Candidates(candidates)
        if known.holds(value):
            return not negated
        return Unknown if known.has_nothing else negated

    @staticmethod
    def outcome(how, value, candidates, negated):
        """What one of them answered, or the number it refused with.

        A refusal is an answer to compare as much as a row is. Comparing a
        moment with '2' is message 241 on a real server, so both of these
        have to refuse and refuse alike, and an exception escaping the grid
        would only say that one of them raised first.
        """
        try:
            return how(value, candidates, negated)
        except PredicateError as exc:
            return f"refused {exc.number}"

    @pytest.mark.parametrize("size", [1, 2, 3])
    def test_it_answers_what_walking_them_answered(self, size):
        checked = 0
        for value in self.VALUES:
            if isinstance(value, datetime.datetime):
                # Walking is the wrong model for a moment on the left, and
                # measurably so: a real server reads the whole candidate list
                # as datetimes at once there, so it refuses a list holding
                # 'nonsense' even where an earlier candidate matched, which
                # no pairwise walk can produce. Those rules are checked one
                # at a time below instead.
                continue
            for candidates in itertools.combinations(self.VALUES, size):
                for negated in (False, True):
                    checked += 1
                    assert (self.outcome(self.looked_up, value, candidates, negated)
                            == self.outcome(self.walked, value, candidates, negated)), (
                        f"{value!r} IN {candidates!r} negated={negated}")
        assert checked > 1000, "the grid got smaller than it was"

    WHEN = datetime.datetime(2024, 1, 15)

    @pytest.mark.parametrize("value, candidates, expected", [
        # Every one measured on SQL Server 2025 against a datetime column.
        (WHEN, ("2024-01-15",), True),          # text is read as a moment
        (WHEN, ("20240115",), True),            # eight digits are a date
        (WHEN, ("2024/01/15",), True),          # so are slashes
        (WHEN, ("2024-1-15",), True),           # and no leading nought
        (WHEN, ("01/15/2024",), True),          # month first, us_english
        (WHEN, ("Jan 15 2024",), True),         # and the month by name
        (WHEN, ("2024-01-16",), False),
        (WHEN, ("2024-01-15", "1965-03-02"), True),
        # A number is days since 1900, and 45304 is what a real server says
        # CAST(CAST('2024-01-15' AS datetime) AS int) is. Excel counts from
        # two days earlier, which is why its own serial for that day is 45306.
        (WHEN, (45304,), True),
        (WHEN, (datetime.datetime(2024, 1, 15),), True),
        # A moment on the right instead, which is pairwise and stops at a
        # match: 'a' IN ('a', a datetime) is yes on a real server.
        ("a", ("a", WHEN), True),
        (2, ("2", WHEN), True),
        (1, ("2", WHEN), False),
    ])
    def test_the_moments_it_holds(self, value, candidates, expected):
        assert self.looked_up(value, candidates) == expected

    @pytest.mark.parametrize("value, candidates", [
        # Message 241, measured. The list is read as datetimes once, so a
        # candidate that is not one is an error even where another matched.
        (WHEN, ("nonsense",)),
        (WHEN, ("2024-01-15", "nonsense")),
        (WHEN, ("2",)),
        # And the other way, where nothing matched first: 'b' IN ('a', a
        # datetime) is 241 where 'a' IN ('a', that datetime) is yes.
        ("b", ("a", WHEN)),
    ])
    def test_what_it_will_not_read_as_a_moment(self, value, candidates):
        with pytest.raises(PredicateError) as raised:
            self.looked_up(value, candidates)
        assert raised.value.number == 241

    @pytest.mark.parametrize("value, candidates, expected", [
        (2, (1, 2, 3), True),
        (2, ("2",), True),                     # a number against text
        ("2", (2,), True),                     # and the other way round
        ("A", ("a",), True),                   # case does not count
        ("a ", ("a",), True),                  # nor does a trailing space
        (2, (1, 3), False),
        (2, (1, None), Unknown),               # it might have been the NULL
        (2, (2, None), True),                  # found, so the NULL is moot
        (None, (1, 2), Unknown),
        (2, (), False),
    ])
    def test_the_rules_it_has_to_keep(self, value, candidates, expected):
        assert self.looked_up(value, candidates) == expected

    def test_negated_turns_the_answer_over_but_not_the_unknown(self):
        assert self.looked_up(2, (1, None), negated=True) == Unknown
        assert self.looked_up(2, (1, 3), negated=True) is True
        assert self.looked_up(2, (1, 2), negated=True) is False


class TestHowADateWrittenOutIsRead:
    """The spellings a real server accepts, each measured against one.

    Only ISO was read before, through fromisoformat, which was enough while
    nothing produced a datetime column: a JSON source has no dates and a CSV's
    are text. A workbook has them, so WHERE hired > '2024-6-1' became a query
    somebody would write, and refusing it is not what a real server does.
    """

    WHEN = datetime.datetime(2024, 1, 15)
    NOON = datetime.datetime(2024, 1, 15, 13, 30)

    @pytest.mark.parametrize("written, expected", [
        ("2024-01-15", WHEN),
        ("2024/01/15", WHEN),
        ("2024.01.15", WHEN),
        ("2024-1-15", WHEN),                   # no leading nought
        ("20240115", WHEN),                    # eight digits
        ("01/15/2024", WHEN),                  # month first, us_english
        ("01-15-2024", WHEN),
        ("1/15/2024", WHEN),
        ("Jan 15 2024", WHEN),
        ("January 15, 2024", WHEN),
        ("jan 15 2024", WHEN),                 # the name is not case-sensitive
        ("15 Jan 2024", WHEN),
        ("  2024-01-15  ", WHEN),              # space around it
        ("2024-01-15 13:30", NOON),
        ("2024-01-15T13:30:00", NOON),
        ("2024-01-15 1:30 PM", NOON),
        ("2024-01-15 1:30PM", NOON),
        ("Jan 15 2024 1:30PM", NOON),
        ("2024-01-15 13:30:00.500", datetime.datetime(2024, 1, 15, 13, 30, 0, 500000)),
        ("2024-01-15 00:00:00", WHEN),
        # A time with no date is the first day of 1900, where a datetime
        # counts from. Measured: CAST('13:30' AS datetime) is 1900-01-01 13:30.
        ("13:30", datetime.datetime(1900, 1, 1, 13, 30)),
        ("1:30 AM", datetime.datetime(1900, 1, 1, 1, 30)),
        ("12:30 AM", datetime.datetime(1900, 1, 1, 0, 30)),
        ("12:30 PM", datetime.datetime(1900, 1, 1, 12, 30)),
    ])
    def test_what_it_reads(self, written, expected):
        assert _as_datetime(written) == expected

    @pytest.mark.parametrize("written", [
        "nonsense", "2", "", "   ", "2024", "2024-13-01", "2024-01-32",
        "25:00", "2024-01-15 13:70",
        # Two digits of year say nothing about which part is which: 01/02/03
        # is three different days depending on who reads it, so it is refused
        # rather than guessed at.
        "01/02/03",
    ])
    def test_what_it_will_not_read(self, written):
        with pytest.raises(ValueError):
            _as_datetime(written)

    def test_a_number_is_days_since_1900(self):
        # Which is why CAST(0 AS datetime) is the first of January 1900.
        assert _as_datetime(0) == datetime.datetime(1900, 1, 1)
        assert _as_datetime(45304) == datetime.datetime(2024, 1, 15)
        assert _as_datetime(0.5) == datetime.datetime(1900, 1, 1, 12)

    def test_a_bit_is_the_number_it_is(self):
        # Measured: a datetime of 1900-01-02 equals a bit of 1.
        assert _as_datetime(True) == datetime.datetime(1900, 1, 2)


class TestAMomentAsANumber:
    """CAST(a datetime AS int), and the rounding it does.

    Measured on SQL Server 2025: 2024-06-01 13:30 is 45442.5625 as a float
    and 45443 as an int, so the whole one rounds rather than truncating, and
    a half goes away from nought, which only a date before 1900 shows.
    """

    @pytest.mark.parametrize("moment, expected", [
        (datetime.datetime(2024, 6, 1, 13, 30), 45442.5625),
        (datetime.datetime(2024, 1, 15), 45304.0),
        (datetime.datetime(1900, 1, 1), 0.0),
        (datetime.datetime(1899, 6, 1, 18), -213.25),
    ])
    def test_as_a_float(self, moment, expected):
        assert _days_since_1900(moment) == expected

    @pytest.mark.parametrize("days, expected", [
        (45442.75, 45443), (45442.5, 45443), (45442.25, 45442),
        (-213.75, -214), (-213.5, -214), (-213.25, -213),
        (0.0, 0),
    ])
    def test_the_whole_one_rounds(self, days, expected):
        assert _rounded_away(days) == expected


class TestWhatABugHuntFound:
    """Six answers that were wrong, each measured against SQL Server 2025.

    Every one reached a client: four as a wrong answer with nothing said, one
    as an internal error, and one as a refusal carrying the number for a
    different mistake.
    """

    WHEN = datetime.datetime(2024, 1, 15)

    def refusal(self, how):
        with pytest.raises(PredicateError) as raised:
            how()
        return raised.value.number

    def test_a_number_too_big_to_be_a_date(self):
        # An OverflowError out of timedelta, which travelled out of the query
        # as an internal error rather than as anything a client can read.
        # Measured: a real server calls it an arithmetic overflow, 8115.
        assert self.refusal(lambda: compare(">", self.WHEN, 1e18)) == 8115
        assert self.refusal(lambda: compare(">", self.WHEN, -1e18)) == 8115
        assert self.refusal(lambda: _as_datetime(10 ** 30)) == 8115

    def test_a_number_that_is_a_date_still_converts(self):
        # The guard is on the overflow and not on numbers.
        assert _as_datetime(45304) == self.WHEN

    def test_text_that_is_not_a_number_beside_one(self):
        # Answering false was the quiet kind of wrong: a table of numbers
        # joined to a column of codes came back empty with nothing said.
        # Measured: 1 = 'x' is 245, and 1.5 = 'x' is 8114, the number
        # depending on which numeric type the text has to reach.
        assert self.refusal(lambda: compare("=", 1, "x")) == 245
        assert self.refusal(lambda: compare("=", "x", 1)) == 245
        assert self.refusal(lambda: compare("=", 1.5, "x")) == 8114
        assert self.refusal(lambda: compare("<", 1, "x")) == 245

    def test_text_that_is_a_number_still_compares(self):
        assert compare("=", 1, "1") is True
        assert compare("=", 2, "1") is False
        assert compare("=", "2", 2) is True

    def test_text_beside_something_that_is_not_a_number_is_untouched(self):
        # bytes on the other side, which is neither a number nor text, and
        # was compared as characters before and still is.
        assert compare("=", "x", b"y") is False

    def test_a_match_wins_over_a_conversion_that_cannot_be_done(self):
        # Measured: 1 IN ('1', 'x') is true and 1 IN ('x') is 245, so the
        # refusal is only reached once nothing has matched.
        assert _Candidates(["1", "x"]).holds(1) is True
        assert self.refusal(lambda: _Candidates(["x"]).holds(1)) == 245
        assert _Candidates([1, 2, 0]).holds("2") is True
        assert self.refusal(lambda: _Candidates([1, 2, 0]).holds("a")) == 245

    def test_the_first_refusal_written_is_the_one_reported(self):
        # 'a' IN (1, a datetime) is 245 and not the 241 the datetime gives,
        # because the 1 was written first.
        assert self.refusal(
            lambda: _Candidates([1, self.WHEN]).holds("a")) == 245
        assert self.refusal(
            lambda: _Candidates([self.WHEN, 1]).holds("a")) == 241

    def test_a_moment_becomes_characters_the_way_a_cast_does(self):
        # LIKE used str, which gives the ISO spelling, where everything else
        # that turns a value into characters uses the conversion a real
        # server uses. Measured both ways round: hired LIKE '2024%' matched
        # here and matches nothing there, and 'Jan%' the other way about.
        matched = matches(parse_predicate("hired LIKE 'Jan%'"),
                          {"hired": self.WHEN}, {})
        assert matched is True
        assert matches(parse_predicate("hired LIKE '2024%'"),
                       {"hired": self.WHEN}, {}) is False

    def test_patindex_will_not_take_a_moment_at_all(self):
        # Where LIKE takes one and converts it. The difference is the
        # function's rather than the value's; measured, message 8116.
        assert self.refusal(
            lambda: parse_expression("PATINDEX('%Jan%', hired)").evaluate(
                {"hired": self.WHEN}, {})) == 8116

    def test_a_bare_name_after_a_join_has_qualified_them(self):
        # A join renames its columns to table.column, so a reference written
        # as plain n found nothing and was refused as a name that is not
        # there. Measured: a real server answers it.
        row = {"numbers.n": 1, "codes.code": 1}
        assert matches(parse_predicate("n = code"), row, {}) is True

    def test_a_bare_name_more_than_one_of_them_has(self):
        # Not a spelling mistake, and it does not get the number for one.
        # Measured: message 209.
        row = {"a.id": 1, "b.id": 2}
        assert self.refusal(
            lambda: matches(parse_predicate("id = 1"), row, {})) == 209

    def test_a_bare_name_none_of_them_has_is_still_207(self):
        row = {"a.id": 1, "b.id": 2}
        assert self.refusal(
            lambda: matches(parse_predicate("nope = 1"), row, {})) == 207


class TestAWrittenInList:
    """A list of written values, which used to be read once for every row.

    Every candidate was evaluated per row and a tuple of all of them built to
    look the kept set up by, so the cost was rows times values rather than
    rows plus values. Measured: 2,000 values over 3,200 rows took 452 ms and
    takes 10, and the gap widens with every row because one of them grew with
    the rows and the other does not.
    """

    def catalog(self, rows):
        from pysqlbridge.catalog import Catalog
        from pysqlbridge.source import from_records

        c = Catalog()
        c.add(from_records([{"id": n} for n in range(rows)], name="t"))
        return c

    def asked(self, c, sql):
        from pysqlbridge.tds.result import Query

        return [list(row) for row in
                c.answer(Query(sql=sql, parameters={}, session={})).rows]

    def test_it_costs_the_rows_plus_the_values_and_not_their_product(self):
        import time

        rows, values = 20000, 2000
        c = self.catalog(rows)
        listed = ",".join(str(n) for n in range(values))
        sql = f"SELECT COUNT(*) AS n FROM t WHERE id IN ({listed})"

        began = time.perf_counter()
        assert self.asked(c, sql) == [[values]]
        took = time.perf_counter() - began

        # Reading the values once a row is 40 million evaluations and takes
        # about three seconds; reading them once takes about a fiftieth of
        # one. A second sits far below the one and far above the other, so
        # this says which shape is running rather than how quick the machine
        # is.
        assert took < 1.0, f"took {took:.2f}s, so the values were read per row"

    def test_a_written_list_still_answers_what_it_answered(self):
        c = self.catalog(10)
        assert self.asked(c, "SELECT id FROM t WHERE id IN (2, 4)") == [[2], [4]]
        assert self.asked(c, "SELECT id FROM t WHERE id NOT IN (0,1,2,3,4,5,6,7,8)") == [[9]]
        assert self.asked(c, "SELECT id FROM t WHERE id IN ('2')") == [[2]]
        assert self.asked(c, "SELECT COUNT(*) AS n FROM t WHERE id IN (1, NULL)") == [[1]]
        assert self.asked(c, "SELECT COUNT(*) AS n FROM t WHERE id NOT IN (1, NULL)") == [[0]]

    def test_a_list_that_is_not_all_written_values_is_still_read_per_row(self):
        # A column or a subquery on the right can differ from row to row, so
        # only a list of literals may be settled once.
        c = self.catalog(5)
        assert self.asked(c, "SELECT id FROM t WHERE 2 IN (id, 99)") == [[2]]
        assert self.asked(
            c, "SELECT id FROM t WHERE id IN (SELECT id FROM t WHERE id > 3)"
        ) == [[4]]

    def test_the_same_node_answers_two_different_rows(self):
        # The set is kept on the node, so the second row must not be given
        # the first row's answer.
        c = self.catalog(6)
        assert self.asked(c, "SELECT id FROM t WHERE id IN (1, 3, 5)") == [
            [1], [3], [5]]
