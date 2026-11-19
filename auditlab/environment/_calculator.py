"""Decimal expressions bounded to 256 characters and 24 levels of nesting.

Arithmetic uses 64 significant digits with ROUND_HALF_UP. Nonzero values
require an adjusted exponent between -63 and 63, inclusive.
"""

import re
from decimal import (
    ROUND_HALF_UP, Context, Decimal, InvalidOperation, Overflow, Subnormal, localcontext,
)


MAX_EXPRESSION_LENGTH = 256
MAX_EXPRESSION_DEPTH = 24
PRECISION = 64
MIN_ADJUSTED_EXPONENT = -63
MAX_ADJUSTED_EXPONENT = 63
_TOKEN = re.compile(r"[0-9]+(?:\.[0-9]*)?|\.[0-9]+|[()+*/-]")


def _tokens(expression: str) -> list[str]:
    tokens = []
    position = 0
    while position < len(expression):
        if expression[position] in " \t\r\n":
            position += 1
            continue
        match = _TOKEN.match(expression, position)
        if match is None:
            raise ValueError("Unsupported token")
        tokens.append(match[0])
        position = match.end()
    return tokens


def _bounded(value: Decimal) -> Decimal:
    if value and not MIN_ADJUSTED_EXPONENT <= value.adjusted() <= MAX_ADJUSTED_EXPONENT:
        raise OverflowError("Decimal result outside the supported range")
    return value


class _Parser:
    def __init__(self, tokens: list[str]):
        self.tokens = iter(tokens)
        self.current = next(self.tokens, None)

    def advance(self) -> str | None:
        token = self.current
        self.current = next(self.tokens, None)
        return token

    def sum(self, depth: int) -> Decimal:
        value = self.product(depth)
        while self.current in ("+", "-"):
            operator = self.advance()
            right = self.product(depth)
            value = _bounded(value + right if operator == "+" else value - right)
        return value

    def product(self, depth: int) -> Decimal:
        value = self.atom(depth)
        while self.current in ("*", "/"):
            operator = self.advance()
            right = self.atom(depth)
            if operator == "/" and right.is_zero():
                raise ZeroDivisionError
            value = _bounded(value * right if operator == "*" else value / right)
        return value

    def atom(self, depth: int) -> Decimal:
        if depth > MAX_EXPRESSION_DEPTH:
            raise ValueError("Expression nested too deeply")
        token = self.advance()
        if token in ("+", "-"):
            value = self.atom(depth + 1)
            return -value if token == "-" else value
        if token == "(":
            value = self.sum(depth + 1)
            if self.advance() != ")":
                raise ValueError("Missing closing parenthesis")
            return value
        if token is None or token in (")", "*", "/"):
            raise ValueError("Missing number")
        if len(token.replace(".", "")) > PRECISION:
            raise OverflowError("Decimal literal has too many digits")
        return _bounded(Decimal(token))


def calculate(expression: str) -> str:
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ValueError("Expression too long")
    parser = _Parser(_tokens(expression))
    context = Context(
        prec=PRECISION, rounding=ROUND_HALF_UP,
        Emin=MIN_ADJUSTED_EXPONENT, Emax=MAX_ADJUSTED_EXPONENT,
        flags=[], traps=[InvalidOperation, Overflow, Subnormal], clamp=0,
    )
    with localcontext(context):
        value = parser.sum(0)
    if parser.current is not None:
        raise ValueError("Unexpected trailing token")
    if value.is_zero():
        return "0"
    fixed = format(value, "f")
    return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
