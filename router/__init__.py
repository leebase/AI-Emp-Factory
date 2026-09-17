"""Shared provider and response types for the staffing service.

The current command surface is implemented by :mod:`lee_llm_router.doctor`:
``staff`` selects a staffing block and ``run`` dispatches one recorded attempt.
Only provider-facing response/error shapes remain as package-level lazy
exports; staffing, availability, crews, events, and shims are imported from
their explicit modules.
"""

from __future__ import annotations

import importlib
from typing import Any

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "LLMRequest",
    "LLMResponse",
    "LLMUsage",
    "LLMRouterError",
    "FailureType",
]

# Maps each remaining package-level name to the submodule that defines it.
# Accessing ``lee_llm_router.<name>`` imports only that module on first use,
# preserving a small import surface for the staffing CLI.
_ATTR_MODULES: dict[str, str] = {
    "LLMRequest": "lee_llm_router.response",
    "LLMResponse": "lee_llm_router.response",
    "LLMUsage": "lee_llm_router.response",
    "LLMRouterError": "lee_llm_router.providers.base",
    "FailureType": "lee_llm_router.providers.base",
}


def __getattr__(name: str) -> Any:
    """Lazily resolve a public name to its defining submodule (PEP 562)."""
    module_name = _ATTR_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_name)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(globals()))
