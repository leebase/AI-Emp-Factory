#!/usr/bin/env python3
"""Generate and reconcile the first AI Employee Foundation registry records.

The deployment schema and validation live in the sibling ``ai-employee``
repository. This script owns only Factory concrete entries and source
reconciliation. It never edits roster, mission, workspace, Board, or live
state; contradictions are emitted as findings.

Usage::

    python3 scripts/employee_registry.py generate
    python3 scripts/employee_registry.py reconcile \
        --output hiring/registry/reconciliation-report.json

The checked-in seed is deliberately concrete for the first four adopters. It
contains install-local workspace paths and source fingerprints; those are
Factory registry inputs, not fields copied into the canonical deployment
record.
"""

import argparse
import hashlib
import importlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SEED = ROOT / "hiring" / "registry" / "registry-seed.json"
DEFAULT_RECORDS = ROOT / "hiring" / "registry" / "records"
DEFAULT_REPORT = ROOT / "hiring" / "registry" / "reconciliation-report.json"
DEFAULT_ROSTER = ROOT / "hiring" / "roster.md"
DEFAULT_MISSIONS = ROOT.parent / "auto-orch" / "missions"


def _load_contract_api() -> tuple[Any, ...]:
    """Load the canonical contract without vendoring or reimplementing it."""

    try:
        contract = importlib.import_module("ai_employee")
    except ModuleNotFoundError:
        sibling_source = ROOT.parent / "ai-employee" / "src"
        if not sibling_source.is_dir():
            raise
        sys.path.insert(0, str(sibling_source))
        contract = importlib.import_module("ai_employee")
    return tuple(
        getattr(contract, name)
        for name in (
            "BoardIdentity",
            "CommissioningLineage",
            "DeliveryProfileReference",
            "DeploymentError",
            "DeploymentRecord",
            "EmployeeSpecReference",
            "LifecycleState",
            "LifecycleTransition",
            "WorkspaceBinding",
        )
    )


(
    BoardIdentity,
    CommissioningLineage,
    DeliveryProfileReference,
    DeploymentError,
    DeploymentRecord,
    EmployeeSpecReference,
    LifecycleState,
    LifecycleTransition,
    WorkspaceBinding,
) = _load_contract_api()


class RegistryError(ValueError):
    """Raised when concrete registry input cannot be reconciled safely."""


_SEED_KEYS = frozenset({"schema_version", "tenant_id", "captured_at", "entries"})
_ENTRY_KEYS = frozenset(
    {
        "employee_id",
        "record_mode",
        "display_name",
        "mission_name",
        "workspace_ref",
        "workspace_path",
        "board_ref",
        "delivery_profile_ref",
        "specification_ref",
        "specification_source",
        "specification_digest",
        "commissioning_ref",
        "commissioning_source_ref",
        "commissioned_by",
        "commissioned_at",
        "roster_key",
        "principal_refs",
        "credential_scope_refs",
    }
)
_DRAFT_ENTRY_KEYS = _ENTRY_KEYS | frozenset({"specification_body"})
_INTERACTIVE_ENTRY_KEYS = (
    _ENTRY_KEYS - frozenset({"mission_name", "delivery_profile_ref"})
) | frozenset({"registration_ref"})
# The additive Board participation trio. It is optional on draft/prepared
# entries only, and all-or-none: a record without it carries no participant
# binding. It never aliases the record-level ``principal_refs`` list.
_PARTICIPATION_ENTRY_KEYS = frozenset({"principal_ref", "agent_id", "machine_id"})
_EXPECTED_SEED_VERSION = "ai-employee-registry-seed/1.0"
_RECONCILIATION_VERSION = "ai-employee-registry-reconciliation/1.2"
_OPERATING_STATES = frozenset({"pilot", "scheduled", "live"})


def _strict_keys(
    body: Any,
    required: frozenset[str],
    field: str,
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise RegistryError(f"{field} must be an object")
    allowed = required | optional
    unknown = sorted(str(key) for key in set(body) - allowed)
    if unknown:
        raise RegistryError(f"{field} has unknown field(s): {', '.join(unknown)}")
    missing = sorted(required - set(body))
    if missing:
        raise RegistryError(
            f"{field} is missing required field(s): {', '.join(missing)}"
        )
    return body


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{field} must be a non-empty string")
    return value


def _reference_array(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise RegistryError(f"{field} must be an array")
    references = [_text(item, f"{field}[{index}]") for index, item in enumerate(value)]
    _ensure_unique(references, field)
    return references


def _required_references(value: Any, field: str) -> list[str]:
    references = _reference_array(value, field)
    if not references:
        raise RegistryError(f"{field} must be a non-empty array")
    return references


def _ensure_unique(values: list[str], field: str) -> None:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise RegistryError(
            f"{field} contains duplicate value(s): {', '.join(duplicates)}"
        )


def _strict_bool(value: Any, field: str) -> bool:
    if value not in {"true", "false"}:
        raise RegistryError(f"{field} must be true or false")
    return value == "true"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RegistryError(f"cannot read JSON source {path}: {exc}") from exc


def load_seed(path: Path = DEFAULT_SEED) -> dict[str, Any]:
    body = _strict_keys(_read_json(path), _SEED_KEYS, "registry seed")
    if body["schema_version"] != _EXPECTED_SEED_VERSION:
        raise RegistryError(
            f"unsupported registry seed schema version: {body['schema_version']!r}"
        )
    _text(body["tenant_id"], "registry seed.tenant_id")
    _text(body["captured_at"], "registry seed.captured_at")
    entries = body["entries"]
    if not isinstance(entries, list) or not entries:
        raise RegistryError("registry seed.entries must be a non-empty array")
    for index, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, dict):
            raise RegistryError(f"registry seed.entries[{index}] must be an object")
        record_mode = raw_entry.get("record_mode")
        if record_mode == "draft":
            entry_keys: frozenset[str] = _DRAFT_ENTRY_KEYS
            optional_keys: frozenset[str] = frozenset()
        elif record_mode == "interactive":
            entry_keys = _INTERACTIVE_ENTRY_KEYS
            optional_keys = frozenset({"delivery_profile_ref"}) | _PARTICIPATION_ENTRY_KEYS
        elif record_mode == "commissioning":
            entry_keys = _INTERACTIVE_ENTRY_KEYS
            optional_keys = frozenset({"delivery_profile_ref"}) | _PARTICIPATION_ENTRY_KEYS
        else:
            entry_keys = _ENTRY_KEYS
            optional_keys = _PARTICIPATION_ENTRY_KEYS
        entry = _strict_keys(
            raw_entry,
            entry_keys,
            f"registry seed.entries[{index}]",
            optional=optional_keys,
        )
        _required_references(
            entry["principal_refs"],
            f"registry seed.entries[{index}].principal_refs",
        )
        _reference_array(
            entry["credential_scope_refs"],
            f"registry seed.entries[{index}].credential_scope_refs",
        )
        if record_mode not in {"live", "draft", "interactive", "commissioning"}:
            raise RegistryError(
                f"registry seed.entries[{index}].record_mode must be "
                "live, draft, interactive, or commissioning"
            )
        participation = _PARTICIPATION_ENTRY_KEYS & set(entry)
        if participation and participation != _PARTICIPATION_ENTRY_KEYS:
            raise RegistryError(
                f"registry seed.entries[{index}] participation fields must include "
                "principal_ref, agent_id, and machine_id together"
            )
        for key in sorted(participation):
            _text(entry[key], f"registry seed.entries[{index}].{key}")
        if entry["record_mode"] == "draft" and not isinstance(
            entry["specification_body"], dict
        ):
            raise RegistryError(
                f"registry seed.entries[{index}].specification_body must be an object"
            )
        if entry["record_mode"] == "interactive":
            _text(
                entry["registration_ref"],
                f"registry seed.entries[{index}].registration_ref",
            )
    for key in (
        "employee_id",
        "workspace_ref",
        "workspace_path",
        "board_ref",
    ):
        _ensure_unique([entry[key] for entry in entries], f"registry seed.{key}")
    _ensure_unique(
        [entry["mission_name"] for entry in entries if "mission_name" in entry],
        "registry seed.mission_name",
    )
    return body


def _sha256_file(path: Path) -> str:
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RegistryError(f"cannot hash source {path}: {exc}") from exc
    return f"sha256:{digest}"


def _source_digest(path: Path) -> str:
    """Digest of a live specification source.

    JSON `ai-employee-spec/*` sources are digested canonically by the schema owner
    (`ai_employee.deployment.employee_spec_digest`) — the same value the foundation
    validator and the leebase design records use — so one spec has one digest
    everywhere (employee-foundation/1.0 rule 2). Prose sources (mission.md, README)
    keep the raw-file SHA-256.
    """
    if path.suffix == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return _sha256_file(path)
        if isinstance(payload, dict) and str(payload.get("schema_version", "")).startswith("ai-employee-spec/"):
            contract = importlib.import_module("ai_employee")
            foundation = importlib.import_module("ai_employee.foundation")
            deployment = importlib.import_module("ai_employee.deployment")
            return deployment.employee_spec_digest(foundation.load_employee_spec(payload))
    return _sha256_file(path)


def _canonical_body_digest(body: dict[str, Any]) -> str:
    try:
        canonical = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RegistryError(
            "draft specification_body must contain JSON values"
        ) from exc
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _parse_config(path: Path) -> dict[str, str]:
    """Read only the stable scalar fields needed for reconciliation."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RegistryError(f"cannot read mission config {path}: {exc}") from exc

    values: dict[str, str] = {}
    board_values: dict[str, str] = {}
    in_board = False
    board_indent = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if stripped == "board:":
            in_board = True
            board_indent = indent
            continue
        if in_board and indent <= board_indent:
            in_board = False
        if ":" not in stripped:
            continue
        key, value = (part.strip() for part in stripped.split(":", 1))
        value = value.split(" #", 1)[0].strip().strip("\"'")
        if in_board:
            if key in {"enabled", "machine_id", "mission_label"}:
                board_values[key] = value
        elif key in {"launch_approved", "cadence", "workspace"}:
            values[key] = value
    values.update({f"board.{key}": value for key, value in board_values.items()})
    return values


def _parse_state(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryError(f"cannot read mission state {path}: {exc}") from exc

    fields: dict[str, str] = {}
    for key in (
        "paused",
        "last_cycle_outcome",
        "last_cycle_end",
        "last_execute_run_status",
        "last_run_passed",
        "loop_state",
        "supervised_commissioning",
    ):
        match = re.search(rf"(?m)^{re.escape(key)}:\s*(.+?)\s*$", text)
        if match:
            fields[key] = match.group(1).strip()
    return fields


def _validate_live_config(config: dict[str, str], employee_id: str) -> None:
    required = (
        "launch_approved",
        "cadence",
        "workspace",
        "board.enabled",
        "board.machine_id",
    )
    missing = [key for key in required if key not in config]
    if missing:
        raise RegistryError(
            f"mission config for {employee_id} is missing required field(s): "
            + ", ".join(missing)
        )
    _strict_bool(config["launch_approved"], f"{employee_id}.launch_approved")
    _strict_bool(config["board.enabled"], f"{employee_id}.board.enabled")
    if config["cadence"] not in {
        "hourly",
        "daily",
        "manual",
        "manual-first-fire",
        "static",
        "none",
    }:
        raise RegistryError(
            f"{employee_id}.cadence is unsupported: {config['cadence']!r}"
        )
    _text(config["workspace"], f"{employee_id}.workspace")
    _text(config["board.machine_id"], f"{employee_id}.board.machine_id")


def _observed_lifecycle(config: dict[str, str], state: dict[str, str]) -> Any:
    paused = state.get("paused")
    if paused is not None and paused.lower() not in {"true", "false"}:
        raise RegistryError(
            f"mission state paused must be true or false, got {paused!r}"
        )
    _validate_live_config(config, config.get("employee_id", "mission"))
    if paused == "true":
        return LifecycleState.PAUSED
    if config["launch_approved"] == "true":
        if config["cadence"] in {"hourly", "daily"}:
            return LifecycleState.SCHEDULED
        return LifecycleState.PILOT
    return LifecycleState.DRAFT


_PROVENANCE_STAMPS = frozenset({"created_at", "created_by", "updated_at", "updated_by"})


def _binding_view(record: Any) -> dict[str, Any]:
    """Record fields that constitute the binding; provenance stamps are not bindings.

    Records minted or re-stamped outside ``generate`` (a lifecycle edit, a digest
    refresh) legitimately carry their own timestamps; drift is about what the
    record binds, not when it was written.
    """
    return {k: v for k, v in record.to_dict().items() if k not in _PROVENANCE_STAMPS}


def _binding_missing_fields(record: Any) -> list[str]:
    """Return missing or mismatched fields in the instance Board binding."""

    identity = record.board_identity
    missing = [
        field
        for field in ("board_ref", "principal_ref", "agent_id", "machine_id")
        if not isinstance(getattr(identity, field, None), str)
        or not getattr(identity, field).strip()
    ]
    if "agent_id" not in missing and identity.agent_id != record.employee_id:
        missing.append("agent_id=employee_id")
    if (
        "principal_ref" not in missing
        and "machine_id" not in missing
        and identity.principal_ref == identity.machine_id
    ):
        missing.append("principal_ref!=machine_id")
    return missing


def _has_complete_binding(record: Any) -> bool:
    return not _binding_missing_fields(record)


def _has_grant_reference(record: Any) -> bool:
    """A scope reference is metadata only; its value is never a credential."""

    return bool(record.credential_scope_refs)


def _is_participant(record: Any) -> bool:
    """Whether a record has both the complete binding and a grant reference."""

    return _has_complete_binding(record) and _has_grant_reference(record)


def _record_classification(
    record: Any | None, entry: dict[str, Any] | None
) -> str:
    """Classify registry posture without treating ``board_ref`` as participation."""

    if record is None:
        mode = entry.get("record_mode") if entry is not None else None
        if mode == "draft":
            return "draft"
        # No record on disk: nothing can be commissioned, whatever the seed
        # entry calls itself. Reconcile also raises RECORD_MISSING for it.
        return "missing"
    state = record.lifecycle_state.value
    if state == LifecycleState.DRAFT.value:
        return "draft"
    if state == LifecycleState.COMMISSIONING.value:
        return "commissionable" if _is_participant(record) else "prepared"
    return "commissioned"


def _report_operating_compliance(
    record: Any,
    employee_id: str,
    lifecycle_state: str,
    findings: list[dict[str, str]],
    *,
    emit_finding: bool = True,
) -> str:
    """Report, but never repair, an active record that cannot participate."""

    if lifecycle_state not in _OPERATING_STATES:
        return "not_applicable"
    missing = _binding_missing_fields(record)
    if not _has_grant_reference(record):
        missing.append("credential_scope_refs/grant")
    if missing and emit_finding:
        findings.append(
            _finding(
                "BOARD_PARTICIPATION_NONCOMPLIANT",
                "registry record",
                f"operating record for {employee_id} lacks the complete Board "
                "binding/grant; missing "
                + ", ".join(missing)
                + "; a bare board_ref does not establish participation",
            )
        )
    return "noncompliant" if missing else "compliant"


def _disposition_override(mission_dir: Path, observed: Any) -> Any:
    """A mission Auto-Orch has retired is retired in identity terms too (D79/D93)."""
    disposition_path = mission_dir / "mission-disposition.json"
    if not disposition_path.is_file():
        return observed
    try:
        disposition = json.loads(disposition_path.read_text(encoding="utf-8")).get("disposition")
    except (OSError, json.JSONDecodeError, AttributeError):
        return observed
    return LifecycleState.RETIRED if disposition == "retired" else observed


def _deployment_base_lifecycle(
    mission_dir: Path,
    employee_id: str,
    observed: Any,
) -> Any:
    observed = _disposition_override(mission_dir, observed)
    transition_path = mission_dir / "lifecycle-transitions.json"
    if not transition_path.is_file():
        return observed
    try:
        payload = json.loads(transition_path.read_text(encoding="utf-8"))
        transitions = payload["transitions"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RegistryError(
            f"cannot read lifecycle transitions for {employee_id}: {exc}"
        ) from exc
    if not isinstance(transitions, list) or not transitions:
        return observed
    prior: Any | None = None
    base: Any | None = None
    for index, transition in enumerate(transitions):
        if (
            prior is not None
            and isinstance(transition, dict)
            and transition.get("from_state") == LifecycleState.DRAFT.value
        ):
            raise RegistryError(
                f"lifecycle transition {index} for {employee_id} starts at draft; "
                f"expected {prior.value}"
            )
        try:
            parsed = LifecycleTransition.from_dict(transition)
        except (TypeError, ValueError) as exc:
            raise RegistryError(
                f"lifecycle transition {index} for {employee_id} is malformed: {exc}"
            ) from exc
        if parsed.employee_id != employee_id:
            raise RegistryError(
                f"lifecycle transition {index} does not belong to {employee_id}"
            )
        from_state = parsed.from_state
        to_state = parsed.to_state
        if prior is not None and from_state is not prior:
            raise RegistryError(
                f"lifecycle transition {index} for {employee_id} starts at "
                f"{from_state.value}; expected {prior.value}"
            )
        if base is None:
            base = from_state
        prior = to_state
    if prior is not observed:
        raise RegistryError(
            f"lifecycle transition state for {employee_id} is "
            f"{prior.value}; mission state is {observed.value}"
        )
    return base


def _mission_health(state: dict[str, str]) -> str:
    if state.get("last_cycle_outcome", "").lower() == "failed":
        return "failed"
    if state.get("last_run_passed", "").lower() == "true":
        return "healthy"
    return "unknown"


def _parse_roster(path: Path) -> dict[str, dict[str, str | None]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RegistryError(f"cannot read roster {path}: {exc}") from exc

    entries: dict[str, dict[str, str | None]] = {}
    for line in lines:
        if not line.startswith("|") or "|---" in line:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0].lower() in {
            "employee",
            "employee (mission)",
        }:
            continue
        normalized_cells = [cell.strip("`") for cell in cells]
        key = re.sub(r"\*", "", normalized_cells[0]).strip()
        if not key:
            continue
        mission_key = key.split(" (", 1)[0]
        state_text = normalized_cells[2]
        normalized_state: str | None
        upper_state = state_text.upper()
        if "PRE-HIRE" in upper_state:
            normalized_state = LifecycleState.DRAFT.value
        elif "PAUSED" in upper_state:
            normalized_state = LifecycleState.PAUSED.value
        elif "ACTIVE" in upper_state:
            normalized_state = LifecycleState.SCHEDULED.value
        elif "CLOSED" in upper_state or "RETIRED" in upper_state:
            normalized_state = LifecycleState.RETIRED.value
        else:
            normalized_state = None
        workspace = next(
            (cell for cell in normalized_cells[1:] if cell.startswith(("~", "/"))),
            None,
        )
        entries[mission_key] = {
            "state": normalized_state,
            "workspace": workspace,
            "raw_state": state_text,
        }
    return entries


def _record_from_entry(
    entry: dict[str, Any],
    tenant_id: str,
    captured_at: str,
    missions_root: Path,
    provenance_at: str | None = None,
) -> Any:
    record_mode = entry["record_mode"]
    if record_mode == "interactive":
        # No Auto-Orch mission exists or should exist for this operating mode
        # (D1/D2-style two-employee split; see chief-of-staff/decisions.md).
        # Identity is independent of operating mode: an interactive employee
        # is hired, active, and permanently off the autonomy ladder rather
        # than mid-commissioning, so PILOT is the least-wrong existing
        # LifecycleState -- none of the seven mean "will never be scheduled."
        lifecycle_state = LifecycleState.PILOT
        mission_ref = entry["registration_ref"]
    elif record_mode == "commissioning":
        # A prepared-but-not-activated installation. The record carries the
        # complete additive binding (a reference only), but COMMISSIONING is
        # inactive on the Board, so it authorizes no participant yet and holds
        # no credential.
        lifecycle_state = LifecycleState.COMMISSIONING
        mission_ref = entry["registration_ref"]
    else:
        config_path = missions_root / entry["mission_name"] / "config.yaml"
        if record_mode == "live":
            state_path = config_path.parent / "state.md"
            if not config_path.is_file() or not state_path.is_file():
                raise RegistryError(
                    f"mission binding is incomplete for {entry['employee_id']}: "
                    f"{config_path} and {state_path} are both required"
                )
            config = _parse_config(config_path)
            state = _parse_state(state_path)
            _validate_live_config(config, entry["employee_id"])
            lifecycle_state = _deployment_base_lifecycle(
                config_path.parent,
                entry["employee_id"],
                _observed_lifecycle(config, state),
            )
        else:
            lifecycle_state = LifecycleState.DRAFT
        mission_ref = f"mission:{entry['mission_name']}"
    expected_spec_digest = (
        _canonical_body_digest(entry["specification_body"])
        if record_mode == "draft"
        else _source_digest(Path(entry["specification_source"]))
    )
    if expected_spec_digest != entry["specification_digest"]:
        raise RegistryError(
            f"specification digest drift for {entry['employee_id']}: "
            f"seed={entry['specification_digest']} actual={expected_spec_digest}"
        )
    workspace_path = Path(entry["workspace_path"])
    if not workspace_path.is_dir():
        raise RegistryError(
            f"workspace binding is missing for {entry['employee_id']}: {workspace_path}"
        )
    delivery_profile_refs = (
        ()
        if "delivery_profile_ref" not in entry
        else (
            DeliveryProfileReference(
                profile_ref=entry["delivery_profile_ref"],
                schema_version="delivery-profile/1.0",
            ),
        )
    )
    participation = {
        key: entry[key] for key in _PARTICIPATION_ENTRY_KEYS if key in entry
    }
    if participation:
        board_identity = BoardIdentity(
            entry["board_ref"],
            principal_ref=participation["principal_ref"],
            agent_id=participation["agent_id"],
            machine_id=participation["machine_id"],
        )
    else:
        board_identity = BoardIdentity(entry["board_ref"])
    record_timestamp = provenance_at or captured_at
    return DeploymentRecord(
        schema_version="ai-employee-deployment/1.0",
        employee_id=entry["employee_id"],
        tenant_id=tenant_id,
        display_name=entry["display_name"],
        specification=EmployeeSpecReference(
            ref=entry["specification_ref"], digest=entry["specification_digest"]
        ),
        lifecycle_state=lifecycle_state,
        mission_ref=mission_ref,
        workspace_bindings=(
            WorkspaceBinding(
                binding_ref="workspace-primary", workspace_ref=entry["workspace_ref"]
            ),
        ),
        delivery_profile_refs=delivery_profile_refs,
        board_identity=board_identity,
        commissioning_lineage=CommissioningLineage(
            lineage_ref=entry["commissioning_ref"],
            source_ref=entry["commissioning_source_ref"],
            commissioned_by=entry["commissioned_by"],
            commissioned_at=entry["commissioned_at"],
        ),
        principal_refs=tuple(entry["principal_refs"]),
        credential_scope_refs=tuple(entry["credential_scope_refs"]),
        created_by=entry["commissioned_by"],
        created_at=record_timestamp,
        updated_by=entry["commissioned_by"],
        updated_at=record_timestamp,
    )


def _validated_observed_timestamp(value: str | None) -> str:
    """Return an observed UTC timestamp suitable for fresh scoped provenance."""

    timestamp = value or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RegistryError(
            f"observed timestamp must be RFC3339 UTC: {timestamp!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RegistryError(
            f"observed timestamp must be RFC3339 UTC: {timestamp!r}"
        )
    if parsed.utcoffset().total_seconds() != 0:
        raise RegistryError(f"observed timestamp must be RFC3339 UTC: {timestamp!r}")
    return timestamp


def generate_records(
    seed_path: Path = DEFAULT_SEED,
    records_dir: Path = DEFAULT_RECORDS,
    missions_root: Path = DEFAULT_MISSIONS,
    employee_ids: frozenset[str] | None = None,
    observed_at: str | None = None,
) -> list[Path]:
    """Generate canonical deployment records for seed entries.

    ``employee_ids`` narrows generation to the named employees. This is the
    supported scoped path for a reviewable single-record projection: an
    unrelated entry whose live source has drifted cannot block (or silently
    repair) the one record being commissioned.
    """

    seed = load_seed(seed_path)
    if employee_ids is not None:
        unknown = employee_ids - {entry["employee_id"] for entry in seed["entries"]}
        if unknown:
            raise RegistryError(
                "unknown employee_id(s) for generation: " + ", ".join(sorted(unknown))
            )
    # Scoped generation is the reviewable single-instance projection, so it
    # carries fresh observed provenance. This applies to whichever employees are
    # named, not to one hard-coded role. Unscoped generation of the whole
    # registry keeps the seed's captured_at, unchanged.
    scoped_observed_at = (
        _validated_observed_timestamp(observed_at) if employee_ids is not None else None
    )
    records = [
        _record_from_entry(
            entry,
            seed["tenant_id"],
            seed["captured_at"],
            missions_root,
            scoped_observed_at,
        )
        for entry in seed["entries"]
        if employee_ids is None or entry["employee_id"] in employee_ids
    ]
    records_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for record in records:
        destination = records_dir / f"{record.employee_id}.json"
        destination.write_bytes(record.to_bytes())
        written.append(destination)
    return written


def _finding(
    code: str, source: str, message: str, *, kind: str = "drift"
) -> dict[str, str]:
    return {"code": code, "source": source, "message": message, "kind": kind}


def _safe_expected_record(
    entry: dict[str, Any],
    tenant_id: str,
    captured_at: str,
    missions_root: Path,
    findings: list[dict[str, str]],
    source: str,
) -> Any | None:
    """Build the comparison record without letting one entry abort the run.

    ``_record_from_entry`` enforces its own strict invariants (e.g. the
    registry seed's captured specification digest still matching the live
    specification source). Those invariants can legitimately drift out from
    under a long-lived seed even when nothing about *this reconciliation run*
    is wrong, so a violation is reported as a finding rather than raised.
    """

    try:
        return _record_from_entry(entry, tenant_id, captured_at, missions_root)
    except RegistryError as exc:
        findings.append(_finding("RECORD_RECONCILIATION_ERROR", source, str(exc)))
        return None


def _stable_record_body(record: Any) -> dict[str, Any]:
    body = _binding_view(record)
    body.pop("lifecycle_state", None)
    return body


def reconcile(
    seed_path: Path = DEFAULT_SEED,
    records_dir: Path = DEFAULT_RECORDS,
    missions_root: Path = DEFAULT_MISSIONS,
    roster_path: Path = DEFAULT_ROSTER,
) -> dict[str, Any]:
    seed = load_seed(seed_path)
    roster = _parse_roster(roster_path)
    entries = {entry["employee_id"]: entry for entry in seed["entries"]}
    records: dict[str, Any] = {}
    findings_by_employee: dict[str, list[dict[str, str]]] = {}
    effective_states: dict[str, str] = {}
    board_compliance: dict[str, str] = {}
    invalid_record_ids: set[str] = set()
    duplicate_values: dict[str, list[str]] = {
        "employee_id": [],
        "mission_ref": [],
        "workspace_ref": [],
    }
    for path in sorted(records_dir.glob("*.json")):
        raw = path.read_bytes()
        try:
            record = DeploymentRecord.from_json(raw)
        except DeploymentError as exc:
            try:
                raw_body = json.loads(raw)
            except json.JSONDecodeError:
                raw_body = None
            employee_id = (
                raw_body.get("employee_id")
                if isinstance(raw_body, dict) and isinstance(raw_body.get("employee_id"), str)
                else None
            ) or path.stem
            invalid_record_ids.add(employee_id)
            findings_by_employee.setdefault(employee_id, []).append(
                _finding("invalid-deployment-record", str(path), str(exc))
            )
            continue
        if record.employee_id in records:
            duplicate_values["employee_id"].append(record.employee_id)
        records[record.employee_id] = record
        findings = findings_by_employee.setdefault(record.employee_id, [])
        if raw != record.to_bytes():
            findings.append(
                _finding(
                    "NON_CANONICAL_RECORD",
                    str(path),
                    "deployment record bytes do not match canonical serialization",
                )
            )
        for field, value in (
            ("mission_ref", record.mission_ref),
            ("workspace_ref", record.workspace_bindings[0].workspace_ref),
        ):
            duplicate_values.setdefault(field, [])
            previous = [
                other.employee_id
                for other in records.values()
                if other.employee_id != record.employee_id
                and (
                    getattr(other, field) == value
                    if field == "mission_ref"
                    else other.workspace_bindings[0].workspace_ref == value
                )
            ]
            if previous:
                duplicate_values[field].extend([record.employee_id, *previous])

    for employee_id in sorted(set(records) - set(entries)):
        findings_by_employee[employee_id].append(
            _finding(
                "UNSEEDED_RECORD",
                "registry",
                "deployment record is not present in registry seed",
            )
        )

    for employee_id, entry in entries.items():
        findings = findings_by_employee.setdefault(employee_id, [])
        record = records.get(employee_id)
        if record is None:
            findings.append(
                _finding("RECORD_MISSING", "registry", "no deployment record exists")
            )
            continue
        # Legacy operating records are classified and marked noncompliant
        # without rewriting their bytes. The live record mode additionally
        # emits the detailed reconciliation finding; older interactive and
        # prepared projections retain their historical finding surface.
        board_compliance[employee_id] = _report_operating_compliance(
            record,
            employee_id,
            record.lifecycle_state.value,
            findings,
            emit_finding=entry["record_mode"] == "live",
        )
        if entry["record_mode"] == "draft":
            effective_states[employee_id] = LifecycleState.DRAFT.value
            expected_record = _safe_expected_record(
                entry,
                seed["tenant_id"],
                seed["captured_at"],
                missions_root,
                findings,
                "registry/seed and draft specification",
            )
            if (
                expected_record is not None
                and _binding_view(record) != _binding_view(expected_record)
            ):
                findings.append(
                    _finding(
                        "RECORD_BINDING_DRIFT",
                        "registry/seed and draft specification",
                        "draft record fields do not match the Factory seed",
                    )
                )
            if record.lifecycle_state is not LifecycleState.DRAFT:
                findings.append(
                    _finding(
                        "DRAFT_LIFECYCLE_DRIFT",
                        "registry record",
                        "draft registry entry must remain in lifecycle state draft",
                    )
                )
            if not Path(entry["workspace_path"]).is_dir():
                findings.append(
                    _finding(
                        "WORKSPACE_MISSING",
                        "filesystem",
                        entry["workspace_path"],
                    )
                )
            actual_spec_digest = _canonical_body_digest(entry["specification_body"])
            if actual_spec_digest != record.specification.digest:
                findings.append(
                    _finding(
                        "SPECIFICATION_DIGEST_DRIFT",
                        entry["specification_source"],
                        f"record={record.specification.digest} live={actual_spec_digest}",
                    )
                )
            expected_mission = f"mission:{entry['mission_name']}"
            if record.mission_ref != expected_mission:
                findings.append(
                    _finding(
                        "MISSION_REF_DRIFT",
                        "registry/seed",
                        f"record={record.mission_ref} expected={expected_mission}",
                    )
                )
            continue
        if entry["record_mode"] == "interactive":
            effective_states[employee_id] = LifecycleState.PILOT.value
            expected_record = _safe_expected_record(
                entry,
                seed["tenant_id"],
                seed["captured_at"],
                missions_root,
                findings,
                "registry/seed and interactive specification",
            )
            if expected_record is not None and _binding_view(record) != _binding_view(expected_record):
                findings.append(
                    _finding(
                        "RECORD_BINDING_DRIFT",
                        "registry/seed and interactive specification",
                        "interactive record fields do not match the Factory seed",
                    )
                )
            if record.lifecycle_state is not LifecycleState.PILOT:
                findings.append(
                    _finding(
                        "INTERACTIVE_LIFECYCLE_DRIFT",
                        "registry record",
                        "interactive registry entry must remain in lifecycle state pilot",
                    )
                )
            if not Path(entry["workspace_path"]).is_dir():
                findings.append(
                    _finding(
                        "WORKSPACE_MISSING",
                        "filesystem",
                        entry["workspace_path"],
                    )
                )
            actual_spec_digest = _source_digest(Path(entry["specification_source"]))
            if actual_spec_digest != record.specification.digest:
                findings.append(
                    _finding(
                        "SPECIFICATION_DIGEST_DRIFT",
                        entry["specification_source"],
                        f"record={record.specification.digest} live={actual_spec_digest}",
                    )
                )
            if record.mission_ref != entry["registration_ref"]:
                findings.append(
                    _finding(
                        "MISSION_REF_DRIFT",
                        "registry/seed",
                        f"record={record.mission_ref} "
                        f"expected={entry['registration_ref']}",
                    )
                )
            continue
        if entry["record_mode"] == "commissioning":
            effective_states[employee_id] = LifecycleState.COMMISSIONING.value
            expected_record = _safe_expected_record(
                entry,
                seed["tenant_id"],
                seed["captured_at"],
                missions_root,
                findings,
                "registry/seed and commissioning specification",
            )
            if expected_record is not None and _binding_view(record) != _binding_view(
                expected_record
            ):
                findings.append(
                    _finding(
                        "RECORD_BINDING_DRIFT",
                        "registry/seed and commissioning specification",
                        "commissioning record fields do not match the Factory seed",
                    )
                )
            if record.lifecycle_state is not LifecycleState.COMMISSIONING:
                findings.append(
                    _finding(
                        "COMMISSIONING_LIFECYCLE_DRIFT",
                        "registry record",
                        "commissioning registry entry must remain in lifecycle "
                        "state commissioning",
                    )
                )
            if not Path(entry["workspace_path"]).is_dir():
                findings.append(
                    _finding(
                        "WORKSPACE_MISSING",
                        "filesystem",
                        entry["workspace_path"],
                    )
                )
            actual_spec_digest = _source_digest(Path(entry["specification_source"]))
            if actual_spec_digest != record.specification.digest:
                findings.append(
                    _finding(
                        "SPECIFICATION_DIGEST_DRIFT",
                        entry["specification_source"],
                        f"record={record.specification.digest} live={actual_spec_digest}",
                    )
                )
            if record.mission_ref != entry["registration_ref"]:
                findings.append(
                    _finding(
                        "MISSION_REF_DRIFT",
                        "registry/seed",
                        f"record={record.mission_ref} "
                        f"expected={entry['registration_ref']}",
                    )
                )
            continue
        mission_dir = missions_root / entry["mission_name"]
        config_path = mission_dir / "config.yaml"
        state_path = mission_dir / "state.md"
        if not config_path.is_file() or not state_path.is_file():
            findings.append(
                _finding(
                    "MISSION_SOURCE_MISSING",
                    "auto-orch",
                    f"mission sources missing under {mission_dir}",
                )
            )
            continue
        config = _parse_config(config_path)
        state = _parse_state(state_path)
        observed_state = _disposition_override(mission_dir, _observed_lifecycle(config, state)).value
        effective_states[employee_id] = observed_state
        if record.lifecycle_state.value not in _OPERATING_STATES:
            board_compliance[employee_id] = _report_operating_compliance(
                record,
                employee_id,
                observed_state,
                findings,
                emit_finding=True,
            )
        expected_record = _safe_expected_record(
            entry,
            seed["tenant_id"],
            seed["captured_at"],
            missions_root,
            findings,
            "registry/seed and live mission sources",
        )
        if expected_record is not None and _stable_record_body(
            record
        ) != _stable_record_body(expected_record):
            findings.append(
                _finding(
                    "RECORD_BINDING_DRIFT",
                    "registry/seed and live mission sources",
                    "record fields do not match the Factory seed and live binding facts",
                )
            )
        if (
            expected_record is not None
            and record.lifecycle_state is not expected_record.lifecycle_state
        ):
            findings.append(
                _finding(
                    "MISSION_LIFECYCLE_DRIFT",
                    "auto-orch lifecycle-transitions.json/state.md/config.yaml",
                    "record base="
                    f"{record.lifecycle_state.value} expected base="
                    f"{expected_record.lifecycle_state.value} live={observed_state}",
                )
            )
        if config.get("workspace") != entry["workspace_path"]:
            findings.append(
                _finding(
                    "MISSION_WORKSPACE_DRIFT",
                    "auto-orch config.yaml",
                    f"seed={entry['workspace_path']} live={config.get('workspace', '<missing>')}",
                )
            )
        if not Path(entry["workspace_path"]).is_dir():
            findings.append(
                _finding(
                    "WORKSPACE_MISSING",
                    "filesystem",
                    entry["workspace_path"],
                )
            )
        expected_mission = f"mission:{entry['mission_name']}"
        if record.mission_ref != expected_mission:
            findings.append(
                _finding(
                    "MISSION_REF_DRIFT",
                    "registry/seed",
                    f"record={record.mission_ref} expected={expected_mission}",
                )
            )
        board_machine_id = config.get("board.machine_id")
        expected_board = entry["board_ref"].split(":", 1)[-1]
        if config.get("board.enabled", "").lower() != "true":
            findings.append(
                _finding(
                    "BOARD_DISABLED",
                    "auto-orch config.yaml",
                    "Board reporting is disabled",
                )
            )
        if board_machine_id != expected_board:
            findings.append(
                _finding(
                    "BOARD_IDENTITY_DRIFT",
                    "auto-orch config.yaml",
                    f"record={expected_board} live={board_machine_id or '<missing>'}",
                )
            )
        actual_spec_digest = _source_digest(Path(entry["specification_source"]))
        if actual_spec_digest != record.specification.digest:
            findings.append(
                _finding(
                    "SPECIFICATION_DIGEST_DRIFT",
                    entry["specification_source"],
                    f"record={record.specification.digest} live={actual_spec_digest}",
                )
            )
        roster_entry = roster.get(entry["roster_key"])
        if roster_entry is None:
            findings.append(
                _finding(
                    "ROSTER_ENTRY_MISSING", "hiring/roster.md", entry["roster_key"]
                )
            )
        else:
            if (
                roster_entry["state"] is not None
                and roster_entry["state"] != observed_state
            ):
                findings.append(
                    _finding(
                        "ROSTER_PROSE_DRIFT",
                        "hiring/roster.md",
                        f"roster={roster_entry['raw_state']} live={observed_state}",
                    )
                )
            roster_workspace = roster_entry["workspace"]
            if roster_workspace is not None:
                normalized_roster_workspace = str(Path(roster_workspace).expanduser())
                if normalized_roster_workspace != entry["workspace_path"]:
                    findings.append(
                        _finding(
                            "ROSTER_WORKSPACE_DRIFT",
                            "hiring/roster.md",
                            f"roster={roster_workspace} seed={entry['workspace_path']}",
                        )
                    )
            else:
                findings.append(
                    _finding(
                        "ROSTER_WORKSPACE_MISSING",
                        "hiring/roster.md",
                        "roster row has no concrete workspace binding",
                    )
                )
        if _mission_health(state) == "failed":
            findings.append(
                _finding(
                    "MISSION_HEALTH_ATTENTION",
                    "auto-orch state.md",
                    "latest recorded mission outcome is failed",
                    kind="attention",
                )
            )

    duplicate_report = {
        field: sorted(set(values))
        for field, values in duplicate_values.items()
        if values
    }
    record_reports = []
    for employee_id in sorted(set(entries) | set(records) | invalid_record_ids):
        record = records.get(employee_id)
        entry = entries.get(employee_id)
        record_reports.append(
            {
                "employee_id": employee_id,
                "record_mode": entry.get("record_mode") if entry is not None else None,
                "classification": _record_classification(record, entry),
                "board_compliance": board_compliance.get(
                    employee_id, "not_applicable"
                ),
                "board_participation": (
                    _is_participant(record) if record is not None else False
                ),
                "mission_name": (
                    entry.get("mission_name", entry.get("registration_ref"))
                    if entry is not None
                    else (record.mission_ref if record is not None else None)
                ),
                "lifecycle_state": effective_states.get(
                    employee_id,
                    record.lifecycle_state.value if record is not None else None,
                ),
                "record_lifecycle_state": (
                    record.lifecycle_state.value if record is not None else None
                ),
                "findings": sorted(
                    findings_by_employee.get(employee_id, []),
                    key=lambda finding: (finding["kind"], finding["code"]),
                ),
            }
        )
    drifted = sum(
        any(finding["kind"] == "drift" for finding in report["findings"])
        for report in record_reports
    )
    attention = sum(
        any(finding["kind"] == "attention" for finding in report["findings"])
        for report in record_reports
    )
    return {
        "schema_version": _RECONCILIATION_VERSION,
        "tenant_id": seed["tenant_id"],
        "authority": "live mission config/state plus explicit draft seed records; roster prose is reconciled and never auto-corrected",
        "records": record_reports,
        "duplicates": duplicate_report,
        "summary": {
            "records_checked": len(record_reports),
            "records_with_drift": drifted,
            "records_needing_attention": attention,
            "duplicate_binding_sets": len(duplicate_report),
        },
    }


_ACTIVATION_TARGETS = frozenset({"pilot", "scheduled"})
_ACTIVATION_SCHEMA_VERSION = "ai-employee-lifecycle-activation/1.0"
# One canonical record file per employee id. The slug rule keeps a declared
# employee id from naming a path component, another directory, or a dotfile.
_EMPLOYEE_ID_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _canonical_json_bytes(body: Any) -> bytes:
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _validate_instance_binding(record: Any, employee_id: str) -> None:
    """Require the requested employee instance and its complete participant binding.

    This is the generic replacement for the former Clerk-only gate. It pins no
    employee id of its own: the caller names one employee, and the canonical
    record must be exactly that employee with a complete, internally distinct
    Board binding. A partial identity (for example ``board_ref`` alone, the
    prepared-but-unbound shape) fails closed here rather than producing a
    half-bound participant.
    """

    if record.employee_id != employee_id:
        raise RegistryError(
            f"canonical record is for {record.employee_id!r}, not the requested "
            f"{employee_id!r}"
        )
    missing = _binding_missing_fields(record)
    if missing:
        raise RegistryError(
            "commissioning record is missing the complete Board participation binding: "
            + ", ".join(missing)
        )
    if not _has_grant_reference(record):
        raise RegistryError(
            "commissioning record has no credential-scope/grant reference; activation "
            "into an operating state requires one (a bare Board binding is not participation)"
        )


def activate_employee_instance(
    *,
    records_dir: Path = DEFAULT_RECORDS,
    employee_id: str,
    to_state: Any = "pilot",
    actor: str,
    reason: str,
    at: str | None = None,
    evidence_out: Path | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    """Human/manager-gated lifecycle transition of ONE named commissioning record.

    This is the narrow generic instance transition: there is no
    ``activate_<employee>`` function and no employee-specific constant. It
    validates the requested employee identity and its complete binding,
    requires the commissioning state, applies one legal transition to an active
    state, writes the activated record canonically, and records prior
    digest/byte evidence so history is preserved. Default is a dry-run plan.
    """

    deployment = importlib.import_module("ai_employee.deployment")
    _text(employee_id, "activation.employee_id")
    if not _EMPLOYEE_ID_SLUG.match(employee_id):
        raise RegistryError(
            f"activation employee_id must be a lowercase hyphenated slug: {employee_id!r}"
        )
    _text(actor, "activation.actor")
    _text(reason, "activation.reason")

    record_path = records_dir / f"{employee_id}.json"
    if not record_path.is_file():
        raise RegistryError(f"canonical record not found: {record_path}")
    raw = record_path.read_bytes()
    try:
        record = DeploymentRecord.from_json(raw)
    except DeploymentError as exc:
        raise RegistryError(f"canonical record is invalid: {exc}") from exc
    if raw != record.to_bytes():
        raise RegistryError(
            "canonical record bytes are not canonical; refusing to activate"
        )
    _validate_instance_binding(record, employee_id)
    if record.lifecycle_state is not LifecycleState.COMMISSIONING:
        raise RegistryError(
            "activation requires the commissioning state, got "
            f"{record.lifecycle_state.value}"
        )

    try:
        target = to_state if isinstance(to_state, LifecycleState) else LifecycleState(to_state)
    except (TypeError, ValueError) as exc:
        raise RegistryError(f"unknown activation target: {to_state!r}") from exc
    if target.value not in _ACTIVATION_TARGETS:
        raise RegistryError("activation target must be pilot or scheduled")
    if not deployment.is_legal_transition(record.lifecycle_state, target):
        raise RegistryError(
            f"illegal lifecycle transition {record.lifecycle_state.value} -> {target.value}"
        )

    timestamp = at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated, transition = record.transition(
        target, actor=actor, reason=reason, timestamp=timestamp
    )
    prior = {
        "lifecycle_state": record.lifecycle_state.value,
        "record_digest": record.digest,
        "bytes_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "board_identity": record.board_identity.to_dict(),
    }
    result: dict[str, Any] = {
        "employee_id": employee_id,
        "from": record.lifecycle_state.value,
        "to": updated.lifecycle_state.value,
        "actor": actor,
        "reason": reason,
        "timestamp": timestamp,
        "prior_digest": prior["record_digest"],
        "new_digest": updated.digest,
        "transition": transition.to_dict(),
        "record_path": str(record_path),
        "evidence_path": str(
            evidence_out
            or records_dir.parent / "evidence" / f"{employee_id}.activation.json"
        ),
        "executed": bool(execute),
    }
    if not execute:
        return result

    evidence = {
        "schema_version": _ACTIVATION_SCHEMA_VERSION,
        "employee_id": employee_id,
        "activated_by": actor,
        "reason": reason,
        "activated_at": timestamp,
        "prior": prior,
        "transition": transition.to_dict(),
        "new_record_digest": updated.digest,
    }
    evidence_path = Path(result["evidence_path"])
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_bytes(_canonical_json_bytes(evidence))

    tmp = record_path.with_name(record_path.name + ".tmp")
    tmp.write_bytes(updated.to_bytes())
    os.replace(tmp, record_path)
    return result


def _write_canonical_json(path: Path, body: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_json_bytes(body))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("generate", "reconcile"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
        subparser.add_argument("--records-dir", type=Path, default=DEFAULT_RECORDS)
        subparser.add_argument("--missions-root", type=Path, default=DEFAULT_MISSIONS)
    subparsers.choices["generate"].add_argument(
        "--employee",
        action="append",
        default=None,
        help="limit generation to the named employee_id(s); repeatable",
    )
    subparsers.choices["generate"].add_argument(
        "--observed-at",
        default=None,
        help="fresh observed UTC timestamp for a scoped single-employee record",
    )
    reconcile_parser = subparsers.choices["reconcile"]
    reconcile_parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
    reconcile_parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    activate_parser = subparsers.add_parser(
        "activate",
        help=(
            "human/manager-gated lifecycle activation of one named canonical "
            "commissioning record"
        ),
    )
    activate_parser.add_argument("--records-dir", type=Path, default=DEFAULT_RECORDS)
    activate_parser.add_argument(
        "--employee",
        required=True,
        help="employee_id of the canonical commissioning record to activate",
    )
    activate_parser.add_argument("--to", default="pilot", choices=sorted(_ACTIVATION_TARGETS))
    activate_parser.add_argument("--actor", required=True)
    activate_parser.add_argument("--reason", required=True)
    activate_parser.add_argument("--at", default=None, help="RFC3339 activation timestamp")
    activate_parser.add_argument("--evidence-out", type=Path, default=None)
    activate_parser.add_argument(
        "--execute", action="store_true", help="write the record and evidence (default: dry-run)"
    )
    args = parser.parse_args(argv)

    if args.command == "activate":
        result = activate_employee_instance(
            records_dir=args.records_dir,
            employee_id=args.employee,
            to_state=args.to,
            actor=args.actor,
            reason=args.reason,
            at=args.at,
            evidence_out=args.evidence_out,
            execute=args.execute,
        )
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0

    if args.command == "generate":
        employee_ids = (
            None if not args.employee else frozenset(args.employee)
        )
        paths = generate_records(
            args.seed,
            args.records_dir,
            args.missions_root,
            employee_ids,
            args.observed_at,
        )
        print(json.dumps({"generated": [str(path) for path in paths]}, sort_keys=True))
        return 0

    report = reconcile(args.seed, args.records_dir, args.missions_root, args.roster)
    _write_canonical_json(args.output, report)
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
