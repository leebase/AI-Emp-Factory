"""Deterministic echo provider for tests and CI."""

from __future__ import annotations

from typing import Any

from lee_llm_router.providers.base import FailureType, LLMRouterError
from lee_llm_router.response import LLMRequest, LLMResponse, LLMUsage


class MockProvider:
    """Returns configurable fixed text. No I/O — safe for all tests."""

    name = "mock"
    supported_types = {"mock"}

    def validate_config(self, config: dict[str, Any]) -> None:
        pass  # No required config keys

    def complete(self, request: LLMRequest, config: dict[str, Any]) -> LLMResponse:
        if config.get("raise_timeout"):
            raise LLMRouterError("mock timeout", failure_type=FailureType.TIMEOUT)

        if config.get("raise_contract_violation"):
            raise LLMRouterError(
                "mock contract violation",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )

        if config.get("raise_rate_limit"):
            raise LLMRouterError("mock rate limit", failure_type=FailureType.RATE_LIMIT)

        text = config.get("response_text", f"mock response for role={request.role}")

        return LLMResponse(
            text=text,
            raw={"mock": True, "role": request.role},
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            request_id=request.request_id,
            model=request.model or "mock-model",
            provider="mock",
        )

    async def complete_async(
        self, request: LLMRequest, config: dict[str, Any]
    ) -> LLMResponse:
        if config.get("raise_timeout"):
            raise LLMRouterError("mock timeout", failure_type=FailureType.TIMEOUT)

        if config.get("raise_contract_violation"):
            raise LLMRouterError(
                "mock contract violation",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )

        if config.get("raise_rate_limit"):
            raise LLMRouterError("mock rate limit", failure_type=FailureType.RATE_LIMIT)

        text = config.get("response_text", f"mock response for role={request.role}")

        return LLMResponse(
            text=text,
            raw={"mock": True, "role": request.role},
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            request_id=request.request_id,
            model=request.model or "mock-model",
            provider="mock",
        )
