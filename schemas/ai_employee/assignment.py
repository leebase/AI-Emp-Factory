"""Strict, immutable assignment admission and authority grants.

The assignment is the finite admission record.  The authority grant is the
separate, explicitly enumerated permission record that an assignment must
reference by content digest.  A Work Crew reference is retained only as a
routing hint; it never supplies identity, scope, or authority.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .deployment import (
    DeploymentError,
    _canonical_bytes,
    _digest_body,
    _parse_timestamp,
    _validate_digest,
    _validate_identifier,
    _validate_reference,
    _validate_tenant_id,
)

AUTHORITY_GRANT_SCHEMA_VERSION = "ai-employee-authority-grant/1.0"
AUTHORITY_STATUS_SCHEMA_VERSION = "ai-employee-authority-status/1.0"
ASSIGNMENT_SCHEMA_VERSION = "ai-employee-assignment/1.0"

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


class AssignmentError(ValueError):
    """Raised when an assignment or authority grant is not well formed."""


def _wrap(call, field: str):
    try:
        return call()
    except DeploymentError as exc:
        raise AssignmentError(str(exc)) from exc


def _text(value: Any, field: str) -> str:
    return _wrap(lambda: _validate_reference(value, field), field)


def _identifier(value: Any, field: str) -> str:
    return _wrap(lambda: _validate_identifier(value, field), field)


def _tenant(value: Any, field: str) -> str:
    return _wrap(lambda: _validate_tenant_id(value, field), field)


def _digest(value: Any, field: str) -> str:
    return _wrap(lambda: _validate_digest(value, field), field)


def _plain_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AssignmentError(f"{field} must be a non-empty string")
    return value


def _schema(value: Any, expected: str, field: str) -> str:
    if value != expected:
        raise AssignmentError(f"unsupported {field} schema version: {value!r}")
    return value


def _timestamp(value: Any, field: str) -> tuple[str, datetime]:
    try:
        parsed = _parse_timestamp(value, field)
    except DeploymentError as exc:
        raise AssignmentError(str(exc)) from exc
    return value, parsed


def _tuple(value: Any, field: str) -> tuple[Any, ...]:
    if not isinstance(value, tuple):
        raise AssignmentError(f"{field} must be an immutable tuple")
    return value


def _unique(values: tuple[str, ...], field: str) -> None:
    if len(values) != len(set(values)):
        raise AssignmentError(f"{field} contains duplicate references")


def _refs(value: Any, field: str, *, required: bool = False) -> tuple[str, ...]:
    values = _tuple(value, field)
    if required and not values:
        raise AssignmentError(f"{field} must not be empty")
    if not all(isinstance(item, str) for item in values):
        raise AssignmentError(f"{field} contains a non-string reference")
    result = tuple(_text(item, f"{field} item") for item in values)
    _unique(result, field)
    return result


def _classes(value: Any, field: str, *, required: bool = False) -> tuple[str, ...]:
    values = _tuple(value, field)
    if required and not values:
        raise AssignmentError(f"{field} must not be empty")
    result: list[str] = []
    for item in values:
        text = _plain_text(item, f"{field} item")
        if "*" in text:
            raise AssignmentError(f"{field} may not contain wildcard classes")
        result.append(text)
    result_tuple = tuple(result)
    _unique(result_tuple, field)
    return result_tuple


def _strict_object(
    body: Any,
    *,
    field: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise AssignmentError(f"{field} must be an object")
    allowed = required | optional
    unknown = sorted(str(key) for key in body if key not in allowed)
    if unknown:
        raise AssignmentError(f"{field} has unknown field(s): {', '.join(unknown)}")
    missing = sorted(required - set(body))
    if missing:
        raise AssignmentError(
            f"{field} is missing required field(s): {', '.join(missing)}"
        )
    return body


def _array(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise AssignmentError(f"{field} must be an array")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AssignmentError(f"{field} must be a positive integer")
    return value


def _decision_cap(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3:
        raise AssignmentError("max_human_decisions must be an integer from 0 through 3")
    return value


def _decimal(value: Any, field: str) -> str:
    if not isinstance(value, str) or _DECIMAL_RE.fullmatch(value) is None:
        raise AssignmentError(f"{field} must be a canonical decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise AssignmentError(f"{field} must be a canonical decimal string") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise AssignmentError(f"{field} must be greater than zero")
    return value


def _pairs_no_duplicates(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str):
            raise AssignmentError("JSON object keys must be strings")
        if key in body:
            raise AssignmentError(f"JSON object repeats field {key!r}")
        body[key] = value
    return body


def _reject_constant(value: str) -> None:
    raise AssignmentError(f"JSON contains non-finite number {value!r}")


def _load_json(serialized: str | bytes, field: str) -> Any:
    try:
        if isinstance(serialized, bytes):
            serialized = serialized.decode("utf-8")
        return json.loads(
            serialized,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except AssignmentError:
        raise
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssignmentError(f"{field} JSON is malformed") from exc


def _check_window(start: datetime, end: datetime, field: str) -> None:
    if end <= start:
        raise AssignmentError(f"{field}.expires_at must be later than issued_at")


def _check_now(now: str | datetime | None, issued: datetime, expires: datetime) -> None:
    if now is None:
        current = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        current = now
    else:
        _, current = _timestamp(now, "validation.now")
    if current.tzinfo is None or current.utcoffset() is None:
        raise AssignmentError("validation.now must include a timezone")
    if current < issued:
        raise AssignmentError("assignment is not yet active")
    if current >= expires:
        raise AssignmentError("assignment is expired")


@dataclass(frozen=True)
class AssignmentInputReference:
    """One exact, digest-pinned input admitted to an assignment."""

    name: str
    artifact_id: str
    artifact_type: str
    schema_version: str
    digest: str
    tenant_id: str
    context_class: str
    purpose: str
    data_class: str
    observed_at: str
    expires_at: str
    required: bool
    account_ref: str | None = None

    def __post_init__(self) -> None:
        _plain_text(self.name, "assignment input.name")
        _text(self.artifact_id, "assignment input.artifact_id")
        _plain_text(self.artifact_type, "assignment input.artifact_type")
        _plain_text(self.schema_version, "assignment input.schema_version")
        _digest(self.digest, "assignment input.digest")
        _tenant(self.tenant_id, "assignment input.tenant_id")
        _plain_text(self.context_class, "assignment input.context_class")
        _text(self.purpose, "assignment input.purpose")
        _plain_text(self.data_class, "assignment input.data_class")
        if self.account_ref is not None:
            _text(self.account_ref, "assignment input.account_ref")
        _, observed = _timestamp(self.observed_at, "assignment input.observed_at")
        _, expires = _timestamp(self.expires_at, "assignment input.expires_at")
        if expires < observed:
            raise AssignmentError(
                "assignment input.expires_at must not precede observed_at"
            )
        if not isinstance(self.required, bool):
            raise AssignmentError("assignment input.required must be a boolean")

    @classmethod
    def from_dict(cls, body: Any) -> AssignmentInputReference:
        body = _strict_object(
            body,
            field="assignment_input",
            required=frozenset(
                {
                    "name",
                    "artifact_id",
                    "artifact_type",
                    "schema_version",
                    "digest",
                    "tenant_id",
                    "context_class",
                    "purpose",
                    "data_class",
                    "observed_at",
                    "expires_at",
                    "required",
                }
            ),
            optional=frozenset({"account_ref"}),
        )
        return cls(**body)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "schema_version": self.schema_version,
            "digest": self.digest,
            "tenant_id": self.tenant_id,
            "context_class": self.context_class,
            "purpose": self.purpose,
            "data_class": self.data_class,
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
            "required": self.required,
            "account_ref": self.account_ref,
        }


@dataclass(frozen=True)
class AuthorityGrant:
    """An explicit, finite permission set; everything else is denied."""

    schema_version: str
    grant_id: str
    employee_id: str
    tenant_id: str
    principal_ref: str
    assignment_ref: str
    grant_kind: str
    context_class: str
    purpose: str
    data_class: str
    allowed_read_refs: tuple[str, ...]
    allowed_write_refs: tuple[str, ...]
    credential_scope_refs: tuple[str, ...]
    allowed_action_classes: tuple[str, ...]
    approval_required_action_classes: tuple[str, ...]
    prohibited_action_classes: tuple[str, ...]
    issued_by: str
    issued_at: str
    expires_at: str
    revocation_ref: str

    def __post_init__(self) -> None:
        _schema(self.schema_version, AUTHORITY_GRANT_SCHEMA_VERSION, "authority grant")
        _text(self.grant_id, "grant_id")
        _identifier(self.employee_id, "employee_id")
        _tenant(self.tenant_id, "tenant_id")
        _text(self.principal_ref, "principal_ref")
        _text(self.assignment_ref, "assignment_ref")
        if _DIGEST_RE.fullmatch(self.assignment_ref):
            raise AssignmentError(
                "assignment_ref must be an assignment ID, not a digest"
            )
        if self.grant_kind not in {"assignment", "standing"}:
            raise AssignmentError("grant_kind must be 'assignment' or 'standing'")
        _plain_text(self.context_class, "context_class")
        _text(self.purpose, "purpose")
        _plain_text(self.data_class, "data_class")
        read_refs = _refs(self.allowed_read_refs, "allowed_read_refs")
        write_refs = _refs(self.allowed_write_refs, "allowed_write_refs")
        if not read_refs and not write_refs:
            raise AssignmentError(
                "authority grant must enumerate at least one allowed resource"
            )
        _refs(self.credential_scope_refs, "credential_scope_refs")
        allowed = _classes(
            self.allowed_action_classes,
            "allowed_action_classes",
            required=True,
        )
        approvals = _classes(
            self.approval_required_action_classes,
            "approval_required_action_classes",
        )
        prohibited = _classes(
            self.prohibited_action_classes,
            "prohibited_action_classes",
        )
        if not set(approvals) <= set(allowed):
            raise AssignmentError(
                "approval_required_action_classes must be allowed action classes"
            )
        if set(allowed) & set(prohibited):
            raise AssignmentError(
                "prohibited_action_classes cannot be allowed action classes"
            )
        _text(self.issued_by, "issued_by")
        _, issued = _timestamp(self.issued_at, "issued_at")
        _, expires = _timestamp(self.expires_at, "expires_at")
        _check_window(issued, expires, "authority grant")
        _text(self.revocation_ref, "revocation_ref")

    @classmethod
    def from_dict(cls, body: Any) -> AuthorityGrant:
        body = _strict_object(
            body,
            field="authority_grant",
            required=frozenset(
                {
                    "schema_version",
                    "grant_id",
                    "employee_id",
                    "tenant_id",
                    "principal_ref",
                    "assignment_ref",
                    "grant_kind",
                    "context_class",
                    "purpose",
                    "data_class",
                    "allowed_read_refs",
                    "allowed_write_refs",
                    "credential_scope_refs",
                    "allowed_action_classes",
                    "approval_required_action_classes",
                    "prohibited_action_classes",
                    "issued_by",
                    "issued_at",
                    "expires_at",
                    "revocation_ref",
                }
            ),
        )
        return cls(
            schema_version=body["schema_version"],
            grant_id=body["grant_id"],
            employee_id=body["employee_id"],
            tenant_id=body["tenant_id"],
            principal_ref=body["principal_ref"],
            assignment_ref=body["assignment_ref"],
            grant_kind=body["grant_kind"],
            context_class=body["context_class"],
            purpose=body["purpose"],
            data_class=body["data_class"],
            allowed_read_refs=tuple(
                _array(body["allowed_read_refs"], "allowed_read_refs")
            ),
            allowed_write_refs=tuple(
                _array(body["allowed_write_refs"], "allowed_write_refs")
            ),
            credential_scope_refs=tuple(
                _array(body["credential_scope_refs"], "credential_scope_refs")
            ),
            allowed_action_classes=tuple(
                _array(body["allowed_action_classes"], "allowed_action_classes")
            ),
            approval_required_action_classes=tuple(
                _array(
                    body["approval_required_action_classes"],
                    "approval_required_action_classes",
                )
            ),
            prohibited_action_classes=tuple(
                _array(body["prohibited_action_classes"], "prohibited_action_classes")
            ),
            issued_by=body["issued_by"],
            issued_at=body["issued_at"],
            expires_at=body["expires_at"],
            revocation_ref=body["revocation_ref"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "grant_id": self.grant_id,
            "employee_id": self.employee_id,
            "tenant_id": self.tenant_id,
            "principal_ref": self.principal_ref,
            "assignment_ref": self.assignment_ref,
            "grant_kind": self.grant_kind,
            "context_class": self.context_class,
            "purpose": self.purpose,
            "data_class": self.data_class,
            "allowed_read_refs": list(self.allowed_read_refs),
            "allowed_write_refs": list(self.allowed_write_refs),
            "credential_scope_refs": list(self.credential_scope_refs),
            "allowed_action_classes": list(self.allowed_action_classes),
            "approval_required_action_classes": list(
                self.approval_required_action_classes
            ),
            "prohibited_action_classes": list(self.prohibited_action_classes),
            "issued_by": self.issued_by,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "revocation_ref": self.revocation_ref,
        }

    def to_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())

    def to_json(self) -> str:
        return self.to_bytes().decode("utf-8")

    @classmethod
    def from_json(cls, serialized: str | bytes) -> AuthorityGrant:
        return cls.from_dict(_load_json(serialized, "authority grant"))

    @property
    def digest(self) -> str:
        return _digest_body(self.to_dict())

@dataclass(frozen=True)
class AuthorityStatus:
    """A short-lived, immutable observation of grant validity/revocation."""

    schema_version: str
    status_id: str
    grant_ref: str
    grant_digest: str
    tenant_id: str
    principal_ref: str
    source_ref: str
    checked_by: str
    checked_at: str
    expires_at: str
    revoked: bool
    revocation_receipt_ref: str | None = None

    def __post_init__(self) -> None:
        _schema(self.schema_version, AUTHORITY_STATUS_SCHEMA_VERSION, "authority status")
        _text(self.status_id, "status_id")
        if _DIGEST_RE.fullmatch(self.status_id):
            raise AssignmentError("status_id must be a status ID, not a digest")
        _text(self.grant_ref, "grant_ref")
        if _DIGEST_RE.fullmatch(self.grant_ref):
            raise AssignmentError("grant_ref must be a grant ID, not a digest")
        _digest(self.grant_digest, "grant_digest")
        _tenant(self.tenant_id, "tenant_id")
        _text(self.principal_ref, "principal_ref")
        _text(self.source_ref, "source_ref")
        _text(self.checked_by, "checked_by")
        _, checked = _timestamp(self.checked_at, "checked_at")
        _, expires = _timestamp(self.expires_at, "expires_at")
        if expires <= checked:
            raise AssignmentError("authority status.expires_at must be later than checked_at")
        if not isinstance(self.revoked, bool):
            raise AssignmentError("authority status.revoked must be a boolean")
        if self.revoked:
            if self.revocation_receipt_ref is None:
                raise AssignmentError(
                    "revoked authority status requires revocation_receipt_ref"
                )
            _text(self.revocation_receipt_ref, "revocation_receipt_ref")
        elif self.revocation_receipt_ref is not None:
            raise AssignmentError(
                "revocation_receipt_ref is only valid when revoked is true"
            )

    @classmethod
    def from_dict(cls, body: Any) -> AuthorityStatus:
        body = _strict_object(
            body,
            field="authority_status",
            required=frozenset(
                {
                    "schema_version",
                    "status_id",
                    "grant_ref",
                    "grant_digest",
                    "tenant_id",
                    "principal_ref",
                    "source_ref",
                    "checked_by",
                    "checked_at",
                    "expires_at",
                    "revoked",
                }
            ),
            optional=frozenset({"revocation_receipt_ref"}),
        )
        return cls(
            schema_version=body["schema_version"],
            status_id=body["status_id"],
            grant_ref=body["grant_ref"],
            grant_digest=body["grant_digest"],
            tenant_id=body["tenant_id"],
            principal_ref=body["principal_ref"],
            source_ref=body["source_ref"],
            checked_by=body["checked_by"],
            checked_at=body["checked_at"],
            expires_at=body["expires_at"],
            revoked=body["revoked"],
            revocation_receipt_ref=body.get("revocation_receipt_ref"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status_id": self.status_id,
            "grant_ref": self.grant_ref,
            "grant_digest": self.grant_digest,
            "tenant_id": self.tenant_id,
            "principal_ref": self.principal_ref,
            "source_ref": self.source_ref,
            "checked_by": self.checked_by,
            "checked_at": self.checked_at,
            "expires_at": self.expires_at,
            "revoked": self.revoked,
            "revocation_receipt_ref": self.revocation_receipt_ref,
        }

    def to_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())

    def to_json(self) -> str:
        return self.to_bytes().decode("utf-8")

    @classmethod
    def from_json(cls, serialized: str | bytes) -> AuthorityStatus:
        return cls.from_dict(_load_json(serialized, "authority status"))

    @property
    def digest(self) -> str:
        return _digest_body(self.to_dict())



@dataclass(frozen=True)
class AssignmentEnvelope:
    """A finite objective admission bound to one exact authority grant."""

    schema_version: str
    assignment_id: str
    employee_id: str
    tenant_id: str
    principal_ref: str
    deployment_ref: str
    deployment_digest: str
    specification_ref: str
    specification_digest: str
    authority_grant_ref: str
    authority_grant_digest: str
    objective: str
    requested_by: str
    acceptance_owner: str
    context_class: str
    purpose: str
    data_class: str
    inputs: tuple[AssignmentInputReference, ...]
    workspace_allowlist: tuple[str, ...]
    system_allowlist: tuple[str, ...]
    credential_scope_refs: tuple[str, ...]
    prohibited_action_classes: tuple[str, ...]
    work_crew_ref: str
    work_crew_config_version: str
    max_model_calls: int
    max_elapsed_seconds: int
    max_cost_usd: str
    max_human_decisions: int
    issued_at: str
    expires_at: str
    stop_conditions: tuple[str, ...]
    refusal_rules: tuple[str, ...]
    escalation_rules: tuple[str, ...]
    rollback_instructions: tuple[str, ...]
    handoff_owner: str
    acceptance_criteria: tuple[str, ...]
    account_ref: str | None = None

    def __post_init__(self) -> None:
        _schema(self.schema_version, ASSIGNMENT_SCHEMA_VERSION, "assignment")
        _text(self.assignment_id, "assignment_id")
        if _DIGEST_RE.fullmatch(self.assignment_id):
            raise AssignmentError(
                "assignment_id must be an assignment ID, not a digest"
            )
        _identifier(self.employee_id, "employee_id")
        _tenant(self.tenant_id, "tenant_id")
        _text(self.principal_ref, "principal_ref")
        _text(self.deployment_ref, "deployment_ref")
        _digest(self.deployment_digest, "deployment_digest")
        _text(self.specification_ref, "specification_ref")
        _digest(self.specification_digest, "specification_digest")
        _text(self.authority_grant_ref, "authority_grant_ref")
        if _DIGEST_RE.fullmatch(self.authority_grant_ref):
            raise AssignmentError(
                "authority_grant_ref must be a grant ID, not a digest"
            )
        _digest(self.authority_grant_digest, "authority_grant_digest")
        _plain_text(self.objective, "objective")
        _text(self.requested_by, "requested_by")
        _text(self.acceptance_owner, "acceptance_owner")
        if self.account_ref is not None:
            _text(self.account_ref, "account_ref")
        _plain_text(self.context_class, "context_class")
        _text(self.purpose, "purpose")
        _plain_text(self.data_class, "data_class")
        _, issued = _timestamp(self.issued_at, "issued_at")
        _, expires = _timestamp(self.expires_at, "expires_at")
        _check_window(issued, expires, "assignment")

        inputs = _tuple(self.inputs, "inputs")
        if not inputs:
            raise AssignmentError("inputs must not be empty")
        if not all(isinstance(item, AssignmentInputReference) for item in inputs):
            raise AssignmentError("inputs contains an invalid reference")
        _unique(tuple(item.name for item in inputs), "inputs.name")
        _unique(tuple(item.artifact_id for item in inputs), "inputs.artifact_id")
        for item in inputs:
            if item.tenant_id != self.tenant_id:
                raise AssignmentError("input tenant_id does not match assignment")
            if (
                self.account_ref is not None
                and item.account_ref is not None
                and item.account_ref != self.account_ref
            ):
                raise AssignmentError("input account_ref does not match assignment")
            if item.context_class != self.context_class:
                raise AssignmentError("input context_class does not match assignment")
            if item.purpose != self.purpose:
                raise AssignmentError("input purpose does not match assignment")
            if item.data_class != self.data_class:
                raise AssignmentError("input data_class does not match assignment")
            _, observed = _timestamp(
                item.observed_at,
                "assignment input.observed_at",
            )
            if observed > issued:
                raise AssignmentError(
                    "assignment input.observed_at is later than assignment issued_at"
                )
            _, input_expires = _timestamp(
                item.expires_at,
                "assignment input.expires_at",
            )
            if input_expires < expires:
                raise AssignmentError(
                    "assignment expires after an admitted input expires"
                )

        _refs(self.workspace_allowlist, "workspace_allowlist", required=True)
        _refs(self.system_allowlist, "system_allowlist", required=True)
        _refs(self.credential_scope_refs, "credential_scope_refs")
        _classes(
            self.prohibited_action_classes,
            "prohibited_action_classes",
            required=True,
        )
        _text(self.work_crew_ref, "work_crew_ref")
        _plain_text(self.work_crew_config_version, "work_crew_config_version")
        _positive_int(self.max_model_calls, "max_model_calls")
        _positive_int(self.max_elapsed_seconds, "max_elapsed_seconds")
        _decimal(self.max_cost_usd, "max_cost_usd")
        _decision_cap(self.max_human_decisions)
        _classes(self.stop_conditions, "stop_conditions", required=True)
        _classes(self.refusal_rules, "refusal_rules", required=True)
        _classes(self.escalation_rules, "escalation_rules", required=True)
        _classes(self.rollback_instructions, "rollback_instructions", required=True)
        _text(self.handoff_owner, "handoff_owner")
        _classes(self.acceptance_criteria, "acceptance_criteria", required=True)

    @classmethod
    def from_dict(cls, body: Any) -> AssignmentEnvelope:
        required = frozenset(
            {
                "schema_version",
                "assignment_id",
                "employee_id",
                "tenant_id",
                "principal_ref",
                "deployment_ref",
                "deployment_digest",
                "specification_ref",
                "specification_digest",
                "authority_grant_ref",
                "authority_grant_digest",
                "objective",
                "requested_by",
                "acceptance_owner",
                "context_class",
                "purpose",
                "data_class",
                "inputs",
                "workspace_allowlist",
                "system_allowlist",
                "credential_scope_refs",
                "prohibited_action_classes",
                "work_crew_ref",
                "work_crew_config_version",
                "max_model_calls",
                "max_elapsed_seconds",
                "max_cost_usd",
                "max_human_decisions",
                "issued_at",
                "expires_at",
                "stop_conditions",
                "refusal_rules",
                "escalation_rules",
                "rollback_instructions",
                "handoff_owner",
                "acceptance_criteria",
            }
        )
        body = _strict_object(
            body,
            field="assignment",
            required=required,
            optional=frozenset({"account_ref"}),
        )
        return cls(
            schema_version=body["schema_version"],
            assignment_id=body["assignment_id"],
            employee_id=body["employee_id"],
            tenant_id=body["tenant_id"],
            principal_ref=body["principal_ref"],
            deployment_ref=body["deployment_ref"],
            deployment_digest=body["deployment_digest"],
            specification_ref=body["specification_ref"],
            specification_digest=body["specification_digest"],
            authority_grant_ref=body["authority_grant_ref"],
            authority_grant_digest=body["authority_grant_digest"],
            objective=body["objective"],
            requested_by=body["requested_by"],
            acceptance_owner=body["acceptance_owner"],
            account_ref=body.get("account_ref"),
            context_class=body["context_class"],
            purpose=body["purpose"],
            data_class=body["data_class"],
            inputs=tuple(
                AssignmentInputReference.from_dict(item)
                for item in _array(body["inputs"], "inputs")
            ),
            workspace_allowlist=tuple(
                _array(body["workspace_allowlist"], "workspace_allowlist")
            ),
            system_allowlist=tuple(
                _array(body["system_allowlist"], "system_allowlist")
            ),
            credential_scope_refs=tuple(
                _array(body["credential_scope_refs"], "credential_scope_refs")
            ),
            prohibited_action_classes=tuple(
                _array(
                    body["prohibited_action_classes"],
                    "prohibited_action_classes",
                )
            ),
            work_crew_ref=body["work_crew_ref"],
            work_crew_config_version=body["work_crew_config_version"],
            max_model_calls=body["max_model_calls"],
            max_elapsed_seconds=body["max_elapsed_seconds"],
            max_cost_usd=body["max_cost_usd"],
            max_human_decisions=body["max_human_decisions"],
            issued_at=body["issued_at"],
            expires_at=body["expires_at"],
            stop_conditions=tuple(
                _array(body["stop_conditions"], "stop_conditions")
            ),
            refusal_rules=tuple(_array(body["refusal_rules"], "refusal_rules")),
            escalation_rules=tuple(
                _array(body["escalation_rules"], "escalation_rules")
            ),
            rollback_instructions=tuple(
                _array(body["rollback_instructions"], "rollback_instructions")
            ),
            handoff_owner=body["handoff_owner"],
            acceptance_criteria=tuple(
                _array(body["acceptance_criteria"], "acceptance_criteria")
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "assignment_id": self.assignment_id,
            "employee_id": self.employee_id,
            "tenant_id": self.tenant_id,
            "principal_ref": self.principal_ref,
            "deployment_ref": self.deployment_ref,
            "deployment_digest": self.deployment_digest,
            "specification_ref": self.specification_ref,
            "specification_digest": self.specification_digest,
            "authority_grant_ref": self.authority_grant_ref,
            "authority_grant_digest": self.authority_grant_digest,
            "objective": self.objective,
            "requested_by": self.requested_by,
            "acceptance_owner": self.acceptance_owner,
            "account_ref": self.account_ref,
            "context_class": self.context_class,
            "purpose": self.purpose,
            "data_class": self.data_class,
            "inputs": [item.to_dict() for item in self.inputs],
            "workspace_allowlist": list(self.workspace_allowlist),
            "system_allowlist": list(self.system_allowlist),
            "credential_scope_refs": list(self.credential_scope_refs),
            "prohibited_action_classes": list(self.prohibited_action_classes),
            "work_crew_ref": self.work_crew_ref,
            "work_crew_config_version": self.work_crew_config_version,
            "max_model_calls": self.max_model_calls,
            "max_elapsed_seconds": self.max_elapsed_seconds,
            "max_cost_usd": self.max_cost_usd,
            "max_human_decisions": self.max_human_decisions,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "stop_conditions": list(self.stop_conditions),
            "refusal_rules": list(self.refusal_rules),
            "escalation_rules": list(self.escalation_rules),
            "rollback_instructions": list(self.rollback_instructions),
            "handoff_owner": self.handoff_owner,
            "acceptance_criteria": list(self.acceptance_criteria),
        }

    def to_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())

    def to_json(self) -> str:
        return self.to_bytes().decode("utf-8")

    @classmethod
    def from_json(cls, serialized: str | bytes) -> AssignmentEnvelope:
        return cls.from_dict(_load_json(serialized, "assignment"))

    @property
    def digest(self) -> str:
        return _digest_body(self.to_dict())

    def is_expired(self, at: str | datetime | None = None) -> bool:
        _, issued = _timestamp(self.issued_at, "issued_at")
        _, expires = _timestamp(self.expires_at, "expires_at")
        if at is None:
            current = datetime.now(timezone.utc)
        elif isinstance(at, datetime):
            current = at
        else:
            _, current = _timestamp(at, "validation.now")
        if current.tzinfo is None or current.utcoffset() is None:
            raise AssignmentError("validation.now must include a timezone")
        if current < issued:
            return False
        return current >= expires

    def validate_active(self, at: str | datetime | None = None) -> None:
        _, issued = _timestamp(self.issued_at, "issued_at")
        _, expires = _timestamp(self.expires_at, "expires_at")
        _check_now(at, issued, expires)

    def validate_authority_grant(self, grant: AuthorityGrant) -> None:
        """Prove that a grant is the exact, narrower grant for this assignment."""
        if not isinstance(grant, AuthorityGrant):
            raise AssignmentError("authority grant must be an AuthorityGrant")
        if grant.digest != self.authority_grant_digest:
            raise AssignmentError("authority grant digest does not match assignment")
        if grant.grant_id != self.authority_grant_ref:
            raise AssignmentError("authority grant ref does not match assignment")
        if grant.grant_kind != "assignment":
            raise AssignmentError("assignment requires an assignment-scoped grant")
        if grant.assignment_ref != self.assignment_id:
            raise AssignmentError("authority grant assignment ref does not match")
        for field in (
            "employee_id",
            "tenant_id",
            "principal_ref",
            "context_class",
            "purpose",
            "data_class",
        ):
            if getattr(grant, field) != getattr(self, field):
                raise AssignmentError(
                    f"authority grant {field} does not match assignment"
                )
        allowed_input_refs = set(grant.allowed_read_refs)
        if any(item.artifact_id not in allowed_input_refs for item in self.inputs):
            raise AssignmentError(
                "assignment input artifact is not in authority read allowlist"
            )
        if not set(self.workspace_allowlist) <= set(grant.allowed_write_refs):
            raise AssignmentError("assignment workspace allowlist exceeds authority grant")
        grant_refs = set(grant.allowed_read_refs) | set(grant.allowed_write_refs)
        if not set(self.system_allowlist) <= grant_refs:
            raise AssignmentError("assignment system allowlist exceeds authority grant")
        if not set(self.credential_scope_refs) <= set(grant.credential_scope_refs):
            raise AssignmentError("assignment credential scope exceeds authority grant")
        if not set(grant.prohibited_action_classes) <= set(
            self.prohibited_action_classes
        ):
            raise AssignmentError("assignment may not remove grant prohibitions")
        if not grant.allowed_action_classes:
            raise AssignmentError("authority grant has no explicitly allowed action class")
        _, assignment_issued = _timestamp(self.issued_at, "issued_at")
        _, assignment_expires = _timestamp(self.expires_at, "expires_at")
        _, grant_issued = _timestamp(grant.issued_at, "grant.issued_at")
        _, grant_expires = _timestamp(grant.expires_at, "grant.expires_at")
        if assignment_issued < grant_issued:
            raise AssignmentError("assignment starts before authority grant")
        if grant_expires < assignment_expires:
            raise AssignmentError("assignment outlives authority grant")
def validate_authority_status(
    assignment: AssignmentEnvelope,
    grant: AuthorityGrant,
    status: AuthorityStatus,
    at: str | datetime | None = None,
) -> None:
    """Admit work only while an exact, current, non-revoked status is valid."""
    if not isinstance(status, AuthorityStatus):
        raise AssignmentError("authority status must be an AuthorityStatus")
    status_inputs = tuple(
        item for item in assignment.inputs if item.artifact_id == status.status_id
    )
    if len(status_inputs) != 1:
        raise AssignmentError(
            "assignment must admit exactly one authority status input"
        )
    admitted = status_inputs[0]
    if (
        admitted.artifact_type != "ai-employee-authority-status"
        or admitted.schema_version != AUTHORITY_STATUS_SCHEMA_VERSION
        or admitted.digest != status.digest
    ):
        raise AssignmentError(
            "assignment authority status input does not match status receipt"
        )
    assignment.validate_active(at)
    assignment.validate_authority_grant(grant)
    if status.grant_ref != assignment.authority_grant_ref:
        raise AssignmentError("authority status grant ref does not match assignment")
    if status.grant_digest != assignment.authority_grant_digest:
        raise AssignmentError(
            "authority status grant digest does not match assignment"
        )
    if status.tenant_id != assignment.tenant_id:
        raise AssignmentError("authority status tenant_id does not match assignment")
    if status.principal_ref != assignment.principal_ref:
        raise AssignmentError(
            "authority status principal_ref does not match assignment"
        )
    if at is None:
        current = datetime.now(timezone.utc)
    elif isinstance(at, datetime):
        current = at
    else:
        _, current = _timestamp(at, "validation.now")
    if current.tzinfo is None or current.utcoffset() is None:
        raise AssignmentError("validation.now must include a timezone")
    _, checked = _timestamp(status.checked_at, "authority status.checked_at")
    _, expires = _timestamp(status.expires_at, "authority status.expires_at")
    _, grant_issued = _timestamp(grant.issued_at, "grant.issued_at")
    if checked < grant_issued:
        raise AssignmentError(
            "authority status.checked_at predates authority grant issuance"
        )
    if checked > current:
        raise AssignmentError("authority status.checked_at is in the future")
    if current >= expires:
        raise AssignmentError("authority status is expired")
    if status.revoked:
        raise AssignmentError("authority status is revoked")


def validate_authority_grant(
    assignment: AssignmentEnvelope, grant: AuthorityGrant
) -> None:
    """Validate an exact grant binding without allowing routing metadata to grant."""
    if not isinstance(assignment, AssignmentEnvelope):
        raise AssignmentError("assignment must be an AssignmentEnvelope")
    assignment.validate_authority_grant(grant)

__all__ = [
    "ASSIGNMENT_SCHEMA_VERSION",
    "AUTHORITY_GRANT_SCHEMA_VERSION",
    "AUTHORITY_STATUS_SCHEMA_VERSION",
    "AssignmentEnvelope",
    "AssignmentError",
    "AssignmentInputReference",
    "AuthorityGrant",
    "AuthorityStatus",
    "validate_authority_grant",
    "validate_authority_status",
]
