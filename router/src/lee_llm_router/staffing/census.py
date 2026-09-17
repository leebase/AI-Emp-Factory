"""Live run registry and census (Phase 3 packet P3-4, D213 ruling 4).

D213 ruling 4 (``docs/staffing/phase3-contracts.md`` §D213 rulings): "A run
registers pid, route, packet id, owned paths, and start under the state
directory; an intersecting live run is refused. ``census`` lists live rows
and cleans stale pids with a note. Parallel work is permitted only for
disjoint owned paths."

This module is the single registry boundary used by both the ``run`` CLI
(before launch, and deregistration on every exit boundary) and the
``census`` CLI (listing genuinely live runs and cleaning stale records).

* **Registry shape.** One JSON file per live run under the per-host state
  directory: ``<state root>/runs/<hostname>/<pid>-<registry_id>.json``. The
  per-host state directory is the same root the attempt ledger and events
  ledger already use (``~/.local/state/lee-llm-router``), overridable for
  tests through :data:`RUN_REGISTRY_DIR_ENV_VAR` (whole directory) or
  :data:`RUNS_STATE_ROOT_ENV_VAR` (state root). Registration writes a unique
  temporary file and atomically renames it into place, so a reader never
  observes a partial record.
* **Race-safe check-and-register.** Registration, census cleanup, and stale
  purging all hold an exclusive :mod:`fcntl` ``flock`` on a lock file next
  to the per-host registry directory, so "no live record intersects the
  requested paths" and "the new record is durable" are one atomic step.
  Where ``fcntl`` is unavailable the module degrades to an atomic
  ``O_EXCL`` lock file with bounded retry and a stale takeover, and this
  is a conservative best-effort fallback, not a stronger guarantee.
* **Process identity.** A pid is not an identity: it can be reused. On
  Linux every record additionally carries the kernel ``starttime`` of the
  registered process (``/proc/<pid>/stat`` field 22, via the committed
  :func:`lee_llm_router.staffing.run._linux_process_record` reader). A
  record is stale when its pid is gone, or when the pid exists but its
  current start time differs from the recorded one (pid reuse). On
  platforms without ``/proc`` the record carries a ``pid_only`` identity
  and liveness is judged conservatively: a record is live while its pid
  exists, and cleanup never deletes a record whose reuse it cannot prove.
* **Deregistration.** Each run removes exactly its own record file by its
  unique registry id. This is deliberately lock-free and best-effort: the
  path is owner-unique so no other record can be touched, a concurrent
  census can only ever judge the owner's record live (the owner process is
  still running), and a deregistration failure is returned as a note —
  never raised over the run's real outcome, so attempt-record truth is
  never masked.
* **Empty owned paths.** Refused (:class:`RunRegistryError`): a run with no
  owned paths cannot prove disjointness, so it must not enter the registry.

This module performs no subprocess launches, no network calls, and calls no
providers.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from lee_llm_router.providers.base import LLMRouterError
from lee_llm_router.staffing.run import _linux_process_record

__all__ = [
    "DEFAULT_RUNS_DIR",
    "REGISTRY_SCHEMA_VERSION",
    "RECORD_KIND",
    "RUN_REGISTRY_DIR_ENV_VAR",
    "RUNS_STATE_ROOT_ENV_VAR",
    "CensusCleaned",
    "CensusResult",
    "ProcessIdentity",
    "RegistryConflictError",
    "RunRegistryError",
    "RunRegistryRecord",
    "census_record_json",
    "census_registry",
    "deregister_run",
    "normalize_owned_path",
    "normalize_owned_paths",
    "paths_intersect",
    "pid_alive",
    "process_identity",
    "register_run",
    "resolve_registry_dir",
]

DEFAULT_RUNS_DIR = Path("~/.local/state/lee-llm-router/runs")
"""Directory holding one per-host live-run registry (``~`` expanded at use)."""

RUNS_STATE_ROOT_ENV_VAR = "LEE_LLM_ROUTER_RUNS_STATE_ROOT"
"""Test-only override of the app state root the ``runs/`` dir lives under."""

RUN_REGISTRY_DIR_ENV_VAR = "LEE_LLM_ROUTER_RUN_REGISTRY_DIR"
"""Test-only override of the whole per-host registry directory."""

REGISTRY_SCHEMA_VERSION = 1
"""Registry record schema version."""

RECORD_KIND = "run_registry"
"""Registry record kind."""

_ON_LINUX = os.name == "posix" and sys.platform.startswith("linux")

_LOCK_TIMEOUT_SECONDS = 30.0
"""Bounded wait for the exclusive registry lock before failing closed."""

_LOCK_FALLBACK_STALE_SECONDS = 600.0
"""Non-fcntl fallback: age after which an O_EXCL lock file is taken over."""

_REGISTRATION_EXIT_CODE = 3
"""Process exit code for every registry refusal (mirrors ``run`` refusals)."""


class RunRegistryError(LLMRouterError):
    """A registry/census failure or refusal; the ``run`` CLI exits 3."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int = _REGISTRATION_EXIT_CODE,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message, cause=cause)
        self.exit_code = exit_code


class RegistryConflictError(RunRegistryError):
    """Refusal: a live registry record owns an intersecting path.

    Carries ``conflicts``: one ``(pid, route_id, packet_id, overlapping
    paths)`` tuple per intersecting live record. Nothing is launched when
    it is raised.
    """

    def __init__(
        self,
        message: str,
        *,
        conflicts: Sequence[tuple[int, str, str, tuple[str, ...]]],
    ) -> None:
        super().__init__(message)
        self.conflicts = tuple(conflicts)


# ---------------------------------------------------------------------------
# Process identity (pid reuse safety)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessIdentity:
    """Observed identity of one process incarnation.

    ``kind`` is ``"linux_start_time"`` when the kernel start time was read
    from ``/proc`` (the strong identity), or ``"pid_only"`` — the
    conservative portable fallback when no start-time evidence is
    available. ``start_time`` is the ``/proc/<pid>/stat`` ``starttime``
    field (clock ticks since boot) exactly as read; it is never normalized
    or recomputed.
    """

    kind: str
    start_time: int | None


def process_identity(pid: int) -> ProcessIdentity | None:
    """Read the current identity of ``pid``.

    On Linux the identity is the ``/proc`` start time; ``None`` means the
    process record could not be read (gone, or unreadable procfs — callers
    treat ``None`` as "cannot verify" and stay conservative). Off Linux no
    start-time evidence exists, so the fallback identity is returned for
    any pid; actual existence is :func:`pid_alive`'s job.
    """
    if _ON_LINUX:
        record = _linux_process_record(pid)
        if record is None:
            return None
        return ProcessIdentity(kind="linux_start_time", start_time=record[1])
    return ProcessIdentity(kind="pid_only", start_time=None)


def pid_alive(pid: int) -> bool:
    """Whether a process exists for ``pid`` right now (signal 0 probe).

    Only proven absence (``ProcessLookupError``) reports ``False``; every
    other failure is conservative ``True`` so cleanup never deletes a
    record it could not prove stale.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


#: Injectable liveness/identity seams (tests substitute fake processes).
_PID_ALIVE: Callable[[int], bool] = pid_alive
_PROCESS_IDENTITY: Callable[[int], ProcessIdentity | None] = process_identity


def evaluate_record_liveness(record: RunRegistryRecord) -> tuple[bool, str]:
    """Judge one registry record genuinely live or stale, with a reason.

    A record is stale when its pid no longer exists, or — the pid-reuse
    safety rule — when the pid exists but its current Linux start time
    differs from the recorded start time: that pid now belongs to a
    different process incarnation. When start-time evidence cannot be read
    (off Linux, or an unreadable ``/proc``), a record whose pid exists is
    conservatively live: cleanup must not delete what it cannot prove
    stale.
    """
    if not _PID_ALIVE(record.pid):
        return False, f"pid {record.pid} no longer exists"
    identity = record.identity
    if identity is not None and identity.kind == "linux_start_time":
        current = _PROCESS_IDENTITY(record.pid)
        if (
            current is not None
            and current.kind == "linux_start_time"
            and current.start_time is not None
        ):
            if current.start_time != identity.start_time:
                return False, (
                    f"pid {record.pid} was reused: recorded start time "
                    f"{identity.start_time} differs from current "
                    f"{current.start_time}"
                )
    return True, "live"


# ---------------------------------------------------------------------------
# Owned-path normalization and intersection
# ---------------------------------------------------------------------------


def normalize_owned_path(raw: str, workdir: str | Path | None = None) -> str:
    """Normalize one owned path to an absolute, symlink-safe string.

    Relative paths are resolved against ``workdir`` (``--workdir``) when
    supplied, otherwise against the process working directory. Normalization
    resolves symlinks only through components that actually exist — the
    committed ``os.path.realpath(..., strict=False)`` boundary — so a
    nonexistent target is never followed unsafely and the remaining tail is
    normalized lexically. ``~`` expansion is applied.

    Raises:
        RunRegistryError: When ``raw`` is not a nonempty string.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise RunRegistryError("--owned-paths: every path must be a nonempty string")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        if workdir is not None:
            base = Path(workdir).expanduser()
        else:
            base = Path.cwd()
        candidate = base / candidate
    return str(Path(os.path.realpath(candidate, strict=False)))


def normalize_owned_paths(
    paths: Sequence[str], workdir: str | Path | None = None
) -> tuple[str, ...]:
    """Normalize an owned-path list; refuse an empty one.

    Empty owned paths are refused because a run without owned paths cannot
    prove disjointness against live runs (D213 ruling 4). Duplicates are
    removed preserving first-occurrence order.

    Raises:
        RunRegistryError: When ``paths`` is empty or any entry is invalid.
    """
    if not paths:
        raise RunRegistryError(
            "--owned-paths is required: a run with no owned paths cannot "
            "prove disjointness, so it cannot be registered"
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        candidate = normalize_owned_path(raw, workdir=workdir)
        if candidate not in seen:
            seen.add(candidate)
            normalized.append(candidate)
    return tuple(normalized)


def paths_intersect(a: str, b: str) -> bool:
    """Whether two normalized paths are equal or ancestor/descendant.

    Both directions are checked: a run owning a directory conflicts with a
    run owning any file or subdirectory inside it, and vice versa. Path
    components are compared exactly; ``/a/b`` does not intersect ``/a/bc``.
    """
    try:
        left, right = Path(a), Path(b)
        return left.is_relative_to(right) or right.is_relative_to(left)
    except ValueError:  # e.g. incomparable drives on Windows
        return a == b


# ---------------------------------------------------------------------------
# Registry records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunRegistryRecord:
    """One live run's registration row.

    ``identity`` is the registering process's :class:`ProcessIdentity`
    (Linux start time when available, ``pid_only`` fallback). ``path`` is
    the record's registry file; it is ``None`` only for a record not yet
    written.
    """

    registry_id: str
    pid: int
    identity: ProcessIdentity | None
    route_id: str
    packet_id: str
    owned_paths: tuple[str, ...]
    workdir: str | None
    started_at: str
    path: Path | None = None


def record_to_json(record: RunRegistryRecord) -> dict[str, Any]:
    """Encode one registry record as its strict-JSON payload mapping."""
    identity: dict[str, Any] | None
    if record.identity is None:
        identity = None
    else:
        identity = {
            "kind": record.identity.kind,
            "start_time": record.identity.start_time,
        }
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "record_kind": RECORD_KIND,
        "registry_id": record.registry_id,
        "pid": record.pid,
        "identity": identity,
        "route_id": record.route_id,
        "packet_id": record.packet_id,
        "owned_paths": list(record.owned_paths),
        "workdir": record.workdir,
        "started_at": record.started_at,
    }


def record_from_json(
    payload: Mapping[str, Any], path: Path | None = None
) -> RunRegistryRecord:
    """Decode and shape-validate one registry record payload.

    Fails closed (:class:`RunRegistryError`) on any missing or ill-typed
    field: the registry is consulted before a worker launch, so a record
    that cannot be understood must refuse rather than be silently skipped.
    """
    where = str(path) if path is not None else "<registry record>"
    if not isinstance(payload, Mapping):
        raise RunRegistryError(f"run registry record is not a JSON object: {where}")

    def _field(name: str, expected: type, *, allow_none: bool = False) -> Any:
        value = payload.get(name)
        if allow_none and value is None:
            return None
        if expected is str and (
            not isinstance(value, str) or (name != "workdir" and not value)
        ):
            raise RunRegistryError(
                f"run registry record field {name!r} must be a nonempty "
                f"string: {where}"
            )
        if expected is int and (not isinstance(value, int) or isinstance(value, bool)):
            raise RunRegistryError(
                f"run registry record field {name!r} must be an integer: {where}"
            )
        return value

    registry_id = _field("registry_id", str)
    pid = _field("pid", int)
    if pid <= 0:
        raise RunRegistryError(f"run registry record pid must be positive: {where}")
    route_id = _field("route_id", str)
    packet_id = _field("packet_id", str)
    started_at = _field("started_at", str)
    workdir = _field("workdir", str, allow_none=True)
    owned_paths = payload.get("owned_paths")
    if (
        not isinstance(owned_paths, list)
        or not owned_paths
        or not all(isinstance(item, str) and item for item in owned_paths)
    ):
        raise RunRegistryError(
            f"run registry record field 'owned_paths' must be a nonempty "
            f"list of nonempty strings: {where}"
        )

    identity_payload = payload.get("identity")
    identity: ProcessIdentity | None = None
    if identity_payload is not None:
        if not isinstance(identity_payload, Mapping):
            raise RunRegistryError(
                f"run registry record field 'identity' must be an object "
                f"or null: {where}"
            )
        kind = identity_payload.get("kind")
        start_time = identity_payload.get("start_time")
        if kind not in ("linux_start_time", "pid_only"):
            raise RunRegistryError(
                f"run registry record identity has unknown kind {kind!r}: {where}"
            )
        if start_time is not None and (
            not isinstance(start_time, int) or isinstance(start_time, bool)
        ):
            raise RunRegistryError(
                f"run registry record identity start_time must be an integer "
                f"or null: {where}"
            )
        if kind == "linux_start_time" and start_time is None:
            raise RunRegistryError(
                f"run registry record identity kind 'linux_start_time' "
                f"requires a start_time: {where}"
            )
        identity = ProcessIdentity(kind=kind, start_time=start_time)

    return RunRegistryRecord(
        registry_id=registry_id,
        pid=pid,
        identity=identity,
        route_id=route_id,
        packet_id=packet_id,
        owned_paths=tuple(owned_paths),
        workdir=workdir,
        started_at=started_at,
        path=path,
    )


# ---------------------------------------------------------------------------
# State-directory resolution and locking
# ---------------------------------------------------------------------------


def resolve_registry_dir(explicit: str | Path | None = None) -> Path:
    """Resolve the per-host live-run registry directory.

    Precedence mirrors :func:`lee_llm_router.staffing.ledger.resolve_attempts_path`:
    an explicit path wins, then :data:`RUN_REGISTRY_DIR_ENV_VAR`, then
    :data:`RUNS_STATE_ROOT_ENV_VAR` (``<root>/runs/<hostname>``), then
    ``<DEFAULT_RUNS_DIR>/<hostname>``.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    from_dir_env = os.environ.get(RUN_REGISTRY_DIR_ENV_VAR)
    if from_dir_env:
        return Path(from_dir_env).expanduser()
    from_root_env = os.environ.get(RUNS_STATE_ROOT_ENV_VAR)
    if from_root_env:
        base = Path(from_root_env).expanduser() / "runs"
    else:
        base = DEFAULT_RUNS_DIR.expanduser()
    return base / socket.gethostname()


try:  # pragma: no cover - exercised trivially on POSIX CI
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback only
    _fcntl = None


@contextmanager
def _registry_lock(registry_dir: Path) -> Iterator[None]:
    """Hold the exclusive registry lock for one check-and-register step.

    With ``fcntl`` (every POSIX platform this repo targets) the lock is a
    kernel ``flock`` on ``<registry>.lock`` next to the per-host directory:
    it is released by the kernel even when a holder crashes, so a crashed
    run never wedges the registry. Without ``fcntl`` the fallback is an
    atomic ``O_EXCL`` lock file with bounded retry and a stale takeover —
    best-effort only, documented as weaker than the ``flock`` guarantee.
    Lock acquisition failure raises :class:`RunRegistryError` (fail closed)
    instead of proceeding unserialized.
    """
    registry_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = registry_dir.parent / f"{registry_dir.name}.lock"
    if _fcntl is not None:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RunRegistryError(
                            f"run registry lock could not be acquired within "
                            f"{_LOCK_TIMEOUT_SECONDS:.0f}s: {lock_path}"
                        ) from None
                    time.sleep(0.01)
            try:
                yield
            finally:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return

    # Portable fallback: atomic O_EXCL create; a lock older than the stale
    # bound is taken over on the assumption its holder crashed.
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if time.monotonic() >= deadline:
                try:
                    age = max(0.0, time.time() - lock_path.stat().st_mtime)
                except OSError as exc:
                    raise RunRegistryError(
                        f"run registry fallback lock cannot be inspected: "
                        f"{lock_path}: {exc}"
                    ) from exc
                if age < _LOCK_FALLBACK_STALE_SECONDS:
                    raise RunRegistryError(
                        f"run registry fallback lock is still live after "
                        f"{_LOCK_TIMEOUT_SECONDS:.0f}s: {lock_path}"
                    )
                try:
                    lock_path.unlink()
                except OSError as exc:
                    raise RunRegistryError(
                        f"stale run registry fallback lock cannot be removed: "
                        f"{lock_path}: {exc}"
                    ) from exc
                continue
            time.sleep(0.01)
            continue
        try:
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        except OSError:
            pass
        finally:
            os.close(fd)
        try:
            yield
        finally:
            try:
                lock_path.unlink()
            except OSError:
                pass
        return


# ---------------------------------------------------------------------------
# Registry operations
# ---------------------------------------------------------------------------


def _load_records(registry_dir: Path) -> list[RunRegistryRecord]:
    """Load and shape-validate every record file (sorted, deterministic).

    Fails closed on an unreadable, unparsable, or shape-invalid record.
    Temporary files (hidden ``.tmp`` suffix) are never record files and are
    skipped by the ``*.json`` glob.
    """
    records: list[RunRegistryRecord] = []
    try:
        entries = sorted(registry_dir.glob("*.json"))
    except OSError as exc:
        raise RunRegistryError(
            f"run registry directory cannot be listed: {registry_dir}: {exc}"
        ) from exc
    for entry in entries:
        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RunRegistryError(
                f"run registry record cannot be read: {entry}: {exc}"
            ) from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RunRegistryError(
                f"run registry record is not valid JSON: {entry}: {exc}"
            ) from exc
        records.append(record_from_json(payload, path=entry))
    return records


def _unlink_record(record: RunRegistryRecord) -> None:
    """Remove one record file, tolerating an already-gone file."""
    if record.path is None:
        return
    try:
        record.path.unlink()
    except FileNotFoundError:
        pass


def _purge_stale_locked(
    registry_dir: Path,
) -> tuple[list[RunRegistryRecord], list[tuple[RunRegistryRecord, str]]]:
    """Under the lock: classify records live/stale and unlink stale ones.

    Returns ``(live, cleaned)`` where ``cleaned`` pairs each removed stale
    record with its stale reason. The unlink removes exactly the evaluated
    record's unique file, so a record registered after evaluation for a
    reused pid (a replacement record) can never be deleted by this step.
    """
    live: list[RunRegistryRecord] = []
    cleaned: list[tuple[RunRegistryRecord, str]] = []
    for record in _load_records(registry_dir):
        is_live, reason = evaluate_record_liveness(record)
        if is_live:
            live.append(record)
        else:
            _unlink_record(record)
            cleaned.append((record, reason))
    return live, cleaned


def _utc_now() -> str:
    """One aware UTC timestamp for a registration start."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_record(registry_dir: Path, record: RunRegistryRecord) -> None:
    """Atomically publish one record file (unique tmp + ``os.replace``)."""
    assert record.path is not None
    target = record.path
    tmp = target.parent / f".{record.registry_id}.tmp"
    line = json.dumps(record_to_json(record), ensure_ascii=False, sort_keys=True)
    payload = line.encode("utf-8") + b"\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        written = os.write(fd, payload)
    finally:
        os.close(fd)
    if written != len(payload):
        try:
            tmp.unlink()
        except OSError:
            pass
        raise RunRegistryError(
            f"run registry short write to {tmp}: wrote {written} of "
            f"{len(payload)} bytes"
        )
    try:
        os.replace(tmp, target)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise RunRegistryError(
            f"run registry record could not be published: {target}: {exc}"
        ) from exc


def register_run(
    *,
    route_id: str,
    packet_id: str,
    owned_paths: Sequence[str],
    workdir: str | Path | None = None,
    pid: int | None = None,
    identity: ProcessIdentity | None = None,
    started_at: str | None = None,
    registry_dir: str | Path | None = None,
) -> RunRegistryRecord:
    """Atomically check-and-register one run, refusing on intersection.

    Under the exclusive registry lock, stale records are purged first, then
    every remaining live record is checked for owned-path intersection with
    the normalized ``owned_paths``. Any intersection raises
    :class:`RegistryConflictError` (nothing is launched by the caller);
    otherwise the new record is written atomically and returned.

    Args:
        route_id: The selected route id.
        packet_id: Canonical packet identity (``sha256:...``).
        owned_paths: Owned paths; normalized against ``workdir`` (or the
            process working directory). Empty lists are refused.
        workdir: ``--workdir`` for relative-path normalization.
        pid: Registering process pid (default: this process).
        identity: Process identity (default: observed for ``pid``).
        started_at: Registration start timestamp (default: now, UTC).
        registry_dir: Explicit registry directory (default: resolved).

    Returns:
        The published :class:`RunRegistryRecord` (with its ``path``).

    Raises:
        RegistryConflictError: An intersecting live record exists; carries
            one ``(pid, route_id, packet_id, overlapping paths)`` tuple per
            conflicting record.
        RunRegistryError: Empty/invalid owned paths, an invalid pid, or any
            registry I/O/locking failure.
    """
    if pid is None:
        pid = os.getpid()
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise RunRegistryError(
            f"run registry pid must be a positive integer, got {pid!r}"
        )
    if identity is None:
        identity = _PROCESS_IDENTITY(pid)
    if started_at is None:
        started_at = _utc_now()
    normalized = normalize_owned_paths(owned_paths, workdir=workdir)

    directory = resolve_registry_dir(registry_dir)
    registry_id = uuid.uuid4().hex
    record = RunRegistryRecord(
        registry_id=registry_id,
        pid=pid,
        identity=identity,
        route_id=route_id,
        packet_id=packet_id,
        owned_paths=normalized,
        workdir=None if workdir is None else str(workdir),
        started_at=started_at,
        path=directory / f"{pid}-{registry_id}.json",
    )

    with _registry_lock(directory):
        live, _cleaned = _purge_stale_locked(directory)
        conflicts: list[tuple[int, str, str, tuple[str, ...]]] = []
        for existing in live:
            overlapping = tuple(
                path
                for path in normalized
                if any(paths_intersect(path, other) for other in existing.owned_paths)
            )
            if overlapping:
                conflicts.append(
                    (
                        existing.pid,
                        existing.route_id,
                        existing.packet_id,
                        overlapping,
                    )
                )
        if conflicts:
            parts = [
                f"pid {existing_pid} (route {route}, packet {packet}) owns "
                + ", ".join(repr(p) for p in overlapping)
                for existing_pid, route, packet, overlapping in conflicts
            ]
            raise RegistryConflictError(
                "owned paths intersect a live run: " + "; ".join(parts),
                conflicts=conflicts,
            )
        _write_record(directory, record)
    return record


def deregister_run(record: RunRegistryRecord | None) -> str | None:
    """Best-effort removal of one run's own registration record.

    Only the record the run itself registered is removed, by its unique
    registry-id file name. This is deliberately lock-free: the owner is the
    only process that removes this exact file, a concurrent census can only
    judge it live while the owner runs (so it never deletes it), and a
    missing file means someone else already cleaned it. The function never
    raises: any failure is returned as a warning note so a caller at a
    normal, failure, exception, timeout, or interrupt boundary can report
    it without masking the outcome it is already returning.
    """
    if record is None or record.path is None:
        return None
    try:
        record.path.unlink()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"could not deregister run registry record {record.path}: {exc}"
    return None


@dataclass(frozen=True)
class CensusCleaned:
    """One stale record removed by census, with its stale reason."""

    record: RunRegistryRecord
    reason: str


@dataclass(frozen=True)
class CensusResult:
    """One census pass: genuinely live records plus stale cleanups."""

    live: tuple[RunRegistryRecord, ...]
    cleaned: tuple[CensusCleaned, ...]


def census_registry(registry_dir: str | Path | None = None) -> CensusResult:
    """List genuinely live runs and clean stale records with a note.

    Under the exclusive registry lock every record is evaluated; stale
    records (dead pid, or pid reuse proven by a differing Linux start time)
    are removed and reported with their reason, and a replacement record
    registered for a reused pid is never touched. An absent registry
    directory is an empty result, not an error.

    Raises:
        RunRegistryError: On registry I/O failure or an unparsable/
            shape-invalid record (fail closed), or lock timeout.
    """
    directory = resolve_registry_dir(registry_dir)
    if not directory.is_dir():
        return CensusResult((), ())
    with _registry_lock(directory):
        live, cleaned = _purge_stale_locked(directory)
    return CensusResult(
        live=tuple(live),
        cleaned=tuple(CensusCleaned(record, reason) for record, reason in cleaned),
    )


def census_record_json(record: RunRegistryRecord) -> dict[str, Any]:
    """The JSON view of one live registry row for ``census --json``."""
    return {
        "pid": record.pid,
        "identity": (
            None
            if record.identity is None
            else {
                "kind": record.identity.kind,
                "start_time": record.identity.start_time,
            }
        ),
        "route_id": record.route_id,
        "packet_id": record.packet_id,
        "owned_paths": list(record.owned_paths),
        "workdir": record.workdir,
        "started_at": record.started_at,
        "registry_path": str(record.path) if record.path is not None else None,
    }
