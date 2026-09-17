"""Provider registration and discovery."""

from __future__ import annotations

from typing import Iterable

_REGISTRY: dict[str, type] = {}
_ALIASES: dict[str, str] = {}


def register(
    name: str, provider_cls: type, *, aliases: Iterable[str] | None = None
) -> None:
    """Register a provider class under the given name."""
    _REGISTRY[name] = provider_cls
    if aliases:
        for alias in aliases:
            _ALIASES[alias] = name


def register_alias(alias: str, target: str) -> None:
    """Register an additional lookup alias for an existing provider name."""
    _ALIASES[alias] = target


def get(name: str) -> type:
    """Retrieve a registered provider class by name or alias.

    Raises KeyError if the provider is not registered.
    """
    canonical = _ALIASES.get(name, name)
    if canonical not in _REGISTRY:
        raise KeyError(f"Provider {name!r} not registered. Available: {available()}")
    return _REGISTRY[canonical]


def available() -> list[str]:
    """Return a list of all registered provider names and aliases."""
    return sorted(set(_REGISTRY.keys()) | set(_ALIASES.keys()))


def _register_builtins() -> None:
    """Auto-register built-in provider adapters."""
    from lee_llm_router.providers.antigravity_cli import AntigravityCLIProvider
    from lee_llm_router.providers.codex_cli import (
        ClaudeCodeCLIProvider,
        CodexCLIProvider,
        GeminiCLIProvider,
    )
    from lee_llm_router.providers.http import OpenRouterHTTPProvider
    from lee_llm_router.providers.mock import MockProvider
    from lee_llm_router.providers.omp_cli import OmpCLIProvider
    from lee_llm_router.providers.openai_codex_subscription import (
        OpenAICodexSubscriptionHTTPProvider,
    )
    from lee_llm_router.providers.opencode_cli import OpenCodeCLIProvider

    register("mock", MockProvider)
    register("openrouter_http", OpenRouterHTTPProvider, aliases=("openai_http",))
    register(
        "openai_codex_subscription_http",
        OpenAICodexSubscriptionHTTPProvider,
        aliases=("openai_codex_http", "chatgpt_subscription_http"),
    )
    register_alias("opencode_subscription_http", "openrouter_http")
    register("codex_cli", CodexCLIProvider)
    register("gemini_cli", GeminiCLIProvider, aliases=("gemini",))
    register(
        "claude_code_cli",
        ClaudeCodeCLIProvider,
        aliases=("claude_code", "claude"),
    )
    register("omp_cli", OmpCLIProvider)
    register("opencode_cli", OpenCodeCLIProvider, aliases=("opencode",))
    register(
        "antigravity_cli",
        AntigravityCLIProvider,
        aliases=("antigravity", "agy"),
    )


_register_builtins()
