"""The strict, versioned identity and runtime binding contract.

``EmployeeSpec`` remains the behavioral declaration: it says what an employee
consumes, produces, refuses, and escalates.  ``DeploymentRecord`` is the
separate installation record shared by the factory and runtime systems.  It
contains references to runtime-owned things, never their mutable contents or
secrets.

The module deliberately uses only immutable dataclasses, enums, and the
standard library.  Parsing is strict at every object boundary so a misspelled
field cannot silently weaken a downstream control.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from .contract import EmployeeSpec
from .envelope import DegradationBehavior

DEPLOYMENT_SCHEMA_VERSION = "ai-employee-deployment/1.0"
TRANSITION_SCHEMA_VERSION = "ai-employee-lifecycle-transition/1.0"
EMPLOYEE_SPEC_SCHEMA_VERSION = "ai-employee-spec/1.0"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*$")
_REFERENCE_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9+.-]*(?::[A-Za-z0-9][A-Za-z0-9._/-]*)?$"
)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}" r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)


class DeploymentError(ValueError):
    """Raised when a deployment or lifecycle record is not well formed."""


class LifecycleState(str, Enum):
    """The deliberately small lifecycle vocabulary for current operations."""

    DRAFT = "draft"
    COMMISSIONING = "commissioning"
    PILOT = "pilot"
    SCHEDULED = "scheduled"
    PAUSED = "paused"
    RETIRED = "retired"
    ARCHIVED = "archived"


_ALLOWED_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.DRAFT: frozenset({LifecycleState.COMMISSIONING}),
    LifecycleState.COMMISSIONING: frozenset(
        {LifecycleState.PILOT, LifecycleState.PAUSED, LifecycleState.RETIRED}
    ),
    LifecycleState.PILOT: frozenset(
        {LifecycleState.SCHEDULED, LifecycleState.PAUSED, LifecycleState.RETIRED}
    ),
    LifecycleState.SCHEDULED: frozenset(
        {LifecycleState.PAUSED, LifecycleState.RETIRED}
    ),
    LifecycleState.PAUSED: frozenset(
        {
            LifecycleState.COMMISSIONING,
            LifecycleState.PILOT,
            LifecycleState.SCHEDULED,
            LifecycleState.RETIRED,
        }
    ),
    LifecycleState.RETIRED: frozenset({LifecycleState.ARCHIVED}),
    LifecycleState.ARCHIVED: frozenset(),
}


def _canonical_bytes(body: Mapping[str, Any]) -> bytes:
    """Serialize a JSON object without whitespace or implementation variance."""

    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest_body(body: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(body)).hexdigest()


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeploymentError(f"{field} must be a non-empty string")
    return value


def _validate_identifier(value: Any, field: str) -> str:
    value = _require_text(value, field)
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise DeploymentError(
            f"{field} must match [A-Za-z][A-Za-z0-9]*(with - _ or . segments), "
            f"got {value!r}"
        )
    return value


_TENANT_PLACEHOLDERS = frozenset(
    {"future", "pending", "placeholder", "unknown", "tbd", "todo"}
)


def _validate_tenant_id(value: Any, field: str) -> str:
    value = _validate_identifier(value, field)
    if value.lower() in _TENANT_PLACEHOLDERS:
        raise DeploymentError(f"{field} must identify a real tenant, not a placeholder")
    return value


def _validate_reference(value: Any, field: str) -> str:
    value = _require_text(value, field)
    if _REFERENCE_RE.fullmatch(value) is None:
        raise DeploymentError(f"{field} is not a valid reference: {value!r}")
    return value


def _validate_digest(value: Any, field: str) -> str:
    value = _require_text(value, field)
    if _DIGEST_RE.fullmatch(value) is None:
        raise DeploymentError(f"{field} must be a lowercase sha256 digest")
    return value


def _parse_timestamp(value: Any, field: str) -> datetime:
    value = _require_text(value, field)
    if _TIMESTAMP_RE.fullmatch(value) is None:
        raise DeploymentError(f"{field} must be RFC3339 with a timezone, got {value!r}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeploymentError(f"{field} is not a valid timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DeploymentError(f"{field} must include a timezone")
    return parsed


def _coerce_state(value: Any, field: str) -> LifecycleState:
    try:
        return value if isinstance(value, LifecycleState) else LifecycleState(value)
    except (TypeError, ValueError) as exc:
        raise DeploymentError(
            f"{field} is not a supported lifecycle state: {value!r}"
        ) from exc


def _require_tuple(value: Any, field: str) -> tuple[Any, ...]:
    if not isinstance(value, tuple):
        raise DeploymentError(f"{field} must be an immutable tuple")
    return value


def _ensure_unique(values: tuple[str, ...], field: str) -> None:
    if len(set(values)) != len(values):
        raise DeploymentError(f"{field} contains duplicate references")


def _validate_absolute_root(value: Any, field: str) -> str:
    """Require an absolute path.

    A relative root would resolve against the runtime's working directory, so
    a durable authority record must not admit one.
    """

    value = _require_text(value, field)
    if not os.path.isabs(value):
        raise DeploymentError(f"{field} must be a non-empty absolute filesystem path")
    return value


def _validate_token(value: Any, field: str) -> str:
    """Require one non-empty token: text that carries no whitespace."""

    value = _require_text(value, field)
    if any(character.isspace() for character in value):
        raise DeploymentError(f"{field} must be a single non-empty token")
    return value


def _strict_object(
    body: Any,
    *,
    field: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise DeploymentError(f"{field} must be an object")
    allowed = required | optional
    unknown = [key for key in body if key not in allowed]
    if unknown:
        unknown_names = ", ".join(sorted((str(key) for key in unknown)))
        raise DeploymentError(f"{field} has unknown field(s): {unknown_names}")
    missing = sorted(required - set(body))
    if missing:
        raise DeploymentError(
            f"{field} is missing required field(s): {', '.join(missing)}"
        )
    return body


def _strict_array(body: Any, field: str) -> list[Any]:
    if not isinstance(body, list):
        raise DeploymentError(f"{field} must be an array")
    return body


def _input_spec_to_dict(input_spec: Any) -> dict[str, Any]:
    if not hasattr(input_spec, "name"):
        raise DeploymentError("EmployeeSpec.inputs contains an invalid item")
    try:
        on_deficient = DegradationBehavior(input_spec.on_deficient).value
        on_absent = DegradationBehavior(input_spec.on_absent).value
    except (TypeError, ValueError) as exc:
        raise DeploymentError(
            "EmployeeSpec input has an invalid degradation behavior"
        ) from exc
    body = {
        "name": input_spec.name,
        "artifact_type": input_spec.artifact_type,
        "required": input_spec.required,
        "on_deficient": on_deficient,
        "on_absent": on_absent,
        "min_confidence": input_spec.min_confidence,
        "requires_independent_provider": input_spec.requires_independent_provider,
    }
    if input_spec.schema_version is not None:
        body["schema_version"] = input_spec.schema_version
    return body


def employee_spec_body(spec: object) -> dict[str, Any]:
    """Return the versioned, domain-neutral declaration used for spec digests."""

    if not isinstance(spec, EmployeeSpec):
        raise DeploymentError("spec must be an EmployeeSpec")
    body = {
        "schema_version": EMPLOYEE_SPEC_SCHEMA_VERSION,
        "employee_id": spec.employee_id,
        "name": spec.name,
        "produces": spec.produces,
        "inputs": [_input_spec_to_dict(item) for item in spec.inputs],
        "reads_knowledge_layer": spec.reads_knowledge_layer,
        "escalation_target": spec.escalation_target,
    }
    if spec.output_schema_version is not None:
        body["output_schema_version"] = spec.output_schema_version
    return body


def employee_spec_digest(spec: EmployeeSpec) -> str:
    """Compute a deterministic digest for an existing :class:`EmployeeSpec`."""

    return _digest_body(employee_spec_body(spec))


def is_legal_transition(from_state: Any, to_state: Any) -> bool:
    """Return whether the lifecycle vocabulary permits the state change."""

    try:
        source = _coerce_state(from_state, "from_state")
        target = _coerce_state(to_state, "to_state")
    except DeploymentError:
        return False
    return target in _ALLOWED_TRANSITIONS[source]


@dataclass(frozen=True)
class EmployeeSpecReference:
    """A reference to a behavioral specification and its content digest."""

    ref: str
    digest: str
    schema_version: str = EMPLOYEE_SPEC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_reference(self.ref, "specification.ref")
        _validate_digest(self.digest, "specification.digest")
        if self.schema_version != EMPLOYEE_SPEC_SCHEMA_VERSION:
            raise DeploymentError(
                f"unsupported EmployeeSpec schema version: {self.schema_version!r}"
            )

    @classmethod
    def from_spec(cls, spec: EmployeeSpec) -> EmployeeSpecReference:
        return cls(ref=spec.employee_id, digest=employee_spec_digest(spec))

    @classmethod
    def from_dict(cls, body: Any) -> EmployeeSpecReference:
        body = _strict_object(
            body,
            field="specification",
            required=frozenset({"ref", "digest", "schema_version"}),
        )
        return cls(
            ref=body["ref"],
            digest=body["digest"],
            schema_version=body["schema_version"],
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "ref": self.ref,
            "digest": self.digest,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class WorkspaceBinding:
    """A logical workspace reference; filesystem contents stay outside this record."""

    binding_ref: str
    workspace_ref: str

    def __post_init__(self) -> None:
        _validate_reference(self.binding_ref, "workspace binding.binding_ref")
        _validate_reference(self.workspace_ref, "workspace binding.workspace_ref")

    @classmethod
    def from_dict(cls, body: Any) -> WorkspaceBinding:
        body = _strict_object(
            body,
            field="workspace_binding",
            required=frozenset({"binding_ref", "workspace_ref"}),
        )
        return cls(binding_ref=body["binding_ref"], workspace_ref=body["workspace_ref"])

    def to_dict(self) -> dict[str, str]:
        return {"binding_ref": self.binding_ref, "workspace_ref": self.workspace_ref}


@dataclass(frozen=True)
class AssignmentWorkspaceGrant:
    """Durable authority to work in an assigned workspace.

    ``WorkspaceBinding`` records where an employee *lives*; this grant records
    where it is *authorized to work*.  Identity location and work location are
    separate authorities, so a supervising employee can be granted authority
    over a project workspace without being relocated into it.  Employee home,
    assignment workspace and control plane are three distinct authorities.

    The grant is **durable employee authority**, not a dispatch: it survives
    individual assignments, is inspectable without reconstructing execution
    history, and is centrally revocable by the principal that issued it.  A
    per-dispatch scope may narrow this grant; it may never enlarge it.

    The grant is a **logical authority record**.  It confers no filesystem
    access by itself and is not a credential; enforcement lives in the
    runtime's permission policy.  Reachability must not imply authority: a
    workspace the runtime can physically see, but which no grant names, is not
    authorized.
    """

    binding_ref: str
    workspace_ref: str
    root: str
    capability: str
    granted_by: str
    granted_at: str
    grant_ref: str

    def __post_init__(self) -> None:
        _validate_reference(self.binding_ref, "assignment_workspace_grant.binding_ref")
        _validate_reference(
            self.workspace_ref, "assignment_workspace_grant.workspace_ref"
        )
        _validate_absolute_root(self.root, "assignment_workspace_grant.root")
        _validate_token(self.capability, "assignment_workspace_grant.capability")
        _validate_reference(self.granted_by, "assignment_workspace_grant.granted_by")
        _parse_timestamp(self.granted_at, "assignment_workspace_grant.granted_at")
        _validate_reference(self.grant_ref, "assignment_workspace_grant.grant_ref")

    @classmethod
    def from_dict(cls, body: Any) -> AssignmentWorkspaceGrant:
        body = _strict_object(
            body,
            field="assignment_workspace_grant",
            required=frozenset(
                {
                    "binding_ref",
                    "workspace_ref",
                    "root",
                    "capability",
                    "granted_by",
                    "granted_at",
                    "grant_ref",
                }
            ),
        )
        return cls(
            binding_ref=body["binding_ref"],
            workspace_ref=body["workspace_ref"],
            root=body["root"],
            capability=body["capability"],
            granted_by=body["granted_by"],
            granted_at=body["granted_at"],
            grant_ref=body["grant_ref"],
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "binding_ref": self.binding_ref,
            "workspace_ref": self.workspace_ref,
            "root": self.root,
            "capability": self.capability,
            "granted_by": self.granted_by,
            "granted_at": self.granted_at,
            "grant_ref": self.grant_ref,
        }


@dataclass(frozen=True)
class DeliveryProfileReference:
    """A versioned reference to a runtime-owned Delivery Profile."""

    profile_ref: str
    schema_version: str

    def __post_init__(self) -> None:
        _validate_reference(self.profile_ref, "delivery profile.profile_ref")
        _require_text(self.schema_version, "delivery profile.schema_version")

    @classmethod
    def from_dict(cls, body: Any) -> DeliveryProfileReference:
        body = _strict_object(
            body,
            field="delivery_profile_ref",
            required=frozenset({"profile_ref", "schema_version"}),
        )
        return cls(
            profile_ref=body["profile_ref"], schema_version=body["schema_version"]
        )

    def to_dict(self) -> dict[str, str]:
        return {"profile_ref": self.profile_ref, "schema_version": self.schema_version}


# The additive participation trio is all-or-none. A record without it is a
# legacy board-ref-only identity that carries no participant binding; a record
# with it binds the Board principal, Board agent, and physical machine. The
# deployment record's separate ``principal_refs`` list is never this binding.
BOARD_PARTICIPATION_FIELDS = ("principal_ref", "agent_id", "machine_id")


@dataclass(frozen=True)
class BoardIdentity:
    """The coordination identity to which Board projections attach.

    ``board_ref`` is always required. The optional additive trio
    ``principal_ref``, ``agent_id`` and ``machine_id`` is all-or-none: legacy
    board-ref-only identities remain valid and confer no participant authority,
    while a participant identity carries the complete, distinct binding. The
    trio never aliases the deployment record's separate ``principal_refs``.
    """

    board_ref: str
    principal_ref: str | None = None
    agent_id: str | None = None
    machine_id: str | None = None

    def __post_init__(self) -> None:
        _validate_reference(self.board_ref, "board_identity.board_ref")
        trio = (self.principal_ref, self.agent_id, self.machine_id)
        present = tuple(item is not None for item in trio)
        if any(present) and not all(present):
            raise DeploymentError(
                "board_identity participation fields must include "
                "principal_ref, agent_id, and machine_id together"
            )
        if all(present):
            for field, value in zip(BOARD_PARTICIPATION_FIELDS, trio):
                _validate_reference(value, f"board_identity.{field}")

    @classmethod
    def from_dict(cls, body: Any) -> BoardIdentity:
        body = _strict_object(
            body,
            field="board_identity",
            required=frozenset({"board_ref"}),
            optional=frozenset(BOARD_PARTICIPATION_FIELDS),
        )
        present = [field for field in BOARD_PARTICIPATION_FIELDS if field in body]
        if present and len(present) != len(BOARD_PARTICIPATION_FIELDS):
            raise DeploymentError(
                "board_identity participation fields must include "
                "principal_ref, agent_id, and machine_id together"
            )
        if present:
            for field in BOARD_PARTICIPATION_FIELDS:
                _validate_reference(body[field], f"board_identity.{field}")
            return cls(
                board_ref=body["board_ref"],
                principal_ref=body["principal_ref"],
                agent_id=body["agent_id"],
                machine_id=body["machine_id"],
            )
        return cls(board_ref=body["board_ref"])

    @property
    def has_participation_binding(self) -> bool:
        """Whether the complete additive participation trio is present."""

        return self.principal_ref is not None

    def to_dict(self) -> dict[str, str]:
        body = {"board_ref": self.board_ref}
        if self.principal_ref is not None:
            body["principal_ref"] = self.principal_ref
            body["agent_id"] = self.agent_id  # type: ignore[assignment]
            body["machine_id"] = self.machine_id  # type: ignore[assignment]
        return body


@dataclass(frozen=True)
class CommissioningLineage:
    """Immutable references proving where and by whom commissioning began."""

    lineage_ref: str
    source_ref: str
    commissioned_by: str
    commissioned_at: str

    def __post_init__(self) -> None:
        _validate_reference(self.lineage_ref, "commissioning_lineage.lineage_ref")
        _validate_reference(self.source_ref, "commissioning_lineage.source_ref")
        _validate_reference(
            self.commissioned_by, "commissioning_lineage.commissioned_by"
        )
        _parse_timestamp(self.commissioned_at, "commissioning_lineage.commissioned_at")

    @classmethod
    def from_dict(cls, body: Any) -> CommissioningLineage:
        body = _strict_object(
            body,
            field="commissioning_lineage",
            required=frozenset(
                {"lineage_ref", "source_ref", "commissioned_by", "commissioned_at"}
            ),
        )
        return cls(
            lineage_ref=body["lineage_ref"],
            source_ref=body["source_ref"],
            commissioned_by=body["commissioned_by"],
            commissioned_at=body["commissioned_at"],
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "lineage_ref": self.lineage_ref,
            "source_ref": self.source_ref,
            "commissioned_by": self.commissioned_by,
            "commissioned_at": self.commissioned_at,
        }


@dataclass(frozen=True)
class LifecycleTransition:
    """One immutable, separately stored lifecycle state transition."""

    employee_id: str
    from_state: LifecycleState
    to_state: LifecycleState
    actor: str
    reason: str
    timestamp: str
    schema_version: str = TRANSITION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_identifier(self.employee_id, "transition.employee_id")
        source = _coerce_state(self.from_state, "transition.from_state")
        target = _coerce_state(self.to_state, "transition.to_state")
        object.__setattr__(self, "from_state", source)
        object.__setattr__(self, "to_state", target)
        if not is_legal_transition(source, target):
            raise DeploymentError(
                f"illegal lifecycle transition: {source.value} -> {target.value}"
            )
        _validate_reference(self.actor, "transition.actor")
        _require_text(self.reason, "transition.reason")
        _parse_timestamp(self.timestamp, "transition.timestamp")
        if self.schema_version != TRANSITION_SCHEMA_VERSION:
            raise DeploymentError(
                f"unsupported lifecycle transition schema version: "
                f"{self.schema_version!r}"
            )

    @classmethod
    def from_dict(cls, body: Any) -> LifecycleTransition:
        body = _strict_object(
            body,
            field="lifecycle_transition",
            required=frozenset(
                {
                    "employee_id",
                    "from_state",
                    "to_state",
                    "actor",
                    "reason",
                    "timestamp",
                    "schema_version",
                }
            ),
        )
        return cls(
            employee_id=body["employee_id"],
            from_state=body["from_state"],
            to_state=body["to_state"],
            actor=body["actor"],
            reason=body["reason"],
            timestamp=body["timestamp"],
            schema_version=body["schema_version"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "actor": self.actor,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "schema_version": self.schema_version,
        }

    def to_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())

    def to_json(self) -> str:
        return self.to_bytes().decode("utf-8")

    @property
    def digest(self) -> str:
        return _digest_body(self.to_dict())


@dataclass(frozen=True)
class DeploymentRecord:
    """Canonical identity and runtime bindings for one employee installation."""

    schema_version: str
    employee_id: str
    tenant_id: str
    display_name: str
    specification: EmployeeSpecReference
    lifecycle_state: LifecycleState
    mission_ref: str
    workspace_bindings: tuple[WorkspaceBinding, ...]
    delivery_profile_refs: tuple[DeliveryProfileReference, ...]
    board_identity: BoardIdentity
    commissioning_lineage: CommissioningLineage
    principal_refs: tuple[str, ...]
    credential_scope_refs: tuple[str, ...]
    created_by: str
    created_at: str
    updated_by: str
    updated_at: str
    assignment_workspace_refs: tuple[AssignmentWorkspaceGrant, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != DEPLOYMENT_SCHEMA_VERSION:
            raise DeploymentError(
                f"unsupported deployment schema version: {self.schema_version!r}"
            )
        _validate_identifier(self.employee_id, "employee_id")
        _validate_tenant_id(self.tenant_id, "tenant_id")
        _require_text(self.display_name, "display_name")
        if not isinstance(self.specification, EmployeeSpecReference):
            raise DeploymentError("specification must be an EmployeeSpecReference")
        state = _coerce_state(self.lifecycle_state, "lifecycle_state")
        object.__setattr__(self, "lifecycle_state", state)
        _validate_reference(self.mission_ref, "mission_ref")

        workspace_bindings = _require_tuple(
            self.workspace_bindings, "workspace_bindings"
        )
        if not workspace_bindings:
            raise DeploymentError("workspace_bindings must not be empty")
        if not all(isinstance(item, WorkspaceBinding) for item in workspace_bindings):
            raise DeploymentError("workspace_bindings contains an invalid binding")
        binding_refs = tuple(item.binding_ref for item in workspace_bindings)
        workspace_refs = tuple(item.workspace_ref for item in workspace_bindings)
        _ensure_unique(binding_refs, "workspace_bindings.binding_ref")
        _ensure_unique(workspace_refs, "workspace_bindings.workspace_ref")
        object.__setattr__(
            self,
            "workspace_bindings",
            tuple(sorted(workspace_bindings, key=lambda item: item.binding_ref)),
        )

        profiles = _require_tuple(self.delivery_profile_refs, "delivery_profile_refs")
        if not all(isinstance(item, DeliveryProfileReference) for item in profiles):
            raise DeploymentError("delivery_profile_refs contains an invalid reference")
        _ensure_unique(
            tuple(item.profile_ref for item in profiles), "delivery_profile_refs"
        )
        object.__setattr__(
            self,
            "delivery_profile_refs",
            tuple(sorted(profiles, key=lambda item: item.profile_ref)),
        )

        if not isinstance(self.board_identity, BoardIdentity):
            raise DeploymentError("board_identity must be a BoardIdentity")
        if not isinstance(self.commissioning_lineage, CommissioningLineage):
            raise DeploymentError(
                "commissioning_lineage must be a CommissioningLineage"
            )

        principal_refs = _require_tuple(self.principal_refs, "principal_refs")
        credential_refs = _require_tuple(
            self.credential_scope_refs, "credential_scope_refs"
        )
        if not principal_refs:
            raise DeploymentError("principal_refs must not be empty")
        if not all(isinstance(item, str) for item in principal_refs):
            raise DeploymentError("principal_refs contains a non-string reference")
        if not all(isinstance(item, str) for item in credential_refs):
            raise DeploymentError(
                "credential_scope_refs contains a non-string reference"
            )
        for item in principal_refs:
            _validate_reference(item, "principal_refs")
        for item in credential_refs:
            _validate_reference(item, "credential_scope_refs")
        _ensure_unique(principal_refs, "principal_refs")
        _ensure_unique(credential_refs, "credential_scope_refs")
        object.__setattr__(self, "principal_refs", tuple(sorted(principal_refs)))
        object.__setattr__(
            self, "credential_scope_refs", tuple(sorted(credential_refs))
        )

        # Optional, additive: absent from every pre-existing record, which keeps
        # its canonical bytes and digest byte-identical.  A record that carries
        # grants sorts them by ``binding_ref`` exactly as ``workspace_bindings``
        # does, so two orderings of the same authority never differ in bytes.
        grants = _require_tuple(
            self.assignment_workspace_refs, "assignment_workspace_refs"
        )
        if not all(isinstance(item, AssignmentWorkspaceGrant) for item in grants):
            raise DeploymentError("assignment_workspace_refs contains an invalid grant")
        _ensure_unique(
            tuple(item.binding_ref for item in grants),
            "assignment_workspace_refs.binding_ref",
        )
        _ensure_unique(
            tuple(item.workspace_ref for item in grants),
            "assignment_workspace_refs.workspace_ref",
        )
        object.__setattr__(
            self,
            "assignment_workspace_refs",
            tuple(sorted(grants, key=lambda item: item.binding_ref)),
        )

        _validate_reference(self.created_by, "created_by")
        _validate_reference(self.updated_by, "updated_by")
        created = _parse_timestamp(self.created_at, "created_at")
        updated = _parse_timestamp(self.updated_at, "updated_at")
        if updated < created:
            raise DeploymentError("updated_at must not be earlier than created_at")

    @classmethod
    def from_dict(cls, body: Any) -> DeploymentRecord:
        required = frozenset(
            {
                "schema_version",
                "employee_id",
                "tenant_id",
                "display_name",
                "specification",
                "lifecycle_state",
                "mission_ref",
                "workspace_bindings",
                "delivery_profile_refs",
                "board_identity",
                "commissioning_lineage",
                "principal_refs",
                "credential_scope_refs",
                "created_by",
                "created_at",
                "updated_by",
                "updated_at",
            }
        )
        body = _strict_object(
            body,
            field="deployment",
            required=required,
            optional=frozenset({"assignment_workspace_refs"}),
        )
        if body["schema_version"] != DEPLOYMENT_SCHEMA_VERSION:
            raise DeploymentError(
                f"unsupported deployment schema version: {body['schema_version']!r}"
            )
        workspace_bindings = tuple(
            WorkspaceBinding.from_dict(item)
            for item in _strict_array(body["workspace_bindings"], "workspace_bindings")
        )
        profiles = tuple(
            DeliveryProfileReference.from_dict(item)
            for item in _strict_array(
                body["delivery_profile_refs"], "delivery_profile_refs"
            )
        )
        principal_refs = tuple(
            _require_text(item, "principal_refs item")
            for item in _strict_array(body["principal_refs"], "principal_refs")
        )
        credential_refs = tuple(
            _require_text(item, "credential_scope_refs item")
            for item in _strict_array(
                body["credential_scope_refs"], "credential_scope_refs"
            )
        )
        grants = tuple(
            AssignmentWorkspaceGrant.from_dict(item)
            for item in _strict_array(
                body.get("assignment_workspace_refs", []), "assignment_workspace_refs"
            )
        )
        return cls(
            schema_version=body["schema_version"],
            employee_id=body["employee_id"],
            tenant_id=body["tenant_id"],
            display_name=body["display_name"],
            specification=EmployeeSpecReference.from_dict(body["specification"]),
            lifecycle_state=body["lifecycle_state"],
            mission_ref=body["mission_ref"],
            workspace_bindings=workspace_bindings,
            delivery_profile_refs=profiles,
            board_identity=BoardIdentity.from_dict(body["board_identity"]),
            commissioning_lineage=CommissioningLineage.from_dict(
                body["commissioning_lineage"]
            ),
            principal_refs=principal_refs,
            credential_scope_refs=credential_refs,
            created_by=body["created_by"],
            created_at=body["created_at"],
            updated_by=body["updated_by"],
            updated_at=body["updated_at"],
            assignment_workspace_refs=grants,
        )

    @classmethod
    def from_json(cls, serialized: str | bytes) -> DeploymentRecord:
        try:
            if isinstance(serialized, bytes):
                serialized = serialized.decode("utf-8")
            body = json.loads(serialized)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeploymentError("deployment JSON is malformed") from exc
        return cls.from_dict(body)

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema_version": self.schema_version,
            "employee_id": self.employee_id,
            "tenant_id": self.tenant_id,
            "display_name": self.display_name,
            "specification": self.specification.to_dict(),
            "lifecycle_state": self.lifecycle_state.value,
            "mission_ref": self.mission_ref,
            "workspace_bindings": [item.to_dict() for item in self.workspace_bindings],
            "delivery_profile_refs": [
                item.to_dict() for item in self.delivery_profile_refs
            ],
            "board_identity": self.board_identity.to_dict(),
            "commissioning_lineage": self.commissioning_lineage.to_dict(),
            "principal_refs": list(self.principal_refs),
            "credential_scope_refs": list(self.credential_scope_refs),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at,
        }
        # Emitted only when granted, so a record without authority over an
        # assignment workspace serializes to exactly its historical bytes.
        if self.assignment_workspace_refs:
            body["assignment_workspace_refs"] = [
                item.to_dict() for item in self.assignment_workspace_refs
            ]
        return body

    def to_bytes(self) -> bytes:
        """Return canonical UTF-8 JSON bytes for storage and hashing."""

        return _canonical_bytes(self.to_dict())

    def to_json(self) -> str:
        return self.to_bytes().decode("utf-8")

    @property
    def digest(self) -> str:
        """Content digest of the canonical deployment record."""

        return _digest_body(self.to_dict())

    def transition(
        self,
        to_state: LifecycleState,
        *,
        actor: str,
        reason: str,
        timestamp: str,
    ) -> tuple[DeploymentRecord, LifecycleTransition]:
        """Apply one legal transition and return the new record plus its event."""

        current_updated = _parse_timestamp(self.updated_at, "updated_at")
        transition_timestamp = _parse_timestamp(timestamp, "transition.timestamp")
        if transition_timestamp < current_updated:
            raise DeploymentError(
                "transition timestamp must not move updated_at backward"
            )
        transition = LifecycleTransition(
            employee_id=self.employee_id,
            from_state=self.lifecycle_state,
            to_state=to_state,
            actor=actor,
            reason=reason,
            timestamp=timestamp,
        )
        return (
            replace(
                self,
                lifecycle_state=transition.to_state,
                updated_by=transition.actor,
                updated_at=transition.timestamp,
            ),
            transition,
        )


def validate_deployment(body: Mapping[str, Any]) -> None:
    """Validate a serialized deployment record, raising :class:`DeploymentError`."""

    DeploymentRecord.from_dict(body)


def deployment_digest_of(body: Mapping[str, Any]) -> str:
    """Compute the canonical digest of a serialized deployment body."""

    return DeploymentRecord.from_dict(body).digest
