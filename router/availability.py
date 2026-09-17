"""Reader for the ``ai-subs`` subscription-headroom snapshot.

Chief of Staff's ``scripts/ai-subs.sh`` emits an ``AFTER_REPORT_JSON`` object
describing how much of each subscription's quota is left. A refresh script
writes that object (plus ``host`` and ``written_at``) to
``~/.local/state/lee-llm-router/availability/<host>.json``. This module reads
that snapshot and normalises it into per-channel headroom the resolver can use
as a veto.

The reader never runs a provider CLI: it only reads the snapshot file. A
snapshot older than :data:`DEFAULT_MAX_AGE_MINUTES` — or one stamped more than
:data:`MAX_FUTURE_SKEW_MINUTES` in the future — degrades every channel to
``unknown``, never to ``healthy``. Age is governed by the *oldest* timestamp
present: a fresh ``written_at`` can never make an old ``observed_at`` read as
current, because a refresh only rewrites the snapshot, it does not re-observe
the provider quotas.
"""

from __future__ import annotations

import enum
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

DEFAULT_AVAILABILITY_DIR = Path("~/.local/state/lee-llm-router/availability")
"""Directory holding one snapshot file per host (``~`` expanded at load time)."""

AVAILABILITY_FILE_ENV_VAR = "LEE_LLM_ROUTER_AVAILABILITY_FILE"
"""Environment variable that overrides the default snapshot file location."""

DEFAULT_MAX_AGE_MINUTES = 90.0
"""Snapshots older than this many minutes degrade every channel to ``unknown``."""

MAX_FUTURE_SKEW_MINUTES = 5.0
"""Clock skew tolerated before a future-stamped snapshot is treated as stale."""

FUTURE_TIMESTAMP_REASON = "timestamp is in the future"
"""``stale_reason`` when either timestamp is stamped beyond the skew tolerance."""

CHANNELS: tuple[str, ...] = (
    "openai-sub",
    "anthropic-sub",
    "gemini-sub",
    "gemini-sub-thirdparty",
    "openrouter",
    "opencode-go",
)
"""Funding channels the resolver can ask about, in report order."""

GEMINI_CHANNELS: tuple[str, ...] = ("gemini-sub", "gemini-sub-thirdparty")
"""The two quotas ``Gemini/agy`` meters separately, in report order."""

GEMINI_BUCKET_PREFIXES: tuple[tuple[str, str], ...] = (
    ("Gemini models", "gemini-sub"),
    ("Claude/GPT models", "gemini-sub-thirdparty"),
)
"""Bucket-name prefixes that pin a ``Gemini/agy`` record to one channel."""

OPENCODE_GO_PROVIDER = "OpenCode/Go"
"""Provider label ``ai-subs`` emits for OpenCode Go."""

MODEL_SCOPED_BUCKETS: Mapping[str, str] = {
    "Claude Fable — weekly": "claude-fable",
}
"""Exact ``ai-subs`` bucket name -> model-family token (model sub-limits).

Some buckets ``ai-subs`` reports are *model sub-limits*: they meter one
model family inside a funding channel, not the channel's whole quota.
Anthropic reports ``Claude Fable — weekly`` beside ``Current session`` and
``All models — weekly``; when the Fable sub-limit empties, only Fable is
out of quota — Sonnet and Opus still draw on the channel-wide windows. Read
as a channel-wide constraint, as it was before this table existed, that one
sub-limit gated every Anthropic route.

This table is the only place that mapping lives, and it is keyed by the
exact bucket label: the family is *stated*, never parsed out of the prose
of the name. A heuristic over words like ``weekly`` or ``models`` would
silently re-scope a renamed or newly coined bucket, which is the failure
mode this table exists to avoid. A bucket name absent from this table is a
channel-wide constraint and behaves exactly as it always has.

Extending it: add one ``"<exact ai-subs bucket name>": "<family token>"``
row when a provider starts reporting another model sub-limit. The token is
matched against a route's model by ``route.model.startswith(family)`` in
:mod:`lee_llm_router.staffing.eligibility`.
"""


DEGRADED_STATUSES: frozenset[str] = frozenset({"TOO FAST", "HOT"})
"""Raw ``ai-subs`` status badges that mean "burning too fast" regardless of pct."""

KNOWN_STATUSES: frozenset[str] = frozenset(
    {
        "COLD",
        "ON TRACK",
        "HOT",
        "TOO FAST",
        "USE IT",
        "NO DATA",
        "UNAVAILABLE",
    }
)
"""The complete status vocabulary ``ai-subs`` emits.

``status_badge()`` in ``scripts/ai-subs.sh`` yields ``COLD``, ``ON TRACK``,
``HOT``, ``TOO FAST``, ``USE IT`` and ``NO DATA``; provider-level failures are
reported as ``UNAVAILABLE`` or ``NO_DATA``. Comparison is case-sensitive after
``str.strip()``, with ``NO_DATA`` accepted as an alias of ``NO DATA``: an
unrecognised badge is a snapshot the reader does not understand, and guessing
at its meaning is exactly the fail-open behaviour this set exists to prevent.
"""

_STATUS_ALIASES: dict[str, str] = {"NO_DATA": "NO DATA"}

_NO_DATA_STATUSES: frozenset[str] = frozenset({"NO DATA", "NO_DATA"})
"""Badges that mean "nothing was measured", regardless of any reported pct."""

MALFORMED_STATUS = "MALFORMED"
"""``raw_status`` stand-in for a record that carries no ``status`` at all."""

EXHAUSTED_FRACTION = 0.0
"""At or below this remaining fraction a bucket is ``exhausted``."""

LIKELY_EXHAUSTED_FRACTION = 0.10
"""Below this remaining fraction a bucket is ``likely_exhausted``."""

DEGRADED_FRACTION = 0.25
"""Below this remaining fraction a bucket is ``degraded``."""

_FAILURE_STATUSES: frozenset[str] = frozenset({"UNAVAILABLE", "NO_DATA"})


class AvailabilityError(ValueError):
    """Raised by :func:`parse_availability` when a snapshot cannot be read."""


class Health(enum.Enum):
    """How much headroom a bucket or channel has left."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    LIKELY_EXHAUSTED = "likely_exhausted"
    EXHAUSTED = "exhausted"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        """Return the wire value (``"healthy"``, ``"degraded"``, ...)."""
        return self.value


_SEVERITY: dict[Health, int] = {
    Health.HEALTHY: 0,
    Health.UNKNOWN: 1,
    Health.DEGRADED: 2,
    Health.LIKELY_EXHAUSTED: 3,
    Health.EXHAUSTED: 4,
}
"""Worst-wins ordering: any unknown bucket clouds a channel that is only healthy."""


@dataclass(frozen=True)
class Bucket:
    """One quota window reported by ``ai-subs``.

    Attributes:
        channel: The funding channel this bucket constrains.
        provider: The raw ``ai-subs`` provider label.
        name: The raw ``ai-subs`` bucket label.
        health: Health derived from the remaining fraction and status badge.
        remaining_fraction: Remaining quota as ``0.0``–``1.0``, or ``None``.
        resets_at: When the window resets, normalised to aware UTC.
        resets_in_hours: Hours until reset, as reported.
        pace_ratio: Burn pace relative to the window, as reported.
        raw_status: The raw status badge (``COLD``, ``HOT``, ``TOO FAST``, ...).
        instance: The instance id within the channel, or ``None`` if unspecified.
        model_scope: Model family this bucket meters, or ``None`` when the
            bucket constrains the channel as a whole. A model-scoped bucket is
            a provider's model *sub-limit*: it contributes to neither the
            channel-wide nor the instance-wide health/fraction reduction, and
            constrains only the routes for its family (see
            :data:`MODEL_SCOPED_BUCKETS`). It stays in the reported
            ``buckets`` tuple so no reading is hidden from reporting.
    """

    channel: str
    provider: str
    name: str
    health: Health
    remaining_fraction: float | None
    resets_at: datetime | None
    resets_in_hours: float | None
    pace_ratio: float | None
    raw_status: str
    instance: str | None = None
    model_scope: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of this bucket."""
        return {
            "channel": self.channel,
            "provider": self.provider,
            "name": self.name,
            "health": self.health.value,
            "remaining_fraction": self.remaining_fraction,
            "resets_at": _iso(self.resets_at),
            "resets_in_hours": self.resets_in_hours,
            "pace_ratio": self.pace_ratio,
            "raw_status": self.raw_status,
            "instance": self.instance,
            "model_scope": self.model_scope,
        }


@dataclass(frozen=True)
class ChannelHeadroom:
    """Aggregated headroom for one funding channel.

    Attributes:
        channel: The channel name (one of :data:`CHANNELS`).
        health: Worst health across the channel's buckets.
        remaining_fraction: Smallest numeric remaining fraction, or ``None``.
        limiting_bucket: Name of the bucket that set ``health``, or ``None``.
        observed_at: Snapshot observation time (aware UTC), or ``None``.
        stale: True when the snapshot is older than the staleness ceiling.
        buckets: The channel's buckets, in snapshot order.
        instance: The instance id if this headroom is for a specific instance,
            or ``None`` if channel-wide aggregate.
    """

    channel: str
    health: Health
    remaining_fraction: float | None
    limiting_bucket: str | None
    observed_at: datetime | None
    stale: bool
    buckets: tuple[Bucket, ...]
    instance: str | None = None

    @property
    def usable(self) -> bool:
        """True when the channel is known to have headroom left."""
        return self.health in (Health.HEALTHY, Health.DEGRADED)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of this channel."""
        return {
            "channel": self.channel,
            "health": self.health.value,
            "remaining_fraction": self.remaining_fraction,
            "limiting_bucket": self.limiting_bucket,
            "observed_at": _iso(self.observed_at),
            "stale": self.stale,
            "buckets": [bucket.to_dict() for bucket in self.buckets],
            "instance": self.instance,
        }


@dataclass(frozen=True)
class AvailabilitySnapshot:
    """A normalised ``ai-subs`` snapshot.

    Attributes:
        observed_at: When ``ai-subs`` observed the quotas (aware UTC).
        host: Host that wrote the snapshot, when recorded.
        path: Snapshot file the data came from, when read from disk.
        stale: True when the snapshot is too old to trust (or unreadable).
        age_minutes: Age in minutes of the oldest timestamp on the snapshot,
            or ``None`` when it carries none.
        channels: Headroom per channel, keyed by channel name.
        ignored: Labels of entries whose provider was not recognised.
        problem: Why the snapshot is unusable, or ``None`` when it is fine.
        written_at: When the refresh script wrote the snapshot (aware UTC).
        stale_reason: Why ``stale`` is set, or ``None`` when it is not.
        malformed: Labels of recognised-provider entries that could not be
            routed or scored and were forced to ``unknown``.
        instances: Headroom per (channel, instance) pair, keyed by
            (channel, instance).
    """

    observed_at: datetime | None
    host: str | None
    path: Path | None
    stale: bool
    age_minutes: float | None
    channels: dict[str, ChannelHeadroom]
    ignored: tuple[str, ...] = ()
    problem: str | None = None
    written_at: datetime | None = field(default=None)
    stale_reason: str | None = field(default=None)
    malformed: tuple[str, ...] = field(default=())
    instances: dict[tuple[str, str], ChannelHeadroom] = field(default_factory=dict)

    def headroom(self, channel: str) -> ChannelHeadroom:
        """Return the headroom for ``channel``.

        Args:
            channel: A channel name. Unknown or absent channels are reported as
                ``unknown`` rather than raising.

        Returns:
            The channel's :class:`ChannelHeadroom`.
        """
        existing = self.channels.get(channel)
        if existing is not None:
            return existing
        return _unknown_channel(channel, self.observed_at, self.stale)

    def instance_headroom(self, channel: str, instance: str) -> ChannelHeadroom:
        """Return the headroom for a specific ``(channel, instance)``.

        Args:
            channel: A channel name.
            instance: An instance identifier within that channel.

        Returns:
            The instance's :class:`ChannelHeadroom`. If missing, an unknown
            headroom for that channel and instance is returned.
        """
        existing = self.instances.get((channel, instance))
        if existing is not None:
            return existing
        return _unknown_channel(
            channel, self.observed_at, self.stale, instance=instance
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the whole snapshot."""
        return {
            "observed_at": _iso(self.observed_at),
            "written_at": _iso(self.written_at),
            "host": self.host,
            "path": str(self.path) if self.path is not None else None,
            "stale": self.stale,
            "stale_reason": self.stale_reason,
            "age_minutes": self.age_minutes,
            "problem": self.problem,
            "ignored": list(self.ignored),
            "malformed": list(self.malformed),
            "malformed_count": len(self.malformed),
            "channels": {
                name: headroom.to_dict() for name, headroom in self.channels.items()
            },
            "instances": [
                headroom.to_dict() for _, headroom in sorted(self.instances.items())
            ],
        }


def resolve_availability_path(explicit: str | Path | None = None) -> Path:
    """Resolve which snapshot file to read.

    Args:
        explicit: An explicit path, which wins over everything else.

    Returns:
        The resolved path: ``explicit``, else ``$LEE_LLM_ROUTER_AVAILABILITY_FILE``,
        else ``<default dir>/<hostname>.json``.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    from_env = os.environ.get(AVAILABILITY_FILE_ENV_VAR)
    if from_env:
        return Path(from_env).expanduser()
    if "socket" in sys.modules:
        import socket

        hostname = socket.gethostname()
    else:
        try:
            hostname = os.uname().nodename
        except AttributeError:
            import socket

            hostname = socket.gethostname()

    return DEFAULT_AVAILABILITY_DIR.expanduser() / f"{hostname}.json"


def load_availability(
    path: str | Path | None = None,
    *,
    now: datetime | None = None,
    max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES,
) -> AvailabilitySnapshot:
    """Read and normalise the availability snapshot, never raising.

    Args:
        path: Explicit snapshot path; resolved by :func:`resolve_availability_path`
            when omitted.
        now: Reference time for staleness. A naive value is read as UTC, never
            as host-local time. Defaults to the current UTC time.
        max_age_minutes: Age ceiling before the snapshot is treated as stale.

    Returns:
        An :class:`AvailabilitySnapshot`. On any problem — missing file, invalid
        JSON, missing ``subscriptions``, unparseable timestamps — the snapshot is
        stale, every channel is ``unknown``, and ``problem`` explains why.
    """
    resolved = resolve_availability_path(path)
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        return _problem_snapshot(resolved, f"cannot read {resolved}: {exc}")
    try:
        return parse_availability(
            text,
            path=resolved,
            now=now,
            max_age_minutes=max_age_minutes,
        )
    except AvailabilityError as exc:
        return _problem_snapshot(resolved, str(exc))


def parse_availability(
    text_or_dict: str | bytes | Mapping[str, Any],
    *,
    path: Path | None = None,
    now: datetime | None = None,
    max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES,
) -> AvailabilitySnapshot:
    """Parse a snapshot strictly.

    Args:
        text_or_dict: Snapshot JSON text, bytes, or an already-decoded mapping.
        path: Path the data came from, recorded on the snapshot.
        now: Reference time for staleness. A naive value is read as UTC, never
            as host-local time. Defaults to the current UTC time.
        max_age_minutes: Age ceiling before the snapshot is treated as stale.

    Returns:
        The normalised :class:`AvailabilitySnapshot`. Age is measured from the
        *oldest* timestamp the snapshot carries — ``age_minutes`` is the maximum
        of the ``observed_at`` and ``written_at`` ages — so a fresh write cannot
        launder a stale observation, and ``stale_reason`` names the timestamp
        that tripped the ceiling. The future-skew check applies to each
        timestamp independently: if either is more than
        :data:`MAX_FUTURE_SKEW_MINUTES` ahead of ``now`` the snapshot is stale
        with every channel ``unknown``, and a negative ``age_minutes`` is kept
        for diagnostics. With no timestamp at all the snapshot is stale.

    Raises:
        AvailabilityError: If the payload is not JSON, is not a mapping, lacks a
            ``subscriptions`` list, or carries an unparseable timestamp.
    """
    raw = _decode(text_or_dict)
    entries = raw.get("subscriptions")
    if not isinstance(entries, list):
        raise AvailabilityError("snapshot missing a 'subscriptions' list")

    observed_at = _parse_timestamp(raw.get("observed_at"), "observed_at")
    written_at = _parse_timestamp(raw.get("written_at"), "written_at")

    moment = _as_utc(now) if now is not None else _utc_now()
    ages = [
        (name, (moment - stamp).total_seconds() / 60.0)
        for name, stamp in (("observed_at", observed_at), ("written_at", written_at))
        if stamp is not None
    ]

    age_minutes: float | None = None
    stale = True
    stale_reason: str | None = "snapshot has no timestamp"
    problem: str | None = "snapshot has no timestamp"
    if ages:
        problem = None
        oldest_name, oldest_age = max(ages, key=lambda item: item[1])
        age_minutes = oldest_age
        future = [age for _, age in ages if age < -MAX_FUTURE_SKEW_MINUTES]
        if future:
            stale = True
            stale_reason = FUTURE_TIMESTAMP_REASON
            problem = FUTURE_TIMESTAMP_REASON
            # Report the skew itself: it is the actionable diagnostic, and the
            # snapshot is stale either way.
            age_minutes = min(future)
        elif oldest_age > max_age_minutes:
            stale = True
            stale_reason = f"{oldest_name} is older than {max_age_minutes:g} minutes"
        else:
            stale = False
            stale_reason = None

    buckets, ignored, malformed = _buckets_from_entries(entries)
    channels = _channels_from_buckets(buckets, observed_at, stale)
    instances = _instances_from_buckets(buckets, observed_at, stale)

    host = raw.get("host")
    return AvailabilitySnapshot(
        observed_at=observed_at,
        host=str(host) if isinstance(host, str) else None,
        path=path,
        stale=stale,
        age_minutes=age_minutes,
        channels=channels,
        ignored=tuple(ignored),
        problem=problem,
        written_at=written_at,
        stale_reason=stale_reason,
        malformed=tuple(malformed),
        instances=instances,
    )


def bucket_health(remaining_fraction: float | None, raw_status: str) -> Health:
    """Derive a bucket's health.

    Args:
        remaining_fraction: Remaining quota as ``0.0``–``1.0``, or ``None``.
        raw_status: The raw ``ai-subs`` status badge.

    Returns:
        ``unknown`` on a ``NO DATA`` badge (nothing was measured, so any
        accompanying percentage is meaningless), ``exhausted`` at or below zero
        remaining, ``likely_exhausted`` below 10%, ``degraded`` below 25% or on
        a ``HOT``/``TOO FAST`` badge, ``unknown`` without a finite remaining
        fraction and for any fraction above ``1.0`` (more headroom than
        exists), otherwise ``healthy``. A negative fraction stays ``exhausted``,
        which is the fail-closed reading. ``USE IT`` is an ordinary healthy-by-
        percentage badge, like ``COLD`` and ``ON TRACK``.
    """
    if raw_status.strip().upper() in _NO_DATA_STATUSES:
        return Health.UNKNOWN
    import math

    if remaining_fraction is None or not math.isfinite(remaining_fraction):
        return Health.UNKNOWN
    if remaining_fraction > 1.0:
        return Health.UNKNOWN
    if remaining_fraction <= EXHAUSTED_FRACTION:
        return Health.EXHAUSTED
    if remaining_fraction < LIKELY_EXHAUSTED_FRACTION:
        return Health.LIKELY_EXHAUSTED
    if remaining_fraction < DEGRADED_FRACTION:
        return Health.DEGRADED
    if raw_status.strip().upper() in DEGRADED_STATUSES:
        return Health.DEGRADED
    return Health.HEALTHY


def channels_for(provider: str, bucket_name: str) -> tuple[str, ...]:
    """Map a raw provider/bucket pair onto funding channels.

    Args:
        provider: The raw ``ai-subs`` provider label.
        bucket_name: The raw ``ai-subs`` bucket label (may be empty).

    Returns:
        The channels the entry constrains; empty only when the *provider* is
        unknown. A ``Gemini/agy`` bucket whose name matches neither prefix —
        empty, renamed, or garbled — constrains *both* Gemini channels, because
        the reader cannot tell which of the two quotas it belongs to. Returning
        both is what makes such a record cloud them rather than disappear; see
        :func:`is_routable_bucket`.
    """
    label = provider.strip()
    if label == "OpenAI/Codex":
        return ("openai-sub",)
    if label == "Anthropic/Claude":
        return ("anthropic-sub",)
    if label == "Gemini/agy":
        name = bucket_name.strip()
        for prefix, channel in GEMINI_BUCKET_PREFIXES:
            if name.startswith(prefix):
                return (channel,)
        return GEMINI_CHANNELS
    if label == OPENCODE_GO_PROVIDER:
        return ("opencode-go",)
    return ()


def is_routable_bucket(provider: str, bucket_name: str) -> bool:
    """Return True when a provider/bucket pair names exactly one channel.

    Args:
        provider: The raw ``ai-subs`` provider label.
        bucket_name: The raw ``ai-subs`` bucket label.

    Returns:
        True when :func:`channels_for` resolves the pair to a single channel.
        A pair that fans out to several is *ambiguous*, not routable: a
        ``Gemini/agy`` bucket the prefix table does not recognise could be
        metered against either Gemini quota, so its numbers cannot be scored
        against either one and the record is treated as malformed.
    """
    return len(channels_for(provider, bucket_name)) == 1


is_single_channel = is_routable_bucket


def is_known_status(raw_status: str) -> bool:
    """Return True when ``raw_status`` is a badge ``ai-subs`` actually emits.

    Args:
        raw_status: The raw ``status`` field, as read from the snapshot.

    Returns:
        True when the stripped value is in :data:`KNOWN_STATUSES`, treating
        ``NO_DATA`` as an alias of ``NO DATA``. The comparison is
        case-sensitive: ``ai-subs`` emits upper-case badges, and anything else
        is a payload this reader does not understand.
    """
    text = raw_status.strip()
    return _STATUS_ALIASES.get(text, text) in KNOWN_STATUSES


def model_scope_for(bucket_name: str) -> str | None:
    """Return the model family a bucket name is scoped to, when it is one.

    Args:
        bucket_name: The raw ``ai-subs`` bucket label.

    Returns:
        The family token from :data:`MODEL_SCOPED_BUCKETS` for an exact
        (whitespace-stripped) label match, else ``None`` — which means the
        bucket constrains the whole channel, the behaviour every bucket had
        before the table existed.
    """
    return MODEL_SCOPED_BUCKETS.get(bucket_name.strip())


def is_wellformed_quota(entry: Mapping[str, Any]) -> bool:
    """Return True when a quota record carries every field health needs.

    Args:
        entry: One ``subscriptions`` element that is *not* a provider-level
            ``UNAVAILABLE``/``NO_DATA`` failure record.

    Returns:
        True only when ``bucket`` is a non-empty string, ``status`` is a
        recognised badge, and ``remaining_pct`` is a finite number inside
        ``0``–``100``. Anything less is data the reader cannot score, and
        scoring it anyway is how a truncated or renamed ai-subs field would
        read as ``healthy``.
    """
    bucket = entry.get("bucket")
    if not isinstance(bucket, str) or not bucket.strip():
        return False
    status = entry.get("status")
    if not isinstance(status, str) or not is_known_status(status):
        return False
    remaining = entry.get("remaining_pct")
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
        return False
    return _fraction(remaining) is not None


def _buckets_from_entries(
    entries: Iterable[Any],
) -> tuple[list[Bucket], list[str], list[str]]:
    """Turn raw ``subscriptions`` entries into buckets, ignored, and malformed.

    Args:
        entries: The raw ``subscriptions`` list.

    Returns:
        The buckets, the labels of entries whose *provider* is unrecognised,
        and the labels of recognised-provider entries that failed
        :func:`is_routable_bucket` or :func:`is_wellformed_quota` and were
        therefore forced to ``unknown``.
    """
    buckets: list[Bucket] = []
    ignored: list[str] = []
    malformed: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            ignored.append(repr(entry))
            continue
        provider = str(entry.get("provider", "")).strip()
        raw_name = str(entry.get("bucket", "")).strip()
        has_status = entry.get("status") is not None
        status = str(entry.get("status", "")).strip() if has_status else ""
        targets = channels_for(provider, raw_name)
        if not targets:
            ignored.append(f"{provider or '?'}/{raw_name or status or '?'}")
            continue

        raw_instance = entry.get("instance")
        instance = (
            raw_instance.strip()
            if isinstance(raw_instance, str) and raw_instance.strip()
            else None
        )

        failure = status.upper() in _FAILURE_STATUSES
        name = raw_name or status or "unknown"
        if failure:
            remaining = None
            health = Health.UNKNOWN
            raw_status = status
        elif is_routable_bucket(provider, raw_name) and is_wellformed_quota(entry):
            remaining = _fraction(entry.get("remaining_pct"))
            health = bucket_health(remaining, status)
            raw_status = status
        else:
            # Fail closed: a record missing a bucket, a status, or a usable
            # percentage — or one whose bucket name pins it to no single
            # channel — tells us nothing about any channel's headroom, so it
            # must not be scored as if it did.
            remaining = None
            health = Health.UNKNOWN
            raw_status = status if has_status else MALFORMED_STATUS
            malformed.append(f"{provider or '?'}/{name} [{raw_status or '?'}]")
        for channel in targets:
            buckets.append(
                Bucket(
                    channel=channel,
                    provider=provider,
                    name=name,
                    health=health,
                    remaining_fraction=remaining,
                    resets_at=_safe_timestamp(entry.get("resets_at")),
                    resets_in_hours=_number(entry.get("resets_in_hours")),
                    pace_ratio=_number(entry.get("pace_ratio")),
                    raw_status=raw_status,
                    instance=instance,
                    model_scope=model_scope_for(name),
                )
            )
    return buckets, ignored, malformed


def _effective_instance(bucket: Bucket, channel: str) -> str:
    """Return the bucket's instance, or the channel name as the default instance."""
    return bucket.instance if bucket.instance is not None else channel


def _reduce_channel_wide(
    buckets: tuple[Bucket, ...], stale: bool
) -> tuple[Health, float | None, str | None]:
    """Reduce one group to ``(health, remaining_fraction, limiting_bucket)``.

    Only channel-wide buckets take part: a bucket carrying a ``model_scope``
    meters one model family inside the channel, so it belongs to neither the
    ``min()`` nor the ``limiting`` health of the channel or instance it sits
    on. It is reported through ``ChannelHeadroom.buckets`` unchanged.

    An aggregate with no channel-wide bucket left — every bucket on it is
    model-scoped — reads ``unknown`` with no fraction and no limiting bucket:
    the snapshot observed no channel-wide constraint, and the reader invents
    none. That is the fail-closed reading, matching ``_unknown_channel``.

    Args:
        buckets: Every bucket aggregated into this channel or instance, in
            snapshot order.
        stale: Whether the snapshot is stale, which degrades any derived
            health to ``unknown``.

    Returns:
        The worst channel-wide health (``unknown`` when stale), the smallest
        numeric remaining fraction among channel-wide buckets, and the name of
        the bucket that set the health.
    """
    channel_wide = [bucket for bucket in buckets if bucket.model_scope is None]
    if not channel_wide:
        return Health.UNKNOWN, None, None

    worst = max(_SEVERITY[bucket.health] for bucket in channel_wide)
    limiting = next(
        bucket for bucket in channel_wide if _SEVERITY[bucket.health] == worst
    )
    numbers = [
        bucket.remaining_fraction
        for bucket in channel_wide
        if bucket.remaining_fraction is not None
    ]
    return (
        Health.UNKNOWN if stale else limiting.health,
        min(numbers) if numbers else None,
        None if stale else limiting.name,
    )


def _instances_from_buckets(
    buckets: list[Bucket],
    observed_at: datetime | None,
    stale: bool,
) -> dict[tuple[str, str], ChannelHeadroom]:
    """Aggregate buckets into one :class:`ChannelHeadroom` per (channel, instance)."""
    groups: dict[tuple[str, str], list[Bucket]] = {}
    for bucket in buckets:
        key = (bucket.channel, _effective_instance(bucket, bucket.channel))
        groups.setdefault(key, []).append(bucket)

    instances: dict[tuple[str, str], ChannelHeadroom] = {}
    for (channel, instance), group_buckets in groups.items():
        owned = tuple(group_buckets)
        health, remaining, limiting_bucket = _reduce_channel_wide(owned, stale)
        instances[(channel, instance)] = ChannelHeadroom(
            channel=channel,
            health=health,
            remaining_fraction=remaining,
            limiting_bucket=limiting_bucket,
            observed_at=observed_at,
            stale=stale,
            buckets=owned,
            instance=instance,
        )
    return instances


def _channels_from_buckets(
    buckets: list[Bucket],
    observed_at: datetime | None,
    stale: bool,
) -> dict[str, ChannelHeadroom]:
    """Aggregate buckets into one :class:`ChannelHeadroom` per known channel."""
    channels: dict[str, ChannelHeadroom] = {}
    for channel in CHANNELS:
        owned = tuple(bucket for bucket in buckets if bucket.channel == channel)
        if not owned:
            channels[channel] = _unknown_channel(channel, observed_at, stale)
            continue

        health, remaining, limiting_bucket = _reduce_channel_wide(owned, stale)
        channels[channel] = ChannelHeadroom(
            channel=channel,
            health=health,
            remaining_fraction=remaining,
            limiting_bucket=limiting_bucket,
            observed_at=observed_at,
            stale=stale,
            buckets=owned,
        )
    return channels


def _unknown_channel(
    channel: str,
    observed_at: datetime | None,
    stale: bool,
    instance: str | None = None,
) -> ChannelHeadroom:
    """Build an empty ``unknown`` headroom record for ``channel``."""
    return ChannelHeadroom(
        channel=channel,
        health=Health.UNKNOWN,
        remaining_fraction=None,
        limiting_bucket=None,
        observed_at=observed_at,
        stale=stale,
        buckets=(),
        instance=instance,
    )


def _problem_snapshot(path: Path | None, problem: str) -> AvailabilitySnapshot:
    """Build a stale, all-``unknown`` snapshot explaining ``problem``."""
    return AvailabilitySnapshot(
        observed_at=None,
        host=None,
        path=path,
        stale=True,
        age_minutes=None,
        channels={
            channel: _unknown_channel(channel, None, True) for channel in CHANNELS
        },
        ignored=(),
        problem=problem,
        stale_reason=problem,
    )


def _decode(payload: str | bytes | Mapping[str, Any]) -> Mapping[str, Any]:
    """Decode snapshot input into a mapping."""
    if isinstance(payload, Mapping):
        return payload
    try:
        import json

        raw = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise AvailabilityError(f"invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AvailabilityError("snapshot top level must be a JSON object")
    return raw


def _parse_timestamp(value: Any, field_name: str) -> datetime | None:
    """Parse an ISO timestamp into aware UTC, treating naive input as UTC."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise AvailabilityError(f"{field_name} must be an ISO timestamp string")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AvailabilityError(
            f"{field_name} is not an ISO timestamp: {value}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_timestamp(value: Any) -> datetime | None:
    """Parse an optional timestamp, returning ``None`` instead of raising."""
    try:
        return _parse_timestamp(value, "resets_at")
    except AvailabilityError:
        return None


def _number(value: Any) -> float | None:
    """Coerce ``value`` to a finite float.

    Args:
        value: Any snapshot value that should hold a number.

    Returns:
        The value as a float, or ``None`` when it is not numeric or is not
        finite. ``NaN`` and the infinities are rejected deliberately: ``NaN``
        loses every ``<``/``<=`` comparison, so letting one through would make a
        bucket read as ``healthy``.
    """
    if isinstance(value, bool) or value is None:
        return None
    number: float
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    import math

    return number if math.isfinite(number) else None


def _fraction(value: Any) -> float | None:
    """Convert a percentage to a ``0.0``–``1.0`` fraction.

    Args:
        value: A percentage, typically ``remaining_pct``.

    Returns:
        The fraction, or ``None`` when the value is not a finite number or falls
        outside ``0``–``100``. Out-of-range percentages are reported as unknown
        rather than clamped, because a clamp would invent headroom.
    """
    number = _number(value)
    if number is None:
        return None
    fraction = number / 100.0
    return fraction if 0.0 <= fraction <= 1.0 else None


def _as_utc(moment: datetime) -> datetime:
    """Normalise a datetime to aware UTC, treating a naive value as UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    """Render an optional datetime as an ISO string."""
    return None if moment is None else moment.isoformat()


def _utc_now() -> datetime:
    """Return the current time as aware UTC."""
    return datetime.now(timezone.utc)
