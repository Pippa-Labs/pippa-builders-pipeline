"""Evaluates the GitHub Actions expressions of reusable-hack-publish.yml the way GitHub does.

Enough of the language for the workflow's `if` conditions and `with` values: literals ('text',
numbers, true, false, null), context paths looked up in a flat dict such as
{"github.actor": "x", "needs.check.result": "success"} (a missing one is null), `!`, `==`, `!=`,
`<`, `<=`, `>`, `>=`, `&&`, `||`, parentheses, and the functions startsWith, endsWith, contains,
success, failure, cancelled and always.

As on GitHub: `&&` and `||` return one of their operands, strings compare without regard to case,
values of different types are compared as numbers (null, '' and false are 0), and an `if` that
calls no status function gets an implicit `success() &&`. A parser of its own, never `eval`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

STATUS_FUNCTIONS = ("success", "failure", "cancelled", "always")
_STATUS_CALL = re.compile(r"\b(?:success|failure|cancelled|always)\s*\(")
_TOKEN = re.compile(r"""
    \s*(?:
        (?P<string>'(?:[^']|'')*')
      | (?P<number>\d+(?:\.\d+)?)
      | (?P<op>&&|\|\||==|!=|<=|>=|<|>|!|\(|\)|,|\.)
      | (?P<name>[A-Za-z_][A-Za-z0-9_-]*)
    )""", re.VERBOSE)
_LITERALS = {"true": True, "false": False, "null": None}


@dataclass(frozen=True)
class Status:
    """What the status functions return: for a job, the results of the jobs it needs."""

    success: bool = True
    failure: bool = False
    cancelled: bool = False

    @classmethod
    def of(cls, *results: str) -> "Status":
        """The status of a job whose needed jobs ended with these results."""
        return cls(success=all(result == "success" for result in results),
                   failure=any(result == "failure" for result in results))


def _tokens(text: str) -> list[tuple[str, str]]:
    found, position = [], 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if not match or match.end() == position:
            if text[position:].strip():
                raise ValueError(f"cannot read the expression at {text[position:]!r}")
            break
        kind = match.lastgroup
        found.append((kind, match.group(kind)))
        position = match.end()
    return found


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    raise TypeError(f"no expression type for {value!r}")


def _number(value: Any) -> float:
    kind = _kind(value)
    if kind == "null":
        return 0.0
    if kind in ("boolean", "number"):
        return float(value)
    if not value.strip():
        return 0.0
    try:
        return float(value.strip())
    except ValueError:
        return math.nan


def _text(value: Any) -> str:
    kind = _kind(value)
    if kind == "null":
        return ""
    if kind == "boolean":
        return "true" if value else "false"
    if kind == "number":
        return format(value, "g")
    return value


def truthy(value: Any) -> bool:
    kind = _kind(value)
    if kind == "null":
        return False
    if kind == "number":
        return value != 0 and not math.isnan(value)
    return bool(value)


def _equal(left: Any, right: Any) -> bool:
    if _kind(left) == _kind(right):
        if isinstance(left, str):
            return left.casefold() == right.casefold()
        return left == right
    return _number(left) == _number(right)


def _compare(left: Any, right: Any, op: str) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        a, b = left.casefold(), right.casefold()
    else:
        a, b = _number(left), _number(right)
        if math.isnan(a) or math.isnan(b):
            return False
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]


class _Parser:
    def __init__(self, text: str, context: dict[str, Any], status: Status) -> None:
        self.tokens = _tokens(text)
        self.position = 0
        self.context = context
        self.status = status

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self, value: str | None = None) -> tuple[str, str]:
        token = self.peek()
        if token is None or (value is not None and token[1] != value):
            raise ValueError(f"expected {value or 'a value'}, found {token}")
        self.position += 1
        return token

    def at(self, *values: str) -> bool:
        token = self.peek()
        return token is not None and token[0] == "op" and token[1] in values

    def parse(self) -> Any:
        value = self.either()
        if self.peek() is not None:
            raise ValueError(f"unexpected {self.peek()}")
        return value

    def either(self) -> Any:
        value = self.both()
        while self.at("||"):
            self.take()
            right = self.both()
            value = value if truthy(value) else right
        return value

    def both(self) -> Any:
        value = self.equality()
        while self.at("&&"):
            self.take()
            right = self.equality()
            value = right if truthy(value) else value
        return value

    def equality(self) -> Any:
        value = self.comparison()
        while self.at("==", "!="):
            op = self.take()[1]
            right = self.comparison()
            value = _equal(value, right) == (op == "==")
        return value

    def comparison(self) -> Any:
        value = self.unary()
        while self.at("<", "<=", ">", ">="):
            op = self.take()[1]
            value = _compare(value, self.unary(), op)
        return value

    def unary(self) -> Any:
        if self.at("!"):
            self.take()
            return not truthy(self.unary())
        return self.primary()

    def primary(self) -> Any:
        kind, value = self.take()
        if kind == "op" and value == "(":
            inner = self.either()
            self.take(")")
            return inner
        if kind == "string":
            return value[1:-1].replace("''", "'")
        if kind == "number":
            return float(value)
        if kind != "name":
            raise ValueError(f"unexpected {value!r}")
        if value in _LITERALS:
            return _LITERALS[value]
        if self.at("("):
            return self.call(value)
        path = [value]
        while self.at("."):
            self.take()
            path.append(self.take()[1])
        return self.context.get(".".join(path))

    def call(self, name: str) -> Any:
        self.take("(")
        arguments = []
        while not self.at(")"):
            arguments.append(self.either())
            if not self.at(")"):
                self.take(",")
        self.take(")")
        if name in STATUS_FUNCTIONS:
            if arguments:
                raise ValueError(f"{name}() takes no arguments")
            return True if name == "always" else getattr(self.status, name)
        if name in ("startsWith", "endsWith", "contains") and len(arguments) == 2:
            haystack, needle = (_text(item).casefold() for item in arguments)
            return {"startsWith": haystack.startswith, "endsWith": haystack.endswith,
                    "contains": haystack.__contains__}[name](needle)
        raise ValueError(f"unknown function {name}()")


def _inner(expression: str) -> str:
    text = expression.strip()
    if text.startswith("${{") and text.endswith("}}") and text.count("${{") == 1:
        return text[3:-2]
    return text


def evaluate(expression: str, context: dict[str, Any], status: Status = Status()) -> Any:
    """The value of an expression, with or without its `${{ }}`."""
    return _Parser(_inner(expression), context, status).parse()


def runs(condition: str, context: dict[str, Any], status: Status = Status()) -> bool:
    """Whether a job or step with this `if` runs: GitHub adds `success() &&` when no status function is called."""
    inner = _inner(condition)
    value = truthy(evaluate(inner, context, status))
    return value if _STATUS_CALL.search(inner) else status.success and value
