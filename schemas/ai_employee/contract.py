"""The employee contract — what every AI employee is, uniformly.

`architecture.md` → The Employee Contract. The point of rank 2 is that adding
employee N+1 is domain logic and configuration, not new architecture — so the
shape below is deliberately small and deliberately mandatory.

Two things are enforced rather than encouraged:

* **Degradation is declared, not improvised.** An employee states, per input,
  which of the three behaviors it performs. It cannot decide at runtime.
* **Refusal is a first-class success.** ``refuse()`` produces a real artifact
  with a refusal record. An employee that invents rather than refusing is the
  failure this product exists to prevent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping

from .envelope import (
    Artifact,
    ArtifactStatus,
    DegradationBehavior,
    Envelope,
    EnvelopeError,
    Flag,
    FlagSeverity,
    InputRef,
    Producer,
    RefusalRecord,
    validate,
)


@dataclass(frozen=True)
class InputSpec:
    """One declared input, and what this employee does when it is deficient."""

    name: str
    artifact_type: str
    required: bool = True
    #: What to do when the input is present but unreliable — partial, refused
    #: upstream, or below the confidence floor.
    on_deficient: DegradationBehavior = DegradationBehavior.REFUSE_AND_ESCALATE
    #: What to do when the input is **absent entirely**. Distinct from
    #: `on_deficient` because the two are genuinely different situations: a
    #: partial evidence pack is the normal case and degrading is right, while
    #: a missing one leaves the employee with no bound at all. Collapsing them let
    #: E9 degrade past an absent pack and then crash reaching for it.
    on_absent: DegradationBehavior = DegradationBehavior.REFUSE_AND_ESCALATE
    #: Below this confidence an input counts as deficient.
    min_confidence: float = 0.0
    #: Refuse unless this employee runs on a different **provider** than the one
    #: that produced this input.
    #:
    #: The Red Team Rule, made mechanical. A model reviewing its own provider's
    #: output produces process refinements, not real criticism — observed on the
    #: erd-tool mission, 2026-07-23, and the reason `producer.provider` is on
    #: every envelope. Enforced at provider level rather than model level
    #: because two models from one lab share training and failure modes.
    requires_independent_provider: bool = False
    #: When declared, the input must carry this exact envelope schema version.
    #: Keeping the field at the end preserves every legacy positional call.
    schema_version: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version is not None and (
            not isinstance(self.schema_version, str) or not self.schema_version.strip()
        ):
            raise ValueError("InputSpec.schema_version must be a non-empty string")


@dataclass(frozen=True)
class EmployeeSpec:
    """The declaration an employee makes about itself."""

    employee_id: str
    name: str
    produces: str
    inputs: tuple[InputSpec, ...] = ()
    #: Employees with no knowledge-layer dependency (E1, E3) declare False, and
    #: their envelopes carry knowledge_snapshot=None legitimately.
    reads_knowledge_layer: bool = False
    escalation_target: str = "human:proposal-manager"
    #: Explicit payload contract version. Existing employees retain the legacy
    #: ``<artifact_type>/0.1`` default when this is not declared.
    output_schema_version: str | None = None


@dataclass
class RunContext:
    """Everything the envelope needs that is not the employee's own judgment.

    ``now`` is injected rather than read from the clock so a doubles-driven
    simulation is reproducible — the same inputs must produce the same digest.
    """

    pursuit_id: str
    run_ref: str
    provider: str
    model: str
    now: str
    effort: str | None = None
    prompt_version: str | None = None
    knowledge_snapshot: str | None = None
    sequence: int = 0

    def next_artifact_id(self, employee_id: str) -> str:
        self.sequence += 1
        return f"{self.pursuit_id}:{employee_id}:{self.sequence:04d}"


@dataclass
class Outcome:
    """What an employee's domain logic returns, before enveloping."""

    payload: dict[str, Any]
    status: ArtifactStatus = ArtifactStatus.COMPLETE
    confidence_score: float = 1.0
    confidence_basis: str = "deterministic"
    assumptions: tuple[str, ...] = ()
    evidence: tuple[dict[str, Any], ...] = ()
    flags: tuple[Flag, ...] = field(default_factory=tuple)


class Employee(ABC):
    """Base class for every AI employee.

    Subclasses implement :meth:`perform` and nothing else about enveloping,
    validation, degradation, or refusal — those are the harness's job, so they
    cannot be got subtly wrong twelve different ways.
    """

    spec: EmployeeSpec

    def __init__(self, spec: EmployeeSpec) -> None:
        self.spec = spec

    # -- domain logic ------------------------------------------------------
    @abstractmethod
    def perform(self, inputs: Mapping[str, Artifact], context: RunContext) -> Outcome:
        """Do the work. Raise nothing for deficient inputs — the harness has
        already applied the declared degradation behaviour before calling."""

    # -- harness -----------------------------------------------------------
    def run(
        self, inputs: Mapping[str, Artifact] | None = None, *, context: RunContext
    ) -> Artifact:
        """Validate inputs, apply declared degradation, perform, envelope, verify."""
        supplied = dict(inputs or {})
        for artifact in supplied.values():
            validate(artifact.to_dict())  # never trust an upstream blindly

        # Independence is checked before anything else. An employee whose value
        # is independence producing output anyway would be worse than producing
        # none: it would carry a reviewed stamp it did not earn.
        for spec in self.spec.inputs:
            if not spec.requires_independent_provider:
                continue
            artifact = supplied.get(spec.name)
            if artifact is None:
                continue
            producer = artifact.envelope.producer.provider
            if producer and producer == context.provider:
                return self.refuse(
                    context=context,
                    missing_or_deficient_input=spec.name,
                    what_was_expected=(
                        f"a reviewer on a provider other than {producer!r}"
                    ),
                    what_was_found=(
                        f"this employee is running on {context.provider!r}, the "
                        f"same provider that produced {spec.name}. A model "
                        "reviewing its own provider's output yields process "
                        "refinements, not real criticism."
                    ),
                    what_would_unblock=(
                        "route this employee to a different provider and re-run"
                    ),
                    inputs=supplied,
                )

        deficiencies = self._deficiencies(supplied)

        for spec, reason in deficiencies:
            behavior = (
                spec.on_absent
                if reason == "input was not supplied"
                else spec.on_deficient
            )
            if behavior is DegradationBehavior.REFUSE_AND_ESCALATE:
                return self.refuse(
                    context=context,
                    missing_or_deficient_input=spec.name,
                    what_was_expected=(
                        f"a {spec.artifact_type} with confidence "
                        f">= {spec.min_confidence}"
                    ),
                    what_was_found=reason,
                    inputs=supplied,
                )

        outcome = self.perform(supplied, context)

        extra_flags: list[Flag] = list(outcome.flags)
        degraded = False
        for spec, reason in deficiencies:
            behavior = (
                spec.on_absent
                if reason == "input was not supplied"
                else spec.on_deficient
            )
            extra_flags.append(
                Flag(
                    code="degraded_input",
                    severity=FlagSeverity.WARNING,
                    subject_ref=spec.name,
                    note=f"{behavior.value}: {reason}",
                )
            )
            if behavior is DegradationBehavior.DEGRADE:
                degraded = True

        status = outcome.status
        score = outcome.confidence_score
        if degraded:
            # `degrade` lowers its own confidence; `proceed_and_flag` does not.
            status = ArtifactStatus.PARTIAL
            score = min(score, 0.5)

        artifact = self._envelope(
            context=context,
            payload=outcome.payload,
            status=status,
            confidence_score=score,
            confidence_basis=outcome.confidence_basis,
            assumptions=outcome.assumptions,
            evidence=outcome.evidence,
            flags=tuple(extra_flags),
            inputs=supplied,
        )
        validate(artifact.to_dict())  # the harness checks its own output too
        return artifact

    def refuse(
        self,
        *,
        context: RunContext,
        missing_or_deficient_input: str,
        what_was_expected: str,
        what_was_found: str,
        what_would_unblock: str | None = None,
        inputs: Mapping[str, Artifact] | None = None,
    ) -> Artifact:
        """Emit a refusal. A refusal is a successful outcome of an employee."""
        record = RefusalRecord(
            missing_or_deficient_input=missing_or_deficient_input,
            what_was_expected=what_was_expected,
            what_was_found=what_was_found,
            what_would_unblock=(
                what_would_unblock
                or f"supply a valid {missing_or_deficient_input} and re-run"
            ),
            escalation_target=self.spec.escalation_target,
        )
        artifact = self._envelope(
            context=context,
            payload={},
            status=ArtifactStatus.REFUSED,
            confidence_score=0.0,
            confidence_basis="refused",
            assumptions=(),
            evidence=(),
            flags=(
                Flag(
                    code="refusal",
                    severity=FlagSeverity.BLOCKING,
                    subject_ref=missing_or_deficient_input,
                    note=what_was_found,
                ),
            ),
            inputs=dict(inputs or {}),
            refusal=record,
        )
        validate(artifact.to_dict())
        return artifact

    # -- internals ---------------------------------------------------------
    def _deficiencies(
        self, supplied: Mapping[str, Artifact]
    ) -> list[tuple[InputSpec, str]]:
        found: list[tuple[InputSpec, str]] = []
        for spec in self.spec.inputs:
            artifact = supplied.get(spec.name)
            if artifact is None:
                if spec.required:
                    found.append((spec, "input was not supplied"))
                continue
            env = artifact.envelope
            if env.artifact_type != spec.artifact_type:
                found.append(
                    (spec, f"expected {spec.artifact_type}, got {env.artifact_type}")
                )
            elif (
                spec.schema_version is not None
                and env.schema_version != spec.schema_version
            ):
                found.append(
                    (
                        spec,
                        f"expected schema {spec.schema_version}, got "
                        f"{env.schema_version}",
                    )
                )
            elif env.status is ArtifactStatus.REFUSED:
                found.append((spec, "upstream refused"))
            elif env.status is ArtifactStatus.PARTIAL:
                found.append((spec, "upstream is partial"))
            elif env.confidence_score < spec.min_confidence:
                found.append(
                    (
                        spec,
                        f"confidence {env.confidence_score} below required "
                        f"{spec.min_confidence}",
                    )
                )
        return found

    def _envelope(
        self,
        *,
        context: RunContext,
        payload: dict[str, Any],
        status: ArtifactStatus,
        confidence_score: float,
        confidence_basis: str,
        assumptions: tuple[str, ...],
        evidence: tuple[dict[str, Any], ...],
        flags: tuple[Flag, ...],
        inputs: Mapping[str, Artifact],
        refusal: RefusalRecord | None = None,
    ) -> Artifact:
        if self.spec.reads_knowledge_layer and context.knowledge_snapshot is None:
            raise EnvelopeError(
                f"{self.spec.employee_id} reads the knowledge layer but the run "
                "context carries no promoted knowledge_snapshot"
            )
        input_refs: tuple[InputRef, ...] = tuple(
            artifact.as_input_ref() for artifact in inputs.values()
        )
        schema_version = (
            f"{self.spec.produces}/0.1"
            if self.spec.output_schema_version is None
            else self.spec.output_schema_version
        )
        if not isinstance(schema_version, str) or not schema_version.strip():
            raise EnvelopeError("output_schema_version must be a non-empty string")
        envelope = Envelope(
            artifact_id=context.next_artifact_id(self.spec.employee_id),
            artifact_type=self.spec.produces,
            schema_version=schema_version,
            pursuit_id=context.pursuit_id,
            producer=Producer(
                employee_id=self.spec.employee_id,
                provider=context.provider,
                model=context.model,
                effort=context.effort,
                prompt_version=context.prompt_version,
            ),
            run_ref=context.run_ref,
            status=status,
            confidence_score=confidence_score,
            confidence_basis=confidence_basis,
            created_at=context.now,
            inputs=input_refs,
            knowledge_snapshot=(
                context.knowledge_snapshot if self.spec.reads_knowledge_layer else None
            ),
            assumptions=assumptions,
            evidence=evidence,
            flags=flags,
            refusal=refusal,
        )
        return Artifact(envelope=envelope, payload=payload)
