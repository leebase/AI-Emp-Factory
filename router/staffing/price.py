"""Total list and marginal USD pricing for routes and token counts (P4-1)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import yaml

from lee_llm_router.availability import AvailabilitySnapshot
from lee_llm_router.providers.base import FailureType, LLMRouterError
from lee_llm_router.staffing.catalog import StaffingCatalog
from lee_llm_router.staffing.eligibility import _availability_badge, _dated_terms_entry
from lee_llm_router.staffing.terms import (
    DEFAULT_OPENROUTER_SNAPSHOT_PATH,
    DEFAULT_RATE_TABLE_PATH,
    RoutePrice,
    StaffingTermsError,
    route_price,
)

__all__ = ["PriceError", "PriceResult", "compute_price", "resolve_cache_read_rate"]


class PriceError(LLMRouterError):
    """Raised when price calculation fails (fail closed)."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(
            message, failure_type=FailureType.CONTRACT_VIOLATION, cause=cause
        )


@dataclass(frozen=True)
class PriceResult:
    """Computed total list and marginal USD price for a route and token counts."""

    route_id: str
    model: str
    channel: str
    badge: str
    multiplier: float
    source: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    list_usd: float
    marginal_usd: float
    replacement_input_usd_per_token: float
    replacement_output_usd_per_token: float
    marginal_input_usd_per_token: float
    marginal_output_usd_per_token: float
    cache_read_usd_per_token: float | None = None
    marginal_cache_read_usd_per_token: float | None = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "route_id": self.route_id,
            "model": self.model,
            "channel": self.channel,
            "badge": self.badge,
            "multiplier": self.multiplier,
            "source": self.source,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "list_usd": self.list_usd,
            "marginal_usd": self.marginal_usd,
            "replacement_input_usd_per_token": self.replacement_input_usd_per_token,
            "replacement_output_usd_per_token": self.replacement_output_usd_per_token,
            "marginal_input_usd_per_token": self.marginal_input_usd_per_token,
            "marginal_output_usd_per_token": self.marginal_output_usd_per_token,
        }
        if self.cache_read_usd_per_token is not None:
            d["cache_read_usd_per_token"] = self.cache_read_usd_per_token
            d["marginal_cache_read_usd_per_token"] = (
                self.marginal_cache_read_usd_per_token
            )
        return d

    def render_text(self) -> str:
        lines = [
            f"route:         {self.route_id}",
            f"model:         {self.model}",
            f"channel:       {self.channel}",
            f"badge:         {self.badge} (multiplier {self.multiplier})",
            f"source:        {self.source}",
            f"input_tokens:  {self.input_tokens}",
            f"output_tokens: {self.output_tokens}",
            f"cached_tokens: {self.cached_tokens}",
            f"list_usd:      {self.list_usd}",
            f"marginal_usd:  {self.marginal_usd}",
        ]
        return "\n".join(lines)


def resolve_cache_read_rate(
    rp: RoutePrice,
    *,
    openrouter_snapshot_path: str | Path | None = None,
    rate_table_path: str | Path | None = None,
) -> float | None:
    """Resolve per-token read-cache rate for route's resolved pricing source."""
    src = rp.replacement.source
    if src.startswith("openrouter-snapshot:"):
        model_id = src.split(":", 1)[1]
        p = Path(openrouter_snapshot_path or DEFAULT_OPENROUTER_SNAPSHOT_PATH)
        data = json.loads(p.read_text(encoding="utf-8"))
        for row in data.get("data", []):
            if isinstance(row, Mapping) and row.get("id") == model_id:
                val = row.get("pricing", {}).get("input_cache_read")
                if val and val != "-1":
                    return float(Decimal(str(val)))
                break
        return None
    if src.startswith("rate-table:"):
        key = src.split(":", 1)[1]
        p = Path(rate_table_path or DEFAULT_RATE_TABLE_PATH)
        rates = yaml.safe_load(p.read_text(encoding="utf-8")).get("rates", {})
        for k in (key, rp.model):
            row = rates.get(k, {})
            if (
                "cache_read_usd_per_1m" in row
                and row["cache_read_usd_per_1m"] is not None
            ):
                return float(Decimal(str(row["cache_read_usd_per_1m"]))) / 1_000_000
    return None


def compute_price(
    route_id: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    *,
    catalog: StaffingCatalog,
    availability: AvailabilitySnapshot,
    at_date: date | str | None = None,
    openrouter_snapshot_path: str | Path | None = None,
    rate_table_path: str | Path | None = None,
) -> PriceResult:
    """Compute list and marginal price for route under live badge and token counts."""
    for name, tok in (
        ("input", input_tokens),
        ("output", output_tokens),
        ("cached", cached_tokens),
    ):
        if isinstance(tok, bool) or not isinstance(tok, int) or tok < 0:
            raise PriceError(
                f"{name} tokens must be a non-negative integer, got {tok!r}"
            )

    if at_date is None:
        when = date.today()
    elif isinstance(at_date, date):
        when = at_date
    elif isinstance(at_date, str):
        try:
            when = date.fromisoformat(at_date)
        except ValueError as exc:
            raise PriceError(
                f"--at: not an ISO date (YYYY-MM-DD): {at_date!r}", cause=exc
            ) from exc
    else:
        raise PriceError(f"--at: invalid date type: {type(at_date)}")

    route = next((r for r in catalog.routes.routes if r.route_id == route_id), None)
    if route is None:
        raise PriceError(f"route '{route_id}' not found in the staffing routes catalog")

    if _dated_terms_entry(catalog.terms, route.channel, when) is None:
        raise PriceError(
            f"terms unavailable at {when.isoformat()} for channel '{route.channel}'"
        )

    badge = _availability_badge(availability.headroom(route.channel)) or "NO DATA"
    try:
        rp = route_price(
            route_id,
            badge,
            catalog,
            openrouter_snapshot_path=openrouter_snapshot_path,
            rate_table_path=rate_table_path,
        )
    except StaffingTermsError as exc:
        raise PriceError(str(exc), cause=exc) from exc

    cache_rate = resolve_cache_read_rate(
        rp,
        openrouter_snapshot_path=openrouter_snapshot_path,
        rate_table_path=rate_table_path,
    )
    if cached_tokens > 0 and cache_rate is None:
        raise PriceError(
            f"no read-cache price for model '{rp.model}' on channel '{rp.channel}'"
        )

    list_usd = (
        input_tokens * rp.replacement.input_usd_per_token
        + output_tokens * rp.replacement.output_usd_per_token
    )
    marginal_usd = (
        input_tokens * rp.marginal_input_usd_per_token
        + output_tokens * rp.marginal_output_usd_per_token
    )
    if cached_tokens > 0 and cache_rate is not None:
        list_usd += cached_tokens * cache_rate
        marginal_usd += cached_tokens * (cache_rate * rp.multiplier)

    marginal_cache_rate = cache_rate * rp.multiplier if cache_rate is not None else None

    return PriceResult(
        route_id=rp.route_id,
        model=rp.model,
        channel=rp.channel,
        badge=rp.badge,
        multiplier=rp.multiplier,
        source=rp.replacement.source,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        list_usd=list_usd,
        marginal_usd=marginal_usd,
        replacement_input_usd_per_token=rp.replacement.input_usd_per_token,
        replacement_output_usd_per_token=rp.replacement.output_usd_per_token,
        marginal_input_usd_per_token=rp.marginal_input_usd_per_token,
        marginal_output_usd_per_token=rp.marginal_output_usd_per_token,
        cache_read_usd_per_token=cache_rate,
        marginal_cache_read_usd_per_token=marginal_cache_rate,
    )
