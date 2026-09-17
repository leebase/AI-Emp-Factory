"""``employee-foundation/1.0`` — composition, not a fifth part.

Source of truth: ``/home/lee/projects/chief-of-staff/docs/employee-foundation-1.0.md``
(D171 item 4, DRAFT at the time this module was written). That page names four
parts that already exist and already work — the behavioral contract
(``Employee``/``EmployeeSpec``, this package), the deployment record (also this
package), the governance spine (``employee_contract.ticket.HireTicket``,
``employee-contract`` repo), and the identity file set (``employee-pattern/1.0``,
proven by the Chief of Staff) — and says which part owns which concern.

This module does **not** introduce a new base class, persona, or runtime (per
the 08-11 ruling the contract page cites). It does two things:

* ``validate_foundation`` checks that one employee's workspace + deployment
  record actually compose correctly per the contract page's seven rules.
* ``scaffold_employee`` lays down the file layout from "The scaffold" section
  of the contract page, with placeholder content a human must fill in before
  the scaffold will pass validation.

Every existing ``Employee``/``EmployeeSpec``/``DeploymentRecord`` behavior is
untouched; this module only reads them.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .contract import EmployeeSpec, InputSpec
from .deployment import (
    EMPLOYEE_SPEC_SCHEMA_VERSION,
    DeploymentError,
    DeploymentRecord,
    employee_spec_digest,
)
from .envelope import DegradationBehavior

FOUNDATION_SCHEMA_VERSION = "employee-foundation/1.0"

#: Read-only reference used only as a fallback when ``employee_contract`` is
#: not already importable (e.g. not pip-installed in this interpreter).
_EMPLOYEE_CONTRACT_SRC = Path("/home/lee/projects/employee-contract/src")

_IDENTITY_FILES: tuple[str, ...] = (
    "charter.md",
    "AGENTS.md",
    "personality.md",
    "decisions.md",
    "decisions-index.md",
)
_MEMORY_SUBDIRS: tuple[str, ...] = ("relationship", "preference", "lesson", "episodic")
_CHARTER_SECTIONS: tuple[str, ...] = (
    "## Role",
    "## Responsibilities",
    "## Authority",
    "## Escalation",
    "## Provenance",
)
_BUDGET_FIELDS: tuple[str, ...] = (
    "max_usd_per_run",
    "max_usd_per_month",
    "max_seconds_per_run",
)


class FoundationError(ValueError):
    """Raised for I/O problems (missing workspace) and scaffold refusals.

    Never raised by :func:`validate_foundation` for a content problem — those
    become :class:`Finding` entries instead, per the fail-closed-with-findings
    contract this module implements.
    """


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FoundationLayout:
    """The ``employee-foundation/1.0`` scaffold layout for one employee.

    Mirrors "The scaffold" in the contract page exactly. The deployment
    record lives outside the workspace — the Factory owns concrete records
    under ``ai-employee-factory/hiring/registry/records/<id>.json`` — so its
    path is carried here rather than derived.
    """

    employee_id: str
    workspace: Path
    deployment_record: Path

    @property
    def charter_path(self) -> Path:
        return self.workspace / "charter.md"

    @property
    def agents_path(self) -> Path:
        return self.workspace / "AGENTS.md"

    @property
    def personality_path(self) -> Path:
        return self.workspace / "personality.md"

    @property
    def decisions_path(self) -> Path:
        return self.workspace / "decisions.md"

    @property
    def decisions_index_path(self) -> Path:
        return self.workspace / "decisions-index.md"

    @property
    def journal_dir(self) -> Path:
        return self.workspace / "journal"

    @property
    def memory_dir(self) -> Path:
        return self.workspace / "memory"

    @property
    def memory_subdirs(self) -> tuple[Path, ...]:
        return tuple(self.memory_dir / sub for sub in _MEMORY_SUBDIRS)

    @property
    def hire_ticket_path(self) -> Path:
        return self.workspace / "hire-ticket.json"

    @property
    def spec_dir(self) -> Path:
        return self.workspace / "spec"

    @property
    def spec_path(self) -> Path:
        return self.spec_dir / f"{self.employee_id}.json"

    @property
    def evidence_dir(self) -> Path:
        return self.workspace / "evidence"

    def all_paths(self) -> tuple[Path, ...]:
        """Every path the scaffold writes or creates, in write order."""

        return (
            self.charter_path,
            self.agents_path,
            self.personality_path,
            self.decisions_path,
            self.decisions_index_path,
            self.journal_dir,
            *self.memory_subdirs,
            self.hire_ticket_path,
            self.spec_path,
            self.evidence_dir,
        )


# ---------------------------------------------------------------------------
# Findings / report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One fail-closed validation finding."""

    severity: str  # "error" | "warning" | "info"
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


@dataclass
class FoundationReport:
    """Result of :func:`validate_foundation`."""

    ok: bool
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": FOUNDATION_SCHEMA_VERSION,
            "ok": self.ok,
            "findings": [item.to_dict() for item in self.findings],
        }


# ---------------------------------------------------------------------------
# Minimal, permissive EmployeeSpec JSON loader
#
# There is no existing JSON -> EmployeeSpec loader in ai_employee (only
# EmployeeSpec -> canonical dict, via deployment.employee_spec_body, used for
# digesting). This mirrors that canonical shape in reverse. It is
# deliberately permissive about *extra* top-level keys (e.g. a stray
# "budget") because rule (e) below reports those as a named finding in their
# own right; a strict "unknown field" parse error would swallow that finding
# under a generic parse failure instead.
# ---------------------------------------------------------------------------


def _load_input_spec(item: Any, index: int) -> InputSpec:
    if not isinstance(item, Mapping):
        raise FoundationError(f"spec.inputs[{index}] must be an object")
    name = item.get("name")
    artifact_type = item.get("artifact_type")
    if not isinstance(name, str) or not name.strip():
        raise FoundationError(f"spec.inputs[{index}].name must be a non-empty string")
    if not isinstance(artifact_type, str) or not artifact_type.strip():
        raise FoundationError(
            f"spec.inputs[{index}].artifact_type must be a non-empty string"
        )
    try:
        on_deficient = DegradationBehavior(
            item.get("on_deficient", DegradationBehavior.REFUSE_AND_ESCALATE.value)
        )
        on_absent = DegradationBehavior(
            item.get("on_absent", DegradationBehavior.REFUSE_AND_ESCALATE.value)
        )
    except ValueError as exc:
        raise FoundationError(
            f"spec.inputs[{index}] has an invalid degradation behavior"
        ) from exc
    min_confidence = item.get("min_confidence", 0.0)
    if not isinstance(min_confidence, (int, float)) or isinstance(min_confidence, bool):
        raise FoundationError(f"spec.inputs[{index}].min_confidence must be a number")
    schema_version = item.get("schema_version")
    if schema_version is not None and (
        not isinstance(schema_version, str) or not schema_version.strip()
    ):
        raise FoundationError(
            f"spec.inputs[{index}].schema_version must be a non-empty string"
        )
    return InputSpec(
        name=name,
        artifact_type=artifact_type,
        required=bool(item.get("required", True)),
        on_deficient=on_deficient,
        on_absent=on_absent,
        min_confidence=float(min_confidence),
        requires_independent_provider=bool(
            item.get("requires_independent_provider", False)
        ),
        schema_version=schema_version,
    )


def load_employee_spec(body: Any) -> EmployeeSpec:
    """Parse a spec/<id>.json body into an :class:`EmployeeSpec`.

    Extra top-level keys are ignored here on purpose — see the module note
    above. Raises :class:`FoundationError` on a structural problem; callers
    inside :func:`validate_foundation` catch this and turn it into a finding
    rather than letting it propagate.
    """

    if not isinstance(body, Mapping):
        raise FoundationError("spec must be a JSON object")
    schema_version = body.get("schema_version")
    if schema_version != EMPLOYEE_SPEC_SCHEMA_VERSION:
        raise FoundationError(
            f"unsupported EmployeeSpec schema_version: {schema_version!r}"
        )
    employee_id = body.get("employee_id")
    name = body.get("name")
    produces = body.get("produces")
    if not isinstance(employee_id, str) or not employee_id.strip():
        raise FoundationError("spec.employee_id must be a non-empty string")
    if not isinstance(name, str) or not name.strip():
        raise FoundationError("spec.name must be a non-empty string")
    if not isinstance(produces, str) or not produces.strip():
        raise FoundationError("spec.produces must be a non-empty string")
    inputs_raw = body.get("inputs", [])
    if not isinstance(inputs_raw, list):
        raise FoundationError("spec.inputs must be an array")
    inputs = tuple(
        _load_input_spec(item, index) for index, item in enumerate(inputs_raw)
    )
    reads_knowledge_layer = bool(body.get("reads_knowledge_layer", False))
    escalation_target = body.get("escalation_target", "human:proposal-manager")
    if not isinstance(escalation_target, str) or not escalation_target.strip():
        raise FoundationError("spec.escalation_target must be a non-empty string")
    output_schema_version = body.get("output_schema_version")
    if output_schema_version is not None and (
        not isinstance(output_schema_version, str) or not output_schema_version.strip()
    ):
        raise FoundationError("spec.output_schema_version must be a non-empty string")
    return EmployeeSpec(
        employee_id=employee_id,
        name=name,
        produces=produces,
        inputs=inputs,
        reads_knowledge_layer=reads_knowledge_layer,
        escalation_target=escalation_target,
        output_schema_version=output_schema_version,
    )


# ---------------------------------------------------------------------------
# employee_contract import (read-only; never modifies that repo)
# ---------------------------------------------------------------------------


def _import_hire_ticket_cls() -> Any | None:
    try:
        from employee_contract.ticket import HireTicket

        return HireTicket
    except ImportError:
        pass
    if _EMPLOYEE_CONTRACT_SRC.is_dir():
        src = str(_EMPLOYEE_CONTRACT_SRC)
        if src not in sys.path:
            sys.path.insert(0, src)
        try:
            from employee_contract.ticket import HireTicket

            return HireTicket
        except ImportError:
            return None
    return None


def _check_budget_complete(
    budget_raw: Any, path: Path, findings: list[Finding]
) -> None:
    """Foundation-level policy: a hire ticket's budget must be fully declared.

    ``employee_contract.ticket.BudgetDeclaration`` itself treats all three
    fields as optional (a ticket with no cost ceiling is a legal governance
    object — e.g. some read-only static rungs). The foundation scaffold is
    stricter: rule 3 of the contract page ("Budget lives in the ticket, is
    enforced by the engine") and deliverable 2's explicit requirement that a
    freshly scaffolded employee (budget fields ``null``) FAILS validation
    until a human fills them in, only holds if *this* layer enforces
    completeness — the ticket schema alone will not. This is a documented
    foundation policy on top of the ticket schema, not a reinterpretation of
    it; see docs/employee-foundation-1.0.md in this repo.
    """

    if not isinstance(budget_raw, Mapping):
        findings.append(
            Finding(
                "error",
                "hire-ticket-budget-incomplete",
                str(path),
                "budget must be an object declaring max_usd_per_run, "
                "max_usd_per_month, and max_seconds_per_run",
            )
        )
        return
    for field_name in _BUDGET_FIELDS:
        value = budget_raw.get(field_name)
        is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
        if not is_number or not math.isfinite(value) or value <= 0:
            findings.append(
                Finding(
                    "error",
                    "hire-ticket-budget-incomplete",
                    str(path),
                    f"budget.{field_name} must be a finite positive number, "
                    f"got {value!r}",
                )
            )


def _validate_hire_ticket_minimally(
    body: Mapping[str, Any], path: Path, findings: list[Finding]
) -> None:
    employee_id = body.get("employee_id")
    if not isinstance(employee_id, str) or not employee_id.strip():
        findings.append(
            Finding(
                "error",
                "hire-ticket-invalid",
                str(path),
                "employee_id must be a non-empty string",
            )
        )
    verifiability_test = body.get("verifiability_test")
    if not (
        isinstance(verifiability_test, dict)
        or (isinstance(verifiability_test, str) and verifiability_test.strip())
    ):
        findings.append(
            Finding(
                "error",
                "hire-ticket-invalid",
                str(path),
                "verifiability_test must be a non-empty object or string",
            )
        )


# ---------------------------------------------------------------------------
# validate_foundation
# ---------------------------------------------------------------------------


def _sole_spec_employee_id(spec_dir: Path) -> str | None:
    if not spec_dir.is_dir():
        return None
    candidates = sorted(spec_dir.glob("*.json"))
    if len(candidates) != 1:
        return None
    return candidates[0].stem


def validate_foundation(workspace: Path, deployment_record: Path) -> FoundationReport:
    """Validate one employee's workspace + deployment record composition.

    Fail-closed, with per-check :class:`Finding` entries. Raises
    :class:`FoundationError` only for I/O of a missing *workspace* — every
    other problem (missing deployment record, malformed JSON, digest
    mismatch, missing identity files, ...) is reported as a finding.
    """

    workspace = Path(workspace)
    deployment_record = Path(deployment_record)
    if not workspace.is_dir():
        raise FoundationError(
            f"workspace does not exist or is not a directory: {workspace}"
        )

    findings: list[Finding] = []

    # -- (b) deployment record --------------------------------------------
    deployment: DeploymentRecord | None = None
    try:
        deployment = DeploymentRecord.from_json(deployment_record.read_bytes())
    except OSError as exc:
        findings.append(
            Finding(
                "error",
                "deployment-record-missing",
                str(deployment_record),
                str(exc),
            )
        )
    except DeploymentError as exc:
        findings.append(
            Finding(
                "error",
                "deployment-record-invalid",
                str(deployment_record),
                str(exc),
            )
        )

    employee_id = (
        deployment.employee_id
        if deployment is not None
        else _sole_spec_employee_id(workspace / "spec")
    )
    if employee_id is None:
        findings.append(
            Finding(
                "error",
                "employee-id-undetermined",
                str(workspace / "spec"),
                "cannot determine employee_id: deployment record did not "
                "parse and spec/ does not contain exactly one <id>.json",
            )
        )

    # -- (a) spec/<id>.json --------------------------------------------
    spec_path = workspace / "spec" / f"{employee_id}.json" if employee_id else None
    spec_body_raw: Any = None
    spec: EmployeeSpec | None = None
    if spec_path is None:
        pass
    elif not spec_path.is_file():
        findings.append(
            Finding("error", "spec-missing", str(spec_path), "spec file not found")
        )
    else:
        try:
            spec_body_raw = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            findings.append(
                Finding("error", "spec-parse-error", str(spec_path), str(exc))
            )
        else:
            try:
                spec = load_employee_spec(spec_body_raw)
            except FoundationError as exc:
                findings.append(
                    Finding("error", "spec-parse-error", str(spec_path), str(exc))
                )

    if spec is not None and deployment is not None:
        digest = employee_spec_digest(spec)
        if digest != deployment.specification.digest:
            findings.append(
                Finding(
                    "error",
                    "spec-digest-mismatch",
                    str(spec_path),
                    f"spec digest {digest} does not match deployment "
                    f"record specification.digest "
                    f"{deployment.specification.digest}",
                )
            )
        if spec.employee_id != deployment.employee_id:
            findings.append(
                Finding(
                    "error",
                    "employee-id-mismatch",
                    str(spec_path),
                    f"spec.employee_id {spec.employee_id!r} != deployment "
                    f"employee_id {deployment.employee_id!r}",
                )
            )

    # -- (c) hire-ticket.json ----------------------------------------------
    ticket_path = workspace / "hire-ticket.json"
    ticket_body_raw: Any = None
    if not ticket_path.is_file():
        findings.append(
            Finding(
                "error", "hire-ticket-missing", str(ticket_path), "not found"
            )
        )
    else:
        try:
            ticket_body_raw = json.loads(ticket_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            findings.append(
                Finding("error", "hire-ticket-parse-error", str(ticket_path), str(exc))
            )

    if isinstance(ticket_body_raw, dict):
        hire_ticket_cls = _import_hire_ticket_cls()
        if hire_ticket_cls is None:
            findings.append(
                Finding(
                    "info",
                    "employee-contract-not-importable",
                    str(ticket_path),
                    "employee_contract not importable; validating minimal "
                    "hire-ticket fields directly instead of via HireTicket",
                )
            )
            _validate_hire_ticket_minimally(ticket_body_raw, ticket_path, findings)
        else:
            try:
                hire_ticket_cls.from_payload(ticket_body_raw)
            except Exception as exc:  # noqa: BLE001 - ticket library's own error type
                findings.append(
                    Finding("error", "hire-ticket-invalid", str(ticket_path), str(exc))
                )

        # Foundation policy, always enforced regardless of which of the two
        # branches above ran: see _check_budget_complete's docstring.
        _check_budget_complete(
            ticket_body_raw.get("budget"), ticket_path, findings
        )

        if deployment is not None:
            ticket_employee_id = ticket_body_raw.get("employee_id")
            if ticket_employee_id != deployment.employee_id:
                findings.append(
                    Finding(
                        "error",
                        "employee-id-mismatch",
                        str(ticket_path),
                        f"hire-ticket employee_id {ticket_employee_id!r} != "
                        f"deployment employee_id {deployment.employee_id!r}",
                    )
                )
            ticket_tenant_id = ticket_body_raw.get("tenant_id")
            if (
                ticket_tenant_id is not None
                and ticket_tenant_id != deployment.tenant_id
            ):
                findings.append(
                    Finding(
                        "error",
                        "tenant-id-mismatch",
                        str(ticket_path),
                        f"hire-ticket tenant_id {ticket_tenant_id!r} != "
                        f"deployment tenant_id {deployment.tenant_id!r}",
                    )
                )

    # -- (d) identity files --------------------------------------------
    for name in _IDENTITY_FILES:
        path = workspace / name
        if not path.is_file():
            findings.append(
                Finding(
                    "error",
                    "identity-file-missing",
                    str(path),
                    f"{name} is required and was not found",
                )
            )

    journal_dir = workspace / "journal"
    if not journal_dir.is_dir():
        findings.append(
            Finding(
                "error",
                "identity-dir-missing",
                str(journal_dir),
                "journal/ directory is required",
            )
        )

    for sub in _MEMORY_SUBDIRS:
        path = workspace / "memory" / sub
        if not path.is_dir():
            findings.append(
                Finding(
                    "error",
                    "identity-dir-missing",
                    str(path),
                    f"memory/{sub}/ directory is required",
                )
            )

    charter_path = workspace / "charter.md"
    if charter_path.is_file():
        text = charter_path.read_text(encoding="utf-8", errors="replace")
        for heading in _CHARTER_SECTIONS:
            if heading not in text:
                findings.append(
                    Finding(
                        "error",
                        "charter-section-missing",
                        str(charter_path),
                        f"missing required section heading {heading!r}",
                    )
                )

    # -- (e) budget appears only in the ticket ------------------------------
    if isinstance(spec_body_raw, Mapping) and "budget" in spec_body_raw:
        findings.append(
            Finding(
                "error",
                "budget-outside-ticket",
                str(spec_path),
                "budget must not appear in the EmployeeSpec; it belongs "
                "only in hire-ticket.json (contract page rule 3)",
            )
        )
    for design_path in sorted(workspace.rglob("*.design.json")):
        try:
            body = json.loads(design_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(body, Mapping) and "budget" in body:
            findings.append(
                Finding(
                    "error",
                    "budget-outside-ticket",
                    str(design_path),
                    "budget must not appear outside hire-ticket.json "
                    "(contract page rule 3)",
                )
            )

    # -- (f) no unresolved provider-profile:* references --------------------
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "provider-profile:" in text:
            findings.append(
                Finding(
                    "error",
                    "provider-profile-reference",
                    str(path),
                    "literal 'provider-profile:' reference found; routing is "
                    "the router's (D152-D158), not a scaffold concern "
                    "(contract page open question 7)",
                )
            )

    ok = not any(item.severity == "error" for item in findings)
    return FoundationReport(ok=ok, findings=findings)


# ---------------------------------------------------------------------------
# scaffold_employee
# ---------------------------------------------------------------------------


def _charter_template(employee_id: str, display_name: str, principal: str,
                       tenant_id: str) -> str:
    return f"""# {display_name} — Charter

`employee-foundation/1.0` scaffold. Every `<FILL: ...>` marker below is a
placeholder; this charter does not describe a real employee until a human
(Lee, or an explicitly delegated reviewer) replaces them.

## Role

<FILL: one paragraph — who is {employee_id} to its principal, and why does
it exist?>

## Responsibilities

- <FILL: responsibility one>
- <FILL: responsibility two>

## Authority

- Principal: {principal}
- Tenant: {tenant_id}
- <FILL: what this employee may read/write/decide without asking, and what
  it may never do>

## Escalation

- <FILL: who or what this employee escalates to, and under what conditions>

## Provenance

- Scaffolded by `ai_employee.foundation.scaffold_employee`
  (`{FOUNDATION_SCHEMA_VERSION}`).
- <FILL: date and who approved this charter>
"""


def _agents_template(employee_id: str, display_name: str) -> str:
    return f"""# {display_name} — operating instructions

<FILL: machine-facing operating doctrine for {employee_id}. This is the
`employee-pattern/1.0` AGENTS.md slot: what this employee reads before
acting, what order its responsibilities come in, and which tool map or
skill set it uses. See charter.md for the human-facing authority statement
this file must not contradict.>
"""


def _personality_template(display_name: str) -> str:
    return f"""# {display_name} — personality

<FILL: one paragraph is enough at scaffold time. `employee-pattern/1.0`
does not require a full relationship profile for every employee — only
that this file exists and is not invented later without a human writing
it.>
"""


def _decisions_template(display_name: str) -> str:
    return f"""# {display_name} — decisions

Append-only. Never edit a past entry; add a new one. Header-only at
scaffold time is legal — decisions land here as they are made.
"""


def _decisions_index_template(display_name: str) -> str:
    return f"""# {display_name} — decisions index

Generated view of decisions.md. Regenerate after every append (mirror the
Chief of Staff's `scripts/build_decisions_index.py` pattern if this
employee accumulates enough decisions to need one).

## Standing doctrine

<FILL: none yet — this employee has no decisions recorded>

## All decisions

| ID | Date | Status | Title | Decision |
|---|---|---|---|---|
"""


def scaffold_employee(
    root: Path,
    employee_id: str,
    *,
    display_name: str,
    tenant_id: str = "lee-installation",
    principal: str = "principal:lee",
) -> list[Path]:
    """Write the ``employee-foundation/1.0`` scaffold at ``root``.

    Refuses (raises :class:`FoundationError`) if any target path already
    exists — this never overwrites. Every generated file is a template with
    `<FILL: ...>` placeholders; the hire ticket's budget fields are ``null``,
    which is deliberate: :func:`validate_foundation` fails on a freshly
    scaffolded workspace until a human fills them in. That failure is the
    scaffold working correctly, not a bug ("refusal is success").
    """

    if not isinstance(employee_id, str) or not employee_id.strip():
        raise FoundationError("employee_id must be a non-empty string")

    root = Path(root)
    layout = FoundationLayout(
        employee_id=employee_id, workspace=root, deployment_record=root
    )
    existing = [path for path in layout.all_paths() if path.exists()]
    if existing:
        raise FoundationError(
            "refusing to scaffold over existing path(s): "
            + ", ".join(str(path) for path in existing)
        )

    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def _write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)

    _write(
        layout.charter_path,
        _charter_template(employee_id, display_name, principal, tenant_id),
    )
    _write(layout.agents_path, _agents_template(employee_id, display_name))
    _write(layout.personality_path, _personality_template(display_name))
    _write(layout.decisions_path, _decisions_template(display_name))
    _write(layout.decisions_index_path, _decisions_index_template(display_name))

    layout.journal_dir.mkdir(parents=True, exist_ok=True)
    written.append(layout.journal_dir)
    for sub_dir in layout.memory_subdirs:
        sub_dir.mkdir(parents=True, exist_ok=True)
        written.append(sub_dir)
    layout.evidence_dir.mkdir(parents=True, exist_ok=True)
    written.append(layout.evidence_dir)

    hire_ticket_body = {
        "schema_version": 1,
        "employee_id": employee_id,
        "tenant_id": tenant_id,
        "business_question": (
            "<FILL: what decision or output does this employee exist to produce?>"
        ),
        "verifiability_test": (
            "<FILL: how would a skeptic verify this employee's output is real?>"
        ),
        "kill_switch": "<FILL: how does a human stop this employee immediately?>",
        "owner_quote": "<FILL: the literal owner sign-off approving this hire>",
        "rung": "static",
        "runtime_binding": "factory-local",
        "boundaries": {},
        "cadence": None,
        "budget": {
            "max_usd_per_run": None,
            "max_usd_per_month": None,
            "max_seconds_per_run": None,
        },
        "reviewer": "lee",
    }
    _write(
        layout.hire_ticket_path,
        json.dumps(hire_ticket_body, indent=2, sort_keys=True) + "\n",
    )

    spec_body = {
        "schema_version": EMPLOYEE_SPEC_SCHEMA_VERSION,
        "employee_id": employee_id,
        "name": display_name,
        "produces": "<FILL: artifact_type this employee produces>",
        "inputs": [
            {
                "name": "<FILL: input name>",
                "artifact_type": "<FILL: input artifact_type>",
                "required": True,
                "on_deficient": "refuse_and_escalate",
                "on_absent": "refuse_and_escalate",
                "min_confidence": 0.0,
                "requires_independent_provider": False,
            }
        ],
        "reads_knowledge_layer": False,
        "escalation_target": "human:proposal-manager",
    }
    _write(
        layout.spec_path,
        json.dumps(spec_body, indent=2, sort_keys=True) + "\n",
    )

    return written
