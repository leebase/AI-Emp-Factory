"""Append-only ledger of resolution events.

Every resolution the router makes (strict, flex, or bind) is recorded as one
compact JSON line in ``~/.local/state/lee-llm-router/events/<host>.jsonl``. The
ledger is append-only and per-host: one writer per machine, never a rewrite,
never a lock file, so a Syncthing-replicated state directory can carry it
between machines without conflict files.

The record carries route identity (``route_id``, provider, model, effort,
channel) alongside the funding-headroom judgement that produced it, so a later
benchmark or LEPR comparison can line up like with like.

This module performs no network calls, starts no subprocesses, and imports no
provider code: it only builds a mapping and appends bytes to a file.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

DEFAULT_EVENTS_DIR = Path("~/.local/state/lee-llm-router/events")
"""Directory holding one event ledger per host (``~`` expanded at write time)."""

EVENTS_FILE_ENV_VAR = "LEE_LLM_ROUTER_EVENTS_FILE"
"""Environment variable that overrides the default ledger file location."""

MAX_EVENT_BYTES = 4096
"""Largest encoded line, including its trailing newline, that may be appended."""

BIND_MODE = "bind"
"""The only mode in which ``authorized_by`` may be non-null."""

EVENT_FIELDS: tuple[str, ...] = (
    "ts",
    "host",
    "harness",
    "crew",
    "role",
    "mode",
    "worker_id",
    "provider",
    "model",
    "effort",
    "channel",
    "headroom",
    "reason",
    "authorized_by",
    "route_id",
    "snapshot_observed_at",
    "snapshot_stale",
)
"""Exactly the keys an event carries, in the order they are serialised."""

OPTIONAL_FIELDS: tuple[str, ...] = (
    "ts",
    "host",
    "harness",
    "effort",
    "authorized_by",
    "snapshot_observed_at",
)
"""Fields :func:`build_event` fills in when the caller omits them."""

REQUIRED_FIELDS: tuple[str, ...] = tuple(
    name for name in EVENT_FIELDS if name not in OPTIONAL_FIELDS
)
"""Fields the caller must supply; omitting one raises :class:`ValueError`."""


class EventError(ValueError):
    """Raised when an event cannot be built or serialised."""


class EventTooLarge(EventError):
    """Raised when the encoded line exceeds :data:`MAX_EVENT_BYTES` bytes."""


class ShortWriteError(OSError):
    """Raised when the single ``os.write`` did not write the whole line."""


def resolve_events_path(explicit: str | Path | None = None) -> Path:
    """Resolve which ledger file to append to.

    Precedence mirrors
    :func:`lee_llm_router.availability.resolve_availability_path`: an explicit
    path wins, then the environment variable, then the per-host default.

    Args:
        explicit: An explicit path, which wins over everything else.

    Returns:
        The resolved path: ``explicit``, else ``$LEE_LLM_ROUTER_EVENTS_FILE``,
        else ``<default dir>/<hostname>.jsonl``, where the host name is
        ``socket.gethostname()`` — the same value ``availability.py`` uses and
        the same value ``scripts/refresh_availability.sh`` gets from
        ``hostname``.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    from_env = os.environ.get(EVENTS_FILE_ENV_VAR)
    if from_env:
        return Path(from_env).expanduser()
    hostname = socket.gethostname()
    return DEFAULT_EVENTS_DIR.expanduser() / f"{hostname}.jsonl"


def _utc_now_iso() -> str:
    """Return the current UTC time as an aware ISO string, seconds precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalise_timestamp(value: Any, field: str) -> str | None:
    """Coerce a timestamp field to an aware UTC ISO string or ``None``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    if isinstance(value, str):
        return value
    raise EventError(f"{field} must be a datetime, an ISO string, or None")


def build_event(**fields: Any) -> dict[str, Any]:
    """Build one ledger event with exactly :data:`EVENT_FIELDS`, in that order.

    Args:
        **fields: Event fields. Everything in :data:`REQUIRED_FIELDS` must be
            present. ``ts`` and ``host`` default to now and this host,
            ``harness`` defaults to ``"cli"``, and ``effort``,
            ``authorized_by`` and ``snapshot_observed_at`` default to ``None``.

    Returns:
        A new dict whose keys are exactly :data:`EVENT_FIELDS`, in order.

    Raises:
        ValueError: If an unknown key is supplied, a required key is missing,
            ``snapshot_stale`` is not a bool, or ``authorized_by`` is non-null
            while ``mode`` is not ``"bind"``.
    """
    unknown = sorted(set(fields) - set(EVENT_FIELDS))
    if unknown:
        raise EventError(f"unknown event field(s): {', '.join(unknown)}")
    missing = [name for name in REQUIRED_FIELDS if name not in fields]
    if missing:
        raise EventError(f"missing event field(s): {', '.join(missing)}")

    mode = fields["mode"]
    authorized_by = fields.get("authorized_by")
    if authorized_by is not None and mode != BIND_MODE:
        raise EventError(
            f"authorized_by is only allowed when mode is {BIND_MODE!r}, got {mode!r}"
        )
    snapshot_stale = fields["snapshot_stale"]
    if not isinstance(snapshot_stale, bool):
        raise EventError("snapshot_stale must be a bool")

    host = fields.get("host") or socket.gethostname()

    event: dict[str, Any] = {
        "ts": _normalise_timestamp(fields.get("ts"), "ts") or _utc_now_iso(),
        "host": host,
        "harness": fields.get("harness") or "cli",
        "crew": fields["crew"],
        "role": fields["role"],
        "mode": mode,
        "worker_id": fields["worker_id"],
        "provider": fields["provider"],
        "model": fields["model"],
        "effort": fields.get("effort"),
        "channel": fields["channel"],
        "headroom": fields["headroom"],
        "reason": fields["reason"],
        "authorized_by": authorized_by,
        "route_id": fields["route_id"],
        "snapshot_observed_at": _normalise_timestamp(
            fields.get("snapshot_observed_at"), "snapshot_observed_at"
        ),
        "snapshot_stale": snapshot_stale,
    }
    return event


def encode_event(event: Mapping[str, Any]) -> bytes:
    """Encode one event as a single UTF-8 line, newline included.

    ``json.dumps`` escapes control characters, so a value containing a literal
    newline still encodes to one physical line.

    Args:
        event: The event mapping to encode.

    Returns:
        The encoded line, ending in ``b"\\n"``.

    Raises:
        EventTooLarge: If the encoded line exceeds :data:`MAX_EVENT_BYTES`.
        EventError: If the mapping is not JSON-serialisable.
    """
    try:
        text = json.dumps(dict(event), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise EventError(f"event is not JSON-serialisable: {exc}") from exc
    line = text.encode("utf-8") + b"\n"
    if len(line) > MAX_EVENT_BYTES:
        raise EventTooLarge(
            f"encoded event is {len(line)} bytes, limit is {MAX_EVENT_BYTES}"
        )
    return line


def append_event(event: Mapping[str, Any], path: str | Path | None = None) -> Path:
    """Append one event to the ledger as a single line.

    The line is written with one ``os.write`` on a descriptor opened
    ``O_WRONLY|O_CREAT|O_APPEND`` with mode ``0o600``. A short write raises
    :class:`ShortWriteError` rather than looping: a retry would append the
    remainder as if it were a fresh record, and a silently torn line is worse
    for an append-only ledger than a raised error the caller can see.

    Args:
        event: The event mapping, normally from :func:`build_event`.
        path: Explicit ledger path; resolved by :func:`resolve_events_path`
            when omitted.

    Returns:
        The path written to.

    Raises:
        EventTooLarge: If the encoded line exceeds :data:`MAX_EVENT_BYTES`.
        ShortWriteError: If the whole line was not written in one call.
    """
    line = encode_event(event)
    target = resolve_events_path(path)
    parent = target.parent
    if not parent.is_dir():
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(parent, 0o700)
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


def read_events(path: str | Path) -> list[dict[str, Any]]:
    """Read a ledger, tolerating damage.

    Blank lines are skipped. A line that is not a JSON object is returned as
    ``{"_malformed": <raw line>}`` instead of raising, so one bad line never
    hides the good ones around it.

    Args:
        path: The ledger file to read.

    Returns:
        One dict per non-blank line, in file order.
    """
    events: list[dict[str, Any]] = []
    with open(Path(path).expanduser(), "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                events.append({"_malformed": line})
                continue
            if not isinstance(parsed, dict):
                events.append({"_malformed": line})
                continue
            events.append(parsed)
    return events
