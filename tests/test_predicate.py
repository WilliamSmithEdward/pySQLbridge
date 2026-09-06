import pytest

from pysqlbridge.predicate import PredicateError, matches, parse_predicate

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
