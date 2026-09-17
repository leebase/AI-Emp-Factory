"""Derive cheapest-first escalation ladders from the attempt ledger (P5-2).

Reads the validated attempt ledger through ``rollup.rollup_ledger()`` for
grouping and comparison eligibility, then for each observed class_key ranks
every route with ``comparison_eligible: true`` (n >= ``MINIMUM_SAMPLE_SIZE``,
i.e. n >= 5) cheapest-first by mean ``cost.usd_marginal`` among verified-success
attempts, breaking ties by higher ``verified_pass / attempts``.

The derived ladder is written to a new additive file
``config/staffing/derived-ladders-<YYYY-MM-DD>.json``.  The module also diffs
against the two hand-authored ladder sources per contract §3:
``ai-workforce-benchmark/config/escalation-ladders.json`` (via
``model_aliases``/``role_aliases``) and ``config/staffing/crews.yaml``
``escalation_ladder`` fields (flat route id list, compared directly).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from lee_llm_router.staffing.rollup import (
    MINIMUM_SAMPLE_SIZE,
    rollup_ledger,
)

# ---------------------------------------------------------------------------
# Schema and role constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "derived-ladder/1"
"""Schema version literal for the derived-ladder output file."""

ROUTER_ROLE_TO_BENCHMARK_ROLE: dict[str, str] = {
    "impl": "coder",
    "plan": "planner",
    "review": "code-review",
}
"""Mapping from router taxonomy roles to canonical benchmark roles per contract §3.

Router taxonomy roles (impl, plan, review) map to benchmark canonical roles
(coder, planner, code-review). Roles without benchmark escalation ladders
(judge, prose) are treated as unmatched.
"""

# ---------------------------------------------------------------------------
# Cost helpers
# ---------------------------------------------------------------------------


def _has_usable_cost(record: dict[str, Any]) -> bool:
    """True when the record has a cost object with a finite usd_marginal."""
    import math

    cost = record.get("cost")
    if not isinstance(cost, dict):
        return False
    usd_marginal = cost.get("usd_marginal")
    if not isinstance(usd_marginal, (int, float)):
        return False
    return math.isfinite(usd_marginal)


def _compute_mean_marginal_cost(
    members: list[dict[str, Any]],
) -> float | None:
    """Return the mean ``cost.usd_marginal`` across verified-success records.

    Returns ``None`` when no verified-success record has a usable cost.
    """
    verified = [r for r in members if r.get("verified_success") is True]
    usable = [r for r in verified if _has_usable_cost(r)]
    if not usable:
        return None
    total = sum(r["cost"]["usd_marginal"] for r in usable)
    return total / len(usable)


# ---------------------------------------------------------------------------
# Route id helpers (same logic as rollup._route_id)
# ---------------------------------------------------------------------------


def _route_id(record: dict[str, Any]) -> str | None:
    """Read the route id from the v2 router-event evidence."""
    event = record.get("router_event")
    if isinstance(event, dict):
        route_id = event.get("route_id")
        if isinstance(route_id, str) and route_id:
            return route_id
    route_id = record.get("route_id")
    if isinstance(route_id, str) and route_id:
        return route_id
    route = record.get("route")
    if isinstance(route, dict):
        return route.get("route_id")
    return None


def _class_key(record: dict[str, Any]) -> str | None:
    """Read the canonical class key without deriving or rewriting it."""
    class_record = record.get("class_record")
    if isinstance(class_record, dict):
        ck = class_record.get("class_key")
        if isinstance(ck, str) and ck:
            return ck
    ck = record.get("class_key")
    return ck if isinstance(ck, str) and ck else None


# ---------------------------------------------------------------------------
# Benchmark de-duplication (copied from rollup — identical logic)
# ---------------------------------------------------------------------------

_BENCHMARK_CORRECTION_PREFIX = "benchmark:v6:"
_BENCHMARK_SCHEMA_VERSION = "benchmark.staffing-evidence/2"


def _run_id_from_payload(record: dict[str, Any]) -> str | None:
    """Return the benchmark source run id, or None for non-benchmark."""
    if record.get("record_kind") != "benchmark_run":
        return None
    payload = record.get("benchmark_run")
    if not isinstance(payload, dict):
        return None
    run_id = payload.get("run_id")
    if isinstance(run_id, str) and run_id:
        return run_id
    if payload.get("schema_version") == "benchmark.crew-run/1":
        aid = record.get("attempt_id", "")
        if isinstance(aid, str) and aid.startswith("benchmark:"):
            return aid[len("benchmark:") :]
    return None


def _is_v6_payload(record: dict[str, Any]) -> bool:
    """True when the record embeds a raw benchmark v6 payload."""
    payload = record.get("benchmark_run")
    return (
        record.get("record_kind") == "benchmark_run"
        and isinstance(payload, dict)
        and payload.get("schema_version") == _BENCHMARK_SCHEMA_VERSION
    )


def _deduplicate_benchmarks(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Materialize one preferred benchmark record per source run.

    Identical logic to ``rollup._without_superseded_benchmark_lines``:
    the canonical ``benchmark:v6:<run_id>`` correction wins over other raw-v6
    lines, which win over legacy lines.  Non-benchmark records pass through
    unchanged.
    """
    from lee_llm_router.staffing.json_int import dump_json

    candidates_by_run: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        run_id = _run_id_from_payload(record)
        if run_id is not None:
            candidates_by_run.setdefault(run_id, []).append(record)

    def _preference_key(run_id: str, record: dict[str, Any]) -> tuple[int, str, str]:
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

    kept: list[dict[str, Any]] = []
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
# Derivation
# ---------------------------------------------------------------------------


def _derive_class_ladder(
    class_key: str,
    groups: list[dict[str, Any]],
    cost_map: dict[tuple[str | None, str | None], float | None],
    all_materialized: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive the cheapest-first ladder for one class_key.

    Args:
        class_key: The class key string.
        groups: All rollup groups for this class (one per route).
        cost_map: Pre-computed mean marginal cost per ``(route_id, class_key)``.
        all_materialized: De-duplicated records (not used here directly,
            but kept for future extension).

    Returns:
        A dict with ``class_key``, ``rungs``, and ``evidence_status``.
    """
    eligible = [
        g
        for g in groups
        if g.get("comparison_eligible") is True and g.get("route_id") is not None
    ]

    excluded_routes: list[dict[str, Any]] = []
    rungs_eligible: list[dict[str, Any]] = []
    for g in eligible:
        if g.get("verified_pass", 0) == 0:
            excluded_routes.append(
                {
                    "route_id": g["route_id"],
                    "n": g.get("attempts", 0),
                    "verified_pass": g.get("verified_pass", 0),
                    "reason": "no verified success",
                }
            )
        else:
            rungs_eligible.append(g)

    if not rungs_eligible:
        return {
            "class_key": class_key,
            "rungs": [],
            "excluded_routes": excluded_routes,
            "evidence_status": "insufficient evidence, hand ladder retained",
        }

    # Add cost data to each route
    for g in rungs_eligible:
        key = (g["route_id"], class_key)
        g["_mean_cost"] = cost_map.get(key)
        g["_pass_rate"] = (
            g["verified_pass"] / g["attempts"] if g["attempts"] > 0 else 0.0
        )

    # Sort: cheapest-first by mean cost.usd_marginal, break ties by higher pass_rate
    def _sort_key(g: dict[str, Any]) -> tuple:
        mc = g.get("_mean_cost")
        pr = g.get("_pass_rate", 0.0)
        # Routes with no cost go last; among those with cost, cheaper first;
        # then higher pass_rate first (negate for descending); then route_id
        if mc is None:
            return (1, 0.0, -pr, g["route_id"] or "")
        return (0, mc, -pr, g["route_id"] or "")

    rungs_eligible.sort(key=_sort_key)

    rungs = []
    for g in rungs_eligible:
        rungs.append(
            {
                "route_id": g["route_id"],
                "n": g["attempts"],
                "verified_pass": g["verified_pass"],
                "pass_rate": round(g["verified_pass"] / g["attempts"], 4),
                "mean_cost_usd_marginal": g["_mean_cost"],
            }
        )

    return {
        "class_key": class_key,
        "rungs": rungs,
        "excluded_routes": excluded_routes,
        "evidence_status": "derived",
    }


def _read_materialized_records(
    ledger_path: str | None = None,
) -> list[dict[str, Any]]:
    """Read the ledger, de-duplicate benchmarks, return materialized records."""
    from lee_llm_router.staffing.ledger import read_attempts, resolve_attempts_path

    resolved = resolve_attempts_path(ledger_path)
    try:
        raw = read_attempts(resolved)
    except FileNotFoundError:
        raw = []
    return _deduplicate_benchmarks(raw)


def derive_ladders(
    *,
    ledger_path: str | None = None,
) -> dict[str, Any]:
    """Derive cheapest-first ladders from the attempt ledger.

    Args:
        ledger_path: Optional explicit ledger path.

    Returns:
        A JSON-serialisable dict with ``schema_version``, ``derived_at``
        (ISO date), and ``classes`` (list of per-class ladder results).
    """
    # 1. Get the rollup for grouping and comparison eligibility
    rollup = rollup_ledger(ledger_path)
    all_groups = rollup.get("groups", [])

    # 2. Read materialized records for cost computation
    materialized = _read_materialized_records(ledger_path)

    # 3. Compute mean cost per (route_id, class_key) for verified-success records
    cost_map: dict[tuple[str | None, str | None], float | None] = {}
    grouped: dict[tuple[str | None, str | None], list[dict[str, Any]]] = {}
    for record in materialized:
        key = (_route_id(record), _class_key(record))
        grouped.setdefault(key, []).append(record)

    for key, members in grouped.items():
        cost_map[key] = _compute_mean_marginal_cost(members)

    # 4. Group rollup groups by class_key
    by_class: dict[str, list[dict[str, Any]]] = {}
    for group in all_groups:
        ck = group.get("class_key")
        if ck is not None:
            by_class.setdefault(ck, []).append(group)

    # 5. Derive per-class ladders
    classes: list[dict[str, Any]] = []
    for class_key in sorted(by_class):
        classes.append(
            _derive_class_ladder(
                class_key,
                by_class[class_key],
                cost_map,
                materialized,
            )
        )

    today = date.today().isoformat()
    return {
        "schema_version": SCHEMA_VERSION,
        "derived_at": today,
        "classes": classes,
    }


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------


def _load_benchmark_ladders(
    benchmark_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Load ``ai-workforce-benchmark/config/escalation-ladders.json``.

    Args:
        benchmark_path: Explicit path.  When None, resolves the default
            location at ``~/projects/ai-workforce-benchmark/config/
            escalation-ladders.json``.

    Returns:
        The parsed JSON object, or ``None`` if the file cannot be read.
    """
    if benchmark_path is None:
        sibling = (
            Path(__file__).resolve().parents[4]
            / "ai-workforce-benchmark"
            / "config"
            / "escalation-ladders.json"
        )
        if sibling.is_file():
            benchmark_path = sibling
        else:
            benchmark_path = (
                Path.home()
                / "projects"
                / "ai-workforce-benchmark"
                / "config"
                / "escalation-ladders.json"
            )
    path = Path(benchmark_path)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _load_crews_ladders(
    catalog_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Extract ``escalation_ladder`` fields from crew entries.

    Args:
        catalog_dir: Catalog directory holding ``crews.yaml``.  When None,
            resolves the default repo ``config/staffing`` directory.

    Returns:
        A list of ``{"crew_id": str, "escalation_ladder": list[str]}`` dicts
        for every crew with a non-empty ``escalation_ladder``, or a single
        error entry dict on catalog load failure.
    """
    from lee_llm_router.staffing.catalog import (
        StaffingCatalogError,
        load_staffing_catalog,
    )

    if catalog_dir is None:
        catalog_dir = Path(__file__).resolve().parents[3] / "config" / "staffing"
    try:
        catalog = load_staffing_catalog(Path(catalog_dir))
    except (StaffingCatalogError, OSError, UnicodeError) as exc:
        return [{"source": "crews.yaml", "error": str(exc)}]

    ladders: list[dict[str, Any]] = []
    for crew in catalog.crews.crews:
        ladder = getattr(crew, "escalation_ladder", None)
        if (
            isinstance(ladder, Sequence)
            and not isinstance(ladder, (str, bytes))
            and ladder
        ):
            ladders.append(
                {
                    "crew_id": crew.crew_id,
                    "escalation_ladder": list(ladder),
                }
            )
    return ladders


def _resolve_route_model_family(
    route_id: str,
    catalog_routes: dict[str, Any],
    model_aliases: dict[str, Any],
) -> str:
    """Resolve a route id's model to a model_family through model_aliases.

    Looks up the route in the catalog, reads its ``model`` field, then
    looks up that model in ``model_aliases``.  Returns the model_family
    string, or ``"unmapped"`` when either the route or its model lacks an
    alias entry.
    """
    route = catalog_routes.get(route_id)
    if route is None:
        return "unmapped"
    model = getattr(route, "model", None)
    if model is None:
        return "unmapped"
    alias = model_aliases.get(model)
    if isinstance(alias, dict) and "model_family" in alias:
        return alias["model_family"]
    return "unmapped"


def _build_catalog_route_map(
    catalog_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Build a ``{route_id: route_obj}`` map from the staffing catalog."""
    from lee_llm_router.staffing.catalog import load_staffing_catalog

    if catalog_dir is None:
        catalog_dir = Path(__file__).resolve().parents[3] / "config" / "staffing"
    try:
        catalog = load_staffing_catalog(Path(catalog_dir))
    except Exception:
        return {}

    return {r.route_id: r for r in catalog.routes.routes}


def _diff_benchmark_ladder(
    derived_rungs: list[dict[str, Any]],
    benchmark_rungs: list[dict[str, Any]],
    catalog_routes: dict[str, Any],
    model_aliases: dict[str, Any],
    role_aliases: dict[str, str],
    role: str,
    ladder_id: str,
    label: str,
) -> dict[str, Any]:
    """Diff one derived ladder against one benchmark ladder.

    Args:
        derived_rungs: Derived rung list (``route_id`` order).
        benchmark_rungs: Benchmark ladder rungs (``worker.model_family`` etc.).
        catalog_routes: Route id -> route object map.
        model_aliases: Model id -> ``{model_family, display_name}``.
        role_aliases: Benchmark role -> canonical role.
        role: The benchmark ladder's role.
        ladder_id: The benchmark ladder's id.
        label: The benchmark ladder's label.

    Returns:
        A diff entry with ``ladder_id``, ``role``, ``label``,
        ``agreement`` (True/False), and details of disagreement.
    """
    # Per contract §3, benchmark comparison is by model_family only.
    # Map derived route_ids -> model_family through model_aliases.
    derived_families: list[str] = []
    for rung in derived_rungs:
        route_id = rung["route_id"]
        route = catalog_routes.get(route_id)
        if route is None:
            derived_families.append("unmapped")
        else:
            model = getattr(route, "model", None) or ""
            alias = model_aliases.get(model)
            family = (
                alias["model_family"]
                if isinstance(alias, dict) and "model_family" in alias
                else "unmapped"
            )
            derived_families.append(family)

    # Map benchmark rungs -> model_family
    benchmark_families: list[str] = []
    for rung in benchmark_rungs:
        worker = rung.get("worker", {})
        family = worker.get("model_family", "")
        benchmark_families.append(family)

    if derived_families == benchmark_families:
        return {
            "ladder_id": ladder_id,
            "role": role,
            "label": label,
            "agreement": True,
            "disagreements": [],
        }

    # Build disagreement details
    disagreements: list[dict[str, Any]] = []
    max_len = max(len(derived_families), len(benchmark_families))
    for i in range(max_len):
        if i >= len(derived_families):
            disagreements.append(
                {
                    "position": i,
                    "message": (
                        f"benchmark has model_family="
                        f"{benchmark_families[i]} at position {i}, "
                        f"but derived ladder has no more rungs"
                    ),
                }
            )
        elif i >= len(benchmark_families):
            d_rung = derived_rungs[i]
            cost_val = d_rung.get("mean_cost_usd_marginal")
            cost_str = "unknown" if cost_val is None else str(cost_val)
            disagreements.append(
                {
                    "position": i,
                    "message": (
                        f"derived has {d_rung['route_id']} "
                        f"(family={derived_families[i]}, "
                        f"n={d_rung['n']}, pass_rate={d_rung['pass_rate']}, "
                        f"cost={cost_str}) "
                        f"at position {i}, "
                        f"but benchmark ladder has no more rungs"
                    ),
                }
            )
        elif derived_families[i] != benchmark_families[i]:
            d_rung = derived_rungs[i]
            cost_val = d_rung.get("mean_cost_usd_marginal")
            cost_str = "unknown" if cost_val is None else str(cost_val)
            disagreements.append(
                {
                    "position": i,
                    "message": (
                        f"derived has {d_rung['route_id']} "
                        f"(family={derived_families[i]}, "
                        f"n={d_rung['n']}, pass_rate={d_rung['pass_rate']}, "
                        f"cost={cost_str}) "
                        f"at position {i}; "
                        f"benchmark has model_family="
                        f"{benchmark_families[i]} at the same position"
                    ),
                }
            )

    return {
        "ladder_id": ladder_id,
        "role": role,
        "label": label,
        "agreement": False,
        "disagreements": disagreements,
    }


def diff_against_benchmark(
    derived: dict[str, Any],
    *,
    benchmark_path: str | Path | None = None,
    catalog_dir: str | Path | None = None,
    role_aliases_override: dict[str, str] | None = None,
    benchmark_data_override: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Diff derived ladders against benchmark escalation-ladders.json.

    Args:
        derived: The derived ladders dict.
        benchmark_path: Explicit benchmark path.
        catalog_dir: Catalog directory for route lookups.
        role_aliases_override: When provided, used instead of the benchmark
            file's ``role_aliases`` (for testing).
        benchmark_data_override: When provided, used instead of loading
            from the benchmark file (for testing).

    Returns:
        A list of diff entries, one per benchmark ladder with derived
        data for the corresponding role.
    """
    if benchmark_data_override is not None:
        benchmark = benchmark_data_override
    else:
        benchmark = _load_benchmark_ladders(benchmark_path)
    if benchmark is None:
        return [
            {
                "source": "benchmark",
                "error": "benchmark ladders file not found or unreadable",
            }
        ]

    model_aliases = benchmark.get("model_aliases", {})
    role_aliases = (
        role_aliases_override
        if role_aliases_override is not None
        else benchmark.get("role_aliases", {})
    )
    benchmark_ladders = benchmark.get("ladders", [])

    catalog_routes = _build_catalog_route_map(catalog_dir)

    # Build a lookup: class_key (derived by role) -> derived classes
    by_role: dict[str, list[dict[str, Any]]] = {}
    unmatched_roles: set[str] = set()
    for cls in derived.get("classes", []):
        ck = cls.get("class_key", "")
        parts = ck.split("/")
        router_role = parts[0] if len(parts) >= 1 else ck
        if router_role in ROUTER_ROLE_TO_BENCHMARK_ROLE:
            benchmark_role_name = ROUTER_ROLE_TO_BENCHMARK_ROLE[router_role]
            canonical_role = role_aliases.get(benchmark_role_name, benchmark_role_name)
            by_role.setdefault(canonical_role, []).append(cls)
        elif router_role in role_aliases:
            canonical_role = role_aliases[router_role]
            by_role.setdefault(canonical_role, []).append(cls)
        elif router_role in {ladder.get("role") for ladder in benchmark_ladders}:
            by_role.setdefault(router_role, []).append(cls)
        else:
            unmatched_roles.add(router_role)

    results: list[dict[str, Any]] = []
    for ladder in benchmark_ladders:
        role = ladder.get("role", "")
        ladder_id = ladder.get("ladder_id", "")
        label = ladder.get("label", "")
        rungs = ladder.get("rungs", [])

        # Find derived classes for this role
        derived_classes = by_role.get(role, [])
        derived_active = [
            cls for cls in derived_classes if cls.get("evidence_status") == "derived"
        ]

        if not derived_active:
            classes_considered = sorted(
                cls["class_key"] for cls in derived_classes if "class_key" in cls
            )
            excluded: list[dict[str, Any]] = []
            for cls in derived_classes:
                excluded.extend(cls.get("excluded_routes", []))
            entry: dict[str, Any] = {
                "source": "benchmark",
                "ladder_id": ladder_id,
                "role": role,
                "label": label,
                "agreement": None,
                "status": "insufficient evidence, hand ladder retained",
                "classes_considered": classes_considered,
                "minimum_sample_size": MINIMUM_SAMPLE_SIZE,
            }
            if excluded:
                entry["excluded_routes"] = excluded
            results.append(entry)
        else:
            for cls in derived_active:
                ck = cls.get("class_key", "")
                diff_entry = _diff_benchmark_ladder(
                    cls.get("rungs", []),
                    rungs,
                    catalog_routes,
                    model_aliases,
                    role_aliases,
                    role,
                    ladder_id,
                    label,
                )
                diff_entry["source"] = "benchmark"
                diff_entry["class_key"] = ck
                if cls.get("excluded_routes"):
                    diff_entry["excluded_routes"] = cls["excluded_routes"]
                results.append(diff_entry)

    if unmatched_roles:
        results.append(
            {
                "source": "benchmark",
                "unmatched_roles": sorted(unmatched_roles),
                "status": "no benchmark ladder for this role",
            }
        )

    return results


def diff_against_crews(
    derived: dict[str, Any],
    *,
    catalog_dir: str | Path | None = None,
    crews_ladders_override: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Diff derived ladders against crews.yaml ``escalation_ladder`` fields.

    Args:
        derived: The derived ladders dict.
        catalog_dir: Catalog directory for crew loading.
        crews_ladders_override: When provided, used instead of loading
            from the crews.yaml (for testing).

    Returns:
        A list of diff entries, one per crew with a non-empty
        ``escalation_ladder``.
    """
    if crews_ladders_override is not None:
        crew_ladders = crews_ladders_override
    else:
        crew_ladders = _load_crews_ladders(catalog_dir)

    if any("error" in entry for entry in crew_ladders):
        return [entry for entry in crew_ladders if "error" in entry]

    classes = derived.get("classes", [])
    classes_considered = sorted(
        cls["class_key"] for cls in classes if "class_key" in cls
    )

    # Build a lookup of derived rung order for each class_key with rungs
    derived_by_class: dict[str, list[str]] = {}
    for cls in classes:
        ck = cls.get("class_key", "")
        rungs = cls.get("rungs", [])
        if rungs:
            derived_by_class[ck] = [r["route_id"] for r in rungs]

    has_any_rungs = bool(derived_by_class)

    results: list[dict[str, Any]] = []
    if not has_any_rungs:
        excluded = [exc for cls in classes for exc in cls.get("excluded_routes", [])]
        for entry in crew_ladders:
            crew_id = entry.get("crew_id", "")
            crew_res: dict[str, Any] = {
                "source": "crews.yaml",
                "crew_id": crew_id,
                "agreement": None,
                "status": "insufficient evidence, hand ladder retained",
                "classes_considered": classes_considered,
                "minimum_sample_size": MINIMUM_SAMPLE_SIZE,
            }
            if excluded:
                crew_res["excluded_routes"] = excluded
            results.append(crew_res)
        return results

    for entry in crew_ladders:
        crew_id = entry.get("crew_id", "")
        ladder_ids = entry.get("escalation_ladder", [])

        # Check if the crew's ladder order matches any derived class order
        best_match = None
        best_match_class = None
        for ck, derived_ids in derived_by_class.items():
            overlap = [rid for rid in ladder_ids if rid in derived_ids]
            if overlap and (best_match is None or len(overlap) > len(best_match)):
                best_match = overlap
                best_match_class = ck

        if best_match is None:
            results.append(
                {
                    "source": "crews.yaml",
                    "crew_id": crew_id,
                    "agreement": None,
                    "message": (
                        f"crew {crew_id} escalation_ladder {ladder_ids} "
                        f"has no overlap with any derived class"
                    ),
                }
            )
        else:
            derived_order_ids = derived_by_class.get(best_match_class, [])
            crew_order_ids = ladder_ids

            derived_ordered = [rid for rid in derived_order_ids if rid in best_match]
            crew_ordered = [rid for rid in crew_order_ids if rid in best_match]

            matched_cls = next(
                (c for c in classes if c.get("class_key") == best_match_class), None
            )
            matched_excluded = (
                matched_cls.get("excluded_routes", []) if matched_cls else []
            )

            if derived_ordered == crew_ordered:
                agree_res: dict[str, Any] = {
                    "source": "crews.yaml",
                    "crew_id": crew_id,
                    "agreement": True,
                    "class_key": best_match_class,
                    "message": (
                        f"crew {crew_id} escalation_ladder order agrees "
                        f"with derived order for class {best_match_class}"
                    ),
                }
                if matched_excluded:
                    agree_res["excluded_routes"] = matched_excluded
                results.append(agree_res)
            else:
                disagreements: list[str] = []
                max_len = max(len(derived_ordered), len(crew_ordered))
                for i in range(max_len):
                    d_rid = derived_ordered[i] if i < len(derived_ordered) else None
                    c_rid = crew_ordered[i] if i < len(crew_ordered) else None
                    if d_rid != c_rid:
                        disagreements.append(
                            f"position {i}: derived={d_rid}, crew={c_rid}"
                        )

                disagree_res: dict[str, Any] = {
                    "source": "crews.yaml",
                    "crew_id": crew_id,
                    "agreement": False,
                    "class_key": best_match_class,
                    "disagreements": disagreements,
                    "message": (
                        f"crew {crew_id} escalation_ladder order "
                        f"disagrees with derived order for class "
                        f"{best_match_class}"
                    ),
                }
                if matched_excluded:
                    disagree_res["excluded_routes"] = matched_excluded
                results.append(disagree_res)

    return results


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def default_output_dir() -> Path:
    """Return the default output directory for derived-ladder files."""
    return Path(__file__).resolve().parents[3] / "config" / "staffing"


def write_derived_ladders(
    derived: dict[str, Any],
    output_dir: str | Path | None = None,
) -> Path:
    """Write the derived-ladder JSON to a dated additive file.

    Args:
        derived: The derived ladders dict.
        output_dir: Output directory (default: ``config/staffing``).

    Returns:
        The path to the written file.
    """
    from lee_llm_router.staffing.json_int import dump_json

    if output_dir is None:
        output_dir = default_output_dir()
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    today = date.today()
    filename = f"derived-ladders-{today.isoformat()}.json"
    dest = out_path / filename

    dest.write_text(
        dump_json(derived, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return dest


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_ladder_diff(
    benchmark_diffs: list[dict[str, Any]],
    crews_diffs: list[dict[str, Any]],
) -> str:
    """Render the diff results as readable text.

    Args:
        benchmark_diffs: Results from ``diff_against_benchmark``.
        crews_diffs: Results from ``diff_against_crews``.

    Returns:
        A human-readable string describing agreements and disagreements.
    """
    lines: list[str] = []

    lines.append("Diff against benchmark escalation-ladders.json:")
    lines.append("")
    for entry in benchmark_diffs:
        if "error" in entry:
            lines.append(f"  ERROR: {entry['error']}")
            lines.append("")
            continue
        if "unmatched_roles" in entry:
            roles_str = ", ".join(entry["unmatched_roles"])
            status = entry.get("status", "no benchmark ladder for this role")
            lines.append(f"  – unmatched roles ({roles_str}): {status}")
            lines.append("")
            continue
        lid = entry.get("ladder_id", "")
        role = entry.get("role", "")
        agreement = entry.get("agreement")
        class_key = entry.get("class_key")
        target = (
            f"{lid} ({role}) vs class {class_key}" if class_key else f"{lid} ({role})"
        )
        if agreement is True:
            lines.append(f"  ✓ {target}: agrees")
        elif agreement is False:
            lines.append(f"  ✗ {target}: DISAGREES")
            for d in entry.get("disagreements", []):
                lines.append(f"    - {d.get('message', '')}")
        elif agreement is None:
            if entry.get("status") == "insufficient evidence, hand ladder retained":
                classes_considered = entry.get("classes_considered", [])
                k = len(classes_considered)
                n_min = entry.get("minimum_sample_size", MINIMUM_SAMPLE_SIZE)
                lines.append(
                    f"  – {lid} ({role}): insufficient evidence, hand ladder retained "
                    f"({k} classes observed, none at n>={n_min})"
                )
            else:
                lines.append(f"  ? {target}: unknown")
        for exc in entry.get("excluded_routes", []):
            lines.append(
                f"    excluded: {exc['route_id']} "
                f"(n={exc['n']}, verified_pass={exc['verified_pass']})"
            )
        lines.append("")

    lines.append("Diff against crews.yaml escalation_ladder fields:")
    lines.append("")
    for entry in crews_diffs:
        if "error" in entry:
            lines.append(f"  ERROR: {entry['error']}")
            lines.append("")
            continue
        crew_id = entry.get("crew_id", "")
        agreement = entry.get("agreement")
        msg = entry.get("message", "")
        if agreement is True:
            lines.append(f"  ✓ crew {crew_id}: agrees")
            lines.append(f"    {msg}")
        elif agreement is False:
            lines.append(f"  ✗ crew {crew_id}: DISAGREES")
            lines.append(f"    {msg}")
            for d in entry.get("disagreements", []):
                lines.append(f"    - {d}")
        elif agreement is None:
            if entry.get("status") == "insufficient evidence, hand ladder retained":
                classes_considered = entry.get("classes_considered", [])
                k = len(classes_considered)
                n_min = entry.get("minimum_sample_size", MINIMUM_SAMPLE_SIZE)
                lines.append(
                    f"  – crew {crew_id}: insufficient evidence, hand ladder retained "
                    f"({k} classes observed, none at n>={n_min})"
                )
            else:
                lines.append(f"  ? crew {crew_id}: {msg}")
        for exc in entry.get("excluded_routes", []):
            lines.append(
                f"    excluded: {exc['route_id']} "
                f"(n={exc['n']}, verified_pass={exc['verified_pass']})"
            )
        lines.append("")

    return "\n".join(lines)
