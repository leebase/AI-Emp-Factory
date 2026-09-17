"""OpenAI Codex subscription provider via ChatGPT backend API.

This adapter mirrors OpenClaw's credential discovery strategy:
1) access_token_env (if configured)
2) macOS keychain (service: "Codex Auth")
3) CODEX_HOME/auth.json (or ~/.codex/auth.json)

``httpx`` is imported lazily, inside
:meth:`OpenAICodexSubscriptionHTTPProvider.complete` and
:meth:`OpenAICodexSubscriptionHTTPProvider.complete_async`, so that importing
this module (via provider registration) does not pull ``httpx`` into a
cold-start consumer that only resolves a CLI worker. The ``httpx.Response``
type hint in :func:`_collect_stream_events` is unevaluated at runtime thanks
to ``from __future__ import annotations``. See
``docs/crew-resolver/sprint-plan.md`` Sprint 3's "under 50 ms" done
condition.

Module-level ``__getattr__`` (PEP 562) makes ``lee_llm_router.providers.
openai_codex_subscription.httpx`` resolve to the real ``httpx`` module on
attribute access — without importing it at module-import time — so
``unittest.mock.patch`` targets like ``"lee_llm_router.providers.
openai_codex_subscription.httpx.Client"`` keep working: patch's dotted-path
lookup does a ``getattr`` on this module to find ``httpx`` before patching an
attribute on it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lee_llm_router.providers.base import FailureType, LLMRouterError
from lee_llm_router.response import LLMRequest, LLMResponse, LLMUsage

if TYPE_CHECKING:
    import httpx


def __getattr__(name: str) -> Any:
    if name == "httpx":
        import httpx

        globals()["httpx"] = httpx
        return httpx
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _resolve_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME", "~/.codex")
    return Path(configured).expanduser()


def _compute_keychain_account(codex_home: Path) -> str:
    resolved = str(codex_home.resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
    return f"cli|{digest[:16]}"


def _read_codex_keychain_credentials(codex_home: Path) -> tuple[str, str | None] | None:
    if sys.platform != "darwin":
        return None

    account = _compute_keychain_account(codex_home)
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                "Codex Auth",
                "-a",
                account,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except Exception:
        return None

    try:
        data = json.loads(result.stdout.strip())
        tokens = data.get("tokens", {})
        access_token = str(tokens.get("access_token", "")).strip()
        if not access_token:
            return None
        account_id = tokens.get("account_id")
        if isinstance(account_id, str) and account_id.strip():
            return access_token, account_id.strip()
        return access_token, None
    except Exception:
        return None


def _read_codex_auth_file(codex_home: Path) -> tuple[str, str | None] | None:
    auth_path = codex_home / "auth.json"
    if not auth_path.exists():
        return None

    try:
        data = json.loads(auth_path.read_text())
        tokens = data.get("tokens", {})
        access_token = str(tokens.get("access_token", "")).strip()
        if not access_token:
            return None
        account_id = tokens.get("account_id")
        if isinstance(account_id, str) and account_id.strip():
            return access_token, account_id.strip()
        return access_token, None
    except Exception:
        return None


def _resolve_credentials(config: dict[str, Any]) -> tuple[str, str | None]:
    access_token_env = config.get("access_token_env")
    if isinstance(access_token_env, str) and access_token_env.strip():
        token = os.environ.get(access_token_env, "").strip()
        if not token:
            raise LLMRouterError(
                "Codex subscription access token env var is unset: "
                f"{access_token_env!r}",
                failure_type=FailureType.PROVIDER_ERROR,
            )

        account_id = config.get("account_id")
        if isinstance(account_id, str) and account_id.strip():
            return token, account_id.strip()

        account_id_env = config.get("account_id_env")
        if isinstance(account_id_env, str) and account_id_env.strip():
            env_account_id = os.environ.get(account_id_env, "").strip()
            if env_account_id:
                return token, env_account_id

        return token, None

    codex_home = _resolve_codex_home()
    creds = _read_codex_keychain_credentials(codex_home) or _read_codex_auth_file(
        codex_home
    )
    if creds is None:
        raise LLMRouterError(
            "No Codex subscription credentials found. Set access_token_env "
            "or run `codex login`.",
            failure_type=FailureType.PROVIDER_ERROR,
        )

    token, discovered_account_id = creds

    configured_account_id = config.get("account_id")
    if isinstance(configured_account_id, str) and configured_account_id.strip():
        return token, configured_account_id.strip()

    account_id_env = config.get("account_id_env")
    if isinstance(account_id_env, str) and account_id_env.strip():
        env_account_id = os.environ.get(account_id_env, "").strip()
        if env_account_id:
            return token, env_account_id

    return token, discovered_account_id


def _build_endpoint(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/responses"):
        return normalized
    if normalized.endswith("/codex"):
        return f"{normalized}/responses"
    if normalized.endswith("/backend-api"):
        return f"{normalized}/codex/responses"
    return f"{normalized}/responses"


def _build_request_parts(
    request: LLMRequest, config: dict[str, Any]
) -> tuple[str, dict[str, str], dict[str, Any], float]:
    base_url = str(config.get("base_url", "https://chatgpt.com/backend-api/codex"))
    timeout = request.timeout or float(config.get("timeout", 60.0))
    access_token, account_id = _resolve_credentials(config)

    headers: dict[str, str] = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    headers.update(config.get("headers", {}))

    instructions_parts: list[str] = []
    input_messages: list[dict[str, Any]] = []
    for message in request.messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "")).strip().lower()
        content = message.get("content")
        if role == "system" and isinstance(content, str) and content.strip():
            instructions_parts.append(content.strip())
            continue
        input_messages.append(message)

    instructions = "\n\n".join(instructions_parts).strip()
    if not instructions:
        instructions = str(config.get("instructions") or "You are a helpful assistant.")
    if not input_messages:
        input_messages = [{"role": "user", "content": ""}]

    payload: dict[str, Any] = {
        "model": request.model,
        "instructions": instructions,
        "input": input_messages,
        "stream": True,
        # ChatGPT backend Codex responses require store=false.
        "store": False,
    }
    if request.max_tokens is not None:
        payload["max_output_tokens"] = request.max_tokens
    if request.json_mode:
        payload["text"] = {"format": {"type": "json_object"}}

    return _build_endpoint(base_url), headers, payload, timeout


def _iter_sse_events(chunks: Iterable[str]) -> Iterable[dict[str, Any]]:
    buffer = ""
    for chunk in chunks:
        if not chunk:
            continue
        buffer += chunk.replace("\r\n", "\n").replace("\r", "\n")
        while "\n\n" in buffer:
            raw_event, buffer = buffer.split("\n\n", 1)
            data_lines = [
                line[5:].strip()
                for line in raw_event.splitlines()
                if line.startswith("data:")
            ]
            if not data_lines:
                continue
            data = "\n".join(data_lines).strip()
            if not data:
                continue
            if data == "[DONE]":
                return
            try:
                event = json.loads(data)
            except json.JSONDecodeError as exc:
                raise LLMRouterError(
                    f"Invalid SSE event payload: {data[:200]}",
                    failure_type=FailureType.INVALID_RESPONSE,
                    cause=exc,
                ) from exc
            if isinstance(event, dict):
                yield event
                event_type = str(event.get("type", "")).strip()
                if event_type in {"response.done", "response.completed"}:
                    return

    trailing = buffer.strip()
    if trailing:
        data_lines = [
            line[5:].strip()
            for line in trailing.splitlines()
            if line.startswith("data:")
        ]
        if data_lines:
            data = "\n".join(data_lines).strip()
            if not data or data == "[DONE]":
                return
            try:
                event = json.loads(data)
            except json.JSONDecodeError as exc:
                raise LLMRouterError(
                    f"Invalid trailing SSE event payload: {data[:200]}",
                    failure_type=FailureType.INVALID_RESPONSE,
                    cause=exc,
                ) from exc
            if isinstance(event, dict):
                yield event


def _collect_stream_events(
    resp: httpx.Response, timeout: float
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    chunks: list[str] = []
    for chunk in resp.iter_text():
        if time.monotonic() > deadline:
            raise LLMRouterError(
                f"Request timed out after {timeout}s",
                failure_type=FailureType.TIMEOUT,
            )
        chunks.append(chunk)
    if time.monotonic() > deadline:
        raise LLMRouterError(
            f"Request timed out after {timeout}s",
            failure_type=FailureType.TIMEOUT,
        )
    return list(_iter_sse_events(chunks))


def _extract_text(resp_data: dict[str, Any]) -> str:
    output_text = resp_data.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text

    output = resp_data.get("output")
    collected: list[str] = []

    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue

            item_text = item.get("text")
            if isinstance(item_text, str) and item_text:
                collected.append(item_text)

            content = item.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_text = block.get("text")
                if isinstance(block_text, str) and block_text:
                    collected.append(block_text)

    text = "".join(collected).strip()
    if not text:
        raise LLMRouterError(
            "Unexpected responses payload: no output text found",
            failure_type=FailureType.INVALID_RESPONSE,
        )
    return text


def _extract_streamed_text(event: dict[str, Any]) -> str:
    if not isinstance(event, dict):
        return ""
    if isinstance(event.get("delta"), str):
        return str(event["delta"])
    if isinstance(event.get("text"), str):
        return str(event["text"])
    part = event.get("part")
    if isinstance(part, dict):
        part_text = part.get("text")
        if isinstance(part_text, str):
            return part_text
    item = event.get("item")
    if isinstance(item, dict):
        item_text = item.get("text")
        if isinstance(item_text, str):
            return item_text
        content = item.get("content")
        if isinstance(content, list):
            collected: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_text = block.get("text")
                if isinstance(block_text, str) and block_text:
                    collected.append(block_text)
            return "".join(collected)
    return ""


def _parse_usage(resp_data: dict[str, Any]) -> LLMUsage:
    usage_data = resp_data.get("usage", {})
    if not isinstance(usage_data, dict):
        return LLMUsage()

    prompt = int(
        usage_data.get("input_tokens", usage_data.get("prompt_tokens", 0)) or 0
    )
    completion = int(
        usage_data.get("output_tokens", usage_data.get("completion_tokens", 0)) or 0
    )
    total = int(usage_data.get("total_tokens", prompt + completion) or 0)

    return LLMUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
    )


def _parse_response(resp_data: dict[str, Any], request: LLMRequest) -> LLMResponse:
    return LLMResponse(
        text=_extract_text(resp_data),
        raw=resp_data,
        usage=_parse_usage(resp_data),
        request_id=request.request_id,
        model=str(resp_data.get("model", request.model)),
        provider="openai_codex_subscription_http",
    )


def _parse_stream_response(
    events: Iterable[dict[str, Any]], request: LLMRequest
) -> LLMResponse:
    event_list = list(events)
    collected_chunks: list[str] = []
    final_response: dict[str, Any] | None = None

    for event in event_list:
        event_type = str(event.get("type", "")).strip()
        if event_type == "error":
            message = str(event.get("message") or event.get("code") or "").strip()
            raise LLMRouterError(
                f"Codex error: {message or json.dumps(event)[:200]}",
                failure_type=FailureType.PROVIDER_ERROR,
            )
        if event_type == "response.failed":
            response_data = event.get("response")
            if isinstance(response_data, dict):
                error_data = response_data.get("error")
                if isinstance(error_data, dict):
                    message = str(error_data.get("message") or "").strip()
                    if message:
                        raise LLMRouterError(
                            message,
                            failure_type=FailureType.PROVIDER_ERROR,
                        )
            raise LLMRouterError(
                "Codex response failed",
                failure_type=FailureType.PROVIDER_ERROR,
            )
        if event_type in {
            "response.output_text.delta",
            "response.output_text.done",
            "response.content_part.added",
        }:
            chunk = _extract_streamed_text(event)
            if chunk:
                collected_chunks.append(chunk)
            continue
        if event_type in {"response.done", "response.completed"}:
            response_data = event.get("response")
            if isinstance(response_data, dict):
                final_response = response_data

    if final_response is not None:
        try:
            return _parse_response(final_response, request)
        except LLMRouterError:
            raise
        except Exception:
            pass

    text = "".join(collected_chunks).strip()
    if not text:
        raise LLMRouterError(
            "Unexpected responses stream: no output text found",
            failure_type=FailureType.INVALID_RESPONSE,
        )

    return LLMResponse(
        text=text,
        raw={"events": event_list},
        usage=LLMUsage(),
        request_id=request.request_id,
        model=request.model,
        provider="openai_codex_subscription_http",
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


class OpenAICodexSubscriptionHTTPProvider:
    """HTTP provider for ChatGPT subscription-backed Codex access."""

    name = "openai_codex_subscription_http"
    supported_types = {
        "openai_codex_subscription_http",
        "openai_codex_http",
        "chatgpt_subscription_http",
    }

    def validate_config(self, config: dict[str, Any]) -> None:
        access_token_env = config.get("access_token_env")
        if access_token_env is not None and not isinstance(access_token_env, str):
            raise LLMRouterError(
                "openai_codex_subscription_http: access_token_env must be a string",
                failure_type=FailureType.PROVIDER_ERROR,
            )

        base_url = config.get("base_url")
        if base_url is not None and not isinstance(base_url, str):
            raise LLMRouterError(
                "openai_codex_subscription_http: base_url must be a string",
                failure_type=FailureType.PROVIDER_ERROR,
            )

    def complete(self, request: LLMRequest, config: dict[str, Any]) -> LLMResponse:
        import httpx

        url, headers, payload, timeout = _build_request_parts(request, config)

        try:
            with httpx.Client(timeout=timeout) as client:
                with client.stream(
                    "POST",
                    url,
                    json=payload,
                    headers=headers,
                ) as resp:
                    if resp.status_code >= 400:
                        error_text = resp.read().decode("utf-8", errors="replace")
                        _check_status(resp.status_code, error_text)
                    events = _collect_stream_events(resp, timeout)
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

        return _parse_stream_response(events, request)

    async def complete_async(
        self, request: LLMRequest, config: dict[str, Any]
    ) -> LLMResponse:
        import httpx

        url, headers, payload, timeout = _build_request_parts(request, config)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    url,
                    json=payload,
                    headers=headers,
                ) as resp:
                    if resp.status_code >= 400:
                        error_text = (await resp.aread()).decode(
                            "utf-8", errors="replace"
                        )
                        _check_status(resp.status_code, error_text)
                    chunks: list[str] = []
                    async for chunk in resp.aiter_text():
                        chunks.append(chunk)
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

        return _parse_stream_response(_iter_sse_events(chunks), request)
