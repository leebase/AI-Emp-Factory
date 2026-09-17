"""Pure benchmark evidence, proposal, and HTML projection primitives.

This module reads the optional ``benchmark.staffing-evidence/2`` sidecar,
maps a resolved crew worker onto its explicit sidecar identity, selects textual
swap proposals, and renders a self-contained crew page.  The identity mapping
is explicit: no display name or model-name heuristic is used to identify a
worker.  Rendering and writing remain pure with respect to the loaded inputs;
the writer only replaces the caller's selected output path.
"""

from __future__ import annotations

import html
import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .availability import AvailabilitySnapshot
from .crews import (
    CrewsConfig,
    CrewsConfigError,
    ResolvedWorker,
    Worker,
    is_role_scoped,
    resolve_worker,
    role_class,
)

BENCHMARK_SCHEMA = "benchmark.staffing-evidence/2"
"""The only benchmark sidecar schema accepted by this module."""

BENCHMARK_SCHEMA_VERSION = BENCHMARK_SCHEMA
"""Descriptive alias for the accepted benchmark schema version."""

# The sidecar uses its own short harness labels in worker_key.  These are
# explicit aliases for router providers, not guesses derived from display
# names, binaries, or model names.  ``pi`` is an explicit alternate harness
# for an OpenAI/Codex identity and must be supplied by the caller.
HARNESS_BY_PROVIDER: dict[str, str] = {
    "codex_cli": "codex",
    "openai": "codex",
    "openai/codex": "codex",
    "codex": "codex",
    "claude_code_cli": "claude",
    "claude_code": "claude",
    "anthropic/claude": "claude",
    "claude": "claude",
    "antigravity_cli": "agy",
    "antigravity": "agy",
    "google/agy": "agy",
    "agy": "agy",
    "opencode_cli": "opencode",
    "opencode": "opencode",
    "pi_cli": "pi",
    "omp_cli": "omp",
    "omp": "omp",
}
"""Explicit router-provider to benchmark-harness identity mapping."""

ALTERNATE_HARNESSES: frozenset[str] = frozenset(
    {"codex", "pi", "claude", "agy", "opencode", "omp"}
)
"""Harness labels that are valid in a sidecar worker key."""

# Benchmark roles are not display labels.  They are the sidecar's canonical
# role vocabulary, explicitly assigned to resolver roles.  A comparison also
# requires the two rows to carry the same role and task key.
BENCHMARK_ROLES_BY_RESOLVER_ROLE: dict[str, frozenset[str]] = {
    "author": frozenset({"coder", "implementation"}),
    "envision": frozenset({"planner"}),
    "ideate": frozenset({"planner"}),
    "reconsider": frozenset({"code-review", "reviewer"}),
    "score": frozenset({"code-review", "reviewer"}),
}
"""Explicit bridge from resolver roles to benchmark role vocabulary."""


class BenchmarkEvidenceError(CrewsConfigError):
    """Raised when a benchmark sidecar is present but cannot be trusted."""


CrewPageConfigError = BenchmarkEvidenceError
"""Compatibility alias for the crew-page configuration error type."""


@dataclass(frozen=True)
class WorkerIdentity:
    """The provider/model/effort/harness identity used by the sidecar.

    Attributes:
        provider: Router provider name, such as ``codex_cli``.
        model: Exact model identifier.
        effort: Exact effort value; ``None`` means the sidecar's empty effort.
        harness: Explicit sidecar harness label, such as ``codex`` or ``pi``.
    """

    provider: str
    model: str
    effort: str | None
    harness: str

    @property
    def worker_key(self) -> str:
        """Return the deterministic sidecar worker key for this identity."""
        effort = "" if self.effort is None else self.effort
        return f"{self.model}|{self.harness}|{effort}"


def _canonical_provider(provider: str) -> str | None:
    """Return the router provider spelling for a known provider alias."""
    if provider in {"codex", "codex_cli", "openai", "openai/codex"}:
        return "codex_cli"
    if provider in {"claude", "claude_code", "claude_code_cli", "anthropic/claude"}:
        return "claude_code_cli"
    if provider in {"agy", "antigravity", "antigravity_cli", "google/agy"}:
        return "antigravity_cli"
    if provider in {"opencode", "opencode_cli"}:
        return "opencode_cli"
    if provider in {"omp", "omp_cli"}:
        return "omp_cli"
    return None


_PROVIDER_LABELS: dict[str, str] = {
    "codex_cli": "OpenAI",
    "claude_code_cli": "Anthropic",
    "antigravity_cli": "Google",
    "opencode_cli": "OpenCode",
    "omp_cli": "OMP",
}
"""Fixed human-facing labels for known router provider identities."""


def _provider_label(provider: str) -> str:
    """Return a fixed human label without exposing provider internals."""
    canonical_provider = _canonical_provider(provider)
    if canonical_provider is None:
        return "unknown"
    return _PROVIDER_LABELS.get(canonical_provider, "unknown")


def _sanitize_public_prose(value: str) -> str:
    """Replace known provider tokens in untrusted human-facing prose."""
    sanitized = value
    for provider, label in _PROVIDER_LABELS.items():
        sanitized = sanitized.replace(provider, label)
    return sanitized


def worker_key_for_identity(
    provider: str,
    model: str,
    effort: str | None = None,
    harness: str | None = None,
) -> str | None:
    """Return a sidecar key from explicit provider/model/harness fields.

    Unknown provider or harness combinations return ``None`` so callers cannot
    fall back to a display-name match.
    """
    canonical_provider = _canonical_provider(provider)
    selected_harness = _canonical_harness(provider, harness)
    if canonical_provider is None or selected_harness is None:
        return None
    return worker_key_for(
        WorkerIdentity(canonical_provider, model, effort, selected_harness)
    )


def worker_key_for_worker(
    worker: Worker | ResolvedWorker, *, harness: str | None = None
) -> str | None:
    """Return the explicit sidecar key for a crews worker or resolved worker."""
    try:
        resolved = (
            worker if isinstance(worker, ResolvedWorker) else resolve_worker(worker)
        )
    except CrewsConfigError:
        return None
    return worker_key_for(resolved, harness=harness)


def _canonical_harness(provider: str, harness: str | None) -> str | None:
    """Resolve an explicit or provider-default harness without heuristics."""
    if harness is not None:
        return harness if harness in ALTERNATE_HARNESSES else None
    return HARNESS_BY_PROVIDER.get(provider)


def worker_key_for(
    identity: WorkerIdentity | ResolvedWorker | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    harness: str | None = None,
) -> str | None:
    """Map a resolved identity to a sidecar ``worker_key``.

    Args:
        identity: A :class:`WorkerIdentity` or a resolved crews worker.
        harness: Explicit harness override, required for alternate routes such
            as OpenAI/Pi.  A resolved worker otherwise uses the provider's
            explicit default mapping.

    Returns:
        The deterministic sidecar key, or ``None`` for an unknown provider or
        harness.  Display names are not consulted.
    """
    if identity is None:
        if provider is None or model is None:
            return None
        return worker_key_for_identity(provider, model, effort, harness)
    if isinstance(identity, ResolvedWorker):
        source_provider = identity.provider
        source_model = identity.model
        source_effort = identity.effort
        selected_harness = _canonical_harness(source_provider, harness)
    elif isinstance(identity, WorkerIdentity):
        source_provider = identity.provider
        source_model = identity.model
        source_effort = identity.effort
        selected_harness = _canonical_harness(
            source_provider, harness or identity.harness
        )
    else:
        return None
    canonical_provider = _canonical_provider(source_provider)
    model = source_model
    effort = source_effort
    if canonical_provider is None or selected_harness is None:
        return None
    if not isinstance(model, str) or not model.strip():
        return None
    if effort is not None and (not isinstance(effort, str) or "|" in effort):
        return None
    return WorkerIdentity(
        provider=canonical_provider,
        model=model,
        effort=effort,
        harness=selected_harness,
    ).worker_key


def identity_for_worker(
    worker: Worker | ResolvedWorker,
    *,
    harness: str | None = None,
) -> WorkerIdentity | None:
    """Resolve a crews worker and return its explicit benchmark identity.

    Args:
        worker: A crews :class:`Worker` or already resolved worker.
        harness: Optional explicit alternate harness, such as ``pi``.

    Returns:
        The mapped identity, or ``None`` when the worker cannot be mapped.
    """
    try:
        resolved = (
            worker if isinstance(worker, ResolvedWorker) else resolve_worker(worker)
        )
    except CrewsConfigError:
        return None
    key = worker_key_for(resolved, harness=harness)
    if key is None:
        return None
    selected_harness = _canonical_harness(resolved.provider, harness)
    if selected_harness is None:
        return None
    return WorkerIdentity(
        provider=_canonical_provider(resolved.provider) or resolved.provider,
        model=resolved.model,
        effort=resolved.effort,
        harness=selected_harness,
    )


@dataclass(frozen=True)
class EvidenceRow:
    """Validated, relevant fields from one benchmark sidecar row."""

    worker_key: str
    role: str
    task_key: str
    acceptance: str | None
    accepted_count: int
    score: float | None
    cost_low_usd: float | None
    cost_high_usd: float | None
    run_count: int
    run_ids: tuple[str, ...]
    worker: WorkerIdentity

    @property
    def accepted(self) -> bool:
        """Return whether this row contains at least one accepted run."""
        return self.accepted_count > 0


@dataclass(frozen=True)
class BenchmarkEvidence:
    """Loaded benchmark evidence, including an explicit absent state.

    ``present=False`` is the normal result for a missing optional path; it is
    not an error and is suitable for a later renderer to display as no
    evidence.  A present file always has validated rows.
    """

    present: bool
    rows: tuple[EvidenceRow, ...] = ()
    path: Path | None = None
    schema_version: str | None = None

    @property
    def available(self) -> bool:
        """Return whether a valid benchmark sidecar was present."""
        return self.present

    @property
    def absent(self) -> bool:
        """Return whether the optional benchmark sidecar was absent."""
        return not self.present


@dataclass(frozen=True)
class WorkerEvidenceMetrics:
    """Aggregate evidence metrics for one mapped worker.

    Unknown measurements are ``None``.  In particular, no matching worker is
    not represented as zero runs or zero tasks.
    """

    worker_key: str | None
    best_score: float | None
    cost_to_accept: float | None
    cost_to_accept_low_usd: float | None
    cost_to_accept_high_usd: float | None
    total_run_count: int | None
    task_count: int | None
    one_task: bool | None
    run_ids: tuple[str, ...]
    accepted_run_count: int | None

    @property
    def total_runs(self) -> int | None:
        """Return the aggregate run count."""
        return self.total_run_count

    @property
    def distinct_task_count(self) -> int | None:
        """Return the number of distinct benchmark task keys."""
        return self.task_count

    @property
    def all_run_ids(self) -> tuple[str, ...]:
        """Return all run ids for a later evidence appendix."""
        return self.run_ids

    @property
    def cost_to_accept_usd(self) -> float | None:
        """Return the scalar low estimate for cost-to-accept."""
        return self.cost_to_accept

    @property
    def best_score_100(self) -> float | None:
        """Return the best benchmark score on its 100-point scale."""
        return self.best_score

    @property
    def accepted(self) -> bool | None:
        """Return whether at least one accepted run is evidenced."""
        if self.accepted_run_count is None:
            return None
        return self.accepted_run_count > 0


def _unknown_metrics(worker_key: str | None = None) -> WorkerEvidenceMetrics:
    """Return metrics whose measurements are all explicitly unknown."""
    return WorkerEvidenceMetrics(
        worker_key=worker_key,
        best_score=None,
        cost_to_accept=None,
        cost_to_accept_low_usd=None,
        cost_to_accept_high_usd=None,
        total_run_count=None,
        task_count=None,
        one_task=None,
        run_ids=(),
        accepted_run_count=None,
    )


def _coerce_worker_key(
    worker: WorkerIdentity | ResolvedWorker | Worker | str,
) -> str | None:
    """Return a worker key from supported matching inputs."""
    if isinstance(worker, str):
        return worker if worker else None
    if isinstance(worker, WorkerIdentity):
        return worker_key_for(worker)
    if isinstance(worker, ResolvedWorker):
        return worker_key_for(worker)
    if isinstance(worker, Worker):
        resolved_identity = identity_for_worker(worker)
        return worker_key_for(resolved_identity) if resolved_identity else None
    return None


def match_worker_evidence(
    evidence: BenchmarkEvidence | WorkerIdentity | ResolvedWorker | Worker,
    worker: BenchmarkEvidence | WorkerIdentity | ResolvedWorker | Worker | str,
) -> tuple[EvidenceRow, ...]:
    """Return rows matching a worker only through its deterministic key.

    Both ``match_worker_evidence(evidence, worker)`` and the reversed argument
    order are accepted to keep this small pure primitive convenient to callers.
    """
    if isinstance(evidence, BenchmarkEvidence):
        loaded = evidence
        target = worker
    elif isinstance(worker, BenchmarkEvidence):
        loaded = worker
        target = evidence
    else:
        return ()
    key = _coerce_worker_key(target)  # type: ignore[arg-type]
    if key is None:
        return ()
    return tuple(row for row in loaded.rows if row.worker_key == key)


def aggregate_worker_metrics(
    evidence: BenchmarkEvidence | WorkerIdentity | ResolvedWorker | Worker,
    worker: BenchmarkEvidence | WorkerIdentity | ResolvedWorker | Worker | str,
) -> WorkerEvidenceMetrics:
    """Aggregate benchmark rows for one worker.

    Args:
        evidence: Loaded evidence, in either argument position.
        worker: A worker identity, resolved worker, crews worker, or exact key.

    Returns:
        Best non-null score, accepted-only cost, run/task counts, one-task
        status, accepted-run count, and all run ids.  Missing values remain
        unknown instead of becoming zero.
    """
    if isinstance(evidence, BenchmarkEvidence):
        loaded = evidence
        target = worker
    elif isinstance(worker, BenchmarkEvidence):
        loaded = worker
        target = evidence
    else:
        return _unknown_metrics()
    key = _coerce_worker_key(target)  # type: ignore[arg-type]
    rows = match_worker_evidence(loaded, target) if key is not None else ()
    if not rows:
        return _unknown_metrics(key)

    scores = [row.score for row in rows if row.score is not None]
    accepted_representatives = [row for row in rows if row.acceptance == "accepted"]
    low_costs = [
        row.cost_low_usd
        for row in accepted_representatives
        if row.cost_low_usd is not None
    ]
    high_costs = [
        row.cost_high_usd
        for row in accepted_representatives
        if row.cost_high_usd is not None
    ]
    low_cost = min(low_costs) if low_costs else None
    high_cost = min(high_costs) if high_costs else None
    # The low estimate is the stable scalar used by existing staffing
    # summaries.  The high range is retained beside it for later rendering.
    scalar_cost = low_cost
    tasks = {row.task_key for row in rows}
    run_ids = tuple(sorted({run_id for row in rows for run_id in row.run_ids}))
    return WorkerEvidenceMetrics(
        worker_key=key,
        best_score=max(scores) if scores else None,
        cost_to_accept=scalar_cost,
        cost_to_accept_low_usd=low_cost,
        cost_to_accept_high_usd=high_cost,
        total_run_count=sum(row.run_count for row in rows),
        task_count=len(tasks),
        one_task=len(tasks) == 1,
        run_ids=run_ids,
        accepted_run_count=sum(row.accepted_count for row in rows),
    )


# Friendly aliases for integrations that name the operation after its result.
load_benchmark_evidence: Any
metrics_for_worker: Any
worker_evidence_metrics: Any


def _invalid(path: Path, detail: str) -> BenchmarkEvidenceError:
    """Create a concise typed configuration error."""
    return BenchmarkEvidenceError(f"{path}: invalid benchmark evidence: {detail}")


def _number(
    value: Any,
    *,
    path: Path,
    field: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    """Validate an optional finite numeric field, including numeric strings."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise _invalid(path, f"row field {field!r} must be a number or null")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise _invalid(path, f"row field {field!r} must be a number or null") from exc
    if not math.isfinite(number):
        raise _invalid(path, f"row field {field!r} must be finite")
    if minimum is not None and number < minimum:
        raise _invalid(path, f"row field {field!r} is out of range")
    if maximum is not None and number > maximum:
        raise _invalid(path, f"row field {field!r} is out of range")
    return number


def _required_string(raw: Mapping[str, Any], field: str, path: Path) -> str:
    """Validate and return a required non-empty string field."""
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(path, f"row field {field!r} must be a non-empty string")
    return value.strip()


def _parse_row(raw: Any, path: Path) -> EvidenceRow:
    """Validate one sidecar row and retain only core evidence fields."""
    if not isinstance(raw, dict):
        raise _invalid(path, "every row must be an object")
    worker_key = _required_string(raw, "worker_key", path)
    role = _required_string(raw, "role", path)
    task_key = _required_string(raw, "task_key", path)

    acceptance = raw.get("acceptance")
    if acceptance is not None and (
        not isinstance(acceptance, str)
        or acceptance not in {"accepted", "not_accepted"}
    ):
        raise _invalid(
            path, "row field 'acceptance' must be accepted, not_accepted, or null"
        )

    accepted_count = raw.get("accepted_count")
    if isinstance(accepted_count, bool) or not isinstance(accepted_count, int):
        raise _invalid(
            path, "row field 'accepted_count' must be a non-negative integer"
        )
    if accepted_count < 0:
        raise _invalid(
            path, "row field 'accepted_count' must be a non-negative integer"
        )

    run_count = raw.get("run_count")
    if isinstance(run_count, bool) or not isinstance(run_count, int) or run_count < 0:
        raise _invalid(path, "row field 'run_count' must be a non-negative integer")
    raw_run_ids = raw.get("run_ids")
    if not isinstance(raw_run_ids, list) or any(
        not isinstance(run_id, str) or not run_id.strip() for run_id in raw_run_ids
    ):
        raise _invalid(path, "row field 'run_ids' must be a list of non-empty strings")
    run_ids = tuple(run_id.strip() for run_id in raw_run_ids)
    if run_count != len(run_ids):
        raise _invalid(path, "row field 'run_count' must equal the run_ids length")
    if accepted_count > run_count:
        raise _invalid(path, "row field 'accepted_count' cannot exceed run_count")
    raw_worker = raw.get("worker")
    if not isinstance(raw_worker, dict):
        raise _invalid(path, "row field 'worker' must be an object")
    provider = raw_worker.get("provider")
    model = raw_worker.get("model")
    harness = raw_worker.get("harness")
    effort = raw_worker.get("effort")
    if not isinstance(provider, str) or not provider.strip():
        raise _invalid(path, "worker.provider must be a non-empty string")
    if not isinstance(model, str) or not model.strip():
        raise _invalid(path, "worker.model must be a non-empty string")
    if not isinstance(harness, str) or harness not in ALTERNATE_HARNESSES:
        raise _invalid(path, "worker.harness is not a recognised harness")
    if effort is None:
        normalized_effort: str | None = None
    elif isinstance(effort, str):
        normalized_effort = effort or None
    else:
        raise _invalid(path, "worker.effort must be a string or null")
    worker_identity = WorkerIdentity(
        provider=provider.strip(),
        model=model.strip(),
        effort=normalized_effort,
        harness=harness,
    )
    if worker_identity.worker_key != worker_key:
        raise _invalid(path, "worker_key does not match worker model/harness/effort")

    return EvidenceRow(
        worker_key=worker_key,
        role=role,
        task_key=task_key,
        acceptance=acceptance,
        accepted_count=accepted_count,
        score=_number(
            raw.get("score_100"),
            path=path,
            field="score_100",
            minimum=0,
            maximum=100,
        ),
        cost_low_usd=_number(
            raw.get("cost_low_usd"),
            path=path,
            field="cost_low_usd",
            minimum=0,
        ),
        cost_high_usd=_number(
            raw.get("cost_high_usd"),
            path=path,
            field="cost_high_usd",
            minimum=0,
        ),
        run_count=run_count,
        run_ids=run_ids,
        worker=worker_identity,
    )


def load_benchmark(path: str | Path | None = None) -> BenchmarkEvidence:
    """Load the optional benchmark sidecar without accepting malformed data.

    Args:
        path: Sidecar path, or ``None`` when no sidecar was configured.

    Returns:
        A present, validated :class:`BenchmarkEvidence`, or an explicit absent
        state when ``path`` is missing/``None``.

    Raises:
        BenchmarkEvidenceError: If a present file has invalid JSON, the wrong
            schema, or malformed required row shapes.
    """
    if path is None:
        return BenchmarkEvidence(present=False)
    resolved = Path(path).expanduser()
    if not resolved.exists():
        return BenchmarkEvidence(present=False, path=resolved)
    if not resolved.is_file():
        raise BenchmarkEvidenceError(
            f"{resolved}: invalid benchmark evidence: not a file"
        )
    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _invalid(resolved, f"cannot read file: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _invalid(resolved, "malformed JSON") from exc
    if not isinstance(raw, dict):
        raise _invalid(resolved, "top level must be an object")
    if raw.get("schema_version") != BENCHMARK_SCHEMA:
        raise _invalid(resolved, f"schema_version must be {BENCHMARK_SCHEMA!r}")
    rows = raw.get("rows")
    if not isinstance(rows, list):
        raise _invalid(resolved, "'rows' must be a list")
    parsed = tuple(_parse_row(row, resolved) for row in rows)
    return BenchmarkEvidence(
        present=True,
        rows=parsed,
        path=resolved,
        schema_version=BENCHMARK_SCHEMA,
    )


load_benchmark_evidence = load_benchmark
load_staffing_evidence = load_benchmark
metrics_for_worker = aggregate_worker_metrics
aggregate_metrics = aggregate_worker_metrics
worker_evidence_metrics = aggregate_worker_metrics


@dataclass(frozen=True)
class Proposal:
    """A text-only candidate swap proven by the benchmark predicates."""

    crew: str
    role: str
    current_worker: str
    candidate_worker: str
    benchmark_role: str
    task_key: str
    current_metrics: WorkerEvidenceMetrics
    candidate_metrics: WorkerEvidenceMetrics

    @property
    def current(self) -> str:
        """Return the current assignment worker id."""
        return self.current_worker

    @property
    def candidate(self) -> str:
        """Return the candidate worker id."""
        return self.candidate_worker

    @property
    def text(self) -> str:
        """Return a renderer-ready proposal sentence."""
        return (
            f"{self.crew} / {self.role}: consider {self.candidate_worker} "
            f"instead of {self.current_worker} for {self.task_key}."
        )

    def __str__(self) -> str:
        """Return the text-only proposal."""
        return self.text


def select_proposals(
    config: CrewsConfig | BenchmarkEvidence,
    evidence: BenchmarkEvidence | CrewsConfig,
) -> tuple[Proposal, ...]:
    """Return no proposals until authoritative boundary evidence is available.

    The crews schema and benchmark sidecar identify workers and report
    measurements, but they do not prove model-tier or vendor-independence
    boundaries.  This Sprint 5 implementation therefore fails closed rather
    than deriving either boundary from provider, harness, channel, or model.
    The proposals section remains available for a future authoritative source.

    Args:
        config: Loaded crews config, in either argument position.
        evidence: Loaded benchmark evidence, in either argument position.

    Returns:
        An empty tuple; no staffing proposal is emitted without authoritative
        boundary evidence.
    """
    del config, evidence
    return ()


select_benchmark_proposals = select_proposals
choose_proposals = select_proposals
proposals_for_crews = select_proposals


_HEADROOM_LABELS: frozenset[str] = frozenset(
    {"healthy", "degraded", "likely_exhausted", "exhausted", "unknown"}
)


_PAGE_CSS = """
:root {
  color-scheme: light dark;
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  line-height: 1.45;
  background: #f7f8fa;
  color: #17202a;
}
* { box-sizing: border-box; }
html, body { max-width: 100%; overflow-x: hidden; }
body { margin: 0; min-width: 0; }
.page { width: min(100%, 1120px); margin: 0 auto; padding: 1rem; }
h1, h2, h3, h4 { line-height: 1.2; overflow-wrap: anywhere; }
h1 { margin: 0; font-size: clamp(1.6rem, 5vw, 2.4rem); }
h2 { margin: 0; font-size: 1.35rem; }
h3 { margin: 0; font-size: 1.05rem; }
h4 { margin: 0; font-size: 1rem; }
p { margin: .4rem 0; overflow-wrap: anywhere; }
code { overflow-wrap: anywhere; word-break: break-word; }
.intro, .notice, .empty-state, .proposal, .worker-card, .crew-card {
  border: 1px solid #d6dbe3;
  border-radius: .65rem;
  background: #ffffff;
}
.intro { padding: 1rem; margin-bottom: 1rem; }
.notice { padding: .75rem 1rem; margin: 1rem 0; }
.crew-list { display: grid; gap: 1rem; }
.crew-card { padding: 1rem; }
.purpose { color: #4b5563; }
.stage-list { display: grid; gap: .8rem; margin-top: 1rem; }
.stage { min-width: 0; }
.stage h3 { border-bottom: 1px solid #d6dbe3; padding-bottom: .3rem; }
.worker-list { display: grid; gap: .65rem; margin: .65rem 0 0; padding-left: 1.5rem; }
.worker-card { padding: .8rem; min-width: 0; }
.worker-id { margin-bottom: .3rem; }
.identity, .availability, .metrics { color: #374151; }
.availability { display: flex; flex-wrap: wrap; gap: .35rem .8rem; }
.availability span { overflow-wrap: anywhere; }
.headroom { font-weight: 700; }
.headroom.healthy { color: #166534; }
.headroom.degraded, .headroom.likely_exhausted { color: #92400e; }
.headroom.exhausted { color: #991b1b; }
.headroom.unknown { color: #6b7280; }
.badge { display: inline-block; padding: .1rem .4rem; border-radius: .3rem;
  background: #e5e7eb; color: #374151; font-size: .84rem; }
.metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: .4rem .8rem; margin: .65rem 0 0; }
.metrics div { min-width: 0; }
.metrics dt { font-size: .78rem; color: #6b7280; }
.metrics dd { margin: 0; overflow-wrap: anywhere; }
.proposals, .evidence-appendix { margin-top: 1.25rem; }
.proposal-list { display: grid; gap: .6rem; padding: 0; list-style: none; }
.proposal { padding: .75rem 1rem; }
.empty-state { padding: .75rem 1rem; color: #4b5563; }
.evidence-appendix { border-top: 2px solid #d6dbe3; padding-top: 1rem; }
.evidence-rows { display: grid; gap: .7rem; padding: 0; list-style: none; }
.evidence-row { border-left: .25rem solid #9ca3af; padding: .4rem .7rem;
  overflow-wrap: anywhere; }
.evidence-row ul { margin: .3rem 0 0; padding-left: 1.2rem; }
@media (prefers-color-scheme: dark) {
  :root { background: #111827; color: #f3f4f6; }
  .intro, .notice, .empty-state, .proposal, .worker-card, .crew-card {
    border-color: #374151; background: #1f2937;
  }
  .purpose, .identity, .availability, .metrics, .empty-state { color: #d1d5db; }
  .stage h3, .evidence-appendix { border-color: #374151; }
  .metrics dt { color: #9ca3af; }
  .headroom.healthy { color: #86efac; }
  .headroom.degraded, .headroom.likely_exhausted { color: #fcd34d; }
  .headroom.exhausted { color: #fca5a5; }
  .headroom.unknown { color: #d1d5db; }
  .badge { background: #374151; color: #e5e7eb; }
}
@media (max-width: 600px) {
  .page { padding: .7rem; }
  .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 390px) {
  .page { padding: .5rem; }
  .metrics { grid-template-columns: minmax(0, 1fr); }
  .worker-list { padding-left: 1.2rem; }
}
"""


def _html_text(value: object) -> str:
    """Escape a value for use as HTML text or an attribute value."""
    return html.escape(str(value), quote=True)


def _format_number(value: float) -> str:
    """Render a finite metric without an unnecessary decimal suffix."""
    if value.is_integer():
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _format_money(value: float | None) -> str:
    """Render a cost measurement, preserving an explicit unknown state."""
    if value is None:
        return "unknown"
    return f"${value:.2f}"


def _format_cost(metrics: WorkerEvidenceMetrics) -> str:
    """Render the accepted-cost range without inventing unavailable values."""
    low = metrics.cost_to_accept_low_usd
    high = metrics.cost_to_accept_high_usd
    if low is None:
        return "unknown"
    if high is not None and high != low:
        return f"{_format_money(low)}–{_format_money(high)}"
    return _format_money(low)


def _format_task_count(metrics: WorkerEvidenceMetrics) -> str:
    """Render task count, using the required singular wording for one task."""
    if metrics.task_count is None:
        return "unknown"
    if metrics.task_count == 1:
        return "one task"
    return f"{metrics.task_count} tasks"


def _metric_text(value: float | int | None) -> str:
    """Render an optional numeric metric as text."""
    if value is None:
        return "unknown"
    if isinstance(value, float):
        return _format_number(value)
    return str(value)


def _resolved_for_page(config: CrewsConfig, worker_id: str) -> ResolvedWorker | None:
    """Resolve one configured worker, failing closed for page projection."""
    worker = config.workers.get(worker_id)
    if worker is None:
        return None
    try:
        return resolve_worker(worker)
    except CrewsConfigError:
        return None


def _worker_html(
    config: CrewsConfig,
    availability: AvailabilitySnapshot,
    evidence: BenchmarkEvidence,
    stage_name: str,
    worker_id: str,
) -> str:
    """Render one ordered stage roster entry."""
    resolved = _resolved_for_page(config, worker_id)
    if resolved is None:
        headroom_label = "unknown"
        observed_at = "unknown"
        stale = "unknown"
        provider = "unknown"
        model = "unknown"
        effort = "unknown"
        channel = "unknown"
        metrics = aggregate_worker_metrics(evidence, "")
    else:
        headroom = availability.headroom(resolved.channel)
        candidate_label = getattr(headroom.health, "value", str(headroom.health))
        headroom_label = (
            candidate_label if candidate_label in _HEADROOM_LABELS else "unknown"
        )
        observed_at = (
            headroom.observed_at.isoformat()
            if headroom.observed_at is not None
            else "unknown"
        )
        stale = "yes" if headroom.stale else "no"
        provider = _provider_label(resolved.provider)
        model = resolved.model
        effort = resolved.effort if resolved.effort is not None else "unknown"
        channel = resolved.channel
        metrics = aggregate_worker_metrics(evidence, resolved)

    try:
        planning_only = (
            resolved is not None
            and role_class(stage_name) == "coding"
            and is_role_scoped(resolved.model)
        )
    except CrewsConfigError:
        planning_only = False

    role_note = (
        '<span class="badge">planning/review only</span>' if planning_only else ""
    )
    identity_text = (
        f"Provider: <code>{_html_text(provider)}</code> · "
        f"Model: <code>{_html_text(model)}</code> · "
        f"Effort: <code>{_html_text(effort)}</code>"
    )
    headroom_text = _html_text(headroom_label)
    metrics_text = {
        "best": _html_text(_metric_text(metrics.best_score)),
        "cost": _html_text(_format_cost(metrics)),
        "runs": _html_text(_metric_text(metrics.total_run_count)),
        "tasks": _html_text(_format_task_count(metrics)),
    }
    return f"""
<li class="worker-card">
  <div class="worker-id"><h4><code>{_html_text(worker_id)}</code></h4>{role_note}</div>
  <p class="identity">{identity_text}</p>
  <p class="availability">
    <span>Channel: <code>{_html_text(channel)}</code></span>
    <span class="headroom {headroom_text}">Headroom: {headroom_text}</span>
    <span>Observation time: <time>{_html_text(observed_at)}</time></span>
    <span>Stale: {_html_text(stale)}</span>
  </p>
  <dl class="metrics">
    <div><dt>Best score</dt><dd>{metrics_text["best"]}</dd></div>
    <div><dt>Cost-to-accept</dt><dd>{metrics_text["cost"]}</dd></div>
    <div><dt>Run count</dt><dd>{metrics_text["runs"]}</dd></div>
    <div><dt>Task count</dt><dd>{metrics_text["tasks"]}</dd></div>
  </dl>
</li>"""


def _proposal_key(proposal: Proposal) -> tuple[str, ...]:
    """Return the stable P34 ordering key for one proposal."""
    return (
        proposal.crew,
        proposal.role,
        proposal.task_key,
        proposal.benchmark_role,
        proposal.current_worker,
        proposal.candidate_worker,
    )


def _proposals_html(proposals: Iterable[Proposal] | None) -> str:
    """Render deterministic text-only proposals or an explicit empty state."""
    ordered = tuple(sorted(proposals or (), key=_proposal_key))
    if not ordered:
        return '<p class="empty-state">No proposals qualify.</p>'
    items = "".join(
        f'<li class="proposal">{_html_text(proposal.text)}</li>' for proposal in ordered
    )
    return f'<ol class="proposal-list">{items}</ol>'


def _evidence_appendix_html(evidence: BenchmarkEvidence) -> str:
    """Render every supplied run identifier in the final appendix only."""
    rows = sorted(
        evidence.rows,
        key=lambda row: (row.worker_key, row.role, row.task_key, row.run_ids),
    )
    if not rows:
        body = '<p class="empty-state">No run ids recorded.</p>'
    else:
        rendered_rows: list[str] = []
        for row in rows:
            ids = "".join(
                f"<li><code>{_html_text(run_id)}</code></li>" for run_id in row.run_ids
            )
            if not ids:
                ids = "<li>unknown</li>"
            rendered_rows.append(
                f'<li class="evidence-row"><strong>'
                f"{_html_text(row.worker_key)}</strong> · "
                f"{_html_text(row.role)} · {_html_text(row.task_key)}"
                f"<ul>{ids}</ul></li>"
            )
        body = f'<ul class="evidence-rows">{"".join(rendered_rows)}</ul>'
    return f"""
<section class="evidence-appendix" id="evidence-appendix"
         aria-labelledby="evidence-appendix-title">
  <h2 id="evidence-appendix-title">Evidence appendix</h2>
  <p>Run ids appear only in this appendix.</p>
  {body}
</section>"""


def render_crew_page(
    config: CrewsConfig,
    availability: AvailabilitySnapshot,
    evidence: BenchmarkEvidence,
    proposals: Iterable[Proposal] | None = (),
) -> str:
    """Build a self-contained HTML projection from loaded page inputs.

    Args:
        config: Loaded crews configuration. Crew and stage mappings retain their
            declared insertion order.
        availability: Parsed availability snapshot used through its channel
            lookup API.
        evidence: Parsed optional benchmark evidence.
        proposals: Already-selected text-only proposals.

    Returns:
        One HTML document with inline CSS and no external assets. This function
        does not modify any input or perform I/O.
    """
    crew_sections: list[str] = []
    for crew in config.crews.values():
        stage_sections: list[str] = []
        for stage_name, stage in crew.stages.items():
            workers = "".join(
                _worker_html(
                    config,
                    availability,
                    evidence,
                    stage_name,
                    worker_id,
                )
                for worker_id in stage.workers
            )
            stage_sections.append(f"""
<section class="stage">
  <h3>{_html_text(stage_name)}</h3>
  <ol class="worker-list">{workers}</ol>
</section>""")
        purpose = _sanitize_public_prose(crew.description or "unknown")
        crew_sections.append(f"""
<article class="crew-card">
  <h2>{_html_text(crew.name)}</h2>
  <p class="purpose"><strong>Purpose:</strong> {_html_text(purpose)}</p>
  <div class="stage-list">{"".join(stage_sections)}</div>
</article>""")

    benchmark_notice = (
        '<p class="notice">No benchmark evidence yet.</p>'
        if not evidence.present
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crew staffing</title>
<style>{_PAGE_CSS}</style>
</head>
<body>
<main class="page">
  <header class="intro">
    <h1>Crew staffing</h1>
    <p>Current rosters, channel headroom, and benchmark evidence.</p>
    {benchmark_notice}
  </header>
  <section class="crew-list" aria-label="Crews">
    {"".join(crew_sections)}
  </section>
  <section class="proposals" aria-labelledby="proposals-title">
    <h2 id="proposals-title">Proposals</h2>
    {_proposals_html(proposals)}
  </section>
  {_evidence_appendix_html(evidence)}
</main>
</body>
</html>
"""


def write_crew_page(
    path: str | Path,
    config: CrewsConfig,
    availability: AvailabilitySnapshot,
    evidence: BenchmarkEvidence,
    proposals: Iterable[Proposal] | None = (),
) -> Path:
    """Atomically write a rendered page to the caller-selected path.

    The selected path's parent must already exist. No live input path is read
    or written by this operation.

    Args:
        path: Output HTML path selected by the caller.
        config: Loaded crews configuration.
        availability: Parsed availability snapshot.
        evidence: Parsed optional benchmark evidence.
        proposals: Already-selected text-only proposals.

    Returns:
        The expanded output path.
    """
    target = Path(path).expanduser()
    rendered = render_crew_page(config, availability, evidence, proposals)
    temp_path: str | None = None
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temp_path = temp_name
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(rendered)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    return target


# Short aliases keep the pure projection convenient to callers that name the
# operation after either its construction or its presentation.
build_crew_page = render_crew_page
render_page = render_crew_page
build_page = render_crew_page
write_page = write_crew_page


__all__ = [
    "ALTERNATE_HARNESSES",
    "BENCHMARK_ROLES_BY_RESOLVER_ROLE",
    "BENCHMARK_SCHEMA",
    "BENCHMARK_SCHEMA_VERSION",
    "BenchmarkEvidence",
    "BenchmarkEvidenceError",
    "CrewPageConfigError",
    "build_crew_page",
    "build_page",
    "EvidenceRow",
    "HARNESS_BY_PROVIDER",
    "Proposal",
    "WorkerEvidenceMetrics",
    "WorkerIdentity",
    "aggregate_metrics",
    "aggregate_worker_metrics",
    "choose_proposals",
    "identity_for_worker",
    "load_benchmark",
    "load_benchmark_evidence",
    "load_staffing_evidence",
    "match_worker_evidence",
    "metrics_for_worker",
    "proposals_for_crews",
    "render_crew_page",
    "render_page",
    "select_benchmark_proposals",
    "select_proposals",
    "worker_evidence_metrics",
    "worker_key_for",
    "worker_key_for_identity",
    "worker_key_for_worker",
    "write_crew_page",
    "write_page",
]
