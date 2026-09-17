"""Pure staffing-block representation and deterministic renderers (P2-4).

The compact ``auto`` staffing block is the D203-format disclosure that the
Phase 2 ``staff`` command emits for one role/class.  This module is a pure
consumer of already-computed facts — it never performs I/O, never loads a
catalog, never dispatches, and never maps a class key to a route (D205/D206):
every route id, evidence join, proof status, and ladder figure is supplied by
the caller.

Facts carried (each absent fact explicit, never invented):

* an optional **Supervisor** route with its channel/headroom facts;
* **Workers** — every caller-supplied eligibility row, in input order, with
  route, channel, headroom, badge, and D211 proof status;
* the **Reason** for the selected route — ``accepted k/n comparable (level)``
  from the evidence join, together with the join's benchmark-prior count
  (``prior_n``) and production-posterior count (``posterior_n``).  The
  Laplace estimate is shown only together with ``k``/``n`` and the used
  level; with no comparable evidence at all the reason is the explicit
  unavailable disclosure;
* the **Expected cost** — the ladder's argmin expected cost, or the literal
  ``unavailable`` with the ladder's stated reasons (D211 ruling 3);
* **Escalation** — the ladder's escalation chain, which stops at the human
  terminal cost and never crosses the never-automatic boundary; the
  never-automatic routes themselves are listed as bind-only;
* an independent **Review** route — named only when the caller passes one,
  with its eligibility, its exclusion reason, whether author-route
  independence was evaluated, and which route was the actual author
  independence reference (an explicit ``--author-route`` when supplied,
  otherwise the selected worker); the reviewed route and its model family
  are always excluded from the review choice;
* the **Expected cost** and **Escalation** of the *actual selected route*:
  when proof-first selection overrides the ladder's pure argmin, the
  expected cost is the selected rung's own ``E`` and the escalation chain
  is the ladder's suffix starting at the selected route — the rejected
  argmin is disclosed as the ladder argmin, never silently reused as the
  selected route's figures.

Text and JSON carry exactly the same facts: :func:`render_json` is the
structured form of :func:`render_text`.  Input order is preserved
everywhere; nothing is sorted, ranked, or re-derived here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from lee_llm_router.staffing.proof import ProofStatus

__all__ = [
    "AUTHORITY_POLICY",
    "MODE_AUTO",
    "PROVEN",
    "STAFFING_BLOCK_JSON_KEYS",
    "UNPROVEN",
    "ReasonFacts",
    "StaffingBlock",
    "WorkerFacts",
    "build_auto_block",
    "render_json",
    "render_text",
]

MODE_AUTO = "auto"
"""The only mode this module renders (D211 ruling 5: the ``auto`` block)."""

AUTHORITY_POLICY = "policy"
"""The fixed authority of a computed ``auto`` block (D211 ruling 5)."""

PROVEN = ProofStatus.PROVEN.value
UNPROVEN = ProofStatus.UNPROVEN.value

#: Top-level key order of the JSON rendering (the text section order).
STAFFING_BLOCK_JSON_KEYS: tuple[str, ...] = (
    "mode",
    "authority",
    "role",
    "class_key",
    "supervisor",
    "workers",
    "selected_route",
    "selected_instance",
    "reason",
    "expected_cost",
    "escalation",
    "never_automatic",
    "unproven_routes",
    "review",
)

_NEVER_AUTOMATIC_REASON = "never_automatic"
_HEADER_PREFIX = "staff auto"
_NO_SUPERVISOR = "unavailable (no supervisor route supplied)"
_UNKNOWN = "unknown"
_ARROW = " -> "


def _field(value: object, name: str, default: object = None) -> object:
    """Read one attribute from an object or mapping, else the default."""
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _fmt_headroom(value: float | None) -> str:
    """Render headroom as the explicit ``unknown`` or a compact number."""
    return _UNKNOWN if value is None else f"{value:g}"


def _fmt_usd(value: float) -> str:
    return f"${value:.4f}"


def _proof_value(proof_status: object) -> str:
    if isinstance(proof_status, ProofStatus):
        raw: object = proof_status.value
    else:
        raw = proof_status
    return PROVEN if raw == PROVEN else UNPROVEN


@dataclass(frozen=True)
class WorkerFacts:
    """One route's already-computed facts, verbatim from the caller.

    ``headroom``/``badge`` stay ``None`` when the eligibility row carries
    none — the renderers then say ``unknown`` rather than inventing a value.
    ``proof_status`` is exactly ``"proven"`` or ``"unproven"`` (D211 ruling
    4; a missing proof record is unproven, never assumed proven).
    """

    route_id: str
    channel: str | None
    headroom: float | None
    badge: str | None
    eligible: bool
    reasons: tuple[str, ...]
    proof_status: str
    instance_headrooms: tuple[dict[str, Any], ...] = ()

    def describe(self) -> str:
        """One deterministic compact description of the route's facts."""
        channel = self.channel if self.channel is not None else _UNKNOWN
        headroom = _fmt_headroom(self.headroom)
        badge = self.badge if self.badge is not None else _UNKNOWN
        eligibility = (
            "eligible"
            if self.eligible
            else f"excluded: {'; '.join(self.reasons) or 'no reason supplied'}"
        )
        return (
            f"{self.route_id} (channel {channel}, headroom {headroom}, "
            f"badge {badge}, {eligibility}, {self.proof_status})"
        )

    def as_dict(self) -> dict[str, Any]:
        """The same facts as a JSON-safe mapping, in a fixed key order."""
        return {
            "route_id": self.route_id,
            "channel": self.channel,
            "headroom": self.headroom,
            "badge": self.badge,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "proof_status": self.proof_status,
            "instance_headrooms": [
                {
                    "instance_id": entry["instance_id"],
                    "eligible": entry["eligible"],
                    "reasons": list(entry["reasons"]),
                    "remaining_fraction": entry["remaining_fraction"],
                    "health": entry["health"],
                    "badge": entry["badge"],
                }
                for entry in self.instance_headrooms
            ],
        }


def _instance_dict(entry: object) -> dict[str, Any]:
    """Convert an EligibilityInstance or mapping into a JSON-safe dict."""
    raw_id = _field(entry, "instance_id")
    instance_id = (
        raw_id if isinstance(raw_id, str) else ("" if raw_id is None else str(raw_id))
    )
    eligible = bool(_field(entry, "eligible", False))
    raw_reasons = _field(entry, "reasons", ())
    reasons = [
        str(r)
        for r in (raw_reasons if isinstance(raw_reasons, (list, tuple)) else ())
        if isinstance(r, str) and r
    ]
    remaining_fraction = _float_or_none(_field(entry, "remaining_fraction"))
    raw_health = _field(entry, "health", "unknown")
    health = getattr(raw_health, "value", raw_health)
    health_str = str(health) if health is not None else "unknown"
    badge = _string_or_none(_field(entry, "badge"))
    return {
        "instance_id": instance_id,
        "eligible": eligible,
        "reasons": reasons,
        "remaining_fraction": remaining_fraction,
        "health": health_str,
        "badge": badge,
    }


def _worker_facts(row: object, proof_status: object) -> WorkerFacts:
    route_id = _string_or_none(_field(row, "route_id"))
    if route_id is None:
        raise ValueError("each worker eligibility row requires a non-empty route_id")
    reasons_raw = _field(row, "reasons", ())
    reasons_iterable: Iterable[object] = (
        reasons_raw if isinstance(reasons_raw, (list, tuple)) else ()
    )
    raw_instances = _field(row, "instance_headrooms")
    if isinstance(raw_instances, Iterable) and not isinstance(
        raw_instances, (str, bytes, Mapping)
    ):
        instance_headrooms = tuple(_instance_dict(entry) for entry in raw_instances)
    else:
        instance_headrooms = ()
    return WorkerFacts(
        route_id=route_id,
        channel=_string_or_none(_field(row, "channel")),
        headroom=_float_or_none(_field(row, "availability_headroom")),
        badge=_string_or_none(_field(row, "availability_badge")),
        eligible=bool(_field(row, "eligible", False)),
        reasons=tuple(
            reason for reason in reasons_iterable if isinstance(reason, str) and reason
        ),
        proof_status=_proof_value(proof_status),
        instance_headrooms=instance_headrooms,
    )


@dataclass(frozen=True)
class ReasonFacts:
    """The selected route's evidence join, verbatim from the caller.

    ``level``, ``n``, and ``k`` come from the accepted evidence join, together
    with its benchmark-prior count (``prior_n``, the joined ``benchmark_run``
    rows) and production-posterior count (``posterior_n``, the joined
    ``router_run`` rows); both are carried and rendered in text and JSON.  The
    estimate is carried only when ``level``/``n``/``k`` are present and the
    level is comparable (never ``none``) — the disclosure never shows an
    estimate without ``k``/``n`` and the level.
    """

    level: str
    n: int
    k: int
    prior_n: int
    posterior_n: int
    estimate: float | None
    low_evidence: bool

    def as_dict(self) -> dict[str, Any]:
        """The same facts, with the estimate omitted at the ``none`` level."""
        payload: dict[str, Any] = {
            "level": self.level,
            "n": self.n,
            "k": self.k,
            "prior_n": self.prior_n,
            "posterior_n": self.posterior_n,
        }
        if self.estimate is not None:
            payload["estimate"] = self.estimate
        payload["low_evidence"] = self.low_evidence
        return payload

    def as_text(self) -> str:
        """The ``accepted k/n comparable (level)`` reason line."""
        accepted = f"accepted {self.k}/{self.n} comparable ({self.level})"
        accepted += (
            f", prior {self.prior_n} benchmark, "
            f"posterior {self.posterior_n} production"
        )
        if self.low_evidence:
            accepted += ", low evidence"
        if self.estimate is not None:
            accepted += f", estimate {self.estimate:g}"
        return accepted


def _reason_facts(evidence: object) -> ReasonFacts | None:
    level = _string_or_none(_field(evidence, "level"))
    if level is None:
        return None
    n = _field(evidence, "n")
    k = _field(evidence, "k")
    if isinstance(n, bool) or isinstance(k, bool):
        return None
    if not isinstance(n, int) or not isinstance(k, int) or n < 0 or k < 0 or k > n:
        return None

    def _count(name: str) -> int:
        value = _field(evidence, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0
        return value

    # The estimate is disclosed only with k/n and a comparable level; the
    # bare Laplace prior at the "none" level is never a disclosed estimate.
    if level == "none":
        estimate: float | None = None
    else:
        raw_estimate = _field(evidence, "estimate")
        estimate = _float_or_none(raw_estimate)
    low_evidence = bool(_field(evidence, "low_evidence", n < 5))
    return ReasonFacts(
        level=level,
        n=n,
        k=k,
        prior_n=_count("prior_n"),
        posterior_n=_count("posterior_n"),
        estimate=estimate,
        low_evidence=low_evidence,
    )


@dataclass(frozen=True)
class StaffingBlock:
    """The pure representation of one computed ``auto`` staffing block."""

    role: str
    class_key: str
    supervisor: WorkerFacts | None
    workers: tuple[WorkerFacts, ...]
    selected_route: str | None
    selected_instance: str | None
    reason: ReasonFacts | None
    expected_cost_usd: float | None
    expected_cost_available: bool
    expected_cost_unavailable_reasons: tuple[str, ...]
    argmin_route: str | None
    escalation: tuple[str, ...]
    never_automatic: tuple[str, ...]
    unproven_routes: tuple[str, ...]
    review_route: str | None
    review_eligible: bool | None
    review_reason: str | None
    review_independence_evaluated: bool
    review_independence_reference: str | None
    human_escalation_cost_usd: float | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        """The full structured block; the exact JSON payload of the text."""
        supervisor = self.supervisor.as_dict() if self.supervisor is not None else None
        if self.expected_cost_available and self.expected_cost_usd is not None:
            expected: dict[str, Any] = {
                "status": "available",
                "usd": self.expected_cost_usd,
                "argmin_route": self.argmin_route,
            }
        else:
            expected = {
                "status": "unavailable",
                "usd": None,
                "argmin_route": self.argmin_route,
                "reasons": list(self.expected_cost_unavailable_reasons),
            }
        reference = (
            self.review_independence_reference or self.selected_route
            if self.review_independence_evaluated
            else None
        )
        review: dict[str, Any]
        if self.review_route is None:
            review = {
                "route_id": None,
                "eligible": None,
                "reason": None,
                "independence_evaluated": self.review_independence_evaluated,
                "independence_reference": reference,
            }
        else:
            review = {
                "route_id": self.review_route,
                "eligible": self.review_eligible,
                "reason": self.review_reason,
                "independence_evaluated": self.review_independence_evaluated,
                "independence_reference": reference,
            }
        return {
            "mode": MODE_AUTO,
            "authority": AUTHORITY_POLICY,
            "role": self.role,
            "class_key": self.class_key,
            "supervisor": supervisor,
            "workers": [worker.as_dict() for worker in self.workers],
            "selected_route": self.selected_route,
            "selected_instance": self.selected_instance,
            "reason": self.reason.as_dict() if self.reason is not None else None,
            "expected_cost": expected,
            "escalation": list(self.escalation),
            "never_automatic": list(self.never_automatic),
            "unproven_routes": list(self.unproven_routes),
            "review": review,
        }

    # -- text sections ------------------------------------------------------

    def _header_lines(self) -> list[str]:
        return [f"{_HEADER_PREFIX} {self.role} {self.class_key} (authority: policy)"]

    def _supervisor_lines(self) -> list[str]:
        if self.supervisor is None:
            return [f"Supervisor: {_NO_SUPERVISOR}"]
        return [f"Supervisor: {self.supervisor.describe()}"]

    def _worker_lines(self) -> list[str]:
        if not self.workers:
            return ["Workers: unavailable (no eligibility rows supplied)"]
        lines = ["Workers:"]
        lines.extend(f"- {worker.describe()}" for worker in self.workers)
        return lines

    def _reason_lines(self) -> list[str]:
        instance_suffix = (
            f", instance {self.selected_instance}"
            if self.selected_instance is not None
            else ""
        )
        if self.selected_route is None:
            return ["Reason: unavailable (no eligible route)"]
        if self.reason is None:
            return [
                "Reason: unavailable (no evidence join for "
                f"{self.selected_route}){instance_suffix}"
            ]
        return [f"Reason: {self.reason.as_text()}{instance_suffix}"]

    def _expected_cost_lines(self) -> list[str]:
        if self.expected_cost_available and self.expected_cost_usd is not None:
            argmin = self.argmin_route or _UNKNOWN
            if self.selected_route is not None and self.selected_route != argmin:
                # Proof-first overrode the ladder argmin: the figure and the
                # suffix belong to the selected route; the rejected argmin is
                # disclosed, never silently reused.
                return [
                    f"Expected cost: {_fmt_usd(self.expected_cost_usd)} "
                    f"(selected {self.selected_route}; ladder argmin {argmin})"
                ]
            return [
                f"Expected cost: {_fmt_usd(self.expected_cost_usd)} "
                f"(argmin {argmin})"
            ]
        reasons = "; ".join(self.expected_cost_unavailable_reasons) or "unknown reason"
        return [f"Expected cost: unavailable ({reasons})"]

    def _escalation_lines(self) -> list[str]:
        chain = list(self.escalation)
        if self.human_escalation_cost_usd is not None:
            terminal = (
                f"human ({_fmt_usd(self.human_escalation_cost_usd)} "
                "policy terminal, beyond the never-automatic boundary)"
            )
        else:
            terminal = "human (policy terminal, beyond the never-automatic boundary)"
        if not chain:
            return [f"Escalation: none ({terminal})"]
        return [f"Escalation: {_ARROW.join(chain)} -> {terminal}"]

    def _never_automatic_lines(self) -> list[str]:
        named = ", ".join(self.never_automatic) if self.never_automatic else "none"
        return [
            "Never-automatic (the boundary automatic escalation never crosses; "
            f"bind only with --authorized-by lee): {named}"
        ]

    def _unproven_lines(self) -> list[str]:
        if not self.unproven_routes:
            return []
        named = ", ".join(self.unproven_routes)
        return [
            "Unproven (auto never selects an unproven route while a proven "
            f"eligible one exists): {named}"
        ]

    def _review_lines(self) -> list[str]:
        if self.review_route is None:
            return ["Review: unavailable (no independent review route supplied)"]
        state = (
            "eligible"
            if self.review_eligible
            else f"excluded ({self.review_reason or 'no reason supplied'})"
        )
        if self.review_independence_evaluated:
            reference = self.review_independence_reference or self.selected_route
            if self.review_eligible and reference is not None:
                if self.selected_route is not None and self.selected_route != reference:
                    independence = (
                        f"independent of author {reference}; "
                        f"selected {self.selected_route} also excluded"
                    )
                else:
                    independence = f"independent of {reference}"
            elif self.review_reason and "independence" in self.review_reason:
                independence = "not independent"
            else:
                independence = "independence evaluated"
        else:
            independence = "independence not evaluated (no --author-route)"
        return [f"Review: {self.review_route} ({state}, {independence})"]


def render_text(block: StaffingBlock) -> str:
    """Render the deterministic D203-format compact text block.

    The sections are fixed: header, optional Supervisor, Workers, Reason,
    Expected cost, Escalation, Never-automatic boundary, the unproven
    disclosure when any eligible worker is unproven, and the Review route.
    Every absent fact is stated explicitly; no blank line or trailing
    whitespace is ever emitted.
    """
    sections: list[str] = []
    sections.extend(block._header_lines())
    sections.extend(block._supervisor_lines())
    sections.extend(block._worker_lines())
    sections.extend(block._reason_lines())
    sections.extend(block._expected_cost_lines())
    sections.extend(block._escalation_lines())
    sections.extend(block._never_automatic_lines())
    sections.extend(block._unproven_lines())
    sections.extend(block._review_lines())
    return "\n".join(sections)


def render_json(block: StaffingBlock) -> dict[str, Any]:
    """Render the structured form carrying exactly the text's facts."""
    payload = block.as_dict()
    return {key: payload[key] for key in STAFFING_BLOCK_JSON_KEYS}


def build_auto_block(
    *,
    role: str,
    class_key: str,
    eligibility_rows: Iterable[object],
    evidence_by_route: Mapping[str, object] | None = None,
    proof_status_by_route: Mapping[str, object] | None = None,
    ladder_result: Mapping[str, Any] | None = None,
    selected_route: str | None = None,
    supervisor_route: str | None = None,
    review_route: str | None = None,
    review_eligible: bool | None = None,
    review_reason: str | None = None,
    review_independence_evaluated: bool = False,
    review_independence_reference: str | None = None,
    human_escalation_cost_usd: float | None = None,
) -> StaffingBlock:
    """Assemble the ``auto`` block from already-computed staffing facts.

    Args:
        role: The stage role, echoed verbatim (never mapped to a class here).
        class_key: The canonical evidence class key, echoed verbatim; it
            never selects a route in this module (D205/D206).
        eligibility_rows: Eligibility rows (or their mappings) in input
            order — every row becomes one Worker line in the same order.
        evidence_by_route: Per-route evidence joins (an ``EvidenceJoin`` or
            its seven-key summary) keyed by exact route id.  Absent entries
            make the reason explicitly unavailable.
        proof_status_by_route: Per-route D211 proof status (a
            :class:`~lee_llm_router.staffing.proof.ProofStatus`, its value
            string, or ``None``); a route missing from the mapping is
            unproven.
        ladder_result: The accepted :func:`calculate_ladder` output — its
            ``argmin_start``, ``escalation``, ``expected_cost_status``, and
            ``unavailable_reasons`` are consumed verbatim; the argmin
            rung's ``E`` is the expected cost.  When ``selected_route``
            names a different route than ``argmin_start`` (proof-first
            policy overrode the pure argmin) while the expected cost is
            available, the expected cost is re-taken from the selected
            rung's own ``E`` and the escalation chain is re-derived as the
            rung suffix starting at the selected route — the rejected
            argmin stays disclosed as the ladder argmin.
        selected_route: Optional explicit selection.  When omitted it is
            the ladder's ``argmin_start`` when the expected cost is
            available, else the first eligible route in input order with
            proven routes before unproven ones, else ``None``.
        supervisor_route: Optional supervisor route id; its facts are taken
            from the matching eligibility row.
        review_route: Optional independent review route id.
        review_eligible: The review route's eligibility; ``None`` (unknown)
            renders as an exclusion without a reason only when a route is
            named.
        review_reason: The review route's exact explain reason when it is
            excluded.
        review_independence_evaluated: Whether author-route independence
            was evaluated for the review choice.
        review_independence_reference: The actual author independence
            reference: the explicit ``--author-route`` route id when one was
            supplied, otherwise the selected worker the reviewer reviews.
            Disclosed verbatim; when omitted it falls back to
            ``selected_route`` so older callers stay truthful.
        human_escalation_cost_usd: The policy terminal cost, used only in
            the escalation line when the ladder could not compute an
            expected cost.

    Returns:
        The assembled :class:`StaffingBlock`; render it with
        :func:`render_text` / :func:`render_json`.

    Raises:
        ValueError: When a worker row has no usable route id, or when a
            route named as supervisor or review is absent from the supplied
            eligibility rows.
    """
    proof_map = proof_status_by_route or {}
    workers = tuple(
        _worker_facts(row, proof_map.get(_string_or_none(_field(row, "route_id"))))
        for row in eligibility_rows
    )
    by_route = {worker.route_id: worker for worker in workers}

    supervisor = None
    if supervisor_route is not None:
        supervisor = by_route.get(supervisor_route)
        if supervisor is None:
            raise ValueError(
                f"supervisor_route {supervisor_route!r} is not among the "
                "supplied eligibility rows"
            )

    eligible_workers = [worker for worker in workers if worker.eligible]

    argmin_route: str | None = None
    escalation: tuple[str, ...] = ()
    expected_cost_usd: float | None = None
    expected_cost_available = False
    unavailable_reasons: tuple[str, ...] = ()
    if ladder_result is not None:
        raw_status = ladder_result.get("expected_cost_status")
        expected_cost_available = raw_status == "available"
        argmin_route = _string_or_none(ladder_result.get("argmin_start"))
        raw_escalation = ladder_result.get("escalation", ())
        escalation = tuple(
            route for route in raw_escalation if isinstance(route, str) and route
        )
        raw_reasons = ladder_result.get("unavailable_reasons", ())
        unavailable_reasons = tuple(
            reason for reason in raw_reasons if isinstance(reason, str) and reason
        )
        if expected_cost_available:
            for rung in ladder_result.get("rungs", ()):
                if not isinstance(rung, Mapping):
                    continue
                if rung.get("route") == argmin_route:
                    expected_cost_usd = _float_or_none(rung.get("E"))
                    break
        if expected_cost_available and expected_cost_usd is None:
            expected_cost_available = False
            unavailable_reasons += ("argmin expected cost unavailable",)

    if selected_route is not None:
        if selected_route not in {worker.route_id for worker in eligible_workers}:
            raise ValueError(
                f"selected_route {selected_route!r} is not an eligible supplied route"
            )
        selection = selected_route
    elif argmin_route is not None and argmin_route in {
        worker.route_id for worker in eligible_workers
    }:
        # calculate_ladder's argmin is also the proof-first, marginal-price
        # fallback when arithmetic is unavailable; consume it in both modes.
        selection = argmin_route
    else:
        proven_first = sorted(
            eligible_workers,
            key=lambda worker: 0 if worker.proof_status == PROVEN else 1,
        )
        selection = proven_first[0].route_id if proven_first else None

    selected_instance: str | None = None
    if selection is not None:
        selected_worker = by_route.get(selection)
        if selected_worker is not None:
            for instance in selected_worker.instance_headrooms:
                if instance.get("eligible") is True:
                    raw_id = instance.get("instance_id")
                    if isinstance(raw_id, str) and raw_id:
                        selected_instance = raw_id
                        break

    reason = (
        _reason_facts((evidence_by_route or {}).get(selection, None))
        if selection is not None
        else None
    )

    if expected_cost_available and selection is not None and selection != argmin_route:
        # Proof-first overrode the pure argmin: reconcile the expected cost
        # and the escalation suffix to the actual selected route.  The
        # rejected argmin remains disclosed as the ladder argmin.
        rung_entries = [
            rung for rung in ladder_result.get("rungs", ()) if isinstance(rung, Mapping)
        ]
        rung_routes = [
            rung.get("route")
            for rung in rung_entries
            if isinstance(rung.get("route"), str) and rung.get("route")
        ]
        selected_cost = None
        if selection in rung_routes:
            start = rung_routes.index(selection)
            escalation = tuple(rung_routes[start:])
            selected_cost = _float_or_none(rung_entries[start].get("E"))
        if selected_cost is None:
            expected_cost_available = False
            expected_cost_usd = None
            unavailable_reasons += ("selected expected cost unavailable",)
        else:
            expected_cost_usd = selected_cost

    never_automatic = tuple(
        worker.route_id
        for worker in workers
        if _NEVER_AUTOMATIC_REASON in worker.reasons
    )
    unproven_routes = tuple(
        worker.route_id
        for worker in eligible_workers
        if worker.proof_status == UNPROVEN
    )

    if review_route is not None:
        review_worker = by_route.get(review_route)
        if review_worker is None:
            raise ValueError(
                f"review_route {review_route!r} is not among the supplied "
                "eligibility rows"
            )
        review_eligible: bool | None = review_worker.eligible
        review_reason = (
            review_reason
            if review_reason is not None
            else ("; ".join(review_worker.reasons) or None)
        )
    else:
        review_eligible = None
        review_reason = None

    return StaffingBlock(
        role=role,
        class_key=class_key,
        supervisor=supervisor,
        workers=workers,
        selected_route=selection,
        selected_instance=selected_instance,
        reason=reason,
        expected_cost_usd=expected_cost_usd,
        expected_cost_available=expected_cost_available,
        expected_cost_unavailable_reasons=unavailable_reasons,
        argmin_route=argmin_route,
        escalation=escalation,
        never_automatic=never_automatic,
        unproven_routes=unproven_routes,
        review_route=review_route,
        review_eligible=review_eligible,
        review_reason=review_reason,
        review_independence_evaluated=review_independence_evaluated,
        review_independence_reference=review_independence_reference,
        human_escalation_cost_usd=(
            _float_or_none(human_escalation_cost_usd)
            if human_escalation_cost_usd is not None
            else None
        ),
    )
