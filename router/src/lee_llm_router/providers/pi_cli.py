"""Pi CLI command builder (P0-2b) and JSON usage parser (P1-4a).

Mirrors the proven argv shape from auto-orch's ``pi_stage_worker.py``
``_build_argv``: a fixed base of headless/read-only flags, optional
``--provider``/``--model``/``--thinking`` flags, and the literal
``{prompt}`` placeholder as the final positional element.

Deliberately scoped: this module builds argv and parses captured Pi
JSON-event output for authoritative usage (``capture_usage``). There is
no ``.complete()`` method, no subprocess execution, and no provider
registration here — those are later phases.
"""

from __future__ import annotations

import json
from typing import Any

from lee_llm_router.providers.base import FailureType, LLMRouterError
from lee_llm_router.staffing.json_int import int_from_decimal

PROMPT_PLACEHOLDER = "{prompt}"

# The proven read-only tool set from pi_stage_worker.py (Sprint 36 doctrine:
# stage workers produce text, they do not edit files).
READ_ONLY_TOOLS = "read,grep,find,ls"

# The exact proven system prompt from pi_stage_worker.py, verbatim.
SYSTEM_PROMPT = (
    "You are a headless stage worker. Output only the JSON response the "
    "prompt asks for - no prose, no code fences."
)

# Exact usage.source string required for Pi JSON events by D209 ruling 3
# via docs/staffing/phase1-contracts.md §Usage evidence taxonomy.
PI_USAGE_SOURCE = "pi --mode json events"

# Pi's JSON mode emits one terminal ``message_end`` event per assistant
# response and repeats aggregate state in ``agent_end``. Terminal
# assistant events are the only accounting boundary; every counted
# assistant must report all billable components.
_REQUIRED_USAGE_KEYS = ("input", "output", "cacheRead", "cacheWrite")
_REASONING_USAGE_KEYS = ("reasoning", "reasoningTokens", "reasoning_tokens")
_ID_KEYS = ("responseId", "response_id", "id")


def _nonnegative_int(value: Any) -> bool:
    """Return True only for a genuine nonnegative ``int`` (bools excluded)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _parse_json_events(output: str) -> list[dict[str, Any]]:
    """Parse Pi JSON-mode output as one JSON object or JSON lines.

    Mirrors the proven benchmark reader ``workbench/pi.py``
    ``_read_json_events``: a whole-text JSON object is a single event;
    otherwise each line is parsed independently and malformed lines are
    skipped. Non-dict JSON values are never events.

    Args:
        output: Captured Pi stdout in JSON mode.

    Returns:
        Every parsed JSON object event, in order; ``[]`` when the output
        is empty or contains nothing parseable.
    """
    if not isinstance(output, str) or not output.strip():
        return []
    try:
        whole = json.loads(output, parse_int=int_from_decimal)
    except json.JSONDecodeError:
        whole = None
    if isinstance(whole, dict):
        return [whole]
    events: list[dict[str, Any]] = []
    for line in output.splitlines():
        try:
            event = json.loads(line, parse_int=int_from_decimal)
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
    ``agent_end`` state is never counted. An assistant message carrying a
    stable id (``responseId``/``response_id``/``id``, checked on the
    nested message then the event) is kept once; messages without a
    stable id cannot be deduplicated and are always kept.

    Args:
        events: Parsed Pi JSON events.

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
    """Parse authoritative Pi JSON-mode usage from captured output.

    Implements the P1-4a usage parser (D209 ruling 3 via
    ``docs/staffing/phase1-contracts.md`` §Usage evidence taxonomy).
    Sums provider-reported, nonnegative token components from
    deduplicated assistant ``message_end`` events and never counts
    repeated ``agent_end`` state. Contradictory ``totalTokens`` figures
    or conflicting assistant models are rejected rather than estimated; a
    genuine reported zero is valid evidence; absent or partial usage is
    returned as ``unavailable`` with a specific reason. No token figure
    is ever estimated from text, context length, cost, or elapsed time.

    Args:
        output: Captured stdout of a ``pi --mode json events`` run — one
            JSON object or JSON lines. Malformed lines are skipped.

    Returns:
        A usage mapping that is directly schema-valid against the
        attempt-record v2 ``$defs/usage`` subschema (P1-5): ``basis``,
        ``source`` or ``unavailable_reason``, and the token counters
        ``input_tokens``, ``output_tokens``, ``cached_input_tokens``,
        ``reasoning_tokens``, ``total_tokens`` — nothing else. When every
        counted assistant terminal message reports all billable
        components (``input``, ``output``, ``cacheRead``, ``cacheWrite``),
        ``basis`` is ``provider_reported`` with the exact source
        ``pi --mode json events`` and summed integer counters.
        ``reasoning_tokens`` is summed only when every counted message
        reports a valid reasoning figure (an optional subset of output);
        otherwise it stays ``null`` with no reason field. Pi's
        ``cacheWrite`` contributes to ``total_tokens`` and to
        ``totalTokens`` reconciliation but is not surfaced as a schema
        field. When authoritative usage is absent or partial, ``basis``
        is ``unavailable`` with ``unavailable_reason`` and ``None``
        counters.

    Raises:
        LLMRouterError: With ``FailureType.CONTRACT_VIOLATION`` when a
            reported ``totalTokens`` contradicts its components, when the
            aggregate reported total contradicts the summed components,
            or when counted assistant messages disagree on the model.
    """
    events = _parse_json_events(output)
    messages = _assistant_terminal_messages(events)

    totals = {
        # cache_write_tokens is internal evidence only: Pi's cacheWrite
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
            # Reasoning is an optional subset of output. An absent, partial,
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
                    "Pi assistant usage totalTokens contradicts its components",
                    failure_type=FailureType.CONTRACT_VIOLATION,
                )
            reported_total += usage["totalTokens"]
            total_presence += 1

    if len(observed_models) > 1:
        raise LLMRouterError(
            "Pi JSON stream has conflicting assistant models",
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
                "Pi aggregate usage totalTokens contradicts its components",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
        # Schema-valid v2 usage only: cacheWrite is folded into
        # total_tokens but has no field of its own, and reasoning stays
        # null without an explanatory key when it is unknown.
        return {
            "basis": "provider_reported",
            "source": PI_USAGE_SOURCE,
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
                reason = "Pi JSON output was empty"
            else:
                reason = "Pi JSON output contained no parseable JSON events"
        else:
            reason = "Pi JSON output contained no assistant message_end events"
    else:
        reason = (
            "Pi assistant message_end usage was incomplete; one or more "
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


class PiCLIProvider:
    """Builds the argv for a headless ``pi`` CLI invocation.

    Command-builder only: compatible with the repository's provider
    conventions (``name``/``supported_types``/``validate_config``/
    ``build_command``) to the degree needed for later registration,
    but intentionally omits completion execution.
    """

    name = "pi_cli"
    supported_types = {"pi_cli", "pi"}
    default_command = "pi"

    def validate_config(self, config: dict[str, Any]) -> None:
        """Validate the provider config block types.

        Args:
            config: Provider configuration mapping. All keys are optional;
                when present, ``command``, ``provider``, ``model``,
                ``thinking``, ``effort``, and ``tools`` must be non-empty
                strings.

        Raises:
            LLMRouterError: With ``FailureType.PROVIDER_ERROR`` if a key
                has the wrong type or is empty.
        """
        for key in ("command", "provider", "model", "thinking", "effort", "tools"):
            value = config.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise LLMRouterError(
                    f"{self.name} provider key {key!r} must be a non-empty "
                    f"string, got {type(value).__name__}",
                    failure_type=FailureType.PROVIDER_ERROR,
                )

    def build_command(
        self,
        config: dict[str, Any],
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]:
        """Return the Pi CLI dispatch command as an argv list.

        The argv shape mirrors ``pi_stage_worker.py`` ``_build_argv``
        flag-for-flag — fixed headless/read-only base flags, then
        optional ``--provider``/``--model``/``--thinking`` flags, then
        the literal ``{prompt}`` placeholder as the final element — with
        one deliberate divergence: this builder emits ``--mode json``
        where the text-mode stage worker emits ``--mode text``, so
        captured stdout can feed the accepted ``capture_usage()`` parser
        (P1-4a). The argv is always a list; callers must never
        shell-quote it into one string.

        Governed ``run`` dispatch (P1-8) supplies ``config["tools"]`` —
        an explicit bounded editing allowlist such as
        :data:`_RUN_EDIT_TOOLS` in ``staffing/run.py`` — because headless
        implementation workers must edit their scoped files. The default
        stays :data:`READ_ONLY_TOOLS` so every legacy argv contract is
        untouched.

        Args:
            config: Provider configuration mapping. ``thinking`` (or the
                ``effort`` alias) supplies the ``--thinking`` value;
                ``tools`` overrides the ``--tools`` allowlist.
            model: Optional model override; beats ``config["model"]``.
            effort: Optional thinking-effort override; beats the config's
                ``thinking``/``effort`` value.

        Returns:
            The argv list for the Pi CLI invocation, ending in the literal
            ``{prompt}`` placeholder.

        Raises:
            LLMRouterError: If the config or overrides are of invalid type.
        """
        self.validate_config(config)

        command = config.get("command", self.default_command)
        provider = config.get("provider")
        tools = config.get("tools", READ_ONLY_TOOLS)
        resolved_model = model if model is not None else config.get("model")
        resolved_thinking = effort
        if resolved_thinking is None:
            resolved_thinking = config.get("thinking", config.get("effort"))
        if model is not None or effort is not None:
            # Overrides go through the same type checks as config values.
            self.validate_config(
                {
                    **({"model": model} if model is not None else {}),
                    **({"effort": effort} if effort is not None else {}),
                }
            )

        argv = [
            command,
            "--print",
            "--mode",
            "json",
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--tools",
            tools,
            "--system-prompt",
            SYSTEM_PROMPT,
        ]
        if provider:
            argv.extend(["--provider", provider])
        if resolved_model:
            argv.extend(["--model", resolved_model])
        if resolved_thinking:
            argv.extend(["--thinking", resolved_thinking])
        argv.append(PROMPT_PLACEHOLDER)
        return argv
