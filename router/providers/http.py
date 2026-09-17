"""OpenRouter / OpenAI-compatible REST provider.

Uses httpx for both sync and async HTTP (Phase 2).

``httpx`` is imported lazily, inside :meth:`OpenRouterHTTPProvider.complete`
and :meth:`OpenRouterHTTPProvider.complete_async`, so that importing this
module (via provider registration) does not pull ``httpx`` into a cold-start
consumer that only resolves a CLI worker. See
``docs/crew-resolver/sprint-plan.md`` Sprint 3's "under 50 ms" done
condition.

Module-level ``__getattr__`` (PEP 562) makes ``lee_llm_router.providers.
http.httpx`` resolve to the real ``httpx`` module on attribute access —
without importing it at module-import time — so ``unittest.mock.patch``
targets like ``"lee_llm_router.providers.http.httpx.Client"`` keep working:
patch's dotted-path lookup does a ``getattr`` on this module to find
``httpx`` before patching an attribute on it.
"""

from __future__ import annotations

import os
from typing import Any

from lee_llm_router.providers.base import FailureType, LLMRouterError
from lee_llm_router.response import LLMRequest, LLMResponse, LLMUsage


def __getattr__(name: str) -> Any:
    if name == "httpx":
        import httpx

        globals()["httpx"] = httpx
        return httpx
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _build_request_parts(
    request: LLMRequest, config: dict[str, Any]
) -> tuple[str, dict[str, str], dict[str, Any], float]:
    """Return (url, headers, payload, timeout) — shared by sync and async."""
    base_url = config.get("base_url", "https://openrouter.ai/api/v1")
    api_key_env = config.get("api_key_env", "OPENROUTER_API_KEY")
    api_key = os.environ.get(api_key_env, "")
    timeout = request.timeout or float(config.get("timeout", 60.0))

    headers: dict[str, str] = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    headers.update(config.get("headers", {}))

    payload: dict[str, Any] = {
        "model": request.model,
        "messages": request.messages,
        "temperature": request.temperature,
    }
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens
    if request.json_mode:
        payload["response_format"] = {"type": "json_object"}

    return f"{base_url}/chat/completions", headers, payload, timeout


def _parse_response(resp_data: dict[str, Any], request: LLMRequest) -> LLMResponse:
    """Parse a /chat/completions JSON body into LLMResponse."""
    try:
        text = resp_data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise LLMRouterError(
            f"Unexpected response structure: {exc}",
            failure_type=FailureType.INVALID_RESPONSE,
            cause=exc,
        ) from exc

    usage_data = resp_data.get("usage", {})
    return LLMResponse(
        text=text,
        raw=resp_data,
        usage=LLMUsage(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
        ),
        request_id=request.request_id,
        model=resp_data.get("model", request.model),
        provider="openrouter_http",
    )


def _check_status(status_code: int, text: str) -> None:
    if status_code == 429:
        raise LLMRouterError(
            "Rate limited by provider", failure_type=FailureType.RATE_LIMIT
        )
    if status_code >= 400:
        raise LLMRouterError(
            f"Provider returned HTTP {status_code}: {text[:200]}",
            failure_type=FailureType.PROVIDER_ERROR,
        )


class OpenRouterHTTPProvider:
    """Generic HTTP provider for OpenRouter and OpenAI-compatible APIs."""

    name = "openrouter_http"
    supported_types = {"openrouter_http", "openai_http"}

    def validate_config(self, config: dict[str, Any]) -> None:
        for key in ("base_url", "api_key_env"):
            if key not in config:
                raise LLMRouterError(
                    f"HTTP provider missing required config key: {key!r}",
                    failure_type=FailureType.PROVIDER_ERROR,
                )

    def complete(self, request: LLMRequest, config: dict[str, Any]) -> LLMResponse:
        import httpx

        url, headers, payload, timeout = _build_request_parts(request, config)
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMRouterError(
                f"Request timed out after {timeout}s",
                failure_type=FailureType.TIMEOUT,
                cause=exc,
            ) from exc
        except httpx.RequestError as exc:
            raise LLMRouterError(
                f"HTTP request failed: {exc}",
                failure_type=FailureType.PROVIDER_ERROR,
                cause=exc,
            ) from exc

        _check_status(resp.status_code, resp.text)
        return _parse_response(resp.json(), request)

    async def complete_async(
        self, request: LLMRequest, config: dict[str, Any]
    ) -> LLMResponse:
        import httpx

        url, headers, payload, timeout = _build_request_parts(request, config)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMRouterError(
                f"Request timed out after {timeout}s",
                failure_type=FailureType.TIMEOUT,
                cause=exc,
            ) from exc
        except httpx.RequestError as exc:
            raise LLMRouterError(
                f"HTTP request failed: {exc}",
                failure_type=FailureType.PROVIDER_ERROR,
                cause=exc,
            ) from exc

        _check_status(resp.status_code, resp.text)
        return _parse_response(resp.json(), request)
