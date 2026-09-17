"""LLMRequest and LLMResponse dataclasses."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMRequest:
    """Represents a single completion request passed to a provider."""

    role: str
    messages: list[dict[str, str]]
    model: str = ""
    temperature: float = 0.2
    json_mode: bool = False
    max_tokens: int | None = None
    timeout: float = 60.0
    effort: str | None = None
    schema: dict[str, Any] | None = None
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    workspace: str | None = None
    work_package_id: str | None = None


@dataclass
class LLMUsage:
    """Token usage reported by a provider."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMResponse:
    """Represents the result of a completion call."""

    text: str
    raw: dict[str, Any] = field(default_factory=dict)
    usage: LLMUsage = field(default_factory=LLMUsage)
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    model: str = ""
    provider: str = ""
