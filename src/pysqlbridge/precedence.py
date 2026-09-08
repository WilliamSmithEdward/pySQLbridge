"""Which type a column gets when the branches of a union disagree.

A combined SELECT gives each output column one type. SQL Server chooses it
across every branch by data type precedence, converts every branch's values
to it, and only then drops repeated rows, so a client is handed a column
that holds one kind of value and is declared as that kind.

Serving the first branch's type instead is not a small difference. A union
of an int column and a float one declared int and handed 10.5 back as 10,
and a union of an int column and a name declared int and then could not
encode the name at all.

The order below was measured against SQL Server 2025, every pair of the
types this serves, in both directions. It came out a total order, so the
winner is the highest-ranked branch and nothing is worked out pairwise
except the pairs that refuse to convert at all.

Branches that already agree are left alone. A winner is only worked out
where they differ, so the ordinary union costs nothing and cannot start
behaving differently.
"""

from __future__ import annotations

import uuid

from .predicate import PredicateError, converted, fits
from .source import MAX_NVARCHAR_CHARS
from .tds.result import (
    Binary,
    Bit,
    ColumnType,
    DateTime,
    Float,
    Integer,
    NVarChar,
    QueryError,
    SmallInt,
    UniqueIdentifier,
    VarBinary,
)

# The numbers SQL Server answers a failed conversion with. Which one depends
# on the type being converted to and there is no pattern to it: int, smallint,
# tinyint and bit say 245, bigint and float say 8114, and the two types that
# parse their text have a number each. Measured, all of them.
CONVERSION_FAILED = 245
CONVERSION_ERROR = 8114
DATETIME_CONVERSION_FAILED = 241
GUID_CONVERSION_FAILED = 8169
TYPE_CLASH = 206

# Highest wins. Integer is four SQL types sharing one encoding here, so its
# width is part of its rank; everything else ranks by what it is. The gaps
# leave room to put a type in without renumbering the rest.
_RANK = {
    VarBinary: 10,
    Binary: 10,
    NVarChar: 20,
    UniqueIdentifier: 30,
    Bit: 40,
    SmallInt: 60,
    Float: 90,
    DateTime: 100,
}

_INTEGER_RANK = {1: 50, 2: 60, 4: 70, 8: 80}

# What SQL Server calls each of them. Worth naming exactly, because the
# messages below quote the type and a client shows them to a person, who
# should be able to search for one and find SQL Server's own documentation.
_NAMES = {
    VarBinary: "varbinary",
    Binary: "binary",
    NVarChar: "nvarchar",
    UniqueIdentifier: "uniqueidentifier",
    Bit: "bit",
    SmallInt: "smallint",
    Float: "float",
    DateTime: "datetime",
}

_INTEGER_NAMES = {1: "tinyint", 2: "smallint", 4: "int", 8: "bigint"}

# The pairs that do not convert either way round, which SQL Server refuses
# before it runs anything. Everything else in the order above converts.
# Measured: varbinary is fine beside int and refused beside float, which is
# not a rule anyone would have guessed at.
_INCOMPATIBLE = frozenset({
    frozenset({UniqueIdentifier, DateTime}),
    frozenset({UniqueIdentifier, Float}),
    frozenset({UniqueIdentifier, Integer}),
    frozenset({UniqueIdentifier, SmallInt}),
    frozenset({UniqueIdentifier, Bit}),
    frozenset({VarBinary, Float}),
    frozenset({Binary, Float}),
})

# What converting to each type is called, the way CAST names it. A type with
# no entry here needs none, because the only branch that can meet it is one
# that already is it: varbinary and binary sit at the bottom of the order and
# never win against anything else.
_CAST_NAME = {
    NVarChar: "NVARCHAR",
    Bit: "BIT",
    Float: "FLOAT",
    DateTime: "DATETIME",
    SmallInt: "SMALLINT",
}

# How wide each integer target is, and what overflowing it says. Three shapes
# for four widths, all measured: the narrow two name the encoding and suggest
# a wider column, int names the type, and bigint does not mention the value.
_OVERFLOW = {
    1: (244, "The conversion of the {source} value '{value}' overflowed an "
             "INT1 column. Use a larger integer column."),
    2: (244, "The conversion of the {source} value '{value}' overflowed an "
             "INT2 column. Use a larger integer column."),
    4: (248, "The conversion of the {source} value '{value}' overflowed an "
             "int column."),
    8: (8115, "Arithmetic overflow error converting expression to data type "
              "bigint."),
}


def named(kind: ColumnType) -> str:
    """What SQL Server calls this type."""
    if isinstance(kind, Integer):
        return _INTEGER_NAMES[kind.width]
    return _NAMES[type(kind)]


def resolve(kinds: list[ColumnType]) -> ColumnType | None:
    """The one type a column of several branches gets.

    None when they already agree, which is the answer that means there is
    nothing to convert and the branches can be used as they came.
    """
    first = kinds[0]
    if all(kind == first for kind in kinds):
        return None

    for one in kinds:
        for other in kinds:
            if frozenset({type(one), type(other)}) in _INCOMPATIBLE:
                low, high = sorted((one, other), key=_rank)
                raise QueryError(
                    f"Operand type clash: {named(low)} is incompatible with "
                    f"{named(high)}",
                    number=TYPE_CLASH,
                )
    winner = max(kinds, key=lambda kind: (_rank(kind), _width(kind)))
    if isinstance(winner, SmallInt):
        # INT2 is the fixed form, which spends no length byte and so cannot
        # say NULL. A column built from branches that disagree can always be
        # handed one, so the winner is the nullable encoding of that type.
        return Integer(2)
    return winner


def convert(value: object, to: ColumnType, source: ColumnType) -> object:
    """One value brought to the type its column settled on.

    source names the type the value came from, which the message needs: SQL
    Server reports both ends of a conversion it could not make.
    """
    if value is None:
        return None
    if isinstance(to, UniqueIdentifier):
        return _as_guid(value)

    name = "INT" if isinstance(to, Integer) else _CAST_NAME.get(type(to))
    if name is None:
        _refuse(value, source, to)
    try:
        result = converted(value, name)
    except (PredicateError, ValueError):
        _refuse(value, source, to)

    width = _integer_width(to)
    # The ranges live with the cast, so the two cannot come to disagree about
    # what a tinyint holds.
    if width and not fits(result, _INTEGER_NAMES[width]):
        _overflowed(value, source, width)
    return result


def sized(kind: ColumnType, values: list) -> ColumnType:
    """The winning type, widened to hold what converting actually produced.

    Only text can outgrow the branches it came from: a number written out is
    as long as the number, and neither branch's declared width knew that.
    """
    if not isinstance(kind, NVarChar):
        return kind
    longest = max((len(value) for value in values if value is not None), default=1)
    if longest > MAX_NVARCHAR_CHARS:
        return NVarChar(None)
    return NVarChar(max(longest, 1))


def _integer_width(kind: ColumnType) -> int:
    """How many bytes this holds a whole number in, or zero if it is not one."""
    if isinstance(kind, Integer):
        return kind.width
    return 2 if isinstance(kind, SmallInt) else 0


def _rank(kind: ColumnType) -> int:
    if isinstance(kind, Integer):
        return _INTEGER_RANK[kind.width]
    return _RANK[type(kind)]


def _width(kind: ColumnType) -> int:
    """How much a type holds, which breaks a tie between two of one rank.

    nvarchar(50) beside nvarchar(5) is nvarchar(50). MAX holds more than any
    declared width, so it stands above all of them.
    """
    if isinstance(kind, NVarChar):
        return MAX_NVARCHAR_CHARS + 1 if kind.is_max else kind.max_chars
    return getattr(kind, "size", getattr(kind, "width", 0))


def _as_guid(value: object) -> str:
    """Text read as a uniqueidentifier, and kept as text, because that is how
    the catalog views hold one and what the column encodes."""
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError):
        raise QueryError(
            "Conversion failed when converting from a character string to "
            "uniqueidentifier.",
            number=GUID_CONVERSION_FAILED,
        ) from None


def _refuse(value: object, source: ColumnType, to: ColumnType) -> None:
    """The error SQL Server answers with when a value will not convert."""
    target = named(to)
    if isinstance(to, DateTime):
        raise QueryError(
            "Conversion failed when converting date and/or time from "
            "character string.",
            number=DATETIME_CONVERSION_FAILED,
        )
    if target in ("bigint", "float"):
        raise QueryError(
            f"Error converting data type {named(source)} to {target}.",
            number=CONVERSION_ERROR,
        )
    raise QueryError(
        f"Conversion failed when converting the {named(source)} value "
        f"'{value}' to data type {target}.",
        number=CONVERSION_FAILED,
    )


def _overflowed(value: object, source: ColumnType, width: int) -> None:
    number, wording = _OVERFLOW[width]
    raise QueryError(
        wording.format(source=named(source), value=value), number=number
    )
