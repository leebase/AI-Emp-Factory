"""Exact, bounded JSON <-> Python conversions for arbitrary-precision integers.

Every validated ledger integer is exact evidence: token counters are
arbitrary-precision JSON integers, and nothing in this module truncates,
rounds, floats, or rewrites one. CPython >= 3.11 nevertheless refuses decimal
``int <-> str`` conversions beyond ``sys.get_int_max_str_digits()`` digits
(4300 by default, a quadratic-conversion defense). A single validated counter
at the P1 reproducer scale (``9 * 10**4299``) already sits at that ceiling and
any aggregate of such counters goes past it, so the stock ``json.dumps`` /
``json.loads`` pair fails closed on evidence the schema accepts.

JSON itself has no such limit: an integer literal may carry any number of
digits. The ceiling is a Python conversion detail, not a data limitation, so
the honest repair is to compute both conversions in bounded chunks instead of
mutating the process-wide interpreter setting (``sys.set_int_max_str_digits``
is never called here) or degrading huge integers to floats or strings:

- to text: repeated ``divmod`` by ``10**chunk``; each per-chunk ``str`` is at
  most ``chunk`` digits, far below the configured ceiling;
- from text: Horner-style accumulation of bounded ``int(chunk)`` conversions.

The chunk size is derived from the ceiling read at call time (a limit of 0
means the interpreter disabled it, in which case a fixed chunk is used for
deterministic output). Text output therefore never depends on the ambient
limit beyond that derivation, and no conversion anywhere can raise the
limit's :class:`ValueError`.

:func:`dump_json` is a strict-JSON serializer with ``json.dumps``-compatible
output for every value the stock encoder accepts (compact separators,
``ensure_ascii=False`` escaping delegated to :mod:`json` for strings and
``float.__repr__`` for finite floats, ``allow_nan=False`` strictness), plus
exact arbitrary-precision integers. Unsupported types raise the same
:class:`TypeError` :func:`json.dumps` would, and circular containers raise the
same :class:`ValueError`, so callers see one uniform contract.
"""

from __future__ import annotations

import json
import math
import sys
from typing import Any

__all__ = [
    "dump_json",
    "int_from_decimal",
    "int_to_decimal",
]

#: Chunk size used when the interpreter has disabled the digit limit (0).
_FIXED_CHUNK_DIGITS = 1024

#: Digits of headroom kept below the configured limit in every chunk.
_HEADROOM_DIGITS = 32


def _chunk_digits() -> int:
    """Return the decimal digit budget of one bounded conversion chunk.

    The interpreter's configured ceiling is read at call time, never at
    import time, so a caller that legitimately resized the limit sees the
    change. ``max(1, ...)`` keeps the chunk valid even if a future CPython
    relaxes the 640-digit minimum; a disabled limit (0) uses the fixed chunk
    so output and behaviour stay deterministic either way.
    """
    get_limit = getattr(sys, "get_int_max_str_digits", None)
    limit = get_limit() if get_limit is not None else 0
    if limit <= 0:
        return _FIXED_CHUNK_DIGITS
    return max(1, limit - _HEADROOM_DIGITS)


def int_to_decimal(value: int) -> str:
    """Return the exact decimal digits of any integer as a ``str``.

    Unlike ``str(value)`` this never trips the interpreter's default
    int-to-decimal conversion ceiling, because every per-chunk ``str``
    conversion stays strictly below it. The output for values the default
    conversion accepts is exactly ``str(value)``.

    Args:
        value: Any Python ``int`` (``bool`` is rejected as a type error, as
            :func:`json.dumps` treats it as a distinct scalar).

    Returns:
        The exact decimal representation, with a leading ``-`` for negative
        values and no leading zeros.

    Raises:
        TypeError: If ``value`` is not an ``int``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"value is not an int: {type(value).__name__}")
    negative = value < 0
    magnitude = -value if negative else value
    chunk = _chunk_digits()
    base = 10**chunk
    chunks: list[str] = []
    while magnitude:
        magnitude, rest = divmod(magnitude, base)
        chunks.append(str(rest))
    if not chunks:
        return "0"
    chunks.reverse()
    # str() never pads, so the most significant chunk needs no stripping;
    # every less significant chunk is zero-padded to the full chunk width.
    return (
        ("-" if negative else "")
        + chunks[0]
        + "".join(piece.zfill(chunk) for piece in chunks[1:])
    )


def int_from_decimal(token: str) -> int:
    """Return the exact ``int`` for a decimal JSON integer token.

    Unlike ``int(token)`` this never trips the interpreter's default
    str-to-int conversion ceiling, because every per-chunk ``int`` conversion
    stays strictly below it. For tokens the default conversion accepts, the
    result is exactly ``int(token)``; the leading sign token of a negative
    JSON number is accepted, matching the ``parse_int`` hook contract of
    :func:`json.loads`.

    Args:
        token: The decimal digits, optionally preceded by ``-`` or ``+``.

    Returns:
        The exact integer value.

    Raises:
        ValueError: If the text is not a decimal integer literal.
    """
    body = token
    sign = 1
    if body[:1] in ("-", "+"):
        sign = -1 if body[0] == "-" else 1
        body = body[1:]
    if len(body) <= _chunk_digits():
        return sign * int(body)
    value = 0
    for start in range(0, len(body), _chunk_digits()):
        part = body[start : start + _chunk_digits()]
        value = value * 10 ** len(part) + int(part)
    return sign * value


def dump_json(value: Any, *, sort_keys: bool = False) -> str:
    """Serialize ``value`` as one strict compact JSON document.

    Compact separators, ``ensure_ascii=False``, and ``allow_nan=False``
    strictness match the ledger's encoding contract. Ordinary scalars are
    delegated to :mod:`json` (strings) or ``repr`` (finite floats), so output
    is byte-identical to :func:`json.dumps` for everything the stock encoder
    accepts; arbitrary-precision integers are emitted exactly through
    :func:`int_to_decimal` instead of tripping the interpreter's default
    int-to-decimal ceiling.

    Args:
        value: ``None``, ``bool``, ``int``, finite ``float``, ``str``, list /
            tuple, or dict with JSON-scalar keys.
        sort_keys: When true, dict keys are emitted in sorted order, matching
            ``json.dumps(..., sort_keys=True)``.

    Returns:
        The JSON text (no trailing newline).

    Raises:
        TypeError: For non-JSON types or non-scalar dict keys, matching
            :func:`json.dumps`.
        ValueError: For non-finite floats or circular containers, matching
            ``json.dumps(..., allow_nan=False)``.
    """
    return _dump_json_value(value, sort_keys, set())


def _dump_json_value(value: Any, sort_keys: bool, active: set[int]) -> str:
    """Recursive worker for :func:`dump_json` (compact, no whitespace)."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        # Escaping (including control characters, so one physical line stays
        # one physical line) is delegated to the stdlib implementation.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return int_to_decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(
                "Out of range float values are not JSON compliant: " f"{value!r}"
            )
        return repr(value)
    if isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in active:
            raise ValueError("Circular reference detected")
        active.add(marker)
        try:
            return (
                "["
                + ",".join(_dump_json_value(item, sort_keys, active) for item in value)
                + "]"
            )
        finally:
            active.discard(marker)
    if isinstance(value, dict):
        marker = id(value)
        if marker in active:
            raise ValueError("Circular reference detected")
        active.add(marker)
        try:
            items = list(value.items())
            if sort_keys:
                items = sorted(items)
            parts = [
                _dump_json_key(key) + ":" + _dump_json_value(item, sort_keys, active)
                for key, item in items
            ]
            return "{" + ",".join(parts) + "}"
        finally:
            active.discard(marker)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _dump_json_key(key: Any) -> str:
    """Render one dict key as a JSON string, matching ``json.dumps``."""
    if isinstance(key, str):
        text = key
    elif isinstance(key, bool):
        text = "true" if key else "false"
    elif key is None:
        text = "null"
    elif isinstance(key, int):
        text = int_to_decimal(key)
    elif isinstance(key, float):
        if not math.isfinite(key):
            raise ValueError(
                "Out of range float values are not JSON compliant: " f"{key!r}"
            )
        text = repr(key)
    else:
        raise TypeError(
            "keys must be str, int, float, bool or None, not " f"{type(key).__name__}"
        )
    return json.dumps(text, ensure_ascii=False)
