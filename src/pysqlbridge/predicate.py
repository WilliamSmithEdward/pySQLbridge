"""Boolean expressions, for WHERE clauses.

Small on purpose: comparisons, IS NULL, AND, OR, NOT and parentheses, over
column references, literals and parameters. That is the shape clients send, and
in particular it is exactly what a catalog query looks like:

    (TABLE_CATALOG = @Catalog or (@Catalog is null))
    and (TABLE_SCHEMA = @Owner or (@Owner is null))

Evaluation uses SQL's three-valued logic rather than Python's two, and that is
not a detail. In the clause above every parameter is normally NULL, so each
left-hand comparison is unknown rather than false, and only OR-ing it with a
true IS NULL makes the whole thing true. Treating unknown as false would return
no rows, which looks like an empty database rather than a bug.

A row passes only when the result is true. Unknown does not pass.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping

# Unknown is spelled None here, distinct from the Python False that a genuinely
# false comparison produces.
Unknown = None
Ternary = bool | None


class PredicateError(Exception):
    """An expression this project cannot parse or evaluate."""


_TOKEN = re.compile(
    r"""
      (?P<space>    \s+ )
    | (?P<number>   -? \d+ (?: \.\d+ )? (?: [eE][-+]?\d+ )? )
    | (?P<string>   N?' (?: [^'] | '' )* ' )
    | (?P<param>    @ [A-Za-z0-9_@#$]+ )
    | (?P<bracketed> \[ (?: [^\]] | \]\] )* \] )
    | (?P<quoted>   " (?: [^"] | "" )* " )
    | (?P<operator> <> | != | >= | <= | = | < | > | \|\| | [-+*/%] )
    | (?P<punct>    [(),.] )
    | (?P<word>     [A-Za-z_@#][A-Za-z0-9_@#$]* )
    """,
    re.VERBOSE,
)

_KEYWORDS = {
    "AND", "OR", "NOT", "IS", "NULL", "TRUE", "FALSE",
    "LIKE", "IN", "BETWEEN", "ESCAPE",
    "CASE", "WHEN", "THEN", "ELSE", "END", "AS",
}

# What CAST and CONVERT will produce. The names are SQL Server's; the values
# are what this actually serves, so a cast to any integer type is an integer.
# The aggregates, named here because a function call has to be recognised as
# one before anything can say whether the name is known at all. sql.py takes
# its own AGGREGATES from this.
AGGREGATE_NAMES = frozenset({"COUNT", "SUM", "MIN", "MAX", "AVG"})

CAST_TYPES = {
    "INT": int, "INTEGER": int, "BIGINT": int, "SMALLINT": int,
    "TINYINT": int, "BIT": bool,
    "FLOAT": float, "REAL": float, "DECIMAL": float, "NUMERIC": float,
    "MONEY": float,
    "NVARCHAR": str, "VARCHAR": str, "NCHAR": str, "CHAR": str, "TEXT": str,
    "NTEXT": str, "SYSNAME": str,
}


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _number(value: object) -> float | int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    try:
        text = str(value).strip()
        return int(text) if text.lstrip("-+").isdigit() else float(text)
    except (TypeError, ValueError):
        raise PredicateError(f"{value!r} is not a number") from None


def _substring(value, start, length):
    """SUBSTRING, which counts from one and tolerates running off the end."""
    text = _text(value)
    begin = max(int(_number(start)) - 1, 0)
    return text[begin:begin + max(int(_number(length)), 0)]


def _round(value, digits=0):
    return round(_number(value), int(_number(digits)))


# The functions a query is likely to use on data this serves. Each takes the
# already-evaluated arguments. NULL handling is per function: most of these
# return NULL for a NULL input, which is what SQL Server does.
FUNCTIONS = {
    "LEN": lambda v: None if v is None else len(_text(v).rstrip()),
    "DATALENGTH": lambda v: None if v is None else len(_text(v)),
    "UPPER": lambda v: None if v is None else _text(v).upper(),
    "LOWER": lambda v: None if v is None else _text(v).lower(),
    "LTRIM": lambda v: None if v is None else _text(v).lstrip(),
    "RTRIM": lambda v: None if v is None else _text(v).rstrip(),
    "TRIM": lambda v: None if v is None else _text(v).strip(),
    "REVERSE": lambda v: None if v is None else _text(v)[::-1],
    "LEFT": lambda v, n: None if v is None else _text(v)[:int(_number(n))],
    "RIGHT": lambda v, n: None if v is None else (
        _text(v)[-int(_number(n)):] if int(_number(n)) else ""
    ),
    "SUBSTRING": lambda v, a, b: None if v is None else _substring(v, a, b),
    "REPLACE": lambda v, a, b: None if v is None else _text(v).replace(
        _text(a), _text(b)
    ),
    "CHARINDEX": lambda needle, hay, *rest: (
        None if hay is None else _text(hay).lower().find(
            _text(needle).lower(),
            int(_number(rest[0])) - 1 if rest else 0,
        ) + 1
    ),
    "CONCAT": lambda *values: "".join(_text(v) for v in values),
    "ISNULL": lambda a, b: b if a is None else a,
    "COALESCE": lambda *values: next(
        (v for v in values if v is not None), None
    ),
    "NULLIF": lambda a, b: None if a == b else a,
    "IIF": lambda test, yes, no: yes if test else no,
    "ABS": lambda v: None if v is None else abs(_number(v)),
    "SIGN": lambda v: None if v is None else (
        0 if _number(v) == 0 else (1 if _number(v) > 0 else -1)
    ),
    "FLOOR": lambda v: None if v is None else int(math.floor(_number(v))),
    "CEILING": lambda v: None if v is None else int(math.ceil(_number(v))),
    "ROUND": lambda v, *rest: None if v is None else _round(v, *rest),
    "POWER": lambda a, b: None if a is None else _number(a) ** _number(b),
    "SQRT": lambda v: None if v is None else math.sqrt(_number(v)),
    "SPACE": lambda n: " " * int(_number(n)),
    "STR": lambda v, *rest: None if v is None else _text(v),
}


# What arithmetic does, and what + means depends on its operands: two numbers
# add and anything with text concatenates, which is what SQL Server does with
# a string.
def _plus(a, b):
    if isinstance(a, str) or isinstance(b, str):
        return _text(a) + _text(b)
    return _number(a) + _number(b)


ARITHMETIC = {
    "+": _plus,
    "||": lambda a, b: _text(a) + _text(b),
    "-": lambda a, b: _number(a) - _number(b),
    "*": lambda a, b: _number(a) * _number(b),
    "/": lambda a, b: (
        None if _number(b) == 0
        else (_number(a) // _number(b)
              if isinstance(_number(a), int) and isinstance(_number(b), int)
              else _number(a) / _number(b))
    ),
    "%": lambda a, b: None if _number(b) == 0 else _number(a) % _number(b),
}


@dataclass(frozen=True)
class Token:
    kind: str
    text: str


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    at = 0
    while at < len(text):
        match = _TOKEN.match(text, at)
        if not match:
            raise PredicateError(f"cannot read {text[at:at + 20]!r}")
        at = match.end()
        kind = match.lastgroup
        if kind == "space":
            continue
        value = match.group()
        if kind == "word" and value.upper() in _KEYWORDS:
            kind = "keyword"
            value = value.upper()
        tokens.append(Token(kind, value))
    return tokens


# The expression tree. Each node knows how to evaluate itself against a row,
# which keeps the walk next to the shape it walks.


@dataclass(frozen=True)
class Column:
    """A column reference, with whatever qualified it.

    The qualifier is kept rather than discarded because a join needs it: u.id
    and o.id are two columns. It is tried first and the bare name second, so a
    single-table query writing dbo.people.id still finds id.
    """

    name: str
    qualified: str | None = None

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> object:
        for wanted in (self.qualified, self.name):
            if wanted is None:
                continue
            for key, value in row.items():
                if key.lower() == wanted.lower():
                    return value
        raise PredicateError(f"invalid column name '{self.qualified or self.name}'")


@dataclass(frozen=True)
class Arithmetic:
    """An operator with two operands, or one for a leading minus.

    Division by zero gives NULL rather than raising. SQL Server raises, but a
    query that dies partway through a scan leaves a client with neither an
    answer nor the rows it already had, and this is a read-only bridge.
    """

    operator: str
    left: object
    right: object

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> object:
        left = self.left.evaluate(row, params)
        right = self.right.evaluate(row, params)
        if left is None or right is None:
            return None
        return ARITHMETIC[self.operator](left, right)


@dataclass(frozen=True)
class Negate:
    operand: object

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> object:
        value = self.operand.evaluate(row, params)
        return None if value is None else -_number(value)


@dataclass(frozen=True)
class Case:
    """CASE, in both its forms.

    With an operand each WHEN is compared to it; without one each WHEN is a
    condition in its own right. Both stop at the first that holds, and a CASE
    with nothing matching and no ELSE is NULL.
    """

    branches: tuple
    otherwise: object = None
    operand: object = None

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> object:
        subject = self.operand.evaluate(row, params) if self.operand else None
        for test, result in self.branches:
            if self.operand is not None:
                against = test.evaluate(row, params)
                if subject is not None and against is not None and compare(
                    "=", subject, against
                ):
                    return result.evaluate(row, params)
            elif test.evaluate(row, params) is True:
                return result.evaluate(row, params)
        return self.otherwise.evaluate(row, params) if self.otherwise else None


@dataclass(frozen=True)
class Cast:
    """CAST(x AS type) and CONVERT(type, x), which mean the same thing here."""

    operand: object
    to: str

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> object:
        value = self.operand.evaluate(row, params)
        if value is None:
            return None
        convert = CAST_TYPES[self.to]
        try:
            if convert is int:
                return int(_number(value))
            if convert is float:
                return float(_number(value))
            if convert is bool:
                return bool(_number(value))
            return _text(value)
        except (PredicateError, ValueError):
            raise PredicateError(
                f"cannot convert {value!r} to {self.to}"
            ) from None


@dataclass(frozen=True)
class Call:
    """One of the functions in FUNCTIONS, applied to evaluated arguments."""

    function: str
    arguments: tuple

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> object:
        values = [argument.evaluate(row, params) for argument in self.arguments]
        try:
            return FUNCTIONS[self.function](*values)
        except PredicateError:
            raise
        except TypeError:
            raise PredicateError(
                f"{self.function} was given {len(values)} argument(s), which "
                f"is not a number of them it takes"
            ) from None
        except (ValueError, ArithmeticError) as exc:
            raise PredicateError(f"{self.function}: {exc}") from None


@dataclass(frozen=True)
class Aggregate:
    """An aggregate named in a HAVING, read back from the grouped row.

    A HAVING runs after the groups are formed, so the value it wants has
    already been computed. This looks it up rather than computing it again,
    which is also what keeps HAVING COUNT(*) and SELECT COUNT(*) in agreement.
    """

    function: str
    argument: str

    @property
    def key(self) -> str:
        return f"{self.function}({self.argument})"

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> object:
        for name, value in row.items():
            if name.lower() == self.key.lower():
                return value
        raise PredicateError(
            f"{self.key} is in the HAVING but not in the select list"
        )


@dataclass(frozen=True)
class Literal:
    value: object

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> object:
        return self.value


@dataclass(frozen=True)
class ParameterRef:
    name: str

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> object:
        for key, value in params.items():
            if key.lstrip("@").lower() == self.name.lstrip("@").lower():
                return value
        # An undeclared parameter is null rather than an error: clients send
        # clauses guarded by IS NULL precisely so an absent value is harmless.
        return None


_COMPARISONS = {
    "=":  lambda a, b: a == b,
    "<>": lambda a, b: a != b,
    "!=": lambda a, b: a != b,
    "<":  lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">":  lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


def collated(value: object) -> object:
    """A value as the declared collation compares it.

    Every text column here is declared SQL_Latin1_General_CP1_CI_AS, sort id
    0x34, which is case-insensitive and accent-sensitive. Comparing text
    exactly would mean telling a client one thing and doing another: WHERE
    name = 'ADA' finds ada on a real server with this collation.
    """
    return value.casefold() if isinstance(value, str) else value


def compare(operator: str, left: object, right: object) -> bool:
    """One comparison, under the collation and SQL's type coercion."""
    left, right = collated(left), collated(right)
    try:
        return _COMPARISONS[operator](left, right)
    except TypeError:
        # Comparing text with a number is not an error in SQL, it is a
        # conversion; falling back to text keeps a sensible answer.
        return _COMPARISONS[operator](str(left), str(right))


# LIKE, translated to a regular expression once and kept. A pattern usually
# comes from the query rather than the data, so the same one is used for every
# row of a scan.
@lru_cache(maxsize=256)
def like_pattern(pattern: str, escape: str | None) -> re.Pattern:
    """A LIKE pattern as a regular expression.

    T-SQL wildcards: % is any run of characters, _ is exactly one, and a
    bracketed set is one character from it, negated with a leading ^. An
    escape character, when the query names one, makes the next character
    literal whatever it is.
    """
    out = ["\\A"]
    at = 0
    while at < len(pattern):
        char = pattern[at]
        if escape and char == escape and at + 1 < len(pattern):
            out.append(re.escape(pattern[at + 1]))
            at += 2
            continue
        if char == "%":
            out.append(".*")
        elif char == "_":
            out.append(".")
        elif char == "[":
            end = pattern.find("]", at + 1)
            if end < 0:
                out.append(re.escape(char))
            else:
                body = pattern[at + 1:end]
                negated = body.startswith("^")
                if negated:
                    body = body[1:]
                # The body is a set of characters and ranges. Only ] and \
                # need escaping inside one.
                body = body.replace("\\", "\\\\").replace("]", "\\]")
                out.append(f"[{'^' if negated else ''}{body}]")
                at = end + 1
                continue
        else:
            out.append(re.escape(char))
        at += 1
    out.append("\\Z")
    return re.compile("".join(out), re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class Like:
    """value LIKE pattern, with the wildcards T-SQL defines."""

    operand: object
    pattern: object
    negated: bool = False
    escape: object = None

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> Ternary:
        value = self.operand.evaluate(row, params)
        pattern = self.pattern.evaluate(row, params)
        if value is None or pattern is None:
            return Unknown
        escape = None
        if self.escape is not None:
            found = self.escape.evaluate(row, params)
            escape = str(found)[:1] if found else None
        matched = bool(like_pattern(str(pattern), escape).match(str(value)))
        return not matched if self.negated else matched


@dataclass(frozen=True)
class In:
    """value IN (a, b, c).

    Unknown rather than false when the value is not found and one of the
    candidates is NULL, because NULL might have been the match.
    """

    operand: object
    values: tuple
    negated: bool = False

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> Ternary:
        value = self.operand.evaluate(row, params)
        if value is None:
            return Unknown
        unknown = False
        for candidate in self.values:
            found = candidate.evaluate(row, params)
            # A subquery binds the whole column to one parameter, so a single
            # entry in the list may itself be many values.
            offered = found if isinstance(found, (list, tuple)) else [found]
            for other in offered:
                if other is None:
                    unknown = True
                elif compare("=", value, other):
                    return not self.negated
        if unknown:
            return Unknown
        return self.negated


@dataclass(frozen=True)
class Between:
    """value BETWEEN low AND high, which is inclusive at both ends."""

    operand: object
    low: object
    high: object
    negated: bool = False

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> Ternary:
        value = self.operand.evaluate(row, params)
        low = self.low.evaluate(row, params)
        high = self.high.evaluate(row, params)
        if value is None or low is None or high is None:
            return Unknown
        inside = compare(">=", value, low) and compare("<=", value, high)
        return not inside if self.negated else inside


@dataclass(frozen=True)
class Comparison:
    left: object
    operator: str
    right: object

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> Ternary:
        left = self.left.evaluate(row, params)
        right = self.right.evaluate(row, params)
        if left is None or right is None:
            return Unknown
        return compare(self.operator, left, right)


@dataclass(frozen=True)
class IsNull:
    operand: object
    negated: bool = False

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> Ternary:
        # IS NULL is the one test that is never unknown: that is its purpose.
        is_null = self.operand.evaluate(row, params) is None
        return not is_null if self.negated else is_null


@dataclass(frozen=True)
class And:
    left: object
    right: object

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> Ternary:
        left = self.left.evaluate(row, params)
        if left is False:
            return False          # false and anything is false, even unknown
        right = self.right.evaluate(row, params)
        if right is False:
            return False
        if left is Unknown or right is Unknown:
            return Unknown
        return True


@dataclass(frozen=True)
class Or:
    left: object
    right: object

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> Ternary:
        left = self.left.evaluate(row, params)
        if left is True:
            return True           # true or anything is true, even unknown
        right = self.right.evaluate(row, params)
        if right is True:
            return True
        if left is Unknown or right is Unknown:
            return Unknown
        return False


@dataclass(frozen=True)
class Not:
    operand: object

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> Ternary:
        value = self.operand.evaluate(row, params)
        return Unknown if value is Unknown else not value


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.at = 0

    def peek(self) -> Token | None:
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def take(self) -> Token:
        token = self.peek()
        if token is None:
            raise PredicateError("expression ended early")
        self.at += 1
        return token

    def accept(self, kind: str, text: str | None = None) -> Token | None:
        token = self.peek()
        if token and token.kind == kind and (text is None or token.text == text):
            return self.take()
        return None

    def parse(self) -> object:
        node = self.parse_or()
        if self.peek():
            raise PredicateError(f"unexpected {self.peek().text!r} in the condition")
        return node

    def parse_or(self) -> object:
        node = self.parse_and()
        while self.accept("keyword", "OR"):
            node = Or(node, self.parse_and())
        return node

    def parse_and(self) -> object:
        node = self.parse_not()
        while self.accept("keyword", "AND"):
            node = And(node, self.parse_not())
        return node

    def parse_not(self) -> object:
        if self.accept("keyword", "NOT"):
            return Not(self.parse_not())
        return self.parse_primary()

    def parse_primary(self) -> object:
        if self.accept("punct", "("):
            node = self.parse_or()
            if not self.accept("punct", ")"):
                raise PredicateError("a bracket was opened and not closed")
            return node

        left = self.parse_operand()

        if self.accept("keyword", "IS"):
            negated = bool(self.accept("keyword", "NOT"))
            if not self.accept("keyword", "NULL"):
                raise PredicateError("IS must be followed by NULL or NOT NULL")
            return IsNull(left, negated)

        # NOT sits between the value and the operator in these three, unlike
        # every other place it appears.
        negated = bool(self.accept("keyword", "NOT"))
        for keyword, build in (
            ("LIKE", self.parse_like),
            ("IN", self.parse_in),
            ("BETWEEN", self.parse_between),
        ):
            if self.accept("keyword", keyword):
                return build(left, negated)
        if negated:
            raise PredicateError(
                "NOT here must be followed by LIKE, IN or BETWEEN"
            )

        operator = self.accept("operator")
        if not operator:
            token = self.peek()
            raise PredicateError(
                f"expected a comparison after {getattr(left, 'name', left)!r}"
                + (f", found {token.text!r}" if token else "")
            )
        return Comparison(left, operator.text, self.parse_operand())

    def parse_like(self, left: object, negated: bool) -> object:
        pattern = self.parse_operand()
        escape = self.parse_operand() if self.accept("keyword", "ESCAPE") else None
        return Like(left, pattern, negated, escape)

    def parse_in(self, left: object, negated: bool) -> object:
        if not self.accept("punct", "("):
            raise PredicateError("IN must be followed by a bracketed list")
        values = []
        while True:
            values.append(self.parse_operand())
            if self.accept("punct", ","):
                continue
            if self.accept("punct", ")"):
                break
            token = self.peek()
            raise PredicateError(
                "IN list expects a comma or a closing bracket"
                + (f", found {token.text!r}" if token else "")
            )
        if not values:
            raise PredicateError("IN needs at least one value")
        return In(left, tuple(values), negated)

    def parse_between(self, left: object, negated: bool) -> object:
        low = self.parse_operand()
        if not self.accept("keyword", "AND"):
            raise PredicateError("BETWEEN needs AND between its two bounds")
        return Between(left, low, self.parse_operand(), negated)

    def parse_operand(self) -> object:
        """An expression that produces a value rather than a truth."""
        node = self.parse_term()
        while True:
            operator = self.peek()
            if (operator and operator.kind == "operator"
                    and operator.text in ("+", "-", "||")):
                self.take()
                node = Arithmetic(operator.text, node, self.parse_term())
                continue
            return node

    def parse_term(self) -> object:
        node = self.parse_unary()
        while True:
            operator = self.peek()
            if (operator and operator.kind == "operator"
                    and operator.text in ("*", "/", "%")):
                self.take()
                node = Arithmetic(operator.text, node, self.parse_unary())
                continue
            return node

    def parse_unary(self) -> object:
        operator = self.peek()
        if operator and operator.kind == "operator" and operator.text in ("-", "+"):
            self.take()
            operand = self.parse_unary()
            return Negate(operand) if operator.text == "-" else operand
        return self.parse_value()

    def parse_case(self) -> object:
        """CASE, either compared against an operand or a run of conditions."""
        operand = None
        if not (self.peek() and self.peek().kind == "keyword"
                and self.peek().text == "WHEN"):
            operand = self.parse_operand()

        branches = []
        while self.accept("keyword", "WHEN"):
            test = self.parse_operand() if operand is not None else self.parse_or()
            if not self.accept("keyword", "THEN"):
                raise PredicateError("each WHEN in a CASE needs a THEN")
            branches.append((test, self.parse_operand()))
        if not branches:
            raise PredicateError("a CASE needs at least one WHEN")

        otherwise = self.parse_operand() if self.accept("keyword", "ELSE") else None
        if not self.accept("keyword", "END"):
            raise PredicateError("a CASE must be closed with END")
        return Case(tuple(branches), otherwise, operand)

    def parse_cast(self, reversed_arguments: bool) -> object:
        """CAST(x AS type), or CONVERT(type, x), which says it the other way."""
        if not self.accept("punct", "("):
            raise PredicateError("CAST and CONVERT need brackets")
        if reversed_arguments:
            to = self._type_name()
            if not self.accept("punct", ","):
                raise PredicateError("CONVERT needs a comma after the type")
            operand = self.parse_operand()
            while self.accept("punct", ","):        # a style, which is ignored
                self.parse_operand()
        else:
            operand = self.parse_operand()
            if not self.accept("keyword", "AS"):
                raise PredicateError("CAST needs AS between the value and the type")
            to = self._type_name()
        if not self.accept("punct", ")"):
            raise PredicateError("CAST( was opened and not closed")
        return Cast(operand, to)

    def _type_name(self) -> str:
        token = self.take()
        name = token.text.upper()
        if name not in CAST_TYPES:
            raise PredicateError(
                f"'{token.text}' is not a type this converts to; it has "
                f"{', '.join(sorted(set(CAST_TYPES)))}"
            )
        if self.accept("punct", "("):               # a size, which is ignored
            while not self.accept("punct", ")"):
                if self.peek() is None:
                    raise PredicateError(f"{name}( was opened and not closed")
                self.take()
        return name

    def parse_value(self) -> object:
        token = self.take()

        if token.kind == "number":
            text = token.text
            return Literal(float(text) if any(c in text for c in ".eE") else int(text))
        if token.kind == "string":
            body = token.text.lstrip("Nn")[1:-1]
            return Literal(body.replace("''", "'"))
        if token.kind == "param":
            return ParameterRef(token.text)
        if token.kind == "bracketed":
            return self._qualified(token.text[1:-1].replace("]]", "]"))
        if token.kind == "quoted":
            return self._qualified(token.text[1:-1].replace('""', '"'))
        if token.kind == "keyword" and token.text in ("TRUE", "FALSE"):
            return Literal(token.text == "TRUE")
        if token.kind == "keyword" and token.text == "NULL":
            return Literal(None)
        if token.kind == "keyword" and token.text == "CASE":
            return self.parse_case()
        if token.kind == "word" and token.text.upper() in ("CAST", "CONVERT"):
            return self.parse_cast(token.text.upper() == "CONVERT")
        if token.kind == "punct" and token.text == "(":
            inner = self.parse_operand()
            if not self.accept("punct", ")"):
                raise PredicateError("a bracket was opened and not closed")
            return inner
        if token.kind == "word":
            following = self.peek()
            if following and following.kind == "punct" and following.text == "(":
                name = token.text.upper()
                if name in FUNCTIONS:
                    return self._call(name)
                return self._aggregate(token.text)
            return self._qualified(token.text)

        raise PredicateError(f"expected a value, found {token.text!r}")

    def parse_argument(self) -> object:
        """An argument, which may be a condition rather than a value.

        IIF takes one, and there is no way to tell which a function wants
        without knowing the function, so both are tried. The condition first,
        because a value that happens to parse as one is a comparison and
        should be read as what it says.
        """
        mark = self.at
        try:
            return self.parse_or()
        except PredicateError:
            self.at = mark
            return self.parse_operand()

    def _call(self, function: str) -> Call:
        self.take()                                   # the opening bracket
        arguments = []
        if not self.accept("punct", ")"):
            while True:
                arguments.append(self.parse_argument())
                if self.accept("punct", ","):
                    continue
                if self.accept("punct", ")"):
                    break
                raise PredicateError(f"{function}( was opened and not closed")
        return Call(function, tuple(arguments))

    def _aggregate(self, function: str) -> Aggregate:
        """An aggregate call, read back from the row a group produced."""
        if function.upper() not in AGGREGATE_NAMES:
            raise PredicateError(
                f"'{function}' is not a function this server knows; it has "
                f"{', '.join(sorted(FUNCTIONS))} and the aggregates "
                f"{', '.join(sorted(AGGREGATE_NAMES))}"
            )
        self.take()                                   # the opening bracket
        argument = "*"
        if self.accept("operator", "*"):
            pass
        else:
            token = self.peek()
            if token and not (token.kind == "punct" and token.text == ")"):
                inner = self.parse_operand()
                argument = getattr(inner, "name", None) or str(
                    getattr(inner, "value", "")
                )
        if not self.accept("punct", ")"):
            raise PredicateError(f"{function}( was opened and not closed")
        return Aggregate(function.upper(), argument)

    def _qualified(self, name: str) -> Column:
        """Read a dotted reference, keeping both the last part and the whole."""
        parts = [name]
        while self.accept("punct", "."):
            part = self.take()
            if part.kind == "bracketed":
                parts.append(part.text[1:-1].replace("]]", "]"))
            elif part.kind == "quoted":
                parts.append(part.text[1:-1].replace('""', '"'))
            else:
                parts.append(part.text)
        # Only the last two matter: a joined column is named alias.column, and
        # anything in front of that is a schema or database.
        return Column(parts[-1], ".".join(parts[-2:]) if len(parts) > 1 else None)


def parse_expression(text: str) -> object:
    """Parse a value expression: arithmetic, CASE, CAST, a function, a column."""
    tokens = tokenize(text)
    if not tokens:
        raise PredicateError("empty expression")
    parser = _Parser(tokens)
    node = parser.parse_operand()
    if parser.peek():
        raise PredicateError(f"unexpected {parser.peek().text!r} in the expression")
    return node


def parse_predicate(text: str) -> object:
    """Parse a WHERE condition into something that can evaluate a row."""
    tokens = tokenize(text)
    if not tokens:
        raise PredicateError("empty condition")
    return _Parser(tokens).parse()


def matches(
    node: object, row: Mapping[str, object], parameters: Mapping[str, object]
) -> bool:
    """Whether a row satisfies the condition.

    Only true passes. Unknown does not, which is what SQL does and why a
    comparison against NULL filters a row out rather than keeping it.
    """
    return node.evaluate(row, parameters) is True
