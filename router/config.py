"""YAML config loader + dataclasses + env var interpolation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when a config file is missing, malformed, or invalid."""


@dataclass
class ProviderConfig:
    """Config for a single provider entry."""

    name: str  # YAML key (e.g. "openrouter")
    type: str  # registry key (e.g. "openrouter_http")
    raw: dict[str, Any] = field(default_factory=dict)  # everything except "type"


@dataclass
class RoleConfig:
    """Config for a single role entry."""

    name: str  # YAML key (e.g. "planner")
    provider: str  # references a key in LLMConfig.providers
    model: str = ""
    temperature: float = 0.2
    json_mode: bool = False
    max_tokens: int | None = None
    timeout: float = 60.0
    fallback_providers: list[str] = field(default_factory=list)


@dataclass
class LLMConfig:
    """Top-level config object produced by load_config()."""

    default_role: str
    providers: dict[str, ProviderConfig]
    roles: dict[str, RoleConfig]


def load_config(path: str | Path) -> LLMConfig:
    """Load and validate LLM config from a YAML file.

    Config format::

        llm:
          default_role: planner
          providers:
            openrouter:
              type: openrouter_http
              base_url: https://openrouter.ai/api/v1
              api_key_env: OPENROUTER_API_KEY
          roles:
            planner:
              provider: openrouter
              model: gpt-4o
              temperature: 0.2

    Raises:
        ConfigError: if the file is missing, invalid YAML, or fails validation.
    """
    path = Path(path)
    try:
        with open(path) as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}")
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}")

    if not isinstance(data, dict) or "llm" not in data:
        raise ConfigError("Config must have a top-level 'llm' key")

    llm = data["llm"]

    if "default_role" not in llm:
        raise ConfigError("Config missing required field: llm.default_role")
    if not llm.get("providers"):
        raise ConfigError("Config missing required field: llm.providers")
    if not llm.get("roles"):
        raise ConfigError("Config missing required field: llm.roles")

    providers: dict[str, ProviderConfig] = {}
    for pname, pcfg in llm["providers"].items():
        if not isinstance(pcfg, dict) or "type" not in pcfg:
            raise ConfigError(f"Provider {pname!r} missing required field: 'type'")
        providers[pname] = ProviderConfig(
            name=pname,
            type=pcfg["type"],
            raw={k: v for k, v in pcfg.items() if k != "type"},
        )

    roles: dict[str, RoleConfig] = {}
    for rname, rcfg in llm["roles"].items():
        if not isinstance(rcfg, dict) or "provider" not in rcfg:
            raise ConfigError(f"Role {rname!r} missing required field: 'provider'")
        if rcfg["provider"] not in providers:
            raise ConfigError(
                f"Role {rname!r} references unknown provider: {rcfg['provider']!r}"
            )
        fallback_providers = list(rcfg.get("fallback_providers", []))
        for fallback_provider in fallback_providers:
            if fallback_provider not in providers:
                raise ConfigError(
                    f"Role {rname!r} references unknown fallback provider: "
                    f"{fallback_provider!r}"
                )
        roles[rname] = RoleConfig(
            name=rname,
            provider=rcfg["provider"],
            model=rcfg.get("model", ""),
            temperature=float(rcfg.get("temperature", 0.2)),
            json_mode=bool(rcfg.get("json_mode", False)),
            max_tokens=rcfg.get("max_tokens"),
            timeout=float(rcfg.get("timeout", 60.0)),
            fallback_providers=fallback_providers,
        )

    if llm["default_role"] not in roles:
        raise ConfigError(
            f"Config default_role references unknown role: {llm['default_role']!r}"
        )

    return LLMConfig(
        default_role=llm["default_role"],
        providers=providers,
        roles=roles,
    )
