"""Subprocess provider for the omp (oh-my-pi) harness.

Invokes ``omp -p [--model <model>]`` with the prompt on stdin and returns
stdout.  JSON output is an explicit, opt-in governed mode: ``--mode json``
emits the pi-style agent-session event stream whose assistant
``message_end`` events carry the authoritative provider-reported usage
receipt (:func:`capture_usage`).  The default remains omp's legacy text
mode with its "Working..." prefix cleaning.  The omp harness handles auth
internally via its stored credentials.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from lee_llm_router.providers.base import FailureType, LLMRouterError
from lee_llm_router.response import LLMRequest, LLMResponse, LLMUsage


class OmpCLIProvider:
    """Invokes the omp harness via subprocess and returns its stdout."""

    name = "omp_cli"
    supported_types = {"omp_cli"}
    default_command = "omp"

    #: Legacy default: no ``--mode`` flag is injected.  P1-5 supplies the
    #: governed opt-in config when a run needs omp's JSON event stream for
    #: authoritative usage.
    default_mode: str | None = None

    def validate_config(self, config: dict[str, Any]) -> None:
        command = config.get("command", self.default_command)
        if not isinstance(command, str) or not command.strip():
            raise LLMRouterError(
                f"{self.name} provider missing required config key: 'command'",
                failure_type=FailureType.PROVIDER_ERROR,
            )
        self._resolve_mode(config)

    def _resolve_mode(self, config: dict[str, Any]) -> str | None:
        """Resolve the opt-in config for omp's ``--mode`` output option.

        Args:
            config: Provider configuration mapping.

        Returns:
            ``None`` for the legacy text default, otherwise ``"text"`` or
            ``"json"``.  ``"json"`` is the governed opt-in that makes omp
            emit the event stream :func:`capture_usage` parses; ``"text"``
            is the legacy default made explicit and adds no flag.

        Raises:
            LLMRouterError: If ``mode`` is present but not ``"text"`` or
                ``"json"``.  The unmanaged ``rpc``/``rpc-ui`` modes are
                rejected, not silently mapped.
        """
        mode = config.get("mode")
        if mode is None:
            return self.default_mode
        if mode not in {"text", "json"}:
            raise LLMRouterError(
                f"{self.name} provider key 'mode' must be 'text' or 'json', "
                f"got {mode!r}",
                failure_type=FailureType.PROVIDER_ERROR,
            )
        return mode

    def build_command(
        self,
        config: dict[str, Any],
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]:
        """Return the dispatch command template as an argv list.

        The prompt is delivered on stdin, so the argv list carries no prompt
        placeholder.  The omp harness exposes no reasoning-effort flag, so
        ``effort`` is accepted for interface parity and ignored.  The
        governed opt-in ``{"mode": "json"}`` injects ``--mode json`` without
        changing the legacy ``-p``/``--model`` ordering; the legacy text
        default adds no ``--mode`` flag at all.

        Args:
            config: Provider configuration mapping.
            model: Optional model override; falls back to ``config['model']``.
            effort: Accepted for interface parity; omp has no effort flag.

        Returns:
            The argv list for the omp CLI invocation.

        Raises:
            LLMRouterError: If the config is invalid.
        """
        self.validate_config(config)
        command = config.get("command", self.default_command)
        resolved_model = model or config.get("model", "")

        cmd = [command, "-p"]
        if self._resolve_mode(config) == "json":
            cmd.extend(["--mode", "json"])
        if resolved_model:
            cmd.extend(["--model", resolved_model])
        return cmd

    def complete(self, request: LLMRequest, config: dict[str, Any]) -> LLMResponse:
        self.validate_config(config)

        command = config.get("command", self.default_command)
        timeout = request.timeout or float(config.get("timeout", 120.0))

        # Build prompt from last user message
        user_messages = [m for m in request.messages if m.get("role") == "user"]
        prompt = user_messages[-1]["content"] if user_messages else ""

        # If there's a system message, prepend it
        system_messages = [m for m in request.messages if m.get("role") == "system"]
        if system_messages:
            system_prompt = system_messages[0]["content"]
            prompt = f"{system_prompt}\n\n{prompt}"

        model = request.model or config.get("model", "")
        cmd = self.build_command(
            config, model=request.model or None, effort=request.effort
        )

        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMRouterError(
                f"omp CLI timed out after {timeout}s",
                failure_type=FailureType.TIMEOUT,
                cause=exc,
            ) from exc
        except FileNotFoundError as exc:
            raise LLMRouterError(
                f"omp CLI binary not found: {command!r}",
                failure_type=FailureType.PROVIDER_ERROR,
                cause=exc,
            ) from exc

        if result.returncode != 0:
            detail = _snippet(result.stderr) or _snippet(result.stdout) or "no output"
            raise LLMRouterError(
                f"omp CLI exited {result.returncode}: {detail}",
                failure_type=FailureType.PROVIDER_ERROR,
            )

        raw: dict[str, Any] = {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "command": cmd,
        }
        usage = LLMUsage()
        if self._resolve_mode(config) == "json":
            # Governed JSON mode: response text comes from the terminal
            # assistant message and usage from capture_usage; the legacy
            # "Working..." cleaning never applies.
            text = _extract_response_text(result.stdout)
            captured_usage = capture_usage(result.stdout)
            raw["captured_usage"] = captured_usage
            if captured_usage["basis"] == "provider_reported":
                usage = LLMUsage(
                    prompt_tokens=captured_usage["input_tokens"],
                    completion_tokens=captured_usage["output_tokens"],
                    total_tokens=captured_usage["total_tokens"],
                )
        else:
            text = _clean_output(result.stdout)
        if not text:
            raise LLMRouterError(
                "omp CLI returned empty output",
                failure_type=FailureType.INVALID_RESPONSE,
            )

        return LLMResponse(
            text=text,
            raw=raw,
            usage=usage,
            request_id=request.request_id,
            model=request.model or model,
            provider=self.name,
        )


# Exact usage.source string required for OMP JSON events by D209 ruling 3
# via docs/staffing/phase1-contracts.md §Usage evidence taxonomy.
OMP_USAGE_SOURCE = "omp -p --mode json events"

#: The governed opt-in config: setting ``mode: json`` makes omp emit the
#: pi-style agent-session event stream that :func:`capture_usage` parses.
OMP_GOVERNED_CONFIG: dict[str, str] = {"mode": "json"}

# OMP's ``--mode json`` stream emits one terminal ``message_end`` event per
# assistant response and repeats aggregate state in ``agent_end``.  Terminal
# assistant events are the only accounting boundary; every counted assistant
# must report all billable components.  Recorded local omp sessions confirm
# the usage shape: ``input``, ``output``, ``cacheRead``, ``cacheWrite``,
# ``totalTokens``, optional ``reasoningTokens``, plus an embedded ``cost``
# object that is harness provenance and never token evidence.
_REQUIRED_USAGE_KEYS = ("input", "output", "cacheRead", "cacheWrite")
_REASONING_USAGE_KEYS = ("reasoning", "reasoningTokens", "reasoning_tokens")
_ID_KEYS = ("responseId", "response_id", "id")


def _nonnegative_int(value: Any) -> bool:
    """Return True only for a genuine nonnegative ``int`` (bools excluded)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _parse_json_events(output: str) -> list[dict[str, Any]]:
    """Parse OMP JSON-mode output as one JSON object or JSON lines.

    Mirrors the accepted benchmark reader semantics (``workbench/pi.py``
    ``_read_json_events``; omp is a pi fork emitting the same event
    stream): a whole-text JSON object is a single event; otherwise each
    line is parsed independently and malformed lines are skipped.
    Non-dict JSON values are never events.

    Args:
        output: Captured omp stdout in ``--mode json``.

    Returns:
        Every parsed JSON object event, in order; ``[]`` when the output
        is empty or contains nothing parseable.
    """
    if not isinstance(output, str) or not output.strip():
        return []
    try:
        whole = json.loads(output)
    except json.JSONDecodeError:
        whole = None
    if isinstance(whole, dict):
        return [whole]
    events: list[dict[str, Any]] = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _assistant_terminal_messages(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return deduplicated assistant ``message_end`` messages.

    Only ``message_end`` events with role ``assistant`` are counted;
    ``agent_end`` state is never counted.  An assistant message carrying a
    stable id (``responseId``/``response_id``/``id``, checked on the
    nested message then the event) is kept once; messages without a
    stable id cannot be deduplicated and are always kept.

    Args:
        events: Parsed OMP JSON events.

    Returns:
        Counted assistant terminal messages in stream order.
    """
    messages: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for event in events:
        if event.get("type") != "message_end":
            continue
        raw = event.get("message")
        message = raw if isinstance(raw, dict) else event
        if message.get("role") != "assistant":
            continue
        marker = next(
            (
                value
                for item in (message, event)
                for key in _ID_KEYS
                for value in (item.get(key),)
                if isinstance(value, str) and value
            ),
            None,
        )
        if marker is not None:
            if marker in seen_ids:
                continue
            seen_ids.add(marker)
        messages.append(message)
    return messages


def capture_usage(output: str) -> dict[str, Any]:
    """Parse authoritative OMP JSON-mode usage from captured output.

    Implements the P1-4f usage parser (D209 ruling 3 via
    ``docs/staffing/phase1-contracts.md`` §Usage evidence taxonomy).  Sums
    provider-reported, nonnegative token components from deduplicated
    assistant ``message_end`` events and never counts repeated
    ``agent_end`` state.  Contradictory ``totalTokens`` figures or
    conflicting assistant models are rejected rather than estimated; a
    genuine reported zero is valid evidence; absent or partial usage is
    returned as ``unavailable`` with a specific reason.  No token figure
    is ever estimated from text, context length, cost, or elapsed time.

    Args:
        output: Captured stdout of an ``omp -p --mode json`` run — one
            JSON object or JSON lines.  Malformed lines are skipped.

    Returns:
        A usage mapping that is directly schema-valid against the
        attempt-record v2 ``$defs/usage`` subschema (P1-5): ``basis``,
        ``source`` or ``unavailable_reason``, and the token counters
        ``input_tokens``, ``output_tokens``, ``cached_input_tokens``,
        ``reasoning_tokens``, ``total_tokens`` — nothing else.  When every
        counted assistant terminal message reports all billable components
        (``input``, ``output``, ``cacheRead``, ``cacheWrite``), ``basis``
        is ``provider_reported`` with the exact source
        ``omp -p --mode json events`` and summed integer counters.
        ``reasoning_tokens`` is summed from the reported reasoning figure
        (``reasoningTokens`` in recorded omp output) only when every
        counted message reports a valid reasoning figure that does not
        exceed its output; otherwise it stays ``null`` with no reason
        field.  ``cacheWrite`` contributes to ``total_tokens`` and to
        ``totalTokens`` reconciliation but is not surfaced as a schema
        field.  An embedded ``cost`` object is harness cost-estimate
        provenance and never influences token capture.  When
        authoritative usage is absent or partial, ``basis`` is
        ``unavailable`` with ``unavailable_reason`` and ``None`` counters.

    Raises:
        LLMRouterError: With ``FailureType.CONTRACT_VIOLATION`` when a
            reported ``totalTokens`` contradicts its components, when the
            aggregate reported total contradicts the summed components,
            or when counted assistant messages disagree on the model.
    """
    events = _parse_json_events(output)
    messages = _assistant_terminal_messages(events)

    totals = {
        # cache_write_tokens is internal evidence only: omp's cacheWrite
        # contributes to totalTokens validation and total_tokens but has
        # no field in the schema-valid v2 usage mapping.
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
    }
    complete = bool(messages)
    reasoning_complete = bool(messages)
    reported_total = 0
    total_presence = 0
    observed_models: set[str] = set()

    for message in messages:
        model = message.get("model")
        if isinstance(model, str) and model:
            observed_models.add(model)
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
        values = {key: usage.get(key) for key in _REQUIRED_USAGE_KEYS}
        if any(not _nonnegative_int(value) for value in values.values()):
            complete = False
            continue
        totals["input_tokens"] += values["input"]
        totals["output_tokens"] += values["output"]
        totals["cached_input_tokens"] += values["cacheRead"]
        totals["cache_write_tokens"] += values["cacheWrite"]
        reasoning = next(
            (usage[key] for key in _REASONING_USAGE_KEYS if key in usage),
            None,
        )
        if not _nonnegative_int(reasoning) or reasoning > values["output"]:
            # Reasoning is an optional subset of output.  An absent, partial,
            # or invalid figure withholds only the reasoning total; it is
            # never fabricated as zero.
            reasoning_complete = False
        else:
            totals["reasoning_tokens"] += reasoning
        expected = sum(values.values())
        if usage.get("totalTokens") is not None:
            if (
                not _nonnegative_int(usage["totalTokens"])
                or usage["totalTokens"] != expected
            ):
                raise LLMRouterError(
                    "OMP assistant usage totalTokens contradicts its components",
                    failure_type=FailureType.CONTRACT_VIOLATION,
                )
            reported_total += usage["totalTokens"]
            total_presence += 1

    if len(observed_models) > 1:
        raise LLMRouterError(
            "OMP JSON stream has conflicting assistant models",
            failure_type=FailureType.CONTRACT_VIOLATION,
        )

    if complete:
        computed = (
            totals["input_tokens"]
            + totals["output_tokens"]
            + totals["cached_input_tokens"]
            + totals["cache_write_tokens"]
        )
        if total_presence == len(messages) and reported_total != computed:
            raise LLMRouterError(
                "OMP aggregate usage totalTokens contradicts its components",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
        # Schema-valid v2 usage only: cacheWrite is folded into
        # total_tokens but has no field of its own, and reasoning stays
        # null without an explanatory key when it is unknown.
        return {
            "basis": "provider_reported",
            "source": OMP_USAGE_SOURCE,
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "cached_input_tokens": totals["cached_input_tokens"],
            "reasoning_tokens": (
                totals["reasoning_tokens"] if reasoning_complete else None
            ),
            "total_tokens": computed,
        }

    if not messages:
        if not events:
            if not isinstance(output, str) or not output.strip():
                reason = "OMP JSON output was empty"
            else:
                reason = "OMP JSON output contained no parseable JSON events"
        else:
            reason = "OMP JSON output contained no assistant message_end events"
    else:
        reason = (
            "OMP assistant message_end usage was incomplete; one or more "
            "counted assistant messages lacked valid input, output, or "
            "cache components"
        )
    return {
        "basis": "unavailable",
        "unavailable_reason": reason,
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
    }


def _extract_response_text(output: str) -> str:
    """Extract and join response text from OMP JSON ``message_end`` events.

    Only deduplicated assistant terminal messages contribute.  Assistant
    content is a list of typed blocks; blocks with type ``text`` and a
    string ``text`` field are joined in stream order.  A plain-string
    message content is accepted as itself.

    Args:
        output: Captured stdout from ``omp -p --mode json``.

    Returns:
        The joined response text, stripped; ``""`` when no assistant
        terminal message carries text.
    """
    chunks: list[str] = []
    for message in _assistant_terminal_messages(_parse_json_events(output)):
        content = message.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            chunks.extend(
                block["text"]
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            )
    return "\n".join(chunk for chunk in chunks if chunk).strip()


def _clean_output(stdout: str) -> str:
    """Strip omp's 'Working...' prefix and trailing whitespace from output."""
    text = stdout.strip()
    # Remove the "Working..." status line if present
    text = re.sub(r"^Working\.\.\.\s*\n?", "", text)
    return text.strip()


def _snippet(text: str, limit: int = 200) -> str:
    normalized = " ".join(text.split())
    return normalized[:limit]
