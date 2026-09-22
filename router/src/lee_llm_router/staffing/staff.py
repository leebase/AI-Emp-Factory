"""Phase 2 staffing service (P2-5a): ``auto``, ``crew``, and ``bind`` modes.

This module is the D211 ruling 5 service the ``staff`` command calls.  It
composes the already-accepted Phase 0/Phase 2 pieces — eligibility
(:mod:`lee_llm_router.staffing.eligibility`), the evidence join
(:mod:`lee_llm_router.staffing.evidence`), proof status
(:mod:`lee_llm_router.staffing.proof`), ladder arithmetic
(:mod:`lee_llm_router.staffing.ladder`), and the compact block
(:mod:`lee_llm_router.staffing.block`) — into one deterministic result per
mode, and owns the single write seam:

* ``auto`` — authority ``policy``: evaluate every Phase 0 exclusion at the
  ``--at`` date, join route/class evidence and demand, consume proof
  statuses, compute the eligible ladder and the independent review route,
  then return and render the accepted P2-4 block.  It never selects an
  unproven route while a proven eligible one exists (and says so), never
  maps a class to a route (D205/D206), and its cost-unavailable fallback is
  proof status then marginal price — exactly the ladder's own fallback
  ordering.  Never-automatic routes stay excluded at the boundary.
* ``crew NAME`` — render the exact saved catalog crew fields and authority
  into a saved staffing block.  No route is ever invented: every referenced
  route id must exist in the routes catalog, and the reserved computed
  ``auto`` placeholder refuses (use ``auto`` mode instead).
* ``bind ROUTE --authorized-by --reason`` — the explicit human override.
  Route, reason, and ``authorized_by`` are all required; a never-automatic
  route refuses unless ``authorized_by`` is exactly ``lee``; and only after
  every validation passes is exactly one event appended through
  :func:`lee_llm_router.events.append_event`.  No dispatch ever happens.

Pure planning is separated from that single bind append seam: every mode is
computable against scratch state (injected attempt records, injectable event
path), JSON and text carry the same facts, and both are deterministic for
the same inputs (the bind event timestamp is injectable for tests).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from lee_llm_router.events import BIND_MODE, append_event, build_event
from lee_llm_router.staffing.block import (
    StaffingBlock,
    build_auto_block,
    render_json,
    render_text,
)
from lee_llm_router.staffing.catalog import Crew, StaffingCatalog, canonical_class_key
from lee_llm_router.staffing.eligibility import (
    EligibilityRow,
    StaffingEligibilityError,
    evaluate_eligibility,
)
from lee_llm_router.staffing.evidence import join_evidence
from lee_llm_router.staffing.ladder import LadderInput, calculate_ladder
from lee_llm_router.staffing.ledger import read_attempts
from lee_llm_router.staffing.proof import ProofStatus, proof_status_by_route
from lee_llm_router.staffing.rollup import build_rollup

__all__ = [
    "BIND_AUTHORIZED_BY",
    "MODE_AUTO",
    "MODE_BIND",
    "MODE_CREW",
    "NEVER_AUTOMATIC_REASON",
    "REVIEW_ROLE",
    "STAFF_MODES",
    "StaffResult",
    "StaffServiceError",
    "staff",
    "render_staff_json",
    "render_staff_text",
]

MODE_AUTO = "auto"
MODE_CREW = "crew"
MODE_BIND = "bind"

STAFF_MODES: tuple[str, ...] = (MODE_AUTO, MODE_CREW, MODE_BIND)
"""The exactly-three mode vocabulary of D211 ruling 5."""

NEVER_AUTOMATIC_REASON = "never_automatic"
"""The exact eligibility reason marking the never-automatic boundary."""

_RESERVE_REASON_PREFIX = "reserve:"
"""The exact prefix for the subscription-channel reserve exclusion reason (D216)."""

BIND_AUTHORIZED_BY = "lee"
"""The only ``--authorized-by`` value that binds a never-automatic or reserved route."""

REVIEW_ROLE = "review"
"""The role under which the independent review route is chosen."""


class StaffServiceError(Exception):
    """A staffing-service refusal: nothing was written, nothing launched.

    Attributes:
        kind: A stable machine-readable refusal kind: ``invalid_mode``,
            ``invalid_arguments``, ``invalid_class``, ``invalid_date``,
            ``unknown_crew``, ``reserved_auto_crew``, ``unknown_route``,
            ``never_automatic``, or ``reserve``.
        cause: The underlying exception, when one was caught.
    """

    exit_code = 3

    def __init__(
        self,
        message: str,
        *,
        kind: str = "invalid_arguments",
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.cause = cause


@dataclass(frozen=True)
class StaffResult:
    """The deterministic result of one staffing-service call.

    ``text`` and ``payload`` carry exactly the same facts; ``payload`` is
    the JSON form.  ``block`` is the P2-4 :class:`StaffingBlock` in ``auto``
    mode and ``None`` otherwise.  ``event``/``event_path`` are set only by a
    successful ``bind`` — the single appended ledger event and where it
    went.
    """

    mode: str
    role: str | None
    class_key: str | None
    at_date: str | None
    text: str
    payload: dict[str, Any]
    block: StaffingBlock | None = None
    event: dict[str, Any] | None = field(default=None)
    event_path: str | None = field(default=None)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _string_argument(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise StaffServiceError(f"{name}: must be a non-empty string")
    return value


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _normalize_at_date(at_date: object) -> date:
    """Parse the service's strict calendar-date boundary."""
    if type(at_date) is date:
        return at_date
    if not isinstance(at_date, str) or not _ISO_DATE.fullmatch(at_date):
        raise StaffServiceError(
            f"at_date: not an ISO date (YYYY-MM-DD): {at_date!r}",
            kind="invalid_date",
        )
    try:
        return date.fromisoformat(at_date)
    except ValueError as exc:
        raise StaffServiceError(
            f"at_date: not a real calendar date (YYYY-MM-DD): {at_date!r}",
            kind="invalid_date",
            cause=exc,
        ) from exc


def _marginal_price_sort_key(row: EligibilityRow) -> tuple[int, float, float, str]:
    """Marginal-price order: priced rows ascend, unpriced last, id tie-break.

    Exactly the committed ``catalog explain`` display key, so the auto
    fallback and independent-review picks match the explain report order.
    """
    pricing = row.pricing
    if pricing is None:
        return (1, 0.0, 0.0, row.route_id)
    return (
        0,
        pricing.marginal_input_usd_per_token,
        pricing.marginal_output_usd_per_token,
        row.route_id,
    )


def _attempt_cost(row: EligibilityRow, demand: object) -> float | None:
    """Price one known review attempt from the accepted demand shape."""
    if not isinstance(demand, Mapping):
        return None
    tokens = demand.get("tokens")
    if not isinstance(tokens, Mapping) or row.pricing is None:
        return None
    input_tokens = tokens.get("input_tokens")
    output_tokens = tokens.get("output_tokens")
    if (
        isinstance(input_tokens, bool)
        or isinstance(output_tokens, bool)
        or not isinstance(input_tokens, (int, float))
        or not isinstance(output_tokens, (int, float))
        or not math.isfinite(float(input_tokens))
        or not math.isfinite(float(output_tokens))
        or input_tokens < 0
        or output_tokens < 0
    ):
        return None
    value = (
        float(input_tokens) * row.pricing.marginal_input_usd_per_token
        + float(output_tokens) * row.pricing.marginal_output_usd_per_token
    )
    return value if math.isfinite(value) and value >= 0 else None


def _load_attempt_records(
    attempt_records: Iterable[Mapping[str, Any]] | None,
    attempts_path: str | Path | None,
) -> tuple[Mapping[str, Any], ...]:
    """The evidence/proof input: injected records, or a ledger read.

    A missing ledger file is treated as empty, exactly like
    :func:`lee_llm_router.staffing.proof.proof_status_ledger`.  An explicit
    record iterable wins over a path; giving neither yields no evidence.
    """
    if attempt_records is not None:
        return tuple(attempt_records)
    if attempts_path is not None:
        try:
            return tuple(read_attempts(attempts_path))
        except FileNotFoundError:
            return ()
    return ()


def _class_components(class_key: str) -> tuple[str, str, tuple[str, ...], str, str]:
    """Split a canonical five-segment class key into structured fields.

    Raises:
        StaffServiceError: When the key does not have exactly five
            ``/``-separated segments.
    """
    parts = class_key.split("/")
    if len(parts) != 5 or any(not part for part in parts):
        raise StaffServiceError(
            f"class_key: not a canonical five-segment key: {class_key!r}",
            kind="invalid_class",
        )
    role, oracle_type, tags, size_band, language = parts
    if tags == "none":
        domain_tags: tuple[str, ...] = ()
    else:
        domain_tags = tuple(tags.split("+"))
        if any(not tag for tag in domain_tags):
            raise StaffServiceError(
                f"class_key: empty domain tag in {class_key!r}",
                kind="invalid_class",
            )
    return role, oracle_type, domain_tags, size_band, language


# ---------------------------------------------------------------------------
# auto mode (authority: policy)
# ---------------------------------------------------------------------------


def _select_auto_route(
    eligible_rows: Iterable[EligibilityRow],
    ladder_result: Mapping[str, Any],
    proof: Mapping[str, ProofStatus],
) -> str | None:
    """Select the ladder argmin without crossing the proof boundary.

    When arithmetic is available, the least-E proven rung wins if the global
    argmin is unproven.  Only the unavailable-cost path falls back to marginal
    price, as required by D211 ruling 3.  Stable input order breaks E ties;
    route id breaks marginal-price ties.
    """
    argmin = ladder_result.get("argmin_start")
    if not isinstance(argmin, str) or not argmin:
        return None
    if proof.get(argmin) is ProofStatus.PROVEN:
        return argmin

    proven = [
        row for row in eligible_rows if proof.get(row.route_id) is ProofStatus.PROVEN
    ]
    if not proven:
        return argmin
    if ladder_result.get("expected_cost_status") == "available":
        proven_ids = {row.route_id for row in proven}
        candidates = [
            rung
            for rung in ladder_result.get("rungs", ())
            if isinstance(rung, Mapping)
            and rung.get("route") in proven_ids
            and isinstance(rung.get("E"), (int, float))
            and not isinstance(rung.get("E"), bool)
            and math.isfinite(float(rung["E"]))
        ]
        if candidates:
            return min(candidates, key=lambda rung: float(rung["E"]))["route"]
    return min(proven, key=_marginal_price_sort_key).route_id


def _select_review_route(
    catalog: StaffingCatalog,
    availability: Any,
    *,
    oracle_type: str,
    domain_tags: tuple[str, ...],
    size_band: str,
    language: str,
    at_date: date,
    author_route_id: str | None,
    excluded_route_id: str | None,
    proof: Mapping[str, ProofStatus],
    openrouter_snapshot_path: str | Path | None,
    rate_table_path: str | Path | None,
) -> tuple[str | None, bool, EligibilityRow | None]:
    """Choose an independent review route and return its eligibility row.

    Review is a second, role-scoped eligibility evaluation.  Its class role is
    deliberately not replaced in the caller's class key: the class key is
    only the evidence join key, never a route preference.  The route choice
    uses the same proof-before-price boundary as the auto worker choice, so an
    unproven reviewer cannot displace a proven eligible reviewer.

    Evaluate against the explicit author when supplied, otherwise the selected
    worker. When those are different routes, intersect a second eligibility
    evaluation against the selected worker. This keeps exact-route exclusion,
    family policy, and limited-independence disclosures in one policy engine.
    """
    try:
        rows = evaluate_eligibility(
            catalog,
            role=REVIEW_ROLE,
            oracle_type=oracle_type,
            size_band=size_band,
            language=language,
            domain_tags=domain_tags,
            author_route_id=author_route_id or excluded_route_id,
            availability=availability,
            at_date=at_date,
            openrouter_snapshot_path=openrouter_snapshot_path,
            rate_table_path=rate_table_path,
        )
    except StaffingEligibilityError as exc:
        raise StaffServiceError(str(exc), kind="invalid_class", cause=exc) from exc

    evaluated = author_route_id is not None or excluded_route_id is not None
    if (
        author_route_id is not None
        and excluded_route_id is not None
        and author_route_id != excluded_route_id
    ):
        try:
            worker_rows = evaluate_eligibility(
                catalog,
                role=REVIEW_ROLE,
                oracle_type=oracle_type,
                size_band=size_band,
                language=language,
                domain_tags=domain_tags,
                author_route_id=excluded_route_id,
                availability=availability,
                at_date=at_date,
                openrouter_snapshot_path=openrouter_snapshot_path,
                rate_table_path=rate_table_path,
            )
        except StaffingEligibilityError as exc:
            raise StaffServiceError(str(exc), kind="invalid_class", cause=exc) from exc
        worker_by_route = {row.route_id: row for row in worker_rows}
        effective_rows = tuple(
            replace(
                row,
                reasons=tuple(
                    dict.fromkeys(
                        row.reasons
                        + tuple(
                            reason.replace(
                                "same model family as author",
                                "same model family as selected worker",
                            )
                            if reason.startswith("same-family review:")
                            else reason
                            for reason in worker_by_route[row.route_id].reasons
                        )
                    )
                ),
                eligible=row.eligible and worker_by_route[row.route_id].eligible,
            )
            for row in rows
        )
    else:
        effective_rows = rows

    eligible = [row for row in effective_rows if row.eligible]
    proven = [row for row in eligible if proof.get(row.route_id) is ProofStatus.PROVEN]
    candidates = proven or eligible
    if not candidates:
        return None, evaluated, None
    selected = min(candidates, key=_marginal_price_sort_key)
    return selected.route_id, evaluated, selected


def _staff_auto(
    catalog: StaffingCatalog,
    availability: Any,
    *,
    role: str,
    class_key: str,
    at_date: date,
    author_route_id: str | None,
    supervisor_route_id: str | None,
    reviewer_expected_cost_usd: float | None,
    records: tuple[Mapping[str, Any], ...],
    rollup: Any,
    openrouter_snapshot_path: str | Path | None,
    rate_table_path: str | Path | None,
) -> StaffResult:
    """Compute and render the accepted P2-4 ``auto`` block (authority policy)."""
    _class_role, oracle_type, review_tags, size_band, language = _class_components(
        class_key
    )
    try:
        rows = evaluate_eligibility(
            catalog,
            role=role,
            oracle_type=oracle_type,
            size_band=size_band,
            language=language,
            domain_tags=review_tags,
            class_key=class_key,
            author_route_id=author_route_id,
            availability=availability,
            at_date=at_date,
            openrouter_snapshot_path=openrouter_snapshot_path,
            rate_table_path=rate_table_path,
        )
    except StaffingEligibilityError as exc:
        raise StaffServiceError(str(exc), kind="invalid_class", cause=exc) from exc

    proof = proof_status_by_route(records)
    eligible_rows = tuple(row for row in rows if row.eligible)

    evidence_by_route: dict[str, dict[str, Any]] = {}
    ladder_rungs: list[LadderInput] = []
    for row in eligible_rows:
        join = join_evidence(row.route_id, class_key, records, rollup)
        summary = join.as_dict()
        evidence_by_route[row.route_id] = summary
        ladder_rungs.append(
            LadderInput(
                route=row.route_id,
                demand=join.demand,
                pricing=row.pricing,
                evidence=summary,
                proof_status=proof.get(row.route_id, ProofStatus.UNPROVEN),
            )
        )

    def calculate(reviewer_cost: float | None) -> dict[str, Any]:
        return calculate_ladder(
            ladder_rungs,
            human_escalation_cost_usd=catalog.policy.human_escalation_cost_usd,
            oracle_type=oracle_type,
            reviewer_expected_cost_usd=reviewer_cost,
            attempt_records=records,
            supervisor_route=supervisor_route_id,
            class_key=class_key,
            channel_kind_by_route={
                route.route_id: channel.kind
                for route in catalog.routes.routes
                for channel in catalog.channels.channels
                if route.channel == channel.channel_id
            },
        )

    ladder_result = calculate(reviewer_expected_cost_usd)
    selected = _select_auto_route(eligible_rows, ladder_result, proof)

    def choose_review(worker_route: str | None):
        return _select_review_route(
            catalog,
            availability,
            oracle_type=oracle_type,
            domain_tags=review_tags,
            size_band=size_band,
            language=language,
            at_date=at_date,
            author_route_id=author_route_id or worker_route,
            excluded_route_id=worker_route,
            proof=proof,
            openrouter_snapshot_path=openrouter_snapshot_path,
            rate_table_path=rate_table_path,
        )

    review_route, review_independence_evaluated, review_row = choose_review(selected)

    # Judge verification cost is the chosen independent review attempt.  Its
    # identity can in turn change the worker argmin, so settle the finite
    # worker/reviewer state space.  A cycle fails closed to unavailable cost
    # (the proof/price fallback), rather than publishing mismatched arithmetic.
    if reviewer_expected_cost_usd is None and oracle_type == "judge":
        review_class = canonical_class_key(
            REVIEW_ROLE, oracle_type, review_tags, size_band, language
        )
        seen: set[tuple[str | None, str | None]] = set()
        while (selected, review_route) not in seen:
            seen.add((selected, review_route))
            reviewer_cost = None
            if review_row is not None:
                review_join = join_evidence(
                    review_row.route_id, review_class, records, rollup
                )
                reviewer_cost = _attempt_cost(review_row, review_join.demand)
            next_ladder = calculate(reviewer_cost)
            next_selected = _select_auto_route(eligible_rows, next_ladder, proof)
            next_review = choose_review(next_selected)
            if (next_selected, next_review[0]) == (selected, review_route):
                ladder_result = next_ladder
                selected = next_selected
                review_route, review_independence_evaluated, review_row = next_review
                break
            ladder_result = next_ladder
            selected = next_selected
            review_route, review_independence_evaluated, review_row = next_review
        else:
            ladder_result = calculate(None)
            selected = _select_auto_route(eligible_rows, ladder_result, proof)
            review_route, review_independence_evaluated, review_row = choose_review(
                selected
            )

    block = build_auto_block(
        role=role,
        class_key=class_key,
        eligibility_rows=rows,
        evidence_by_route=evidence_by_route,
        proof_status_by_route=proof,
        ladder_result=ladder_result,
        selected_route=selected,
        supervisor_route=supervisor_route_id,
        human_escalation_cost_usd=catalog.policy.human_escalation_cost_usd,
    )
    block = replace(
        block,
        review_route=review_route,
        review_eligible=(review_row.eligible if review_row is not None else None),
        review_reason=(
            "; ".join(review_row.reasons) or None if review_row is not None else None
        ),
        review_independence_evaluated=review_independence_evaluated,
        review_independence_reference=(
            (author_route_id or selected) if review_independence_evaluated else None
        ),
    )
    rendered_text = render_text(block)
    if review_route is not None and review_row is not None and any(
        "limited independence" in reason for reason in review_row.reasons
    ):
        # The compact block's standard review line says "independent of" its
        # reference. A policy-permitted same-family route has only limited
        # independence, so state the exact route comparisons and disclosure.
        reference = author_route_id or selected
        comparison = f"distinct from {reference}"
        if author_route_id is not None and selected != author_route_id:
            comparison = (
                f"distinct from author {author_route_id}; "
                f"distinct from selected {selected}"
            )
        rendered_text = rendered_text.rsplit("\n", 1)[0] + (
            f"\nReview: {review_route} (eligible, {comparison}; "
            f"{block.review_reason})"
        )
    return StaffResult(
        mode=MODE_AUTO,
        role=role,
        class_key=class_key,
        at_date=at_date.isoformat(),
        text=rendered_text,
        payload=render_json(block),
        block=block,
    )


# ---------------------------------------------------------------------------
# crew mode (saved authority)
# ---------------------------------------------------------------------------

_CREW_JSON_KEYS: tuple[str, ...] = (
    "mode",
    "crew_id",
    "kind",
    "authority",
    "evidence_ref",
    "governed_ref",
    "supervisor_route",
    "worker_routes",
    "reviewer_route",
    "escalation_ladder",
    "source",
    "crew_name",
    "supervisor",
    "escalation",
    "computed",
)


def _resolve_crew(catalog: StaffingCatalog, crew_id: str) -> Crew:
    crew = next(
        (entry for entry in catalog.crews.crews if entry.crew_id == crew_id), None
    )
    if crew is None:
        raise StaffServiceError(
            f"crew {crew_id!r} is not in the crews catalog", kind="unknown_crew"
        )
    if crew.computed or crew.crew_id == MODE_AUTO:
        raise StaffServiceError(
            "crew 'auto' is the reserved computed placeholder; it has no saved "
            "routes — use mode auto to compute it",
            kind="reserved_auto_crew",
        )
    return crew


def _crew_route_ids(crew: Crew) -> tuple[str, ...]:
    """Every route id the saved crew names, in a fixed deterministic order."""
    ids: list[str] = []
    if crew.supervisor_route is not None:
        ids.append(crew.supervisor_route)
    if crew.worker_routes is not None:
        for routes in crew.worker_routes.values():
            ids.extend(routes)
    if crew.reviewer_route is not None:
        ids.append(crew.reviewer_route)
    if crew.escalation_ladder is not None:
        ids.extend(crew.escalation_ladder)
    return tuple(ids)


def _staff_crew(catalog: StaffingCatalog, crew_id: str) -> StaffResult:
    """Render the exact saved crew fields and authority; no route invented."""
    crew = _resolve_crew(catalog, crew_id)
    known_routes = {route.route_id for route in catalog.routes.routes}
    for route_id in _crew_route_ids(crew):
        if route_id not in known_routes:
            raise StaffServiceError(
                f"crew {crew_id!r} names unknown route {route_id!r}; the saved "
                "block is never rendered with an invented or unknown route",
                kind="unknown_route",
            )

    worker_routes = None
    if crew.worker_routes is not None:
        # Preserve role and route preference order from the saved Crew object;
        # neither is sorted or reconstructed by this service.
        worker_routes = {
            role: list(routes) for role, routes in crew.worker_routes.items()
        }
    supervisor = None
    if crew.supervisor is not None:
        supervisor = {
            "kind": crew.supervisor.kind,
            "owner": crew.supervisor.owner,
        }
    payload: dict[str, Any] = {
        "mode": MODE_CREW,
        "crew_id": crew.crew_id,
        "kind": crew.kind,
        "authority": crew.authority,
        "evidence_ref": crew.evidence_ref,
        "governed_ref": crew.governed_ref,
        "supervisor_route": crew.supervisor_route,
        "worker_routes": worker_routes,
        "reviewer_route": crew.reviewer_route,
        "escalation_ladder": (
            list(crew.escalation_ladder) if crew.escalation_ladder is not None else None
        ),
        "source": crew.source,
        "crew_name": crew.crew_name,
        "supervisor": supervisor,
        "escalation": crew.escalation,
        "computed": crew.computed,
    }
    payload = {key: payload[key] for key in _CREW_JSON_KEYS}

    lines = [f"staff crew {crew.crew_id} (authority: {crew.authority})"]
    lines.append(f"Kind: {crew.kind}")
    if crew.governed_ref is not None:
        lines.append(f"Governed ref: {crew.governed_ref}")
    if crew.supervisor_route is not None:
        lines.append(f"Supervisor: {crew.supervisor_route}")
    else:
        lines.append("Supervisor: unavailable (no supervisor route recorded)")
    if worker_routes:
        lines.append("Workers:")
        lines.extend(
            f"- {role}: {', '.join(routes)}" for role, routes in worker_routes.items()
        )
    else:
        lines.append("Workers: unavailable (no role routes recorded)")
    if crew.reviewer_route is not None:
        lines.append(f"Reviewer: {crew.reviewer_route}")
    else:
        lines.append("Reviewer: unavailable (no reviewer route recorded)")
    if crew.escalation_ladder is not None:
        lines.append(f"Escalation: {' -> '.join(crew.escalation_ladder)}")
    else:
        recorded = crew.escalation or "not recorded"
        lines.append(f"Escalation: {recorded}")
    if crew.source is not None:
        lines.append(f"Source: {crew.source}")
    if crew.crew_name is not None:
        lines.append(f"Saved crew name: {crew.crew_name}")
    if crew.supervisor is not None:
        lines.append(
            f"Supervisor owner: {crew.supervisor.owner} ({crew.supervisor.kind})"
        )
    if crew.escalation is not None:
        lines.append(f"Saved escalation: {crew.escalation}")
    if crew.computed is not None:
        lines.append(f"Computed: {'yes' if crew.computed else 'no'}")
    lines.append(f"Evidence ref: {crew.evidence_ref}")

    return StaffResult(
        mode=MODE_CREW,
        role=None,
        class_key=None,
        at_date=None,
        text="\n".join(lines),
        payload=payload,
    )


# ---------------------------------------------------------------------------
# bind mode (explicit, ledgered through events.py)
# ---------------------------------------------------------------------------


def _staff_bind(
    catalog: StaffingCatalog,
    availability: Any,
    *,
    role: str,
    class_key: str,
    at_date: date,
    bind_route: str,
    authorized_by: str,
    reason: str,
    events_path: str | Path | None,
    snapshot_stale: bool | None,
    now: str | datetime | None,
    openrouter_snapshot_path: str | Path | None,
    rate_table_path: str | Path | None,
) -> StaffResult:
    """Validate fully, then append exactly one bind event.  No dispatch."""
    route = next(
        (entry for entry in catalog.routes.routes if entry.route_id == bind_route),
        None,
    )
    if route is None:
        raise StaffServiceError(
            f"bind route {bind_route!r} is not in the routes catalog",
            kind="unknown_route",
        )
    _class_role, oracle_type, domain_tags, size_band, language = _class_components(
        class_key
    )
    try:
        rows = evaluate_eligibility(
            catalog,
            role=role,
            oracle_type=oracle_type,
            size_band=size_band,
            language=language,
            domain_tags=domain_tags,
            class_key=class_key,
            availability=availability,
            at_date=at_date,
            openrouter_snapshot_path=openrouter_snapshot_path,
            rate_table_path=rate_table_path,
        )
    except StaffingEligibilityError as exc:
        raise StaffServiceError(str(exc), kind="invalid_class", cause=exc) from exc
    row = next((entry for entry in rows if entry.route_id == bind_route), None)
    if row is None:  # pragma: no cover - the route list is the row list
        raise StaffServiceError(
            f"bind route {bind_route!r} has no eligibility row",
            kind="unknown_route",
        )

    never_automatic = NEVER_AUTOMATIC_REASON in row.reasons
    if never_automatic and authorized_by != BIND_AUTHORIZED_BY:
        raise StaffServiceError(
            f"route {bind_route!r} is never-automatic; it binds only with "
            f"--authorized-by {BIND_AUTHORIZED_BY!r}, got {authorized_by!r}",
            kind="never_automatic",
        )
    reserved = any(r.startswith(_RESERVE_REASON_PREFIX) for r in row.reasons)
    if reserved and authorized_by != BIND_AUTHORIZED_BY:
        raise StaffServiceError(
            f"route {bind_route!r} is on a reserved channel; it binds only with "
            f"--authorized-by {BIND_AUTHORIZED_BY!r}, got {authorized_by!r}",
            kind="reserve",
        )

    observed_at = getattr(availability, "observed_at", None)
    if observed_at is not None and not hasattr(observed_at, "isoformat"):
        raise StaffServiceError(
            "availability.observed_at must be a date-time or None",
            kind="invalid_arguments",
        )
    stale = getattr(availability, "stale", None)
    event_stale = stale if snapshot_stale is None else snapshot_stale
    if not isinstance(event_stale, bool):
        raise StaffServiceError(
            "availability.stale must be a boolean",
            kind="invalid_arguments",
        )
    event = build_event(
        **{
            # bind has no saved crew: the stable service identity is used for
            # the required events.py provenance fields, while the route id is
            # the explicit worker identity.
            "crew": "staff",
            "role": role,
            "mode": BIND_MODE,
            "worker_id": bind_route,
            "provider": route.harness,
            "model": route.model,
            "effort": route.effort,
            "channel": route.channel,
            "headroom": row.availability_health,
            "reason": reason,
            "authorized_by": authorized_by,
            "route_id": bind_route,
            "snapshot_observed_at": (
                observed_at.isoformat() if observed_at is not None else None
            ),
            "snapshot_stale": event_stale,
            **({"ts": now} if now is not None else {}),
        }
    )
    # The single write seam: reached only after every validation above.
    path = append_event(event, path=events_path)

    lines = [f"staff bind {bind_route} (authorized_by: {authorized_by})"]
    lines.append(f"Role/class: {role} {class_key}")
    if never_automatic:
        lines.append(
            f"Never-automatic: yes (bound by {BIND_AUTHORIZED_BY}, "
            "the only authorized exception)"
        )
    if reserved:
        lines.append(
            f"Reserve: yes (bound by {BIND_AUTHORIZED_BY}, "
            "the only authorized exception)"
        )
    lines.append(f"Reason: {reason}")
    lines.append(f"Event: appended to {path}")
    payload = {
        "mode": MODE_BIND,
        "role": role,
        "class_key": class_key,
        "route_id": bind_route,
        "reason": reason,
        "authorized_by": authorized_by,
        "never_automatic": never_automatic,
        "reserved": reserved,
        "event": event,
        "event_path": str(path),
    }
    return StaffResult(
        mode=MODE_BIND,
        role=role,
        class_key=class_key,
        at_date=at_date.isoformat(),
        text="\n".join(lines),
        payload=payload,
        event=event,
        event_path=str(path),
    )


# ---------------------------------------------------------------------------
# Service entry point
# ---------------------------------------------------------------------------


def _reject_supplied(name: str, value: object, mode: str) -> None:
    """Reject an argument which has no meaning in ``mode``."""
    if value is not None:
        raise StaffServiceError(
            f"{name} is only valid in its mode, not {mode!r}",
            kind="invalid_arguments",
        )


def _validate_money(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StaffServiceError(
            "reviewer_expected_cost_usd: must be a finite nonnegative number",
            kind="invalid_arguments",
        )
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise StaffServiceError(
            "reviewer_expected_cost_usd: must be a finite nonnegative number",
            kind="invalid_arguments",
        )
    return number


def _validate_known_route(
    catalog: StaffingCatalog, route_id: str, argument: str
) -> None:
    if not any(route.route_id == route_id for route in catalog.routes.routes):
        raise StaffServiceError(
            f"{argument} {route_id!r} does not match a route in the routes catalog",
            kind="unknown_route",
        )


def staff(
    catalog: StaffingCatalog,
    availability: Any,
    *,
    mode: str = MODE_AUTO,
    role: str | None = None,
    class_key: str | None = None,
    at_date: date | str | None = None,
    crew_id: str | None = None,
    bind_route: str | None = None,
    authorized_by: str | None = None,
    reason: str | None = None,
    author_route_id: str | None = None,
    supervisor_route_id: str | None = None,
    reviewer_expected_cost_usd: float | None = None,
    attempt_records: Iterable[Mapping[str, Any]] | None = None,
    attempts_path: str | Path | None = None,
    rollup: Any = None,
    events_path: str | Path | None = None,
    snapshot_stale: bool | None = None,
    now: str | datetime | None = None,
    openrouter_snapshot_path: str | Path | None = None,
    rate_table_path: str | Path | None = None,
) -> StaffResult:
    """Compute one deterministic staffing result.

    ``auto`` and ``bind`` require a role, canonical class key, and strict
    calendar date.  ``crew`` requires only the exact saved crew id.  All route
    selection is pure; only a successful ``bind`` calls ``append_event``.
    """
    if not isinstance(mode, str) or mode not in STAFF_MODES:
        raise StaffServiceError(
            f"mode: must be one of {', '.join(STAFF_MODES)}, got {mode!r}",
            kind="invalid_mode",
        )

    # Validate optional scalar spellings before any catalog work.  Returning
    # the original string is intentional: route and crew ids are exact refs.
    role_value = _string_argument(role, "role")
    class_key_value = _string_argument(class_key, "class_key")
    author_value = _string_argument(author_route_id, "author_route_id")
    supervisor_value = _string_argument(supervisor_route_id, "supervisor_route_id")
    cost_value = _validate_money(reviewer_expected_cost_usd)

    when: date | None = None
    if at_date is not None:
        when = _normalize_at_date(at_date)

    if mode == MODE_AUTO:
        if role_value is None or class_key_value is None or when is None:
            raise StaffServiceError(
                "mode 'auto' requires role, class_key, and at_date",
                kind="invalid_arguments",
            )
        _class_components(class_key_value)
        _reject_supplied("crew_id", crew_id, mode)
        _reject_supplied("bind_route", bind_route, mode)
        _reject_supplied("authorized_by", authorized_by, mode)
        _reject_supplied("reason", reason, mode)
        _reject_supplied("events_path", events_path, mode)
        _reject_supplied("snapshot_stale", snapshot_stale, mode)
        _reject_supplied("now", now, mode)
        if supervisor_value is not None:
            _validate_known_route(catalog, supervisor_value, "supervisor_route_id")
        records = _load_attempt_records(attempt_records, attempts_path)
        if rollup is None:
            try:
                rollup = build_rollup(records)
            except (TypeError, ValueError) as exc:
                raise StaffServiceError(
                    f"attempt evidence cannot be rolled up: {exc}",
                    kind="invalid_arguments",
                    cause=exc,
                ) from exc
        return _staff_auto(
            catalog,
            availability,
            role=role_value,
            class_key=class_key_value,
            at_date=when,
            author_route_id=author_value,
            supervisor_route_id=supervisor_value,
            reviewer_expected_cost_usd=cost_value,
            records=records,
            rollup=rollup,
            openrouter_snapshot_path=openrouter_snapshot_path,
            rate_table_path=rate_table_path,
        )

    if mode == MODE_CREW:
        crew_value = _string_argument(crew_id, "crew_id")
        if crew_value is None:
            raise StaffServiceError(
                "mode 'crew' requires crew_id", kind="invalid_arguments"
            )
        _reject_supplied("bind_route", bind_route, mode)
        _reject_supplied("authorized_by", authorized_by, mode)
        _reject_supplied("reason", reason, mode)
        _reject_supplied("events_path", events_path, mode)
        _reject_supplied("snapshot_stale", snapshot_stale, mode)
        _reject_supplied("now", now, mode)
        _reject_supplied("reviewer_expected_cost_usd", cost_value, mode)
        _reject_supplied("author_route_id", author_value, mode)
        _reject_supplied("supervisor_route_id", supervisor_value, mode)
        _reject_supplied("attempt_records", attempt_records, mode)
        _reject_supplied("attempts_path", attempts_path, mode)
        _reject_supplied("rollup", rollup, mode)
        _reject_supplied("openrouter_snapshot_path", openrouter_snapshot_path, mode)
        _reject_supplied("rate_table_path", rate_table_path, mode)
        return _staff_crew(catalog, crew_value)

    # bind: validate the complete request before constructing or appending an
    # event.  In particular, an ineligible ordinary route may be explicitly
    # bound, but never-automatic still has its separate Lee-only boundary.
    if role_value is None or class_key_value is None or when is None:
        raise StaffServiceError(
            "mode 'bind' requires role, class_key, and at_date",
            kind="invalid_arguments",
        )
    _class_components(class_key_value)
    _reject_supplied("crew_id", crew_id, mode)
    _reject_supplied("author_route_id", author_value, mode)
    _reject_supplied("supervisor_route_id", supervisor_value, mode)
    _reject_supplied("reviewer_expected_cost_usd", cost_value, mode)
    _reject_supplied("attempt_records", attempt_records, mode)
    _reject_supplied("attempts_path", attempts_path, mode)
    _reject_supplied("rollup", rollup, mode)
    if snapshot_stale is not None and not isinstance(snapshot_stale, bool):
        raise StaffServiceError(
            "snapshot_stale: must be a boolean", kind="invalid_arguments"
        )
    if now is not None and (
        not isinstance(now, (str, datetime))
        or (isinstance(now, str) and not now.strip())
    ):
        raise StaffServiceError(
            "now: must be a non-empty timestamp", kind="invalid_arguments"
        )
    bind_value = _string_argument(bind_route, "bind_route")
    authorized_by_value = _string_argument(authorized_by, "authorized_by")
    reason_value = _string_argument(reason, "reason")
    if bind_value is None or authorized_by_value is None or reason_value is None:
        raise StaffServiceError(
            "mode 'bind' requires bind_route, authorized_by, and reason",
            kind="invalid_arguments",
        )
    return _staff_bind(
        catalog,
        availability,
        role=role_value,
        class_key=class_key_value,
        at_date=when,
        bind_route=bind_value,
        authorized_by=authorized_by_value,
        reason=reason_value,
        events_path=events_path,
        snapshot_stale=snapshot_stale,
        now=now,
        openrouter_snapshot_path=openrouter_snapshot_path,
        rate_table_path=rate_table_path,
    )


def render_staff_json(result: StaffResult) -> dict[str, Any]:
    """Return the stable JSON payload of a service result."""
    return dict(result.payload)


def render_staff_text(result: StaffResult) -> str:
    """Return the stable text rendering of a service result."""
    return result.text
