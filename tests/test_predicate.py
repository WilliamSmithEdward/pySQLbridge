import datetime
import itertools

import pytest

from pysqlbridge.predicate import (
    PredicateError,
    Unknown,
    _Candidates,
    compare,
    matches,
    parse_expression,
    parse_predicate,
    result_kind,
)

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
        """What In.evaluate did before: compare each candidate in turn."""
        if value is None:
            return Unknown
        unknown = False
        for other in candidates:
            if other is None:
                unknown = True
            elif compare("=", value, other):
                return not negated
        return Unknown if unknown else negated

    def looked_up(self, value, candidates, negated=False):
        if value is None:
            return Unknown
        known = _Candidates(candidates)
        if known.holds(value):
            return not negated
        return Unknown if known.has_nothing else negated

    @pytest.mark.parametrize("size", [1, 2, 3])
    def test_it_answers_what_walking_them_answered(self, size):
        checked = 0
        for value in self.VALUES:
            for candidates in itertools.combinations(self.VALUES, size):
                for negated in (False, True):
                    checked += 1
                    assert (self.looked_up(value, candidates, negated)
                            == self.walked(value, candidates, negated)), (
                        f"{value!r} IN {candidates!r} negated={negated}")
        assert checked > 1000, "the grid got smaller than it was"

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
