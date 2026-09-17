"""Subprocess provider for the OpenCode CLI.

Invokes ``opencode run -m <provider/model> [--agent <agent>] <message>`` and
returns stdout.  JSON output is an explicit, opt-in governed mode: its
``step_finish`` events provide the authoritative usage receipt while ``text``
events provide the response text.  The default remains OpenCode's legacy text
mode.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from lee_llm_router.providers.base import FailureType, LLMRouterError
from lee_llm_router.response import LLMRequest, LLMResponse, LLMUsage

PROMPT_PLACEHOLDER = "{prompt}"

# Exact usage.source strings required by the Phase 1 usage taxonomy.
OPENCODE_USAGE_SOURCE = "opencode run JSON usage event"
OPENCODE_WORKER_USAGE_SOURCE = "opencode worker-written usage.json from provider output"

# The default stays in legacy text mode.  P1-5 supplies this opt-in config when
# a run needs OpenCode's JSON event stream for authoritative usage.
OPENCODE_GOVERNED_CONFIG: dict[str, str] = {"output_format": "json"}

_INPUT_KEYS = ("input", "input_tokens")
_OUTPUT_KEYS = ("output", "output_tokens")
_REASONING_KEYS = ("reasoning", "reasoning_tokens")
_CACHE_READ_KEYS = ("read", "input", "cached_input_tokens")
_CACHE_WRITE_KEYS = ("write", "cache_write_tokens", "cached_write_tokens")
_V2_COUNTER_KEYS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
    "cache_write_tokens",
    "total_tokens",
)
_WORKER_ARTIFACT_KEYS = {"basis", "source", *_V2_COUNTER_KEYS}


class _DuplicateJSONKeyError(ValueError):
    """Raised when a captured JSON event repeats an object key."""


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """Reject duplicate JSON keys instead of silently selecting one value."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError(key)
        result[key] = value
    return result


def _nonnegative_int(value: Any) -> bool:
    """Return whether ``value`` is a genuine nonnegative integer."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _unavailable_usage(reason: str) -> dict[str, Any]:
    """Return a strict v2 usage object with every unknown counter null."""
    return {
        "basis": "unavailable",
        "unavailable_reason": reason,
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
    }


def _parse_json_events(output: str) -> tuple[list[dict[str, Any]], bool]:
    """Parse OpenCode JSON output as one object or JSON lines.

    OpenCode's governed ``--format json`` output is JSONL.  A single object is
    also accepted because it is convenient for deterministic test harnesses.
    Non-JSON diagnostic lines and non-object JSON values are not events.  The
    returned boolean distinguishes that condition so a caller can provide a
    specific unavailable reason without inventing usage.
    """
    if not isinstance(output, str) or not output.strip():
        return [], False

    try:
        whole = json.loads(output, object_pairs_hook=_reject_duplicate_json_keys)
    except json.JSONDecodeError:
        whole = None
    except _DuplicateJSONKeyError as exc:
        raise LLMRouterError(
            f"OpenCode JSON event contains duplicate key {exc.args[0]!r}",
            failure_type=FailureType.CONTRACT_VIOLATION,
            cause=exc,
        ) from exc
    if isinstance(whole, dict):
        return [whole], False
    if whole is not None:
        return [], True

    events: list[dict[str, Any]] = []
    unusable_line = False
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line, object_pairs_hook=_reject_duplicate_json_keys)
        except json.JSONDecodeError:
            unusable_line = True
            continue
        except _DuplicateJSONKeyError as exc:
            raise LLMRouterError(
                f"OpenCode JSON event contains duplicate key {exc.args[0]!r}",
                failure_type=FailureType.CONTRACT_VIOLATION,
                cause=exc,
            ) from exc
        if isinstance(event, dict):
            events.append(event)
        else:
            unusable_line = True
    return events, unusable_line


def _consistent_counter(
    mapping: dict[str, Any], keys: tuple[str, ...], field_name: str
) -> int | None:
    """Read one counter alias family and reject contradictory values."""
    values: list[tuple[str, Any]] = [
        (key, mapping[key]) for key in keys if key in mapping
    ]
    if not values:
        return None

    first_key, first_value = values[0]
    for key, value in values[1:]:
        if value != first_value or type(value) is not type(first_value):
            raise LLMRouterError(
                f"OpenCode usage contains conflicting values for {first_key!r} "
                f"and {key!r} ({field_name})",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
    if not _nonnegative_int(first_value):
        raise LLMRouterError(
            f"OpenCode usage field {field_name!r} must be a nonnegative integer",
            failure_type=FailureType.CONTRACT_VIOLATION,
        )
    return first_value


def _step_part(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return the reference parser's native or envelope-level step payload."""
    raw_part = event.get("part")
    if isinstance(raw_part, dict):
        return raw_part
    # The read-only benchmark parser accepts the event envelope as a fallback.
    if raw_part is None and isinstance(event.get("tokens"), dict):
        return event
    return None


def _capture_event_usage(output: str) -> dict[str, Any]:
    """Parse authoritative OpenCode ``step_finish`` usage into v2 usage.

    Only finalized ``step_finish`` events contribute counters, matching the
    installed read-only benchmark parser. Duplicate JSON keys, conflicting
    aliases, and malformed reported counters fail closed.

    Missing required input/output usage makes the complete usage unavailable.
    Optional cache-read, cache-write, and reasoning counters remain null when
    any counted step omits them; they are never turned into zero. A
    source-reported zero remains zero. OpenCode does not report a native total
    in the authoritative event shape, so total is calculated from input,
    output, and reasoning only when all three are reported. Cache is kept
    separate, as in the reference.

    Args:
        output: Captured stdout from governed ``opencode run --format json``.

    Returns:
        A mapping directly valid against the attempt-record v2 ``usage``
        definition.

    Raises:
        LLMRouterError: If reported JSON usage is internally contradictory or
            contains malformed counters.
    """
    events, unusable_line = _parse_json_events(output)
    step_events = [event for event in events if event.get("type") == "step_finish"]
    if not step_events:
        if not isinstance(output, str) or not output.strip():
            reason = "OpenCode --format json output was empty"
        elif not events and unusable_line:
            reason = "OpenCode --format json output contained no parseable JSON events"
        else:
            reason = (
                "OpenCode --format json output contained no step_finish usage event"
            )
        return _unavailable_usage(reason)

    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "cache_write_tokens": 0,
    }
    parsed_steps: list[dict[str, int | None]] = []
    incomplete = False

    for event in step_events:
        part = _step_part(event)
        if part is None:
            incomplete = True
            continue
        raw_tokens = part.get("tokens")
        if not isinstance(raw_tokens, dict):
            incomplete = True
            continue

        input_tokens = _consistent_counter(raw_tokens, _INPUT_KEYS, "input")
        output_tokens = _consistent_counter(raw_tokens, _OUTPUT_KEYS, "output")
        reasoning_tokens = _consistent_counter(raw_tokens, _REASONING_KEYS, "reasoning")

        raw_cache = raw_tokens.get("cache")
        if raw_cache is None:
            cache_read = None
            cache_write = None
        elif isinstance(raw_cache, dict):
            cache_read = _consistent_counter(raw_cache, _CACHE_READ_KEYS, "cache.read")
            cache_write = _consistent_counter(
                raw_cache, _CACHE_WRITE_KEYS, "cache.write"
            )
        else:
            raise LLMRouterError(
                "OpenCode step_finish usage cache must be an object or null",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )

        if input_tokens is None or output_tokens is None:
            incomplete = True
            continue

        totals["input_tokens"] += input_tokens
        totals["output_tokens"] += output_tokens
        if cache_read is not None:
            totals["cached_input_tokens"] += cache_read
        if reasoning_tokens is not None:
            totals["reasoning_tokens"] += reasoning_tokens
        if cache_write is not None:
            totals["cache_write_tokens"] += cache_write
        parsed_steps.append(
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
            }
        )

    if incomplete or len(parsed_steps) != len(step_events):
        return _unavailable_usage(
            "OpenCode step_finish usage was incomplete; one or more events "
            "lacked valid input/output token counters"
        )

    all_cache_read = all(step["cache_read_tokens"] is not None for step in parsed_steps)
    all_cache_write = all(
        step["cache_write_tokens"] is not None for step in parsed_steps
    )
    all_reasoning = all(step["reasoning_tokens"] is not None for step in parsed_steps)
    total_tokens = (
        totals["input_tokens"] + totals["output_tokens"] + totals["reasoning_tokens"]
        if all_reasoning
        else None
    )

    return {
        "basis": "provider_reported",
        "source": OPENCODE_USAGE_SOURCE,
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        "cached_input_tokens": (
            totals["cached_input_tokens"] if all_cache_read else None
        ),
        "reasoning_tokens": totals["reasoning_tokens"] if all_reasoning else None,
        "cache_write_tokens": (
            totals["cache_write_tokens"] if all_cache_write else None
        ),
        "total_tokens": total_tokens,
    }


def _worker_artifact_usage(path: str | Path) -> dict[str, Any]:
    """Read a strict provider-copied usage artifact.

    A qualifying worker artifact is itself a closed v2 usage object whose
    source says the counters came from the OpenCode JSON usage event. This
    explicit provenance requirement prevents arbitrary or estimated
    ``usage.json`` files from becoming provider-reported evidence.
    """
    candidate = Path(path)
    try:
        payload = json.loads(
            candidate.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _unavailable_usage(
            f"OpenCode worker usage artifact was unavailable or invalid: {exc}"
        )
    except _DuplicateJSONKeyError as exc:
        raise LLMRouterError(
            f"OpenCode worker usage artifact contains duplicate key {exc.args[0]!r}",
            failure_type=FailureType.CONTRACT_VIOLATION,
            cause=exc,
        ) from exc

    if not isinstance(payload, dict):
        return _unavailable_usage(
            "OpenCode worker usage artifact was not a JSON object"
        )
    if set(payload) - _WORKER_ARTIFACT_KEYS:
        return _unavailable_usage(
            "OpenCode worker usage artifact was not a closed v2 usage object"
        )
    if (
        payload.get("basis") != "provider_reported"
        or payload.get("source") != OPENCODE_USAGE_SOURCE
    ):
        return _unavailable_usage(
            "OpenCode worker usage artifact did not identify provider-copied "
            "OpenCode JSON event usage"
        )

    input_tokens = payload.get("input_tokens")
    output_tokens = payload.get("output_tokens")
    if not _nonnegative_int(input_tokens) or not _nonnegative_int(output_tokens):
        return _unavailable_usage(
            "OpenCode worker usage artifact lacked valid input/output token counters"
        )
    optional: dict[str, int | None] = {}
    for key in (
        "cached_input_tokens",
        "reasoning_tokens",
        "cache_write_tokens",
        "total_tokens",
    ):
        value = payload.get(key)
        if value is not None and not _nonnegative_int(value):
            raise LLMRouterError(
                f"OpenCode worker usage artifact field {key!r} must be a "
                "nonnegative integer or null",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
        optional[key] = value

    reasoning_tokens = optional["reasoning_tokens"]
    total_tokens = optional["total_tokens"]
    if total_tokens is not None:
        if reasoning_tokens is not None:
            # Every counter is known: the reported total must match exactly.
            expected_total = input_tokens + output_tokens + reasoning_tokens
            if total_tokens != expected_total:
                raise LLMRouterError(
                    "OpenCode worker usage artifact total_tokens contradicts "
                    "input/output/reasoning counters",
                    failure_type=FailureType.CONTRACT_VIOLATION,
                )
        elif total_tokens < input_tokens + output_tokens:
            # Reasoning is unknown but nonnegative, so it can only add to the
            # known counters; it can never reduce the reported total below
            # input + output.
            raise LLMRouterError(
                "OpenCode worker usage artifact total_tokens is below the "
                "known input/output counters while reasoning is absent",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
    expected_total = (
        input_tokens + output_tokens + reasoning_tokens
        if reasoning_tokens is not None
        else None
    )

    return {
        "basis": "provider_reported",
        "source": OPENCODE_WORKER_USAGE_SOURCE,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": optional["cached_input_tokens"],
        "reasoning_tokens": reasoning_tokens,
        "cache_write_tokens": optional["cache_write_tokens"],
        "total_tokens": total_tokens if total_tokens is not None else expected_total,
    }


def capture_usage(
    output: str, *, worker_usage_path: str | Path | None = None
) -> dict[str, Any]:
    """Capture strict v2 usage from OpenCode JSON events or a copied artifact.

    The raw ``opencode run --format json`` stream is authoritative when it has
    complete ``step_finish`` usage. A worker ``usage.json`` is consulted only
    when that stream lacks usable usage, and qualifies only when its strict v2
    provenance identifies a copy of the same provider event. Otherwise usage
    remains unavailable with null counters. No estimates are accepted.
    """
    event_usage = _capture_event_usage(output)
    if event_usage["basis"] == "provider_reported" or worker_usage_path is None:
        return event_usage
    artifact_usage = _worker_artifact_usage(worker_usage_path)
    if artifact_usage["basis"] == "provider_reported":
        return artifact_usage
    return _unavailable_usage(
        f"{event_usage['unavailable_reason']}; "
        f"{artifact_usage['unavailable_reason']}"
    )


def _extract_response_text(stdout: str, *, output_format: str | None) -> str:
    """Extract and join OpenCode JSON ``text`` event chunks."""
    if output_format != "json":
        return stdout.strip()

    chunks: list[str] = []
    events, _ = _parse_json_events(stdout)
    for event in events:
        if event.get("type") != "text":
            continue
        part = event.get("part")
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            chunks.append(part["text"])
        elif isinstance(event.get("text"), str):
            chunks.append(event["text"])
    return "\n".join(chunks).strip()


class OpenCodeCLIProvider:
    """Invokes the OpenCode CLI via subprocess and returns its stdout."""

    name = "opencode_cli"
    supported_types = {"opencode_cli", "opencode"}
    default_command = "opencode"
    default_output_format: str | None = None

    def _resolve_output_format(self, config: dict[str, Any]) -> str | None:
        """Resolve the opt-in config for OpenCode's ``--format`` option."""
        output_format = config.get("output_format")
        if output_format is None:
            return self.default_output_format
        if output_format not in {"default", "json"}:
            raise LLMRouterError(
                f"{self.name} provider key 'output_format' must be 'default' or 'json'",
                failure_type=FailureType.PROVIDER_ERROR,
            )
        return output_format

    def validate_config(self, config: dict[str, Any]) -> None:
        """Validate the provider config block.

        Args:
            config: Provider configuration mapping.

        Raises:
            LLMRouterError: If a required key is missing or malformed.
        """
        command = config.get("command", self.default_command)
        if not isinstance(command, str) or not command.strip():
            raise LLMRouterError(
                f"{self.name} provider missing required config key: 'command'",
                failure_type=FailureType.PROVIDER_ERROR,
            )

        model = config.get("model")
        if not isinstance(model, str) or not model.strip():
            raise LLMRouterError(
                f"{self.name} provider missing required config key: 'model'",
                failure_type=FailureType.PROVIDER_ERROR,
            )
        _validate_model(model, provider_name=self.name)

        agent = config.get("agent")
        if agent is not None and (not isinstance(agent, str) or not agent.strip()):
            raise LLMRouterError(
                f"{self.name} provider key 'agent' must be a non-empty string",
                failure_type=FailureType.PROVIDER_ERROR,
            )

        timeout = config.get("timeout")
        if timeout is not None and not isinstance(timeout, (int, float)):
            raise LLMRouterError(
                f"{self.name} provider key 'timeout' must be a number",
                failure_type=FailureType.PROVIDER_ERROR,
            )
        self._resolve_output_format(config)

    def build_command(
        self,
        config: dict[str, Any],
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]:
        """Return the dispatch command template as an argv list.

        The final positional element is the literal ``{prompt}`` placeholder,
        which :meth:`complete` replaces with the resolved prompt text.  Setting
        ``output_format: json`` injects ``--format json`` without changing the
        legacy model, agent, and prompt ordering.

        Args:
            config: Provider configuration mapping.
            model: Optional model override.
            effort: Accepted for interface parity; OpenCode has no effort flag.

        Returns:
            The argv list for the OpenCode CLI invocation.

        Raises:
            LLMRouterError: If the config or model override is invalid.
        """
        self.validate_config(config)
        command = config.get("command", self.default_command)
        resolved_model = model or config["model"]
        _validate_model(resolved_model, provider_name=self.name)

        cmd = [command, "run", "-m", resolved_model]
        agent = config.get("agent")
        if agent:
            cmd.extend(["--agent", agent])
        output_format = self._resolve_output_format(config)
        if output_format:
            cmd.extend(["--format", output_format])
        cmd.append(PROMPT_PLACEHOLDER)
        return cmd

    def complete(self, request: LLMRequest, config: dict[str, Any]) -> LLMResponse:
        """Run the OpenCode CLI and return its response.

        Args:
            request: The routed completion request.
            config: Provider configuration mapping.

        Returns:
            The provider response carrying the CLI output and usage counters
            when governed JSON output reports them.

        Raises:
            LLMRouterError: On timeout, missing binary, non-zero exit, malformed
                governed usage, or empty output.
        """
        self.validate_config(config)

        command = config.get("command", self.default_command)
        timeout = request.timeout or float(config.get("timeout", 120.0))
        prompt = _build_prompt(request)
        cmd = [
            prompt if part == PROMPT_PLACEHOLDER else part
            for part in self.build_command(
                config, model=request.model or None, effort=request.effort
            )
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMRouterError(
                f"opencode CLI timed out after {timeout}s",
                failure_type=FailureType.TIMEOUT,
                cause=exc,
            ) from exc
        except FileNotFoundError as exc:
            raise LLMRouterError(
                f"opencode CLI binary not found: {command!r}",
                failure_type=FailureType.PROVIDER_ERROR,
                cause=exc,
            ) from exc

        if result.returncode != 0:
            detail = _snippet(result.stderr) or _snippet(result.stdout) or "no output"
            raise LLMRouterError(
                f"opencode CLI exited {result.returncode}: {detail}",
                failure_type=FailureType.PROVIDER_ERROR,
            )

        output_format = self._resolve_output_format(config)
        text = _extract_response_text(result.stdout, output_format=output_format)
        if not text:
            raise LLMRouterError(
                "opencode CLI returned empty output",
                failure_type=FailureType.INVALID_RESPONSE,
            )

        usage = LLMUsage()
        if output_format == "json":
            captured_usage = capture_usage(result.stdout)
            if captured_usage["basis"] == "provider_reported":
                usage = LLMUsage(
                    prompt_tokens=captured_usage["input_tokens"],
                    completion_tokens=captured_usage["output_tokens"],
                    total_tokens=captured_usage["total_tokens"],
                )

        return LLMResponse(
            text=text,
            raw={
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
                "command": cmd,
            },
            usage=usage,
            request_id=request.request_id,
            model=request.model or str(config["model"]),
            provider=self.name,
        )


def _validate_model(model: str, *, provider_name: str) -> None:
    """Raise if the model is not in ``provider/model`` form."""
    parts = model.split("/")
    if len(parts) != 2 or not all(part.strip() for part in parts):
        raise LLMRouterError(
            f"{provider_name} provider key 'model' must look like "
            f"'provider/model', got {model!r}",
            failure_type=FailureType.PROVIDER_ERROR,
        )


def _build_prompt(request: LLMRequest) -> str:
    """Return the prompt text, prepending a system message when present."""
    user_messages = [m for m in request.messages if m.get("role") == "user"]
    prompt = user_messages[-1]["content"] if user_messages else ""
    system_messages = [m for m in request.messages if m.get("role") == "system"]
    if system_messages:
        prompt = f"{system_messages[0]['content']}\n\n{prompt}"
    return prompt


def _snippet(text: str, limit: int = 200) -> str:
    """Return a single-line, length-capped excerpt of ``text``."""
    normalized = " ".join(text.split())
    return normalized[:limit]
