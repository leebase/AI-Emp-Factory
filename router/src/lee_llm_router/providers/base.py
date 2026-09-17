"""Provider Protocol + exception types."""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from lee_llm_router.response import LLMRequest, LLMResponse


class FailureType(enum.Enum):
    """Classification of provider failures.

    Used to drive retry logic — CONTRACT_VIOLATION must never be retried.
    """

    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    CONTRACT_VIOLATION = "CONTRACT_VIOLATION"  # schema/JSON parse failure — never retry
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class LLMRouterError(Exception):
    """All provider errors are wrapped in this exception."""

    def __init__(
        self,
        message: str,
        failure_type: FailureType = FailureType.UNKNOWN,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_type = failure_type
        self.cause = cause


def should_retry(error: LLMRouterError) -> bool:
    """Return False for failure types that must never be retried.

    CONTRACT_VIOLATION (schema mismatch, JSON parse failure) indicates a
    prompt/contract problem — retrying will produce the same failure.
    """
    return error.failure_type != FailureType.CONTRACT_VIOLATION


@runtime_checkable
class Provider(Protocol):
    """Interface that all provider adapters must satisfy."""

    name: str
    supported_types: set[str]

    def validate_config(self, config: dict[str, Any]) -> None:
        """Raise LLMRouterError if required config keys are missing or invalid."""
        ...

    def complete(self, request: LLMRequest, config: dict[str, Any]) -> LLMResponse:
        """Execute a completion. Raise LLMRouterError on any failure."""
        ...

    # complete_async is optional — providers that don't implement it fall back
    # to asyncio.to_thread(self.complete, ...) in the router.
