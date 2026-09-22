"""Staffing ``run`` selection and subprocess dispatch (P1-5a).

Implements the selection-plus-dispatch foundation of the ``run`` command
(D209 ruling 4 via ``docs/staffing/phase1-contracts.md`` §``run``):

* **Selection.** Role/class selection (always required; ``--route`` is
  optional on top, per the Chief's corrected D209 contract in
  ``docs/staffing/chief-answers-p1-1.md``), over
  exactly the ``catalog explain`` path (:func:`evaluate_eligibility` over
  the committed catalog, an availability snapshot, dated terms, and
  marginal pricing). An explicit route must be active and currently
  eligible under the same role/class path; an excluded explicit route is
  a refusal (exit 3) carrying the
  exact explain reason, and nothing is launched. Role/class selection
  takes the first eligible route in ``catalog explain --json``
  marginal-price order and preserves the selection basis, reason, explain
  reference, and every excluded route id/reason — explain evidence only,
  never a new selection judgment (D206).
* **Dispatch.** The selected route's harness provider ``build_command``
  argv is substituted with the packet prompt and run exactly once through
  the repository's watchdog/subprocess boundary
  (:func:`run_supervised_dispatch` below), with ``cwd`` set to the
  workdir and no shell interpolation anywhere. Pi runs its JSON event
  mode and captures usage through the accepted P1-4a parser; Codex forces
  ``json_flag: --json`` (the P1-4b governed capture note) and parses the
  JSONL receipt; Claude reuses the committed governed capture
  (``--output-format stream-json``) and parses the result event through
  :func:`capture_claude_usage`.
  Governed dispatch (P1-8) also grants implementation routes the minimum
  noninteractive editing capability: Pi receives an explicit bounded
  editing tool allowlist (no ``bash``), Codex receives the exec-level
  writable-workspace sandbox and git-repo-check skip flags, and Claude
  receives the documented noninteractive permission flags
  (``--permission-mode acceptEdits --permission-prompts none``) so scoped
  edits are accepted without an interactive host; no dangerous bypass
  flag is ever emitted on any harness.
  Every other wired harness runs the same boundary and records
  ``usage.basis: unavailable`` with a specific reason — no parser ever
  invents tokens.

P1-5b2 completes the boundary: after the one worker and optional oracle,
``run`` assembles one v2 attempt record, calculates list/marginal cost only
from known counters and the selected dated price, validates it through the
committed ledger API, appends it once, and prints the same record compactly.
``run`` never escalates; parent and escalation-reason are only recorded as a
paired link supplied by its supervisor.

P1-4 ruling 3 (``docs/staffing/chief-answers-p1-4.md``, Astra final-review
finding 5) adds the optional caller attestation ``--supervisor-route <route
id>``: the caller attests its own route. The attested id is validated as a
governed *identity* — it must name a known catalog route that is active and
currently usable (every governed exclusion that is not role/class policy
still refuses, exit 3, before anything is launched) — but the worker's
role/class capability policy is never imposed on the supervisor: a
role-scoped or otherwise class-ineligible model may still attest, because
attesting one's identity is not performing the worker's task. The accepted
supervisor-route object (identity tuple plus observed provider) is recorded
only when the caller supplies the attestation. When the attested dispatch
succeeds, the oracle passes, and every existing evidence gate holds,
``verified_success`` is true; otherwise the record carries exactly one
``verified_success_reason`` from the closed P1-4 vocabulary
(:data:`VERIFIED_SUCCESS_REASON_UNATTESTED` when unattested, otherwise the
first blocking gap in a fixed documented order) — never a claimed success.

Chief round 15 packet D's ``--author-route`` (Astra final-review finding 4)
is forwarded to the same ``catalog explain`` eligibility path so ``run``
can enforce reviewer independence: for a review/judge class the author
route and every same-family candidate are excluded with the reason
``independence`` exactly as ``catalog explain --author-route`` reports, an
unknown author route fails closed, and an excluded explicit route is a
refusal carrying the exact explain reason. Without ``--author-route``
independence is not evaluated, exactly as in explain.

Astra final-review finding 6 also governs the failure boundary: all
caller-controlled record metadata (``--parent``/``--escalation-reason``
pair and pattern, ``attempt_id``) is validated before anything is launched
(:func:`validate_attempt_metadata`), and when the optional oracle cannot be
launched or set up *after* the worker completed, the completed worker is
never dropped: exactly one schema-valid attempt is persisted preserving the
worker evidence and the oracle failure (truthful verdict, failure class,
and ``verified_success``), then the CLI returns its governed failure.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

import yaml

from lee_llm_router.availability import AvailabilitySnapshot
from lee_llm_router.crews import HARNESS_PROVIDER_CHANNELS
from lee_llm_router.doctor import _INDEPENDENCE_APPLICABLE_ROLES
from lee_llm_router.providers.antigravity_cli import (
    AGY_GOVERNED_CONFIG,
    AGY_USAGE_SOURCE,
    AntigravityCLIProvider,
)
from lee_llm_router.providers.antigravity_cli import (
    capture_usage as capture_agy_usage,
)
from lee_llm_router.providers.base import FailureType, LLMRouterError
from lee_llm_router.providers.codex_cli import (
    CLAUDE_GOVERNED_CONFIG,
    CLAUDE_USAGE_SOURCE,
    CODEX_USAGE_SOURCE,
    ClaudeCodeCLIProvider,
    CodexCLIProvider,
    capture_claude_usage,
    claude_aggregate_models,
)
from lee_llm_router.providers.codex_cli import (
    capture_usage as capture_codex_usage,
)
from lee_llm_router.providers.omp_cli import (
    OMP_GOVERNED_CONFIG,
    OMP_USAGE_SOURCE,
    OmpCLIProvider,
)
from lee_llm_router.providers.omp_cli import (
    capture_usage as capture_omp_usage,
)
from lee_llm_router.providers.opencode_cli import (
    OPENCODE_GOVERNED_CONFIG,
    OPENCODE_USAGE_SOURCE,
    OpenCodeCLIProvider,
)
from lee_llm_router.providers.opencode_cli import (
    capture_usage as capture_opencode_usage,
)
from lee_llm_router.providers.pi_cli import (
    PI_USAGE_SOURCE,
    PiCLIProvider,
)
from lee_llm_router.providers.pi_cli import capture_usage as capture_pi_usage
from lee_llm_router.staffing.catalog import Route as StaffingRoute
from lee_llm_router.staffing.catalog import StaffingCatalog
from lee_llm_router.staffing.eligibility import (
    _SAME_FAMILY_REVIEW_DISCLOSURE,
    EligibilityPrice,
    EligibilityRow,
    StaffingEligibilityError,
    evaluate_eligibility,
)
from lee_llm_router.staffing.json_int import int_to_decimal
from lee_llm_router.staffing.terms import (
    DEFAULT_OPENROUTER_SNAPSHOT_PATH,
    DEFAULT_RATE_TABLE_PATH,
    _verify_sha256,
)
from lee_llm_router.watchdog import (
    DEFAULT_MAX_MINUTES,
    DEFAULT_STALL_MINUTES,
    StallReport,
    StallWatchdog,
    run_supervised,
    scan_watch_dirs,
)

__all__ = [
    "ARTIFACTS_DIR_ENV_VAR",
    "ARTIFACTS_FILE_ENV_VAR",
    "ARTIFACTS_STATE_ROOT_ENV_VAR",
    "DEFAULT_ARTIFACTS_DIR",
    "resolve_artifacts_dir",
    "resolve_artifacts_path",
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_RUN_TIMEOUT_SECONDS",
    "VERIFIED_SUCCESS_REASON_EVIDENCE",
    "VERIFIED_SUCCESS_REASON_ORACLE",
    "VERIFIED_SUCCESS_REASON_UNATTESTED",
    "VERIFIED_SUCCESS_REASON_WORKER_DISPATCH",
    "DispatchOutcome",
    "OracleOutcome",
    "RunDispatchError",
    "PROMPT_PLACEHOLDER",
    "Resolution",
    "run_supervised_dispatch",
    "build_attempt_record",
    "packet_id_for_text",
    "RunSelectionError",
    "SELECTION_BASIS_EXPLAIN_CHEAPEST_ELIGIBLE",
    "SELECTION_BASIS_EXPLICIT",
    "SELECTION_EXIT_CODE",
    "SelectionOutcome",
    "build_dispatch_command",
    "dispatch_route",
    "oracle_timeout_seconds",
    "parse_oracle_command",
    "run_json_record",
    "run_oracle",
    "run_summary_lines",
    "select_route",
    "selection_record",
    "unchanged_redispatch_refusal",
    "validate_attempt_metadata",
    "NO_WORK_NOTE_PREFIX",
    "no_work_evidence",
    "stdout_has_work_evidence",
]

SELECTION_EXIT_CODE = 3
"""Process exit code for every selection refusal (mirrors ``catalog explain``)."""

SELECTION_BASIS_EXPLICIT = "explicit"
"""Selection basis when the caller named the route with ``--route``."""

SELECTION_BASIS_EXPLAIN_CHEAPEST_ELIGIBLE = "explain_cheapest_eligible"
"""Selection basis for role/class selection: first eligible route in the
``catalog explain --json`` marginal-price order."""

VERIFIED_SUCCESS_REASON_UNATTESTED = "supervisor_route_unattested"
"""P1-4 ruling 3: exact ``verified_success_reason`` recorded on every false
``router_run`` record built without a ``--supervisor-route`` attestation."""

VERIFIED_SUCCESS_REASON_WORKER_DISPATCH = "worker_dispatch_failed"
"""Attested but the worker dispatch failed: nonzero exit or ceiling timeout."""

VERIFIED_SUCCESS_REASON_ORACLE = "oracle_not_passed"
"""Attested with a successful dispatch but the oracle verdict is not ``pass``
(no oracle / unverified, nonzero exit, launch failure, or ceiling timeout)."""

VERIFIED_SUCCESS_REASON_EVIDENCE = "evidence_incomplete"
"""Attested with a successful dispatch and a passing oracle, but a remaining
route/usage/cost/duration evidence gate does not hold."""

DEFAULT_RUN_TIMEOUT_SECONDS: float | None = None
"""Default wall-clock ceiling: none, so the dispatch boundary default applies
(:data:`lee_llm_router.watchdog.DEFAULT_MAX_MINUTES`)."""

DEFAULT_POLL_SECONDS = 0.5
"""Watchdog polling cadence forwarded to the dispatch boundary."""

_TIMEOUT_EXIT_CODE = 124
"""Exit code the dispatch boundary reports for a ceiling timeout."""

PROMPT_PLACEHOLDER = "{prompt}"
"""Argv element that marks a harness as taking its prompt on the command line.

Moved here (P2-9 removal) from the deleted ``lee_llm_router.resolver``; the
per-harness provider modules carry their own identical constant."""

NO_WORK_NOTE_PREFIX = "dispatch no-work:"
"""Stable provenance-note prefix for a clean dispatch that produced no effective
work evidence: exit 0, no oracle, no owned-path change, and no substantive
answer/result in stdout.

The note is machine-readable evidence for :func:`unchanged_redispatch_refusal`
and is used by no other rule. The record is a governed failure: it carries the
committed schema-valid class ``platform_env`` (the router's no-work shape is a
no-launch/platform surface per the filed needs-lee note) in addition to this
provenance evidence. A substantive stdout answer, an oracle, an owned-path
change, a nonzero exit, or a timeout is never no work.
"""


@dataclass(frozen=True)
class Resolution:
    """Minimal dispatch-boundary resolution (moved from the deleted resolver).

    Only the fields the supervision boundary actually consumes are kept:
    the stall warning names the worker, and the boundary needs the final
    argv plus how the prompt is delivered. Event and provenance shapes live
    in :mod:`lee_llm_router.events` and the attempt record."""

    worker_id: str
    dispatch_command: list[str]
    prompt_delivery: str


DEFAULT_ORACLE_TIMEOUT_SECONDS = DEFAULT_MAX_MINUTES * 60.0
"""Standard wall-clock bound for an oracle when ``--timeout`` is absent."""

DEFAULT_ARTIFACTS_DIR = Path("~/.local/state/lee-llm-router/artifacts")
"""Directory holding attempt artifacts per attempt id (``~`` expanded at use)."""

ARTIFACTS_DIR_ENV_VAR = "LEE_LLM_ROUTER_ARTIFACTS_DIR"
"""Test-only override of the directory holding per-attempt artifacts."""

ARTIFACTS_FILE_ENV_VAR = "LEE_LLM_ROUTER_ARTIFACTS_FILE"
"""Test-only override of the artifacts directory (analogue to attempts file env var)."""

ARTIFACTS_STATE_ROOT_ENV_VAR = "LEE_LLM_ROUTER_ARTIFACTS_STATE_ROOT"
"""Test-only override of the app state root the ``artifacts/`` dir lives under."""


def resolve_artifacts_dir(explicit: str | Path | None = None) -> Path:
    """Resolve the directory holding attempt artifacts.

    Precedence mirrors :func:`lee_llm_router.staffing.ledger.resolve_attempts_path`:
    an explicit path wins, then :data:`ARTIFACTS_DIR_ENV_VAR` /
    :data:`ARTIFACTS_FILE_ENV_VAR`, then :data:`ARTIFACTS_STATE_ROOT_ENV_VAR`
    (``<root>/artifacts``), then :data:`DEFAULT_ARTIFACTS_DIR`.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    from_file_env = os.environ.get(ARTIFACTS_DIR_ENV_VAR) or os.environ.get(
        ARTIFACTS_FILE_ENV_VAR
    )
    if from_file_env:
        return Path(from_file_env).expanduser()
    from_root_env = os.environ.get(ARTIFACTS_STATE_ROOT_ENV_VAR)
    if from_root_env:
        return Path(from_root_env).expanduser() / "artifacts"
    return DEFAULT_ARTIFACTS_DIR.expanduser()


resolve_artifacts_path = resolve_artifacts_dir

# Injected boundaries default to the real subprocess and clock so the CLI
# path is real; tests monkeypatch these module names for fake subprocesses.
_DEFAULT_POPEN: Callable[..., Any] = subprocess.Popen
_DEFAULT_CLOCK: Callable[[], float] = time.monotonic
_DEFAULT_SLEEP: Callable[[float], None] = time.sleep

#: Route harness -> provider class wired for ``run`` dispatch (P1-5a).
#: ``pi_cli`` is deliberately instantiated directly (it is a command builder,
#: not a registered completion provider); the rest mirror the committed
#: harness mappings in :data:`lee_llm_router.crews.WORKER_ENV_PREFIX_PROVIDERS`.
_HARNESS_PROVIDER_CLASSES: dict[str, type] = {
    "pi": PiCLIProvider,
    "codex": CodexCLIProvider,
    "claude": ClaudeCodeCLIProvider,
    "agy": AntigravityCLIProvider,
    "opencode": OpenCodeCLIProvider,
    "omp": OmpCLIProvider,
}

#: Route harness -> registered provider name, for provenance records only.
_HARNESS_PROVIDER_NAMES: dict[str, str] = {
    harness: provider_cls.name
    for harness, provider_cls in _HARNESS_PROVIDER_CLASSES.items()
}

#: Route harness -> authoritative usage taxonomy source string.
_HARNESS_USAGE_SOURCES: dict[str, str] = {
    "pi": PI_USAGE_SOURCE,
    "codex": CODEX_USAGE_SOURCE,
    "claude": CLAUDE_USAGE_SOURCE,
    "agy": AGY_USAGE_SOURCE,
    "opencode": OPENCODE_USAGE_SOURCE,
    "omp": OMP_USAGE_SOURCE,
}

#: Funding channel -> Pi backend provider id, the exact reverse of the
#: committed :data:`lee_llm_router.crews.HARNESS_PROVIDER_CHANNELS` mapping
#: (openai-codex -> openai-sub, openrouter -> openrouter,
#: opencode-go -> opencode-go, anthropic -> anthropic-sub). No id is invented.
_CHANNEL_TO_PI_PROVIDER_ID: dict[str, str] = {
    channel: provider for provider, channel in HARNESS_PROVIDER_CHANNELS.items()
}

_CODEX_GOVERNED_CONFIG: dict[str, Any] = {
    "json_flag": "--json",
    "sandbox_args": ["-s", "workspace-write", "--skip-git-repo-check"],
}
"""P1-4b future note + P1-8 governed edit fix: governed ``run`` capture
forces Codex to emit the JSONL usage receipt stream that
``codex_cli.capture_usage`` parses (``json_flag``), and grants the
exec-level flags a headless implementation worker needs to edit its
scoped file: ``-s workspace-write`` for a writable workspace sandbox and
``--skip-git-repo-check`` because scratch workdirs are not git
repositories. No approval/sandbox bypass flag is used."""

_RUN_EDIT_TOOLS = "read,edit,write,grep,find,ls"
"""P1-8 governed Pi editing tools for ``run`` dispatch: an explicit
bounded allowlist built from pi's committed built-in tool set minus
``bash`` — file edit/create capability for scoped implementation files
without shell escape. Pi's ``--mode json`` usage capture is unaffected."""

#: Governed ``run`` capture and noninteractive editing reuse the committed
#: Claude config (:data:`CLAUDE_GOVERNED_CONFIG`) verbatim, so ``claude -p``
#: emits the result event that ``codex_cli.capture_claude_usage`` parses
#: and accepts scoped edits via ``--permission-mode acceptEdits`` with
#: ``--permission-prompts none``; no bypass flag is ever emitted.


_TOKENS_PER_1M = 1_000_000

#: Harnesses whose authoritative ``total_tokens`` includes the cache-read
#: and cache-write components (Pi and OMP sum ``cacheRead``/``cacheWrite``
#: into ``totalTokens``; the Claude result event reports the same four
#: components). Codex treats cached input as a subset of input with
#: ``total = input + output``, and the proven agy receipt semantics keep
#: cache counters outside ``total_tokens``, so neither can derive cache
#: billing evidence from the total.
_TOTAL_INCLUDES_CACHE_HARNESSES = frozenset({"pi", "omp", "claude"})


class RunSelectionError(Exception):
    """A ``run`` selection refusal: nothing launched, the CLI exits 3.

    Mirrors the old resolver's ``ResolutionError``: a plain
    refusal carrying the process exit code and a stable machine ``kind``
    (``excluded``, ``unknown_route``, ``unknown_instance``, ``no_eligible``,
    ``invalid_class``, ``invalid_date``, ``invalid_metadata``), not a
    provider failure.
    """

    def __init__(
        self,
        message: str,
        *,
        exit_code: int = SELECTION_EXIT_CODE,
        kind: str,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.kind = kind
        self.cause = cause


class RunDispatchError(LLMRouterError):
    """Raised when the selected route's harness cannot be dispatched.

    Chains into :class:`LLMRouterError` with
    :attr:`FailureType.PROVIDER_ERROR`; nothing has been launched when it
    is raised before the subprocess boundary.
    """

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message, failure_type=FailureType.PROVIDER_ERROR, cause=cause)


# ---------------------------------------------------------------------------
# Typed results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SelectionOutcome:
    """One selected route plus the explain evidence behind the selection.

    ``basis`` is :data:`SELECTION_BASIS_EXPLICIT` or
    :data:`SELECTION_BASIS_EXPLAIN_CHEAPEST_ELIGIBLE`; ``excluded`` carries
    every excluded route's id and its exact explain reasons joined with
    ``"; "`` — explain evidence preserved verbatim, never re-judged.

    ``supervisor_route`` is the accepted supervisor-route object
    (``model``/``effort``/``harness``/``channel``/``provider``) built from the
    catalog when the caller attests one via ``--supervisor-route`` (P1-4
    ruling 3); ``None`` means the caller made no attestation and no
    supervisor identity is invented.
    """

    route: StaffingRoute
    basis: str
    reason: str
    explain_ref: str
    excluded: tuple[tuple[str, str], ...]
    pricing: EligibilityPrice | None = None
    supervisor_route: dict[str, Any] | None = None
    cache_replacement_usd_per_token: float | None = None
    cache_marginal_usd_per_token: float | None = None
    cache_rates_checked: bool = False
    channel_kind: str | None = None
    """The selected route's ``channels.yaml`` ``kind`` (``subscription``,
    ``metered``, or ``local``), or ``None`` when the channel is not in the
    committed channel catalog. Fail-closed callers must treat ``None`` like
    a metered channel: only a channel positively known to be non-metered
    excuses missing per-token cost evidence."""
    channel_instance: str | None = None
    """The resolved instance id for the selected route's channel, or ``None``
    when the channel is not a subscription channel (empty instance_headrooms)
    or no instance clears."""


@dataclass(frozen=True)
class DispatchOutcome:
    """One completed subprocess dispatch of the selected route.

    ``argv`` is the final child argv (prompt substituted, no placeholder
    left); ``exit_code`` is the child's exit code, or ``124`` on a ceiling,
    stall, or no-progress timeout (``timed_out`` True); ``stdout``/``stderr``
    are the captured streams decoded with ``errors="replace"``; ``usage`` is
    the schema-valid attempt-record v2 usage mapping from the accepted harness
    capture function; ``usage_models`` is the sorted distinct Claude
    ``modelUsage`` model ids when the receipt is a Claude result event with
    ``modelUsage`` evidence (billing-relevance evidence for cost; ``None``
    otherwise); ``kill_reason`` is ``"ceiling"``, ``"stall"``, or
    ``"no_progress"`` when killed, or ``None`` on normal completion.
    """

    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    usage: dict[str, Any]
    usage_models: tuple[str, ...] | None = None
    kill_reason: str | None = None
    stall_minutes: float | None = None
    progress_minutes: float | None = None
    max_minutes: float | None = None
    owned_paths_changed: bool | None = None
    """Whether any watched owned path changed over the dispatch window.

    ``True``/``False`` when the dispatch boundary measured the owned paths
    (the CLI always supplies a non-empty owned-path set); ``None`` when no
    path was watched and the router therefore must not claim either way.
    """


@dataclass(frozen=True)
class OracleOutcome:
    """Captured result of one optional verification-oracle invocation.

    A missing oracle is represented by ``None`` at the result-building
    boundary, not by this class.  An oracle that cannot be launched has a
    ``None`` exit code and a deterministic ``error``; an oracle killed at its
    wall-clock bound has exit code ``124`` and ``timed_out=True``.
    """

    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    error: str | None = None


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _marginal_price_sort_key(row: EligibilityRow) -> tuple[int, float, float, str]:
    """Marginal-price order: priced rows ascend, unpriced last, id tie-break.

    Exactly the committed ``catalog explain`` display key
    (``doctor._explain_sort_key``): the display ordering the
    ``catalog explain --json`` report presents, so role/class selection
    picks the same first eligible route the report shows first.
    """
    pricing = row.pricing
    if pricing is None:
        return (1, 0.0, 0.0, row.route_id)
    return (
        0,
        pricing.marginal_input_usd_per_token,
        pricing.marginal_output_usd_per_token,
        row.route_id,
    )


def _excluded_summaries(
    rows: tuple[EligibilityRow, ...],
) -> tuple[tuple[str, str], ...]:
    """Explain-evidence excluded summaries: ``(route_id, joined reasons)``.

    Reasons are the exact explain reasons joined with ``"; "`` in row
    order; excluded routes always carry at least one reason.
    """
    return tuple(
        (row.route_id, "; ".join(row.reasons)) for row in rows if not row.eligible
    )


def _normalize_at_date(at_date: date | str) -> date:
    """Normalize ``at_date`` to a :class:`datetime.date`, failing closed."""
    if isinstance(at_date, date):
        return at_date
    try:
        return date.fromisoformat(at_date)
    except (TypeError, ValueError) as exc:
        raise RunSelectionError(
            f"at_date: not an ISO date (YYYY-MM-DD): {at_date!r}",
            kind="invalid_date",
            cause=exc,
        ) from exc


def _channel_kind(catalog: StaffingCatalog, channel_id: str) -> str | None:
    """Return the committed ``channels.yaml`` ``kind`` for ``channel_id``.

    ``None`` when the channel is absent from the catalog; never guessed
    from the channel id's spelling.
    """
    return next(
        (
            channel.kind
            for channel in catalog.channels.channels
            if channel.channel_id == channel_id
        ),
        None,
    )


def _resolve_channel_instance(
    row: EligibilityRow,
    instance_id: str | None,
) -> str | None:
    """Resolve the channel instance id for the selected route.

    Without ``instance_id``, returns the first eligible instance from the
    route's ``instance_headrooms`` (matching ``staff auto``'s selection rule),
    or ``None`` if the channel has no instance concept or no instance is
    eligible.

    With ``instance_id``, verifies that the route's channel has instances,
    the requested instance exists and is enabled, and is currently eligible.
    Otherwise raises :class:`RunSelectionError` with exit code 3.
    """
    if instance_id is None:
        for entry in row.instance_headrooms:
            if getattr(entry, "eligible", False):
                inst_id = getattr(entry, "instance_id", None)
                if inst_id:
                    return inst_id
        return None

    if not row.instance_headrooms:
        raise RunSelectionError(
            f"cannot specify --instance {instance_id!r} for route {row.route_id!r}: "
            f"channel {row.channel!r} has no instance concept",
            kind="unknown_instance",
        )

    matched = next(
        (
            entry
            for entry in row.instance_headrooms
            if getattr(entry, "instance_id", None) == instance_id
        ),
        None,
    )
    if matched is None:
        raise RunSelectionError(
            f"--instance {instance_id!r} does not match any enabled "
            f"instance for route {row.route_id!r}",
            kind="unknown_instance",
        )

    if not getattr(matched, "eligible", False):
        reasons_str = "; ".join(getattr(matched, "reasons", ()))
        raise RunSelectionError(
            f"instance {instance_id!r} of route {row.route_id!r} is not "
            f"eligible: {reasons_str}",
            kind="excluded",
        )

    return instance_id


def select_route(
    catalog: StaffingCatalog,
    availability: AvailabilitySnapshot,
    *,
    role: str,
    oracle_type: str,
    size_band: str,
    language: str,
    domain_tags: tuple[str, ...] | list[str] = (),
    class_key: str,
    at_date: date | str,
    route_id: str | None = None,
    instance_id: str | None = None,
    author_route_id: str | None = None,
    supervisor_route_id: str | None = None,
    openrouter_snapshot_path: str | Path | None = None,
    rate_table_path: str | Path | None = None,
) -> SelectionOutcome:
    """Select exactly one route, or refuse with nothing launched.

    Both bases evaluate eligibility over exactly the ``catalog explain``
    path — :func:`evaluate_eligibility` with the committed catalog, the
    supplied availability snapshot, dated terms at ``at_date``, and
    marginal pricing. No other checks, probabilities, or ladders apply.

    Args:
        catalog: The loaded staffing catalog (read-only).
        availability: An already-normalised availability snapshot.
        role: Class role (from the ``--class`` string; ``run`` has no
            separate role flag in explicit mode).
        oracle_type: Class oracle type.
        size_band: Class size band.
        language: Class language.
        domain_tags: Class domain-tag set.
        class_key: The canonical five-segment class string; must equal the
            canonical rendering of the structured fields (enforced by the
            eligibility evaluation).
        at_date: Date for the dated-terms check (ISO string or date).
        route_id: Explicit ``--route`` selection. The route must exist and
            be currently eligible; otherwise :class:`RunSelectionError`
            with exit code 3 and the exact explain reason.
        instance_id: Optional explicit ``--instance`` pin. When supplied,
            pins a specific channel instance for the selected route. The
            instance must exist, be enabled, and be eligible (otherwise
            :class:`RunSelectionError` with exit code 3). When omitted, the
            first eligible instance in ``instance_headrooms`` is selected,
            or ``None`` for channels without instances or with none eligible.
        supervisor_route_id: Optional ``--supervisor-route`` attestation
            (P1-4 ruling 3): the caller attests its own route. The id must
            name a known, active, currently usable catalog route — every
            governed exclusion that is not role/class policy still refuses
            (exit 3, nothing launched) — but the worker's role/class
            capability policy is never imposed on the supervisor identity
            (Astra final-review finding 5). When resolved, the accepted
            supervisor-route object is carried on the outcome.
        author_route_id: Optional author route id, forwarded to the
            eligibility evaluation (review/judge independence). The CLI
            forwards ``--author-route``; without it independence is not
            evaluated, exactly as in ``catalog explain``, and an unknown
            author id fails closed.
        openrouter_snapshot_path: Injectable pinned OpenRouter snapshot
            path (defaults to the committed pinned file).
        rate_table_path: Injectable agent-orch rate-table path.

    Returns:
        The :class:`SelectionOutcome` for the single selected route, with
        the selection basis, reason, explain reference, and every excluded
        route id/reason preserved from the same evaluation.

    Raises:
        RunSelectionError: When the class arguments are invalid, the
            explicit route id matches no catalog route, the explicit route
            is not currently eligible, no route is eligible for
            role/class selection, or the explicit instance is unknown,
            disabled, ineligible, or specified for a channel with no
            instance concept. ``exit_code`` is always 3.
    """
    when = _normalize_at_date(at_date)
    try:
        rows = evaluate_eligibility(
            catalog,
            role=role,
            oracle_type=oracle_type,
            size_band=size_band,
            language=language,
            domain_tags=tuple(domain_tags),
            class_key=class_key,
            author_route_id=author_route_id,
            availability=availability,
            at_date=when,
            openrouter_snapshot_path=openrouter_snapshot_path,
            rate_table_path=rate_table_path,
        )
    except StaffingEligibilityError as exc:
        raise RunSelectionError(str(exc), kind="invalid_class", cause=exc) from exc

    excluded = _excluded_summaries(rows)
    explain_ref = (
        f"catalog explain --role {role} --class {class_key} "
        f"--at {when.isoformat()} --json"
    )

    # Optional caller attestation (P1-4 ruling 3): resolved through the same
    # committed catalog/eligibility path, failing closed before any branch
    # selects a worker route. ``None`` when the caller makes no attestation.
    attested_supervisor = (
        _resolve_supervisor_route(
            catalog,
            rows,
            supervisor_route_id=supervisor_route_id,
            when=when,
        )
        if supervisor_route_id is not None
        else None
    )

    if route_id is not None:
        row = next((r for r in rows if r.route_id == route_id), None)
        if row is None:
            raise RunSelectionError(
                f"--route {route_id!r} does not match any route_id in the "
                "routes catalog",
                kind="unknown_route",
            )
        route = next(r for r in catalog.routes.routes if r.route_id == route_id)
        if not row.eligible:
            raise RunSelectionError(
                f"explicit route {route_id!r} is not eligible for class "
                f"{class_key!r} at {when.isoformat()}: {'; '.join(row.reasons)}",
                kind="excluded",
            )
        cache_repl, cache_marg = _resolve_cache_rates(
            row.pricing,
            openrouter_snapshot_path=openrouter_snapshot_path,
            rate_table_path=rate_table_path,
        )
        channel_instance = _resolve_channel_instance(row, instance_id)
        return SelectionOutcome(
            route=route,
            basis=SELECTION_BASIS_EXPLICIT,
            reason=(
                f"explicit --route {route_id} is eligible for class "
                f"{class_key} at {when.isoformat()}"
            ),
            explain_ref=explain_ref,
            excluded=excluded,
            pricing=row.pricing,
            supervisor_route=attested_supervisor,
            cache_replacement_usd_per_token=cache_repl,
            cache_marginal_usd_per_token=cache_marg,
            cache_rates_checked=True,
            channel_kind=_channel_kind(catalog, route.channel),
            channel_instance=channel_instance,
        )

    eligible = sorted(
        (row for row in rows if row.eligible), key=_marginal_price_sort_key
    )
    if not eligible:
        raise RunSelectionError(
            f"no eligible route for class {class_key!r} at {when.isoformat()}; "
            f"{len(excluded)} routes excluded",
            kind="no_eligible",
        )
    selected = eligible[0]
    route = next(r for r in catalog.routes.routes if r.route_id == selected.route_id)
    cache_repl, cache_marg = _resolve_cache_rates(
        selected.pricing,
        openrouter_snapshot_path=openrouter_snapshot_path,
        rate_table_path=rate_table_path,
    )
    channel_instance = _resolve_channel_instance(selected, instance_id)
    return SelectionOutcome(
        route=route,
        basis=SELECTION_BASIS_EXPLAIN_CHEAPEST_ELIGIBLE,
        reason=(
            f"first eligible route in catalog explain marginal-price order "
            f"for class {class_key} at {when.isoformat()}"
        ),
        explain_ref=explain_ref,
        excluded=excluded,
        pricing=selected.pricing,
        supervisor_route=attested_supervisor,
        cache_replacement_usd_per_token=cache_repl,
        cache_marginal_usd_per_token=cache_marg,
        cache_rates_checked=True,
        channel_kind=_channel_kind(catalog, route.channel),
        channel_instance=channel_instance,
    )


def selection_record(outcome: SelectionOutcome) -> dict[str, Any]:
    """The attempt-record v2 ``selection`` object for one selection outcome.

    Exactly the four schema fields (``basis``, ``reason``, ``explain_ref``,
    ``excluded``); excluded entries carry exactly ``route_id`` and
    ``reason``. Explain evidence only — never a new selection judgment.
    """
    return {
        "basis": outcome.basis,
        "reason": outcome.reason,
        "explain_ref": outcome.explain_ref,
        "excluded": [
            {"route_id": route_id, "reason": reason}
            for route_id, reason in outcome.excluded
        ],
    }


def _is_role_class_capability_reason(reason: str) -> bool:
    """Whether one explain reason is worker role/class capability policy.

    The worker-selection checks that must never be imposed on an attested
    supervisor identity: the D188 ``role_scoped`` denial (reason
    ``role_scoped: <class> denied for <model>``) and the review/judge
    author-family ``independence`` exclusion (Astra final-review finding 5),
    plus ``never_automatic`` (D223, Lee 2026-09-12: never-automatic governs
    *selection* of a worker, not the *identity* of the supervisor attesting a
    run — Fable/Opus/Luna-Max supervisors were refused as
    ``supervisor_route_unattested`` throughout Phase 5). The permitted
    same-family review disclosure is informational, not an identity veto.
    Every other governed reason — status, channel membership, harness lock,
    availability veto, terms/pricing availability — still refuses an attestation.
    """
    return (
        reason == "independence"
        or reason == "never_automatic"
        or reason == _SAME_FAMILY_REVIEW_DISCLOSURE
        or reason.startswith("role_scoped:")
    )


def _resolve_supervisor_route(
    catalog: StaffingCatalog,
    rows: tuple[EligibilityRow, ...],
    *,
    supervisor_route_id: str,
    when: date,
) -> dict[str, Any]:
    """Resolve an attested supervisor route id, failing closed (P1-4 r3).

    The attestation validates the caller's *identity*, governed over the
    same committed catalog and evaluation path — never the worker's
    capability (Astra final-review finding 5: Chief answer 4 authorizes
    identity attestation, not selection of another worker). The id must:

    * name a known catalog route (else ``unknown_route`` refusal);
    * be ``active`` (else refusal);
    * be currently usable as a governed route: every remaining exclusion
      reason from the same explain evaluation still refuses — route status,
      channel membership, harness lock, availability headroom veto,
      unavailable dated terms or pricing. The only reasons *not* imposed on
      the supervisor are the worker-selection policies (``role_scoped``, the
      review/judge ``independence`` author-family exclusion,
      ``never_automatic`` — D223, and the informational same-family review
      disclosure): attesting one's identity is not selecting
      a worker, and the supervisor is never a review candidate.

    Any refusal raises :class:`RunSelectionError` (exit 3) before anything
    is launched.

    Returns:
        The accepted supervisor-route object: the identity tuple
        ``model``/``effort``/``harness``/``channel`` plus the observed
        ``provider`` name — exactly the record's ``route`` object shape,
        never an invented identity.
    """
    route = next(
        (r for r in catalog.routes.routes if r.route_id == supervisor_route_id),
        None,
    )
    if route is None:
        raise RunSelectionError(
            f"--supervisor-route {supervisor_route_id!r} does not match any "
            "route_id in the routes catalog",
            kind="unknown_route",
        )
    if route.status != "active":
        raise RunSelectionError(
            f"--supervisor-route {supervisor_route_id!r} is not active "
            f"(status {route.status!r}); an attested supervisor route must be "
            "an active catalog route",
            kind="excluded",
        )
    row = next(r for r in rows if r.route_id == supervisor_route_id)
    identity_reasons = tuple(
        reason for reason in row.reasons if not _is_role_class_capability_reason(reason)
    )
    if identity_reasons:
        raise RunSelectionError(
            f"--supervisor-route {supervisor_route_id!r} is not a currently "
            f"usable route identity at {when.isoformat()}: "
            f"{'; '.join(identity_reasons)}",
            kind="excluded",
        )
    return {
        "model": route.model,
        "effort": route.effort,
        "harness": route.harness,
        "channel": route.channel,
        "provider": _HARNESS_PROVIDER_NAMES.get(route.harness, route.harness),
    }


_ATTEMPT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
"""Schema value rule for caller-supplied attempt/parent identifiers
(``attempt-record.schema.json`` ``attempt_id``/``parent_attempt_id``
``pattern``, taken verbatim — no invented rule)."""


def validate_attempt_metadata(
    *,
    parent_attempt_id: str | None = None,
    escalation_reason: str | None = None,
    attempt_id: str | None = None,
) -> None:
    """Validate caller-controlled attempt-record metadata before launch.

    Astra final-review finding 6: the fields the caller controls end up in
    the one attempt record, so a violation must refuse (exit 3) *before*
    anything is launched — never surface as a schema rejection after the
    worker has completed and evidence already exists. The rules are exactly
    the committed attempt-record v2 value constraints, nothing stricter:

    * the ``--parent``/``--escalation-reason`` pair is all-or-nothing (an
      escalation requires both, D209 ruling 2);
    * ``parent_attempt_id`` and ``attempt_id`` are nonempty strings
      matching :data:`_ATTEMPT_ID_PATTERN` (the schema ``pattern``);
    * ``escalation_reason`` is a nonempty string (schema ``minLength 1``).

    Raises:
        RunSelectionError: With ``kind="invalid_metadata"`` and exit code 3
            naming the offending field. Nothing is launched when raised.
    """
    if (parent_attempt_id is None) != (escalation_reason is None):
        raise RunSelectionError(
            "--parent and --escalation-reason must be supplied together",
            kind="invalid_metadata",
        )
    if parent_attempt_id is not None:
        if not isinstance(parent_attempt_id, str) or not _ATTEMPT_ID_PATTERN.fullmatch(
            parent_attempt_id
        ):
            raise RunSelectionError(
                "--parent: parent_attempt_id "
                f"{parent_attempt_id!r} is not a valid attempt id "
                "(nonempty, characters A-Z a-z 0-9 . _ : - only)",
                kind="invalid_metadata",
            )
    if escalation_reason is not None and (
        not isinstance(escalation_reason, str) or not escalation_reason
    ):
        raise RunSelectionError(
            "--escalation-reason: escalation_reason must be a nonempty string",
            kind="invalid_metadata",
        )
    if attempt_id is not None and (
        not isinstance(attempt_id, str) or not _ATTEMPT_ID_PATTERN.fullmatch(attempt_id)
    ):
        raise RunSelectionError(
            f"attempt_id {attempt_id!r} is not a valid attempt id "
            "(nonempty, characters A-Za-z0-9._:- only)",
            kind="invalid_metadata",
        )


def _utc_timestamp() -> str:
    """Return one aware UTC timestamp for a router attempt and its event."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_attempt_id() -> str:
    """Return a schema-safe id for a run that has no caller-supplied id."""
    return f"router-run-{uuid.uuid4().hex}"


def packet_id_for_text(packet_text: str) -> str:
    """Return the canonical content identity for the dispatched packet.

    The governed CLI accepts a packet file but no separately asserted packet
    identifier.  A filesystem path is location metadata, not packet identity,
    so the identifier is the SHA-256 of the exact UTF-8 text sent to the
    provider.  Equal packet text therefore has one stable identity regardless
    of where the file is stored.

    Args:
        packet_text: Exact packet text passed to :func:`dispatch_route`.

    Returns:
        ``sha256:<lowercase hex digest>``.
    """
    digest = hashlib.sha256(packet_text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


_DISPATCH_PROGRESS_KILL_RE = re.compile(
    r"^dispatch kill: (stall|no_progress)\b.*\bceiling " r"([0-9]+(?:\.[0-9]+)?) min\b"
)
"""A governed progress-kill note and its recorded ceiling in minutes."""

_NO_WORK_NOTE_RE = re.compile(r"^dispatch no-work:\s")
"""A governed no-effective-work note (:data:`NO_WORK_NOTE_PREFIX`)."""

_RESULT_FINGERPRINT_RE = re.compile(
    r"^dispatch result fingerprint: (sha256:[0-9a-f]{64})$"
)
"""An exact stdout/stderr result hash for unverified dispatch evidence."""


def _has_no_work_note(attempt: Mapping[str, Any]) -> bool:
    """Whether one validated attempt carries a governed no-work note."""
    provenance = attempt.get("provenance")
    if not isinstance(provenance, Mapping):
        return False
    notes = provenance.get("notes")
    if not isinstance(notes, Sequence) or isinstance(notes, (str, bytes)):
        return False
    return any(isinstance(note, str) and _NO_WORK_NOTE_RE.match(note) for note in notes)


def _result_fingerprint(attempt: Mapping[str, Any]) -> str | None:
    """Return a persisted exact stdout/stderr result fingerprint when present."""
    if attempt.get("verdict") != "unverified":
        return None
    provenance = attempt.get("provenance")
    if not isinstance(provenance, Mapping):
        return None
    notes = provenance.get("notes")
    if not isinstance(notes, Sequence) or isinstance(notes, (str, bytes)):
        return None
    for note in notes:
        if isinstance(note, str) and (match := _RESULT_FINGERPRINT_RE.match(note)):
            return match.group(1)
    return None


def unchanged_redispatch_refusal(
    attempts: Sequence[Mapping[str, Any]],
    packet_id: str,
    route_id: str,
    timeout_seconds: float | None,
    parent: str | None,
) -> str | None:
    """Refuse an unchanged route after its latest no-work or progress kill.

    The most recent attempt for the exact packet and route is authoritative.
    A recorded no-effective-work result (exit 0, no oracle, no owned-path
    change, and no substantive stdout answer) is a mechanical cycle exactly
    like a stall or no-progress kill: it may be retried once as an explicit
    escalation linked to that attempt. Once the immediately preceding attempt
    on the same packet and route is itself no-work, the one repair is spent
    and no parent link reopens the identical cycle; only a materially changed
    packet or route can. A prior stall or no-progress kill may additionally be
    retried with a strictly lower timeout. The caller validates that a
    non-null ``parent`` is paired with an escalation reason before invoking
    this pure decision seam.

    Args:
        attempts: Validated ledger records in chronological file order.
        packet_id: Content identity of the packet about to be dispatched.
        route_id: Selected route for the prospective dispatch.
        timeout_seconds: Prospective wall-clock ceiling, or None for default.
        parent: Prospective parent attempt id, or None.

    Returns:
        The refusal text, or None when the dispatch is allowed.
    """
    fingerprint: list[Mapping[str, Any]] = []
    for attempt in attempts:
        router_event = attempt.get("router_event")
        if (
            attempt.get("packet_id") == packet_id
            and isinstance(router_event, Mapping)
            and router_event.get("route_id") == route_id
        ):
            fingerprint.append(attempt)

    if not fingerprint:
        return None

    prior = fingerprint[-1]
    provenance = prior.get("provenance")
    if not isinstance(provenance, Mapping):
        return None
    notes = provenance.get("notes")
    if not isinstance(notes, Sequence) or isinstance(notes, (str, bytes)):
        return None

    attempt_id = prior.get("attempt_id")
    if _has_no_work_note(prior):
        # One-repair bound (successive unchanged no-work results): the first
        # no-work attempt may be re-dispatched exactly once as an explicit
        # escalation, but once the immediately preceding attempt on the same
        # packet and route is also no-work the repair is spent. A parent link
        # to that prior attempt can no longer reopen the identical cycle.
        previous = fingerprint[-2] if len(fingerprint) >= 2 else None
        if previous is not None and _has_no_work_note(previous):
            return (
                f"refused — unchanged re-dispatch of {packet_id} on {route_id} "
                f"after repeated no-work results (attempts "
                f"{previous.get('attempt_id')}, {attempt_id}); the one repair "
                "is spent — change the packet or produce new evidence"
            )
        if parent == attempt_id:
            return None
        return (
            f"refused — unchanged re-dispatch of {packet_id} on {route_id} "
            f"after a no-work result (attempt {attempt_id}); change the "
            "packet, produce owned-path change or oracle evidence, or escalate "
            "with --parent/--escalation-reason"
        )

    # Structural classification is intentionally finite. As a final bound on
    # an unknown metadata envelope, two consecutive exact stdout/stderr result
    # hashes for an otherwise unchanged, unverified/no-oracle/no-owned-path
    # attempt spend the retry. A first read-only answer is never quarantined,
    # and a changed stdout/stderr result, packet, route, owned-path, or oracle
    # evidence resets this comparison.
    previous = fingerprint[-2] if len(fingerprint) >= 2 else None
    result_fingerprint = _result_fingerprint(prior)
    if (
        previous is not None
        and result_fingerprint is not None
        and _result_fingerprint(previous) == result_fingerprint
    ):
        return (
            f"refused — unchanged re-dispatch of {packet_id} on {route_id} "
            "after repeated identical unverified output (attempts "
            f"{previous.get('attempt_id')}, {attempt_id}); change the packet "
            "or produce changed result, owned-path, or oracle evidence"
        )

    if prior.get("failure_class") != "platform_timeout":
        return None

    for note in notes:
        if not isinstance(note, str):
            continue
        match = _DISPATCH_PROGRESS_KILL_RE.match(note)
        if match is None:
            continue
        kill_reason, ceiling_text = match.groups()
        attempt_id = prior.get("attempt_id")
        if parent == attempt_id:
            return None
        ceiling_seconds = Decimal(ceiling_text) * Decimal(60)
        if timeout_seconds is not None:
            timeout = Decimal(str(timeout_seconds))
            if timeout.is_finite() and timeout < ceiling_seconds:
                return None
        return (
            f"refused — unchanged re-dispatch of {packet_id} on {route_id} "
            f"after a {kill_reason} kill (attempt {attempt_id}); change the "
            f"packet, lower --timeout below {ceiling_text} min, or escalate "
            "with --parent/--escalation-reason"
        )
    return None


def _known_token_count(usage: Mapping[str, Any], field: str) -> bool:
    """Whether one billable counter is an actual nonnegative integer."""
    value = usage.get(field)
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _parse_cache_price(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (str, int, float)):
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return float(parsed)


def _load_openrouter_cache_rates(path: Path) -> dict[str, float]:
    try:
        _verify_sha256(path)
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    rows = data.get("data") if isinstance(data, Mapping) else None
    if not isinstance(rows, list):
        return {}
    rates: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            continue
        model_id = row["id"]
        pricing = row.get("pricing")
        if isinstance(pricing, Mapping):
            parsed = _parse_cache_price(pricing.get("input_cache_read"))
            if parsed is not None:
                rates[model_id] = parsed
    return rates


def _load_rate_table_cache_rates(path: Path) -> dict[str, float]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    rates = data.get("rates") if isinstance(data, Mapping) else None
    if not isinstance(rates, Mapping):
        return {}
    prices: dict[str, float] = {}
    for model_id, row in rates.items():
        if not isinstance(model_id, str) or not isinstance(row, Mapping):
            continue
        parsed = _parse_cache_price(row.get("cache_read_usd_per_1m"))
        if parsed is not None:
            prices[model_id] = parsed / _TOKENS_PER_1M
    return prices


def _resolve_cache_rates(
    pricing: EligibilityPrice | None,
    *,
    openrouter_snapshot_path: str | Path | None = None,
    rate_table_path: str | Path | None = None,
) -> tuple[float | None, float | None]:
    """Resolve replacement and marginal cache-read price per token.

    Uses the authoritative P0-4 pricing sources named in ``pricing.source``:
    either the pinned OpenRouter snapshot (``input_cache_read``) or the
    agent-orch rate table (``cache_read_usd_per_1m``).

    Returns:
        ``(replacement_cache_rate, marginal_cache_rate)`` where each is a
        nonnegative float, or ``(None, None)`` when no cache price is
        sourced.
    """
    if pricing is None or not pricing.source:
        return None, None

    replacement_rate: float | None = None
    if pricing.source.startswith("openrouter-snapshot:"):
        model_id = pricing.source.split(":", 1)[1]
        snap_path = (
            Path(openrouter_snapshot_path)
            if openrouter_snapshot_path is not None
            else DEFAULT_OPENROUTER_SNAPSHOT_PATH
        )
        rates = _load_openrouter_cache_rates(snap_path)
        replacement_rate = rates.get(model_id)

    elif pricing.source.startswith("rate-table:"):
        key = pricing.source.split(":", 1)[1]
        rt_path = (
            Path(rate_table_path)
            if rate_table_path is not None
            else DEFAULT_RATE_TABLE_PATH
        )
        rates = _load_rate_table_cache_rates(rt_path)
        replacement_rate = rates.get(key)

    if replacement_rate is None:
        return None, None

    marginal_rate = replacement_rate * pricing.multiplier
    return replacement_rate, marginal_rate


def _is_cached_input_subset(
    outcome: SelectionOutcome, usage: Mapping[str, Any]
) -> bool:
    """Whether usage semantics treat cached input tokens as a subset of input.

    Codex reports cached input and reasoning as subsets of input and output
    respectively, with total = input + output. Pi, Claude, OMP, and
    Antigravity treat cached tokens as separate counters (or history not
    constrained by input).
    """
    if getattr(outcome.route, "harness", None) == "codex":
        return True
    if usage.get("source") == CODEX_USAGE_SOURCE:
        return True
    return False


def _attempt_cost(
    outcome: SelectionOutcome,
    usage: Mapping[str, Any],
    *,
    aggregate_models: tuple[str, ...] | None = None,
) -> tuple[dict[str, Any], str]:
    """Calculate list and marginal token cost from the selected P0-4 price.

    The committed price API exposes the dated replacement input/output rates,
    the badge-adjusted marginal rates, and authoritative cache-read rates from
    the selected dated terms source.

    When cached tokens are reported:
    * If cached input is a subset of input (Codex), uncached input is separated
      (``input_tokens - cached_input_tokens``). An invalid relationship where
      cached input exceeds input fails closed as unavailable cost.
    * If cached input is reported separately (Pi and other non-subset harnesses),
      ``input_tokens`` is already uncached input and cached tokens are added at
      the cache rate.
    * Cache rates from authoritative P0-4 sources are applied when cached
      tokens are present. If cached tokens are nonzero but cache price evidence
      is unavailable for the model, cost fails closed as unavailable.
    * Null/absent cached tokens do not bill cache and do not fabricate zeros,
      except for harnesses whose receipt separates cache reads from input; for
      those harnesses an absent split is incomplete billing evidence.
    * OpenCode reports reasoning separately from ordinary output. Both are
      billed at the selected output-token rate only when the separate reasoning
      counter is authoritative; no reasoning count or price is invented.
    * Agy and OpenCode cache-write counters are preserved when captured. A
      nonzero write count has no selected dated price in the P0-4 API, so cost
      fails closed rather than dropping the component.

    Complete-billing-evidence gates (Astra final review finding 1):
    * The selected dated price terms price exactly the selected route's
      model. A usage aggregate that spans multiple models (the Claude
      ``modelUsage`` ids surfaced as ``aggregate_models``) cannot be priced
      at that single rate and fails closed as unavailable, as does a
      single-model aggregate for a model other than the priced route model.
    * For harnesses whose authoritative ``total_tokens`` includes the
      cache-read and cache-write components (Pi, OMP, Claude), cost exists
      only when the total is known and every token it counts is
      representable as billing evidence: an unknown total could hide
      cache-write tokens, and a positive remainder over the represented
      input/output/cache-read components is exactly the unrepresentable
      cache-write charge. Either gap fails closed as unavailable cost
      rather than billing a subset of the receipt.
    * A valid JSON token integer has no size bound (finding 2). When the
      known tokens exceed the representable float cost range, the
      multiplication by float prices would raise ``OverflowError`` after
      the worker has completed, so cost fails closed as unavailable while
      the truthful usage and the completed attempt are preserved.
    """
    if usage.get("basis") == "unavailable":
        return {"basis": ["unavailable"]}, "usage basis is unavailable"
    if not _known_token_count(usage, "input_tokens"):
        return {"basis": ["unavailable"]}, "input_tokens is unknown"
    if not _known_token_count(usage, "output_tokens"):
        return {"basis": ["unavailable"]}, "output_tokens is unknown"
    pricing = outcome.pricing
    if pricing is None:
        return {"basis": ["unavailable"]}, "selected route has no dated price"

    if aggregate_models is not None:
        if len(aggregate_models) > 1:
            return {"basis": ["unavailable"]}, (
                "aggregated multi-model usage cannot be priced at the "
                "selected route's model rate"
            )
        if aggregate_models and aggregate_models[0] != outcome.route.model:
            return {"basis": ["unavailable"]}, (
                "usage model does not match the selected route's priced model"
            )

    input_tokens = usage["input_tokens"]
    output_tokens = usage["output_tokens"]
    harness = getattr(outcome.route, "harness", None)

    # Codex's input count can contain cached input, but an absent cached split
    # does not tell us whether the input was all uncached. Agy/OpenCode expose
    # cache as separate billable components; an absent cache counter is just as
    # unknowable. Never substitute an uncached input or zero cache write.
    cached_value = usage.get("cached_input_tokens")
    if cached_value is not None and not _known_token_count(
        usage, "cached_input_tokens"
    ):
        return {"basis": ["unavailable"]}, "cached_input_tokens is invalid"
    if harness == "codex" and not _known_token_count(usage, "cached_input_tokens"):
        return {"basis": ["unavailable"]}, (
            "Codex cached_input_tokens is unknown, so the input billing split "
            "cannot be priced"
        )
    if harness in {"agy", "opencode"}:
        if not _known_token_count(usage, "cached_input_tokens"):
            return {"basis": ["unavailable"]}, (
                f"{harness} cached_input_tokens is unknown, so the separate "
                "cache-read billing component cannot be priced"
            )
        cache_write_value = usage.get("cache_write_tokens")
        if cache_write_value is not None and not _known_token_count(
            usage, "cache_write_tokens"
        ):
            return {"basis": ["unavailable"]}, "cache_write_tokens is invalid"
        if not _known_token_count(usage, "cache_write_tokens"):
            return {"basis": ["unavailable"]}, (
                f"{harness} cache_write_tokens is unknown, so the cache-write "
                "billing component cannot be ruled out"
            )
        if usage["cache_write_tokens"]:
            return {"basis": ["unavailable"]}, (
                f"{harness} cache_write_tokens has no selected dated price"
            )

    if harness in _TOTAL_INCLUDES_CACHE_HARNESSES:
        if not _known_token_count(usage, "total_tokens"):
            return {"basis": ["unavailable"]}, (
                "total_tokens is unknown, so unrepresented cache-write "
                "tokens cannot be ruled out"
            )
        cached_known = _known_token_count(usage, "cached_input_tokens")
        cached_represented = usage["cached_input_tokens"] if cached_known else 0
        unrepresented = (
            usage["total_tokens"] - input_tokens - output_tokens - cached_represented
        )
        if unrepresented < 0:
            return {"basis": ["unavailable"]}, (
                "total_tokens contradicts the represented billing components"
            )
        if unrepresented > 0:
            return {"basis": ["unavailable"]}, (
                "total_tokens includes cache-write tokens that the v2 "
                "usage mapping cannot represent as billing evidence"
            )

    cached_tokens: int | None = None
    if "cached_input_tokens" in usage and usage["cached_input_tokens"] is not None:
        if not _known_token_count(usage, "cached_input_tokens"):
            return {"basis": ["unavailable"]}, "cached_input_tokens is invalid"
        cached_tokens = usage["cached_input_tokens"]

    is_subset = _is_cached_input_subset(outcome, usage)
    if cached_tokens is not None and is_subset:
        if cached_tokens > input_tokens:
            return {
                "basis": ["unavailable"]
            }, "cached_input_tokens contradicts input_tokens"
        uncached_input_tokens = input_tokens - cached_tokens
    else:
        uncached_input_tokens = input_tokens

    cache_repl = outcome.cache_replacement_usd_per_token
    cache_marg = outcome.cache_marginal_usd_per_token
    if not outcome.cache_rates_checked and cache_repl is None:
        cache_repl, cache_marg = _resolve_cache_rates(pricing)

    cache_tokens_known = cached_tokens is not None and cached_tokens > 0
    if cache_tokens_known:
        if cache_repl is None:
            return {
                "basis": ["unavailable"]
            }, "cache price is unavailable for the selected model"
        if (
            not isinstance(cache_repl, (int, float))
            or isinstance(cache_repl, bool)
            or not math.isfinite(cache_repl)
            or cache_repl < 0
        ):
            return {"basis": ["unavailable"]}, "cache price is invalid"
        if cache_marg is None:
            cache_marg = cache_repl * pricing.multiplier
        if (
            not isinstance(cache_marg, (int, float))
            or isinstance(cache_marg, bool)
            or not math.isfinite(cache_marg)
            or cache_marg < 0
        ):
            return {"basis": ["unavailable"]}, "cache price is invalid"

    billable_output_tokens = output_tokens
    if harness == "opencode":
        # OpenCode's ``output`` and ``reasoning`` fields are separate rather
        # than a subset relationship. The pricing model has one output-token
        # rate, so the authoritative sum is the quantity priced as output.
        if not _known_token_count(usage, "reasoning_tokens"):
            return {"basis": ["unavailable"]}, (
                "opencode reasoning_tokens is unknown, so the separate "
                "output billing component cannot be priced"
            )
        billable_output_tokens += usage["reasoning_tokens"]

    # A valid JSON token integer has no size bound, and multiplying it by
    # a float price raises OverflowError before the finite-result check
    # below (Astra final re-review finding 2). Such tokens are truthful
    # usage evidence, so the completed attempt is never lost: the
    # unrepresentable cost fails closed as unavailable.
    try:
        cache_cost_list = cached_tokens * cache_repl if cache_tokens_known else 0.0
        cache_cost_marg = cached_tokens * cache_marg if cache_tokens_known else 0.0
        list_cost = (
            uncached_input_tokens * pricing.replacement_input_usd_per_token
            + billable_output_tokens * pricing.replacement_output_usd_per_token
            + cache_cost_list
        )
        marginal_cost = (
            uncached_input_tokens * pricing.marginal_input_usd_per_token
            + billable_output_tokens * pricing.marginal_output_usd_per_token
            + cache_cost_marg
        )
    except OverflowError:
        return {"basis": ["unavailable"]}, (
            "known token totals exceed the representable float cost range"
        )

    if not math.isfinite(list_cost) or not math.isfinite(marginal_cost):
        return {"basis": ["unavailable"]}, "calculated cost is not finite"

    note = (
        "cost calculated from known input/output/cache counters"
        if cached_tokens is not None and cached_tokens > 0
        else "cost calculated from known input/output counters"
    )
    return {
        "basis": ["list", "marginal"],
        "usd_list": list_cost,
        "usd_marginal": marginal_cost,
    }, note


def _parse_judge_verdict(stdout: str) -> str | None:
    """Parse the last verdict marker from worker stdout for review/judge roles.

    Returns ``"judge_pass"`` for a final ``"REVIEW VERDICT: ACCEPT"`` marker,
    ``"judge_fail"`` for ``"REVIEW VERDICT: REJECT"``, or ``None`` if absent.

    The marker is matched by its last occurrence in ``stdout``, not by exact
    equality with the final line: a harness whose captured stdout is a raw
    JSON event stream (e.g. ``pi``/``agy``) embeds the worker's real final
    text inside a JSON string field followed by wire-format closing tokens
    (e.g. ``...REVIEW VERDICT: ACCEPT"}]}``), so the marker is never
    literally the last line even though it is the worker's true conclusion.
    Using the last occurrence (not the first) still avoids a false match on
    a marker the worker merely quotes or discusses earlier in its output.
    """
    accept_at = stdout.rfind("REVIEW VERDICT: ACCEPT")
    reject_at = stdout.rfind("REVIEW VERDICT: REJECT")
    if accept_at == -1 and reject_at == -1:
        return None
    if accept_at > reject_at:
        return "judge_pass"
    return "judge_fail"


def _persist_worker_output(
    artifacts_root: Path,
    attempt_id: str,
    dispatch: DispatchOutcome,
) -> tuple[str | None, str]:
    """Persist worker stdout and stderr into `<artifacts_root>/<attempt_id>/`.

    Writes ``stdout.txt`` and ``stderr.txt`` verbatim from ``dispatch.stdout``
    and ``dispatch.stderr``.

    Returns ``(worker_output_dir_str, note_str)``. Directory creation and
    file writes fail open on any ``OSError``: returns ``(None, note_str)``
    describing the failure.
    """
    try:
        attempt_dir = (artifacts_root / attempt_id).expanduser()
        if not attempt_dir.is_absolute():
            attempt_dir = attempt_dir.resolve()
        attempt_dir.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "stdout.txt").write_text(dispatch.stdout, encoding="utf-8")
        (attempt_dir / "stderr.txt").write_text(dispatch.stderr, encoding="utf-8")
        dir_str = str(attempt_dir)
        return dir_str, f"worker output persisted to {dir_str} (stdout.txt, stderr.txt)"
    except OSError as exc:
        return None, f"worker output persistence failed: {exc}"


def build_attempt_record(
    outcome: SelectionOutcome,
    dispatch: DispatchOutcome,
    oracle: OracleOutcome | None,
    *,
    packet_id: str,
    class_record: Mapping[str, Any],
    availability: AvailabilitySnapshot,
    at_date: date,
    oracle_cmd: str | None = None,
    parent_attempt_id: str | None = None,
    escalation_reason: str | None = None,
    supervisor_route: Mapping[str, Any] | None = None,
    attempt_id: str | None = None,
    captured_at: str | None = None,
    class_derivation: Mapping[str, Any] | None = None,
    artifacts_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Build one strict v2 router-run attempt record.

    This function only assembles facts already produced by selection,
    dispatch, the optional oracle, and the committed pricing/eligibility
    path. Schema validation and the single append belong to
    :mod:`lee_llm_router.staffing.ledger`; no retry or escalation is done.

    Args:
        outcome: Selected route and catalog-explain evidence.
        dispatch: The one completed worker dispatch and usage receipt.
        oracle: The one optional oracle outcome.
        packet_id: Canonical identifier of the exact dispatched packet.
        class_record: Canonical class block reconstructed from ``--class``.
        availability: The snapshot used during eligibility evaluation.
        at_date: Dated-terms date used during selection.
        oracle_cmd: Original command string, or ``None`` when no oracle ran.
        parent_attempt_id: Optional escalation parent.
        escalation_reason: Optional paired escalation reason.
        supervisor_route: Optional attested supervisor route object (P1-4
            ruling 3), resolved against the committed catalog by
            :func:`select_route` from ``--supervisor-route``. It must not be
            invented when the caller records no supervisor identity.
        attempt_id: Optional caller-supplied id; generated when absent.
        captured_at: Optional aware UTC capture timestamp, primarily for tests.
        class_derivation: Optional validated packet-derivation evidence. Only
            its override records are preserved in provenance; it never selects
            or ranks a route.
        artifacts_dir: Optional explicit root directory for attempt artifacts.

    Returns:
        A mapping in the committed attempt-record v2 shape. It is not
        written by this function.
    """
    route = outcome.route
    usage = dict(dispatch.usage)
    cost, cost_note = _attempt_cost(
        outcome, usage, aggregate_models=dispatch.usage_models
    )
    verdict = _oracle_verdict(oracle)
    if class_record.get("role") in _INDEPENDENCE_APPLICABLE_ROLES:
        judge_verdict = _parse_judge_verdict(dispatch.stdout)
        if judge_verdict is not None:
            verdict = judge_verdict
    record_attempt_id = attempt_id or _new_attempt_id()
    try:
        artifacts_root = resolve_artifacts_dir(artifacts_dir)
        worker_output_dir, output_note = _persist_worker_output(
            artifacts_root, record_attempt_id, dispatch
        )
    except OSError as exc:
        worker_output_dir = None
        output_note = f"worker output persistence failed: {exc}"
    timestamp = captured_at or _utc_timestamp()
    channel_headroom = availability.headroom(route.channel)
    provider = _HARNESS_PROVIDER_NAMES.get(route.harness, route.harness)

    # ``router_event`` is the existing event shape embedded as provenance
    # evidence. A run is a strict eligibility decision, not a new resolver
    # mode; the event remains a normal 17-field build_event result.
    from lee_llm_router import events as events_mod

    router_event = events_mod.build_event(
        ts=timestamp,
        harness="cli",
        crew="run",
        role=class_record["role"],
        mode="strict",
        worker_id=route.route_id,
        provider=provider,
        model=route.model,
        effort=route.effort,
        channel=route.channel,
        headroom=channel_headroom.health.value,
        reason=outcome.reason,
        authorized_by=None,
        route_id=route.route_id,
        snapshot_observed_at=channel_headroom.observed_at,
        snapshot_stale=channel_headroom.stale,
    )

    wall_clock_ms = max(
        0,
        round(
            (dispatch.duration_seconds + (oracle.duration_seconds if oracle else 0.0))
            * 1000.0
        ),
    )
    notes = [
        "router-run dispatch facts: "
        f"exit_code={dispatch.exit_code}, timed_out={dispatch.timed_out}, "
        f"duration_seconds={dispatch.duration_seconds:.6f}",
        output_note,
        f"oracle verdict: {verdict}; command={'present' if oracle_cmd else 'none'}",
        f"cost: {cost_note}; dated terms at {at_date.isoformat()}",
    ]
    if dispatch.timed_out:
        kr = dispatch.kill_reason or "ceiling"
        n = _format_minutes(dispatch.duration_seconds)
        s = _format_minutes(
            dispatch.stall_minutes
            if dispatch.stall_minutes is not None
            else DEFAULT_STALL_MINUTES
        )
        p = (
            _format_minutes(dispatch.progress_minutes)
            if dispatch.progress_minutes is not None
            else "none"
        )
        c = _format_minutes(
            dispatch.max_minutes
            if dispatch.max_minutes is not None
            else DEFAULT_MAX_MINUTES
        )
        notes.append(
            f"dispatch kill: {kr} after {n} s "
            f"(stall {s} min, progress {p} min, ceiling {c} min)"
        )
    if oracle is not None:
        notes.append(
            "oracle evidence: "
            f"exit_code={oracle.exit_code}, timed_out={oracle.timed_out}, "
            f"error={oracle.error or 'none'}"
        )
    if (
        oracle is None
        and dispatch.exit_code == 0
        and not dispatch.timed_out
        and dispatch.owned_paths_changed is False
    ):
        result_bytes = json.dumps(
            {"stderr": dispatch.stderr, "stdout": dispatch.stdout},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        result_digest = hashlib.sha256(result_bytes).hexdigest()
        notes.append(f"dispatch result fingerprint: sha256:{result_digest}")
    if no_work_evidence(dispatch, oracle):
        notes.append(
            f"{NO_WORK_NOTE_PREFIX} worker exit 0 with no oracle, no "
            "owned-path change, and no result/answer in stdout (no effective "
            "work evidence); recorded as a platform_env governed failure and "
            "any unchanged re-dispatch of this packet and route is refused"
        )
    if class_derivation is not None:
        notes.append(
            "class derivation overrides: "
            + json.dumps(
                class_derivation.get("override_records", []),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    record: dict[str, Any] = {
        "schema_version": 2,
        "attempt_id": record_attempt_id,
        "record_kind": "router_run",
        "packet_id": packet_id,
        "parent_attempt_id": parent_attempt_id,
        "escalation_reason": escalation_reason,
        "captured_at": timestamp,
        "verified_success": False,
        "route": {
            "model": route.model,
            "effort": route.effort,
            "harness": route.harness,
            "channel": route.channel,
            "provider": provider,
            "channel_instance": outcome.channel_instance,
        },
        "class_record": dict(class_record),
        "verdict": verdict,
        "failure_class": _failure_class(dispatch, oracle),
        "oracle_cmd": oracle_cmd if verdict != "unverified" else None,
        "usage": usage,
        "cost": cost,
        "wall_clock_ms": wall_clock_ms,
        "selection": selection_record(outcome),
        "router_event": router_event,
        "provenance": {
            "source": "router-run",
            "recorded_by": "lee-llm-router run",
            "source_refs": [
                "src/lee_llm_router/events.py EVENT_FIELDS and build_event contract",
                ("config/staffing/terms.yaml via staffing.terms_at and " "route_price"),
                (
                    "docs/staffing/phase1-contracts.md §Attempt record v2 "
                    "placement, §Usage evidence taxonomy, §Cost rule"
                ),
            ],
            "notes": notes,
            "worker_output_dir": worker_output_dir,
        },
    }
    if supervisor_route is not None:
        record["supervisor_route"] = dict(supervisor_route)

    # The v2 gate is intentionally conservative. The CLI does not claim to
    # observe its caller's route; direct callers may supply one only when it
    # is real source evidence (P1-4 ruling 3: the ``--supervisor-route``
    # attestation resolved against the committed catalog). Worker success is
    # also required even though the schema cannot infer it from the
    # provenance note. When the gate does not hold, exactly one
    # ``verified_success_reason`` from the closed P1-4 vocabulary names the
    # first blocking gap in the fixed order below; a true record never
    # carries a reason (schema-forbidden).
    reason = _verified_success_reason(
        dispatch, verdict, record, usage, cost, outcome.channel_kind
    )
    record["verified_success"] = reason is None
    if reason is not None:
        record["verified_success_reason"] = reason
    return record


def _verified_success_reason(
    dispatch: DispatchOutcome,
    verdict: str,
    record: Mapping[str, Any],
    usage: Mapping[str, Any],
    cost: Mapping[str, Any],
    channel_kind: str | None,
) -> str | None:
    """The exact reason ``verified_success`` is false, or ``None`` when true.

    Fixed truthful precedence (P1-4 ruling 3): the attestation gap first,
    then the worker dispatch result, then the oracle verdict, then the
    remaining evidence gate. The first blocking gap in this order names the
    field; later gaps in the same record are never claimed instead, and a
    passing run is never dressed up as a success it cannot evidence.

    Per-token cost evidence (``basis == ["list", "marginal"]``) is required
    only on a ``metered`` channel, where it is real spend evidence. A
    ``subscription`` channel's cost is a list-equivalent estimate, never
    metered spend (usage-truth rule); many subscription-only route models
    (for example ``agy``/Antigravity model ids) have no committed dated
    price at all, so demanding one would make a passing, fully attested
    subscription attempt permanently unverifiable. ``channel_kind`` other
    than ``"subscription"`` — including ``None``, an unrecognised channel —
    still requires computed cost evidence (fail closed).
    """
    if record.get("supervisor_route") is None:
        return VERIFIED_SUCCESS_REASON_UNATTESTED
    if dispatch.timed_out or dispatch.exit_code != 0:
        return VERIFIED_SUCCESS_REASON_WORKER_DISPATCH
    if verdict != "pass":
        return VERIFIED_SUCCESS_REASON_ORACLE
    cost_basis = cost.get("basis")
    cost_evidence_ok = cost_basis == ["list", "marginal"] or (
        channel_kind == "subscription" and cost_basis == ["unavailable"]
    )
    if (
        not record["route"]
        or not record["class_record"]
        or not record["oracle_cmd"]
        or record["failure_class"] is not None
        or usage.get("basis") == "unavailable"
        or not _known_token_count(usage, "input_tokens")
        or not _known_token_count(usage, "output_tokens")
        or not cost_evidence_ok
        or not isinstance(record["wall_clock_ms"], int)
    ):
        return VERIFIED_SUCCESS_REASON_EVIDENCE
    return None


def _failure_class(
    dispatch: DispatchOutcome, oracle: OracleOutcome | None
) -> str | None:
    """Return only a failure class directly supported by run evidence.

    A clean exit that produced no effective-work evidence is a governed
    ``platform_env`` failure (the no-launch/no-work platform surface), not a
    successful unverified run; the matching provenance note carries the
    no-work evidence and drives the unchanged-re-dispatch refusal.
    """
    if dispatch.timed_out or (oracle is not None and oracle.timed_out):
        return "platform_timeout"
    if no_work_evidence(dispatch, oracle):
        return "platform_env"
    if oracle is not None and oracle.error is not None:
        return "platform_env"
    if oracle is not None and oracle.exit_code not in (None, 0):
        return "spec_rejected"
    return None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _pi_provider_id_for_channel(channel: str) -> str:
    """The Pi backend provider id for a funding channel, from committed data.

    The exact reverse of the committed
    :data:`lee_llm_router.crews.HARNESS_PROVIDER_CHANNELS` mapping; no id
    is invented for an unmapped channel (fail closed).
    """
    provider_id = _CHANNEL_TO_PI_PROVIDER_ID.get(channel)
    if provider_id is None:
        raise RunDispatchError(
            f"no committed Pi provider id for channel {channel!r} "
            "(crews.HARNESS_PROVIDER_CHANNELS has no reverse entry)"
        )
    return provider_id


def build_dispatch_command(
    route: StaffingRoute,
    *,
    timeout_seconds: float | None = None,
) -> list[str]:
    """Build the route's dispatch argv through its harness provider.

    The argv comes from the route harness provider's ``build_command`` —
    never from the catalog ``dispatch_template`` shell string and never
    shell-quoted or interpolated. The final element is the literal
    ``{prompt}`` placeholder for harnesses that take the prompt as argv;
    stdin-delivery harnesses (``omp``) end without it and receive the
    prompt on stdin at the subprocess boundary.

    Pi config carries the channel's committed provider id (e.g.
    ``openrouter``) plus the P1-8 bounded editing tool allowlist
    (:data:`_RUN_EDIT_TOOLS`, bash excluded) because governed
    implementation dispatch must be able to edit its scoped file; Codex
    config forces ``json_flag: --json`` so the governed capture receives
    the JSONL usage receipt and appends the exec-level
    ``-s workspace-write --skip-git-repo-check`` sandbox flags for
    noninteractive edits in a non-git scratch workdir; Claude config
    reuses the committed ``CLAUDE_GOVERNED_CONFIG``
    (``--output-format stream-json`` plus the documented safe
    noninteractive permission flags
    ``--permission-mode acceptEdits --permission-prompts none``) for the
    same reason; the agy, OpenCode, and OMP configs apply their accepted
    governed JSON flags (:data:`AGY_GOVERNED_CONFIG` →
    ``--output-format json``, :data:`OPENCODE_GOVERNED_CONFIG` →
    ``--format json``, :data:`OMP_GOVERNED_CONFIG` → ``--mode json``) so
    each harness emits exactly the receipt its accepted P1-4d–f parser
    captures (Astra final review finding 3). No dangerous bypass flag is
    ever emitted.

    Raises:
        RunDispatchError: When the route's harness has no wired provider,
            or a Pi route's channel has no committed provider id.
    """
    harness = route.harness
    provider_cls = _HARNESS_PROVIDER_CLASSES.get(harness)
    if provider_cls is None:
        raise RunDispatchError(
            f"harness {harness!r} has no provider wired for run dispatch"
        )
    provider = provider_cls()
    if harness == "pi":
        config: dict[str, Any] = {
            "provider": _pi_provider_id_for_channel(route.channel),
            "tools": _RUN_EDIT_TOOLS,
        }
    elif harness == "codex":
        config = dict(_CODEX_GOVERNED_CONFIG)
    elif harness == "claude":
        config = dict(CLAUDE_GOVERNED_CONFIG)
    elif harness == "agy":
        # agy print mode waits 5m0s by default and then returns partial
        # output with the turn still in progress (observed 2026-09-12, P5-2b
        # and P5-3: "[agy] print timeout after 5m0s with turn in progress").
        # Align the harness-side wait with the run ceiling so the watchdog,
        # not agy's default, bounds the worker.
        ceiling = (
            timeout_seconds
            if timeout_seconds is not None
            else DEFAULT_MAX_MINUTES * 60.0
        )
        config = {
            "model": route.model,
            **AGY_GOVERNED_CONFIG,
            "print_timeout": f"{int(math.ceil(ceiling))}s",
        }
    elif harness == "opencode":
        config = {"model": route.model, **OPENCODE_GOVERNED_CONFIG}
    elif harness == "omp":
        config = dict(OMP_GOVERNED_CONFIG)
    else:
        config = {}
    return provider.build_command(config, model=route.model, effort=route.effort)


def _capture_failure_usage(harness: str, exc: Exception) -> dict[str, Any]:
    """Schema-valid unavailable v2 usage preserving a capture failure diagnostic."""
    err = str(exc).strip() or type(exc).__name__
    source_desc = _HARNESS_USAGE_SOURCES.get(harness)
    if source_desc:
        reason = f"usage capture failed for harness {harness!r} ({source_desc}): {err}"
    else:
        reason = f"usage capture failed for harness {harness!r}: {err}"
    return {
        "basis": "unavailable",
        "unavailable_reason": reason,
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
    }


def _usage_for_harness(harness: str, stdout_text: str) -> dict[str, Any]:
    """Schema-valid v2 usage from the harness's accepted capture function.

    Pi, Codex, Claude, agy, OpenCode, and OMP have accepted P1-4a–f capture
    parsers; every wired harness records ``unavailable`` with a specific
    reason only when its accepted parser fails closed. The dispatch argv for
    agy, OpenCode, and OMP carries the accepted governed JSON flags
    (:data:`AGY_GOVERNED_CONFIG`, :data:`OPENCODE_GOVERNED_CONFIG`,
    :data:`OMP_GOVERNED_CONFIG`) so the harness emits exactly the receipt
    its parser accepts. When a capture function raises after the worker
    completes, the failure is recorded as unavailable usage preserving the
    failure diagnostic so the completed worker is never dropped from the
    attempt ledger. No parser estimates tokens from text, context length,
    cost, or elapsed time.
    """
    try:
        if harness == "pi":
            return capture_pi_usage(stdout_text)
        if harness == "codex":
            return capture_codex_usage(stdout_text)
        if harness == "claude":
            return capture_claude_usage(stdout_text)
        if harness == "agy":
            return capture_agy_usage(stdout_text)
        if harness == "opencode":
            return capture_opencode_usage(stdout_text)
        if harness == "omp":
            return capture_omp_usage(stdout_text)
    except Exception as exc:
        return _capture_failure_usage(harness, exc)
    return {
        "basis": "unavailable",
        "unavailable_reason": (
            f"no usage capture parser wired for harness {harness!r}"
        ),
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
    }


def _dispatch_resolution(route: StaffingRoute, argv: list[str]) -> Resolution:
    """Build the dispatch-boundary resolution for one selected route."""
    return Resolution(
        worker_id=route.route_id,
        dispatch_command=list(argv),
        prompt_delivery=("argv" if PROMPT_PLACEHOLDER in argv else "stdin"),
    )


def _format_minutes(val: float) -> str:
    """Format minutes nicely for display: whole numbers as integers, else float."""
    r = round(val)
    if abs(val - r) < 0.05:
        return str(int(r))
    return f"{val:g}"


def _build_argv(resolution: Resolution, prompt: str) -> list[str]:
    """Build the final child argv from the resolution and prompt.

    If prompt delivery is "argv", replaces the single ``{prompt}`` placeholder
    with the prompt text. Otherwise, returns a copy of the dispatch command.
    """
    if resolution.prompt_delivery == "argv":
        return [
            prompt if arg == PROMPT_PLACEHOLDER else arg
            for arg in resolution.dispatch_command
        ]
    return list(resolution.dispatch_command)


_PROCESS_GROUP_KILL_GRACE_SECONDS = 0.5
"""Seconds a worker process group gets to exit after a graceful TERM
before the ceiling kill escalates to SIGKILL on the whole group."""

_WORKER_PROVENANCE_ENV = "LEE_LLM_ROUTER_WORKER_PROVENANCE"
"""Private inherited identity used to find Linux workers after reparenting.

A fresh value is placed only in the real worker's environment. Descendants
inherit it across ``fork``/``exec``, including descendants that start a new
session and outlive an intermediate parent. It is never added to the router's
own environment or to injected fake ``Popen`` calls.
"""


@dataclass
class _ProcessRef:
    """A process identity used while terminating a Linux process tree.

    A PID is not an identity: it can be reused after a process exits. Linux
    pidfds make the signal operation identity-safe; the ``start_time`` check
    is the fallback for older Linux kernels and also protects discovery from
    accidentally following a reused PID.
    """

    pid: int
    start_time: int
    depth: int
    pidfd: int | None = None


def _real_popen_injected(popen: Callable[..., Any]) -> bool:
    """Whether a spawner is known to delegate to real ``subprocess.Popen``.

    Identity with the mutable ``_DEFAULT_POPEN`` is deliberately insufficient:
    tests replace that boundary with fakes, which must receive neither POSIX
    launch flags nor a copied environment. The private attribute is set only
    on this module's argv/cwd wrapper around the real spawner.
    """
    return (
        popen is subprocess.Popen
        or getattr(popen, "_lee_llm_router_real_popen", False) is True
    )


def _linux_process_record(pid: int) -> tuple[int, int] | None:
    """Return ``(ppid, starttime)`` from Linux ``/proc/<pid>/stat``.

    The executable name is parenthesized and may itself contain spaces or
    closing parentheses, so parsing starts after the final ``)`` rather than
    splitting the whole record naively. ``starttime`` is field 22, measured
    from boot; unlike a PID it remains tied to one process incarnation.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    closing_name = stat.rfind(")")
    if closing_name < 0:
        return None
    fields = stat[closing_name + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[1]), int(fields[19])
    except ValueError:
        return None


def _linux_process_table() -> dict[int, tuple[int, int]]:
    """Take one best-effort ``pid -> (ppid, starttime)`` process snapshot."""
    table: dict[int, tuple[int, int]] = {}
    try:
        entries = os.scandir("/proc")
    except OSError:
        return table
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            record = _linux_process_record(int(entry.name))
            if record is not None:
                table[int(entry.name)] = record
    return table


def _linux_process_has_marker(pid: int, marker: bytes) -> bool:
    """Whether one process has the exact inherited provenance entry.

    Reading ``environ`` can fail under ``hidepid`` or after process exit. That
    is a normal miss: callers retain process-group and PPID-tree fallbacks and
    must never broaden a failed lookup into an unscoped signal.
    """
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return False
    return marker in environ.split(b"\0")


class _LinuxDescendantTree:
    """Discover and kill a worker's recursive Linux descendant tree.

    A process group is not a process tree: a descendant can call
    ``setsid(2)`` and escape the worker's group while remaining its child.
    This tracker follows PPID links in ``/proc`` and retains identities that
    were discovered before a parent is killed. On timeout it stops the root,
    repeatedly stops newly discovered descendants, then kills the frozen
    identities. Stopping first closes the fork/discovery race: once every
    known generation is stopped, no member can create another generation.
    """

    _MAX_FREEZE_PASSES = 20

    def __init__(
        self, root_pid: int, root_start_time: int, provenance_marker: bytes | None
    ) -> None:
        self._root = _ProcessRef(root_pid, root_start_time, 0)
        self._provenance_marker = provenance_marker
        self._known: dict[int, _ProcessRef] = {}
        self._stale: set[int] = set()

    @staticmethod
    def available() -> bool:
        """Whether this Linux-specific tree boundary can inspect ``/proc``."""
        return os.name == "posix" and sys.platform.startswith("linux")

    def _remember(
        self, candidates: Sequence[_ProcessRef], *, marker_verified: bool = False
    ) -> None:
        for candidate in candidates:
            if candidate.pid in self._stale:
                if not marker_verified:
                    continue
                # The exact fresh marker proves this reused PID belongs to the
                # worker too; an ancestry-only candidate cannot make that leap.
                self._stale.remove(candidate.pid)
            previous = self._known.get(candidate.pid)
            if previous is None:
                self._known[candidate.pid] = candidate
            elif previous.start_time != candidate.start_time:
                self._close_ref(previous)
                del self._known[candidate.pid]
                self._stale.add(candidate.pid)
            else:
                previous.depth = min(previous.depth, candidate.depth)

    def _descendants_from_table(
        self, table: Mapping[int, tuple[int, int]]
    ) -> list[_ProcessRef]:
        root_record = table.get(self._root.pid)
        if root_record is None or root_record[1] != self._root.start_time:
            return []
        children: dict[int, list[tuple[int, int]]] = {}
        for pid, (ppid, start_time) in table.items():
            children.setdefault(ppid, []).append((pid, start_time))
        found: list[_ProcessRef] = []
        frontier = [(self._root.pid, 0)]
        seen = {self._root.pid}
        while frontier:
            parent, depth = frontier.pop()
            for pid, start_time in children.get(parent, ()):
                if pid in seen:
                    continue
                seen.add(pid)
                found.append(_ProcessRef(pid, start_time, depth + 1))
                frontier.append((pid, depth + 1))
        return found

    def refresh(self, *, include_marked: bool = False) -> list[_ProcessRef]:
        """Refresh known identities from ancestry and optionally the marker.

        PPID discovery runs on every supervisor poll, retaining a descendant
        after it reparents. At timeout, marker discovery additionally recovers
        descendants whose whole ancestry was born and disappeared between two
        polls. Only the exact per-launch random environment entry is accepted.
        """
        table = _linux_process_table()
        for pid, ref in tuple(self._known.items()):
            current = table.get(pid)
            if current is None or current[1] != ref.start_time:
                self._close_ref(ref)
                del self._known[pid]
                self._stale.add(pid)
        self._remember(self._descendants_from_table(table))
        if include_marked and self._provenance_marker is not None:
            marked = [
                _ProcessRef(pid, start_time, 1)
                for pid, (_ppid, start_time) in table.items()
                if pid != self._root.pid
                and _linux_process_has_marker(pid, self._provenance_marker)
            ]
            self._remember(marked, marker_verified=True)
        return list(self._known.values())

    @staticmethod
    def _close_ref(ref: _ProcessRef) -> None:
        if ref.pidfd is not None:
            try:
                os.close(ref.pidfd)
            except OSError:
                pass
            ref.pidfd = None

    @staticmethod
    def _pidfd_send(ref: _ProcessRef, sig: signal.Signals) -> bool:
        """Signal one identity, using a pidfd whenever the kernel supports it."""
        if ref.pidfd is None:
            current = _linux_process_record(ref.pid)
            if current is None or current[1] != ref.start_time:
                return False
            pidfd_open = getattr(os, "pidfd_open", None)
            if callable(pidfd_open):
                try:
                    ref.pidfd = pidfd_open(ref.pid)
                except OSError:
                    return False
                # The pidfd now pins the incarnation. This second check
                # rejects a reuse that raced the first /proc read.
                current = _linux_process_record(ref.pid)
                if current is None or current[1] != ref.start_time:
                    _LinuxDescendantTree._close_ref(ref)
                    return False
        try:
            pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
            if ref.pidfd is not None and callable(pidfd_send_signal):
                pidfd_send_signal(ref.pidfd, sig)
            else:
                # This is only the old-kernel fallback. The identity check
                # above is the strongest protection available without pidfds.
                os.kill(ref.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True

    def _signal_newest_first(self, sig: signal.Signals) -> None:
        for ref in sorted(
            self._known.values(), key=lambda item: item.depth, reverse=True
        ):
            self._pidfd_send(ref, sig)

    def terminate(self, kill_group: Callable[[int], None]) -> None:
        """Freeze and kill all discovered generations, then close pidfds."""
        try:
            self.refresh(include_marked=True)
            # Stop the root first. It cannot fork while we stabilize the
            # descendant snapshot, while already-running descendants are
            # caught by the repeated marker/ancestry passes below.
            self._pidfd_send(self._root, signal.SIGSTOP)
            previous_pids: frozenset[int] = frozenset()
            for _ in range(self._MAX_FREEZE_PASSES):
                self.refresh(include_marked=True)
                self._signal_newest_first(signal.SIGSTOP)
                current_pids = frozenset(self._known)
                if current_pids == previous_pids:
                    break
                previous_pids = current_pids
                time.sleep(0.005)
            self.refresh(include_marked=True)
            self._signal_newest_first(signal.SIGKILL)
            self._pidfd_send(self._root, signal.SIGKILL)
            # The original group catches any same-group process missed by a
            # procfs race. ``kill_group`` refuses the supervisor's own group.
            kill_group(signal.SIGKILL)
        finally:
            self._close_ref(self._root)
            for ref in self._known.values():
                self._close_ref(ref)


class _ProcessGroupProxy:
    """Wrapper around a real ``subprocess.Popen`` launched as a session leader.

    Real POSIX workers are session leaders. On Linux, a timeout kills the
    recursive PPID tree as well as the original process group, so a
    descendant that calls ``start_new_session=True`` cannot survive by
    escaping that group. The fallback retains the process-group behavior on
    other POSIX systems. Signals are never sent to the supervisor's group.
    """

    def __init__(
        self, proc: subprocess.Popen, provenance_marker: bytes | None = None
    ) -> None:
        self._proc = proc
        try:
            self._pgid: int | None = os.getpgid(proc.pid)
        except (OSError, AttributeError):
            self._pgid = None
        root_record = (
            _linux_process_record(proc.pid)
            if _LinuxDescendantTree.available()
            else None
        )
        self._tree = (
            _LinuxDescendantTree(proc.pid, root_record[1], provenance_marker)
            if root_record is not None
            else None
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._proc, name)

    def _group_id(self) -> int | None:
        """The child's process group id, or ``None`` when unavailable."""
        if not hasattr(os, "killpg"):
            return None
        pgid = self._pgid
        if pgid is None:
            return None
        # Never signal the parent's own group: a child that somehow shares
        # our process group must be killed directly, not by killpg.
        try:
            if pgid == os.getpgid(0):
                return None
        except OSError:
            return None
        return pgid

    def _kill_group(self, sig: int) -> None:
        pgid = self._group_id()
        if pgid is None:
            return
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _fallback_kill(self) -> None:
        """Kill the original group when Linux tree discovery is unavailable."""
        self._kill_group(signal.SIGTERM)
        deadline = time.monotonic() + _PROCESS_GROUP_KILL_GRACE_SECONDS
        while time.monotonic() < deadline:
            try:
                pgid = self._group_id()
                if pgid is None:
                    break
                os.killpg(pgid, 0)
            except ProcessLookupError:
                break
            except (PermissionError, OSError):
                break
            else:
                time.sleep(0.01)
        self._kill_group(signal.SIGKILL)
        try:
            self._proc.kill()
        except Exception:
            pass

    def poll(self) -> int | None:
        """Poll the worker while continuously retaining descendant identities."""
        if self._tree is not None:
            try:
                self._tree.refresh()
            except Exception:
                # Procfs tracking is an additional Linux containment layer.
                # Failure narrows cleanup to the worker's isolated group; it
                # never justifies signaling a process whose identity is unknown.
                pass
        return self._proc.poll()

    def kill(self) -> None:
        """Terminate the worker tree without touching the supervisor."""
        if self._tree is None:
            self._fallback_kill()
            return
        try:
            self._tree.terminate(self._kill_group)
        except Exception:
            # A procfs race must not leave the direct worker alive. The
            # fallback group kill is still scoped to this session.
            self._fallback_kill()
        try:
            self._proc.kill()
        except Exception:
            pass

    def wait(self, timeout: float | None = None) -> Any:
        """Reap the direct child, then defensively reap group leftovers."""
        try:
            code = self._proc.wait(timeout=timeout)
        finally:
            self._reap_group()
        return code

    def _reap_group(self) -> None:
        """Best-effort reaping of any group members still our children."""
        pgid = self._group_id()
        if pgid is None:
            return
        while True:
            try:
                pid, _status = os.waitpid(-pgid, os.WNOHANG)
            except (ChildProcessError, OSError):
                return
            if pid == 0:
                return


class SupervisedDispatchExitCode(int):
    """An int exit code that preserves the watchdog kill reason when killed."""

    kill_reason: str | None

    def __new__(cls, value: int, kill_reason: str | None = None):
        obj = super().__new__(cls, value)
        obj.kill_reason = kill_reason
        return obj


def run_supervised_dispatch(
    resolution: Resolution,
    prompt: str,
    *,
    stall_minutes: float = DEFAULT_STALL_MINUTES,
    max_minutes: float = DEFAULT_MAX_MINUTES,
    progress_minutes: float | None = 20.0,
    stall_action: str = "kill",
    watch_dirs: Sequence[Path | str] = (),
    popen: Callable[..., Any] = subprocess.Popen,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = 0.5,
    sink: Callable[[bytes], None] | None = None,
    err_sink: Callable[[bytes], None] | None = None,
    read_chunk: Callable[[], bytes | None] | None = None,
    read_err_chunk: Callable[[], bytes | None] | None = None,
    stderr: TextIO | None = None,
    extra_env: dict[str, str] | None = None,
) -> int:
    """Execute a resolved worker harness and supervise it with StallWatchdog.

    Moved here (P2-9 removal) from the deleted ``lee_llm_router.dispatch``
    with its resolution consumption minimized to the worker id, final argv,
    and prompt delivery. Behavior is unchanged: stream stdout/stderr, feed
    stdin when the prompt is delivered on stdin, flag stalls without
    killing, and kill only at the wall-clock ceiling.

    Args:
        resolution: Dispatch-boundary resolution (worker id, argv, delivery).
        prompt: Prompt string to deliver.
        stall_minutes: Minutes of joint silence before flagging a stall.
        max_minutes: Wall-clock ceiling in minutes before killing the child.
        progress_minutes: Minutes without watch-dir change before killing.
        stall_action: Action on stall ("kill" or "warn").
        watch_dirs: Directories to monitor for file activity.
        popen: Process spawner callable (injected for tests).
        clock: Monotonic clock callable (injected for tests).
        sleep: Sleep callable (injected for tests).
        poll_seconds: Polling cadence for the watchdog loop.
        sink: Callable receiving child stdout byte chunks (defaults to
            stdout buffer).
        err_sink: Callable receiving child stderr byte chunks (defaults to
            stderr buffer).
        read_chunk: Callable returning child stdout byte chunks (defaults to
            reading child stdout).
        read_err_chunk: Callable returning child stderr byte chunks (defaults
            to reading child stderr).
        stderr: Stream for watchdog warnings and ceiling messages (defaults
            to sys.stderr).
        extra_env: Environment variables to merge into the child environment.

    Returns:
        Exit code: 124 on ceiling timeout, otherwise child exit code.
    """
    argv = _build_argv(resolution, prompt)
    target_err = stderr if stderr is not None else sys.stderr

    if sink is not None:
        sink_fn = sink
    else:

        def sink_fn(chunk: bytes) -> None:
            if not chunk:
                return
            stdout_buf = getattr(sys.stdout, "buffer", None)
            if stdout_buf is not None:
                stdout_buf.write(chunk)
                stdout_buf.flush()
            else:
                sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                sys.stdout.flush()

    if err_sink is not None:
        err_sink_fn = err_sink
    else:

        def err_sink_fn(chunk: bytes) -> None:
            if not chunk:
                return
            target_err_buf = getattr(target_err, "buffer", None)
            if target_err_buf is not None:
                target_err_buf.write(chunk)
                target_err_buf.flush()
            else:
                target_err.write(chunk.decode("utf-8", errors="replace"))
                target_err.flush()

    launch_kwargs: dict[str, Any] = {
        "stdin": subprocess.PIPE if resolution.prompt_delivery == "stdin" else None,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    # Astra P2-10: isolate real POSIX workers in a session. On Linux, also
    # inject a fresh identity into only the child's environment. Unlike a PPID
    # chain, that identity survives both setsid and reparenting when a short-
    # lived intermediate exits before the next supervisor poll. Fakes receive
    # neither the platform flag nor an environment override.
    real_popen = _real_popen_injected(popen)
    provenance_marker: bytes | None = None
    child_env: dict[str, str] | None = None
    if extra_env:
        child_env = os.environ.copy()
        child_env.update(extra_env)
    if os.name == "posix" and real_popen:
        launch_kwargs["start_new_session"] = True
        if _LinuxDescendantTree.available():
            provenance_value = uuid.uuid4().hex
            if child_env is None:
                child_env = os.environ.copy()
            child_env[_WORKER_PROVENANCE_ENV] = provenance_value
            provenance_marker = f"{_WORKER_PROVENANCE_ENV}={provenance_value}".encode(
                "ascii"
            )
    if child_env is not None:
        launch_kwargs["env"] = child_env
    proc = popen(argv, **launch_kwargs)
    if os.name == "posix" and isinstance(proc, subprocess.Popen):
        proc = _ProcessGroupProxy(proc, provenance_marker)

    stdin_thread: threading.Thread | None = None
    if resolution.prompt_delivery == "stdin":
        prompt_bytes = prompt.encode("utf-8") if isinstance(prompt, str) else prompt

        def _feed_stdin() -> None:
            try:
                proc.stdin.write(prompt_bytes)
                proc.stdin.flush()
            except OSError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

        stdin_thread = threading.Thread(target=_feed_stdin, daemon=True)
        stdin_thread.start()

    if read_chunk is not None:
        read_stdout_fn = read_chunk
    else:
        os.set_blocking(proc.stdout.fileno(), False)

        def read_stdout_fn() -> bytes | None:
            try:
                chunk = proc.stdout.read(65536)
                return chunk if chunk else None
            except BlockingIOError:
                return None

    if read_err_chunk is not None:
        read_stderr_fn = read_err_chunk
    else:
        os.set_blocking(proc.stderr.fileno(), False)

        def read_stderr_fn() -> bytes | None:
            try:
                chunk = proc.stderr.read(65536)
                return chunk if chunk else None
            except BlockingIOError:
                return None

    def supervised_read() -> bytes | None:
        total = 0

        while True:
            chunk = read_stdout_fn()
            if not chunk:
                break
            sink_fn(chunk)
            total += len(chunk)

        while True:
            chunk = read_stderr_fn()
            if not chunk:
                break
            err_sink_fn(chunk)
            total += len(chunk)

        if total > 0:
            return bytes(total)
        return None

    def on_stall(report: StallReport) -> None:
        n_str = _format_minutes(stall_minutes)
        m_str = _format_minutes(report.elapsed_seconds / 60.0)
        max_str = _format_minutes(max_minutes)
        msg = (
            f"dispatch: no output and no file activity for {n_str} min "
            f"(worker {resolution.worker_id}, elapsed {m_str} min); "
            f"still waiting, ceiling {max_str} min\n"
        )
        target_err.write(msg)
        target_err.flush()

    watch_paths = [Path(p) for p in watch_dirs]
    stall_seconds = float(stall_minutes) * 60.0
    max_seconds = float(max_minutes) * 60.0
    progress_seconds = (
        float(progress_minutes) * 60.0
        if progress_minutes is not None and progress_minutes > 0
        else None
    )

    watchdog = StallWatchdog(
        stall_seconds=stall_seconds,
        max_seconds=max_seconds,
        clock=clock,
        watch_dirs=watch_paths,
        on_stall=on_stall,
        stall_action=stall_action,
        progress_seconds=progress_seconds,
    )

    try:
        result = run_supervised(
            proc,
            watchdog,
            poll_seconds=poll_seconds,
            sleep=sleep,
            read_chunk=supervised_read,
            sink=lambda _chunk: None,
        )
    finally:
        if stdin_thread is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
            stdin_thread.join(timeout=1.0)

    if result.killed:
        p_str = _format_minutes(progress_minutes) if progress_minutes else "none"
        msg = {
            "stall": f"{_format_minutes(stall_minutes)} min stall",
            "no_progress": f"{p_str} min no progress",
        }.get(result.kill_reason or "", f"{_format_minutes(max_minutes)} min ceiling")
        target_err.write(f"dispatch: killed after {msg}\n")
        target_err.flush()
        return SupervisedDispatchExitCode(
            _TIMEOUT_EXIT_CODE, kill_reason=result.kill_reason
        )

    if result.exit_code is not None:
        return SupervisedDispatchExitCode(result.exit_code, kill_reason=None)
    return SupervisedDispatchExitCode(0, kill_reason=None)


def dispatch_route(
    route: StaffingRoute,
    prompt: str,
    *,
    workdir: str | Path | None = None,
    timeout_seconds: float | None = DEFAULT_RUN_TIMEOUT_SECONDS,
    stall_minutes: float = DEFAULT_STALL_MINUTES,
    progress_minutes: float | None = 20.0,
    stall_action: str = "kill",
    watch_dirs: Sequence[Path | str] = (),
    popen: Callable[..., Any] | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    stderr: TextIO | None = None,
    extra_env: dict[str, str] | None = None,
) -> DispatchOutcome:
    """Dispatch the selected route once through the watchdog boundary.

    The route's provider ``build_command`` argv carries the packet prompt
    (argv substitution, or stdin delivery when the harness has no
    placeholder), the child's ``cwd`` is the workdir when supplied, and
    nothing is ever shell-quoted or interpolated. The single launch is
    supervised by :func:`run_supervised_dispatch` (stall
    watchdog + wall-clock ceiling); there is no retry and no escalation.

    Args:
        route: The selected catalog route.
        prompt: The prompt text taken verbatim from the packet.
        workdir: Optional child working directory; must exist.
        timeout_seconds: Wall-clock ceiling in seconds; ``None`` uses the
            boundary default (:data:`DEFAULT_MAX_MINUTES` minutes).
        stall_minutes: Silence minutes before a stall is flagged.
        progress_minutes: Minutes without watch-dir change before kill.
        stall_action: Action on stall ("kill" or "warn").
        watch_dirs: Directories to monitor for file activity.
        popen: Injected process spawner (tests).
        clock: Injected monotonic clock (tests).
        sleep: Injected sleep callable (tests).
        poll_seconds: Watchdog polling cadence.
        stderr: Stream for watchdog warnings (defaults to ``sys.stderr``).
        extra_env: Optional environment variables to merge into the child
            environment.

    Returns:
        The :class:`DispatchOutcome` with the final argv, captured
        stdout/stderr, exit code, wall-clock duration, timeout flag, the
        schema-valid usage from the harness's accepted capture function,
        and the Claude ``usage_models`` billing evidence when the receipt
        carries ``modelUsage`` model ids.

    Raises:
        RunDispatchError: When the harness has no wired provider, a Pi
            route's channel has no committed provider id, the workdir does
            not exist, or the timeout is not positive.
    """
    harness = route.harness
    argv = build_dispatch_command(route, timeout_seconds=timeout_seconds)

    if workdir is not None:
        workdir_path = Path(workdir)
        if not workdir_path.is_dir():
            raise RunDispatchError(f"workdir is not a directory: {workdir_path}")

    if timeout_seconds is None:
        max_minutes: float = DEFAULT_MAX_MINUTES
    else:
        if timeout_seconds <= 0:
            raise RunDispatchError(
                f"timeout must be a positive number of seconds, got "
                f"{timeout_seconds!r}"
            )
        max_minutes = float(timeout_seconds) / 60.0

    base_popen = popen if popen is not None else _DEFAULT_POPEN
    clock_fn = clock if clock is not None else _DEFAULT_CLOCK
    sleep_fn = sleep if sleep is not None else _DEFAULT_SLEEP
    cwd = str(Path(workdir)) if workdir is not None else None

    def safe_popen(child_argv: list[str], **kwargs: Any) -> Any:
        """Keep the worker boundary explicitly argv-only and non-shell."""
        kwargs["shell"] = False
        if cwd is not None:
            kwargs["cwd"] = cwd
        return base_popen(child_argv, **kwargs)

    # Let the supervision layer apply real-process containment without
    # mistaking a monkeypatched ``_DEFAULT_POPEN`` fake for the real spawner.
    setattr(
        safe_popen,
        "_lee_llm_router_real_popen",
        _real_popen_injected(base_popen),
    )
    popen_fn: Callable[..., Any] = safe_popen

    resolution = _dispatch_resolution(route, argv)
    stdout_buf = bytearray()
    stderr_buf = bytearray()

    # Effective-work evidence: snapshot the watched owned paths immediately
    # before and after the dispatch window. ``None`` means no path was
    # watched, so the router must not claim either change or no change.
    watch_paths = tuple(Path(path) for path in watch_dirs)
    owned_before = scan_watch_dirs(watch_paths) if watch_paths else None

    started = clock_fn()
    exit_code = run_supervised_dispatch(
        resolution,
        prompt,
        stall_minutes=stall_minutes,
        max_minutes=max_minutes,
        progress_minutes=progress_minutes,
        stall_action=stall_action,
        watch_dirs=watch_dirs,
        popen=popen_fn,
        clock=clock_fn,
        sleep=sleep_fn,
        poll_seconds=poll_seconds,
        sink=stdout_buf.extend,
        err_sink=stderr_buf.extend,
        stderr=stderr,
        extra_env=extra_env,
    )
    duration = clock_fn() - started

    owned_after = scan_watch_dirs(watch_paths) if watch_paths else None
    owned_paths_changed: bool | None = None
    if owned_before is not None and owned_after is not None:
        owned_paths_changed = owned_after != owned_before

    kill_reason = getattr(exit_code, "kill_reason", None)
    if kill_reason is None and exit_code == _TIMEOUT_EXIT_CODE:
        kill_reason = "ceiling"

    stdout_text = bytes(stdout_buf).decode("utf-8", errors="replace")
    # Usage parsing and Claude aggregate-model extraction are one capture
    # boundary. Both inspect the same receipt, and a failure in either must
    # preserve the completed worker as one unavailable-evidence outcome.
    try:
        usage = _usage_for_harness(harness, stdout_text)
        # Claude ``modelUsage`` model ids are billing-relevance evidence: the
        # selected dated price terms price exactly the route's model, so cost
        # needs to know when the receipt aggregates several models.
        usage_models = (
            claude_aggregate_models(stdout_text) if harness == "claude" else None
        )
    except Exception as exc:
        usage = _capture_failure_usage(harness, exc)
        usage_models = None
    return DispatchOutcome(
        argv=tuple(argv),
        exit_code=int(exit_code),
        stdout=stdout_text,
        stderr=bytes(stderr_buf).decode("utf-8", errors="replace"),
        duration_seconds=duration,
        timed_out=(exit_code == _TIMEOUT_EXIT_CODE),
        usage=usage,
        usage_models=usage_models,
        kill_reason=kill_reason,
        stall_minutes=stall_minutes,
        progress_minutes=progress_minutes,
        max_minutes=max_minutes,
        owned_paths_changed=owned_paths_changed,
    )


_WORK_TEXT_KEYS = frozenset(
    {
        "answer",
        "completion",
        "content",
        "message",
        "output",
        "output_text",
        "response",
        "result",
        "text",
    }
)
"""JSON keys whose non-empty string value is a worker answer/result.

The set is a contract about answer shape, not a harness list: every wired
harness embeds its final text under one of these keys (``content``, ``text``,
``response``, ``output_text``, ...), while a terminal usage receipt carries
only type/usage metadata. Classifying output shape keeps the rule
harness-neutral.
"""


_METADATA_EVENT_TOKENS = frozenset(
    {
        "complete",
        "completed",
        "completion",
        "done",
        "end",
        "ended",
        "error",
        "failed",
        "failure",
        "finish",
        "finished",
        "status",
        "usage",
    }
)
"""Type-name tokens that identify lifecycle, error, or accounting events."""


_ANSWER_EVENT_TYPES = frozenset(
    {
        "agent_message",
        "answer",
        "assistant",
        "assistant_message",
        "output_text",
        "result",
    }
)
"""Typed payload objects that explicitly identify generated answer text."""


_BLOCKING_METADATA_EVENT_TOKENS = frozenset(
    {"error", "failed", "failure", "status", "usage"}
)
"""Metadata event tokens whose nested hints cannot become answer evidence."""


_GENERATED_MESSAGE_KEYS = frozenset({"item", "message", "response"})
"""Direct slots that may contain a generated-message object."""


_GENERATED_MESSAGE_EVENT_TYPES = frozenset(
    {"agent_message", "assistant", "assistant_message", "output_text"}
)
"""Types that unambiguously identify generated text inside such a slot."""


_JSON_PARSE_FAILED = object()
"""Sentinel distinguishing a JSON ``null`` parse from a parse failure."""


def _try_parse_json(text: str) -> Any:
    """Parse one JSON document, returning :data:`_JSON_PARSE_FAILED` on failure."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return _JSON_PARSE_FAILED


def _normalize_event_name(value: str) -> str:
    """Return one separator-normalized event discriminator."""
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value.strip())
    return re.sub(r"[^a-z0-9]+", "_", separated.lower()).strip("_")


def _normalized_event_type(value: Mapping[str, Any]) -> str:
    """Return the structural event discriminator for an output object.

    Harnesses spell the same envelope discriminator as ``type`` or ``event``.
    Some status-only envelopes provide neither and identify terminal lifecycle
    metadata through a scalar ``status`` value. An explicit ``type`` remains
    authoritative, then ``event``; status is used only when its value contains
    a recognized lifecycle/error/accounting token. In particular, an untyped
    generated response with ``status=SUCCESS`` remains answer evidence.
    """
    for key in ("type", "event"):
        discriminator = value.get(key)
        if isinstance(discriminator, str) and discriminator.strip():
            return _normalize_event_name(discriminator)

    status = value.get("status")
    if not isinstance(status, str):
        return ""
    normalized = _normalize_event_name(status)
    if any(token in _METADATA_EVENT_TOKENS for token in normalized.split("_")):
        return normalized
    return ""


def _is_answer_event(value: Mapping[str, Any]) -> bool:
    """Whether an object explicitly identifies an assistant answer payload."""
    role = value.get("role")
    if isinstance(role, str) and role.strip().lower() in {"agent", "assistant"}:
        return True
    return _normalized_event_type(value) in _ANSWER_EVENT_TYPES


def _is_generated_message_event(value: Mapping[str, Any]) -> bool:
    """Whether a direct slotted object unambiguously identifies generated text."""
    role = value.get("role")
    if isinstance(role, str) and role.strip().lower() in {"agent", "assistant"}:
        return True
    if _normalized_event_type(value) in _GENERATED_MESSAGE_EVENT_TYPES:
        return True
    output_text = value.get("output_text")
    return isinstance(output_text, str) and bool(output_text.strip())


def _is_metadata_event(value: Mapping[str, Any]) -> bool:
    """Whether an object identifies lifecycle, error, or accounting metadata."""
    normalized = _normalized_event_type(value)
    return bool(normalized) and any(
        token in _METADATA_EVENT_TOKENS for token in normalized.split("_")
    )


def _is_blocking_metadata_event(value: Mapping[str, Any]) -> bool:
    """Whether metadata context must remain authoritative for descendants."""
    tokens = _normalized_event_type(value).split("_")
    return any(token in _BLOCKING_METADATA_EVENT_TOKENS for token in tokens)


def _grants_generated_message_slot(value: Mapping[str, Any]) -> bool:
    """Whether this event may carry a direct generated-message child."""
    tokens = _normalized_event_type(value).split("_")
    return any(
        token
        in {
            "complete",
            "completed",
            "completion",
            "done",
            "end",
            "ended",
            "finish",
            "finished",
        }
        for token in tokens
    ) and not _is_blocking_metadata_event(value)


def _json_has_work_text(
    value: Any,
    *,
    content_key: bool = False,
    metadata_event: bool = False,
    metadata_locked: bool = False,
    generated_message_slot: bool = False,
    event_root: bool = True,
    root_string_array: bool = False,
) -> bool:
    """Whether a parsed JSON value carries a non-empty worker answer string.

    Metadata context is inherited through generic nested containers, so status
    prose cannot become work merely by moving under ``content`` or ``output``.
    Error, status, and usage events lock that context before same-object role
    or result hints are considered. A completion event can leave metadata
    context only through its own direct generated-message slot (``message``,
    ``item``, or ``response``) whose object explicitly identifies generated
    assistant/agent text; generic descendants such as ``detail`` and ``payload``
    cannot regrant that permission at a deeper generated-message key. Only a
    top-level event object (including an event in a top-level array) can
    originate the slot.
    """
    if isinstance(value, str):
        return (
            not metadata_event
            and (content_key or root_string_array)
            and bool(value.strip())
        )
    if isinstance(value, Mapping):
        grants_generated_slot = event_root and _grants_generated_message_slot(value)
        if _is_metadata_event(value):
            metadata_event = True
            if _is_blocking_metadata_event(value):
                metadata_locked = True
        elif not metadata_locked and (
            (not metadata_event and _is_answer_event(value))
            or (
                metadata_event
                and generated_message_slot
                and _is_generated_message_event(value)
            )
        ):
            metadata_event = False
        for key, item in value.items():
            normalized_key = key.lower() if isinstance(key, str) else ""
            is_content = normalized_key in _WORK_TEXT_KEYS
            if (
                is_content
                and not metadata_event
                and isinstance(item, str)
                and item.strip()
            ):
                return True
            if _json_has_work_text(
                item,
                content_key=is_content,
                metadata_event=metadata_event,
                metadata_locked=metadata_locked,
                generated_message_slot=(
                    grants_generated_slot and normalized_key in _GENERATED_MESSAGE_KEYS
                ),
                event_root=False,
            ):
                return True
        return False
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(
            _json_has_work_text(
                item,
                content_key=content_key,
                metadata_event=metadata_event,
                metadata_locked=metadata_locked,
                generated_message_slot=generated_message_slot,
                event_root=event_root,
                root_string_array=root_string_array,
            )
            for item in value
        )
    return False


def stdout_has_work_evidence(stdout: str) -> bool:
    """Whether captured worker stdout carries a substantive answer/result.

    A terminal usage receipt (for example a codex ``turn.completed`` event or a
    Pi ``message_end`` carrying only usage) is not work. Real work is a
    non-empty answer string under a content-bearing key, or plain non-JSON
    prose. The rule classifies output shape, never a harness id, and uses no
    token or elapsed-time threshold.

    Args:
        stdout: The captured worker stdout.

    Returns:
        True when at least one substantive answer/result is present.
    """
    if not isinstance(stdout, str) or not stdout.strip():
        return False
    text = stdout.strip()
    whole = _try_parse_json(text)
    if whole is not _JSON_PARSE_FAILED:
        if isinstance(whole, str):
            return bool(whole.strip())
        return _json_has_work_text(
            whole,
            root_string_array=isinstance(whole, Sequence)
            and not isinstance(whole, (str, bytes)),
        )
    saw_json = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = _try_parse_json(line)
        if parsed is _JSON_PARSE_FAILED:
            # Non-JSON worker text is itself substantive output.
            return True
        saw_json = True
        if isinstance(parsed, str) and parsed.strip():
            return True
        if _json_has_work_text(parsed):
            return True
    return not saw_json


def no_work_evidence(dispatch: DispatchOutcome, oracle: OracleOutcome | None) -> bool:
    """Whether one completed dispatch produced no effective-work evidence.

    A clean exit is not, by itself, work: the observed failure shape is a
    terminal usage receipt (or no output at all) with no verification oracle
    and no owned-path change. This predicate is harness-neutral and
    content-based (no token or elapsed-time threshold, no packet-id or harness
    special case): the worker must have exited 0 without a timeout, no oracle
    may have run, the dispatch boundary must have positively measured the
    owned paths as unchanged, and stdout must carry no substantive
    answer/result (:func:`stdout_has_work_evidence`). An unmeasured owned-path
    set (``None``) fails closed to "not no-work", never to a false accusation.

    Args:
        dispatch: The one completed worker dispatch.
        oracle: The optional oracle outcome; a present oracle is verification
            evidence and never counts as no work.

    Returns:
        True only when every no-work condition is positively evidenced.
    """
    if oracle is not None:
        return False
    if dispatch.timed_out or dispatch.exit_code != 0:
        return False
    if dispatch.owned_paths_changed is not False:
        return False
    return not stdout_has_work_evidence(dispatch.stdout)


def parse_oracle_command(command: str) -> list[str]:
    """Parse one oracle command into argv without invoking a shell.

    Args:
        command: The command supplied by ``--oracle``.

    Returns:
        The non-empty argv produced by :func:`shlex.split`.

    Raises:
        RunDispatchError: If the command is empty or has malformed shell
            quoting.  The command is only parsed; shell operators are never
            interpreted.
    """
    if not isinstance(command, str) or not command.strip():
        raise RunDispatchError("oracle command must not be empty")
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise RunDispatchError(
            f"oracle command is malformed: {exc}", cause=exc
        ) from exc
    if not argv:
        raise RunDispatchError("oracle command must not be empty")
    return argv


def oracle_timeout_seconds(
    declared_timeout_seconds: float | None,
    worker_duration_seconds: float,
) -> float:
    """Return the oracle's remaining wall-clock budget.

    A declared ``--timeout`` is a shared worker-plus-oracle budget.  Without
    one, the dispatch boundary's standard ceiling is used as the total
    bounded budget.  An exhausted worker budget becomes an immediate oracle
    timeout rather than an unbounded subprocess.
    """
    total = (
        DEFAULT_ORACLE_TIMEOUT_SECONDS
        if declared_timeout_seconds is None
        else float(declared_timeout_seconds)
    )
    return max(0.0, total - max(0.0, float(worker_duration_seconds)))


def _set_nonblocking(stream: Any) -> None:
    """Best-effort nonblocking setup for a captured subprocess stream."""
    try:
        os.set_blocking(stream.fileno(), False)
    except (AttributeError, OSError, ValueError):
        # Small fakes often expose only ``read``; their read method is already
        # nonblocking from the test boundary's perspective.
        pass


def _drain_stream(stream: Any) -> bytes:
    """Drain currently available bytes from a real or fake pipe."""
    if stream is None:
        return b""
    chunks: list[bytes] = []
    while True:
        try:
            chunk = stream.read(65536)
        except (BlockingIOError, OSError):
            break
        if not chunk:
            break
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", errors="replace")
        if not isinstance(chunk, bytes):
            chunk = bytes(chunk)
        chunks.append(chunk)
    return b"".join(chunks)


def run_oracle(
    argv: list[str] | tuple[str, ...] | str,
    *,
    workdir: str | Path | None = None,
    timeout_seconds: float | None = DEFAULT_ORACLE_TIMEOUT_SECONDS,
    popen: Callable[..., Any] | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> OracleOutcome:
    """Run one parsed oracle command through the safe subprocess boundary.

    The command is always passed as an argv sequence with ``shell=False``.
    stdout and stderr are drained without waiting on either pipe, and the
    process is killed with exit code ``124`` at the injected wall-clock
    deadline. Launch failures are returned as failed oracle evidence instead
    of being retried or promoted to a worker failure, and a post-kill
    ``wait`` that still raises ``subprocess.TimeoutExpired`` is contained:
    the timed-out oracle evidence is preserved and the exception never
    escapes past a completed worker.

    Args:
        argv: Parsed oracle argv, or a command string parsed here for direct
            callers.
        workdir: Child working directory, when supplied.
        timeout_seconds: Remaining wall-clock budget. ``None`` selects the
            standard bounded default; zero is an immediate timeout budget.
        popen: Injectable process spawner.
        clock: Injectable monotonic clock.
        sleep: Injectable polling sleep.
        poll_seconds: Maximum polling interval.

    Returns:
        One :class:`OracleOutcome` containing all oracle evidence.

    Raises:
        RunDispatchError: If ``workdir`` is not a directory or the argv is
            empty. A child launch error itself is represented in the result.
    """
    oracle_argv = parse_oracle_command(argv) if isinstance(argv, str) else list(argv)
    if not oracle_argv:
        raise RunDispatchError("oracle command must not be empty")
    if workdir is not None and not Path(workdir).is_dir():
        raise RunDispatchError(f"workdir is not a directory: {Path(workdir)}")

    popen_fn = popen if popen is not None else _DEFAULT_POPEN
    clock_fn = clock if clock is not None else _DEFAULT_CLOCK
    sleep_fn = sleep if sleep is not None else _DEFAULT_SLEEP
    if timeout_seconds is None:
        budget = DEFAULT_ORACLE_TIMEOUT_SECONDS
    else:
        budget = max(0.0, float(timeout_seconds))
    cwd = str(Path(workdir)) if workdir is not None else None
    started = clock_fn()

    try:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
        }
        if cwd is not None:
            kwargs["cwd"] = cwd
        process = popen_fn(oracle_argv, **kwargs)
    except Exception as exc:
        duration = max(0.0, clock_fn() - started)
        return OracleOutcome(
            argv=tuple(oracle_argv),
            exit_code=None,
            stdout="",
            stderr="",
            duration_seconds=duration,
            timed_out=False,
            error=f"launch failure: {type(exc).__name__}: {exc}",
        )

    _set_nonblocking(getattr(process, "stdout", None))
    _set_nonblocking(getattr(process, "stderr", None))
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    poll_delay = max(0.001, float(poll_seconds))

    while True:
        stdout_buf.extend(_drain_stream(getattr(process, "stdout", None)))
        stderr_buf.extend(_drain_stream(getattr(process, "stderr", None)))
        exit_code = process.poll()
        if exit_code is not None:
            # Capture bytes emitted at the exact exit boundary.
            stdout_buf.extend(_drain_stream(getattr(process, "stdout", None)))
            stderr_buf.extend(_drain_stream(getattr(process, "stderr", None)))
            duration = max(0.0, clock_fn() - started)
            return OracleOutcome(
                argv=tuple(oracle_argv),
                exit_code=exit_code,
                stdout=bytes(stdout_buf).decode("utf-8", errors="replace"),
                stderr=bytes(stderr_buf).decode("utf-8", errors="replace"),
                duration_seconds=duration,
                timed_out=False,
            )

        elapsed = max(0.0, clock_fn() - started)
        if elapsed >= budget:
            try:
                process.kill()
            except Exception:
                pass
            # After the kill, ``wait(timeout)`` may still raise
            # ``subprocess.TimeoutExpired`` when the dead worker's own
            # cleanup outlives the oracle deadline (Astra final re-review
            # finding 3). That must never escape past the completed
            # worker: the timeout oracle evidence is preserved and the
            # attempt still appends exactly one record.
            try:
                process.wait(poll_delay)
            except subprocess.TimeoutExpired:
                pass
            except (TypeError, OSError):
                try:
                    process.wait()
                except Exception:
                    pass
            stdout_buf.extend(_drain_stream(getattr(process, "stdout", None)))
            stderr_buf.extend(_drain_stream(getattr(process, "stderr", None)))
            duration = max(0.0, clock_fn() - started)
            return OracleOutcome(
                argv=tuple(oracle_argv),
                exit_code=_TIMEOUT_EXIT_CODE,
                stdout=bytes(stdout_buf).decode("utf-8", errors="replace"),
                stderr=bytes(stderr_buf).decode("utf-8", errors="replace"),
                duration_seconds=duration,
                timed_out=True,
            )

        sleep_for = min(poll_delay, budget - elapsed)
        sleep_fn(max(0.0, sleep_for))


def _oracle_verdict(oracle: OracleOutcome | None) -> str:
    """Map optional oracle evidence to the canonical verification verdict."""
    if oracle is None:
        return "unverified"
    if oracle.exit_code == 0 and not oracle.timed_out and oracle.error is None:
        return "pass"
    return "fail"


def verification_record(oracle: OracleOutcome | None) -> dict[str, Any]:
    """Build only the verification evidence added to the P1-5a result."""
    verdict = _oracle_verdict(oracle)
    if oracle is None:
        return {
            "oracle_type": "none",
            "verdict": verdict,
            "oracle": None,
        }
    return {
        "oracle_type": "command",
        "verdict": verdict,
        "oracle": {
            "argv": list(oracle.argv),
            "exit_code": oracle.exit_code,
            "stdout": oracle.stdout,
            "stderr": oracle.stderr,
            "duration_seconds": oracle.duration_seconds,
            "timed_out": oracle.timed_out,
            "error": oracle.error,
        },
    }


def run_json_record(
    outcome: SelectionOutcome,
    dispatch: DispatchOutcome,
    oracle: OracleOutcome | None = None,
) -> dict[str, Any]:
    """The ``run --json`` result with selection, dispatch, and verification.

    This remains a P1-5a result rather than an attempt record: P1-5b1 adds
    only the optional oracle evidence and canonical verdict. Cost, attempt
    identifiers, provenance, validation, and ledger writes remain outside
    this packet.
    """
    return {
        "schema_version": 2,
        "record_kind": "router_run",
        "route": {
            "model": outcome.route.model,
            "effort": outcome.route.effort,
            "harness": outcome.route.harness,
            "channel": outcome.route.channel,
        },
        "selection": selection_record(outcome),
        "usage": dispatch.usage,
        "verification": verification_record(oracle),
        "wall_clock_ms": max(0, round(dispatch.duration_seconds * 1000.0)),
        "dispatch": {
            "argv": list(dispatch.argv),
            "exit_code": dispatch.exit_code,
            "timed_out": dispatch.timed_out,
            "duration_seconds": dispatch.duration_seconds,
            "stdout": dispatch.stdout,
            "stderr": dispatch.stderr,
        },
    }


def _plain_counter_text(value: object) -> object:
    """Render one usage counter for the plain-text summary, huge-int safe.

    ``str(huge_int)`` trips CPython's default int-to-decimal conversion
    ceiling, which would crash the summary after the attempt already
    persisted. Normal counters render exactly as ``str`` always did; only the
    bounded converter's digits differ, and only above the ceiling.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return int_to_decimal(value)
    return value


def run_summary_lines(
    outcome: SelectionOutcome,
    dispatch: DispatchOutcome,
    oracle: OracleOutcome | None = None,
    *,
    record: Mapping[str, Any] | None = None,
) -> list[str]:
    """The compact plain-text ``run`` summary (one fact per line).

    Mirrors the v2 record when ``record`` is supplied: selected route and
    basis, the dispatch exit and wall clock, the authoritative usage basis
    with its reported counters (``-`` when unknown), cost, and oracle verdict.
    Never prints the packet prompt or the full captured stdout/stderr.
    """
    route = outcome.route
    model = route.model if route.effort is None else f"{route.model}/{route.effort}"
    lines = [
        f"route: {route.route_id} ({route.harness} {model} via {route.channel})",
        f"selection: {outcome.basis} — {outcome.reason}",
        f"dispatch: exit {dispatch.exit_code}"
        + (" (ceiling timeout)" if dispatch.timed_out else "")
        + f", wall {dispatch.duration_seconds:.1f}s",
    ]
    if no_work_evidence(dispatch, oracle):
        lines.append(
            "progress: no effective work (exit 0, no oracle, no owned-path "
            "change, no result/answer); governed failure_class=platform_env"
        )
    usage = dispatch.usage
    if usage.get("basis") == "unavailable":
        lines.append(f"usage: unavailable ({usage.get('unavailable_reason')})")
    else:
        counters = " ".join(
            f"{name}={_plain_counter_text(usage.get(field))}"
            for field, name in (
                ("input_tokens", "in"),
                ("output_tokens", "out"),
                ("cached_input_tokens", "cached"),
                ("reasoning_tokens", "reasoning"),
                ("total_tokens", "total"),
            )
        )
        lines.append(f"usage: {usage.get('basis')} ({usage.get('source')}) {counters}")

    if record is not None:
        cost = record.get("cost", {})
        if cost.get("basis") == ["list", "marginal"]:
            lines.append(
                "cost: list ${:.8f}, marginal ${:.8f}".format(
                    cost["usd_list"], cost["usd_marginal"]
                )
            )
        else:
            lines.append("cost: unavailable")
        lines.append(f"attempt: {record.get('attempt_id', '-')}")

    verdict = _oracle_verdict(oracle)
    if oracle is None:
        lines.append("verification: unverified (no oracle)")
    elif oracle.error is not None:
        lines.append(f"verification: {verdict} ({oracle.error})")
    elif oracle.timed_out:
        lines.append("verification: fail (oracle ceiling timeout)")
    else:
        lines.append(f"verification: {verdict} (oracle exit {oracle.exit_code})")
    return lines
