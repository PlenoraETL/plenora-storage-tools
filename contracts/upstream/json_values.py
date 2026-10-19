"""Equality of JSON data by type and by value, in three contexts.

Python's `==` is not JSON equality: `1 == True == 1.0`, `0 == False`, also
inside a list, a tuple or a dictionary, and `{1} == {True}`. A check that
compares two documents, or a document with an expected value, with `==`
takes a `const` 1 changed into `true`, or a version `1` into `1.0`, for the
same value and passes in silence.

Numbers are compared in one of three contexts, which every caller names
(with a comment saying which and why):

- `same_document` / `document_key`: published documents compared with one
  another and nothing else (the immutability gate, catalog identities and
  fields across versions or between two catalogs, digests). COMPATIBILITY.md
  forbids modifying a published vector, and a consumer reads each number as
  written: the sign of a zero is data (RA-003 keeps it), and `1.0` against
  `1.00`, `10e-1` or `1.0000000000000001` is another number. A number read
  by `loads` keeps its exact text (`JsonNumber`) and is compared by it; no
  spelling is tolerated. (A whole published document that may not grow is
  compared by its bytes, without parsing: `check_schema_immutability`.)
- `same_schema_value` / `schema_value_key`: the numeric assertions of a
  JSON Schema, which compares numbers by value: `1.0`, `1.00` and `1e0`,
  `0.0` and `-0.0` validate the same instances (COMPATIBILITY.md: a schema
  edit that cannot alter validation is compatible).
- `same_value`: every other comparison, of values (two fields of one
  vector, an expectation against what a rule computes, a field against the
  value a rule requires): numbers by exact value, so `-0` is the integer 0
  and `1.00` equals `1.0`, while a binary float keeps the sign of its zero
  (RA-003).

In all three contexts an integer and a decimal stay two values (`1`
against `1.0`), and `1.0` and `1.0000000000000001` two numbers.

Both walk the data with an explicit stack, never by recursion: a document
nested 10,000 levels deep is compared like a flat one, in time linear in its
size (plus the sort of each object's member names for the keys); no subtree
is serialized.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any


class JsonNumber(Decimal):
    """A JSON number as written: its exact value (a `Decimal`, so JSON
    Schema and arithmetic read it as a number) and its exact text, which
    `same_document` compares. `parse_float` gives it for every number with a
    fraction or an exponent: a decimal literal, never an integer one."""

    def __new__(cls, text: str) -> "JsonNumber":
        number = super().__new__(cls, text)
        number.text = text
        return number

    def __reduce__(self):
        return (type(self), (self.text,))

    def __copy__(self) -> "JsonNumber":
        return self

    def __deepcopy__(self, memo: Any) -> "JsonNumber":
        return self


class JsonInteger(int):
    """An integer literal whose text an `int` would lose: `-0`, the only
    one JSON allows (no leading zero, no `+`). It is an `int` of value 0 for
    every consumer, as the literal is an integer (RA-005: `-0` is the
    integer 0, never a decimal and never -0.0); its text stays for the
    exact comparison of a document."""

    def __new__(cls, text: str) -> "JsonInteger":
        number = super().__new__(cls, int(text))
        number.text = text
        return number

    def __reduce__(self):
        return (type(self), (self.text,))

    def __copy__(self) -> "JsonInteger":
        return self

    def __deepcopy__(self, memo: Any) -> "JsonInteger":
        return self


def parse_integer(text: str) -> int:
    """Every integer literal is an `int`; `-0` keeps its text in a
    `JsonInteger`, the integer 0."""
    value = int(text)
    return JsonInteger(text) if str(value) != text else value


def number_text(value: Any) -> str:
    """The exact text of a parsed number; `str` of any other number."""
    return value.text if isinstance(value, (JsonNumber, JsonInteger)) else str(value)


def loads(text: str, **options: Any) -> Any:
    """`json.loads` with exact numbers: every decimal a `JsonNumber`, every
    integer an `int` (`-0` a `JsonInteger`, the integer 0 with its text).
    No binary float is produced."""
    return json.loads(text, parse_float=JsonNumber, parse_int=parse_integer, **options)


class _Member:
    """A member name on the stack of `_key`, told apart from the values."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


DOCUMENT, SCHEMA, VALUE = "document", "schema", "value"


def _scalar(value: Any, mode: str) -> tuple[str, Any] | None:
    """The typed image of a scalar in its context, None for an array or an
    object; anything that is not JSON data raises `TypeError`: comparing it
    would guess. A number's image:

    - DOCUMENT: its exact text (`-0` against `0`, `1.0` against `1.00`
      differ); a binary float by `repr`;
    - SCHEMA: its exact value, integer and decimal kept apart (`1.0 ==
      1.00`, `-0 == 0`, `-0.0 == 0.0`; `1` against `1.0` differ);
    - VALUE: as SCHEMA, but a binary float by `repr`, so the sign of a zero
      the validator computed (RA-003) and its exact binary value count.
    """
    if value is None:
        return ("null", None)
    kind = type(value)
    if kind is bool:
        return ("boolean", value)
    if kind is int:
        return ("integer", value)
    if kind is JsonInteger:
        # `-0`: the integer 0 as a value, its own text in a document.
        return ("integer-text", value.text) if mode == DOCUMENT else ("integer", int(value))
    if kind is float:
        if mode == SCHEMA:
            # -0.0 == 0.0 (and they hash alike); a NaN is the same as itself.
            return ("float", value if value == value else "nan")
        # `repr` tells -0.0 from 0.0; a NaN is the same as itself.
        return ("float", repr(value))
    if kind is Decimal or kind is JsonNumber:
        return ("decimal", number_text(value) if mode == DOCUMENT else decimal_value(value))
    if kind is str:
        return ("string", value)
    if kind in (list, tuple, dict):
        return None
    raise TypeError(f"not JSON data: {kind.__name__}")


def decimal_representation(value: Decimal) -> tuple:
    """The exact representation of a decimal as parsed: sign, coefficient
    digits and exponent, as written (`-0.0`, `1.00` kept)."""
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        # NaN or infinity, which JSON does not have: compared as written.
        return ("special", str(value))
    return (sign, tuple(digits), exponent)


def decimal_value(value: Decimal) -> tuple:
    """The exact value of a decimal, independent of its spelling and of any
    context precision (`Decimal.normalize` would round to 28 digits): sign,
    digits without trailing zeros and exponent; every zero is one value."""
    image = decimal_representation(value)
    if image[0] == "special":
        return image
    sign, digits, exponent = image
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if digits == [0]:
        return (0, (0,), 0)
    return (sign, tuple(digits), exponent)


def decimal_text(value: Decimal) -> str:
    """A JSON number text of the exact value of a decimal (the digest of
    schema assertions, compared by value): `1.0`, `1.00` and `10e-1` have
    one text, `1.0000000000000001` another."""
    image = decimal_value(value)
    if image[0] == "special":
        raise TypeError("not JSON data: a decimal that is not a finite number")
    sign, digits, exponent = image
    # Always with an exponent: an integer is never written so, and stays
    # another value (`1` against `1.0`).
    return ("-" if sign else "") + "".join(map(str, digits)) + f"E{exponent}"


def canonical_text(value: Any, sort_keys: bool = True, by_value: bool = True) -> str:
    """Compact JSON written with an explicit stack (a value deeper than the
    recursion limit is written like a flat one). With sorted member names
    and without decimal numbers it is the text of `json.dumps(value,
    sort_keys=True, separators=(",", ":"))`. A decimal is written by its
    value (`decimal_text`) or, with `by_value=False`, by its exact text; a
    binary float as `json.dumps` writes it. Unsorted, it keeps the member
    order: a key for data whose order matters."""
    parts: list[str] = []
    stack: list[Any] = [value]
    close = object()
    while stack:
        item = stack.pop()
        if type(item) is tuple and len(item) == 2 and item[0] is close:
            parts.append(item[1])
        elif item is None or type(item) in (bool, int, str, float):
            parts.append(json.dumps(item))
        elif type(item) is JsonInteger:
            parts.append(json.dumps(int(item)) if by_value else item.text)
        elif type(item) is Decimal or type(item) is JsonNumber:
            parts.append(decimal_text(item) if by_value else number_text(item))
        elif type(item) is dict:
            names = _names(item)
            if sort_keys:
                names.sort()
            parts.append("{")
            stack.append((close, "}"))
            for index in range(len(names) - 1, -1, -1):
                stack.append(item[names[index]])
                stack.append((close, json.dumps(names[index]) + ":"))
                if index:
                    stack.append((close, ","))
        elif type(item) in (list, tuple):
            parts.append("[")
            stack.append((close, "]"))
            for index in range(len(item) - 1, -1, -1):
                stack.append(item[index])
                if index:
                    stack.append((close, ","))
        else:
            raise TypeError(f"not JSON data: {type(item).__name__}")
    return "".join(parts)


def _names(value: dict) -> list[str]:
    for name in value:
        if type(name) is not str:
            raise TypeError("not JSON data: an object member name that is not a string")
    return list(value)


def _same(left: Any, right: Any, mode: str) -> bool:
    stack = [(left, right)]
    while stack:
        one, other = stack.pop()
        image = _scalar(one, mode)
        if image is not None:
            if image != _scalar(other, mode):
                return False
            continue
        if type(one) is dict:
            if type(other) is not dict or len(one) != len(other):
                _scalar(other, mode)
                return False
            _names(other)
            for name in _names(one):
                # A string name is never equal to a name of another type, so
                # equal sizes and inclusion make the same names.
                if name not in other:
                    return False
                stack.append((one[name], other[name]))
            continue
        if type(other) not in (list, tuple):
            _scalar(other, mode)
            return False
        if len(one) != len(other):
            return False
        stack.extend(zip(one, other))
    return True


def _key(value: Any, mode: str) -> tuple:
    tokens: list[tuple] = []
    stack: list[Any] = [value]
    while stack:
        item = stack.pop()
        if type(item) is _Member:
            tokens.append(("name", item.name))
            continue
        image = _scalar(item, mode)
        if image is not None:
            tokens.append(image)
        elif type(item) is dict:
            names = sorted(_names(item))
            tokens.append(("object", len(names)))
            for name in reversed(names):
                stack.append(item[name])
                stack.append(_Member(name))
        else:
            tokens.append(("array", len(item)))
            stack.extend(reversed(item))
    return tuple(tokens)


def same_document(left: Any, right: Any) -> bool:
    """The same published data: same types at every depth and every number
    with its exact representation. The members of an object compare
    whatever their order, as in JSON; an array and a tuple are both arrays."""
    return _same(left, right, DOCUMENT)


def document_key(value: Any) -> tuple:
    """A hashable image of published data: the same image only when
    `same_document` holds. Flat (every array and object states its size),
    so hashing it never recurses."""
    return _key(value, DOCUMENT)


def same_schema_value(left: Any, right: Any) -> bool:
    """The same schema assertions: as `same_document`, but every number by
    its exact value (`1.0 == 1.00 == 1e0`, `0.0 == -0.0`)."""
    return _same(left, right, SCHEMA)


def schema_value_key(value: Any) -> tuple:
    """A hashable image of schema assertions: the same image only when
    `same_schema_value` holds."""
    return _key(value, SCHEMA)


def same_value(left: Any, right: Any) -> bool:
    """The same values, outside the comparison of published documents (a
    vector's fields against each other, an expectation against what a rule
    computes): numbers by exact value with integer and decimal kept apart
    (`-0` is the integer 0), a binary float by `repr` (the sign of a zero
    counts); members whatever their order."""
    return _same(left, right, VALUE)
