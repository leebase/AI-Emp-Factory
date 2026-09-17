"""Employee doubles — deterministic stand-ins honouring the same contract.

`docs/workforce-build-order.md` → Development Simulation Strategy, levels L0/L1.
The problem they solve, in the build order's words: without doubles, building
employee N+1 requires standing up employee N live, which is the stall
`project-plan.md` predicts at roughly employee two.

A conforming double must be able to produce the **failure shapes**, not just the
happy path. A double that only ever returns ``complete`` lets a receiver ship
without ever exercising its own declared degradation behaviour — which means the
first time that path runs is on a live pursuit.
"""

from __future__ import annotations

from typing import Any, Mapping

from .contract import Employee, EmployeeSpec, Outcome, RunContext
from .envelope import Artifact, ArtifactStatus, Flag, FlagSeverity


class Double(Employee):
    """A scripted employee. Same contract, no model, fully reproducible."""

    def __init__(
        self,
        spec: EmployeeSpec,
        *,
        payload: dict[str, Any] | None = None,
        status: ArtifactStatus = ArtifactStatus.COMPLETE,
        confidence_score: float = 0.9,
        confidence_basis: str = "double",
        assumptions: tuple[str, ...] = (),
        flags: tuple[Flag, ...] = (),
        refuse_because: str | None = None,
    ) -> None:
        super().__init__(spec)
        self._payload = payload if payload is not None else {"double": True}
        self._status = status
        self._score = confidence_score
        self._basis = confidence_basis
        self._assumptions = assumptions
        self._flags = flags
        self._refuse_because = refuse_because

    def perform(self, inputs: Mapping[str, Artifact], context: RunContext) -> Outcome:
        return Outcome(
            payload=dict(self._payload),
            status=self._status,
            confidence_score=self._score,
            confidence_basis=self._basis,
            assumptions=self._assumptions,
            evidence=(),
            flags=self._flags,
        )

    def run(
        self, inputs: Mapping[str, Artifact] | None = None, *, context: RunContext
    ) -> Artifact:
        if self._refuse_because is not None:
            return self.refuse(
                context=context,
                missing_or_deficient_input="(scripted)",
                what_was_expected="a double scripted to succeed",
                what_was_found=self._refuse_because,
                inputs=inputs or {},
            )
        return super().run(inputs, context=context)


def complete(
    spec: EmployeeSpec, payload: dict[str, Any] | None = None, **kwargs: Any
) -> Double:
    """A double that succeeds — the happy path."""
    return Double(spec, payload=payload, status=ArtifactStatus.COMPLETE, **kwargs)


def partial(
    spec: EmployeeSpec,
    payload: dict[str, Any] | None = None,
    *,
    reason: str = "scripted partial output",
    **kwargs: Any,
) -> Double:
    """A double that produces reduced output at lowered confidence.

    Exercises a receiver's ``degrade`` path.
    """
    kwargs.setdefault("confidence_score", 0.4)
    return Double(
        spec,
        payload=payload,
        status=ArtifactStatus.PARTIAL,
        flags=(
            Flag(
                code="partial_output",
                severity=FlagSeverity.WARNING,
                note=reason,
            ),
        ),
        **kwargs,
    )


def low_confidence(
    spec: EmployeeSpec,
    payload: dict[str, Any] | None = None,
    *,
    score: float = 0.2,
    **kwargs: Any,
) -> Double:
    """A double that succeeds but with confidence a receiver may reject."""
    return Double(
        spec,
        payload=payload,
        status=ArtifactStatus.COMPLETE,
        confidence_score=score,
        **kwargs,
    )


def refusing(spec: EmployeeSpec, *, reason: str = "scripted refusal") -> Double:
    """A double that refuses — exercises a receiver's escalation path."""
    return Double(spec, refuse_because=reason)


#: Every failure shape a conforming double must be able to produce. A
#: receiver tested only against `complete` has never run its own degradation
#: code, and the first execution would be on a live pursuit.
CONFORMANCE_SHAPES: tuple[str, ...] = (
    "complete",
    "partial",
    "low_confidence",
    "refusing",
)


def all_shapes(spec: EmployeeSpec) -> dict[str, Double]:
    """One double per failure shape, for exercising a receiver end to end."""
    return {
        "complete": complete(spec),
        "partial": partial(spec),
        "low_confidence": low_confidence(spec),
        "refusing": refusing(spec),
    }
