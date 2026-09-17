"""Stall watchdog for long-running dispatches.

Supervises a single child process across ticks and decides whether it is
making progress. "Progress" is judged on two independent signals: the
cumulative stdout+stderr byte count the caller has observed so far, and a
snapshot of files under a set of watch directories (size + mtime). Either
signal changing counts as activity — a tool that writes files quietly but
emits no output is not stalled, and a tool that streams output while never
touching its watch directories is not stalled either.

A stall is flagged when joint silence (no output and no file changes) persists
for ``stall_seconds``. With ``stall_action="kill"``, a stall kills the process
on its first stalled tick (after firing ``on_stall`` once); with
``stall_action="warn"``, a stall warns but never kills on its own. When
``progress_seconds`` is configured, failure to modify files in watch directories
for that duration kills the process for lack of progress, regardless of output
activity. With no watch directories configured, the progress clock is inactive
(nothing to measure). The absolute wall-clock ceiling (``max_seconds``) always
kills, and the ceiling always wins over stall and no-progress verdicts.

Nothing here touches a real subprocess or the wall clock: :class:`StallWatchdog`
takes an injected ``clock`` callable and :func:`run_supervised` takes an
injected ``sleep`` callable, so the whole module is deterministic and fast
under test.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Sequence

DEFAULT_STALL_MINUTES = 10
"""Default minutes of joint silence before a dispatch is flagged stalled.

Read by the later CLI-wiring packet; unused directly in this module.
"""

DEFAULT_MAX_MINUTES = 120
"""Default wall-clock ceiling in minutes before a dispatch is killed.

Read by the later CLI-wiring packet; unused directly in this module.
"""


class Verdict(Enum):
    """The three outcomes :meth:`StallWatchdog.observe` can return."""

    CONTINUE = "continue"
    STALLED = "stalled"
    KILLED = "killed"


@dataclass(frozen=True)
class ActivitySignature:
    """A comparable snapshot of "has anything happened" at one tick.

    Equality between two signatures means no activity occurred between them:
    neither the cumulative output byte count nor the watched filesystem state
    changed.
    """

    output_bytes: int
    watch_signature: tuple


@dataclass(frozen=True)
class StallReport:
    """Snapshot handed to ``on_stall`` the first tick a stall is declared."""

    stalled_for_seconds: float
    elapsed_seconds: float
    output_bytes_total: int
    watch_dirs: tuple


@dataclass(frozen=True)
class SupervisedResult:
    """Outcome of one :func:`run_supervised` call."""

    exit_code: int | None
    killed: bool
    stalled: bool
    elapsed_seconds: float
    output_bytes_total: int
    kill_reason: str | None = None


def scan_watch_dirs(paths: Sequence[Path]) -> tuple:
    """Read a sorted activity fingerprint for regular files under ``paths``.

    Each entry is ``(relative_path, size, mtime_ns)`` where ``relative_path``
    is the file's path relative to whichever watch directory contains it,
    rendered with ``/`` separators for stable cross-platform sorting. Missing
    or unreadable directories, and individual files that vanish or become
    unreadable mid-scan (a race with the process being watched), contribute
    nothing rather than raising. Returns ``()`` when ``paths`` is empty or
    none of them yield anything.
    """
    entries: list[tuple[str, int, int]] = []
    for base in paths:
        try:
            base_path = Path(base)
            if base_path.is_file():
                try:
                    stat = base_path.stat()
                    entries.append((base_path.name, stat.st_size, stat.st_mtime_ns))
                except OSError:
                    pass
                continue
            if not base_path.is_dir():
                continue
            for root, _dirs, files in os.walk(base_path):
                for name in files:
                    file_path = Path(root) / name
                    try:
                        if not file_path.is_file():
                            continue
                        stat = file_path.stat()
                    except OSError:
                        continue
                    rel = file_path.relative_to(base_path).as_posix()
                    entries.append((rel, stat.st_size, stat.st_mtime_ns))
        except OSError:
            continue
    return tuple(sorted(entries))


class StallWatchdog:
    """Decides ``CONTINUE | STALLED | KILLED`` for one supervised child."""

    def __init__(
        self,
        *,
        stall_seconds: float,
        max_seconds: float,
        clock: Callable[[], float],
        watch_dirs: Sequence[Path] = (),
        on_stall: Callable[[StallReport], None] | None = None,
        stall_action: str = "warn",
        progress_seconds: float | None = None,
    ) -> None:
        if stall_action not in ("warn", "kill"):
            raise ValueError(
                f"stall_action must be 'warn' or 'kill', got {stall_action!r}"
            )
        self._stall_seconds = stall_seconds
        self._max_seconds = max_seconds
        self._clock = clock
        self._watch_dirs = tuple(watch_dirs)
        self._on_stall = on_stall
        self._stall_action = stall_action
        self._progress_seconds = (
            float(progress_seconds)
            if progress_seconds is not None and progress_seconds > 0
            else None
        )

        now = self._clock()
        self._started_at = now
        self._last_activity_at = now
        self._last_progress_at = now
        self._last_watch_signature: tuple | None = None
        self._last_signature: ActivitySignature | None = None
        self._stalled = False
        self._stall_reported = False
        self._kill_reason: str | None = None

    @property
    def started_at(self) -> float:
        return self._started_at

    @property
    def last_activity_at(self) -> float:
        return self._last_activity_at

    @property
    def kill_reason(self) -> str | None:
        return self._kill_reason

    @property
    def stalled(self) -> bool:
        return self._stalled

    @property
    def elapsed_seconds(self) -> float:
        """Seconds since construction, per the injected clock."""
        return self._clock() - self._started_at

    def observe(self, *, output_bytes_total: int) -> Verdict:
        """Advance the watchdog by one tick and return its verdict."""
        now = self._clock()
        watch_signature = scan_watch_dirs(self._watch_dirs)
        signature = ActivitySignature(
            output_bytes=output_bytes_total, watch_signature=watch_signature
        )

        activity = self._last_signature is None or signature != self._last_signature
        self._last_signature = signature

        if activity:
            self._last_activity_at = now
            self._stalled = False
            self._stall_reported = False
        else:
            silent_for = now - self._last_activity_at
            if silent_for >= self._stall_seconds:
                self._stalled = True

        watch_changed = (
            self._last_watch_signature is None
            or watch_signature != self._last_watch_signature
        )
        self._last_watch_signature = watch_signature
        if watch_changed:
            self._last_progress_at = now

        if now - self._started_at >= self._max_seconds:
            self._kill_reason = "ceiling"
            return Verdict.KILLED

        if self._stalled:
            if not self._stall_reported:
                self._stall_reported = True
                if self._on_stall is not None:
                    self._on_stall(
                        StallReport(
                            stalled_for_seconds=now - self._last_activity_at,
                            elapsed_seconds=now - self._started_at,
                            output_bytes_total=output_bytes_total,
                            watch_dirs=self._watch_dirs,
                        )
                    )
            if self._stall_action == "kill":
                self._kill_reason = "stall"
                return Verdict.KILLED

        if (
            self._progress_seconds is not None
            and self._watch_dirs
            and (now - self._last_progress_at >= self._progress_seconds)
        ):
            self._kill_reason = "no_progress"
            return Verdict.KILLED

        return Verdict.STALLED if self._stalled else Verdict.CONTINUE


def run_supervised(
    popen_like,
    watchdog: StallWatchdog,
    *,
    poll_seconds: float,
    sleep: Callable[[float], None],
    read_chunk: Callable[[], bytes | None],
    sink: Callable[[bytes], None] = lambda _chunk: None,
) -> SupervisedResult:
    """Drive one child process to completion or a watchdog kill.

    Polls ``popen_like`` on a ``poll_seconds`` cadence (via the injected
    ``sleep``), draining whatever ``read_chunk`` returns on each pass into
    ``sink`` and into the cumulative byte count fed to ``watchdog.observe``.
    On :data:`Verdict.KILLED` (ceiling, stall-kill, or no-progress) the
    process is killed and waited on; under ``stall_action="warn"`` a stall
    never kills. Returns once the process has exited or been killed.
    """
    output_bytes_total = 0
    ever_stalled = False
    killed = False
    kill_reason: str | None = None
    exit_code: int | None = None

    while True:
        chunk = read_chunk()
        if chunk:
            output_bytes_total += len(chunk)
            sink(chunk)

        verdict = watchdog.observe(output_bytes_total=output_bytes_total)
        if verdict is Verdict.STALLED:
            ever_stalled = True

        if verdict is Verdict.KILLED:
            killed = True
            kill_reason = watchdog.kill_reason
            popen_like.kill()
            exit_code = popen_like.wait(poll_seconds)
            break

        exit_code = popen_like.poll()
        if exit_code is not None:
            # Drain any final chunk emitted right at exit.
            trailing = read_chunk()
            if trailing:
                output_bytes_total += len(trailing)
                sink(trailing)
                watchdog.observe(output_bytes_total=output_bytes_total)
            break

        sleep(poll_seconds)

    elapsed_seconds = watchdog.elapsed_seconds
    return SupervisedResult(
        exit_code=exit_code,
        killed=killed,
        stalled=ever_stalled,
        elapsed_seconds=elapsed_seconds,
        output_bytes_total=output_bytes_total,
        kill_reason=kill_reason,
    )
