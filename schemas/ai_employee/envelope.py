"""The frozen edge contract — the envelope on every artifact between employees.

Specified in `docs/workforce-build-order.md` → Handoff Contracts. Frozen at
rank 2: **no employee negotiates it, no payload overrides it.**

The rule the whole thing serves:

    a consumer must be able to decide whether to trust an input without
    knowing anything about who produced it.

And its enforcement, which is why this module exists at all:

    An artifact missing any envelope field is not a low-quality artifact, it is
    not an artifact — the coordinator rejects it at the edge and it never
    reaches a consumer. That check is deterministic and belongs to the harness,
    not to a model.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

ENVELOPE_VERSION = "rfpf-envelope/1.0"

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}" r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)


def _strict_object(
    body: Any,
    *,
    field_name: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise EnvelopeError(f"{field_name} must be an object")
    allowed = required | optional
    unknown = sorted(str(key) for key in body if key not in allowed)
    if unknown:
        raise EnvelopeError(f"{field_name} has unknown field(s): {', '.join(unknown)}")
    missing = sorted(required - set(body))
    if missing:
        raise EnvelopeError(
            f"{field_name} is missing required field(s): {', '.join(missing)}"
        )
    return body


def _text(value: Any, field_name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise EnvelopeError(f"{field_name} must be a non-empty string")
    return value


def _array(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise EnvelopeError(f"{field_name} must be an array")
    return value


def _timestamp(value: Any, field_name: str) -> str:
    text = _text(value, field_name)
    if _TIMESTAMP_RE.fullmatch(text) is None:
        raise EnvelopeError(f"{field_name} must be RFC3339 with a timezone")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EnvelopeError(f"{field_name} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EnvelopeError(f"{field_name} must include a timezone")
    return text


def _json_value(value: Any, field_name: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EnvelopeError(f"{field_name} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _json_value(item, f"{field_name}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EnvelopeError(f"{field_name} object keys must be strings")
            _json_value(item, f"{field_name}.{key}")
        return
    raise EnvelopeError(f"{field_name} contains a non-JSON value")


class ArtifactStatus(str, Enum):
    """Declared, never inferred from an empty payload.

    ``REFUSED`` is a *successful* outcome. An employee that admits it has
    nothing is working correctly; one that invents rather than refusing is the
    failure this product exists to prevent.
    """

    COMPLETE = "complete"
    PARTIAL = "partial"
    REFUSED = "refused"


class FlagSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class Determination(str, Enum):
    """Whether an employee is *asserting* something or *proposing* it.

    A third position between a confident answer and a refusal, and the one real
    work usually lands in: the employee lacked what it needed to be sure, but
    stopping would have been less useful than saying so.

    ``PROPOSED`` is not a hedge or a low confidence score. It is a distinct
    claim — "a human must decide this, and here is my best reading" — and it
    must be impossible to miss when scanning an artifact, because a proposal
    silently treated as a determination is how an unreviewed guess reaches a
    submitted document.
    """

    DETERMINED = "determined"
    PROPOSED = "proposed"


#: The well-known flag code for work awaiting a human decision. Shared so a
#: supervision surface can filter one code across every employee rather than
#: learning each employee's private vocabulary.
AWAITING_HUMAN = "awaiting_human_determination"


class DegradationBehavior(str, Enum):
    """What a receiver does with a missing, partial, or low-confidence input.

    Exactly three exist. An employee declares one **per input** as part of its
    contract; it is never a runtime choice.
    """

    PROCEED_AND_FLAG = "proceed_and_flag"
    DEGRADE = "degrade"
    REFUSE_AND_ESCALATE = "refuse_and_escalate"


class EnvelopeError(ValueError):
    """Raised at the edge when an artifact is not an artifact."""


@dataclass(frozen=True)
class Producer:
    """Who made this.

    ``provider`` is recorded separately from ``model`` because review
    independence is enforced at the *provider* level, not the model level
    (`AGENTS.md` → The Red Team Rule). A reviewer cannot check independence it
    cannot see.
    """

    employee_id: str
    provider: str
    model: str
    effort: str | None = None
    prompt_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "prompt_version": self.prompt_version,
        }


@dataclass(frozen=True)
class InputRef:
    """Exactly what was read, content-addressed.

    The digest is what makes a reviewer's finding attributable to a *specific*
    draft rather than to "the draft".
    """

    artifact_id: str
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {"artifact_id": self.artifact_id, "digest": self.digest}


@dataclass(frozen=True)
class Flag:
    code: str
    severity: FlagSeverity
    note: str
    subject_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "subject_ref": self.subject_ref,
            "note": self.note,
        }


@dataclass(frozen=True)
class RefusalRecord:
    """A refusal is a real artifact, not an error string."""

    missing_or_deficient_input: str
    what_was_expected: str
    what_was_found: str
    what_would_unblock: str
    escalation_target: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "missing_or_deficient_input": self.missing_or_deficient_input,
            "what_was_expected": self.what_was_expected,
            "what_was_found": self.what_was_found,
            "what_would_unblock": self.what_would_unblock,
            "escalation_target": self.escalation_target,
        }


@dataclass(frozen=True)
class Envelope:
    """Identical on every artifact, from E1 onward."""

    artifact_id: str
    artifact_type: str
    schema_version: str
    pursuit_id: str
    producer: Producer
    run_ref: str
    status: ArtifactStatus
    confidence_score: float
    confidence_basis: str
    created_at: str
    inputs: tuple[InputRef, ...] = ()
    #: A *promoted* knowledge version id, or None for employees with no
    #: knowledge dependency (E1, E3). Never the literal "current", and never a
    #: candidate version — a candidate is not readable by a pursuit.
    knowledge_snapshot: str | None = None
    assumptions: tuple[str, ...] = ()
    evidence: tuple[dict[str, Any], ...] = ()
    flags: tuple[Flag, ...] = ()
    refusal: RefusalRecord | None = None
    envelope_version: str = ENVELOPE_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "envelope_version": self.envelope_version,
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "schema_version": self.schema_version,
            "pursuit_id": self.pursuit_id,
            "producer": self.producer.to_dict(),
            "run_ref": self.run_ref,
            "inputs": [i.to_dict() for i in self.inputs],
            "knowledge_snapshot": self.knowledge_snapshot,
            "status": self.status.value,
            "confidence": {
                "score": round(self.confidence_score, 2),
                "basis": self.confidence_basis,
            },
            "assumptions": list(self.assumptions),
            "evidence": [dict(e) for e in self.evidence],
            "flags": [f.to_dict() for f in self.flags],
            "created_at": self.created_at,
        }
        if self.refusal is not None:
            payload["refusal"] = self.refusal.to_dict()
        return payload


@dataclass(frozen=True)
class Artifact:
    """An envelope plus its payload. The only thing that crosses an edge."""

    envelope: Envelope
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, body: Any) -> Artifact:
        """Strictly reconstruct and verify a serialized artifact."""

        artifact_body = _strict_object(
            body,
            field_name="artifact",
            required=frozenset({"envelope", "payload", "digest"}),
        )
        supplied_digest = _text(artifact_body["digest"], "artifact.digest")
        if _DIGEST_RE.fullmatch(supplied_digest) is None:
            raise EnvelopeError("artifact.digest must be a lowercase sha256 digest")

        envelope_body = _strict_object(
            artifact_body["envelope"],
            field_name="artifact.envelope",
            required=frozenset(
                {
                    "envelope_version",
                    "artifact_id",
                    "artifact_type",
                    "schema_version",
                    "pursuit_id",
                    "producer",
                    "run_ref",
                    "inputs",
                    "knowledge_snapshot",
                    "status",
                    "confidence",
                    "assumptions",
                    "evidence",
                    "flags",
                    "created_at",
                }
            ),
            optional=frozenset({"refusal"}),
        )
        envelope_version = _text(
            envelope_body["envelope_version"],
            "artifact.envelope.envelope_version",
        )
        if envelope_version != ENVELOPE_VERSION:
            raise EnvelopeError(f"unsupported envelope version: {envelope_version!r}")

        producer_body = _strict_object(
            envelope_body["producer"],
            field_name="artifact.envelope.producer",
            required=frozenset(
                {"employee_id", "provider", "model", "effort", "prompt_version"}
            ),
        )
        producer = Producer(
            employee_id=_text(
                producer_body["employee_id"],
                "artifact.envelope.producer.employee_id",
            ),
            provider=_text(
                producer_body["provider"], "artifact.envelope.producer.provider"
            ),
            model=_text(producer_body["model"], "artifact.envelope.producer.model"),
            effort=_text(
                producer_body["effort"],
                "artifact.envelope.producer.effort",
                optional=True,
            ),
            prompt_version=_text(
                producer_body["prompt_version"],
                "artifact.envelope.producer.prompt_version",
                optional=True,
            ),
        )

        input_refs: list[InputRef] = []
        input_ids: set[str] = set()
        for index, item in enumerate(
            _array(envelope_body["inputs"], "artifact.envelope.inputs")
        ):
            input_body = _strict_object(
                item,
                field_name=f"artifact.envelope.inputs[{index}]",
                required=frozenset({"artifact_id", "digest"}),
            )
            artifact_id = _text(
                input_body["artifact_id"],
                f"artifact.envelope.inputs[{index}].artifact_id",
            )
            input_digest = _text(
                input_body["digest"], f"artifact.envelope.inputs[{index}].digest"
            )
            if _DIGEST_RE.fullmatch(input_digest) is None:
                raise EnvelopeError(
                    f"artifact.envelope.inputs[{index}].digest must be a "
                    "lowercase sha256 digest"
                )
            if artifact_id in input_ids:
                raise EnvelopeError(
                    "artifact.envelope.inputs contains duplicate artifact_id "
                    f"{artifact_id!r}"
                )
            input_ids.add(artifact_id)
            input_refs.append(InputRef(artifact_id=artifact_id, digest=input_digest))

        confidence_body = _strict_object(
            envelope_body["confidence"],
            field_name="artifact.envelope.confidence",
            required=frozenset({"score", "basis"}),
        )
        confidence_score = confidence_body["score"]
        if (
            isinstance(confidence_score, bool)
            or not isinstance(confidence_score, (int, float))
            or not math.isfinite(float(confidence_score))
            or not 0.0 <= float(confidence_score) <= 1.0
        ):
            raise EnvelopeError(
                "artifact.envelope.confidence.score must be a finite number "
                "within 0..1"
            )

        assumptions = tuple(
            _text(item, f"artifact.envelope.assumptions[{index}]")
            for index, item in enumerate(
                _array(envelope_body["assumptions"], "artifact.envelope.assumptions")
            )
        )
        evidence_items: list[dict[str, Any]] = []
        for index, item in enumerate(
            _array(envelope_body["evidence"], "artifact.envelope.evidence")
        ):
            if not isinstance(item, dict):
                raise EnvelopeError(
                    f"artifact.envelope.evidence[{index}] must be an object"
                )
            _json_value(item, f"artifact.envelope.evidence[{index}]")
            evidence_items.append(dict(item))

        flags: list[Flag] = []
        for index, item in enumerate(
            _array(envelope_body["flags"], "artifact.envelope.flags")
        ):
            flag_body = _strict_object(
                item,
                field_name=f"artifact.envelope.flags[{index}]",
                required=frozenset({"code", "severity", "subject_ref", "note"}),
            )
            try:
                severity = FlagSeverity(flag_body["severity"])
            except (TypeError, ValueError) as exc:
                raise EnvelopeError(
                    f"artifact.envelope.flags[{index}].severity is unsupported"
                ) from exc
            flags.append(
                Flag(
                    code=_text(
                        flag_body["code"], f"artifact.envelope.flags[{index}].code"
                    ),
                    severity=severity,
                    subject_ref=_text(
                        flag_body["subject_ref"],
                        f"artifact.envelope.flags[{index}].subject_ref",
                        optional=True,
                    ),
                    note=_text(
                        flag_body["note"], f"artifact.envelope.flags[{index}].note"
                    ),
                )
            )

        refusal: RefusalRecord | None = None
        if "refusal" in envelope_body:
            refusal_body = _strict_object(
                envelope_body["refusal"],
                field_name="artifact.envelope.refusal",
                required=frozenset(
                    {
                        "missing_or_deficient_input",
                        "what_was_expected",
                        "what_was_found",
                        "what_would_unblock",
                        "escalation_target",
                    }
                ),
            )
            refusal = RefusalRecord(
                missing_or_deficient_input=_text(
                    refusal_body["missing_or_deficient_input"],
                    "artifact.envelope.refusal.missing_or_deficient_input",
                ),
                what_was_expected=_text(
                    refusal_body["what_was_expected"],
                    "artifact.envelope.refusal.what_was_expected",
                ),
                what_was_found=_text(
                    refusal_body["what_was_found"],
                    "artifact.envelope.refusal.what_was_found",
                ),
                what_would_unblock=_text(
                    refusal_body["what_would_unblock"],
                    "artifact.envelope.refusal.what_would_unblock",
                ),
                escalation_target=_text(
                    refusal_body["escalation_target"],
                    "artifact.envelope.refusal.escalation_target",
                ),
            )

        try:
            status = ArtifactStatus(envelope_body["status"])
        except (TypeError, ValueError) as exc:
            raise EnvelopeError(f"unknown status {envelope_body['status']!r}") from exc

        payload = artifact_body["payload"]
        if not isinstance(payload, dict):
            raise EnvelopeError("artifact.payload must be an object")
        _json_value(payload, "artifact.payload")
        envelope = Envelope(
            artifact_id=_text(
                envelope_body["artifact_id"], "artifact.envelope.artifact_id"
            ),
            artifact_type=_text(
                envelope_body["artifact_type"], "artifact.envelope.artifact_type"
            ),
            schema_version=_text(
                envelope_body["schema_version"], "artifact.envelope.schema_version"
            ),
            pursuit_id=_text(
                envelope_body["pursuit_id"], "artifact.envelope.pursuit_id"
            ),
            producer=producer,
            run_ref=_text(envelope_body["run_ref"], "artifact.envelope.run_ref"),
            status=status,
            confidence_score=confidence_score,
            confidence_basis=_text(
                confidence_body["basis"], "artifact.envelope.confidence.basis"
            ),
            created_at=_timestamp(
                envelope_body["created_at"], "artifact.envelope.created_at"
            ),
            inputs=tuple(input_refs),
            knowledge_snapshot=_text(
                envelope_body["knowledge_snapshot"],
                "artifact.envelope.knowledge_snapshot",
                optional=True,
            ),
            assumptions=assumptions,
            evidence=tuple(evidence_items),
            flags=tuple(flags),
            refusal=refusal,
            envelope_version=envelope_version,
        )
        artifact = cls(envelope=envelope, payload=dict(payload))
        validate(artifact.to_dict())
        recomputed = artifact.digest
        if supplied_digest != recomputed:
            raise EnvelopeError(
                "artifact digest mismatch: "
                f"supplied {supplied_digest}, recomputed {recomputed}"
            )
        return artifact

    @classmethod
    def from_json(cls, serialized: str | bytes) -> Artifact:
        """Strictly decode JSON, rejecting duplicate keys and non-finite numbers."""

        if isinstance(serialized, bytes):
            try:
                serialized = serialized.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise EnvelopeError("artifact JSON must be UTF-8") from exc
        if not isinstance(serialized, str):
            raise EnvelopeError("artifact JSON must be a string or UTF-8 bytes")

        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise EnvelopeError(f"artifact JSON repeats field {key!r}")
                result[key] = value
            return result

        def reject_constant(value: str) -> None:
            raise EnvelopeError(f"artifact JSON contains non-finite number {value}")

        try:
            body = json.loads(
                serialized,
                object_pairs_hook=reject_duplicates,
                parse_constant=reject_constant,
            )
        except EnvelopeError:
            raise
        except (json.JSONDecodeError, TypeError) as exc:
            raise EnvelopeError("artifact JSON is malformed") from exc
        return cls.from_dict(body)

    def to_dict(self) -> dict[str, Any]:
        body = {"envelope": self.envelope.to_dict(), "payload": self.payload}
        body["digest"] = digest_of(body)
        return body

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @property
    def digest(self) -> str:
        return digest_of({"envelope": self.envelope.to_dict(), "payload": self.payload})

    def as_input_ref(self) -> InputRef:
        return InputRef(artifact_id=self.envelope.artifact_id, digest=self.digest)


def digest_of(body: dict[str, Any]) -> str:
    """Content address. Stable across runs for identical content."""
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


#: Every field the coordinator requires. Absence is rejection, not degradation.
REQUIRED_ENVELOPE_FIELDS: tuple[str, ...] = (
    "envelope_version",
    "artifact_id",
    "artifact_type",
    "schema_version",
    "pursuit_id",
    "producer",
    "run_ref",
    "inputs",
    "status",
    "confidence",
    "assumptions",
    "evidence",
    "flags",
    "created_at",
)
_REQUIRED_PRODUCER_FIELDS: tuple[str, ...] = ("employee_id", "provider", "model")
#: Present-but-empty is the same defect as absent: an artifact whose type is ""
#: is not a typed artifact, and a consumer routing on it would route nowhere.
_NON_EMPTY_ENVELOPE_FIELDS: tuple[str, ...] = (
    "envelope_version",
    "artifact_id",
    "artifact_type",
    "schema_version",
    "pursuit_id",
    "run_ref",
    "created_at",
)


def validate(artifact_body: dict[str, Any]) -> None:
    """Reject anything that is not an artifact. Raises :class:`EnvelopeError`.

    Deterministic and harness-owned by design: a model must never be the thing
    deciding whether its own output is well-formed.
    """
    envelope = artifact_body.get("envelope")
    if not isinstance(envelope, dict):
        raise EnvelopeError("artifact has no envelope")

    missing = [f for f in REQUIRED_ENVELOPE_FIELDS if f not in envelope]
    if missing:
        raise EnvelopeError(
            "envelope is missing required field(s): " + ", ".join(sorted(missing))
        )

    empty = [
        f for f in _NON_EMPTY_ENVELOPE_FIELDS if not str(envelope.get(f, "")).strip()
    ]
    if empty:
        raise EnvelopeError(
            "envelope field(s) present but empty: " + ", ".join(sorted(empty))
        )

    producer = envelope.get("producer")
    if not isinstance(producer, dict):
        raise EnvelopeError("envelope.producer must be an object")
    missing_producer = [f for f in _REQUIRED_PRODUCER_FIELDS if not producer.get(f)]
    if missing_producer:
        raise EnvelopeError(
            "envelope.producer is missing: " + ", ".join(sorted(missing_producer))
        )

    try:
        status = ArtifactStatus(envelope["status"])
    except ValueError as exc:
        raise EnvelopeError(f"unknown status {envelope['status']!r}") from exc

    confidence = envelope.get("confidence")
    if not isinstance(confidence, dict) or "score" not in confidence:
        raise EnvelopeError("envelope.confidence must carry a score")
    score = confidence["score"]
    if not isinstance(score, (int, float)) or not 0.0 <= float(score) <= 1.0:
        raise EnvelopeError(f"confidence.score must be within 0..1, got {score!r}")
    if not confidence.get("basis"):
        raise EnvelopeError(
            "confidence.basis is required — a bare score is not evidence"
        )

    if envelope.get("knowledge_snapshot") == "current":
        raise EnvelopeError(
            "knowledge_snapshot must be a resolved promoted version id, never "
            "the literal 'current' — a pursuit must be reproducible"
        )

    # A refusal must carry its record; a non-refusal must not claim one.
    if status is ArtifactStatus.REFUSED:
        if not envelope.get("refusal"):
            raise EnvelopeError(
                "status 'refused' requires a refusal record — a refusal is an "
                "artifact, not an error string"
            )
        if artifact_body.get("payload"):
            raise EnvelopeError("status 'refused' must carry no payload")
    elif envelope.get("refusal"):
        raise EnvelopeError("only a refused artifact may carry a refusal record")
