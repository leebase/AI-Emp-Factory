"""Evidence report (P5-1): per-class/per-route breakdown with cost, escalation,
reviewer-fallback, and headroom data for one calendar month.

The report builds on :func:`rollup.build_rollup` for benchmark de-duplication
and grouping — it does not read the attempt ledger raw and re-group.  Every
number in the report traces to a source row; a number with no source is a
defect, not a rounding choice.

Cost per verified success reports the *list* and *marginal* mean across
verified-success attempts in the group.  When any counted attempt lacks a
usable ``cost`` object, the report states ``unavailable`` with the reason
(e.g. "3 of 5 verified attempts have no cost record") rather than presenting
a partial average as if it were complete.  Cost is read directly from
``cost.usd_list`` and ``cost.usd_marginal`` on each record — never recomputed
from usage tokens and a route id, because that would duplicate
:func:`terms.route_price` and could drift from the price actually charged or
decided at attempt time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from lee_llm_router.staffing.json_int import dump_json
from lee_llm_router.staffing.ledger import read_attempts, resolve_attempts_path
from lee_llm_router.staffing.rollup import (
    MINIMUM_SAMPLE_SIZE,
    TOKEN_FIELDS,
)

# ---------------------------------------------------------------------------
# Internal helpers (re-implemented from rollup.py to avoid importing private
# functions; the logic is identical)
# ---------------------------------------------------------------------------

CHANNEL_IDS: tuple[str, ...] = (
    "openai-sub",
    "anthropic-sub",
    "gemini-sub",
    "opencode-go",
    "openrouter",
    "opencode-zen",
    "local",
)
"""The seven funding channels the evidence report tracks."""


def _string_or_none(value: object) -> str | None:
    """Return a non-empty string, or the explicit unavailable value ``None``."""
    return value if isinstance(value, str) and value else None


def _route_id(record: Mapping[str, Any]) -> str | None:
    """Read the route id from the v2 router-event evidence."""
    event = record.get("router_event")
    if isinstance(event, Mapping):
        route_id = _string_or_none(event.get("route_id"))
        if route_id is not None:
            return route_id
    route_id = _string_or_none(record.get("route_id"))
    if route_id is not None:
        return route_id
    route = record.get("route")
    if isinstance(route, Mapping):
        return _string_or_none(route.get("route_id"))
    return None


def _channel_instance(record: Mapping[str, Any]) -> str | None:
    """Read the channel instance recorded on the route object."""
    route = record.get("route")
    if isinstance(route, Mapping):
        return _string_or_none(route.get("channel_instance"))
    return None


def _class_key(record: Mapping[str, Any]) -> str | None:
    """Read the canonical class key without deriving or rewriting it."""
    class_record = record.get("class_record")
    if isinstance(class_record, Mapping):
        class_key = _string_or_none(class_record.get("class_key"))
        if class_key is not None:
            return class_key
    return _string_or_none(record.get("class_key"))


# ---------------------------------------------------------------------------
# Benchmark de-duplication (re-implemented from rollup._without_superseded)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Benchmark source-run-id extraction (matches rollup._without_superseded
# logic using import_evidence.benchmark_source_run_id)
# ---------------------------------------------------------------------------


def _run_id_from_payload(record: Mapping[str, Any]) -> str | None:
    """Return the benchmark source run id, or None for non-benchmark.

    Mirrors ``import_evidence.benchmark_source_run_id`` exactly: reads
    ``record_kind == 'benchmark_run'``, then extracts ``run_id`` from the
    ``benchmark_run`` payload for v6 records, or recovers it from legacy
    crew-run attempt ids.  Non-benchmark records return None.
    """
    if record.get("record_kind") != "benchmark_run":
        return None
    payload = record.get("benchmark_run")
    if not isinstance(payload, Mapping):
        return None
    run_id = payload.get("run_id")
    if isinstance(run_id, str) and run_id:
        return run_id
    # Legacy crew-run: payload has schema_version "benchmark.crew-run/1"
    # and attempt_id is "benchmark:<run_id>".
    if payload.get("schema_version") == "benchmark.crew-run/1":
        aid = record.get("attempt_id", "")
        if isinstance(aid, str) and aid.startswith("benchmark:"):
            return aid[len("benchmark:") :]
    return None


_BENCHMARK_CORRECTION_PREFIX = "benchmark:v6:"
"""Prefix of canonical correction attempt ids, matching import_evidence."""

_BENCHMARK_SCHEMA_VERSION = "benchmark.staffing-evidence/2"
"""Current benchmark payload version, matching import_evidence."""


def _is_v6_payload(record: Mapping[str, Any]) -> bool:
    """True when the record embeds a raw benchmark v6 payload.

    Mirrors ``import_evidence.is_benchmark_v6_record``:
    ``record_kind == 'benchmark_run'`` and ``benchmark_run.schema_version``
    equals the current version string.
    """
    payload = record.get("benchmark_run")
    return (
        record.get("record_kind") == "benchmark_run"
        and isinstance(payload, Mapping)
        and payload.get("schema_version") == _BENCHMARK_SCHEMA_VERSION
    )


def _deduplicate_benchmarks(
    records: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Materialize one preferred benchmark record per source run.

    Identical logic to ``rollup._without_superseded_benchmark_lines``:
    the canonical ``benchmark:v6:<run_id>`` correction wins over other raw-v6
    lines, which win over legacy lines.  Non-benchmark records pass through
    unchanged.
    """

    candidates_by_run: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        run_id = _run_id_from_payload(record)
        if run_id is not None:
            candidates_by_run.setdefault(run_id, []).append(record)

    def _preference_key(run_id: str, record: Mapping[str, Any]) -> tuple[int, str, str]:
        attempt_id = record["attempt_id"]
        canonical_id = f"{_BENCHMARK_CORRECTION_PREFIX}{run_id}"
        if _is_v6_payload(record):
            if attempt_id == canonical_id:
                rank = 0
            elif attempt_id.startswith(_BENCHMARK_CORRECTION_PREFIX):
                rank = 1
            else:
                rank = 2
        else:
            rank = 3
        return rank, attempt_id, dump_json(dict(record), sort_keys=True)

    winners = {
        run_id: min(candidates, key=lambda c: _preference_key(run_id, c))
        for run_id, candidates in candidates_by_run.items()
    }

    kept: list[Mapping[str, Any]] = []
    emitted_runs: set[str] = set()
    for record in records:
        run_id = _run_id_from_payload(record)
        if run_id is None:
            kept.append(record)
        elif run_id not in emitted_runs:
            kept.append(winners[run_id])
            emitted_runs.add(run_id)
    return kept


# ---------------------------------------------------------------------------
# Month filtering
# ---------------------------------------------------------------------------


def _month_bounds(month: str) -> tuple[str, str]:
    """Return (month_start_iso, next_month_start_iso) for a YYYY-MM string.

    Args:
        month: ISO calendar month (e.g. ``"2026-09"``).

    Returns:
        A pair of ISO-8601 datetime strings: the first moment of the month
        and the first moment of the following month.
    """
    import datetime

    year_s, month_s = month.split("-", 1)
    year = int(year_s)
    m = int(month_s)
    start = datetime.date(year, m, 1)
    if m == 12:
        next_start = datetime.date(year + 1, 1, 1)
    else:
        next_start = datetime.date(year, m + 1, 1)
    return (
        start.strftime("%Y-%m-%dT00:00:00Z"),
        next_start.strftime("%Y-%m-%dT00:00:00Z"),
    )


def _captured_at(record: Mapping[str, Any]) -> str | None:
    """Return the captured_at string, or None if absent."""
    value = record.get("captured_at")
    return value if isinstance(value, str) else None


def _in_month(captured_at: str, month_start: str, next_month_start: str) -> bool:
    """True when captured_at is within [month_start, next_month_start)."""
    return month_start <= captured_at < next_month_start


# ---------------------------------------------------------------------------
# Cost helpers
# ---------------------------------------------------------------------------

_CostResult = dict[str, Any]
"""Either ``{"usd_list": <float>, "usd_marginal": <float>}`` or
``{"unavailable": "<reason>"}``."""


def _verified_successes(
    members: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Return records with verified_success == True."""
    return [r for r in members if r.get("verified_success") is True]


def _has_usable_cost(record: Mapping[str, Any]) -> bool:
    """True when the record has a cost object with numeric list and marginal.

    A record is usable when ``cost`` is a non-None mapping with
    ``usd_list`` as a finite number (int or float) and ``usd_marginal`` as
    a finite number.  ``None``, missing keys, non-finite floats, or any
    other non-numeric type makes it unusable.
    """
    cost = record.get("cost")
    if not isinstance(cost, Mapping):
        return False
    usd_list = cost.get("usd_list")
    usd_marginal = cost.get("usd_marginal")
    if not isinstance(usd_list, (int, float)):
        return False
    if not isinstance(usd_marginal, (int, float)):
        return False
    import math

    if not math.isfinite(usd_list) or not math.isfinite(usd_marginal):
        return False
    return True


def _compute_cost(
    members: list[Mapping[str, Any]],
) -> _CostResult:
    """Compute cost per verified success for a group of records.

    Returns either ``{"usd_list": ..., "usd_marginal": ...}`` with the mean
    cost across all verified successes, or ``{"unavailable": "<reason>"}``
    when any verified success lacks a usable cost object.
    """
    verified = _verified_successes(members)
    if not verified:
        return {"unavailable": "no verified successes in group"}

    missing: list[str] = []
    for v in verified:
        if not _has_usable_cost(v):
            cost_raw = v.get("cost")
            if cost_raw is None:
                missing.append("missing cost object")
            elif not isinstance(cost_raw, Mapping):
                missing.append("cost is not a mapping")
            else:
                basis = cost_raw.get("basis")
                missing.append(
                    f"cost.basis={basis!r} has no numeric usd_list/usd_marginal"
                )

    if missing:
        reason = (
            f"{len(missing)} of {len(verified)} verified attempts "
            f"have no usable cost record"
        )
        return {"unavailable": reason}

    list_sum = 0.0
    marginal_sum = 0.0
    for v in verified:
        cost = v["cost"]
        list_sum += cost["usd_list"]
        marginal_sum += cost["usd_marginal"]

    count = len(verified)
    return {
        "usd_list": list_sum / count,
        "usd_marginal": marginal_sum / count,
    }


# ---------------------------------------------------------------------------
# Token helpers (reuse rollup's token fields and aggregation)
# ---------------------------------------------------------------------------


def _token_values(members: list[Mapping[str, Any]], field: str) -> list[int]:
    """Collect only known values for one token component."""
    values: list[int] = []
    for record in members:
        usage = record.get("usage")
        if not isinstance(usage, Mapping):
            continue
        value = usage.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        values.append(value)
    return values


def _tokens_per_verified_success(
    members: list[Mapping[str, Any]],
) -> dict[str, int | float | str | None]:
    """Compute token sums per verified success.

    Each verified success's token counters (input_tokens, output_tokens,
    etc.) are summed across the group and divided by the verified success
    count.  A token component with no observed values reports as ``None``
    (not zero).
    """
    verified = _verified_successes(members)
    if not verified:
        return {field: None for field in TOKEN_FIELDS}

    result: dict[str, int | float | str | None] = {}
    for field in TOKEN_FIELDS:
        values = _token_values(verified, field)
        if not values:
            result[field] = None
        else:
            total = sum(values)
            # Division may produce a float; use float precision since token
            # sums are never large enough to overflow a float significand
            # in practice.
            result[field] = total / len(verified)
    return result


# ---------------------------------------------------------------------------
# Escalation helpers
# ---------------------------------------------------------------------------


def _escalation_reason(
    record: Mapping[str, Any],
    all_records_index: dict[str, Mapping[str, Any]],
) -> str | None:
    """Return the escalation reason for an escalated record.

    Reads ``parent_attempt_id`` and ``escalation_reason`` directly from the
    record.  If the ``escalation_reason`` is already recorded on the child,
    returns it.  Otherwise, looks up the parent record (by
    ``parent_attempt_id``) and returns the parent's ``failure_class`` or
    an empty string if neither is available.
    """
    reason = record.get("escalation_reason")
    if isinstance(reason, str) and reason:
        return reason
    parent_id = record.get("parent_attempt_id")
    if isinstance(parent_id, str) and parent_id:
        parent = all_records_index.get(parent_id)
        if parent is not None:
            parent_failure = parent.get("failure_class")
            if isinstance(parent_failure, str) and parent_failure:
                return parent_failure
            parent_verdict = parent.get("verdict")
            if isinstance(parent_verdict, str) and parent_verdict:
                return parent_verdict
        return f"parent {parent_id} not found in dataset"
    return None


def _count_escalations(
    members: list[Mapping[str, Any]],
    all_records_index: dict[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Count and describe escalation records in the group.

    Returns a list of escalation descriptions, each with the child's
    ``attempt_id``, ``parent_attempt_id``, and ``reason`` (or ``None``
    when unavailable).
    """
    escalations: list[dict[str, Any]] = []
    for record in members:
        parent_id = record.get("parent_attempt_id")
        if isinstance(parent_id, str) and parent_id:
            reason = _escalation_reason(record, all_records_index)
            escalations.append(
                {
                    "attempt_id": record.get("attempt_id"),
                    "parent_attempt_id": parent_id,
                    "escalation_reason": reason,
                }
            )
    return escalations


# ---------------------------------------------------------------------------
# Reviewer-fallback helpers
# ---------------------------------------------------------------------------

_REVIEWER_ROLES: frozenset[str] = frozenset({"review", "judge"})
"""Roles where independence/applicability exclusions can trigger a fallback."""

_REVIEWER_FALLBACK_UNAVAILABLE_REASON: str = (
    "selection.excluded records independence exclusions without explain order; "
    "fallback cannot be read from the record"
)
_REVIEWER_FALLBACK_NO_SELECTION_REASON: str = (
    "record carries no selection object; fallback cannot be read from the record"
)


def _is_reviewer_role(record: Mapping[str, Any]) -> bool:
    """True when the record's class_record.role or class_role is review/judge."""
    class_record = record.get("class_record")
    if isinstance(class_record, Mapping):
        role = class_record.get("role")
        if isinstance(role, str) and role in _REVIEWER_ROLES:
            return True
    role = record.get("class_role")
    if isinstance(role, str) and role in _REVIEWER_ROLES:
        return True
    ck = _class_key(record)
    if isinstance(ck, str) and ck.split("/", 1)[0] in _REVIEWER_ROLES:
        return True
    return False


def _reviewer_fallbacks(
    members: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Identify reviewer-fallback counts in the group truthfully.

    For each review/judge-role record:
    - ``basis == "explicit"``: not a fallback (the caller chose the route;
      exclusions are enforcement). Certain.
    - ``basis == "explain_cheapest_eligible"``: if ``selection.excluded`` has
      no independence entry, not a fallback (certain). If it has an
      independence entry, fallback status is undecidable from the record
      because ``selection.excluded`` records route ids and reasons without
      order or marginal price, and re-running eligibility is forbidden.
    - any other basis, or no ``selection``: undecidable.

    Returns:
        ``{"count": <int>, "undecidable": <int>, "unavailable_reason": <str or None>}``
        where ``unavailable_reason`` is set when ``undecidable > 0``, else None.
    """
    count = 0
    undecidable = 0
    reasons: list[str] = []

    def _undecidable(reason: str) -> None:
        nonlocal undecidable
        undecidable += 1
        if reason not in reasons:
            reasons.append(reason)

    for record in members:
        if not _is_reviewer_role(record):
            continue
        selection = record.get("selection")
        if not isinstance(selection, Mapping):
            _undecidable(_REVIEWER_FALLBACK_NO_SELECTION_REASON)
            continue

        basis = selection.get("basis")
        if basis == "explicit":
            continue
        if basis == "explain_cheapest_eligible":
            excluded = selection.get("excluded")
            has_independence = False
            if isinstance(excluded, list):
                for exclusion in excluded:
                    if isinstance(exclusion, Mapping):
                        excl_reason = exclusion.get("reason", "")
                        if (
                            isinstance(excl_reason, str)
                            and "independence" in excl_reason.lower()
                        ):
                            has_independence = True
                            break
            if has_independence:
                _undecidable(_REVIEWER_FALLBACK_UNAVAILABLE_REASON)
            continue

        _undecidable(f"selection.basis {basis!r} is not a recognized basis")

    unavailable_reason = "; ".join(reasons) if undecidable > 0 else None
    return {
        "count": count,
        "undecidable": undecidable,
        "unavailable_reason": unavailable_reason,
    }


# ---------------------------------------------------------------------------
# Enriched group computation
# ---------------------------------------------------------------------------


def _class_role(record: Mapping[str, Any]) -> str | None:
    """Read the class_record.role, or None if absent."""
    class_record = record.get("class_record")
    if isinstance(class_record, Mapping):
        return _string_or_none(class_record.get("role"))
    return None


def _enrich_group(
    route_id: str | None,
    class_key: str | None,
    channel_instance: str | None,
    members: list[Mapping[str, Any]],
    all_records_index: dict[str, Mapping[str, Any]],
    source_ledger: str = "",
) -> dict[str, Any]:
    """Build one enriched evidence-report row for a route/class group.

    Starts from the same grouping key used by ``build_rollup`` and adds
    cost, escalation, and reviewer-fallback columns.
    """
    verified = _verified_successes(members)
    attempts = len(members)
    verified_pass = len(verified)
    pass_rate = verified_pass / attempts if attempts > 0 else 0.0

    cost = _compute_cost(members)
    tokens = _tokens_per_verified_success(members)
    escalations = _count_escalations(members, all_records_index)
    fallbacks = _reviewer_fallbacks(members)

    source_attempt_ids = [
        (
            r.get("attempt_id")
            if isinstance(r.get("attempt_id"), str) and r.get("attempt_id")
            else "<no attempt_id>"
        )
        for r in members
    ]

    return {
        "route_id": route_id,
        "class_key": class_key,
        "channel_instance": channel_instance,
        "source_ledger": source_ledger,
        "source_attempt_ids": source_attempt_ids,
        "attempts": attempts,
        "verified_pass": verified_pass,
        "pass_rate": round(pass_rate, 4),
        "comparison_eligible": attempts >= MINIMUM_SAMPLE_SIZE,
        "cost_per_verified_success": cost,
        "tokens_per_verified_success": tokens,
        "escalations": len(escalations),
        "escalation_details": escalations,
        "reviewer_fallbacks": fallbacks,
    }


def _group_sort_key(
    key: tuple[str | None, str | None, str | None],
) -> tuple[int, str, int, str, int, str]:
    """Sort known string keys lexically, with unavailable keys first."""
    route_id, class_key, channel_instance = key
    return (
        0 if route_id is None else 1,
        route_id or "",
        0 if class_key is None else 1,
        class_key or "",
        0 if channel_instance is None else 1,
        channel_instance or "",
    )


# ---------------------------------------------------------------------------
# Channel headroom
# ---------------------------------------------------------------------------


def _channel_headroom_rows(
    catalog: Any,
    availability_file: str | Path | None,
) -> list[dict[str, Any]]:
    """Build the per-channel headroom section of the report.

    For each of the seven channels, loads the headroom from availability
    and the reserve fraction from the catalog, and reports whether the
    channel is inside or outside its reserve.

    ``catalog`` is already loaded by the caller (an invalid ``--catalog-dir``
    is a caller error that propagates, not a per-channel degradation). When
    the availability snapshot itself is unusable, channels are reported with
    ``remaining_fraction: null`` and ``availability`` naming the reason —
    live-data unavailability stays fail-soft.
    """
    from lee_llm_router.availability import load_availability

    availability = load_availability(availability_file)
    channels_by_id = {c.channel_id: c for c in catalog.channels.channels}

    rows: list[dict[str, Any]] = []
    for channel_id in CHANNEL_IDS:
        headroom = availability.headroom(channel_id)
        reserve_frac = catalog.policy.reserve_fraction.fraction_for(channel_id)
        remaining_frac = headroom.remaining_fraction

        if availability.problem is not None:
            inside = None
            av_status = f"snapshot problem: {availability.problem}"
        elif remaining_frac is None:
            inside = None
            av_status = f"no remaining_fraction for {channel_id}"
        else:
            inside = remaining_frac <= reserve_frac
            av_status = "inside reserve" if inside else "outside reserve"

        channel = channels_by_id.get(channel_id)
        if channel is not None:
            instances = channel.effective_instances()
        else:
            from lee_llm_router.staffing.catalog import ChannelInstance

            instances = (
                ChannelInstance(
                    instance_id=channel_id,
                    credential_ref=channel_id,
                    enabled=True,
                ),
            )

        instance_rows: list[dict[str, Any]] = []
        for inst in instances:
            inst_id = inst.instance_id
            inst_headroom = availability.instance_headroom(channel_id, inst_id)
            inst_remaining = inst_headroom.remaining_fraction

            if availability.problem is not None:
                inst_inside = None
                inst_status = f"snapshot problem: {availability.problem}"
            elif inst_remaining is None:
                inst_inside = None
                inst_status = f"no remaining_fraction for {inst_id}"
            else:
                inst_inside = inst_remaining <= reserve_frac
                inst_status = "inside reserve" if inst_inside else "outside reserve"

            instance_rows.append(
                {
                    "instance_id": inst_id,
                    "remaining_fraction": inst_remaining,
                    "reserve_fraction": reserve_frac,
                    "inside_reserve": inst_inside,
                    "availability": inst_status,
                }
            )

        rows.append(
            {
                "channel_id": channel_id,
                "remaining_fraction": remaining_frac,
                "reserve_fraction": reserve_frac,
                "inside_reserve": inside,
                "availability": av_status,
                "instances": instance_rows,
            }
        )

    return rows


def _default_catalog_dir() -> Path:
    """Return the repository's bundled ``config/staffing`` catalog directory."""
    return Path(__file__).resolve().parents[3] / "config" / "staffing"


# ---------------------------------------------------------------------------
# Route change recommendations
# ---------------------------------------------------------------------------


def _is_finite_number(value: object) -> bool:
    """Return whether a value is a finite, non-boolean number."""
    import math

    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _unavailable_cost_count(cost: Mapping[str, Any], verified_pass: int) -> int:
    """Extract the missing-success count from a standard unavailable reason."""
    reason = cost.get("unavailable")
    if isinstance(reason, str):
        parts = reason.split(" ", 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1] == "of":
            return min(int(parts[0]), verified_pass)
    return verified_pass


def _combine_route_costs(
    rows: list[dict[str, Any]], verified_pass: int
) -> dict[str, Any]:
    """Combine per-instance cost means into a route-level cost mean."""
    if verified_pass == 0:
        return {"unavailable": "no verified successes in group"}
    if len(rows) == 1:
        cost = rows[0].get("cost_per_verified_success")
        if isinstance(cost, Mapping):
            return dict(cost)
        return {
            "unavailable": (
                f"{verified_pass} of {verified_pass} verified attempts "
                "have no usable cost record"
            )
        }

    list_sum = 0.0
    marginal_sum = 0.0
    usable_count = 0
    missing_count = 0

    for row in rows:
        row_verified = row.get("verified_pass", 0)
        if not isinstance(row_verified, int) or row_verified <= 0:
            continue
        cost = row.get("cost_per_verified_success")
        if not isinstance(cost, Mapping):
            missing_count += row_verified
            continue
        if "unavailable" in cost:
            missing_count += _unavailable_cost_count(cost, row_verified)
            continue

        usd_list = cost.get("usd_list")
        marginal = cost.get("usd_marginal")
        if not _is_finite_number(usd_list) or not _is_finite_number(marginal):
            missing_count += row_verified
            continue

        list_sum += float(usd_list) * row_verified
        marginal_sum += float(marginal) * row_verified
        usable_count += row_verified

    if missing_count > 0 or usable_count != verified_pass:
        return {
            "unavailable": (
                f"{missing_count or verified_pass - usable_count} of "
                f"{verified_pass} verified attempts have no usable cost record"
            )
        }

    return {
        "usd_list": list_sum / verified_pass,
        "usd_marginal": marginal_sum / verified_pass,
    }


def _aggregate_route_rows(
    enriched: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse per-instance evidence rows into one row per route and class."""
    grouped: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for row in enriched:
        key = (row.get("class_key"), row.get("route_id"))
        grouped.setdefault(key, []).append(row)

    aggregates: list[dict[str, Any]] = []
    for (class_key, route_id), rows in grouped.items():
        attempts = sum(row.get("attempts", 0) for row in rows)
        verified_pass = sum(row.get("verified_pass", 0) for row in rows)
        aggregates.append(
            {
                "class_key": class_key,
                "route_id": route_id,
                "attempts": attempts,
                "verified_pass": verified_pass,
                "pass_rate": round(
                    verified_pass / attempts if attempts > 0 else 0.0, 4
                ),
                "comparison_eligible": attempts >= MINIMUM_SAMPLE_SIZE,
                "cost_per_verified_success": _combine_route_costs(rows, verified_pass),
            }
        )
    return aggregates


def _route_changes(
    enriched: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Recommend cheaper routes for comparison-eligible classes.

    For any class_key with ``comparison_eligible`` (n >= 5) at more than
    one route, name the cheapest route by mean ``cost.usd_marginal`` among
    routes with ``verified_pass / attempts >= 0.8``.  If a route has cost
    marked ``unavailable``, it is excluded from the ranking.

    This is a report line, not a catalog write; no ranking touches
    ``crews.yaml``/``routes.yaml``.
    """
    # Group route-level aggregates by class_key.  The public classes section
    # remains per-instance; only recommendation comparisons are route-level.
    by_class: dict[str, list[dict[str, Any]]] = {}
    for row in _aggregate_route_rows(enriched):
        ck = row.get("class_key")
        if ck is not None:
            by_class.setdefault(ck, []).append(row)

    recommendations: list[dict[str, Any]] = []
    not_recommended: list[dict[str, Any]] = []

    for class_key, rows in sorted(by_class.items()):
        if len(rows) < 2:
            not_recommended.append(
                {"class_key": class_key, "reason": "fewer than 2 routes with evidence"}
            )
            continue
        eligible = [
            r
            for r in rows
            if r.get("comparison_eligible") is True
            and r.get("attempts", 0) >= MINIMUM_SAMPLE_SIZE
        ]
        if len(eligible) < 2:
            not_recommended.append(
                {
                    "class_key": class_key,
                    "reason": (
                        f"fewer than 2 comparison-eligible routes "
                        f"(n>={MINIMUM_SAMPLE_SIZE})"
                    ),
                }
            )
            continue

        # Among routes with pass_rate >= 0.8
        viable = [r for r in eligible if r.get("pass_rate", 0.0) >= 0.8]
        if len(viable) < 2:
            not_recommended.append(
                {
                    "class_key": class_key,
                    "reason": "fewer than 2 routes at pass_rate>=0.8",
                }
            )
            continue

        # Exclude routes with unavailable cost
        priced = []
        for r in viable:
            cost = r.get("cost_per_verified_success", {})
            if isinstance(cost, dict) and "unavailable" in cost:
                continue
            if isinstance(cost, dict) and "usd_marginal" in cost:
                priced.append(r)

        if len(priced) < 2:
            not_recommended.append(
                {
                    "class_key": class_key,
                    "reason": "fewer than 2 routes with a usable marginal cost",
                }
            )
            continue

        cheapest = min(
            priced, key=lambda r: r["cost_per_verified_success"]["usd_marginal"]
        )
        first_route = priced[0]
        cheapest_cost = cheapest["cost_per_verified_success"]["usd_marginal"]
        first_cost = first_route["cost_per_verified_success"]["usd_marginal"]

        if (
            cheapest["route_id"] == first_route["route_id"]
            or first_cost <= cheapest_cost
        ):
            not_recommended.append(
                {"class_key": class_key, "reason": "cheapest route already first"}
            )
            continue

        others = [r for r in priced if r["route_id"] != cheapest["route_id"]]

        evidence_parts = []
        for r in [cheapest] + others:
            evidence_parts.append(
                f"{r['route_id']}: n={r['attempts']}, "
                f"pass_rate={r['pass_rate']}, "
                f"mean_marginal={r['cost_per_verified_success']['usd_marginal']:.4f}"
            )

        recommendations.append(
            {
                "class_key": class_key,
                "recommended_route_id": cheapest["route_id"],
                "evidence": "; ".join(evidence_parts),
            }
        )

    return {
        "recommendations": recommendations,
        "not_recommended": not_recommended,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_evidence_report(
    month: str,
    *,
    catalog_dir: str | Path | None = None,
    availability_file: str | Path | None = None,
    ledger_path: str | None = None,
) -> dict[str, Any]:
    """Produce the evidence report for one calendar month.

    Args:
        month: ISO-8601 calendar month (``"2026-09"``).
        catalog_dir: Override catalog directory (default: bundled config).
        availability_file: Override availability snapshot path.
        ledger_path: Override ledger path (default: resolved by
            :func:`resolve_attempts_path`).

    Returns:
        A JSON-serialisable dict with ``report_for``, ``classes`` (list of
        enriched per-route/per-class/instance groups), ``channels`` (per-channel
        headroom), and ``route_changes`` (cheaper-route recommendations).
        A missing or empty ledger produces a valid report with zero groups.
    """
    month_start, next_month_start = _month_bounds(month)

    # Read ledger
    ledger_path_resolved = resolve_attempts_path(ledger_path)
    try:
        raw_records = read_attempts(ledger_path_resolved)
    except FileNotFoundError:
        raw_records = []

    # Filter by month
    month_records = [
        r
        for r in raw_records
        if _in_month(_captured_at(r) or "", month_start, next_month_start)
    ]

    # De-duplicate benchmark records (same semantics as build_rollup)
    materialized = _deduplicate_benchmarks(month_records)

    # Build index of all records for parent lookups
    all_records_index: dict[str, Mapping[str, Any]] = {}
    for r in materialized:
        aid = r.get("attempt_id")
        if isinstance(aid, str):
            all_records_index[aid] = r

    # Group materialized records by (route_id, class_key, channel_instance)
    grouped: dict[
        tuple[str | None, str | None, str | None], list[Mapping[str, Any]]
    ] = {}
    for record in materialized:
        key = (_route_id(record), _class_key(record), _channel_instance(record))
        grouped.setdefault(key, []).append(record)

    # Build enriched groups
    enriched_groups: list[dict[str, Any]] = []
    for (
        route_id,
        class_key,
        channel_instance,
    ) in sorted(grouped, key=_group_sort_key):
        members = grouped[(route_id, class_key, channel_instance)]
        enriched_groups.append(
            _enrich_group(
                route_id,
                class_key,
                channel_instance,
                members,
                all_records_index,
                source_ledger=str(ledger_path_resolved),
            )
        )

    # Channel headroom. An invalid --catalog-dir is a caller/operator error
    # (the same convention as `price`/`route show`) and propagates to the
    # caller rather than degrading silently; a missing/unusable *availability*
    # snapshot is live-data unavailability and stays fail-soft per channel.
    from lee_llm_router.staffing import load_staffing_catalog

    catalog_dir_resolved = Path(catalog_dir) if catalog_dir else _default_catalog_dir()
    catalog = load_staffing_catalog(catalog_dir_resolved)
    channels = _channel_headroom_rows(catalog, availability_file)

    # Route change recommendations
    route_changes_result = _route_changes(enriched_groups)

    return {
        "report_for": month,
        "classes": enriched_groups,
        "channels": channels,
        "route_changes": route_changes_result["recommendations"],
        "route_changes_not_recommended": route_changes_result["not_recommended"],
    }


def render_evidence_report(report: dict[str, Any]) -> str:
    """Render the evidence report as readable text.

    Text mode is a readable rendering of the same data as the JSON output,
    not a second source of truth.
    """
    lines: list[str] = []
    lines.append(f"Evidence report — {report['report_for']}")
    lines.append("")

    classes = report.get("classes", [])
    if not classes:
        lines.append("(no attempts recorded for this month)")
    else:
        routed_classes = [
            group for group in classes if group.get("route_id") is not None
        ]
        unrouted_classes = [group for group in classes if group.get("route_id") is None]
        lines.append(f"Classes ({len(routed_classes)} groups):")
        lines.append("")
        for group_index, group in enumerate(routed_classes + unrouted_classes):
            if group_index == len(routed_classes) and unrouted_classes:
                lines.append(
                    "Unrouted legacy groups "
                    f"({len(unrouted_classes)} groups, no router route recorded):"
                )
                lines.append("")

            route = group["route_id"] or "(none)"
            ck = group["class_key"] or "(none)"
            lines.append(f"  route: {route}")
            lines.append(f"  instance: {group.get('channel_instance') or '(none)'}")
            lines.append(f"  class: {ck}")

            source_ledger = group.get("source_ledger") or "(none)"
            attempt_ids = group.get("source_attempt_ids", [])
            n = len(attempt_ids) if attempt_ids else group.get("attempts", 0)
            if not attempt_ids:
                lines.append(f"  source: {n} attempts from {source_ledger}")
            elif n <= 3:
                ids_str = ", ".join(attempt_ids)
                lines.append(f"  source: {n} attempts from {source_ledger} ({ids_str})")
            else:
                first_three = ", ".join(attempt_ids[:3])
                k = n - 3
                lines.append(
                    f"  source: {n} attempts from {source_ledger} "
                    f"({first_three}, +{k} more)"
                )

            lines.append(f"  attempts: {group['attempts']}")
            lines.append(f"  verified_pass: {group['verified_pass']}")
            lines.append(f"  pass_rate: {group['pass_rate']}")
            lines.append(f"  comparison_eligible: {group['comparison_eligible']}")

            cost = group["cost_per_verified_success"]
            if isinstance(cost, dict) and "unavailable" in cost:
                lines.append(
                    f"  cost_per_verified_success: UNAVAILABLE ({cost['unavailable']})"
                )
            else:
                lines.append(
                    f"  cost_per_verified_success: "
                    f"list={cost.get('usd_list', '?'):.6f}, "
                    f"marginal={cost.get('usd_marginal', '?'):.6f}"
                )

            tokens = group["tokens_per_verified_success"]
            token_parts = []
            for field in TOKEN_FIELDS:
                val = tokens.get(field)
                if val is not None:
                    token_parts.append(f"{field}={val:.1f}")
                else:
                    token_parts.append(f"{field}=null")
            lines.append(f"  tokens_per_verified_success: {', '.join(token_parts)}")
            lines.append(f"  escalations: {group['escalations']}")

            if group["escalation_details"]:
                for esc in group["escalation_details"]:
                    lines.append(
                        f"    -> {esc.get('attempt_id')} "
                        f"parent={esc.get('parent_attempt_id')} "
                        f"reason={esc.get('escalation_reason') or 'N/A'}"
                    )

            rf = group["reviewer_fallbacks"]
            if isinstance(rf, Mapping):
                rf_count = rf.get("count", 0)
                rf_undecidable = rf.get("undecidable", 0)
                rf_reason = rf.get("unavailable_reason")
            else:
                rf_count = int(rf)
                rf_undecidable = 0
                rf_reason = None

            if rf_count == 0 and rf_undecidable == 0:
                lines.append("  reviewer_fallbacks: 0")
            else:
                lines.append(
                    f"  reviewer_fallbacks: {rf_count} "
                    f"(+{rf_undecidable} undecidable: {rf_reason})"
                )
            lines.append("")

    channels = report.get("channels", [])
    if channels:
        lines.append("Channels:")
        lines.append("")
        for ch in channels:
            rf = ch["remaining_fraction"]
            rv = ch["reserve_fraction"]
            inside = ch["inside_reserve"]
            rf_text = f"{rf:.4f}" if rf is not None else "N/A"
            rv_text = f"{rv:.4f}" if rv is not None else "N/A"
            lines.append(
                f"  {ch['channel_id']}: "
                f"remaining={rf_text}, "
                f"reserve={rv_text}, "
                f"status={ch['availability']}"
            )
            if inside is not None:
                lines.append(f"    inside_reserve: {inside}")
            for inst in ch.get("instances", []):
                irf = inst["remaining_fraction"]
                irv = inst["reserve_fraction"]
                irf_text = f"{irf:.4f}" if irf is not None else "N/A"
                irv_text = f"{irv:.4f}" if irv is not None else "N/A"
                lines.append(
                    f"    instance {inst['instance_id']}: "
                    f"remaining={irf_text}, "
                    f"reserve={irv_text}, "
                    f"status={inst['availability']}"
                )
        lines.append("")

    changes = report.get("route_changes", [])
    if changes:
        lines.append("Recommended route changes:")
        lines.append("")
        for rc in changes:
            lines.append(f"  class: {rc['class_key']}")
            lines.append(f"  recommended: {rc['recommended_route_id']}")
            lines.append(f"  evidence: {rc['evidence']}")
            lines.append("")
    else:
        lines.append("Recommended route changes: none")
        not_rec = report.get("route_changes_not_recommended", [])
        counts: dict[str, int] = {}
        for nr in not_rec:
            reason = nr.get("reason", "")
            counts[reason] = counts.get(reason, 0) + 1
        for reason, count in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"  {count} classes: {reason}")
        lines.append("")

    return "\n".join(lines)
