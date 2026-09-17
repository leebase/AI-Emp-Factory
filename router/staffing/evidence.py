"""Pure D211 route/class evidence joins over validated staffing evidence.

This module implements D211 rulings 1 and 2 of
``docs/staffing/phase2-contracts.md`` as pure functions over already
validated evidence.  It is a consumer, not a producer: it reads the exact
placements committed by the attempt-record v2 schema and by the P2-0
``evidence rollup`` output and never rewrites, derives, or duplicates them.

The evidence join (ruling 1) is scoped to the caller-supplied exact
``(route_id, class_key)`` pair.  It filters the observed route first, then
walks the fixed exact-to-coarse class-key order: the exact
``class_record.class_key`` first, then the key with ``language`` dropped,
then with ``size_band`` also dropped, then ``role/oracle_type`` only, and
finally ``none``.  The first level at which at least one attempt matches is
the used level, exposed both as the result's ``level`` field and as the
:class:`EvidenceJoin` ``evidence_level`` attribute.  ``n`` is the joined
attempts, ``k`` their verified passes (the ``verified_success`` equivalence
the rollup counts as ``verified_pass``), ``prior_n`` counts the benchmark
rows (``record_kind == "benchmark_run"``), and ``posterior_n`` counts the
production rows (``record_kind == "router_run"``).  The Laplace estimate is
exactly ``(k + 1) / (n + 2)`` and ``low_evidence`` is exactly ``n < 5``
(:data:`lee_llm_router.staffing.rollup.MINIMUM_SAMPLE_SIZE`).  A record
whose ``record_kind`` is neither value counts in ``n`` and ``k`` but in
neither split — the contract partitions prior/posterior by those two exact
kinds and invents no third bucket.  The summary returned by
:meth:`EvidenceJoin.as_dict` carries exactly the seven contract keys, so the
estimate is never shown without ``k``/``n`` and the level.

The class key is an evidence key only (D205/D206): it joins comparable
evidence and gates cheap trials.  The caller supplies route scope explicitly;
this module never maps a class to a model or route and never guesses a route
identifier.

Demand (ruling 2) is computed over the *effective comparable attempt
population* at the used join level: the same supersession-filtered,
route-scoped, level-matched rows that produce ``n`` and ``k``.  Each token
component and the wall-clock median is the exact median of that population's
observed counters — not a median of subgroup medians, which would misstate
unequal populations.  Supersession and dedup semantics are the rollup's own
(``_without_superseded_benchmark_lines``): exactly one preferred benchmark
record represents each source run, so legacy and duplicate raw-v6 lines never
both contribute.
Unavailable values stay unavailable: a component with no observed counter is
the explicit string ``"unknown"``, and when no median is usable at all the
whole demand is the literal string ``"unknown"`` and no expected-cost figure
exists.  At the exact level this population median is identical to the
accepted P2-0 rollup group's median (same rows, same aggregation); at coarser
levels it is the truthful population median the rollup's per-class groups
cannot express.  The ``rollup`` argument is still accepted for caller
compatibility but is no longer consulted: the joined attempt population is
the authoritative demand source, and a caller-supplied rollup can never
disagree with it.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from lee_llm_router.staffing.rollup import (
    MINIMUM_SAMPLE_SIZE,
    TOKEN_FIELDS,
    _counter,
    _exact_median,
    _route_id,
    _token_values,
    _without_superseded_benchmark_lines,
)

__all__ = [
    "DEMAND_UNKNOWN",
    "EVIDENCE_LEVELS",
    "EVIDENCE_LEVEL_DROP_LANGUAGE",
    "EVIDENCE_LEVEL_DROP_SIZE_BAND",
    "EVIDENCE_LEVEL_EXACT",
    "EVIDENCE_LEVEL_NONE",
    "EVIDENCE_LEVEL_ROLE_ORACLE_TYPE",
    "EvidenceJoin",
    "join_evidence",
]

DEMAND_UNKNOWN = "unknown"
"""The explicit demand value when no median is usable (D211 ruling 2)."""

EVIDENCE_LEVEL_EXACT = "exact"
"""The join level of the exact ``class_record.class_key``."""

EVIDENCE_LEVEL_DROP_LANGUAGE = "drop_language"
"""The join level after dropping the trailing ``language`` component."""

EVIDENCE_LEVEL_DROP_SIZE_BAND = "drop_size_band"
"""The join level after additionally dropping the ``size_band`` component."""

EVIDENCE_LEVEL_ROLE_ORACLE_TYPE = "role_oracle_type"
"""The join level reduced to the ``role/oracle_type`` prefix."""

EVIDENCE_LEVEL_NONE = "none"
"""The join level with no comparable evidence at all."""

EVIDENCE_LEVELS: tuple[str, ...] = (
    EVIDENCE_LEVEL_EXACT,
    EVIDENCE_LEVEL_DROP_LANGUAGE,
    EVIDENCE_LEVEL_DROP_SIZE_BAND,
    EVIDENCE_LEVEL_ROLE_ORACLE_TYPE,
    EVIDENCE_LEVEL_NONE,
)
"""The fixed exact-to-coarse join order of D211 ruling 1."""

SUMMARY_KEYS: tuple[str, ...] = (
    "level",
    "n",
    "k",
    "estimate",
    "low_evidence",
    "prior_n",
    "posterior_n",
)
"""The exact key set and order of the D211 ruling 1 evidence-join summary."""

BENCHMARK_RECORD_KIND = "benchmark_run"
"""The attempt-record kind counted in ``prior_n`` (D211 ruling 1)."""

ROUTER_RECORD_KIND = "router_run"
"""The attempt-record kind counted in ``posterior_n`` (D211 ruling 1)."""

#: Widths of the coarser join levels against the locked five-component
#: class-key grammar ``role/oracle_type/surface/size_band/language``.
_DROP_LANGUAGE_WIDTH = 4
_DROP_SIZE_BAND_WIDTH = 3
_ROLE_ORACLE_TYPE_WIDTH = 2


def _string_or_none(value: object) -> str | None:
    """Return a non-empty string, or the explicit unavailable value ``None``."""
    return value if isinstance(value, str) and value else None


def _record_class_key(record: Mapping[str, Any]) -> str | None:
    """Read the canonical class key at its committed placement.

    The v2 schema keeps the class key in ``class_record.class_key``; the
    plain ``class_key`` lookup is a compatibility convenience for callers
    passing already-normalised mappings, mirroring the rollup reader.
    """
    class_record = record.get("class_record")
    if isinstance(class_record, Mapping):
        class_key = _string_or_none(class_record.get("class_key"))
        if class_key is not None:
            return class_key
    return _string_or_none(record.get("class_key"))


def _record_kind(record: Mapping[str, Any]) -> str | None:
    """Read the attempt-record kind at its committed v2 placement."""
    return _string_or_none(record.get("record_kind"))


def _target_route_id(route_id: object) -> str | None:
    """Validate the exact route scope, retaining explicit missing-route scope."""
    if route_id is None:
        return None
    if isinstance(route_id, str) and route_id:
        return route_id
    raise ValueError(
        "route_id must be a non-empty string or None; the join never guesses "
        "or rewrites a route"
    )


def _target_components(class_key: object) -> list[str]:
    """Split the target class key, refusing unavailable or empty keys."""
    key = _string_or_none(class_key)
    if key is None:
        raise ValueError(
            "class_key must be a non-empty string; the join never guesses a key"
        )
    return key.split("/")


def _level_widths(target: list[str]) -> list[tuple[str, int]]:
    """Return the offered join levels with their match widths, in order.

    The exact level always matches the whole target.  A coarser level is
    offered only when the target actually carries the component it drops —
    a key without a ``language`` component cannot drop one, so ``exact`` is
    then already its coarsest comparable level before ``none``.  The locked
    grammar is five components, for which the widths are 5, 4, 3, 2.
    """
    widths: list[tuple[str, int]] = [(EVIDENCE_LEVEL_EXACT, len(target))]
    if len(target) > _DROP_LANGUAGE_WIDTH:
        widths.append((EVIDENCE_LEVEL_DROP_LANGUAGE, _DROP_LANGUAGE_WIDTH))
    if len(target) > _DROP_SIZE_BAND_WIDTH:
        widths.append((EVIDENCE_LEVEL_DROP_SIZE_BAND, _DROP_SIZE_BAND_WIDTH))
    if len(target) > _ROLE_ORACLE_TYPE_WIDTH:
        widths.append((EVIDENCE_LEVEL_ROLE_ORACLE_TYPE, _ROLE_ORACLE_TYPE_WIDTH))
    return widths


def _matches_at_width(
    record_key: str, target: list[str], width: int, *, exact: bool
) -> bool:
    """Whether one record class key matches the target at a join level.

    The exact level requires full component equality.  A coarse level joins
    every record sharing the target's leading ``width`` components, which is
    precisely the key with the dropped components removed.
    """
    components = record_key.split("/")
    if len(components) < width:
        return False
    if exact:
        return components == target
    return components[:width] == target[:width]


def _demand_at_level(
    joined: list[Mapping[str, Any]],
) -> dict[str, Any] | str:
    """Build the D211 ruling 2 demand over the joined attempt population.

    Each token component and the wall-clock median are the exact medians of
    the joined records' own observed counters at the used join level — the
    same supersession-filtered, route-scoped rows that produce ``n`` and
    ``k``.  A component with no observed counter is the explicit string
    ``"unknown"``; when no median at all is usable the whole demand is the
    literal string ``"unknown"`` and no expected-cost figure exists.
    """
    tokens: dict[str, int | float | str | None] = {}
    for field in TOKEN_FIELDS:
        values = _token_values(joined, field)
        tokens[field] = _exact_median(values) if values else None
    wall_values = [
        value
        for value in (_counter(record.get("wall_clock_ms")) for record in joined)
        if value is not None
    ]
    wall_clock_ms = _exact_median(wall_values) if wall_values else None

    if all(value is None for value in (*tokens.values(), wall_clock_ms)):
        return DEMAND_UNKNOWN
    return {
        "tokens": {
            field: (DEMAND_UNKNOWN if tokens[field] is None else tokens[field])
            for field in TOKEN_FIELDS
        },
        "wall_clock_ms": (DEMAND_UNKNOWN if wall_clock_ms is None else wall_clock_ms),
    }


class EvidenceJoin:
    """The D211 ruling 1 evidence join for one exact route/class pair.

    Attributes:
        n: Joined attempts at the used level.
        k: Verified passes (``verified_success is True``) among them.
        prior_n: Joined ``benchmark_run`` rows (the benchmark prior).
        posterior_n: Joined ``router_run`` rows (the production posterior).

    The join object never maps the class to a model or route and carries
    no selection, ranking, or expected-cost figure (D205/D206).
    """

    def __init__(
        self,
        *,
        level: str,
        n: int,
        k: int,
        prior_n: int,
        posterior_n: int,
        demand: dict[str, Any] | str,
    ) -> None:
        self._level = level
        self.n = n
        self.k = k
        self.prior_n = prior_n
        self.posterior_n = posterior_n
        self._demand = demand

    @property
    def evidence_level(self) -> str:
        """The used join level, exposed under its D211 ruling 1 name."""
        return self._level

    @property
    def level(self) -> str:
        """The used join level, under the summary's ``level`` key name."""
        return self._level

    @property
    def estimate(self) -> float:
        """The Laplace estimate ``(k + 1) / (n + 2)`` (D211 ruling 1)."""
        return (self.k + 1) / (self.n + 2)

    @property
    def low_evidence(self) -> bool:
        """Exactly ``n < 5`` (D211 ruling 1)."""
        return self.n < MINIMUM_SAMPLE_SIZE

    @property
    def demand(self) -> dict[str, Any] | str:
        """Demand medians at the used join level (D211 ruling 2).

        Either the literal string ``"unknown"`` when no median is usable,
        or a mapping with one explicit entry per rollup token component and
        the ``wall_clock_ms`` median, each unavailable field the string
        ``"unknown"``.
        """
        return self._demand

    def as_dict(self) -> dict[str, Any]:
        """Return exactly the seven D211 ruling 1 summary keys.

        Returns:
            A fresh dict with exactly ``level``, ``n``, ``k``, ``estimate``,
            ``low_evidence``, ``prior_n``, and ``posterior_n``, in the
            contract's order.  The estimate is never shown without ``k``,
            ``n``, and the level.
        """
        return {
            "level": self._level,
            "n": self.n,
            "k": self.k,
            "estimate": self.estimate,
            "low_evidence": self.low_evidence,
            "prior_n": self.prior_n,
            "posterior_n": self.posterior_n,
        }

    def __repr__(self) -> str:
        return (
            f"EvidenceJoin(level={self._level!r}, n={self.n}, k={self.k}, "
            f"prior_n={self.prior_n}, posterior_n={self.posterior_n})"
        )


def join_evidence(
    route_id: object,
    class_key: object,
    records: Iterable[Mapping[str, Any]],
    rollup: object = None,
) -> EvidenceJoin:
    """Join comparable evidence for one exact route/class pair.

    Args:
        route_id: The exact route scope.  A non-empty string is compared
            byte-for-byte; ``None`` explicitly selects records whose route
            is unavailable.  No route alias is inferred.
        class_key: The target ``class_record.class_key``.  Must be a
            non-empty string; the join never guesses or rewrites a key.
        records: Attempt records already validated by
            :func:`lee_llm_router.staffing.ledger.read_attempts` (or
            equivalent); they are consumed truthfully after applying the
            accepted rollup supersession view.
        rollup: Accepted but no longer consulted for demand; retained so
            existing callers (which pass
            :func:`lee_llm_router.staffing.rollup.build_rollup` output over
            the same records) stay source-compatible.  The demand medians
            are computed over the joined attempt population itself, which is
            the supersession-filtered population the rollup aggregates; at
            the exact join level the two agree exactly, and at coarse levels
            the population median is the truthful value a pool of per-class
            group medians cannot express.

    Returns:
        An :class:`EvidenceJoin` whose :meth:`~EvidenceJoin.as_dict` carries
        exactly ``{level, n, k, estimate, low_evidence, prior_n,
        posterior_n}`` and whose ``evidence_level`` is the used level.  The
        used level is the first level in the fixed exact-to-coarse order at
        which at least one record matches; ``none`` (with ``n = 0`` and the
        bare Laplace prior ``0.5``) when nothing matches.  A record whose
        ``record_kind`` is neither ``benchmark_run`` nor ``router_run``
        counts in ``n`` and ``k`` but in neither split.

    Raises:
        ValueError: If ``route_id`` is neither ``None`` nor a non-empty
            string, or if ``class_key`` is not a non-empty string.
    """
    target_route_id = _target_route_id(route_id)
    target = _target_components(class_key)
    levels = _level_widths(target)
    raw_records = [record for record in records if isinstance(record, Mapping)]
    # Reuse the accepted Phase-1/rollup supersession view.  This must happen
    # before route/class filtering so n, k, prior_n, and posterior_n describe
    # the same effective rows as build_rollup, even when the correction has a
    # different (or unavailable) route shape from its legacy source row.
    materialized = _without_superseded_benchmark_lines(raw_records)
    route_records = [
        record for record in materialized if _route_id(record) == target_route_id
    ]

    joined: list[Mapping[str, Any]] = []
    used = EVIDENCE_LEVEL_NONE
    for level, width in levels:
        joined = [
            record
            for record in route_records
            if (key := _record_class_key(record)) is not None
            and _matches_at_width(
                key, target, width, exact=level == EVIDENCE_LEVEL_EXACT
            )
        ]
        if joined:
            used = level
            break

    prior_n = sum(
        1 for record in joined if _record_kind(record) == BENCHMARK_RECORD_KIND
    )
    posterior_n = sum(
        1 for record in joined if _record_kind(record) == ROUTER_RECORD_KIND
    )
    if used == EVIDENCE_LEVEL_NONE:
        # No comparable evidence exists at any level, so there is no attempt
        # population to aggregate: the demand is the explicit unknown, never
        # a guess.
        demand: dict[str, Any] | str = DEMAND_UNKNOWN
    else:
        # The demand population is exactly the joined rows at the used join
        # level.  The caller-supplied rollup is accepted but not consulted:
        # the joined attempts are the authoritative, supersession-filtered
        # population, and a median over it is the truthful demand at every
        # level (identical to the rollup group's median at the exact level).
        demand = _demand_at_level(joined)
    return EvidenceJoin(
        level=used,
        n=len(joined),
        k=sum(1 for record in joined if record.get("verified_success") is True),
        prior_n=prior_n,
        posterior_n=posterior_n,
        demand=demand,
    )
