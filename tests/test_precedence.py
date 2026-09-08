"""The order that decides a union's column type, and the conversion to it.

Every expected value here was measured against SQL Server 2025, pair by
pair, in both directions. The order came out total, which is what lets
resolve take a maximum instead of comparing branches against each other.
"""

import pytest

from pysqlbridge.precedence import convert, named, resolve, sized
from pysqlbridge.tds.result import (
    Binary,
    Bit,
    DateTime,
    Float,
    Integer,
    NVarChar,
    QueryError,
    SmallInt,
    UniqueIdentifier,
    VarBinary,
)

# Lowest to highest, which is the order measured. Each one beats every type
# before it, in a union written either way round.
ORDER = [
    VarBinary(50),
    NVarChar(50),
    UniqueIdentifier(),
    Bit(),
    Integer(1),
    Integer(2),
    Integer(4),
    Integer(8),
    Float(8),
    DateTime(),
]

# The pairs of those that do not convert at all, so a union of them is
# refused before anything runs.
CLASHES = {
    (UniqueIdentifier, Bit), (UniqueIdentifier, Integer),
    (UniqueIdentifier, Float), (UniqueIdentifier, DateTime),
    (VarBinary, Float),
}


def clashes(one, other) -> bool:
    kinds = {type(one), type(other)}
    return any(set(pair) == kinds for pair in CLASHES)


class TestTheOrder:
    @pytest.mark.parametrize("at", range(len(ORDER)))
    @pytest.mark.parametrize("beside", range(len(ORDER)))
    def test_the_higher_of_any_two_wins(self, at, beside):
        one, other = ORDER[at], ORDER[beside]
        if at == beside or clashes(one, other):
            return
        expected = ORDER[max(at, beside)]
        assert resolve([one, other]) == expected
        assert resolve([other, one]) == expected

    def test_branches_that_agree_settle_nothing(self):
        # None is the answer that means there is nothing to convert, which is
        # what keeps the ordinary union exactly as it was.
        assert resolve([Integer(4), Integer(4)]) is None
        assert resolve([NVarChar(50), NVarChar(50)]) is None

    def test_the_widest_of_a_tie_wins(self):
        assert resolve([NVarChar(5), NVarChar(50)]) == NVarChar(50)
        assert resolve([NVarChar(50), NVarChar(5)]) == NVarChar(50)

    def test_max_is_wider_than_any_declared_width(self):
        assert resolve([NVarChar(4000), NVarChar(None)]) == NVarChar(None)

    def test_the_highest_of_several_wins(self):
        assert resolve([NVarChar(5), Integer(4), Float(8)]) == Float(8)

    def test_smallint_ranks_with_the_integer_of_its_width(self):
        assert resolve([SmallInt(), Integer(4)]) == Integer(4)

    def test_a_winning_smallint_becomes_the_nullable_encoding(self):
        # INT2 and INTN(2) are two encodings of one SQL type, and only the
        # second can say NULL. Branches that disagree can always hand the
        # column one, and the fixed form would write it out as a zero.
        assert resolve([SmallInt(), Bit()]) == Integer(2)


class TestWhatDoesNotConvert:
    @pytest.mark.parametrize("one, other", [
        (UniqueIdentifier(), Integer(4)),
        (UniqueIdentifier(), Float(8)),
        (UniqueIdentifier(), DateTime()),
        (UniqueIdentifier(), Bit()),
        (VarBinary(50), Float(8)),
        (Binary(10), Float(8)),
    ])
    def test_a_clash_is_refused_either_way_round(self, one, other):
        for kinds in ([one, other], [other, one]):
            with pytest.raises(QueryError, match="Operand type clash") as refused:
                resolve(kinds)
            assert refused.value.number == 206

    def test_the_clash_names_the_lower_type_first(self):
        with pytest.raises(QueryError) as refused:
            resolve([Integer(8), UniqueIdentifier()])
        assert str(refused.value) == (
            "Operand type clash: uniqueidentifier is incompatible with bigint"
        )


class TestConverting:
    def test_a_null_stays_null_whatever_it_is_converted_to(self):
        assert convert(None, Integer(4), NVarChar(50)) is None
        assert convert(None, DateTime(), NVarChar(50)) is None

    def test_text_that_spells_a_number_converts(self):
        assert convert("7", Integer(4), NVarChar(50)) == 7
        assert convert("2.5", Float(8), NVarChar(50)) == 2.5

    def test_a_number_written_out_converts(self):
        assert convert(7, NVarChar(1), Integer(4)) == "7"

    def test_a_number_is_a_moment_counted_from_1900(self):
        assert convert(1, DateTime(), Integer(4)).isoformat() == "1900-01-02T00:00:00"

    @pytest.mark.parametrize("to, number", [
        (Integer(4), 245),
        (Integer(1), 245),
        (SmallInt(), 245),
        (Bit(), 245),
        (Integer(8), 8114),
        (Float(8), 8114),
        (DateTime(), 241),
        (UniqueIdentifier(), 8169),
    ])
    def test_each_target_refuses_with_its_own_number(self, to, number):
        # No pattern to these; int and tinyint say one thing and bigint says
        # another. Measured one at a time.
        with pytest.raises(QueryError) as refused:
            convert("ada", to, NVarChar(50))
        assert refused.value.number == number

    def test_the_refusal_quotes_both_types_and_the_value(self):
        with pytest.raises(QueryError) as refused:
            convert("ada", Integer(4), NVarChar(50))
        assert str(refused.value) == (
            "Conversion failed when converting the nvarchar value 'ada' to "
            "data type int."
        )

    @pytest.mark.parametrize("value", ["0", "200", "255"])
    def test_a_tinyint_is_unsigned_and_holds_all_of_it(self, value):
        assert convert(value, Integer(1), NVarChar(50)) == int(value)

    @pytest.mark.parametrize("to, value, number", [
        (Integer(1), "-1", 244),
        (Integer(1), "256", 244),
        (Integer(1), "999", 244),
        (Integer(2), "99999", 244),
        (Integer(4), "-99999999999", 248),
        (Integer(8), "99999999999999999999", 8115),
    ])
    def test_a_number_too_big_for_its_column_overflows(self, to, value, number):
        with pytest.raises(QueryError) as refused:
            convert(value, to, NVarChar(50))
        assert refused.value.number == number

    def test_a_guid_in_text_converts(self):
        guid = "6f9619ff-8b86-d011-b42d-00c04fc964ff"
        assert convert(guid, UniqueIdentifier(), NVarChar(50)) == guid


class TestSizing:
    def test_text_is_widened_to_what_it_holds(self):
        # A number written out is longer than the branch that declared the
        # column, and a column too narrow for its own values cuts them short.
        assert sized(NVarChar(1), ["abc", "de", None]).max_chars == 3

    def test_past_the_sized_limit_it_takes_the_max_form(self):
        assert sized(NVarChar(1), ["x" * 5000]).is_max

    def test_nothing_else_is_resized(self):
        assert sized(Integer(4), [1, 2]) == Integer(4)
        assert sized(DateTime(), []) == DateTime()

    def test_a_column_of_nulls_is_still_a_column(self):
        assert sized(NVarChar(1), [None, None]).max_chars == 1


class TestNames:
    @pytest.mark.parametrize("kind, name", [
        (Integer(1), "tinyint"),
        (Integer(2), "smallint"),
        (Integer(4), "int"),
        (Integer(8), "bigint"),
        (SmallInt(), "smallint"),
        (Float(8), "float"),
        (NVarChar(50), "nvarchar"),
        (Bit(), "bit"),
        (DateTime(), "datetime"),
        (UniqueIdentifier(), "uniqueidentifier"),
        (VarBinary(50), "varbinary"),
        (Binary(10), "binary"),
    ])
    def test_each_type_is_called_what_sql_server_calls_it(self, kind, name):
        assert named(kind) == name
