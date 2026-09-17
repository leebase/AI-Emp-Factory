"""Import historical evidence into the staffing attempt ledger.

The Phase-1 benchmark source is the benchmark v6 staffing-evidence sidecar
(``benchmark.staffing-evidence/2``).  Its ``source_csv`` names and hashes the
per-run CSV, while each sidecar row adds a validated task class and a lossless
``run_usage`` entry for every CSV run.  This module verifies that join before
embedding a raw ``benchmarkV6Run`` payload in the attempt-record v2 shape.

The payload contains only facts the v6 sidecar and its pinned CSV actually
carry: the sidecar's own schema version and digest, its verbatim
``source_csv`` block, the join-verified task key, class block, and four
worker fields, the raw ``usage_*_tokens`` receipt columns, the row's
acceptance and ``elapsed_ms``, and every timestamp column the row supplies.
A worker effort the source records as the empty string is preserved
canonically as null — the attempt-record schema's ``crewWorker.effort``
admits only a non-empty string or null — and the exact raw source
representation is disclosed verbatim in provenance; nothing is substituted.
The v6 sidecar provides neither a crew name nor a crews-file identity, so no
``crew_name`` and no ``crews_file_sha256`` are ever written; the sidecar's
own digest is carried as ``sidecar_sha256`` only.  Records committed before
this raw shape existed embed the legacy crew-run envelope; they stay valid
and readable, and re-running this import is the governed correction: it
appends a deterministic ``benchmark:v6:<run_id>`` record built from the same
verified facts instead of rewriting the append-only ledger.

Benchmark CSV rows do not identify a channel-qualified staffing route, an
oracle command, a supervisor route, or a cost calculated from Phase-1 terms.
Those facts are therefore never inferred.  Imported records are deliberately
not ``verified_success`` records and their canonical cost is unavailable.

Agent-Orch imports use only retained ``run.json`` manifests and each
attempt's sibling ``route-selection.json`` and optional ``usage.json``.
They do not infer class metadata or reinterpret raw attempts as the fuller
Phase-4 observation shape.

No provider or subprocess boundary exists here.  Appends go through
``staffing.ledger.append_attempt``, which validates and performs the existing
single-write ``O_APPEND`` operation.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from lee_llm_router.providers.base import LLMRouterError
from lee_llm_router.staffing.catalog import StaffingCatalogError, validate_class_block
from lee_llm_router.staffing.ledger import (
    AttemptLedgerError,
    append_attempt,
    read_attempts,
    resolve_attempts_path,
    validate_attempt,
    writer_transaction,
)

BENCHMARK_SCHEMA_VERSION = "benchmark.staffing-evidence/2"
BENCHMARK_USAGE_SOURCE = "benchmark v6 CSV usage_*_tokens"
_AGENT_ORCH_USAGE_SOURCE = "agent-orch usage.json"
_AGENT_ORCH_PROVENANCE_SOURCE = "agent-orch-runs"
_AGENT_ORCH_DAYS = 30
_AGENT_ORCH_RECORDED_BY = "lee-llm-router evidence import"
_AGENT_ORCH_AUTHORITY_REF = "docs/staffing/chief-answers-p1-3.md"
TOKEN_COLUMNS = (
    "usage_input_tokens",
    "usage_output_tokens",
    "usage_cached_input_tokens",
    "usage_reasoning_tokens",
    "usage_total_tokens",
)
_ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_ATTEMPT_ID_PREFIX = "benchmark:"
_CORRECTION_PREFIX = "benchmark:v6:"
_LEGACY_CREW_RUN_SCHEMA_VERSION = "benchmark.crew-run/1"
_SOURCE_TIMESTAMP_COLUMNS = (
    "run_created_at",
    "launch_created_at",
    "started_at",
    "finished_at",
    "captured_at",
    "evaluation_created_at",
)
_CLASS_FIELDS = {
    "class_key",
    "role",
    "oracle_type",
    "domain_tags",
    "size_band",
    "language",
}


class EvidenceImportError(LLMRouterError):
    """Raised when evidence import inputs or the destination are invalid."""


class _RowError(ValueError):
    """One malformed or unmappable source item, safe to skip explicitly."""


@dataclass(frozen=True)
class ImportIssue:
    """Reason one source row or attempt was skipped.

    Attributes:
        row_number: One-based source row or attempt ordinal.
        run_id: Source run id when it was readable.
        reason: Explicit malformed/unmappable or duplicate reason.
    """

    row_number: int
    run_id: str | None
    reason: str


@dataclass(frozen=True)
class ImportSummary:
    """Counts and per-item diagnostics from one evidence import."""

    imported: int
    skipped: int
    issues: tuple[ImportIssue, ...]
    ledger_path: Path


@dataclass(frozen=True)
class _RunMetadata:
    """Sidecar facts joined to a single source CSV run."""

    task_key: str
    class_record: Mapping[str, Any]
    worker: Mapping[str, Any]
    usage: Mapping[str, Any]


def _read_json_object(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EvidenceImportError(
            f"cannot read benchmark sidecar {path}: {exc}"
        ) from exc
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceImportError(
            f"benchmark sidecar {path} is not valid UTF-8 JSON: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise EvidenceImportError(f"benchmark sidecar {path} is not a JSON object")
    return value, raw


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _RowError(f"{label} is missing or empty")
    return value.strip()


def _aware_timestamp(value: Any, label: str) -> str:
    text = _required_text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _RowError(f"{label} is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _RowError(f"{label} is not timezone-aware")
    return text


def _optional_counter(value: Any, label: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise _RowError(f"{label} is not a nonnegative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise _RowError(f"{label} is not a nonnegative integer") from exc
    if str(parsed) != str(value).strip() or parsed < 0:
        raise _RowError(f"{label} is not a nonnegative integer")
    return parsed


def _optional_elapsed(value: Any) -> int | None:
    return _optional_counter(value, "elapsed_ms")


def _acceptance(row: Mapping[str, Any]) -> str:
    raw_acceptance = row.get("acceptance")
    raw_accepted = row.get("accepted")
    acceptance: str | None = None
    if isinstance(raw_acceptance, str) and raw_acceptance.strip():
        acceptance = raw_acceptance.strip().lower()
    if isinstance(raw_accepted, str) and raw_accepted.strip():
        value = raw_accepted.strip().lower()
        boolean_map = {
            "true": "accepted",
            "yes": "accepted",
            "1": "accepted",
            "false": "not_accepted",
            "no": "not_accepted",
            "0": "not_accepted",
        }
        if value not in boolean_map:
            raise _RowError("accepted is not a supported boolean")
        from_boolean = boolean_map[value]
        if acceptance is not None and acceptance != from_boolean:
            raise _RowError("acceptance and accepted disagree")
        acceptance = from_boolean
    if acceptance not in {"accepted", "not_accepted", "unverified"}:
        raise _RowError("acceptance is not accepted, not_accepted, or unverified")
    return acceptance


def _verdict(acceptance: str) -> str:
    return {
        "accepted": "pass",
        "not_accepted": "fail",
        "unverified": "unverified",
    }[acceptance]


def _attempt_id(run_id: str) -> str:
    candidate = f"{_ATTEMPT_ID_PREFIX}{run_id}"
    if not _ATTEMPT_ID_RE.fullmatch(candidate):
        raise _RowError("run_id cannot form a valid deterministic attempt id")
    return candidate


def _correction_attempt_id(run_id: str) -> str:
    """Deterministic id of the truthful correction of one legacy record."""
    candidate = f"{_CORRECTION_PREFIX}{run_id}"
    if not _ATTEMPT_ID_RE.fullmatch(candidate):
        raise _RowError("run_id cannot form a valid deterministic correction id")
    return candidate


def _legacy_crew_run_run_id(record: Mapping[str, Any]) -> str | None:
    """Recover the source run id from a pre-repair legacy crew-run record.

    Legacy ids were built exactly as ``benchmark:<run_id>``, so stripping the
    fixed prefix recovers the run id even when the run id itself contains a
    colon.  Anything not shaped that way is not a legacy benchmark import.
    """
    if record.get("record_kind") != "benchmark_run":
        return None
    payload = record.get("benchmark_run")
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != _LEGACY_CREW_RUN_SCHEMA_VERSION
    ):
        return None
    attempt_id = record.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id.startswith(_ATTEMPT_ID_PREFIX):
        return None
    return attempt_id[len(_ATTEMPT_ID_PREFIX) :]


BENCHMARK_CORRECTION_PREFIX = _CORRECTION_PREFIX
"""Attempt-id prefix of the deterministic correction of one legacy record."""


def is_benchmark_v6_record(record: Mapping[str, Any]) -> bool:
    """Whether a benchmark_run record embeds the raw benchmark v6 payload.

    True only for ``record_kind='benchmark_run'`` records whose embedded
    payload uses the current ``benchmark.staffing-evidence/2`` shape. Legacy
    crew-run envelopes and every other record kind return ``False``.
    """
    payload = record.get("benchmark_run")
    return (
        record.get("record_kind") == "benchmark_run"
        and isinstance(payload, Mapping)
        and payload.get("schema_version") == BENCHMARK_SCHEMA_VERSION
    )


def benchmark_source_run_id(record: Mapping[str, Any]) -> str | None:
    """Return the source run id a benchmark_run record describes, else None.

    The raw v6 shape carries ``run_id`` in its payload; the pre-repair legacy
    crew-run shape encoded it as the ``benchmark:<run_id>`` attempt id, so
    :func:`_legacy_crew_run_run_id` recovers it. Every other record kind or
    an unrecognized shape returns ``None`` — no id is ever inferred.
    """
    if record.get("record_kind") != "benchmark_run":
        return None
    payload = record.get("benchmark_run")
    if not isinstance(payload, Mapping):
        return None
    if payload.get("schema_version") == BENCHMARK_SCHEMA_VERSION:
        run_id = payload.get("run_id")
        return run_id if isinstance(run_id, str) and run_id else None
    return _legacy_crew_run_run_id(record)


def _source_csv_path(
    sidecar_path: Path,
    source_ref: str,
    explicit: str | Path | None,
) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    referenced = Path(source_ref).expanduser()
    if referenced.is_absolute():
        return referenced
    for parent in (sidecar_path.parent, *sidecar_path.parents):
        candidate = parent / referenced
        if candidate.is_file():
            return candidate
    raise EvidenceImportError(
        f"cannot resolve source_csv.path {source_ref!r} from {sidecar_path}; "
        "supply --csv"
    )


def _validated_class(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != _CLASS_FIELDS:
        raise EvidenceImportError(f"{label} is not an exact Phase-1 class block")
    try:
        validate_class_block(value, document="benchmark sidecar", path=label)
    except (KeyError, TypeError, StaffingCatalogError) as exc:
        raise EvidenceImportError(f"{label} is not canonical: {exc}") from exc
    return value


def _sidecar_index(document: Mapping[str, Any]) -> dict[str, _RunMetadata]:
    if document.get("schema_version") != BENCHMARK_SCHEMA_VERSION:
        raise EvidenceImportError(
            "benchmark sidecar schema_version must be " f"{BENCHMARK_SCHEMA_VERSION!r}"
        )
    _aware_timestamp(document.get("generated_at"), "sidecar generated_at")
    rows = document.get("rows")
    if not isinstance(rows, list):
        raise EvidenceImportError("benchmark sidecar rows is not an array")
    index: dict[str, _RunMetadata] = {}
    for sidecar_number, item in enumerate(rows, start=1):
        label = f"sidecar rows[{sidecar_number - 1}]"
        if not isinstance(item, dict):
            raise EvidenceImportError(f"{label} is not an object")
        task_key = item.get("task_key")
        if not isinstance(task_key, str) or not task_key:
            raise EvidenceImportError(f"{label}.task_key is missing or empty")
        class_record = _validated_class(item.get("class"), f"{label}.class")
        worker = item.get("worker")
        if not isinstance(worker, dict):
            raise EvidenceImportError(f"{label}.worker is not an object")
        run_ids = item.get("run_ids")
        run_usage = item.get("run_usage")
        if not isinstance(run_ids, list) or not isinstance(run_usage, list):
            raise EvidenceImportError(f"{label} lacks run_ids or run_usage arrays")
        usage_by_id: dict[str, Mapping[str, Any]] = {}
        for usage_item in run_usage:
            if not isinstance(usage_item, dict):
                raise EvidenceImportError(f"{label}.run_usage contains a non-object")
            run_id = usage_item.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise EvidenceImportError(f"{label}.run_usage has an invalid run_id")
            if run_id in usage_by_id:
                raise EvidenceImportError(f"duplicate sidecar run_usage id {run_id!r}")
            usage_by_id[run_id] = usage_item
        for run_id in run_ids:
            if not isinstance(run_id, str) or not run_id:
                raise EvidenceImportError(f"{label}.run_ids has an invalid run id")
            if run_id in index:
                raise EvidenceImportError(f"duplicate sidecar run id {run_id!r}")
            if run_id not in usage_by_id:
                raise EvidenceImportError(f"sidecar run {run_id!r} lacks run_usage")
            index[run_id] = _RunMetadata(
                task_key=task_key,
                class_record=class_record,
                worker=worker,
                usage=usage_by_id[run_id],
            )
        extra_usage = set(usage_by_id) - set(run_ids)
        if extra_usage:
            raise EvidenceImportError(
                f"{label}.run_usage contains ids absent from run_ids: "
                f"{sorted(extra_usage)!r}"
            )
    return index


def _row_usage(row: Mapping[str, Any]) -> dict[str, Any]:
    source_status = row.get("usage_status")
    # ``partial`` means the provider receipt omitted some optional semantic
    # detail, not that its reported counters are estimates. Preserve every
    # supplied counter exactly. ``unknown`` has no authoritative counters and
    # cannot truthfully become a provider_reported attempt.
    if source_status not in (None, "", "known", "partial"):
        raise _RowError(
            f"usage_status {source_status!r} is not provider-reported usage"
        )
    parsed = {
        column: _optional_counter(row.get(column), column) for column in TOKEN_COLUMNS
    }
    if parsed["usage_input_tokens"] is None:
        raise _RowError("usage_input_tokens is unavailable")
    if parsed["usage_output_tokens"] is None:
        raise _RowError("usage_output_tokens is unavailable")
    return {
        "basis": "provider_reported",
        "source": BENCHMARK_USAGE_SOURCE,
        "input_tokens": parsed["usage_input_tokens"],
        "output_tokens": parsed["usage_output_tokens"],
        "cached_input_tokens": parsed["usage_cached_input_tokens"],
        "reasoning_tokens": parsed["usage_reasoning_tokens"],
        "total_tokens": parsed["usage_total_tokens"],
    }


def _verify_join(
    row: Mapping[str, Any], metadata: _RunMetadata, usage: Mapping[str, Any]
) -> None:
    task_key = _required_text(row.get("task_key"), "task_key")
    if task_key != metadata.task_key:
        raise _RowError("task_key disagrees with the v6 sidecar")
    for field in ("model", "harness", "effort"):
        csv_value = row.get(field)
        sidecar_value = metadata.worker.get(field)
        if (csv_value or "") != (sidecar_value or ""):
            raise _RowError(f"{field} disagrees with the v6 sidecar")
    for source_name, record_name in (
        ("usage_input_tokens", "input_tokens"),
        ("usage_output_tokens", "output_tokens"),
        ("usage_cached_input_tokens", "cached_input_tokens"),
        ("usage_reasoning_tokens", "reasoning_tokens"),
        ("usage_total_tokens", "total_tokens"),
    ):
        expected = metadata.usage.get(source_name)
        if usage[record_name] != expected:
            raise _RowError(f"{source_name} disagrees with sidecar run_usage")


def _timestamp_note(row: Mapping[str, Any]) -> str:
    timestamps = [
        f"{name}={row[name]}"
        for name in (
            "run_created_at",
            "launch_created_at",
            "started_at",
            "finished_at",
            "captured_at",
            "evaluation_created_at",
        )
        if isinstance(row.get(name), str) and row[name]
    ]
    return "Source row timestamps: " + ", ".join(timestamps)


def _row_usage_status(row: Mapping[str, Any]) -> str | None:
    """The raw CSV usage_status value, or None when the source recorded none."""
    value = row.get("usage_status")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _row_timestamps(row: Mapping[str, Any]) -> dict[str, str]:
    """Every timestamp column the source row supplies, verbatim."""
    timestamps: dict[str, str] = {}
    for name in _SOURCE_TIMESTAMP_COLUMNS:
        value = row.get(name)
        if isinstance(value, str) and value.strip():
            # Validated, not transformed: a malformed source timestamp skips
            # the row instead of entering the record unchecked.
            timestamps[name] = _aware_timestamp(value, name)
    return timestamps


def _benchmark_payload(
    row: Mapping[str, Any],
    metadata: _RunMetadata,
    *,
    usage: Mapping[str, Any],
    source_ref: str,
    sidecar_sha256: str,
    source_csv_sha256: str,
    generated_at: str,
) -> dict[str, Any]:
    """Build the raw benchmarkV6Run payload from verified source facts only."""
    run_id = _required_text(row.get("run_id"), "run_id")
    acceptance = _acceptance(row)
    elapsed_ms = _optional_elapsed(row.get("elapsed_ms"))
    worker = metadata.worker
    effort_value = worker.get("effort")
    if effort_value is None:
        effort: str | None = None
    elif isinstance(effort_value, str) and effort_value.strip():
        effort = effort_value
    elif isinstance(effort_value, str):
        # The authoritative v6 sidecar and its CSV record some adapters'
        # worker effort as the empty string. The canonical crewWorker schema
        # admits only a non-empty string or null, so the exact source value
        # is preserved canonically as null and the raw empty string is
        # disclosed verbatim in provenance. Nothing is substituted.
        effort = None
    else:
        raise _RowError("sidecar worker effort is invalid")
    raw_usage: dict[str, Any] = {
        "usage_status": _row_usage_status(row),
        "usage_input_tokens": usage["input_tokens"],
        "usage_output_tokens": usage["output_tokens"],
        "usage_cached_input_tokens": usage["cached_input_tokens"],
        "usage_reasoning_tokens": usage["reasoning_tokens"],
        "usage_total_tokens": usage["total_tokens"],
    }
    payload: dict[str, Any] = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "sidecar_sha256": sidecar_sha256,
        "source_csv": {"path": source_ref, "sha256": source_csv_sha256},
        "generated_at": generated_at,
        "run_id": run_id,
        "task_key": metadata.task_key,
        "class": dict(metadata.class_record),
        # Exactly the four join-verified worker fields, by name: the sidecar
        # worker block may carry extra provider/vendor labels that would
        # imply route semantics the source does not assert.
        "worker": {
            "model": _required_text(worker.get("model"), "sidecar worker model"),
            "harness": _required_text(worker.get("harness"), "sidecar worker harness"),
            "effort": effort,
            "model_family": _required_text(
                worker.get("model_family"), "sidecar worker model_family"
            ),
        },
        "usage": raw_usage,
        "acceptance": acceptance,
        "elapsed_ms": elapsed_ms,
    }
    timestamps = _row_timestamps(row)
    if timestamps:
        payload["source_timestamps"] = timestamps
    return payload


def _build_record(
    row: Mapping[str, Any],
    metadata: _RunMetadata,
    *,
    attempt_id: str,
    sidecar_path: Path,
    csv_path: Path,
    source_ref: str,
    sidecar_sha256: str,
    source_csv_sha256: str,
    generated_at: str,
    correction_of: str | None = None,
) -> dict[str, Any]:
    usage = _row_usage(row)
    _verify_join(row, metadata, usage)
    acceptance = _acceptance(row)
    captured_at = _aware_timestamp(row.get("captured_at"), "captured_at")
    elapsed_ms = _optional_elapsed(row.get("elapsed_ms"))

    notes = [
        "No route or supervisor route is asserted because the source does not "
        "identify a staffing channel.",
        "Canonical cost is unavailable: source API-equivalent figures are not "
        "Phase-1 list and marginal costs for a selected route.",
        "The payload records only facts from the verified v6 sidecar and its "
        "SHA-256-pinned source CSV; the v6 sidecar carries no crew name and no "
        "crews-file identity, so neither is asserted.",
    ]
    if correction_of is not None:
        notes.insert(
            0,
            f"Truthful correction of legacy record {correction_of}: that "
            "committed line's crew_name and crews_file_sha256 were "
            "compatibility fabrications (the v6 sidecar provides neither); the "
            "append-only ledger keeps the legacy line unmodified.",
        )
    raw_effort = metadata.worker.get("effort")
    if isinstance(raw_effort, str) and not raw_effort.strip():
        notes.append(
            "Source worker effort is the empty string in the v6 sidecar and "
            "CSV; the canonical crewWorker schema admits only a non-empty "
            "string or null, so it is preserved as null."
        )
    notes.append(_timestamp_note(row))
    record = {
        "schema_version": 2,
        "attempt_id": attempt_id,
        "record_kind": "benchmark_run",
        "captured_at": captured_at,
        "verified_success": False,
        "class_record": dict(metadata.class_record),
        "verdict": _verdict(acceptance),
        "usage": usage,
        "cost": {"basis": ["unavailable"]},
        "wall_clock_ms": elapsed_ms,
        "benchmark_run": _benchmark_payload(
            row,
            metadata,
            usage=usage,
            source_ref=source_ref,
            sidecar_sha256=sidecar_sha256,
            source_csv_sha256=source_csv_sha256,
            generated_at=generated_at,
        ),
        "provenance": {
            "source": "benchmark",
            "recorded_by": "lee-llm-router evidence import",
            "source_refs": [str(sidecar_path), str(csv_path)],
            "notes": notes,
        },
    }
    validate_attempt(record)
    return record


def import_benchmark_evidence(
    sidecar: str | Path,
    *,
    csv_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
) -> ImportSummary:
    """Import valid per-run rows from a benchmark v6 sidecar and source CSV.

    Malformed or unmappable CSV rows are skipped and returned as explicit
    issues.  Structural sidecar errors, a source checksum mismatch, or an
    invalid existing ledger fail the whole operation before any new append.

    Args:
        sidecar: Benchmark v6 staffing-evidence JSON sidecar.
        csv_path: Optional source CSV override.  The file still must match the
            SHA-256 pinned by ``sidecar.source_csv``.
        ledger_path: Optional explicit attempt ledger.  When absent the normal
            app-specific environment/default resolution is used.

    Returns:
        Imported/skipped counts, diagnostics, and resolved ledger path.

    Raises:
        EvidenceImportError: If source-level evidence cannot be verified.
        AttemptLedgerError: If the existing ledger or a built record is
            invalid.
        OSError: If the ledger cannot be read or appended.
    """
    sidecar_path = Path(sidecar).expanduser()
    document, sidecar_bytes = _read_json_object(sidecar_path)
    index = _sidecar_index(document)
    source_csv = document.get("source_csv")
    if not isinstance(source_csv, dict):
        raise EvidenceImportError("benchmark sidecar source_csv is not an object")
    source_ref = source_csv.get("path")
    expected_sha256 = source_csv.get("sha256")
    if not isinstance(source_ref, str) or not source_ref:
        raise EvidenceImportError("benchmark sidecar source_csv.path is missing")
    if not isinstance(expected_sha256, str) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", expected_sha256
    ):
        raise EvidenceImportError("benchmark sidecar source_csv.sha256 is invalid")
    resolved_csv = _source_csv_path(sidecar_path, source_ref, csv_path)
    try:
        csv_bytes = resolved_csv.read_bytes()
    except OSError as exc:
        raise EvidenceImportError(
            f"cannot read benchmark CSV {resolved_csv}: {exc}"
        ) from exc
    actual_sha256 = hashlib.sha256(csv_bytes).hexdigest()
    if actual_sha256.lower() != expected_sha256.lower():
        raise EvidenceImportError(
            f"benchmark CSV SHA-256 mismatch for {resolved_csv}: "
            f"expected {expected_sha256.lower()}, got {actual_sha256}"
        )

    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise EvidenceImportError(f"benchmark CSV {resolved_csv} is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise EvidenceImportError(f"benchmark CSV {resolved_csv} has no header")
    required_headers = {
        "run_id",
        "task_key",
        "model",
        "harness",
        "effort",
        "captured_at",
        "usage_input_tokens",
        "usage_output_tokens",
        "usage_cached_input_tokens",
        "usage_reasoning_tokens",
        "usage_total_tokens",
    }
    missing_headers = sorted(required_headers - set(reader.fieldnames))
    if missing_headers:
        raise EvidenceImportError(
            f"benchmark CSV {resolved_csv} is missing columns {missing_headers!r}"
        )

    generated_at = str(document["generated_at"])
    sidecar_sha256 = hashlib.sha256(sidecar_bytes).hexdigest()
    target = resolve_attempts_path(ledger_path)
    with writer_transaction(target):
        existing = read_attempts(target) if target.is_file() else []
        known: dict[str, dict[str, Any]] = {
            record["attempt_id"]: record for record in existing
        }
        imported = 0
        issues: list[ImportIssue] = []
        for row_number, row in enumerate(reader, start=2):
            run_id_value = row.get("run_id")
            run_id = run_id_value.strip() if isinstance(run_id_value, str) else None
            try:
                if not run_id:
                    raise _RowError("run_id is missing or empty")
                metadata = index.get(run_id)
                if metadata is None:
                    raise _RowError("run_id is absent from the v6 sidecar")
                attempt_id = _attempt_id(run_id)
                correction_of: str | None = None
                existing_record = known.get(attempt_id)
                if existing_record is not None:
                    if _legacy_crew_run_run_id(existing_record) != run_id:
                        issues.append(
                            ImportIssue(
                                row_number, run_id, "attempt_id already present"
                            )
                        )
                        continue
                    # Governed reconciliation of a pre-repair legacy crew-run
                    # record: append the truthful raw-shape record under a
                    # deterministic correction id instead of rewriting history.
                    correction_id = _correction_attempt_id(run_id)
                    if correction_id in known:
                        issues.append(
                            ImportIssue(
                                row_number, run_id, "correction already present"
                            )
                        )
                        continue
                    attempt_id = correction_id
                    correction_of = f"{_ATTEMPT_ID_PREFIX}{run_id}"
                record = _build_record(
                    row,
                    metadata,
                    attempt_id=attempt_id,
                    sidecar_path=sidecar_path,
                    csv_path=resolved_csv,
                    source_ref=source_ref,
                    sidecar_sha256=sidecar_sha256,
                    source_csv_sha256=actual_sha256,
                    generated_at=generated_at,
                    correction_of=correction_of,
                )
            except (_RowError, EvidenceImportError, AttemptLedgerError) as exc:
                issues.append(ImportIssue(row_number, run_id, str(exc)))
                continue
            append_attempt(record, target)
            known[record["attempt_id"]] = record
            imported += 1

        return ImportSummary(
            imported=imported,
            skipped=len(issues),
            issues=tuple(issues),
            ledger_path=target,
        )


# ---------------------------------------------------------------------------
# Agent-Orch raw per-attempt import (P1-7b1)
# ---------------------------------------------------------------------------


def _agent_orch_run_files(root: Path) -> list[Path]:
    """Return deterministic run manifests below an Agent-Orch evidence root."""
    root = root.expanduser()
    if root.is_file():
        if root.name != "run.json":
            raise EvidenceImportError(
                f"agent-orch source {root} is not a run.json manifest"
            )
        return [root.resolve()]
    if not root.is_dir():
        raise EvidenceImportError(f"agent-orch source directory not found: {root}")

    if root.name.endswith("-agent-orch-runs") or root.name == "agent-orch-runs":
        paths = (
            path for path in root.glob("*/run.json") if path.parent.name != "latest"
        )
    else:
        paths = (
            path
            for path in root.glob("*-agent-orch-runs/*/run.json")
            if path.parent.name != "latest"
        )
    # Real roots expose ``latest`` as a symlink to a retained run. Excluding
    # that pointer above and de-duplicating resolved paths prevents one source
    # attempt from being reported as a duplicate during its first import.
    return sorted({path.resolve() for path in paths if path.is_file()})


def _agent_orch_json_object(path: Path, label: str) -> dict[str, Any]:
    """Read one authorized Agent-Orch artifact as a JSON object."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _RowError(f"cannot read {label}: {exc}") from exc
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _RowError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise _RowError(f"{label} is not a JSON object")
    return value


def _agent_orch_run_dir(value: Any, run_path: Path) -> Path:
    """Resolve the attempt directory named by a run manifest."""
    if not isinstance(value, str) or not value.strip():
        raise _RowError("run_dir is missing or empty")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = run_path.parent / candidate
    return candidate.resolve()


def _agent_orch_route(route_document: Mapping[str, Any]) -> dict[str, Any]:
    """Copy the authorized three-field selected route, without enrichment."""
    selected = route_document.get("selected_route")
    if not isinstance(selected, dict):
        raise _RowError("route-selection.json selected_route is missing")
    harness = selected.get("harness")
    model = selected.get("model")
    if not isinstance(harness, str) or not harness.strip():
        raise _RowError("selected route harness is missing")
    if not isinstance(model, str) or not model.strip():
        raise _RowError("selected route model is missing")
    effort = selected.get("effort")
    if effort is not None and (not isinstance(effort, str) or not effort.strip()):
        raise _RowError("selected route effort is invalid")
    # Agent-Orch omits effort for adapters without an effort dial.  The raw
    # contract represents that source-level absence explicitly as null; no
    # model, provider, channel, or route id is substituted.
    return {"harness": harness, "model": model, "effort": effort}


def _agent_orch_counter(value: Any, label: str) -> int | None:
    """Validate one raw receipt counter without turning unknown into zero."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _RowError(f"{label} is not a nonnegative integer or null")
    return value


def _agent_orch_amount(value: Any, label: str) -> int | float:
    """Validate one raw receipt amount without accepting JSON booleans/NaN."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _RowError(f"{label} is not a nonnegative number")
    if value < 0 or (isinstance(value, float) and not math.isfinite(value)):
        raise _RowError(f"{label} is not a nonnegative finite number")
    return value


def _agent_orch_raw_usage(
    usage_document: Mapping[str, Any], *, require_core: bool
) -> dict[str, Any]:
    """Project accepted raw token names, requiring core counts when measured."""
    raw_usage = usage_document.get("raw_usage")
    if raw_usage is not None and not isinstance(raw_usage, dict):
        raise _RowError("usage.json raw_usage is not an object")

    raw: dict[str, Any] = {}
    for name in (
        "input_tokens",
        "output_tokens",
        "cached_read_tokens",
        "total_tokens",
    ):
        if name in usage_document:
            raw[name] = _agent_orch_counter(usage_document[name], name)
    if isinstance(raw_usage, dict) and "reasoning_output_tokens" in raw_usage:
        raw["reasoning_output_tokens"] = _agent_orch_counter(
            raw_usage["reasoning_output_tokens"],
            "raw_usage.reasoning_output_tokens",
        )

    if require_core:
        for required in ("input_tokens", "output_tokens"):
            if required not in raw or raw[required] is None:
                raise _RowError(f"usage.json {required} is unavailable")
    return raw


def _agent_orch_unavailable_mapping(
    accounting_status: str | None,
    usage_document: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the fixed canonical usage/cost mapping for no receipt/spend."""
    if accounting_status is None:
        reason = "missing usage receipt"
    elif accounting_status == "not_applicable":
        reason = "non-metered adapter"
    elif accounting_status == "unaccounted":
        raw_reason = usage_document.get("reason") if usage_document else None
        if not isinstance(raw_reason, str) or not raw_reason.strip():
            raise _RowError("unaccounted usage.json reason is missing")
        reason = raw_reason
    else:  # pragma: no cover - status is checked by the caller
        raise _RowError(
            f"unsupported unavailable accounting status {accounting_status!r}"
        )
    return (
        {"basis": "unavailable", "unavailable_reason": reason},
        {"basis": "unavailable"},
    )


def _agent_orch_attempt_record(
    run_document: Mapping[str, Any],
    run_path: Path,
    step: Mapping[str, Any],
    attempt: Mapping[str, Any],
    route_path: Path,
    usage_path: Path | None,
    route_document: Mapping[str, Any],
    usage_document: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build one v2 raw-attempt record from only the three allowed artifacts."""
    run_id = run_document.get("run_id")
    step_id = step.get("step_id")
    attempt_number = attempt.get("attempt_number")
    if not isinstance(run_id, str) or not run_id:
        raise _RowError("run.json run_id is missing")
    if not isinstance(step_id, str) or not step_id:
        raise _RowError("step_id is missing")
    if (
        isinstance(attempt_number, bool)
        or not isinstance(attempt_number, int)
        or attempt_number < 1
    ):
        raise _RowError("attempt_number is not a positive integer")

    worker_exit_code = attempt.get("worker_exit_code")
    validation_passed = attempt.get("validation_passed")
    if isinstance(worker_exit_code, bool) or not isinstance(worker_exit_code, int):
        raise _RowError("worker_exit_code is not an integer")
    if not isinstance(validation_passed, bool):
        raise _RowError("validation_passed is not a boolean")
    policy_decision = attempt.get("policy_decision")
    if not isinstance(policy_decision, str) or not policy_decision.strip():
        raise _RowError("policy_decision is missing or empty")

    failure_classification = attempt.get("failure_classification")
    if failure_classification is not None and (
        not isinstance(failure_classification, str)
        or not failure_classification.strip()
    ):
        raise _RowError("failure_classification is invalid")

    accounting_status: str | None
    if usage_document is None:
        accounting_status = None
    else:
        accounting_status_value = usage_document.get("accounting_status")
        if accounting_status_value not in {
            "measured",
            "unaccounted",
            "not_applicable",
        }:
            raise _RowError("usage.json accounting_status is invalid")
        accounting_status = accounting_status_value

    route = _agent_orch_route(route_document)
    source_paths = [str(run_path), str(route_path)]
    if usage_path is not None:
        source_paths.append(str(usage_path))
    raw_payload: dict[str, Any] = {
        "run_id": run_id,
        "step_id": step_id,
        "attempt_number": attempt_number,
        "route": route,
        "worker_exit_code": worker_exit_code,
        "validation_passed": validation_passed,
        "policy_decision": policy_decision,
        "failure_classification": failure_classification,
        "accounting_status": accounting_status,
        "source_paths": source_paths,
    }

    if usage_document is not None:
        raw_usage = _agent_orch_raw_usage(
            usage_document, require_core=accounting_status == "measured"
        )
        if raw_usage:
            raw_payload["usage"] = raw_usage

    if accounting_status == "measured":
        if usage_path is None:  # pragma: no cover - paired by the caller
            raise _RowError("measured attempt has no usage receipt")
        if "cost_usd" not in usage_document:
            raise _RowError("measured usage.json cost_usd is unavailable")
        raw_payload["cost_usd"] = _agent_orch_amount(
            usage_document["cost_usd"], "usage.json cost_usd"
        )
    elif accounting_status in {"unaccounted", "not_applicable", None}:
        pass
    else:  # pragma: no cover - guarded above
        raise _RowError(f"unsupported accounting status {accounting_status!r}")

    for timestamp_name in ("started_at", "ended_at"):
        timestamp = attempt.get(timestamp_name)
        if timestamp is not None:
            raw_payload[timestamp_name] = _aware_timestamp(
                timestamp, f"attempt {timestamp_name}"
            )

    if accounting_status == "measured":
        usage = raw_payload["usage"]
        canonical_usage = {
            "basis": "provider_reported",
            "source": _AGENT_ORCH_USAGE_SOURCE,
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
        }
        if "cached_read_tokens" in usage:
            canonical_usage["cached_input_tokens"] = usage["cached_read_tokens"]
        if "reasoning_output_tokens" in usage:
            canonical_usage["reasoning_tokens"] = usage["reasoning_output_tokens"]
        if "total_tokens" in usage:
            canonical_usage["total_tokens"] = usage["total_tokens"]
        canonical_cost = {"basis": "list", "usd_list": raw_payload["cost_usd"]}
    else:
        canonical_usage, canonical_cost = _agent_orch_unavailable_mapping(
            accounting_status, usage_document
        )

    attempt_id = hashlib.sha256(
        f"{run_id}/{step_id}/{attempt_number}".encode("utf-8")
    ).hexdigest()
    captured_at = _aware_timestamp(
        run_document.get("last_updated_at"), "run.json last_updated_at"
    )
    verified_success = worker_exit_code == 0 and validation_passed is True
    record = {
        "schema_version": 2,
        "attempt_id": attempt_id,
        "record_kind": "agent_orch_attempt",
        "captured_at": captured_at,
        "verified_success": verified_success,
        "class_source": "none",
        "verdict": {"tier": "engine_validation"},
        "usage": canonical_usage,
        "cost": canonical_cost,
        "agent_orch_attempt": raw_payload,
        "provenance": {
            "source": _AGENT_ORCH_PROVENANCE_SOURCE,
            "recorded_by": _AGENT_ORCH_RECORDED_BY,
            "source_refs": [_AGENT_ORCH_AUTHORITY_REF, *source_paths],
            "notes": [
                "Only run.json, sibling route-selection.json, and usage.json "
                "when present were read for this raw attempt.",
                "attempt_id is sha256 of " f"{run_id}/{step_id}/{attempt_number}.",
                "captured_at preserves run.json last_updated_at; no per-attempt "
                "timestamp or wall-clock duration is inferred.",
                "No class, channel, provider, supervisor route, oracle, or "
                "marginal cost is inferred.",
            ],
        },
    }
    validate_attempt(record)
    return record


def _agent_orch_cutoff(now: datetime | None) -> tuple[datetime, datetime]:
    """Return the UTC now and precise thirty-day lower bound."""
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None or current.utcoffset() is None:
        raise EvidenceImportError("agent-orch cutoff time must be timezone-aware")
    current_utc = current.astimezone(timezone.utc)
    return current_utc - timedelta(days=_AGENT_ORCH_DAYS), current_utc


def import_agent_orch_evidence(
    source: str | Path,
    *,
    ledger_path: str | Path | None = None,
    now: datetime | None = None,
) -> ImportSummary:
    """Import recent Agent-Orch raw attempts into the append-only ledger.

    The source is either a ``*-agent-orch-runs`` directory, its containing
    projects directory, or one ``run.json`` fixture.  A run is eligible only
    when its authoritative ``run.json.last_updated_at`` lies in the precise
    preceding thirty days.  Each attempt reads only the run manifest, its
    sibling route-selection artifact, and usage.json when present.

    Args:
        source: Agent-Orch evidence root or one run.json file.
        ledger_path: Optional explicit attempt ledger path.
        now: Aware UTC reference time, injectable for deterministic fixtures.

    Returns:
        Imported/skipped counts, diagnostics, and the resolved ledger path.

    Raises:
        EvidenceImportError: If the source root or cutoff is unusable.
        AttemptLedgerError: If the existing ledger is invalid.
        OSError: If the ledger cannot be read or appended.
    """
    source_path = Path(source).expanduser()
    run_paths = _agent_orch_run_files(source_path)
    cutoff, current = _agent_orch_cutoff(now)
    target = resolve_attempts_path(ledger_path)
    with writer_transaction(target):
        existing = read_attempts(target) if target.is_file() else []
        known_ids = {record["attempt_id"] for record in existing}

        imported = 0
        issues: list[ImportIssue] = []
        source_ordinal = 0
        for run_path in run_paths:
            run_id_value: str | None = None
            try:
                run_document = _agent_orch_json_object(run_path, str(run_path))
                run_id = run_document.get("run_id")
                if not isinstance(run_id, str) or not run_id:
                    raise _RowError("run.json run_id is missing")
                run_id_value = run_id
                timestamp_text = _aware_timestamp(
                    run_document.get("last_updated_at"), f"{run_path} last_updated_at"
                )
                timestamp = datetime.fromisoformat(
                    timestamp_text.replace("Z", "+00:00")
                )
                timestamp_utc = timestamp.astimezone(timezone.utc)
                if timestamp_utc < cutoff or timestamp_utc > current:
                    continue
                steps = run_document.get("step_results")
                if not isinstance(steps, list):
                    raise _RowError("run.json step_results is not an array")
            except _RowError as exc:
                source_ordinal += 1
                issues.append(ImportIssue(source_ordinal, run_id_value, str(exc)))
                continue

            for step in steps:
                if not isinstance(step, dict):
                    source_ordinal += 1
                    issues.append(
                        ImportIssue(
                            source_ordinal, run_id, "step result is not an object"
                        )
                    )
                    continue
                step_id = step.get("step_id")
                attempts = step.get("attempts")
                if not isinstance(step_id, str) or not step_id:
                    source_ordinal += 1
                    issues.append(
                        ImportIssue(source_ordinal, run_id, "step_id is missing")
                    )
                    continue
                if not isinstance(attempts, list):
                    source_ordinal += 1
                    issues.append(
                        ImportIssue(
                            source_ordinal, run_id, "step attempts is not an array"
                        )
                    )
                    continue
                for attempt in attempts:
                    source_ordinal += 1
                    if not isinstance(attempt, dict):
                        issues.append(
                            ImportIssue(
                                source_ordinal,
                                run_id,
                                "attempt entry is not an object",
                            )
                        )
                        continue
                    try:
                        attempt_dir = _agent_orch_run_dir(
                            attempt.get("run_dir"), run_path
                        )
                        route_path = attempt_dir / "route-selection.json"
                        if not route_path.is_file():
                            raise _RowError("sibling route-selection.json is missing")
                        route_document = _agent_orch_json_object(
                            route_path, str(route_path)
                        )
                        usage_path = attempt_dir / "usage.json"
                        usage_document = (
                            _agent_orch_json_object(usage_path, str(usage_path))
                            if usage_path.is_file()
                            else None
                        )
                        record = _agent_orch_attempt_record(
                            run_document,
                            run_path,
                            step,
                            attempt,
                            route_path,
                            usage_path if usage_document is not None else None,
                            route_document,
                            usage_document,
                        )
                    except (_RowError, AttemptLedgerError) as exc:
                        issues.append(ImportIssue(source_ordinal, run_id, str(exc)))
                        continue
                    if record["attempt_id"] in known_ids:
                        issues.append(
                            ImportIssue(
                                source_ordinal,
                                run_id,
                                "attempt_id already present",
                            )
                        )
                        continue
                    append_attempt(record, target)
                    known_ids.add(record["attempt_id"])
                    imported += 1

        return ImportSummary(
            imported=imported,
            skipped=len(issues),
            issues=tuple(issues),
            ledger_path=target,
        )


__all__ = [
    "BENCHMARK_CORRECTION_PREFIX",
    "BENCHMARK_SCHEMA_VERSION",
    "BENCHMARK_USAGE_SOURCE",
    "benchmark_source_run_id",
    "is_benchmark_v6_record",
    "EvidenceImportError",
    "ImportIssue",
    "ImportSummary",
    "import_agent_orch_evidence",
    "import_benchmark_evidence",
]
