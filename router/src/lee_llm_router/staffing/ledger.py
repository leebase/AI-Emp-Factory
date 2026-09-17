"""Append-only ledger of validated attempt records (staffing Phase 1, P1-3).

Every staffing attempt (live ``run`` command, benchmark import, agent-orch
import) is recorded as one compact JSON line in
``~/.local/state/lee-llm-router/attempts/<hostname>.jsonl``. Like the events
ledger the file is append-only and per-host, with every writer serialized by
an exclusive per-ledger ``flock`` — never a rewrite, never a truncate, never
an update, so a Syncthing-replicated state directory can carry it between
machines without conflict files.

Every record is validated against the committed Draft 2020-12 attempt-record
v2 schema (``config/staffing/schema/attempt-record.schema.json``) *before* any
directory is created or byte is written, and :func:`read_attempts` validates
every line again on the way out: a malformed or schema-invalid line fails
closed with the path and line number rather than surfacing a bad record.

Beyond the schema, :func:`validate_attempt` enforces the two evidence-truth
rules JSON Schema cannot express portably (P1-9 gate): every float in the
record must be finite (``NaN``/``Infinity`` are not valid JSON, so the strict
JSONL line can never contain them), and when ``class_record`` is present its
``class_key`` must equal the canonical reconstruction of the five components
(role/oracle_type/sorted domain_tags-or-``none``/size_band/language). The
class check joins evidence reliably without any class-to-model or
class-to-route lookup (D206); the canonical rendering mirrors
``classes.schema.json`` $defs/classBlock.

Concurrent import writers serialize on an exclusive kernel ``flock`` carried
by a ``<ledger>.lock`` file next to the ledger: plain :func:`append_attempt`
holds it for its one record and :func:`writer_transaction` holds it across a
whole import batch, so concurrent imports against one per-host ledger cannot
interleave, lose, or duplicate accepted records. The kernel releases a dead
holder's ``flock`` (a crashed import never wedges the ledger), and lock
exhaustion raises :class:`AttemptLedgerError` — fail closed, never
unserialized — after :data:`LOCK_TIMEOUT_SECONDS`.

The line is written with one ``os.write`` on a descriptor opened
``O_WRONLY|O_CREAT|O_APPEND`` with mode ``0o600``. A short write raises
:class:`ShortWriteError` rather than looping: a retry would append the
remainder as if it were a fresh record, and a silently torn line is worse for
an append-only ledger than a raised error the caller can see. No rewrite,
truncate, or update API exists in this module.

Token counters are arbitrary-precision JSON integers. CPython >= 3.11's
default 4300-digit int<->decimal conversion ceiling is a Python interpreter
limit, not a JSON limit, so encoding and decoding never mutate the process
security setting (``sys.set_int_max_str_digits``) and never degrade a counter
to a float or a string: the strict line carries the exact integer digits, and
:func:`read_attempts` rebuilds the exact Python ``int`` on the way out, both
through the bounded chunked conversions of
:mod:`lee_llm_router.staffing.json_int`.

This module performs no network calls, starts no subprocesses, and calls no
providers: it validates a mapping and appends bytes to a file.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from lee_llm_router.providers.base import LLMRouterError
from lee_llm_router.staffing.json_int import (
    dump_json,
    int_from_decimal,
    int_to_decimal,
)

try:  # pragma: no cover - exercised trivially on POSIX CI
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback only
    _fcntl = None

DEFAULT_ATTEMPTS_DIR = Path("~/.local/state/lee-llm-router/attempts")
"""Directory holding one attempt ledger per host (``~`` expanded at use time)."""

ATTEMPTS_FILE_ENV_VAR = "LEE_LLM_ROUTER_ATTEMPTS_FILE"
"""Test-only override of the full attempt-ledger file path (never ``HOME``)."""

ATTEMPTS_STATE_ROOT_ENV_VAR = "LEE_LLM_ROUTER_ATTEMPTS_STATE_ROOT"
"""Test-only override of the app state root the ``attempts/`` dir lives under."""

_REPO_ROOT = Path(__file__).resolve().parents[3]
ATTEMPT_RECORD_SCHEMA_PATH = (
    _REPO_ROOT / "config" / "staffing" / "schema" / "attempt-record.schema.json"
)
"""The committed Draft 2020-12 attempt-record v2 schema (P1-2)."""

_SCHEMA_CACHE: dict[str, Mapping[str, Any]] = {}
_VALIDATOR_CACHE: dict[str, Draft202012Validator] = {}


class AttemptLedgerError(LLMRouterError):
    """Raised when an attempt record cannot be validated, encoded, or read."""


class ShortWriteError(OSError):
    """Raised when the single ``os.write`` did not write the whole line."""


LOCK_TIMEOUT_SECONDS = 30.0
"""Bounded wait for the exclusive per-ledger writer lock before failing."""

_HELD_LOCKS = threading.local()
"""Per-thread map of writer locks this thread already holds (re-entrancy)."""


def load_attempt_record_schema() -> Mapping[str, Any]:
    """Load the committed attempt-record v2 schema document.

    Returns:
        The parsed Draft 2020-12 schema from
        :data:`ATTEMPT_RECORD_SCHEMA_PATH`, cached after first load.

    Raises:
        AttemptLedgerError: If the schema file is missing or unparsable.
    """
    if "attempt-record" not in _SCHEMA_CACHE:
        if not ATTEMPT_RECORD_SCHEMA_PATH.is_file():
            raise AttemptLedgerError(
                f"attempt-record schema file not found: {ATTEMPT_RECORD_SCHEMA_PATH}"
            )
        with open(ATTEMPT_RECORD_SCHEMA_PATH, encoding="utf-8") as handle:
            _SCHEMA_CACHE["attempt-record"] = json.load(handle)
    return _SCHEMA_CACHE["attempt-record"]


def attempt_record_validator() -> Draft202012Validator:
    """Return the Draft 2020-12 validator for the attempt-record v2 schema.

    Format checking matches :mod:`lee_llm_router.staffing.catalog`.

    Returns:
        A validator over :func:`load_attempt_record_schema`, cached.
    """
    if "attempt-record" not in _VALIDATOR_CACHE:
        _VALIDATOR_CACHE["attempt-record"] = Draft202012Validator(
            load_attempt_record_schema(), format_checker=FormatChecker()
        )
    return _VALIDATOR_CACHE["attempt-record"]


class _SchemaSafeInt(int):
    """Large integer used only to keep jsonschema diagnostics bounded."""

    def __repr__(self) -> str:
        """Avoid decimal conversion when jsonschema renders an instance."""
        return "<large integer>"


def _schema_safe_value(value: Any) -> Any:
    """Copy values with bounded reprs for jsonschema's internal diagnostics."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return _SchemaSafeInt(value) if value.bit_length() > 512 else value
    if isinstance(value, Mapping):
        return {
            _schema_safe_value(key): _schema_safe_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_schema_safe_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_schema_safe_value(item) for item in value)
    return value


def _schema_violations(record: Mapping[str, Any]) -> list[str]:
    """Render every schema violation of ``record`` as a readable string.

    ``jsonschema`` includes the whole failing instance in some conditional
    diagnostics. A huge valid counter can therefore trip CPython's decimal
    conversion limit before validation finishes. Validation sees equivalent
    integer subclasses whose repr is bounded; the original values are never
    changed, encoded, or returned.
    """
    safe_record = _schema_safe_value(record)
    try:
        errors = sorted(
            attempt_record_validator().iter_errors(safe_record),
            key=lambda e: list(e.absolute_path),
        )
    except ValueError as exc:
        return [
            "$: record is schema-invalid and too large to render full schema "
            f"diagnostics for ({exc})"
        ]
    return [f"{error.json_path}: {error.message}" for error in errors]


def _nonfinite_float_paths(value: Any, path: str = "$") -> list[str]:
    """Return ``$``-paths of every non-finite float inside ``value``.

    ``NaN`` and the infinities are accepted by :func:`json.loads` (and by
    Python's schema validation of Python floats) but are not JSON tokens, so
    a strict-JSONL ledger must reject them wherever they appear.
    """
    if isinstance(value, float):
        return [] if math.isfinite(value) else [path]
    if isinstance(value, Mapping):
        return [
            nested
            for key, item in value.items()
            for nested in _nonfinite_float_paths(item, f"{path}.{key}")
        ]
    if isinstance(value, (list, tuple)):
        return [
            nested
            for index, item in enumerate(value)
            for nested in _nonfinite_float_paths(item, f"{path}[{index}]")
        ]
    return []


def _canonical_class_key(
    role: str,
    oracle_type: str,
    domain_tags: list[str] | tuple[str, ...],
    size_band: str,
    language: str,
) -> str:
    """Render the canonical five-segment class key from its components.

    Mirrors ``classes.schema.json`` $defs/classBlock (and
    ``catalog.canonical_class_key``): tags sorted ascending by codepoint,
    deduplicated, ``+``-joined, empty set rendered as the literal ``none``.
    This is evidence-join consistency only — no class-to-model or
    class-to-route lookup exists here (D206).
    """
    tags = sorted(set(domain_tags))
    segment = "+".join(tags) if tags else "none"
    return f"{role}/{oracle_type}/{segment}/{size_band}/{language}"


def _evidence_violations(record: Mapping[str, Any]) -> list[str]:
    """Render ledger-only evidence-truth violations beyond the schema.

    Two rules JSON Schema cannot express portably: floats must be finite
    (non-finite floats cannot be encoded as strict JSON), and a present
    ``class_record`` must be internally consistent — ``class_key`` equal to
    the canonical rendering of its five components.
    """
    violations = [
        f"{path}: non-finite float (NaN/Infinity) is not valid strict JSON"
        for path in _nonfinite_float_paths(record)
    ]
    class_record = record.get("class_record")
    if isinstance(class_record, Mapping):
        role = class_record.get("role")
        oracle_type = class_record.get("oracle_type")
        domain_tags = class_record.get("domain_tags")
        size_band = class_record.get("size_band")
        language = class_record.get("language")
        # The canonical join is only defined over well-typed components; a
        # malformed class_record (non-string tags, missing/ill-typed fields)
        # is rejected by _schema_violations, so it must never crash this
        # evidence check before those schema errors can be returned.
        well_typed = (
            isinstance(role, str)
            and isinstance(oracle_type, str)
            and isinstance(size_band, str)
            and isinstance(language, str)
            and isinstance(domain_tags, (list, tuple))
            and all(isinstance(tag, str) for tag in domain_tags)
        )
        if not well_typed:
            return violations
        expected = _canonical_class_key(
            role,
            oracle_type,
            domain_tags,
            size_band,
            language,
        )
        actual = class_record.get("class_key")
        if actual != expected:
            violations.append(
                f"$.class_record.class_key: {_bounded_repr(actual)} does not "
                "equal the canonical reconstruction "
                f"{expected!r} of role/oracle_type/sorted domain_tags-or-"
                "none/size_band/language (components are authoritative; "
                "evidence joins require consistency)"
            )
    return violations


def _bounded_repr(value: Any) -> str:
    """Render ``value`` in diagnostics without tripping the digit ceiling."""
    if isinstance(value, int) and not isinstance(value, bool):
        return int_to_decimal(value)
    return repr(value)


def validate_attempt(record: Mapping[str, Any]) -> None:
    """Validate one attempt record against the committed v2 schema.

    Beyond schema membership this enforces the two evidence-truth rules the
    schema cannot express portably: every float must be finite, and a present
    ``class_record`` must carry a ``class_key`` equal to the canonical
    reconstruction of its five components.

    Args:
        record: The candidate attempt record.

    Raises:
        AttemptLedgerError: With every violation rendered, if the record does
            not satisfy the schema or the ledger evidence-truth rules.
    """
    violations = _schema_violations(record) + _evidence_violations(record)
    if violations:
        raise AttemptLedgerError(
            "attempt record failed attempt-record v2 schema validation:\n  "
            + "\n  ".join(violations)
        )


def resolve_attempts_path(explicit: str | Path | None = None) -> Path:
    """Resolve which attempt ledger file to append to.

    Precedence mirrors :func:`lee_llm_router.events.resolve_events_path`: an
    explicit path wins, then the file environment variable, then the state
    root environment variable, then the per-host default.

    Args:
        explicit: An explicit path, which wins over everything else.

    Returns:
        The resolved path: ``explicit``, else ``$LEE_LLM_ROUTER_ATTEMPTS_FILE``,
        else ``<$LEE_LLM_ROUTER_ATTEMPTS_STATE_ROOT>/attempts/<hostname>.jsonl``,
        else ``<default dir>/<hostname>.jsonl`` where the default dir is
        :data:`DEFAULT_ATTEMPTS_DIR` and the host name is
        ``socket.gethostname()`` — the same value ``events.py`` uses.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    from_file_env = os.environ.get(ATTEMPTS_FILE_ENV_VAR)
    if from_file_env:
        return Path(from_file_env).expanduser()
    from_root_env = os.environ.get(ATTEMPTS_STATE_ROOT_ENV_VAR)
    if from_root_env:
        base = Path(from_root_env).expanduser() / "attempts"
    else:
        base = DEFAULT_ATTEMPTS_DIR.expanduser()
    return base / f"{socket.gethostname()}.jsonl"


def encode_attempt(record: Mapping[str, Any]) -> bytes:
    """Validate and encode one attempt record as a single UTF-8 line.

    Control characters are escaped, so a value containing a literal newline
    still encodes to one physical line. Non-finite floats raise instead of
    emitting the ``NaN``/``Infinity`` tokens Python's ``json`` would otherwise
    write into the append-only JSONL, keeping the encoding standards-compliant
    strict JSON. Token counters stay exact arbitrary-precision JSON integers
    at any magnitude: the bounded serializer of
    :func:`lee_llm_router.staffing.json_int.dump_json` emits every digit
    without tripping CPython's default int-to-decimal conversion ceiling, and
    without mutating that process-wide setting.

    Args:
        record: The attempt record to encode.

    Returns:
        The encoded line, ending in ``b"\\n"``.

    Raises:
        AttemptLedgerError: If the record is schema-invalid or evidence-truth
            invalid, or not JSON-serialisable.
    """
    validate_attempt(record)
    try:
        text = dump_json(dict(record))
    except (TypeError, ValueError) as exc:
        raise AttemptLedgerError(
            f"attempt record is not JSON-serialisable: {exc}"
        ) from exc
    return text.encode("utf-8") + b"\n"


def append_attempt(record: Mapping[str, Any], path: str | Path | None = None) -> Path:
    """Append one validated attempt record to the ledger as a single line.

    The record is validated before anything is created, so an invalid record
    never leaves a file behind. The write is serialized against every other
    writer on the same ledger with the exclusive ``flock`` carried by
    ``<ledger>.lock``: when called inside this thread's
    :func:`writer_transaction` the held lock is re-entered (a second flock
    would deadlock across descriptions), otherwise the lock is taken and
    released around the single append. The line is written with one
    ``os.write`` on a descriptor opened ``O_WRONLY|O_CREAT|O_APPEND`` with
    mode ``0o600``; a short write raises :class:`ShortWriteError` and is
    never retried.

    Args:
        record: The attempt record, normally a v2 dict per the committed
            schema.
        path: Explicit ledger path; resolved by :func:`resolve_attempts_path`
            when omitted.

    Returns:
        The path written to.

    Raises:
        AttemptLedgerError: If the record is schema-invalid or
            evidence-truth-invalid, not JSON-serialisable, or the writer lock
            cannot be acquired within :data:`LOCK_TIMEOUT_SECONDS`.
        ShortWriteError: If the whole line was not written in one call.
    """
    line = encode_attempt(record)
    target = resolve_attempts_path(path)
    lock_key = str(_lock_file_path(target))
    if lock_key in _held_writer_locks():
        return _append_line(line, target)
    fd = _acquire_writer_lock(target)
    try:
        return _append_line(line, target)
    finally:
        _release_writer_lock(fd)


def _lock_file_path(target: Path) -> Path:
    """Return the exclusive-writer lock path for one ledger file."""
    return target.with_name(target.name + ".lock")


def _ensure_private_parent(parent: Path) -> None:
    """Create ``parent`` as a ``0o700`` directory when it is missing."""
    if not parent.is_dir():
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(parent, 0o700)


def _append_line(line: bytes, target: Path) -> Path:
    """Append one encoded line as a single ``O_APPEND`` ``os.write``."""
    _ensure_private_parent(target.parent)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        written = os.write(fd, line)
    finally:
        os.close(fd)
    if written != len(line):
        raise ShortWriteError(
            f"short write to {target}: wrote {written} of {len(line)} bytes"
        )
    return target


def _held_writer_locks() -> dict[str, int]:
    """Return this thread's map of held writer locks, creating it lazily."""
    held = getattr(_HELD_LOCKS, "locks", None)
    if held is None:
        held = _HELD_LOCKS.locks = {}
    return held


def _acquire_writer_lock(target: Path) -> int:
    """Open and exclusively ``flock`` the per-ledger lock file.

    The lock file is never appended to and carries no bytes; it exists only
    to hold the kernel lock, which the kernel releases when its holder dies.
    The wait is bounded: exhaustion raises :class:`AttemptLedgerError`
    (fail closed) instead of writing unserialized.

    Args:
        target: The ledger file the lock protects.

    Returns:
        The locked descriptor; release it with :func:`_release_writer_lock`.

    Raises:
        AttemptLedgerError: If ``fcntl`` is unavailable or the lock cannot
            be acquired within :data:`LOCK_TIMEOUT_SECONDS`.
    """
    if _fcntl is None:  # pragma: no cover - non-POSIX only
        raise AttemptLedgerError(
            "attempt-ledger writer serialization requires fcntl (POSIX): " f"{target}"
        )
    lock_path = _lock_file_path(target)
    _ensure_private_parent(target.parent)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            return fd
        except OSError as exc:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise AttemptLedgerError(
                    f"attempt-ledger writer lock not acquired within "
                    f"{LOCK_TIMEOUT_SECONDS:.0f}s: {lock_path}"
                ) from exc
            time.sleep(0.01)


def _release_writer_lock(fd: int) -> None:
    """Release and close one writer lock descriptor."""
    try:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def writer_transaction(path: str | Path | None = None) -> Iterator[Path]:
    """Hold the per-ledger writer lock across a batch of appends.

    Import operations that must read-decide-append as one serialized unit run
    inside this context: concurrent imports against one per-host ledger then
    cannot interleave, lose, or duplicate accepted records. The lock is an
    exclusive kernel ``flock`` on ``<ledger>.lock``, released by the kernel
    even when a holder crashes. :func:`append_attempt` calls on the same
    ledger from this thread re-enter the held lock instead of deadlocking,
    and writers outside any transaction take the same lock around a single
    record, so every writer is serialized while the ledger stays append-only
    JSONL with validation before every write.

    Args:
        path: Explicit ledger path; resolved by :func:`resolve_attempts_path`
            when omitted.

    Yields:
        The resolved ledger path.

    Raises:
        AttemptLedgerError: If the lock cannot be acquired within
            :data:`LOCK_TIMEOUT_SECONDS`, or ``fcntl`` is unavailable.
    """
    target = resolve_attempts_path(path)
    lock_key = str(_lock_file_path(target))
    held = _held_writer_locks()
    if lock_key in held:
        yield target
        return
    fd = _acquire_writer_lock(target)
    held[lock_key] = fd
    try:
        yield target
    finally:
        held.pop(lock_key, None)
        _release_writer_lock(fd)


def read_attempts(path: str | Path) -> list[dict[str, Any]]:
    """Read an attempt ledger, validating every record.

    Blank lines are skipped; everything else must be a JSON object that
    satisfies the attempt-record v2 schema. Unlike :func:`lee_llm_router.
    events.read_events` this fails closed: one damaged or schema-invalid line
    raises rather than being tolerated, because consumers (rollups, imports)
    must never mix unvalidated records into comparisons.

    Args:
        path: The ledger file to read.

    Returns:
        One dict per record, in file order.

    Raises:
        AttemptLedgerError: Naming the path and 1-based line number for any
            line that is not valid JSON, not a JSON object, or schema-invalid.
    """
    ledger_path = Path(path).expanduser()
    records: list[dict[str, Any]] = []
    with open(ledger_path, "r", encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, start=1):
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            try:
                parsed = json.loads(line, parse_int=int_from_decimal)
            except json.JSONDecodeError as exc:
                raise AttemptLedgerError(
                    f"{ledger_path}:{lineno}: line is not valid JSON "
                    f"({exc.msg} at column {exc.colno})"
                ) from exc
            if not isinstance(parsed, dict):
                raise AttemptLedgerError(
                    f"{ledger_path}:{lineno}: line is not a JSON object"
                )
            violations = _schema_violations(parsed) + _evidence_violations(parsed)
            if violations:
                raise AttemptLedgerError(
                    f"{ledger_path}:{lineno}: attempt record failed "
                    "attempt-record v2 schema validation:\n  " + "\n  ".join(violations)
                )
            records.append(parsed)
    return records
