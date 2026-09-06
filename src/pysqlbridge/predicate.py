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

import re
from dataclasses import dataclass
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
    | (?P<operator> <> | != | >= | <= | = | < | > )
    | (?P<punct>    [(),.] )
    | (?P<word>     [A-Za-z_@#][A-Za-z0-9_@#$]* )
    """,
    re.VERBOSE,
)

_KEYWORDS = {"AND", "OR", "NOT", "IS", "NULL", "TRUE", "FALSE"}


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
    name: str

    def evaluate(self, row: Mapping[str, object], params: Mapping[str, object]) -> object:
        for key, value in row.items():
            if key.lower() == self.name.lower():
                return value
        raise PredicateError(f"invalid column name '{self.name}'")


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
        try:
            return _COMPARISONS[self.operator](left, right)
        except TypeError:
            # Comparing text with a number is not an error in SQL, it is a
            # conversion; falling back to text keeps a sensible answer.
            return _COMPARISONS[self.operator](str(left), str(right))


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

        operator = self.accept("operator")
        if not operator:
            token = self.peek()
            raise PredicateError(
                f"expected a comparison after {getattr(left, 'name', left)!r}"
                + (f", found {token.text!r}" if token else "")
            )
        return Comparison(left, operator.text, self.parse_operand())

    def parse_operand(self) -> object:
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
        if token.kind == "word":
            return self._qualified(token.text)

        raise PredicateError(f"expected a value, found {token.text!r}")

    def _qualified(self, name: str) -> Column:
        """Swallow any table qualifier: only the last part names a column."""
        while self.accept("punct", "."):
            part = self.take()
            if part.kind == "bracketed":
                name = part.text[1:-1].replace("]]", "]")
            elif part.kind == "quoted":
                name = part.text[1:-1].replace('""', '"')
            else:
                name = part.text
        return Column(name)


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
