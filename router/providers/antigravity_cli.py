"""Subprocess provider for the Antigravity CLI (``agy``).

Invokes ``agy --dangerously-skip-permissions --model <model> [--effort <level>]
[--output-format json] [--print-timeout <dur>] -p <prompt>`` in print mode
with the prompt delivered as the argument to ``-p``. The optional JSON mode is
the governed usage-receipt path.
The harness handles authentication internally through its stored Google
subscription credentials, so no API key configuration is needed here.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from lee_llm_router.providers.base import FailureType, LLMRouterError
from lee_llm_router.response import LLMRequest, LLMResponse, LLMUsage

PROMPT_PLACEHOLDER = "{prompt}"
VALID_EFFORTS = ("low", "medium", "high")

# Exact usage.source string required by the Phase 1 usage taxonomy.
AGY_USAGE_SOURCE = "agy -p usage line (agent-orch worker.py)"
# P1-5 supplies this opt-in config so the receipt line remains in stdout.
AGY_GOVERNED_CONFIG: dict[str, str] = {"output_format": "json"}

_AGY_INPUT_KEYS = ("input_tokens", "prompt_tokens")
_AGY_OUTPUT_KEYS = ("output_tokens", "completion_tokens")
_AGY_CACHED_READ_KEYS = ("cached_read_tokens", "cache_read_tokens")
_AGY_CACHED_WRITE_KEYS = (
    "cache_write_tokens",
    "cached_write_tokens",
    "cache_write_input_tokens",
)
_AGY_REASONING_KEYS = ("thinking_tokens",)
_AGY_TOTAL_KEYS = ("total_tokens",)
_AGY_REQUIRED_KEYS = _AGY_INPUT_KEYS + _AGY_OUTPUT_KEYS


class _DuplicateJSONKeyError(ValueError):
    """Raised when a recorded receipt repeats a JSON object key."""


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """Reject duplicate keys instead of silently choosing one value."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError(key)
        result[key] = value
    return result


def _nonnegative_int(value: Any) -> bool:
    """Return True only for a genuine nonnegative integer."""
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


def _consistent_usage_value(
    mappings: tuple[dict[str, Any], ...], keys: tuple[str, ...]
) -> tuple[bool, Any]:
    """Return one consistent value across the receipt and its usage object.

    This mirrors ``agent-orch.worker._parse_provider_usage``: aliases are
    reconciled across both mappings, including their value types, rather than
    silently selecting one spelling when the recorded receipt disagrees.
    """
    values: list[tuple[str, Any]] = []
    for mapping_index, mapping in enumerate(mappings):
        for key in keys:
            if key in mapping:
                values.append((f"source-{mapping_index}.{key}", mapping[key]))
    if not values:
        return False, None
    first_name, first_value = values[0]
    conflicts = [
        name
        for name, value in values[1:]
        if value != first_value or type(value) is not type(first_value)
    ]
    if conflicts:
        raise LLMRouterError(
            "agy usage contains conflicting values in fields: "
            + ", ".join([first_name, *conflicts]),
            failure_type=FailureType.CONTRACT_VIOLATION,
        )
    return True, first_value


def _has_nonzero_tokens(usage: object) -> bool:
    """Return whether an agy non-success receipt carries real token evidence.

    Agent-Orch deliberately ignores agy's timeout/error objects whose usage
    block contains only zeros. A zero is accepted only for a successful
    receipt, where the provider explicitly reported that zero.
    """
    if not isinstance(usage, dict):
        return False
    for key in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
    ):
        value = usage.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value > 0
        ):
            return True
    return False


def _parse_agy_json_lines(output: str) -> tuple[list[dict[str, Any]], bool]:
    """Parse recorded agy objects, skipping non-receipt log lines.

    The proven worker parser treats stdout as JSON lines, ignores malformed or
    unrelated lines, and only considers objects carrying the terminal status.
    The boolean reports whether no usable JSON event was available because the
    output was malformed/non-object rather than simply lacking a terminal
    receipt.
    """
    if not isinstance(output, str) or not output.strip():
        return [], False

    try:
        whole = json.loads(output, object_pairs_hook=_reject_duplicate_json_keys)
    except json.JSONDecodeError:
        whole = None
    except _DuplicateJSONKeyError as exc:
        raise LLMRouterError(
            f"agy usage line contains duplicate JSON key {exc.args[0]!r}",
            failure_type=FailureType.CONTRACT_VIOLATION,
            cause=exc,
        ) from exc
    if whole is not None:
        return ([whole], False) if isinstance(whole, dict) else ([], True)

    events: list[dict[str, Any]] = []
    unusable_event = False
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line, object_pairs_hook=_reject_duplicate_json_keys)
        except json.JSONDecodeError:
            unusable_event = True
            continue
        except _DuplicateJSONKeyError as exc:
            raise LLMRouterError(
                f"agy usage line contains duplicate JSON key {exc.args[0]!r}",
                failure_type=FailureType.CONTRACT_VIOLATION,
                cause=exc,
            ) from exc
        if not isinstance(event, dict):
            unusable_event = True
            continue
        events.append(event)
    return events, unusable_event


def _usage_evidence_present(payload: dict[str, Any]) -> bool:
    """Return whether a successful agy object carries a usage field."""
    return "usage" in payload or any(key in payload for key in _AGY_REQUIRED_KEYS)


def _parse_agy_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert one authoritative agy receipt to strict v2 usage."""
    if "usage" in payload:
        source = payload["usage"]
        if not isinstance(source, dict):
            raise LLMRouterError(
                "agy usage receipt field must be an object",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
        mappings = (source, payload)
    else:
        source = payload
        mappings = (payload,)

    input_present, input_tokens = _consistent_usage_value(mappings, _AGY_INPUT_KEYS)
    output_present, output_tokens = _consistent_usage_value(mappings, _AGY_OUTPUT_KEYS)
    if not input_present or not output_present:
        return _unavailable_usage(
            "agy -p usage receipt lacked valid input_tokens/output_tokens"
        )
    if not _nonnegative_int(input_tokens) or not _nonnegative_int(output_tokens):
        raise LLMRouterError(
            "agy usage receipt input/output token counts must be nonnegative "
            "integers",
            failure_type=FailureType.CONTRACT_VIOLATION,
        )

    optional_values = {
        "cached_input_tokens": _consistent_usage_value(mappings, _AGY_CACHED_READ_KEYS),
        "cached_write_tokens": _consistent_usage_value(
            mappings, _AGY_CACHED_WRITE_KEYS
        ),
        "reasoning_tokens": _consistent_usage_value(mappings, _AGY_REASONING_KEYS),
        "total_tokens": _consistent_usage_value(mappings, _AGY_TOTAL_KEYS),
    }
    for field_name, (present, value) in optional_values.items():
        if present and not _nonnegative_int(value):
            raise LLMRouterError(
                f"agy usage receipt field {field_name!r} must be a "
                "nonnegative integer",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )

    reported_total = optional_values["total_tokens"][1]
    expected_total = input_tokens + output_tokens
    if reported_total is not None and reported_total != expected_total:
        raise LLMRouterError(
            "agy usage receipt total_tokens contradicts input_tokens + "
            "output_tokens",
            failure_type=FailureType.CONTRACT_VIOLATION,
        )

    reasoning = optional_values["reasoning_tokens"][1]
    if reasoning is not None and reasoning > output_tokens:
        raise LLMRouterError(
            "agy usage receipt thinking_tokens contradicts output_tokens",
            failure_type=FailureType.CONTRACT_VIOLATION,
        )

    # cache_read_tokens is deliberately not constrained by input_tokens:
    # the proven receipt records cache history that can exceed this turn's
    # input count. Preserve cache-write evidence separately: the v2 cost
    # boundary must not silently price input/output while dropping a billed
    # cache component.
    return {
        "basis": "provider_reported",
        "source": AGY_USAGE_SOURCE,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": optional_values["cached_input_tokens"][1],
        "reasoning_tokens": reasoning,
        "cache_write_tokens": optional_values["cached_write_tokens"][1],
        "total_tokens": (
            reported_total if reported_total is not None else expected_total
        ),
    }


def capture_usage(output: str) -> dict[str, Any]:
    """Parse an authoritative agy ``-p`` usage line into v2 usage.

    Semantics mirror the proven Antigravity parser in
    ``agent-orch/src/agent_orch/worker.py``: terminal ``SUCCESS`` objects with
    an error are ignored, while a non-success terminal object is usable only
    when its usage carries a nonzero token count. This preserves the useful
    timeout/error receipt without turning an all-zero timeout placeholder into
    evidence. Alias fields are reconciled across the nested usage object and
    its envelope, and contradictions fail closed. Malformed or missing
    documented receipt output is returned as ``unavailable`` with null
    counters. No token is estimated from response text, cost, or duration.

    Args:
        output: Captured stdout from governed ``agy -p`` JSON output.

    Returns:
        A strict attempt-record v2 ``usage`` mapping. Known receipts use
        ``provider_reported`` and the exact Phase 1 taxonomy source; missing
        receipts use ``unavailable`` and never substitute zero counters.

    Raises:
        LLMRouterError: If a receipt contains duplicate keys, contradictory
            aliases or totals, or malformed reported counter values.
    """
    events, unusable_event = _parse_agy_json_lines(output)
    candidates: list[dict[str, Any]] = []
    for event in events:
        status = event.get("status")
        if status == "SUCCESS":
            if event.get("error") not in (None, "", {}, []):
                continue
            if _usage_evidence_present(event):
                candidates.append(event)
            continue
        if isinstance(status, str) and _has_nonzero_tokens(event.get("usage")):
            candidates.append(event)

    if len(candidates) > 1:
        raise LLMRouterError(
            "agy output contained multiple terminal usage receipts",
            failure_type=FailureType.CONTRACT_VIOLATION,
        )
    if not candidates:
        if not isinstance(output, str) or not output.strip():
            reason = "agy -p output was empty"
        elif not events and unusable_event:
            reason = "agy -p output contained no parseable JSON usage line"
        else:
            reason = "agy -p output contained no terminal usage receipt"
        return _unavailable_usage(reason)
    return _parse_agy_receipt(candidates[0])


def _extract_response_text(stdout: str, *, output_format: str | None) -> str:
    """Separate agy's ``response`` field from its JSON usage receipt."""
    text = stdout.strip()
    if output_format != "json":
        return text
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("response"), str):
            return payload["response"].strip()
    return text


class AntigravityCLIProvider:
    """Invokes the Antigravity CLI via subprocess and returns its stdout."""

    name = "antigravity_cli"
    supported_types = {"antigravity_cli", "antigravity", "agy"}
    default_command = "agy"

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

        effort = config.get("effort")
        if effort is not None:
            _validate_effort(effort, provider_name=self.name)

        timeout = config.get("timeout")
        if timeout is not None and not isinstance(timeout, (int, float)):
            raise LLMRouterError(
                f"{self.name} provider key 'timeout' must be a number",
                failure_type=FailureType.PROVIDER_ERROR,
            )

        if "print_timeout" in config:
            print_timeout = config["print_timeout"]
            if not isinstance(print_timeout, str) or not print_timeout.strip():
                raise LLMRouterError(
                    f"{self.name} provider key 'print_timeout' "
                    "must be a non-empty string",
                    failure_type=FailureType.PROVIDER_ERROR,
                )

        if "output_format" in config:
            output_format = config["output_format"]
            if not isinstance(output_format, str) or not output_format.strip():
                raise LLMRouterError(
                    f"{self.name} provider key 'output_format' "
                    "must be a non-empty string",
                    failure_type=FailureType.PROVIDER_ERROR,
                )

    def build_command(
        self,
        config: dict[str, Any],
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]:
        """Return the dispatch command template as an argv list.

        The final positional element is the literal ``{prompt}`` placeholder,
        which :meth:`complete` replaces with the resolved prompt text.

        Args:
            config: Provider configuration mapping.
            model: Optional model override.
            effort: Optional effort override (``low``/``medium``/``high``).

        Returns:
            The argv list for the Antigravity CLI invocation.

        Raises:
            LLMRouterError: If the config or an override is invalid.
        """
        self.validate_config(config)
        command = config.get("command", self.default_command)
        resolved_model = model or config["model"]
        resolved_effort = effort if effort is not None else config.get("effort")

        cmd = [command, "--dangerously-skip-permissions", "--model", resolved_model]
        if resolved_effort is not None:
            _validate_effort(resolved_effort, provider_name=self.name)
            cmd.extend(["--effort", resolved_effort])
        output_format = config.get("output_format")
        if output_format is not None:
            cmd.extend(["--output-format", output_format])
        print_timeout = config.get("print_timeout")
        if print_timeout is not None:
            cmd.extend(["--print-timeout", print_timeout])
        cmd.extend(["-p", PROMPT_PLACEHOLDER])
        return cmd

    def complete(self, request: LLMRequest, config: dict[str, Any]) -> LLMResponse:
        """Run the Antigravity CLI and return its stdout as a response.

        Args:
            request: The routed completion request.
            config: Provider configuration mapping.

        Returns:
            The provider response carrying the CLI stdout.

        Raises:
            LLMRouterError: On timeout, missing binary, non-zero exit, or
                empty output.
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
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMRouterError(
                f"agy CLI timed out after {timeout}s",
                failure_type=FailureType.TIMEOUT,
                cause=exc,
            ) from exc
        except FileNotFoundError as exc:
            raise LLMRouterError(
                f"agy CLI binary not found: {command!r}",
                failure_type=FailureType.PROVIDER_ERROR,
                cause=exc,
            ) from exc

        if result.returncode != 0:
            detail = _snippet(result.stderr) or _snippet(result.stdout) or "no output"
            raise LLMRouterError(
                f"agy CLI exited {result.returncode}: {detail}",
                failure_type=FailureType.PROVIDER_ERROR,
            )

        output_format = config.get("output_format")
        text = _extract_response_text(result.stdout, output_format=output_format)
        if not text:
            raise LLMRouterError(
                "agy CLI returned empty output",
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


def _validate_effort(effort: Any, *, provider_name: str) -> None:
    """Raise unless ``effort`` is one of the supported levels.

    The ``agy`` CLI accepts only ``low``, ``medium``, or ``high`` reasoning
    effort (it has no ``max`` level).
    """
    if effort not in VALID_EFFORTS:
        raise LLMRouterError(
            f"{provider_name} provider key 'effort' must be one of "
            f"{', '.join(VALID_EFFORTS)}, got {effort!r}",
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
