"""Dated staffing terms and marginal per-token prices (P0-4b).

A small typed lookup API over the committed staffing catalog and the
pinned pricing sources named in ``config/staffing/terms.yaml``:

* :func:`terms_at` selects, per channel, the latest ``TermsEntry`` whose
  ``effective_from`` is on or before a given date. Deterministic: channel
  order follows the channels document, and a duplicate
  ``(channel, effective_from)`` pair fails closed as ambiguous.
* :func:`badge_multiplier` maps a badge string to the configured
  multiplier. Any badge outside the six configured keys returns
  ``UNKNOWN_BADGE_MULTIPLIER`` (1.0) — unknown badges fail closed to full
  replacement price, per phase0-contracts.md §Terms and prices.
* :func:`replacement_token_prices` resolves a model id's replacement
  input/output per-token price from the pinned OpenRouter snapshot
  (``openrouter-20260911.json``, verified against its adjacent
  ``.sha256`` sidecar before use) for OpenRouter-listed exact ids, and
  from the agent-orch ``rate_table.yaml`` where the snapshot has no row —
  including the D207 Zen/Go accounting-proxy rows keyed
  ``opencode-go/<id>``. Every per-token figure is copied verbatim from a
  source file; no price is invented.
* :func:`route_price` computes a route's marginal input/output price as
  replacement price × badge multiplier while also exposing the
  replacement price and the multiplier. Reporting/list price remains the
  replacement price — this module never folds the multiplier into the
  replacement figure.

No selection, ranking, probability, or provider calls live here. Every
unresolved lookup (no dated terms, tampered or missing snapshot bytes,
missing price row, unpriced snapshot row) raises
:class:`StaffingTermsError` and fails closed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
from typing import Mapping

import yaml

from lee_llm_router.providers.base import FailureType, LLMRouterError
from lee_llm_router.staffing.catalog import (
    Route,
    StaffingCatalog,
    TermsCatalog,
    TermsEntry,
)

__all__ = [
    "DEFAULT_OPENROUTER_SNAPSHOT_PATH",
    "DEFAULT_RATE_TABLE_PATH",
    "ReplacementPrice",
    "RoutePrice",
    "StaffingTermsError",
    "UNKNOWN_BADGE_MULTIPLIER",
    "badge_multiplier",
    "load_openrouter_snapshot",
    "load_rate_table",
    "replacement_token_prices",
    "route_price",
    "terms_at",
]

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: Pinned OpenRouter catalog snapshot (terms.yaml decision_price_ref series).
DEFAULT_OPENROUTER_SNAPSHOT_PATH = (
    _REPO_ROOT / "config" / "staffing" / "pricing" / "openrouter-20260911.json"
)

#: Environment override for the rate-table location. The fallback table is
#: versioned reference data — published list prices with an as-of date — so a
#: distribution can ship it; but the default below is an absolute path into a
#: sibling development checkout, which does not exist on any other machine.
#: Without this override an exported tree prices nothing through the fallback
#: and every route that OpenRouter does not list is excluded as "pricing
#: unavailable". Found on a foreign host, where it silently vetoed every
#: Anthropic subscription route.
RATE_TABLE_PATH_ENV_VAR = "LEE_LLM_ROUTER_RATE_TABLE"

#: agent-orch rate table — the P0-4 fallback where OpenRouter has no row (D207).
#: Unset environment keeps the historical path, so estate behaviour is unchanged.
DEFAULT_RATE_TABLE_PATH = Path(
    os.environ.get(RATE_TABLE_PATH_ENV_VAR)
    or "/home/lee/projects/agent-orch/src/agent_orch/rate_table.yaml"
)

#: Multiplier for any badge not configured in terms.yaml badge_multipliers.
UNKNOWN_BADGE_MULTIPLIER = 1.0

# terms.yaml badge_multipliers keys -> BadgeMultipliers dataclass fields.
_BADGE_FIELDS: dict[str, str] = {
    "COLD": "COLD",
    "USE IT": "USE_IT",
    "ON TRACK": "ON_TRACK",
    "HOT": "HOT",
    "TOO FAST": "TOO_FAST",
    "NO DATA": "NO_DATA",
}

_TOKENS_PER_1M = 1_000_000


class StaffingTermsError(LLMRouterError):
    """Raised when dated-terms or price resolution fails; always fail-closed."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(
            message, failure_type=FailureType.CONTRACT_VIOLATION, cause=cause
        )


# ---------------------------------------------------------------------------
# Typed results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplacementPrice:
    """Replacement (list/reporting) input and output per-token price.

    ``source`` names the exact resolution, e.g.
    ``openrouter-snapshot:z-ai/glm-5.3-flash`` or
    ``rate-table:opencode-go/qwen3.7-plus`` (the D207 Zen/Go accounting
    proxy rows live under the channel-qualified key).
    """

    model: str
    channel: str
    input_usd_per_token: float
    output_usd_per_token: float
    source: str


@dataclass(frozen=True)
class RoutePrice:
    """Marginal price for one route under one badge.

    ``marginal_*`` equals ``replacement_* × multiplier``. The replacement
    (list/reporting) price is exposed unchanged; the multiplier is never
    folded into it.
    """

    route_id: str
    model: str
    channel: str
    badge: str
    multiplier: float
    replacement: ReplacementPrice
    marginal_input_usd_per_token: float
    marginal_output_usd_per_token: float


# ---------------------------------------------------------------------------
# terms_at — dated terms lookup
# ---------------------------------------------------------------------------


def _coerce_date(value: date | str, *, what: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise StaffingTermsError(
            f"{what}: not an ISO date (YYYY-MM-DD): {value!r}", cause=exc
        ) from exc


def badge_multiplier(badge: str, terms: TermsCatalog) -> float:
    """Return the configured multiplier for ``badge``.

    Exact badge strings are the six configured keys (``COLD``, ``USE IT``,
    ``ON TRACK``, ``HOT``, ``TOO FAST``, ``NO DATA``). Any other badge —
    including different casing — is unknown and returns 1.0 (full
    replacement price), per phase0-contracts.md §Terms and prices.
    """
    field = _BADGE_FIELDS.get(badge)
    if field is None:
        return UNKNOWN_BADGE_MULTIPLIER
    return float(getattr(terms.badge_multipliers, field))


def terms_at(
    date: date | str, catalog: StaffingCatalog | TermsCatalog
) -> Mapping[str, TermsEntry]:
    """Select the dated TermsEntry per channel, deterministically.

    For each channel the returned mapping carries the latest entry whose
    ``effective_from`` is on or before ``date``. Channel order follows
    the channels document when a full :class:`StaffingCatalog` is given,
    otherwise first-appearance order in the terms document. Raises
    :class:`StaffingTermsError` when some channel has no such entry, when
    a channel has two entries with the same ``effective_from``
    (ambiguous), or when ``date`` is not an ISO date.
    """
    when = _coerce_date(date, what="terms_at")
    if isinstance(catalog, StaffingCatalog):
        terms = catalog.terms
        channel_order = [c.channel_id for c in catalog.channels.channels]
    else:
        terms = catalog
        channel_order = []
    for entry in terms.terms:
        if entry.channel_ref not in channel_order:
            channel_order.append(entry.channel_ref)

    selected: dict[str, TermsEntry] = {}
    for channel in channel_order:
        candidates = [e for e in terms.terms if e.channel_ref == channel]
        effective = []
        for entry in candidates:
            effective.append(
                (_coerce_date(entry.effective_from, what=f"terms[{channel}]"), entry)
            )
        on_or_before = [pair for pair in effective if pair[0] <= when]
        if not on_or_before:
            raise StaffingTermsError(
                f"terms_at({when.isoformat()}): no TermsEntry for channel "
                f"'{channel}' with effective_from <= date"
            )
        dates = [d for d, _ in on_or_before]
        if len(set(dates)) != len(dates):
            raise StaffingTermsError(
                f"terms_at({when.isoformat()}): ambiguous TermsEntry for channel "
                f"'{channel}': multiple entries share an effective_from"
            )
        selected[channel] = max(on_or_before, key=lambda pair: pair[0])[1]
    return selected


# ---------------------------------------------------------------------------
# Pinned pricing sources
# ---------------------------------------------------------------------------


def _verify_sha256(path: Path) -> None:
    """Verify ``path`` bytes against the adjacent ``<name>.sha256`` sidecar."""
    if not path.is_file():
        raise StaffingTermsError(f"pinned pricing file not found: {path}")
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.is_file():
        raise StaffingTermsError(f"sha256 sidecar not found for {path}: {sidecar}")
    expected_token = sidecar.read_text(encoding="utf-8").split()
    if not expected_token:
        raise StaffingTermsError(f"sha256 sidecar is empty: {sidecar}")
    expected = expected_token[0].strip().lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise StaffingTermsError(f"sha256 sidecar is not a sha256 digest: {sidecar}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise StaffingTermsError(
            f"pinned snapshot bytes do not match sha256 sidecar for {path}: "
            f"expected {expected}, got {actual}"
        )


def _parse_per_token(value: object, *, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise StaffingTermsError(f"{where}: price is not a number: {value!r}")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise StaffingTermsError(
            f"{where}: price is not a decimal number: {value!r}", cause=exc
        ) from exc
    if not parsed.is_finite() or parsed < 0:
        raise StaffingTermsError(f"{where}: price is negative or unpriced: {value!r}")
    return float(parsed)


def load_openrouter_snapshot(
    path: str | Path | None = None,
) -> tuple[Mapping[str, tuple[float, float]], frozenset[str]]:
    """Load the pinned OpenRouter snapshot after sha256 verification.

    Returns ``(priced, listed)`` where ``priced`` maps each OpenRouter
    model id to its ``(input, output)`` per-token price (``pricing.prompt``
    and ``pricing.completion``, verbatim; rows with unusable values such as
    OpenRouter's unpriced ``"-1"`` marker stay listed but unpriced), and
    ``listed`` is every model id present in the snapshot whether priced or
    not. The snapshot bytes are
    verified against the adjacent ``.sha256`` sidecar before parsing.
    """
    snapshot_path = Path(path) if path is not None else DEFAULT_OPENROUTER_SNAPSHOT_PATH
    _verify_sha256(snapshot_path)
    try:
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StaffingTermsError(
            f"pinned OpenRouter snapshot is unreadable: {snapshot_path}: {exc}",
            cause=exc,
        ) from exc
    rows = data.get("data") if isinstance(data, Mapping) else None
    if not isinstance(rows, list):
        raise StaffingTermsError(
            f"pinned OpenRouter snapshot has no 'data' list: {snapshot_path}"
        )
    priced: dict[str, tuple[float, float]] = {}
    listed: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            continue
        model_id = row["id"]
        listed.add(model_id)
        pricing = row.get("pricing")
        if not isinstance(pricing, Mapping):
            continue  # listed but unpriced; fail closed only if looked up
        where = f"OpenRouter snapshot {snapshot_path.name} id '{model_id}'"
        try:
            priced[model_id] = (
                _parse_per_token(pricing.get("prompt"), where=f"{where} prompt"),
                _parse_per_token(
                    pricing.get("completion"), where=f"{where} completion"
                ),
            )
        except StaffingTermsError:
            continue  # unpriced row (e.g. "-1"); fail closed only if looked up
    return priced, frozenset(listed)


def load_rate_table(
    path: str | Path | None = None,
) -> Mapping[str, tuple[float, float]]:
    """Load the agent-orch rate table as per-token ``(input, output)`` prices.

    Each row's ``input_usd_per_1m`` / ``output_usd_per_1m`` is divided by
    1,000,000 verbatim — no value is inferred. Rows missing either field
    are recorded as listed but unpriced and fail closed only when looked
    up. Includes the D207 ``opencode-go/<id>`` Zen accounting-proxy rows.
    """
    table_path = Path(path) if path is not None else DEFAULT_RATE_TABLE_PATH
    try:
        data = yaml.safe_load(table_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise StaffingTermsError(
            f"agent-orch rate table is unreadable: {table_path}: {exc}", cause=exc
        ) from exc
    rates = data.get("rates") if isinstance(data, Mapping) else None
    if not isinstance(rates, Mapping):
        raise StaffingTermsError(
            f"agent-orch rate table has no 'rates' mapping: {table_path}"
        )
    prices: dict[str, tuple[float, float]] = {}
    for model_id, row in rates.items():
        if not isinstance(row, Mapping):
            continue
        if "input_usd_per_1m" not in row or "output_usd_per_1m" not in row:
            continue  # listed but unpriced; fail closed only if looked up
        where = f"rate table {table_path} id '{model_id}'"
        prices[model_id] = (
            _parse_per_token(row["input_usd_per_1m"], where=f"{where} input")
            / _TOKENS_PER_1M,
            _parse_per_token(row["output_usd_per_1m"], where=f"{where} output")
            / _TOKENS_PER_1M,
        )
    return prices


def replacement_token_prices(
    model: str,
    channel: str,
    *,
    openrouter_snapshot_path: str | Path | None = None,
    rate_table_path: str | Path | None = None,
) -> ReplacementPrice:
    """Resolve a route's replacement input/output per-token price.

    Resolution order, per the terms decision_price_ref series:

    1. The pinned OpenRouter snapshot, by exact id — used only when the
       route's model id is an OpenRouter-listed exact id. The snapshot
       bytes are verified against the adjacent ``.sha256`` sidecar before
       use.
    2. The agent-orch rate table, by exact model id, then by the
       channel-qualified id ``<channel>/<model>`` (the D207 Zen/Go
       accounting-proxy row convention, e.g. ``opencode-go/qwen3.7-plus``).

    Raises :class:`StaffingTermsError` when the snapshot or rate table is
    missing/tampered, when a listed row is unpriced, or when no row exists
    for the id in either source — never an invented price.
    """
    priced, listed = load_openrouter_snapshot(openrouter_snapshot_path)
    if model in priced:
        input_price, output_price = priced[model]
        return ReplacementPrice(
            model=model,
            channel=channel,
            input_usd_per_token=input_price,
            output_usd_per_token=output_price,
            source=f"openrouter-snapshot:{model}",
        )
    if model in listed:
        raise StaffingTermsError(
            f"OpenRouter snapshot lists '{model}' but its row is unpriced "
            f"(pricing.prompt/pricing.completion unusable)"
        )

    rates = load_rate_table(rate_table_path)
    for key in (model, f"{channel}/{model}"):
        if key in rates:
            input_price, output_price = rates[key]
            return ReplacementPrice(
                model=model,
                channel=channel,
                input_usd_per_token=input_price,
                output_usd_per_token=output_price,
                source=f"rate-table:{key}",
            )

    raise StaffingTermsError(
        f"no replacement price for model '{model}' on channel '{channel}': "
        f"not an OpenRouter-listed exact id and no exact or channel-qualified "
        f"row in the agent-orch rate table"
    )


def route_price(
    route_id: str,
    badge: str,
    catalog: StaffingCatalog,
    *,
    openrouter_snapshot_path: str | Path | None = None,
    rate_table_path: str | Path | None = None,
) -> RoutePrice:
    """Compute one route's marginal price under one badge.

    Looks up the :class:`Route` by ``route_id`` in the catalog, resolves
    the replacement per-token price via
    :func:`replacement_token_prices`, maps ``badge`` through the
    configured multipliers (unknown badge -> 1.0), and returns marginal =
    replacement × multiplier alongside the untouched replacement price
    and the multiplier. Reporting/list price remains the replacement
    price. Raises :class:`StaffingTermsError` for an unknown route or an
    unresolved/unpriced replacement price.
    """
    route: Route | None = None
    for candidate in catalog.routes.routes:
        if candidate.route_id == route_id:
            route = candidate
            break
    if route is None:
        raise StaffingTermsError(
            f"route '{route_id}' not found in the staffing routes catalog"
        )
    replacement = replacement_token_prices(
        route.model,
        route.channel,
        openrouter_snapshot_path=openrouter_snapshot_path,
        rate_table_path=rate_table_path,
    )
    multiplier = badge_multiplier(badge, catalog.terms)
    return RoutePrice(
        route_id=route.route_id,
        model=route.model,
        channel=route.channel,
        badge=badge,
        multiplier=multiplier,
        replacement=replacement,
        marginal_input_usd_per_token=replacement.input_usd_per_token * multiplier,
        marginal_output_usd_per_token=replacement.output_usd_per_token * multiplier,
    )
