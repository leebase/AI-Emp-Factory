"""Loader for Auto-Orch's crews file.

A crew names a full roster: which worker runs each cognitive stage
(``envision``, ``ideate``, ``reconsider``, ``score``, ``author``) and which
governed routes a mission's playbook must mandate (``primary`` producer,
``reviewer``, ``judge``).

This module is strictly read-only: it never writes to the crews file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


def __getattr__(name: str) -> Any:
    if name == "_YamlSafeLoader":
        try:
            from yaml import CSafeLoader as loader
        except ImportError:  # pragma: no cover
            from yaml import SafeLoader as loader
        return loader
    if name == "_WORKER_ENV_RE":
        return _get_worker_env_re()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


DEFAULT_CREWS_FILE = Path("~/projects/auto-orch/config/crews.yaml")
"""Default crews file location (``~`` expanded at load time)."""

CREWS_FILE_ENV_VAR = "LEE_LLM_ROUTER_CREWS_FILE"
"""Environment variable that overrides the default crews file location."""

STAGE_NAMES: tuple[str, ...] = (
    "envision",
    "ideate",
    "reconsider",
    "score",
    "author",
)
"""The known cognitive stage names, in canonical cycle order."""

GOVERNED_ROLES: tuple[str, ...] = ("primary", "reviewer", "judge")
"""The governed route roles a crew may declare."""

# decisions.md D188: stage assignment is the supervisor's judgment recorded
# for Lee's confirmation.
ROLE_CLASS_BY_ROLE: dict[str, str] = {
    "author": "coding",
    "envision": "planning_review",
    "ideate": "planning_review",
    "reconsider": "planning_review",
    "score": "planning_review",
    "primary": "coding",
    "reviewer": "planning_review",
    "judge": "planning_review",
}
"""Mapping of role names to their role class (decisions.md D188)."""


class CrewsConfigError(ValueError):
    """Raised when the crews file is missing, malformed, or inconsistent."""


@dataclass(frozen=True)
class Worker:
    """A single stage worker declared under the top-level ``workers`` map.

    Attributes:
        id: The worker id (the YAML key).
        command: Shell command template used to run a stage.
        preflight_command: Shell command used to check worker availability.
        timeout_seconds: Per-invocation timeout in seconds.
    """

    id: str
    command: str
    preflight_command: str | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class Stage:
    """An ordered set of workers eligible to run one cognitive stage.

    Attributes:
        name: The stage name (one of :data:`STAGE_NAMES`).
        workers: Worker ids in declared preference order; the first entry is
            the primary worker for the stage.
    """

    name: str
    workers: tuple[str, ...]

    @property
    def primary(self) -> str:
        """Return the first (preferred) worker id for this stage."""
        return self.workers[0]


@dataclass(frozen=True)
class GovernedRoute:
    """A governed harness route for one role of a governed run.

    Attributes:
        role: One of :data:`GOVERNED_ROLES`.
        harness: Harness identifier (e.g. ``claude_code``, ``codex_cli``).
        model: Optional model identifier for the harness.
        effort: Optional reasoning-effort hint for the harness.
    """

    role: str
    harness: str
    model: str | None = None
    effort: str | None = None


@dataclass(frozen=True)
class Crew:
    """A named crew: stage workers plus governed routes.

    Attributes:
        name: The crew name (the YAML key).
        description: Human-readable description of the crew's intent.
        stages: Mapping of stage name to :class:`Stage`, in file order.
        governed: Mapping of governed role to :class:`GovernedRoute`.
        judge_model: Optional explicit semantic-judge model override.
    """

    name: str
    description: str
    stages: dict[str, Stage]
    governed: dict[str, GovernedRoute]
    judge_model: str | None = None

    def eligible(self, stage: str) -> tuple[str, ...]:
        """Return the ordered eligible worker ids for a stage.

        Args:
            stage: Stage name to look up.

        Returns:
            Worker ids in declared preference order.

        Raises:
            CrewsConfigError: If this crew does not declare that stage.
        """
        try:
            return self.stages[stage].workers
        except KeyError as exc:
            raise CrewsConfigError(
                f"crew {self.name!r} has no stage {stage!r}"
            ) from exc

    def primary(self, stage: str) -> str:
        """Return the preferred (first) worker id for a stage.

        Args:
            stage: Stage name to look up.

        Returns:
            The first eligible worker id.

        Raises:
            CrewsConfigError: If this crew does not declare that stage.
        """
        return self.eligible(stage)[0]


@dataclass(frozen=True)
class CrewsConfig:
    """Parsed crews file.

    Attributes:
        workers: Mapping of worker id to :class:`Worker`, in file order.
        crews: Mapping of crew name to :class:`Crew`, in file order.
        path: The resolved path the config was loaded from.
    """

    workers: dict[str, Worker]
    crews: dict[str, Crew]
    path: Path

    def crew(self, name: str) -> Crew:
        """Return a crew by name.

        Args:
            name: Crew name.

        Returns:
            The matching :class:`Crew`.

        Raises:
            CrewsConfigError: If no such crew exists.
        """
        try:
            return self.crews[name]
        except KeyError as exc:
            known = ", ".join(self.crews) or "<none>"
            raise CrewsConfigError(
                f"unknown crew {name!r} (known crews: {known})"
            ) from exc


def resolve_crews_path(explicit: str | Path | None = None) -> Path:
    """Resolve which crews file to load.

    Precedence: explicit argument, then the ``LEE_LLM_ROUTER_CREWS_FILE``
    environment variable, then :data:`DEFAULT_CREWS_FILE`.

    Args:
        explicit: Caller-supplied path, if any.

    Returns:
        An expanded (``~`` resolved) path. Existence is not checked here.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    env_value = os.environ.get(CREWS_FILE_ENV_VAR)
    if env_value:
        return Path(env_value).expanduser()
    return DEFAULT_CREWS_FILE.expanduser()


def _parse_workers(raw: Any, path: Path) -> dict[str, Worker]:
    """Parse the top-level ``workers`` mapping."""
    if not isinstance(raw, dict) or not raw:
        raise CrewsConfigError(f"{path}: 'workers' must be a non-empty mapping")
    workers: dict[str, Worker] = {}
    for worker_id, body in raw.items():
        if not isinstance(body, dict):
            raise CrewsConfigError(f"{path}: worker {worker_id!r} must be a mapping")
        command = body.get("command")
        if not isinstance(command, str) or not command.strip():
            raise CrewsConfigError(
                f"{path}: worker {worker_id!r} is missing a 'command' string"
            )
        timeout = body.get("timeout_seconds")
        if timeout is not None and not isinstance(timeout, int):
            raise CrewsConfigError(
                f"{path}: worker {worker_id!r} has a non-integer 'timeout_seconds'"
            )
        preflight = body.get("preflight_command")
        if preflight is not None and not isinstance(preflight, str):
            raise CrewsConfigError(
                f"{path}: worker {worker_id!r} has a non-string 'preflight_command'"
            )
        workers[str(worker_id)] = Worker(
            id=str(worker_id),
            command=command,
            preflight_command=preflight,
            timeout_seconds=timeout,
        )
    return workers


def _parse_stage(
    crew_name: str,
    stage_name: str,
    value: Any,
    known_workers: dict[str, Worker],
    path: Path,
) -> Stage:
    """Parse one stage value into a :class:`Stage`."""
    if stage_name not in STAGE_NAMES:
        raise CrewsConfigError(
            f"{path}: crew {crew_name!r} declares unknown stage "
            f"{stage_name!r} (known stages: {', '.join(STAGE_NAMES)})"
        )
    if isinstance(value, str):
        worker_ids: tuple[str, ...] = (value,)
    elif isinstance(value, list):
        if not value:
            raise CrewsConfigError(
                f"{path}: crew {crew_name!r} stage {stage_name!r} has an "
                "empty worker list"
            )
        for entry in value:
            if not isinstance(entry, str):
                raise CrewsConfigError(
                    f"{path}: crew {crew_name!r} stage {stage_name!r} list "
                    f"entry {entry!r} is not a string"
                )
        worker_ids = tuple(value)
    else:
        raise CrewsConfigError(
            f"{path}: crew {crew_name!r} stage {stage_name!r} must be a "
            f"worker id string or a list of worker id strings, got "
            f"{type(value).__name__}"
        )
    for worker_id in worker_ids:
        if worker_id not in known_workers:
            raise CrewsConfigError(
                f"{path}: crew {crew_name!r} stage {stage_name!r} references "
                f"unknown worker {worker_id!r}"
            )
    return Stage(name=stage_name, workers=worker_ids)


def _parse_governed(crew_name: str, raw: Any, path: Path) -> dict[str, GovernedRoute]:
    """Parse a crew's ``governed`` mapping."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise CrewsConfigError(
            f"{path}: crew {crew_name!r} 'governed' must be a mapping"
        )
    routes: dict[str, GovernedRoute] = {}
    for role, body in raw.items():
        if not isinstance(body, dict):
            raise CrewsConfigError(
                f"{path}: crew {crew_name!r} governed role {role!r} must be "
                "a mapping"
            )
        harness = body.get("harness")
        if not isinstance(harness, str) or not harness.strip():
            raise CrewsConfigError(
                f"{path}: crew {crew_name!r} governed role {role!r} is "
                "missing a 'harness' string"
            )
        routes[str(role)] = GovernedRoute(
            role=str(role),
            harness=harness,
            model=body.get("model"),
            effort=body.get("effort"),
        )
    return routes


def _parse_crew(
    name: str, body: Any, known_workers: dict[str, Worker], path: Path
) -> Crew:
    """Parse a single crew entry."""
    if not isinstance(body, dict):
        raise CrewsConfigError(f"{path}: crew {name!r} must be a mapping")
    raw_stages = body.get("stages")
    if not isinstance(raw_stages, dict) or not raw_stages:
        raise CrewsConfigError(
            f"{path}: crew {name!r} must declare a non-empty 'stages' mapping"
        )
    stages: dict[str, Stage] = {}
    for stage_name, value in raw_stages.items():
        stages[str(stage_name)] = _parse_stage(
            name, str(stage_name), value, known_workers, path
        )
    description = body.get("description") or ""
    if not isinstance(description, str):
        raise CrewsConfigError(f"{path}: crew {name!r} 'description' must be a string")
    judge_model = body.get("judge_model")
    if judge_model is not None and not isinstance(judge_model, str):
        raise CrewsConfigError(f"{path}: crew {name!r} 'judge_model' must be a string")
    return Crew(
        name=name,
        description=description.strip(),
        stages=stages,
        governed=_parse_governed(name, body.get("governed"), path),
        judge_model=judge_model,
    )


def _cache_dir() -> Path:
    """Return the directory holding crews parse caches."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "lee-llm-router"


def _cache_path_for(abs_path: str) -> Path:
    """Return the cache file path for a crews file's absolute path."""
    import zlib

    hashed = f"{zlib.crc32(abs_path.encode('utf-8')) & 0xFFFFFFFF:08x}"
    return _cache_dir() / f"crews_{hashed}.json"


def _read_cache(
    cache_file: Path,
    abs_path: str,
    size: int,
    mtime_ns: int,
) -> dict[str, Any] | None:
    """Read cached raw crews mapping if path, size, and mtime_ns match."""
    try:
        text = cache_file.read_text(encoding="utf-8")
        data = json.loads(text)
        if (
            isinstance(data, dict)
            and data.get("source_path") == abs_path
            and data.get("size") == size
            and data.get("mtime_ns") == mtime_ns
            and isinstance(data.get("raw"), dict)
        ):
            return data["raw"]
    except Exception:
        return None
    return None


def _write_cache(
    cache_file: Path,
    abs_path: str,
    size: int,
    mtime_ns: int,
    raw: dict[str, Any],
) -> None:
    """Atomically write raw crews mapping to cache."""
    try:
        import tempfile

        cache_dir = cache_file.parent
        cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "source_path": abs_path,
            "size": size,
            "mtime_ns": mtime_ns,
            "raw": raw,
        }
        data = json.dumps(payload)
        temp_fd, temp_path = tempfile.mkstemp(
            prefix="crews_",
            suffix=".tmp",
            dir=cache_dir,
        )
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(temp_path, cache_file)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    except Exception:
        pass


def _load_yaml(resolved: Path) -> dict[str, Any]:
    import yaml

    try:
        from yaml import CSafeLoader as loader
    except ImportError:  # pragma: no cover
        from yaml import SafeLoader as loader

    try:
        raw = yaml.load(  # noqa: S506 - CSafeLoader/SafeLoader only
            resolved.read_text(encoding="utf-8"), Loader=loader
        )
    except yaml.YAMLError as exc:
        raise CrewsConfigError(f"{resolved}: invalid YAML: {exc}") from exc
    return raw


def _build_crews_config(raw: Any, resolved: Path) -> CrewsConfig:
    if not isinstance(raw, dict):
        raise CrewsConfigError(f"{resolved}: top level must be a mapping")
    if "workers" not in raw:
        raise CrewsConfigError(f"{resolved}: missing top-level 'workers'")
    if "crews" not in raw:
        raise CrewsConfigError(f"{resolved}: missing top-level 'crews'")

    workers = _parse_workers(raw["workers"], resolved)
    raw_crews = raw["crews"]
    if not isinstance(raw_crews, dict) or not raw_crews:
        raise CrewsConfigError(f"{resolved}: 'crews' must be a non-empty mapping")
    crews = {
        str(name): _parse_crew(str(name), body, workers, resolved)
        for name, body in raw_crews.items()
    }
    return CrewsConfig(workers=workers, crews=crews, path=resolved)


def load_crews(path: str | Path | None = None) -> CrewsConfig:
    """Load and validate the Auto-Orch crews file.

    Args:
        path: Explicit crews file path. When omitted, the path is resolved by
            :func:`resolve_crews_path`.

    Returns:
        A :class:`CrewsConfig` preserving worker, crew, and stage file order.

    Raises:
        CrewsConfigError: If the file is missing, is not a mapping, lacks
            ``workers`` or ``crews``, or declares an invalid stage.
    """
    resolved = resolve_crews_path(path)
    if not resolved.is_file():
        raise CrewsConfigError(f"crews file not found: {resolved}")

    stat = resolved.stat()
    abs_path = str(resolved.resolve())
    no_cache = os.environ.get("LEE_LLM_ROUTER_NO_CACHE") == "1"
    cache_file = _cache_path_for(abs_path)

    if not no_cache:
        raw = _read_cache(cache_file, abs_path, stat.st_size, stat.st_mtime_ns)
        if raw is not None:
            try:
                return _build_crews_config(raw, resolved)
            except Exception:
                pass

    raw = _load_yaml(resolved)
    config = _build_crews_config(raw, resolved)

    if not no_cache:
        _write_cache(cache_file, abs_path, stat.st_size, stat.st_mtime_ns, raw)

    return config


WORKER_ENV_PREFIX_PROVIDERS: dict[str, str] = {
    "CODEX": "codex_cli",
    "CLAUDE": "claude_code_cli",
    "OMP": "omp_cli",
    "OPENCODE": "opencode_cli",
    "ANTIGRAVITY": "antigravity_cli",
    "PI": "pi_cli",
}
"""Maps a stage-worker env-var prefix to a registered router provider name."""

_BUILTIN_REGISTERED_PROVIDERS: frozenset[str] = frozenset(
    {
        "codex_cli",
        "claude_code_cli",
        "claude_code",
        "claude",
        "gemini_cli",
        "gemini",
        "omp_cli",
        "opencode_cli",
        "opencode",
        "antigravity_cli",
        "antigravity",
        "agy",
        "openrouter_http",
        "openai_http",
        "opencode_subscription_http",
        "openai_codex_subscription_http",
        "openai_codex_http",
        "chatgpt_subscription_http",
        "pi_cli",
        "mock",
    }
)

WORKER_PROVIDER_OVERRIDES: dict[str, tuple[str, str, str | None]] = {}
"""Explicit ``worker_id -> (provider, model, effort)`` pins.

An entry here wins over parsing the worker's command string, so an unusual
worker can be routed correctly without editing the (Auto-Orch-owned) crews
file. Empty by default.
"""

NEVER_AUTOMATIC_MODELS = frozenset(
    {
        "claude-fable-5-1",
        "claude-fable-5",
        "gpt-5.6-luna",
        "claude-opus-5",
    }
)
"""Models that must never appear in an *automatic* fallback chain.

Per the crew-aware worker-resolver plan: Fable 5.1, Luna Max, and Opus 5 are
expensive escalation rungs that a resolver may only reach when a crew names
them explicitly. This constant is declarative only in strict mode — strict
mode returns exactly the crew's named worker, so nothing here is enforced by
:class:`~lee_llm_router.policy.CrewRoutingPolicy` yet.
"""

ROLE_SCOPED_MODELS = frozenset({"gemini-3.1-pro"})
"""Models restricted to specific role classes (decisions.md D188)."""

ROLE_SCOPED_CITATION = "decisions.md D188"
"""The authority a role-scoped refusal or skip must cite."""

CHANNELS: tuple[str, ...] = (
    "openai-sub",
    "anthropic-sub",
    "gemini-sub",
    "gemini-sub-thirdparty",
    "openrouter",
    "opencode-go",
)
"""The funding channels a worker's usage can be drawn from.

A *channel* is the subscription or metered account that pays for a worker's
tokens. It is deliberately coarser than a provider: several providers can draw
on one channel, and one harness (Antigravity) draws on two -- Google's own
models bill to ``gemini-sub`` while the third-party Claude/GPT models it
brokers bill to ``gemini-sub-thirdparty``.

This mapping lives here rather than in ``crews.yaml`` because funding is the
router's concern, not Auto-Orch's.
"""

HARNESS_PROVIDER_CHANNELS: dict[str, str] = {
    "openai-codex": "openai-sub",
    "openrouter": "openrouter",
    "opencode-go": "opencode-go",
    "anthropic": "anthropic-sub",
}
"""Funding channel for each Pi/OMP harness provider id.

The Pi and OMP (oh-my-pi) harnesses select their backend through a provider id
(the ``--provider`` value a stage worker declares via its
``<PREFIX>_STAGE_WORKER_PROVIDER`` environment variable). Because several of
those ids draw on different funding channels from the same harness, the channel
is per-worker, not per-harness: a ``pi_cli``/``omp_cli`` worker bills to the
channel of the provider id its own command declares.
"""

UNKNOWN_CHANNEL = "unknown"
"""Fail-open channel for workers whose funding channel cannot be inferred.

Per decisions D86/D87 (docs/staffing/chief-answers-5.md): a ``pi_cli`` or
``omp_cli`` worker that declares no (or an unrecognized) stage-worker provider
id still resolves normally, but bills to ``unknown``. Availability treats
``unknown`` as never healthy, and strict mode must not halt a governed run on
a dead lookup, so this is a sentinel rather than a member of :data:`CHANNELS`.
"""

WORKER_DERIVED_CHANNEL_PROVIDERS: frozenset[str] = frozenset({"pi_cli", "omp_cli"})
"""Registered providers whose funding channel is derived per worker.

These harnesses are recognized by :data:`PROVIDER_CHANNELS`, but instead of a
static default they resolve through :data:`HARNESS_PROVIDER_CHANNELS` from the
worker's own provider environment. A worker for one of these providers that
declares no (or an unrecognized) provider id fails open to
:data:`UNKNOWN_CHANNEL` instead of raising (decisions D86/D87).
"""

PROVIDER_CHANNELS: dict[str, str | Mapping[str, str]] = {
    "codex_cli": "openai-sub",
    "claude_code_cli": "anthropic-sub",
    "antigravity_cli": "gemini-sub",
    "opencode_cli": "opencode-go",
    "omp_cli": HARNESS_PROVIDER_CHANNELS,
    "pi_cli": HARNESS_PROVIDER_CHANNELS,
}
"""Funding channel for each registered router provider.

A plain string value is the provider's default channel. A mapping value (the
Pi/OMP harnesses) is the per-worker derivation table keyed by the provider id
the worker declares in its stage-worker environment; those providers have no
static default and fail open to :data:`UNKNOWN_CHANNEL` without one.
"""

WORKER_CHANNEL_OVERRIDES: dict[str, str] = {}
"""Explicit ``worker_id -> channel`` pins that beat :data:`PROVIDER_CHANNELS`.

Use this for a worker whose funding differs from its provider's default -- for
example an Antigravity worker running a brokered Claude or GPT model, which
bills to ``gemini-sub-thirdparty`` rather than ``gemini-sub``. Empty by
default.
"""


def channel_for(
    worker_id: str, provider: str, harness_provider: str | None = None
) -> str:
    """Return the funding channel a worker's usage bills to.

    Precedence: an explicit :data:`WORKER_CHANNEL_OVERRIDES` pin wins over
    everything. Otherwise a provider with a plain string entry in
    :data:`PROVIDER_CHANNELS` bills to that default, while a worker-derived
    provider (:data:`WORKER_DERIVED_CHANNEL_PROVIDERS`, i.e. ``pi_cli`` and
    ``omp_cli``) bills to :data:`HARNESS_PROVIDER_CHANNELS[harness_provider]`.

    For the worker-derived providers a missing or unrecognized
    ``harness_provider`` fails open to :data:`UNKNOWN_CHANNEL` (decisions
    D86/D87): channel inference never raises for ``pi_cli``/``omp_cli``.

    Args:
        worker_id: The worker id from the crews file.
        provider: The registered router provider the worker resolves to.
        harness_provider: The provider id the worker's own environment
            declares (``<PREFIX>_STAGE_WORKER_PROVIDER``), for the
            worker-derived harness providers.

    Returns:
        One of :data:`CHANNELS`, or :data:`UNKNOWN_CHANNEL` when a
        worker-derived provider's funding channel cannot be inferred.

    Raises:
        CrewsConfigError: If neither an override nor a provider default maps
            the worker to a known channel.
    """
    override = WORKER_CHANNEL_OVERRIDES.get(worker_id)
    if override is not None:
        if override not in CHANNELS:
            raise CrewsConfigError(
                f"worker {worker_id!r} is pinned to unknown channel "
                f"{override!r} (known channels: {', '.join(CHANNELS)})"
            )
        return override
    try:
        entry: str | Mapping[str, str] = PROVIDER_CHANNELS[provider]
    except KeyError as exc:
        known = ", ".join(sorted(PROVIDER_CHANNELS))
        raise CrewsConfigError(
            f"worker {worker_id!r} resolves to provider {provider!r}, which has "
            f"no funding channel (mapped providers: {known})"
        ) from exc
    if isinstance(entry, str):
        if entry not in CHANNELS:  # pragma: no cover - guards a bad edit here
            raise CrewsConfigError(
                f"provider {provider!r} maps to unknown channel {entry!r}"
            )
        return entry
    # Worker-derived channel (pi_cli / omp_cli): the worker's own provider
    # environment names the underlying Pi/OMP provider id that pays. Per
    # decisions D86/D87 this inference fails OPEN to UNKNOWN_CHANNEL — a
    # missing or unrecognized provider id never raises, it just leaves the
    # worker with a channel availability treats as never healthy.
    if harness_provider is None or harness_provider not in entry:
        return UNKNOWN_CHANNEL
    channel = entry[harness_provider]
    if channel not in CHANNELS:  # pragma: no cover - guards a bad edit here
        raise CrewsConfigError(
            f"stage-worker provider {harness_provider!r} maps to unknown "
            f"channel {channel!r}"
        )
    return channel


_WORKER_ENV_PATTERN: str = (
    r"(?P<prefix>[A-Z]+)_STAGE_WORKER_"
    r"(?P<key>REASONING_EFFORT|EFFORT|THINKING|BINARY|MODEL|PROVIDER)="
    r"(?P<value>\S+)"
)
_worker_env_re_cached: Any = None


def _get_worker_env_re() -> Any:
    global _worker_env_re_cached
    if _worker_env_re_cached is None:
        import re

        _worker_env_re_cached = re.compile(_WORKER_ENV_PATTERN)
    return _worker_env_re_cached


def is_never_automatic(model: str | None) -> bool:
    """Return whether a model may only be used when explicitly named.

    Args:
        model: Model identifier, or ``None``.

    Returns:
        ``True`` if the model is in :data:`NEVER_AUTOMATIC_MODELS`.
    """
    return model is not None and model in NEVER_AUTOMATIC_MODELS


def is_role_scoped(model: str | None) -> bool:
    """Return whether a model is restricted to specific role classes.

    Args:
        model: Model identifier, or ``None``.

    Returns:
        ``True`` if the model is in :data:`ROLE_SCOPED_MODELS`.
    """
    return model is not None and model in ROLE_SCOPED_MODELS


def role_class(role: str) -> str:
    """Return the role class ('coding' or 'planning_review') for a role name.

    Args:
        role: Role name to look up.

    Returns:
        The role class string ('coding' or 'planning_review').

    Raises:
        CrewsConfigError: If the role is not in :data:`ROLE_CLASS_BY_ROLE`,
            naming the role and the known roles.
    """
    try:
        return ROLE_CLASS_BY_ROLE[role]
    except KeyError as exc:
        known = ", ".join(sorted(ROLE_CLASS_BY_ROLE))
        raise CrewsConfigError(
            f"unknown role {role!r} (known roles: {known}; {ROLE_SCOPED_CITATION})"
        ) from exc


@dataclass(frozen=True)
class ResolvedWorker:
    """A crews-file worker mapped onto a router provider.

    Attributes:
        worker_id: The worker id from the crews file.
        provider: Registered router provider name (e.g. ``codex_cli``).
        model: Model identifier the worker runs.
        effort: Reasoning-effort hint, when the worker declares one.
        harness_binary: Path to the harness binary, when declared.
        dispatch_command: The worker's raw command template, unchanged.
        channel: The funding channel this worker's usage bills to, or
            :data:`UNKNOWN_CHANNEL` when it cannot be inferred (Pi/OMP
            workers with a missing or unrecognized provider signal).
    """

    worker_id: str
    provider: str
    model: str
    effort: str | None
    harness_binary: str | None
    dispatch_command: str
    channel: str


def _parse_worker_command(
    worker: Worker,
) -> tuple[str, str, str | None, str | None, str | None]:
    """Parse ``(prefix, model, effort, binary, provider)`` from a command.

    ``provider`` is the value of the worker's ``<PREFIX>_STAGE_WORKER_PROVIDER``
    environment variable (the Pi/OMP ``--provider`` selection), or ``None``
    when the command declares none.
    """
    prefix: str | None = None
    fields: dict[str, str] = {}
    for match in _get_worker_env_re().finditer(worker.command):
        found = match.group("prefix")
        if prefix is None:
            prefix = found
        elif found != prefix:
            raise CrewsConfigError(
                f"worker {worker.id!r} mixes stage-worker env prefixes "
                f"{prefix!r} and {found!r} in its command"
            )
        key = match.group("key")
        # ``REASONING_EFFORT``, ``EFFORT``, and Pi/OMP's ``THINKING`` dial are
        # all effort signals. The first one declared in the command wins
        # (setdefault), and conflicting spellings never raise — the same
        # first-wins, fail-open convention as every other env key.
        if key.endswith("EFFORT") or key == "THINKING":
            fields.setdefault("EFFORT", match["value"])
        else:
            fields.setdefault(key, match["value"])
    if prefix is None:
        raise CrewsConfigError(
            f"worker {worker.id!r} command declares no "
            f"<PREFIX>_STAGE_WORKER_* environment variables: {worker.command!r}"
        )
    if prefix not in WORKER_ENV_PREFIX_PROVIDERS:
        known = ", ".join(sorted(WORKER_ENV_PREFIX_PROVIDERS))
        raise CrewsConfigError(
            f"worker {worker.id!r} uses unknown stage-worker prefix {prefix!r} "
            f"(known prefixes: {known})"
        )
    model = fields.get("MODEL")
    if not model:
        raise CrewsConfigError(
            f"worker {worker.id!r} command declares no " f"{prefix}_STAGE_WORKER_MODEL"
        )
    return (
        prefix,
        model,
        fields.get("EFFORT"),
        fields.get("BINARY"),
        fields.get("PROVIDER"),
    )


def resolve_worker(worker: Worker) -> ResolvedWorker:
    """Map a crews-file worker onto a registered router provider.

    The mapping is derived from the worker's command template, which sets
    ``<PREFIX>_STAGE_WORKER_BINARY``/``_MODEL``/``_EFFORT`` (or
    ``_REASONING_EFFORT``, or the Pi/OMP ``_THINKING`` dial) — and, for the
    Pi/OMP harnesses, the ``_PROVIDER`` id that selects the backend — before
    invoking the stage-worker script. An entry in
    :data:`WORKER_PROVIDER_OVERRIDES` wins over that parse.

    Args:
        worker: The worker to resolve.

    Returns:
        The :class:`ResolvedWorker` for this worker.

    Raises:
        CrewsConfigError: If the command uses an unknown prefix, declares no
            model, resolves to a provider that is not registered, or resolves
            to a provider with no funding channel. For the worker-derived
            channel providers (``pi_cli``/``omp_cli``) a missing or unknown
            ``<PREFIX>_STAGE_WORKER_PROVIDER`` environment value is not an
            error: the worker resolves normally with channel
            :data:`UNKNOWN_CHANNEL` (decisions D86/D87, fail open).
    """
    override = WORKER_PROVIDER_OVERRIDES.get(worker.id)
    if override is not None:
        provider, model, effort = override
        binary: str | None = None
        provider_env: str | None = None
    else:
        prefix, model, effort, binary, provider_env = _parse_worker_command(worker)
        provider = WORKER_ENV_PREFIX_PROVIDERS[prefix]

    if provider not in _BUILTIN_REGISTERED_PROVIDERS:
        from lee_llm_router.providers import registry

        try:
            registry.get(provider)
        except KeyError as exc:
            raise CrewsConfigError(
                f"worker {worker.id!r} resolves to provider {provider!r}, which is "
                f"not registered (available: {', '.join(registry.available())})"
            ) from exc

    return ResolvedWorker(
        worker_id=worker.id,
        provider=provider,
        model=model,
        effort=effort,
        harness_binary=binary,
        dispatch_command=worker.command,
        channel=channel_for(worker.id, provider, harness_provider=provider_env),
    )
