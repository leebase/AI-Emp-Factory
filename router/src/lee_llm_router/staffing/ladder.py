"""Pure expected-cost arithmetic for a D211 staffing ladder.

The caller supplies eligible routes in escalation order.  This module does not
look routes up from a class key and performs no I/O.  Unknown evidence, token
demand, pricing, judge-review cost, or supervisor overhead is contagious: an
expected cost that depends on it is explicitly unavailable rather than being
calculated with an invented zero.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from lee_llm_router.staffing.proof import ProofStatus
from lee_llm_router.staffing.rollup import MINIMUM_SAMPLE_SIZE

__all__ = [
    "EXPECTED_COST_AVAILABLE",
    "EXPECTED_COST_UNAVAILABLE",
    "LadderInput",
    "RungInput",
    "build_ladder",
    "calculate_ladder",
    "compute_ladder",
    "ladder",
    "supervisor_overhead_from_rows",
]

EXPECTED_COST_AVAILABLE = "available"
EXPECTED_COST_UNAVAILABLE = "unavailable"
_RUNG_KEYS = ("route", "a", "v", "s", "q", "q_prime", "E")


@dataclass(frozen=True)
class LadderInput:
    """Inputs for one already-eligible route.

    ``demand`` is normally :attr:`EvidenceJoin.demand`; only its input and
    output medians are priced.  ``pricing`` may be an ``EligibilityPrice`` or
    the corresponding explain mapping.  ``evidence`` may be an
    ``EvidenceJoin`` or its seven-key summary.
    """

    route: str
    demand: object
    pricing: object
    evidence: object
    proof_status: ProofStatus | str = ProofStatus.UNPROVEN


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _finite_nonnegative(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _route_id(record: object) -> str | None:
    if not isinstance(record, Mapping):
        return None
    event = record.get("router_event")
    if isinstance(event, Mapping):
        value = event.get("route_id")
        if isinstance(value, str) and value:
            return value
    route = record.get("route")
    if isinstance(route, Mapping):
        value = route.get("route_id")
        if isinstance(value, str) and value:
            return value
    value = record.get("route_id")
    return value if isinstance(value, str) and value else None


def _class_key(record: object) -> str | None:
    if not isinstance(record, Mapping):
        return None
    class_record = record.get("class_record")
    if isinstance(class_record, Mapping):
        value = class_record.get("class_key")
        if isinstance(value, str) and value:
            return value
    value = record.get("class_key")
    return value if isinstance(value, str) and value else None


def _route_ref_id(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, Mapping):
        route_id = value.get("route_id")
        if isinstance(route_id, str) and route_id:
            return route_id
    return None


def _tokens(demand: object) -> tuple[float, float] | None:
    if demand == "unknown" or demand is None:
        return None
    tokens = _field(demand, "tokens", demand)
    input_tokens = _finite_nonnegative(_field(tokens, "input_tokens"))
    output_tokens = _finite_nonnegative(_field(tokens, "output_tokens"))
    if input_tokens is None or output_tokens is None:
        return None
    # total_tokens, cached_input_tokens, and reasoning_tokens are deliberately
    # not consulted: D211 prices known demand at the two marginal rates only.
    return input_tokens, output_tokens


def _attempt_cost(demand: object, pricing: object) -> float | None:
    token_pair = _tokens(demand)
    if token_pair is None or pricing is None:
        return None
    input_rate = _finite_nonnegative(_field(pricing, "marginal_input_usd_per_token"))
    output_rate = _finite_nonnegative(_field(pricing, "marginal_output_usd_per_token"))
    if input_rate is None or output_rate is None:
        return None
    input_tokens, output_tokens = token_pair
    return input_tokens * input_rate + output_tokens * output_rate


def _laplace(evidence: object) -> float | None:
    if evidence is None or _field(evidence, "level") == "none":
        return None
    n = _field(evidence, "n")
    k = _field(evidence, "k")
    if (
        isinstance(n, bool)
        or isinstance(k, bool)
        or not isinstance(n, int)
        or not isinstance(k, int)
        or n < 0
        or k < 0
        or k > n
    ):
        return None
    # Recalculate rather than trust a caller-provided estimate.
    return (k + 1) / (n + 2)


def _class_matches(record_key: str | None, target: str | None, level: object) -> bool:
    if target is None:
        return True
    if record_key is None:
        return False
    widths = {"drop_language": 4, "drop_size_band": 3, "role_oracle_type": 2}
    if level == "exact":
        return record_key == target
    width = widths.get(level)
    if width is None:
        return False
    return record_key.split("/")[:width] == target.split("/")[:width]


def _repair_probability(
    route: str,
    class_key: str | None,
    evidence_level: object,
    records: tuple[object, ...],
    default: float | None,
) -> float | None:
    repairs = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        parent = record.get("parent_attempt_id")
        if parent is None or parent == "":
            continue
        if _route_id(record) != route:
            continue
        if not _class_matches(_class_key(record), class_key, evidence_level):
            continue
        repairs.append(record)
    if len(repairs) < MINIMUM_SAMPLE_SIZE:
        return default
    successes = sum(row.get("verified_success") is True for row in repairs)
    return (successes + 1) / (len(repairs) + 2)


def supervisor_overhead_from_rows(
    rows: Iterable[object], *, supervisor_route: str | None = None
) -> tuple[float | None, int]:
    """Return the supervisor overhead median from attributable observations.

    Deriving ``s`` requires *attributable* supervision-cost observations: an
    observation that prices the supervision itself.  ``supervisor_route`` on
    an attempt row only attests which route supervised that attempt's work;
    the row's ``cost.usd_marginal`` is the cost of the supervised (worker)
    attempt, not the cost of the supervision it received.  Attempt-record v2
    has no supervision-cost field and no supervision record kind, so no row
    can carry an attributable observation, and the supervisor route's own
    attempt costs are never repurposed as ``s``.

    The function therefore fails closed: it counts the eligible rows that
    attest the supervisor's identity (for the unavailable disclosure) and
    always returns ``None`` — omission means unavailable, never zero and
    never a borrowed worker cost.  The ``>=5`` gate applies to attributable
    supervision-cost observations; attempt-v2 can never supply one.
    """

    materialized = tuple(rows)
    attested = sum(
        1
        for row in materialized
        if isinstance(row, Mapping) and row.get("eligible") is not False
        if (route_id := _route_ref_id(row.get("supervisor_route"))) is not None
        if supervisor_route is None or route_id == supervisor_route
    )
    return None, attested


def _normalise_rung(value: LadderInput | Mapping[str, Any]) -> LadderInput:
    if isinstance(value, LadderInput):
        rung = value
    elif isinstance(value, Mapping):
        route = value.get("route", value.get("route_id"))
        rung = LadderInput(
            route=route,  # type: ignore[arg-type]
            demand=value.get("demand"),
            pricing=value.get("pricing"),
            evidence=value.get("evidence"),
            proof_status=value.get("proof_status", ProofStatus.UNPROVEN),
        )
    else:
        raise TypeError("each ladder rung must be LadderInput or a mapping")
    if not isinstance(rung.route, str) or not rung.route:
        raise ValueError("each ladder rung requires a non-empty route id")
    return rung


def _proof_rank(value: ProofStatus | str) -> int:
    raw = value.value if isinstance(value, ProofStatus) else value
    return 0 if raw == ProofStatus.PROVEN.value else 1


def _marginal_rank(rung: LadderInput) -> tuple[float, ...]:
    input_rate = _finite_nonnegative(
        _field(rung.pricing, "marginal_input_usd_per_token")
    )
    output_rate = _finite_nonnegative(
        _field(rung.pricing, "marginal_output_usd_per_token")
    )
    if input_rate is None or output_rate is None:
        return (1.0, math.inf, math.inf)
    return (0.0, input_rate, output_rate)


def _channel_tier(route: str, channel_kind_by_route: Mapping[str, str] | None) -> int:
    """Return the D215 prepaid-first tier for an eligible route."""
    if channel_kind_by_route is None:
        return 0
    kind = channel_kind_by_route.get(route)
    if kind == "subscription":
        return 0
    if kind == "metered":
        return 1
    return 2


def calculate_ladder(
    rungs: Iterable[LadderInput | Mapping[str, Any]],
    *,
    human_escalation_cost_usd: float,
    oracle_type: str,
    reviewer_expected_cost_usd: float | None = None,
    attempt_records: Iterable[object] = (),
    supervisor_route: str | None = None,
    class_key: str | None = None,
    channel_kind_by_route: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Calculate D211 expected costs and select an argmin escalation suffix.

    The returned ``rungs`` entries contain exactly ``route, a, v, s, q,
    q_prime, E``.  If every dependency is known, ``argmin_start`` is the
    minimum-E route (stable on ties).  If any expected cost is unavailable,
    all eligible routes are instead stably ordered by proven-before-unproven,
    then current marginal price, and the status explicitly says why.  When
    channel kinds are supplied, the D215 subscription, metered, and other
    tiers precede those existing ordering keys.
    """

    terminal = _finite_nonnegative(human_escalation_cost_usd)
    if terminal is None:
        raise ValueError("human_escalation_cost_usd must be finite and nonnegative")
    items = tuple(_normalise_rung(rung) for rung in rungs)
    if not items:
        return {
            "rungs": [],
            "argmin_start": None,
            "escalation": [],
            "expected_cost_status": EXPECTED_COST_UNAVAILABLE,
            "unavailable_reasons": ["no eligible routes"],
        }
    records = tuple(attempt_records)
    supervisor_cost, supervisor_n = supervisor_overhead_from_rows(
        records, supervisor_route=supervisor_route
    )

    if oracle_type == "deterministic":
        verification_cost: float | None = 0.0
    elif oracle_type == "judge":
        verification_cost = _finite_nonnegative(reviewer_expected_cost_usd)
    else:
        verification_cost = None

    costs = [_attempt_cost(item.demand, item.pricing) for item in items]
    probabilities = [_laplace(item.evidence) for item in items]
    class_keys = [
        class_key
        or _field(item.evidence, "class_key")
        or _field(item.demand, "class_key")
        for item in items
    ]
    repair_probabilities = [
        _repair_probability(
            item.route,
            key if isinstance(key, str) else None,
            _field(item.evidence, "level"),
            records,
            probability,
        )
        for item, key, probability in zip(items, class_keys, probabilities, strict=True)
    ]

    expected: list[float | None] = [None] * len(items)
    next_cost: float | None = terminal
    for index in range(len(items) - 1, -1, -1):
        a = costs[index]
        q = probabilities[index]
        q_prime = repair_probabilities[index]
        if None in (a, verification_cost, supervisor_cost, q, q_prime, next_cost):
            expected[index] = None
            next_cost = None
            continue
        assert verification_cost is not None
        assert supervisor_cost is not None
        assert q is not None and q_prime is not None and next_cost is not None
        first = a + verification_cost + supervisor_cost  # type: ignore[operator]
        repair = a + verification_cost + supervisor_cost  # a' uses same known demand
        expected[index] = first + (1 - q) * (repair + (1 - q_prime) * next_cost)
        next_cost = expected[index]

    rendered: list[dict[str, Any]] = []
    for item, a, q, q_prime, value in zip(
        items, costs, probabilities, repair_probabilities, expected, strict=True
    ):
        rendered.append(
            dict(
                zip(
                    _RUNG_KEYS,
                    (
                        item.route,
                        a if a is not None else EXPECTED_COST_UNAVAILABLE,
                        (
                            verification_cost
                            if verification_cost is not None
                            else EXPECTED_COST_UNAVAILABLE
                        ),
                        (
                            supervisor_cost
                            if supervisor_cost is not None
                            else EXPECTED_COST_UNAVAILABLE
                        ),
                        q if q is not None else EXPECTED_COST_UNAVAILABLE,
                        q_prime if q_prime is not None else EXPECTED_COST_UNAVAILABLE,
                        value if value is not None else EXPECTED_COST_UNAVAILABLE,
                    ),
                )
            )
        )

    reasons: list[str] = []
    if any(cost is None for cost in costs):
        reasons.append("demand or marginal pricing unavailable")
    if any(probability is None for probability in probabilities):
        reasons.append("evidence unavailable")
    if verification_cost is None:
        reasons.append("verification expected cost unavailable")
    if supervisor_cost is None:
        reasons.append(
            "supervisor overhead unavailable: attempt-record v2 carries no "
            "attributable supervision-cost observation (its cost fields price "
            "the worker attempt, not the supervision), so the median cannot "
            f"be derived ({supervisor_n} eligible rows attest "
            f"supervisor_route; need {MINIMUM_SAMPLE_SIZE} attributable "
            "observations)"
        )

    if all(value is not None for value in expected):
        if channel_kind_by_route is None:
            start_index = min(
                range(len(items)),
                key=lambda index: expected[index],  # type: ignore[arg-type]
            )
            ordered = rendered
            escalation = [item.route for item in items[start_index:]]
        else:
            order = sorted(
                range(len(items)),
                key=lambda index: (
                    _channel_tier(items[index].route, channel_kind_by_route),
                    expected[index],
                    index,
                ),
            )
            ordered = [rendered[index] for index in order]
            escalation = [items[index].route for index in order]
            start_index = order[0]
        status = EXPECTED_COST_AVAILABLE
    else:
        order = sorted(
            range(len(items)),
            key=lambda index: (
                _channel_tier(items[index].route, channel_kind_by_route),
                _proof_rank(items[index].proof_status),
                _marginal_rank(items[index]),
                index,
            ),
        )
        ordered = [rendered[index] for index in order]
        escalation = [items[index].route for index in order]
        start_index = order[0]
        status = EXPECTED_COST_UNAVAILABLE

    return {
        "rungs": ordered,
        "argmin_start": items[start_index].route,
        "escalation": escalation,
        "expected_cost_status": status,
        "unavailable_reasons": reasons,
    }


RungInput = LadderInput
build_ladder = calculate_ladder
compute_ladder = calculate_ladder
ladder = calculate_ladder
