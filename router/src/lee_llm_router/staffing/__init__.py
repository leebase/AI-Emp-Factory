"""Staffing catalog loader: typed, schema-validated catalog documents.

Shape-only boundary for the six staffing YAML documents (routes,
channels, terms, policy, classes, crews). No selection, ranking, or
pricing decision logic lives here. Class metadata never maps to a
preferred model or route (D206); it only joins evidence or gates an
unevidenced cheap trial.

P0-4b adds the dated-terms/marginal-price API (``staffing.terms``):
``terms_at`` dated terms lookup, ``badge_multiplier`` (unknown badges fail
closed to 1.0), ``replacement_token_prices`` over the sha256-verified pinned
OpenRouter snapshot with agent-orch rate-table fallback (D207 Zen/Go proxy
rows), and ``route_price`` marginal = replacement × multiplier.

P0-5a adds the pure eligibility evaluation (``staffing.eligibility``):
``evaluate_eligibility`` returns one deterministic row per catalog route
(route status, harness lock, never-automatic, D188 role scoping,
subscription headroom veto, dated terms, badge-priced marginal) in catalog
order. Class metadata never maps to a preferred model or route (D206); no
probability, ladder, model choice, or ranking lives here.
"""

from __future__ import annotations

from lee_llm_router.staffing.catalog import (
    DOCUMENT_ORDER,
    BadgeMultipliers,
    CanonicalEncoding,
    Channel,
    ChannelsCatalog,
    CheapTrialRule,
    ClassesCatalog,
    Crew,
    CrewsCatalog,
    DenyPredicate,
    FeeEntry,
    NeverAutomaticRule,
    PolicyCatalog,
    ReserveFractionOverride,
    ReserveFractionPolicy,
    ReviewIndependenceRule,
    RoleClassEntry,
    RoleFloorRecord,
    RoleScopedRule,
    Route,
    RoutesCatalog,
    SpendCap,
    StaffingCatalog,
    StaffingCatalogError,
    TermsCatalog,
    TermsEntry,
    ValueSets,
    canonical_class_key,
    load_staffing_catalog,
    load_staffing_document,
    validate_class_block,
)
from lee_llm_router.staffing.eligibility import (
    EligibilityPrice,
    EligibilityRow,
    StaffingEligibilityError,
    evaluate_eligibility,
)
from lee_llm_router.staffing.terms import (
    DEFAULT_OPENROUTER_SNAPSHOT_PATH,
    DEFAULT_RATE_TABLE_PATH,
    UNKNOWN_BADGE_MULTIPLIER,
    ReplacementPrice,
    RoutePrice,
    StaffingTermsError,
    badge_multiplier,
    load_openrouter_snapshot,
    load_rate_table,
    replacement_token_prices,
    route_price,
    terms_at,
)

__all__ = [
    "DOCUMENT_ORDER",
    "BadgeMultipliers",
    "CanonicalEncoding",
    "Channel",
    "ChannelsCatalog",
    "CheapTrialRule",
    "ClassesCatalog",
    "Crew",
    "CrewsCatalog",
    "DenyPredicate",
    "FeeEntry",
    "NeverAutomaticRule",
    "PolicyCatalog",
    "ReserveFractionOverride",
    "ReserveFractionPolicy",
    "ReviewIndependenceRule",
    "RoleClassEntry",
    "RoleFloorRecord",
    "RoleScopedRule",
    "Route",
    "RoutesCatalog",
    "SpendCap",
    "StaffingCatalog",
    "StaffingCatalogError",
    "TermsCatalog",
    "TermsEntry",
    "ValueSets",
    "canonical_class_key",
    "load_staffing_catalog",
    "load_staffing_document",
    "validate_class_block",
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
    "EligibilityPrice",
    "EligibilityRow",
    "StaffingEligibilityError",
    "evaluate_eligibility",
]
