"""Typed loader and validator for the six staffing catalog documents.

Loads an explicit directory of YAML documents (``routes.yaml``,
``channels.yaml``, ``terms.yaml``, ``policy.yaml``, ``classes.yaml``,
``crews.yaml``), validates each against its JSON Schema under
``config/staffing/schema`` (Draft 2020-12), and returns frozen typed
objects. This module owns shape validation and the canonical class-key
boundary only; it contains no route-selection, ranking, or pricing logic.

D206 invariant: class metadata never maps directly to a preferred model
or route. It may only join production attempts to comparable benchmark
evidence or gate an unevidenced cheap trial. The classes schema forbids
any class-to-model or class-to-route property, and nothing in this
module adds one.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import re
import shutil
import types
from dataclasses import MISSING, dataclass, fields
from pathlib import Path
from typing import (
    Any,
    Callable,
    Mapping,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from lee_llm_router.providers.base import FailureType, LLMRouterError

__all__ = [
    "BadgeMultipliers",
    "CanonicalEncoding",
    "Channel",
    "ChannelInstance",
    "ChannelsCatalog",
    "CheapTrialRule",
    "ClassesCatalog",
    "CrewOrderingRule",
    "Crew",
    "CrewsCatalog",
    "CrewSupervisor",
    "DEFAULT_STAGE_WORKER_DIR",
    "DenyPredicate",
    "FeeEntry",
    "HARNESS_BINARY_ENV_VAR_PREFIX",
    "HARNESS_BINARY_ENV_VAR_SUFFIX",
    "HARNESS_BINARY_TOKENS",
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
    "STAGE_WORKER_DIR_ENV_VAR",
    "STAGE_WORKER_DIR_TOKEN",
    "SpendCap",
    "StaffingCatalog",
    "StaffingCatalogError",
    "TermsCatalog",
    "TermsEntry",
    "ValueSets",
    "canonical_class_key",
    "harness_binary_env_var",
    "load_staffing_catalog",
    "load_staffing_document",
    "resolve_dispatch_template",
    "resolve_harness_binaries",
    "resolve_harness_binary",
    "resolve_stage_worker_dir",
    "validate_class_block",
]

# Order in which the six documents are loaded from the directory.
DOCUMENT_ORDER: tuple[str, ...] = (
    "routes",
    "channels",
    "terms",
    "policy",
    "classes",
    "crews",
)

_SCHEMA_FILES: dict[str, str] = {
    "routes": "routes.schema.json",
    "channels": "channels.schema.json",
    "terms": "terms.schema.json",
    "policy": "policy.schema.json",
    "classes": "classes.schema.json",
    "crews": "crews.schema.json",
}

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SCHEMA_DIR = _REPO_ROOT / "config" / "staffing" / "schema"

# ---------------------------------------------------------------------------
# Stage-worker directory resolution (P1-1)
# ---------------------------------------------------------------------------
# The committed ``routes.yaml`` dispatch templates name the five stage-worker
# scripts (antigravity, claude, codex, opencode, pi) through the single
# placeholder token :data:`STAGE_WORKER_DIR_TOKEN`:
# ``python3 @STAGE_WORKER_DIR@/<name>_stage_worker.py ...``. This module is the
# one resolution point that turns that token into a directory, at catalog load
# time. The scripts themselves are owned by auto-orch: nothing here reads,
# moves, executes, or validates them. A dispatch template is data.

#: Environment variable naming the directory that holds the stage-worker
#: scripts. When set and non-empty it wins over the default below.
STAGE_WORKER_DIR_ENV_VAR = "LEE_LLM_ROUTER_STAGE_WORKER_DIR"

#: Directory substituted when the environment variable is unset or empty.
#: It is the historical hard-coded dispatch path, so an unset environment
#: resolves every template byte-identically to the pre-P1-1 catalog.
DEFAULT_STAGE_WORKER_DIR = "/home/lee/projects/auto-orch/scripts"

#: Placeholder token carrying the stage-worker directory in ``routes.yaml``.
#: Deliberately not shell-shaped (no ``$`` or ``${...}``) so the token can
#: never be mistaken for, or read as, an environment expansion even though
#: the surrounding template is shell-shaped argv text.
STAGE_WORKER_DIR_TOKEN = "@STAGE_WORKER_DIR@"


def resolve_stage_worker_dir() -> str:
    """Return the directory substituted for :data:`STAGE_WORKER_DIR_TOKEN`.

    Resolution order, exactly:

    1. ``LEE_LLM_ROUTER_STAGE_WORKER_DIR`` when set and non-empty.
    2. Otherwise :data:`DEFAULT_STAGE_WORKER_DIR`, so a machine that sets
       nothing loads the same dispatch templates as before this change.

    The value is returned verbatim: no normalisation, expansion, or
    existence check. Dispatch templates are data, and the host that runs a
    stage worker need not be the host that loaded the catalog.
    """
    from_env = os.environ.get(STAGE_WORKER_DIR_ENV_VAR)
    if from_env:
        return from_env
    return DEFAULT_STAGE_WORKER_DIR


# ---------------------------------------------------------------------------
# Harness binary resolution (P1-2)
# ---------------------------------------------------------------------------
# Each committed dispatch template hands the stage worker its harness binary
# through that harness's ``*_STAGE_WORKER_BINARY`` environment variable, for
# example ``/usr/bin/env CODEX_STAGE_WORKER_BINARY=@CODEX_BINARY@ ...``. The
# five binaries used to be named by absolute paths correct only on Lee's
# machine; each is now one per-harness placeholder token from
# :data:`HARNESS_BINARY_TOKENS`, resolved here at catalog load time by
# :func:`resolve_harness_binary`. The binaries themselves are owned by their
# toolchains: nothing here reads, moves, executes, or validates them. A
# dispatch template is data.

#: Placeholder token -> bare binary name, one explicit row per harness. The
#: table is the single authority for which binary a token names, so a token
#: can never resolve to a different harness's binary.
HARNESS_BINARY_TOKENS: dict[str, str] = {
    "@CODEX_BINARY@": "codex",
    "@PI_BINARY@": "pi",
    "@OPENCODE_BINARY@": "opencode",
    "@CLAUDE_BINARY@": "claude",
    "@AGY_BINARY@": "agy",
}

#: Affixes of the environment variable that overrides a token's binary:
#: ``LEE_LLM_ROUTER_<BINARY NAME, UPPERCASED>_BINARY`` — for example
#: ``LEE_LLM_ROUTER_CODEX_BINARY`` or ``LEE_LLM_ROUTER_AGY_BINARY``.
HARNESS_BINARY_ENV_VAR_PREFIX = "LEE_LLM_ROUTER_"
HARNESS_BINARY_ENV_VAR_SUFFIX = "_BINARY"


def harness_binary_env_var(token: str) -> str:
    """Return the override environment variable name for a binary token.

    ``token`` is a key of :data:`HARNESS_BINARY_TOKENS`; an unknown token is
    a programming error, not a host condition.
    """
    binary = HARNESS_BINARY_TOKENS[token]
    return (
        f"{HARNESS_BINARY_ENV_VAR_PREFIX}{binary.upper()}"
        f"{HARNESS_BINARY_ENV_VAR_SUFFIX}"
    )


def resolve_harness_binary(token: str) -> str:
    """Return the binary substituted for harness-binary ``token``.

    Resolution order, exactly:

    1. The token's override variable (:func:`harness_binary_env_var`) when
       set and non-empty — the operator's explicit answer, so it wins.
    2. Otherwise ``shutil.which(<bare binary name>)``, the binary of that
       harness installed anywhere on the process PATH.
    3. Otherwise the bare binary name, deferring the lookup to the PATH of
       the process that finally executes the dispatch template.

    Only the token's own binary is ever considered: step 3 returns the bare
    name even when some other harness's binary is installed, so a token can
    never be silently satisfied by a different harness. Resolution never
    raises, so an uninstalled harness cannot fail catalog loading (and thus
    ``staff``) for unrelated routes. The value is returned verbatim: no
    normalisation, expansion, or existence check.
    """
    from_env = os.environ.get(harness_binary_env_var(token))
    if from_env:
        return from_env
    binary = HARNESS_BINARY_TOKENS[token]
    return shutil.which(binary) or binary


def resolve_harness_binaries() -> dict[str, str]:
    """Return the full token -> binary table resolved for this host.

    One resolution per token per catalog load; order follows
    :data:`HARNESS_BINARY_TOKENS`.
    """
    return {token: resolve_harness_binary(token) for token in HARNESS_BINARY_TOKENS}


def resolve_dispatch_template(
    template: str,
    stage_worker_dir: str | None = None,
    harness_binaries: Mapping[str, str] | None = None,
) -> str:
    """Return ``template`` with its placeholder tokens substituted.

    ``stage_worker_dir`` defaults to :func:`resolve_stage_worker_dir` and
    ``harness_binaries`` defaults to :func:`resolve_harness_binaries`. Both
    are explicit so a caller (or a test) with a known installation can
    render a template without consulting the process environment. A
    harness-binary token that ``harness_binaries`` does not carry is
    resolved individually; a template carrying no token is returned
    unchanged. No other part of a template is interpreted.
    """
    directory = (
        resolve_stage_worker_dir() if stage_worker_dir is None else stage_worker_dir
    )
    resolved = template.replace(STAGE_WORKER_DIR_TOKEN, directory)
    for token in HARNESS_BINARY_TOKENS:
        if token not in resolved:
            continue
        if harness_binaries is not None and token in harness_binaries:
            binary = harness_binaries[token]
        else:
            binary = resolve_harness_binary(token)
        resolved = resolved.replace(token, binary)
    return resolved


_FIELD_FROM_MESSAGE = re.compile(r"\('([^']+)' was unexpected\)")


class StaffingCatalogError(LLMRouterError):
    """Raised when a staffing catalog document is missing, unparsable, or invalid.

    The message always names the document and, when known, the JSON path
    of the offending field. ``document`` is the catalog document name
    (for example ``"routes"``) and ``path`` is a JSON path such as
    ``$.routes[0].usage_capture``.
    """

    def __init__(
        self,
        message: str,
        *,
        document: str,
        path: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(
            message, failure_type=FailureType.CONTRACT_VIOLATION, cause=cause
        )
        self.document = document
        self.path = path


# ---------------------------------------------------------------------------
# Typed objects (shape only; no decision logic)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    """One dispatch route: identity tuple (model, effort, harness, channel).

    ``status`` is the schema-enforced lifecycle value (``active``,
    ``unpriced``, or ``retired``); ``status_reason`` is optional here and
    conditionally required by the schema only when ``status`` is
    ``unpriced``. ``family`` is the optional schema field (Chief round 15)
    carrying an author/candidate independence comparison label only; when
    absent (the committed routes) the independence family check falls back
    to the model-vendor-prefix of ``model``. Shape only; no selection,
    ranking, or pricing logic (D206).
    """

    route_id: str
    model: str
    effort: str | None
    harness: str
    channel: str
    dispatch_template: str
    usage_capture: str
    status: str
    status_reason: str | None = None
    family: str | None = None


@dataclass(frozen=True)
class RoutesCatalog:
    routes: tuple[Route, ...]


@dataclass(frozen=True)
class FeeEntry:
    effective_from: str
    value: float | str


@dataclass(frozen=True)
class ChannelInstance:
    """One independently addressable subscription account instance."""

    instance_id: str
    credential_ref: str
    enabled: bool


@dataclass(frozen=True)
class Channel:
    channel_id: str
    kind: str
    fee_usd_month: tuple[FeeEntry, ...]
    windows: tuple[str, ...]
    harness_lock: tuple[str, ...]
    replacement_price_ref: str
    instances: tuple[ChannelInstance, ...] = ()

    def effective_instances(self) -> tuple[ChannelInstance, ...]:
        """Return declared instances or the channel's implicit instance."""
        if self.instances:
            return self.instances
        return (
            ChannelInstance(
                instance_id=self.channel_id,
                credential_ref=self.channel_id,
                enabled=True,
            ),
        )


@dataclass(frozen=True)
class ChannelsCatalog:
    channels: tuple[Channel, ...]


@dataclass(frozen=True)
class TermsEntry:
    channel_ref: str
    kind: str
    effective_from: str
    fee_usd_month: float | str
    source: str
    decision_price_ref: str
    reporting_price_ref: str


@dataclass(frozen=True)
class BadgeMultipliers:
    COLD: float
    USE_IT: float
    ON_TRACK: float
    HOT: float
    TOO_FAST: float
    NO_DATA: float


@dataclass(frozen=True)
class TermsCatalog:
    terms: tuple[TermsEntry, ...]
    badge_multipliers: BadgeMultipliers


@dataclass(frozen=True)
class NeverAutomaticRule:
    model_ref: str
    decision: str
    source: str
    effort: str | None = None


@dataclass(frozen=True)
class RoleScopedRule:
    model_ref: str
    allowed_role_classes: tuple[str, ...]
    denied_role_classes: tuple[str, ...]
    decision: str
    source: str


@dataclass(frozen=True)
class RoleClassEntry:
    role_ref: str
    role_class: str
    decision: str
    source: str


@dataclass(frozen=True)
class RoleFloorRecord:
    role_ref: str
    floor: str
    decision: str
    source: str
    effective_from: str | None = None


@dataclass(frozen=True)
class ReviewIndependenceRule:
    """One reviewer-independence rule; exactly one mode variant is populated."""

    reviewer_ref: str
    decision: str
    source: str
    not_same_worker_as: str | None = None
    prefer_different_family: bool | None = None
    fresh_eyes_mode: str | None = None
    second_opinion_mode: str | None = None


@dataclass(frozen=True)
class ReserveFractionOverride:
    """Per-channel override of the default reserve fraction (D216).

    Each entry names a ``channel_id`` and the ``reserve_fraction`` applied
    to it instead of the policy default. The override only has effect on
    subscription channels; kind is validated by the catalog, not here.
    """

    channel_id: str
    reserve_fraction: float


@dataclass(frozen=True)
class ReserveFractionPolicy:
    r"""Subscription-channel reserve fraction policy (D216).

    ``default`` is the reserve fraction applied to every subscription
    channel that has no explicit ``override``. ``overrides`` is a tuple of
    per-channel overrides, each naming a single channel_id and its explicit
    reserve fraction. The reserve is a policy floor, distinct from the
    availability reader's health heuristics (``likely_exhausted`` below
    10\%, which stays as a fail-safe). D216 sets the default to 0.10 and
    Anthropic and Gemini to 0.10 explicitly.
    """

    default: float
    overrides: tuple[ReserveFractionOverride, ...]

    def fraction_for(self, channel_id: str) -> float:
        """Return the reserve fraction for a channel (override wins)."""
        for override in self.overrides:
            if override.channel_id == channel_id:
                return override.reserve_fraction
        return self.default


@dataclass(frozen=True)
class SpendCap:
    cap: float
    unit: str
    scope: str
    decision: str
    source: str
    effective_from: str | None = None


@dataclass(frozen=True)
class CrewOrderingRule:
    """Named-crew ordering rule recorded in policy.yaml (D215 ruling 3).

    A single object with nonempty decision and source; not an array of
    records. This is recorded-data policy — a documented authoring rule,
    not new selection code; nothing in this class computes or enforces
    route ordering at runtime.
    """

    decision: str
    source: str


@dataclass(frozen=True)
class PolicyCatalog:
    """Typed policy document, including the D211 escalation-cost constant
    and the D215 crew-ordering rule.

    ``human_escalation_cost_usd`` is the terminal human-escalation cost in
    USD used by staffing ladder arithmetic when automated rungs are
    exhausted (D211 ruling 3). The current committed value is the Chief's
    placeholder and Lee may change it, so the schema fixes only the shape
    (finite nonnegative number); this field exposes the loaded value so
    Phase 2 consumers read it from the frozen catalog without raw YAML
    access.

    ``crew_ordering_rule`` is the named-crew ordering rule recorded in
    policy.yaml (D215 ruling 3): a single object with nonempty decision and
    source documenting that within a role's array, routes are ordered by
    marginal price at authoring time with prepaid-first as the tie-break;
    a human may pin otherwise with a stated reason. This is recorded-data
    policy — a documented authoring rule, not new selection code; nothing
    in this class computes or enforces route ordering at runtime. Shape
    only; no decision logic.
    """

    never_automatic: tuple[NeverAutomaticRule, ...]
    role_scoped: tuple[RoleScopedRule, ...]
    role_class: tuple[RoleClassEntry, ...]
    role_floors: tuple[RoleFloorRecord, ...]
    reviewer_independence: tuple[ReviewIndependenceRule, ...]
    spend_caps: tuple[SpendCap, ...]
    reserve_fraction: ReserveFractionPolicy
    crew_ordering_rule: CrewOrderingRule
    human_escalation_cost_usd: float


@dataclass(frozen=True)
class CanonicalEncoding:
    segment_separator: str
    tag_separator: str
    tag_order: str
    tag_dedup: bool
    empty_tags_segment: str
    notes: str


@dataclass(frozen=True)
class ValueSets:
    role: tuple[str, ...]
    oracle_type: tuple[str, ...]
    domain_tags: tuple[str, ...]
    size_band: tuple[str, ...]
    language: tuple[str, ...]


@dataclass(frozen=True)
class DenyPredicate:
    field: str
    op: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class CheapTrialRule:
    rule_name: str
    default: str
    override_kind: str
    override_direction: str
    override_description: str
    deny_reasons: tuple[str, ...] = ()
    deny_predicates: tuple[tuple[DenyPredicate, ...], ...] = ()


@dataclass(frozen=True)
class ClassesCatalog:
    taxonomy_version: str
    key_grammar: str
    canonical_encoding: CanonicalEncoding
    value_sets: ValueSets
    cheap_trial_rule: CheapTrialRule


@dataclass(frozen=True)
class CrewSupervisor:
    """Governed-crew supervisor identity (Chief round 9).

    The schema fixes this object to exactly ``{kind: engine,
    owner: auto-orch}``: the supervisor is the Auto-Orch loop plus the
    agent-orch engine, never a model route. Shape only; no decision logic.
    """

    kind: str
    owner: str


@dataclass(frozen=True)
class Crew:
    """One saved staffing block, preserving every schema-valid field.

    Chief round 9 gives ``crew`` exactly three conditional shapes, all
    discriminated by the required ``kind``:

    * ``interactive`` (named): ``supervisor_route``, ``worker_routes``,
      ``reviewer_route``, ``escalation_ladder`` are all set.
    * ``governed``: ``source``, ``crew_name``, typed ``supervisor``,
      ``worker_routes``, and ``escalation="not-recorded"``; never a
      supervisor route, reviewer route, or escalation ladder.
    * the reserved ``auto`` placeholder: ``kind="interactive"``,
      ``computed=True``, ``authority="policy"``, with every route-bearing
      and governed-only field unset.

    Each optional field is ``None`` exactly when the schema forbids or
    omits it for that shape, so loading preserves every field of each
    schema-valid shape without interpreting it. The optional
    ``governed_ref`` (ordinary named interactive crews only; the schema
    forbids it on governed crews and on ``auto``) is preserved verbatim
    when present. Shape only; no decision logic and no resolution of
    route references.
    """

    crew_id: str
    kind: str
    authority: str
    evidence_ref: str
    supervisor_route: str | None = None
    worker_routes: Mapping[str, tuple[str, ...]] | None = None
    reviewer_route: str | None = None
    escalation_ladder: tuple[str, ...] | None = None
    source: str | None = None
    crew_name: str | None = None
    supervisor: CrewSupervisor | None = None
    escalation: str | None = None
    computed: bool | None = None
    governed_ref: str | None = None


@dataclass(frozen=True)
class CrewsCatalog:
    crews: tuple[Crew, ...]


@dataclass(frozen=True)
class StaffingCatalog:
    routes: RoutesCatalog
    channels: ChannelsCatalog
    terms: TermsCatalog
    policy: PolicyCatalog
    classes: ClassesCatalog
    crews: CrewsCatalog


# ---------------------------------------------------------------------------
# Canonical class key (loader-owned boundary; D206: metadata, never selection)
# ---------------------------------------------------------------------------


def canonical_class_key(
    role: str,
    oracle_type: str,
    domain_tags: list[str] | tuple[str, ...],
    size_band: str,
    language: str,
) -> str:
    """Return the canonical lowercase five-part class key for a class block.

    Key grammar: ``role/oracle_type/domain_tags/size_band/language``. The
    domain-tags segment is the tag set deduplicated and sorted ascending
    by codepoint, joined by ``+``; the empty set renders as the literal
    ``none``, never an empty segment.
    """
    tags = sorted(set(domain_tags))
    segment = "+".join(tags) if tags else "none"
    return f"{role}/{oracle_type}/{segment}/{size_band}/{language}"


def validate_class_block(
    block: Mapping[str, Any],
    *,
    document: str = "class-block",
    path: str = "$",
) -> None:
    """Enforce canonical class-key equality for a classBlock mapping.

    The five component fields (role, oracle_type, domain_tags, size_band,
    language) are authoritative; ``class_key`` must equal their canonical
    rendering (tags sorted ascending by codepoint, deduplicated,
    ``+``-joined, empty set as ``none``). Raises :class:`StaffingCatalogError`
    naming ``document`` and the offending ``$.class_key`` path on mismatch.
    Performs no model/route selection.
    """
    expected = canonical_class_key(
        block["role"],
        block["oracle_type"],
        block["domain_tags"],
        block["size_band"],
        block["language"],
    )
    actual = block["class_key"]
    if actual != expected:
        raise StaffingCatalogError(
            f"{document}: canonical class-key mismatch at '{path}.class_key': "
            f"expected '{expected}' (components are authoritative: tags sorted "
            f"ascending by codepoint, '+'-joined, empty set as 'none'), "
            f"got '{actual}'",
            document=document,
            path=f"{path}.class_key",
        )


# ---------------------------------------------------------------------------
# Schema validation + typed construction
# ---------------------------------------------------------------------------


def _resolve_schema_dir(schema_dir: str | Path | None) -> Path:
    resolved = Path(schema_dir) if schema_dir is not None else DEFAULT_SCHEMA_DIR
    if not resolved.is_dir():
        raise StaffingCatalogError(
            f"staffing schema directory not found: {resolved}",
            document="schema-dir",
            path=str(resolved),
        )
    return resolved


def _error_field_path(error: ValidationError) -> str:
    """Best-effort JSON path naming the offending field for a schema error."""
    json_path = error.json_path
    if error.validator == "additionalProperties":
        match = _FIELD_FROM_MESSAGE.search(error.message)
        if match:
            return f"{json_path}.{match.group(1)}"
    return json_path


def _validate_document(name: str, data: Any, schema: Mapping[str, Any]) -> None:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if not errors:
        return
    error = errors[0]
    path = _error_field_path(error)
    raise StaffingCatalogError(
        f"staffing catalog document '{name}' failed schema validation at "
        f"'{path}': {error.message}",
        document=name,
        path=path,
        cause=error,
    )


def _build(cls: type, data: Mapping[str, Any], path: str) -> Any:
    """Construct frozen dataclass ``cls`` from validated mapping ``data``."""
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name in data:
            kwargs[f.name] = _convert(data[f.name], hints[f.name], f"{path}.{f.name}")
        elif f.default is not MISSING or f.default_factory is not MISSING:
            continue
        else:
            raise StaffingCatalogError(
                f"staffing catalog document '{path}': missing required field "
                f"'{f.name}' for {cls.__name__}",
                document=path,
                path=f"{path}.{f.name}",
            )
    return cls(**kwargs)


def _convert(value: Any, hint: Any, path: str) -> Any:
    origin = get_origin(hint)
    if origin is tuple:
        args = get_args(hint)
        if len(args) == 2 and args[1] is Ellipsis:
            item_hint = args[0]
            return tuple(
                _convert(item, item_hint, f"{path}[{i}]")
                for i, item in enumerate(value)
            )
        return tuple(
            _convert(item, arg, f"{path}[{i}]")
            for i, (item, arg) in enumerate(zip(value, args))
        )
    if isinstance(origin, type) and issubclass(origin, Mapping):
        _key_hint, value_hint = get_args(hint)
        return {
            key: _convert(item, value_hint, f"{path}.{key}")
            for key, item in value.items()
        }
    if origin is Union or origin is types.UnionType:
        non_none = [a for a in get_args(hint) if a is not type(None)]
        if value is None:
            return None
        for arg in non_none:
            if dataclasses.is_dataclass(arg) and isinstance(value, Mapping):
                return _build(arg, value, path)
        if len(non_none) == 1:
            return _convert(value, non_none[0], path)
        return value
    if dataclasses.is_dataclass(hint) and isinstance(hint, type):
        return _build(hint, value, path)
    return value


def _build_tuple(cls: type, items: list[Any], path: str) -> tuple:
    return tuple(_build(cls, item, f"{path}[{i}]") for i, item in enumerate(items))


def _build_routes(data: Mapping[str, Any]) -> RoutesCatalog:
    """Build the routes catalog, resolving every placeholder once.

    Resolution happens here, at load time (stage-worker directory P1-1,
    harness binaries P1-2), so every :class:`Route` this catalog exposes
    carries a fully resolved ``dispatch_template`` — the same string the
    pre-P1-2 catalog carried on a host that installs the five harness
    binaries at the paths those templates used to hard-code. Resolution
    never raises: an unresolvable harness binary resolves to its bare name.
    No other route field is read, rewritten, or validated differently.
    """
    stage_worker_dir = resolve_stage_worker_dir()
    harness_binaries = resolve_harness_binaries()
    routes = _build_tuple(Route, data["routes"], "$.routes")
    resolved = tuple(
        dataclasses.replace(
            route,
            dispatch_template=resolve_dispatch_template(
                route.dispatch_template, stage_worker_dir, harness_binaries
            ),
        )
        for route in routes
    )
    return RoutesCatalog(routes=resolved)


def _build_channels(data: Mapping[str, Any]) -> ChannelsCatalog:
    channels = _build_tuple(Channel, data["channels"], "$.channels")
    for channel_index, channel in enumerate(channels):
        seen_instance_ids: set[str] = set()
        for instance_index, instance in enumerate(channel.instances):
            if instance.instance_id in seen_instance_ids:
                raise StaffingCatalogError(
                    "staffing catalog document 'channels' has duplicate "
                    f"instance_id {instance.instance_id!r} at "
                    f"'$.channels[{channel_index}].instances[{instance_index}]'",
                    document="channels",
                    path=(
                        f"$.channels[{channel_index}].instances"
                        f"[{instance_index}].instance_id"
                    ),
                )
            seen_instance_ids.add(instance.instance_id)
    return ChannelsCatalog(channels=channels)


_BADGE_FIELDS: dict[str, str] = {
    "COLD": "COLD",
    "USE IT": "USE_IT",
    "ON TRACK": "ON_TRACK",
    "HOT": "HOT",
    "TOO FAST": "TOO_FAST",
    "NO DATA": "NO_DATA",
}


def _build_terms(data: Mapping[str, Any]) -> TermsCatalog:
    badges_raw = data["badge_multipliers"]
    badges = BadgeMultipliers(
        **{field_name: badges_raw[key] for key, field_name in _BADGE_FIELDS.items()}
    )
    return TermsCatalog(
        terms=_build_tuple(TermsEntry, data["terms"], "$.terms"),
        badge_multipliers=badges,
    )


def _build_policy(data: Mapping[str, Any]) -> PolicyCatalog:
    cost = data["human_escalation_cost_usd"]
    # The schema (minimum: 0, type: number) rejects negatives and non-numbers,
    # but YAML permits non-finite floats (.nan/.inf) that jsonschema's numeric
    # keywords cannot see, so finiteness is enforced here at the loader boundary.
    if (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(cost)
        or cost < 0
    ):
        raise StaffingCatalogError(
            "staffing catalog document 'policy': 'human_escalation_cost_usd' "
            f"must be a finite nonnegative number (USD, D211), got {cost!r}",
            document="policy",
            path="$.human_escalation_cost_usd",
        )
    reserve = data["reserve_fraction"]
    default = reserve["default"]
    if not isinstance(default, (int, float)) or isinstance(default, bool):
        raise StaffingCatalogError(
            "staffing catalog document 'policy': 'reserve_fraction.default' "
            f"must be a number (0 to 1, D216), got {default!r}",
            document="policy",
            path="$.reserve_fraction.default",
        )
    if default < 0 or default > 1:
        raise StaffingCatalogError(
            "staffing catalog document 'policy': 'reserve_fraction.default' "
            f"must be between 0 and 1, got {default!r}",
            document="policy",
            path="$.reserve_fraction.default",
        )
    overrides = _build_tuple(
        ReserveFractionOverride,
        reserve.get("overrides", []),
        "$.reserve_fraction.overrides",
    )
    reserve_policy = ReserveFractionPolicy(default=default, overrides=overrides)
    return PolicyCatalog(
        never_automatic=_build_tuple(
            NeverAutomaticRule, data["never_automatic"], "$.never_automatic"
        ),
        role_scoped=_build_tuple(RoleScopedRule, data["role_scoped"], "$.role_scoped"),
        role_class=_build_tuple(RoleClassEntry, data["role_class"], "$.role_class"),
        role_floors=_build_tuple(RoleFloorRecord, data["role_floors"], "$.role_floors"),
        reviewer_independence=_build_tuple(
            ReviewIndependenceRule,
            data["reviewer_independence"],
            "$.reviewer_independence",
        ),
        spend_caps=_build_tuple(SpendCap, data["spend_caps"], "$.spend_caps"),
        reserve_fraction=reserve_policy,
        crew_ordering_rule=_build(
            CrewOrderingRule, data["crew_ordering_rule"], "$.crew_ordering_rule"
        ),
        human_escalation_cost_usd=cost,
    )


def _build_classes(data: Mapping[str, Any]) -> ClassesCatalog:
    enc = data["canonical_encoding"]
    rule = data["cheap_trial_rule"]
    deny_reasons: list[str] = []
    deny_predicates: list[tuple[DenyPredicate, ...]] = []
    for condition in rule["deny_conditions"]:
        deny_reasons.append(condition["reason"])
        when = condition["when"]
        if "all_of" in when:
            predicates = when["all_of"]
        else:
            predicates = [when]
        deny_predicates.append(
            tuple(
                DenyPredicate(field=p["field"], op=p["op"], values=tuple(p["values"]))
                for p in predicates
            )
        )
    return ClassesCatalog(
        taxonomy_version=data["taxonomy_version"],
        key_grammar=data["key_grammar"],
        canonical_encoding=CanonicalEncoding(**enc),
        value_sets=ValueSets(
            role=tuple(data["value_sets"]["role"]),
            oracle_type=tuple(data["value_sets"]["oracle_type"]),
            domain_tags=tuple(data["value_sets"]["domain_tags"]),
            size_band=tuple(data["value_sets"]["size_band"]),
            language=tuple(data["value_sets"]["language"]),
        ),
        cheap_trial_rule=CheapTrialRule(
            rule_name=rule["rule_name"],
            default=rule["default"],
            deny_reasons=tuple(deny_reasons),
            deny_predicates=tuple(deny_predicates),
            override_kind=rule["override"]["kind"],
            override_direction=rule["override"]["direction"],
            override_description=rule["override"]["description"],
        ),
    )


def _build_crews(data: Mapping[str, Any]) -> CrewsCatalog:
    return CrewsCatalog(crews=_build_tuple(Crew, data["crews"], "$.crews"))


_BUILDERS: dict[str, Callable[[Mapping[str, Any]], Any]] = {
    "routes": _build_routes,
    "channels": _build_channels,
    "terms": _build_terms,
    "policy": _build_policy,
    "classes": _build_classes,
    "crews": _build_crews,
}

_SCHEMA_CACHE: dict[str, Mapping[str, Any]] = {}


def _load_schema(name: str, schema_dir: Path) -> Mapping[str, Any]:
    if name not in _SCHEMA_CACHE:
        schema_path = schema_dir / _SCHEMA_FILES[name]
        if not schema_path.is_file():
            raise StaffingCatalogError(
                f"staffing schema file not found for document '{name}': {schema_path}",
                document=name,
                path=str(schema_path),
            )
        with open(schema_path, encoding="utf-8") as handle:
            _SCHEMA_CACHE[name] = json.load(handle)
    return _SCHEMA_CACHE[name]


def load_staffing_document(
    name: str,
    path: str | Path,
    *,
    schema_dir: str | Path | None = None,
) -> Any:
    """Load, schema-validate, and type one staffing catalog document.

    ``name`` is one of ``routes``, ``channels``, ``terms``, ``policy``,
    ``classes``, ``crews``; ``path`` is the YAML file. Raises
    :class:`StaffingCatalogError` naming the document and field path for
    missing files, unparsable YAML, non-object documents, unknown fields,
    or schema violations.
    """
    schema_dir = _resolve_schema_dir(schema_dir)
    yaml_path = Path(path)
    try:
        raw = yaml_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StaffingCatalogError(
            f"staffing catalog document '{name}' could not be read at "
            f"'{yaml_path}': {exc}",
            document=name,
            path=str(yaml_path),
            cause=exc,
        ) from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise StaffingCatalogError(
            f"staffing catalog document '{name}' is not valid YAML at "
            f"'{yaml_path}': {exc}",
            document=name,
            path=str(yaml_path),
            cause=exc,
        ) from exc
    if not isinstance(data, Mapping):
        raise StaffingCatalogError(
            f"staffing catalog document '{name}' must be a YAML mapping, got "
            f"{type(data).__name__} at '{yaml_path}'",
            document=name,
            path=str(yaml_path),
        )
    schema = _load_schema(name, schema_dir)
    _validate_document(name, data, schema)
    return _BUILDERS[name](data)


def load_staffing_catalog(
    directory: str | Path,
    *,
    schema_dir: str | Path | None = None,
) -> StaffingCatalog:
    """Load the six staffing catalog documents from an explicit directory.

    Each of ``routes.yaml``, ``channels.yaml``, ``terms.yaml``,
    ``policy.yaml``, ``classes.yaml``, and ``crews.yaml`` is validated
    against its JSON Schema (Draft 2020-12) under ``schema_dir`` and
    returned as typed objects. No selection, ranking, or pricing logic
    is applied here.
    """
    directory = Path(directory)
    documents = {
        name: load_staffing_document(
            name, directory / f"{name}.yaml", schema_dir=schema_dir
        )
        for name in DOCUMENT_ORDER
    }
    return StaffingCatalog(**documents)
