"""Subprocess provider for Codex CLI."""

from __future__ import annotations

import json
import subprocess
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from lee_llm_router.providers.base import FailureType, LLMRouterError
from lee_llm_router.response import LLMRequest, LLMResponse, LLMUsage

PROMPT_PLACEHOLDER = "{prompt}"

# Exact usage.source string required for Codex JSONL receipts by D209
# ruling 2 via docs/staffing/phase1-contracts.md §Usage evidence taxonomy.
CODEX_USAGE_SOURCE = "codex exec --json usage"

# Exact usage.source string required for Claude Code result-event receipts
# by docs/staffing/phase1-contracts.md §Usage evidence taxonomy (closed
# enum in the attempt-record v2 ``$defs/usage`` subschema). The governed
# argv behind it is ``claude -p --output-format stream-json``.
CLAUDE_USAGE_SOURCE = "claude -p --output-format stream-json result event"

# Governed capture + noninteractive-edit config for ``run`` (counterpart
# of the Codex ``json_flag``/``sandbox_args`` note): forces ``claude -p``
# to emit the stream-json result event that :func:`capture_claude_usage`
# parses, and grants the documented noninteractive permission flags a
# headless implementation worker needs to accept scoped edits:
# ``--permission-mode acceptEdits`` accepts file edits inside the
# permission rules and ``--permission-prompts none`` never opens an
# interactive prompt host (denials still fail closed). No bypass flag
# (``bypassPermissions`` / ``--dangerously-skip-permissions``) is used or
# ever emitted. The provider defaults stay null/empty so legacy Claude
# argv contracts are untouched.
CLAUDE_GOVERNED_CONFIG: dict[str, Any] = {
    "output_format": "stream-json",
    "permission_args": [
        "--permission-mode",
        "acceptEdits",
        "--permission-prompts",
        "none",
    ],
}

#: P1-8 fail-closed guard: the governed run argv must never carry a
#: permission bypass. ``--dangerously-skip-permissions`` is the documented
#: bypass flag and ``bypassPermissions`` is the documented bypass mode
#: value (also reachable as ``--permission-mode=bypassPermissions``).
_CLAUDE_BYPASS_FLAG_TOKENS = ("--dangerously-skip-permissions",)
_CLAUDE_BYPASS_VALUE_TOKEN = "bypassPermissions"

# ``codex exec --json`` emits JSONL events; the successful
# ``turn.completed`` receipt is the only accounting boundary. Interim
# events (``thread.started``, ``item.completed``, ``token_count``, …)
# never contribute counters, so interim usage is never double counted.
CODEX_TERMINAL_EVENT_TYPE = "turn.completed"
_CODEX_FAILURE_EVENT_TYPES = ("turn.failed", "error")
_CODEX_INPUT_KEYS = ("input_tokens", "prompt_tokens")
_CODEX_OUTPUT_KEYS = ("output_tokens", "completion_tokens")
_CODEX_CACHED_KEYS = ("cached_input_tokens", "cached_read_tokens")
_CODEX_REASONING_KEYS = ("reasoning_output_tokens", "reasoning_tokens")
_CODEX_TOTAL_KEYS = ("total_tokens",)


class CodexCLIProvider:
    """Invokes the Codex CLI via subprocess and returns its stdout."""

    name = "codex_cli"
    supported_types = {"codex_cli"}
    default_command = "codex"
    default_subcommand = "exec"
    default_model_flag = "--model"
    default_output_flag = None
    default_prompt_flag = None
    # Governed capture (``run``) sets ``json_flag: "--json"`` in the Codex
    # provider config so ``codex exec`` emits the JSONL usage receipt that
    # :func:`capture_usage` parses. The default stays null so legacy argv
    # contracts are untouched.
    default_json_flag: str | None = None

    def _resolve_config_command(self, config: dict[str, Any]) -> str:
        command = config.get("command", self.default_command)
        if not isinstance(command, str) or not command.strip():
            raise LLMRouterError(
                f"{self.name} provider missing required config key: 'command'",
                failure_type=FailureType.PROVIDER_ERROR,
            )
        return command

    def _resolve_config_string_flag(
        self, config: dict[str, Any], key: str, default: str | None
    ) -> str | None:
        value = config.get(key, default)
        if value is not None and not isinstance(value, str):
            raise LLMRouterError(
                f"{self.name} provider key {key!r} must be a string or null",
                failure_type=FailureType.PROVIDER_ERROR,
            )
        return value

    def _resolve_response_format(self, config: dict[str, Any]) -> str:
        response_format = str(config.get("response_format", "text"))
        if response_format not in {"text", "json"}:
            raise LLMRouterError(
                f"{self.name} provider key 'response_format' must be 'text' or 'json'",
                failure_type=FailureType.PROVIDER_ERROR,
            )
        return response_format

    def validate_config(self, config: dict[str, Any]) -> None:
        self._resolve_config_command(config)
        args = config.get("args", [])
        if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            raise LLMRouterError(
                f"{self.name} provider key 'args' must be a list of strings",
                failure_type=FailureType.PROVIDER_ERROR,
            )

        self._resolve_response_format(config)

        sandbox_args = config.get("sandbox_args", [])
        if not isinstance(sandbox_args, list) or any(
            not isinstance(arg, str) for arg in sandbox_args
        ):
            raise LLMRouterError(
                f"{self.name} provider key 'sandbox_args' must be a list of strings",
                failure_type=FailureType.PROVIDER_ERROR,
            )

        text_field = config.get("text_field")
        if text_field is not None and not isinstance(text_field, str):
            raise LLMRouterError(
                f"{self.name} provider key 'text_field' must be a string",
                failure_type=FailureType.PROVIDER_ERROR,
            )

        for key, default in (
            ("subcommand", self.default_subcommand),
            ("model_flag", self.default_model_flag),
            ("output_flag", self.default_output_flag),
            ("output_path", None),
            ("prompt_flag", self.default_prompt_flag),
            ("json_flag", self.default_json_flag),
        ):
            self._resolve_config_string_flag(config, key, default)

    def _effort_args(self, effort: str) -> list[str]:
        """Return the argv fragment that sets reasoning effort for this CLI.

        Args:
            effort: The reasoning-effort level to request.

        Returns:
            The argv fragment, or an empty list when the CLI has no effort
            flag.
        """
        return ["-c", f"model_reasoning_effort={effort}"]

    def build_command(
        self,
        config: dict[str, Any],
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]:
        """Return the dispatch command template as an argv list.

        The final positional element is the literal ``{prompt}`` placeholder,
        which :meth:`complete` replaces with the resolved prompt text.

        For governed capture, set ``config['json_flag']`` (e.g.
        ``"--json"``) to request the Codex JSONL usage receipt stream; the
        flag is inserted immediately after the subcommand so the relative
        order of the model, effort, output, prompt, and positional-prompt
        elements is unchanged. Governed ``run`` dispatch (P1-8) also sets
        ``config['sandbox_args']`` (e.g. ``["-s", "workspace-write",
        "--skip-git-repo-check"]``) — exec-level flags appended
        immediately after the json flag so a headless implementation
        worker can edit its scoped file in a non-git workdir. The default
        stays empty so legacy argv contracts are untouched.

        Args:
            config: Provider configuration mapping.
            model: Optional model override; falls back to ``config['model']``.
            effort: Optional reasoning-effort override; falls back to
                ``config['effort']``.

        Returns:
            The argv list for the CLI invocation.

        Raises:
            LLMRouterError: If the config is invalid.
        """
        self.validate_config(config)

        command = self._resolve_config_command(config)
        subcommand = self._resolve_config_string_flag(
            config,
            "subcommand",
            self.default_subcommand,
        )
        extra_args = list(config.get("args", []))
        model_flag = self._resolve_config_string_flag(
            config,
            "model_flag",
            self.default_model_flag,
        )
        output_flag = self._resolve_config_string_flag(
            config,
            "output_flag",
            self.default_output_flag,
        )
        output_path = self._resolve_config_string_flag(
            config,
            "output_path",
            None,
        )
        prompt_flag = self._resolve_config_string_flag(
            config,
            "prompt_flag",
            self.default_prompt_flag,
        )
        json_flag = self._resolve_config_string_flag(
            config,
            "json_flag",
            self.default_json_flag,
        )
        sandbox_args = config.get("sandbox_args", [])
        if not isinstance(sandbox_args, list) or any(
            not isinstance(arg, str) for arg in sandbox_args
        ):
            raise LLMRouterError(
                f"{self.name} provider key 'sandbox_args' must be a list of strings",
                failure_type=FailureType.PROVIDER_ERROR,
            )
        resolved_model = model or config.get("model") or ""
        resolved_effort = effort if effort is not None else config.get("effort")

        cmd = [command, *extra_args]
        if subcommand:
            cmd.append(subcommand)
        if json_flag:
            cmd.append(json_flag)
        if sandbox_args:
            cmd.extend(sandbox_args)
        if resolved_model and model_flag:
            cmd.extend([model_flag, str(resolved_model)])
        if resolved_effort:
            cmd.extend(self._effort_args(str(resolved_effort)))
        if output_flag:
            cmd.append(output_flag)
            if output_path:
                cmd.append(str(output_path))
        if prompt_flag:
            cmd.append(prompt_flag)
        cmd.append(PROMPT_PLACEHOLDER)
        return cmd

    def complete(self, request: LLMRequest, config: dict[str, Any]) -> LLMResponse:
        self.validate_config(config)

        command = self._resolve_config_command(config)
        timeout = request.timeout or float(config.get("timeout", 120.0))
        response_format = self._resolve_response_format(config)
        output_path = self._resolve_config_string_flag(config, "output_path", None)

        # Build prompt from last user message
        user_messages = [m for m in request.messages if m.get("role") == "user"]
        prompt = user_messages[-1]["content"] if user_messages else ""

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
                f"Codex CLI timed out after {timeout}s",
                failure_type=FailureType.TIMEOUT,
                cause=exc,
            ) from exc
        except FileNotFoundError as exc:
            raise LLMRouterError(
                f"Codex CLI binary not found: {command!r}",
                failure_type=FailureType.PROVIDER_ERROR,
                cause=exc,
            ) from exc

        if result.returncode != 0:
            detail = _snippet(result.stderr) or _snippet(result.stdout) or "no output"
            raise LLMRouterError(
                f"Codex CLI exited {result.returncode}: {detail}",
                failure_type=FailureType.PROVIDER_ERROR,
            )

        output_text = None
        if output_path:
            out_file = Path(output_path)
            if out_file.is_file():
                output_text = out_file.read_text(encoding="utf-8")

        return _build_response(
            request=request,
            result=result,
            command=cmd,
            response_format=response_format,
            text_field=config.get("text_field"),
            provider=self.name,
            output_text=output_text,
        )


def _build_response(
    *,
    request: LLMRequest,
    result: subprocess.CompletedProcess[str],
    command: list[str],
    response_format: str,
    provider: str,
    text_field: str | None,
    output_text: str | None = None,
) -> LLMResponse:
    stdout = (output_text if output_text is not None else result.stdout).strip()
    raw: dict[str, Any] = {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
        "command": command,
    }

    if response_format == "json":
        payload = _parse_json_payload(stdout)
        text = _extract_text(payload, text_field)
        raw["parsed"] = payload
        usage = _usage_from_payload(payload)
        model = str(payload.get("model") or request.model or "")
    else:
        if not stdout:
            raise LLMRouterError(
                "Codex CLI returned empty output",
                failure_type=FailureType.INVALID_RESPONSE,
            )
        text = stdout
        usage = LLMUsage()
        model = request.model

    return LLMResponse(
        text=text,
        raw=raw,
        usage=usage,
        request_id=request.request_id,
        model=model,
        provider=provider,
    )


class GeminiCLIProvider(CodexCLIProvider):
    """Invokes the Gemini CLI via subprocess and returns its stdout."""

    name = "gemini_cli"
    supported_types = {"gemini_cli", "gemini"}
    default_command = "gemini"
    default_subcommand = None
    default_model_flag = None
    default_output_flag = None
    default_prompt_flag = "-p"

    def _effort_args(self, effort: str) -> list[str]:
        """Return an empty fragment: the Gemini CLI has no effort flag.

        Args:
            effort: Ignored.

        Returns:
            An empty list.
        """
        return []


class ClaudeCodeCLIProvider(CodexCLIProvider):
    """Invokes the Claude CLI via subprocess and returns its stdout."""

    name = "claude_code_cli"
    supported_types = {"claude_code_cli", "claude_code"}
    default_command = "claude"
    default_subcommand = None
    default_model_flag = "--model"
    default_output_flag = None
    default_prompt_flag = "-p"
    # Governed capture (``run``) sets ``output_format: "stream-json"``
    # (see :data:`CLAUDE_GOVERNED_CONFIG`) so ``claude -p`` emits the
    # stream-json result event that :func:`capture_claude_usage` parses,
    # plus ``permission_args`` so the headless worker can accept scoped
    # edits noninteractively. The defaults stay null/empty so legacy argv
    # contracts are untouched.
    default_output_format: str | None = None

    def _resolve_output_format(self, config: dict[str, Any]) -> str | None:
        return self._resolve_config_string_flag(
            config,
            "output_format",
            self.default_output_format,
        )

    def _resolve_permission_args(self, config: dict[str, Any]) -> list[str]:
        """Return the governed permission argv fragment, fail-closed on bypass.

        Governed ``run`` dispatch sets ``permission_args`` (the documented
        safe noninteractive pair from :data:`CLAUDE_GOVERNED_CONFIG`) so
        ``claude -p`` can accept scoped edits without an interactive host.
        Any bypass flag or bypass mode value is rejected instead of
        emitted.

        Raises:
            LLMRouterError: When the key is not a list of strings or any
                element is a permission bypass flag/value.
        """
        args = config.get("permission_args", [])
        if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            raise LLMRouterError(
                f"{self.name} provider key 'permission_args' must be a list "
                "of strings",
                failure_type=FailureType.PROVIDER_ERROR,
            )
        for arg in args:
            if arg in _CLAUDE_BYPASS_FLAG_TOKENS or _CLAUDE_BYPASS_VALUE_TOKEN in arg:
                raise LLMRouterError(
                    f"{self.name} provider key 'permission_args' must never "
                    f"contain a permission bypass flag: {arg!r}",
                    failure_type=FailureType.PROVIDER_ERROR,
                )
        return args

    def validate_config(self, config: dict[str, Any]) -> None:
        super().validate_config(config)
        self._resolve_output_format(config)
        self._resolve_permission_args(config)

    def build_command(
        self,
        config: dict[str, Any],
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]:
        """Return the Claude CLI dispatch command template as an argv list.

        Mirrors the Codex builder, with two governed-run additions, both
        inserted immediately before the trailing ``{prompt}`` placeholder:

        * when ``config['output_format']`` is set (governed capture passes
          ``"stream-json"`` via :data:`CLAUDE_GOVERNED_CONFIG`), the pair
          ``["--output-format", value]`` so the argv carries the stream-json
          result event behind the :data:`CLAUDE_USAGE_SOURCE` taxonomy
          string;
        * ``config['permission_args']`` (the documented safe noninteractive
          pair ``--permission-mode acceptEdits --permission-prompts none``
          from :data:`CLAUDE_GOVERNED_CONFIG`) so a headless implementation
          worker can accept scoped edits; bypass flags are rejected by
          :meth:`_resolve_permission_args` and never emitted.

        The defaults stay null/empty so legacy argv contracts are untouched.

        Args:
            config: Provider configuration mapping.
            model: Optional model override; falls back to ``config['model']``.
            effort: Optional reasoning-effort override; falls back to
                ``config['effort']``.

        Returns:
            The argv list for the CLI invocation.

        Raises:
            LLMRouterError: If the config is invalid.
        """
        output_format = self._resolve_output_format(config)
        permission_args = self._resolve_permission_args(config)
        cmd = list(super().build_command(config, model=model, effort=effort))
        tail: list[str] = []
        if output_format:
            tail.extend(["--output-format", output_format])
            if output_format == "stream-json":
                # The Claude CLI refuses `--print --output-format=stream-json`
                # without `--verbose` ("Error: When using --print,
                # --output-format=stream-json requires --verbose"), exiting
                # before it emits anything. Without this flag every governed
                # Claude dispatch fails instantly with empty stdout and no
                # usage record. Emitted only for stream-json so other output
                # formats keep their existing argv contract.
                tail.append("--verbose")
        tail.extend(permission_args)
        if tail:
            if cmd and cmd[-1] == PROMPT_PLACEHOLDER:
                cmd[len(cmd) - 1 : len(cmd) - 1] = tail
            else:
                cmd.extend(tail)
        return cmd

    def _effort_args(self, effort: str) -> list[str]:
        """Return the Claude CLI's effort fragment.

        Args:
            effort: The reasoning-effort level to request.

        Returns:
            ``["--effort", effort]``.
        """
        return ["--effort", effort]


def _parse_json_payload(stdout: str) -> dict[str, Any]:
    if not stdout:
        raise LLMRouterError(
            "Codex CLI returned empty JSON output",
            failure_type=FailureType.CONTRACT_VIOLATION,
        )
    try:
        payload = json.loads(stdout)
    except JSONDecodeError as exc:
        raise LLMRouterError(
            "Codex CLI returned malformed JSON output",
            failure_type=FailureType.CONTRACT_VIOLATION,
            cause=exc,
        ) from exc
    if not isinstance(payload, dict):
        raise LLMRouterError(
            "Codex CLI JSON output must be an object",
            failure_type=FailureType.CONTRACT_VIOLATION,
        )
    return payload


def _extract_text(payload: dict[str, Any], text_field: str | None) -> str:
    candidate_fields = [text_field] if text_field else ["output_text", "text"]
    for field in candidate_fields:
        if not field:
            continue
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise LLMRouterError(
        "Codex CLI JSON output missing a non-empty response text field",
        failure_type=FailureType.CONTRACT_VIOLATION,
    )


def _usage_from_payload(payload: dict[str, Any]) -> LLMUsage:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return LLMUsage()
    prompt_tokens = _coerce_usage_value(
        usage.get("prompt_tokens", usage.get("input_tokens", 0)),
        field_name="prompt_tokens",
    )
    completion_tokens = _coerce_usage_value(
        usage.get("completion_tokens", usage.get("output_tokens", 0)),
        field_name="completion_tokens",
    )
    total_tokens = _coerce_usage_value(
        usage.get("total_tokens", prompt_tokens + completion_tokens),
        field_name="total_tokens",
    )
    return LLMUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


def _coerce_usage_value(value: Any, *, field_name: str) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError) as exc:
        raise LLMRouterError(
            f"Codex CLI JSON usage field {field_name!r} must be an integer",
            failure_type=FailureType.CONTRACT_VIOLATION,
            cause=exc,
        ) from exc


def _nonnegative_int(value: Any) -> bool:
    """Return True only for a genuine nonnegative ``int`` (bools excluded)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _parse_codex_events(output: str) -> list[dict[str, Any]]:
    """Parse ``codex --json`` output as one JSON object or JSONL events.

    A whole-text JSON object is a single event; otherwise every non-blank
    line must parse as a JSON object. Unlike lenient stream readers, a
    malformed or non-object event fails closed: the receipt stream is the
    sole usage authority, and a corrupted capture must never degrade into
    an estimate.

    Args:
        output: Captured stdout of a ``codex exec --json`` run.

    Returns:
        Every parsed JSON object event, in order; ``[]`` when the output
        is empty or whitespace only.

    Raises:
        LLMRouterError: With ``FailureType.CONTRACT_VIOLATION`` when the
            output is a whole-text non-object, or any line is malformed
            or is not a JSON object.
    """
    if not isinstance(output, str) or not output.strip():
        return []
    try:
        whole = json.loads(output)
    except JSONDecodeError:
        whole = None
    if whole is not None:
        if not isinstance(whole, dict):
            raise LLMRouterError(
                "Codex --json output must be a JSON object or JSONL events",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
        return [whole]
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(output.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except JSONDecodeError as exc:
            raise LLMRouterError(
                f"invalid Codex JSONL event on line {line_number}",
                failure_type=FailureType.CONTRACT_VIOLATION,
                cause=exc,
            ) from exc
        if not isinstance(event, dict):
            raise LLMRouterError(
                f"invalid Codex JSONL event on line {line_number}: "
                "event must be an object",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
        events.append(event)
    return events


def _consistent_codex_usage_value(
    mappings: tuple[dict[str, Any], ...], keys: tuple[str, ...]
) -> Any:
    """Return the single consistent value for a usage key family.

    Mirrors agent-orch ``worker.py`` ``_parse_provider_usage``: every key
    of the family is looked up across the nested ``usage`` mapping and the
    receipt itself, and alias keys that disagree in value or type fail
    closed instead of picking one silently.

    Args:
        mappings: Mappings to read, in precedence order.
        keys: Alias key names for one counter family.

    Returns:
        The single present value, or ``None`` when the family is absent.

    Raises:
        LLMRouterError: With ``FailureType.CONTRACT_VIOLATION`` when two
            present members of the family disagree.
    """
    values: list[tuple[str, Any]] = []
    for mapping in mappings:
        for key in keys:
            if key in mapping:
                values.append((key, mapping[key]))
    if not values:
        return None
    first_key, first_value = values[0]
    for key, value in values[1:]:
        if value != first_value or type(value) is not type(first_value):
            raise LLMRouterError(
                "Codex usage contains conflicting values for "
                f"{first_key!r} and {key!r}",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
    return first_value


def _codex_unavailable_usage(reason: str) -> dict[str, Any]:
    """Return the schema-valid unavailable v2 usage mapping with null counters."""
    return {
        "basis": "unavailable",
        "unavailable_reason": reason,
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
    }


def capture_usage(output: str) -> dict[str, Any]:
    """Parse authoritative Codex JSONL usage from captured output.

    Implements the P1-4b usage parser (D209 ruling 2 via
    ``docs/staffing/phase1-contracts.md`` §Usage evidence taxonomy). Only
    the successful terminal ``turn.completed`` receipt is read; interim
    JSONL events are ignored so usage is never double counted. Key alias
    families are reconciled like the agent-orch ``worker.py`` Codex
    receipt parser: conflicting aliases, multiple terminal receipts,
    terminal failure events, malformed JSONL, and counters that
    contradict each other fail closed with
    ``FailureType.CONTRACT_VIOLATION`` rather than estimating. No token
    figure is ever estimated from text, context length, cost, or elapsed
    time.

    Args:
        output: Captured stdout of a ``codex exec --json`` run — one JSON
            object or JSON lines.

    Returns:
        A usage mapping that is directly schema-valid against the
        attempt-record v2 ``$defs/usage`` subschema (P1-5): ``basis``,
        ``source`` or ``unavailable_reason``, and the token counters
        ``input_tokens``, ``output_tokens``, ``cached_input_tokens``,
        ``reasoning_tokens``, ``total_tokens`` — nothing else. When the
        terminal receipt reports valid required input and output counts,
        ``basis`` is ``provider_reported`` with the exact source
        ``codex exec --json usage``; ``total_tokens`` is the reported
        total (reconciled against ``input_tokens + output_tokens``) or,
        when the receipt reports none, the sum of the reported
        components. ``cached_input_tokens`` and ``reasoning_tokens``
        stay ``null`` when the receipt does not report them, and a
        genuine source-reported zero stays zero. When usage is missing
        or required input/output is absent or invalid, ``basis`` is
        ``unavailable`` with a specific ``unavailable_reason`` and
        ``None`` counters — never default zeros.

    Raises:
        LLMRouterError: With ``FailureType.CONTRACT_VIOLATION`` when a
            JSONL line is malformed or not an object, the whole-output
            JSON is not an object, the stream emits a terminal failure
            event or multiple ``turn.completed`` receipts, the
            ``usage`` field is not an object, alias keys conflict, an
            optional reported counter is not a nonnegative integer, a
            reported total contradicts its components, reasoning exceeds
            output, or cached input exceeds input tokens.
    """
    events = _parse_codex_events(output)

    terminal_receipt: dict[str, Any] | None = None
    for event in events:
        event_type = event.get("type")
        if event_type in _CODEX_FAILURE_EVENT_TYPES:
            raise LLMRouterError(
                f"Codex emitted terminal failure event {event_type!r}",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
        if event_type != CODEX_TERMINAL_EVENT_TYPE:
            continue
        if terminal_receipt is not None:
            raise LLMRouterError(
                "Codex emitted multiple terminal usage receipts",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
        terminal_receipt = event

    if terminal_receipt is None:
        if not isinstance(output, str) or not output.strip():
            reason = "Codex --json output was empty"
        else:
            reason = (
                "Codex --json output contained no terminal "
                "turn.completed usage receipt"
            )
        return _codex_unavailable_usage(reason)

    raw_usage = terminal_receipt.get("usage")
    if raw_usage is not None and not isinstance(raw_usage, dict):
        raise LLMRouterError(
            "Codex turn.completed usage field must be an object",
            failure_type=FailureType.CONTRACT_VIOLATION,
        )
    mappings = (
        (raw_usage, terminal_receipt) if raw_usage is not None else (terminal_receipt,)
    )

    input_value = _consistent_codex_usage_value(mappings, _CODEX_INPUT_KEYS)
    output_value = _consistent_codex_usage_value(mappings, _CODEX_OUTPUT_KEYS)
    if not _nonnegative_int(input_value) or not _nonnegative_int(output_value):
        if raw_usage is None:
            reason = "Codex turn.completed receipt carried no usage object"
        else:
            reason = (
                "Codex turn.completed usage was missing or had invalid "
                "required input_tokens/output_tokens"
            )
        return _codex_unavailable_usage(reason)

    cached_value = _consistent_codex_usage_value(mappings, _CODEX_CACHED_KEYS)
    reasoning_value = _consistent_codex_usage_value(mappings, _CODEX_REASONING_KEYS)
    total_value = _consistent_codex_usage_value(mappings, _CODEX_TOTAL_KEYS)
    for name, value in (
        ("cached_input_tokens", cached_value),
        ("reasoning_output_tokens", reasoning_value),
        ("total_tokens", total_value),
    ):
        if value is not None and not _nonnegative_int(value):
            raise LLMRouterError(
                f"Codex usage field {name!r} must be a nonnegative integer",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )

    # Codex reports cached input and reasoning as subsets of input and
    # output respectively, with total = input + output; reported evidence
    # contradicting those invariants fails closed.
    if total_value is not None and total_value != input_value + output_value:
        raise LLMRouterError(
            "Codex usage total_tokens contradicts input_tokens + output_tokens",
            failure_type=FailureType.CONTRACT_VIOLATION,
        )
    if reasoning_value is not None and reasoning_value > output_value:
        raise LLMRouterError(
            "Codex usage reasoning_output_tokens contradicts output_tokens",
            failure_type=FailureType.CONTRACT_VIOLATION,
        )
    if cached_value is not None and cached_value > input_value:
        raise LLMRouterError(
            "Codex usage cached_input_tokens contradicts input_tokens",
            failure_type=FailureType.CONTRACT_VIOLATION,
        )

    # Final counter assembly reuses the existing payload usage reader:
    # values are already strictly validated, and the reader derives the
    # total from the reported components when the receipt omits one.
    validated_usage: dict[str, Any] = {
        "prompt_tokens": input_value,
        "completion_tokens": output_value,
    }
    if total_value is not None:
        validated_usage["total_tokens"] = total_value
    counters = _usage_from_payload({"usage": validated_usage})

    return {
        "basis": "provider_reported",
        "source": CODEX_USAGE_SOURCE,
        "input_tokens": counters.prompt_tokens,
        "output_tokens": counters.completion_tokens,
        "cached_input_tokens": cached_value,
        "reasoning_tokens": reasoning_value,
        "total_tokens": counters.total_tokens,
    }


# Claude-specific usage key families (``claude -p --output-format
# stream-json`` result events use camelCase; snake aliases are accepted
# for parity with the benchmark capture reader).
_CLAUDE_RESULT_EVENT_TYPE = "result"
_CLAUDE_INPUT_KEYS = ("input_tokens", "inputTokens")
_CLAUDE_OUTPUT_KEYS = ("output_tokens", "outputTokens")
_CLAUDE_CACHED_KEYS = (
    "cache_read_input_tokens",
    "cacheReadInputTokens",
    "cached_input_tokens",
)
_CLAUDE_CACHE_WRITE_KEYS = (
    "cache_creation_input_tokens",
    "cacheCreationInputTokens",
    "cache_write_input_tokens",
)
_CLAUDE_TOTAL_KEYS = ("total_tokens", "totalTokens")


def _claude_unavailable_usage(reason: str) -> dict[str, Any]:
    """Return the schema-valid unavailable v2 usage mapping with null counters."""
    return _codex_unavailable_usage(reason)


def _consistent_claude_usage_value(
    mappings: tuple[dict[str, Any], ...], keys: tuple[str, ...]
) -> Any:
    """Return the single consistent value for a Claude usage key family.

    Same reconciliation rules as the Codex receipt parser: every alias
    key of the family is looked up across the given mappings, and alias
    keys that disagree in value or type fail closed instead of picking
    one silently.

    Args:
        mappings: Mappings to read, in precedence order.
        keys: Alias key names for one counter family.

    Returns:
        The single present value, or ``None`` when the family is absent.

    Raises:
        LLMRouterError: With ``FailureType.CONTRACT_VIOLATION`` when two
            present members of the family disagree.
    """
    values: list[tuple[str, Any]] = []
    for mapping in mappings:
        for key in keys:
            if key in mapping:
                values.append((key, mapping[key]))
    if not values:
        return None
    first_key, first_value = values[0]
    for key, value in values[1:]:
        if value != first_value or type(value) is not type(first_value):
            raise LLMRouterError(
                "Claude result usage contains conflicting values for "
                f"{first_key!r} and {key!r}",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
    return first_value


def _parse_claude_events(output: str) -> tuple[list[dict[str, Any]], str | None]:
    """Parse ``claude -p --output-format stream-json`` output leniently.

    A whole-text JSON object is a single event; otherwise every non-blank
    line must parse as a JSON object. Unlike the Codex receipt parser,
    unusable output does not raise: the governed Claude capture fails
    closed as ``unavailable`` with a specific reason.

    Args:
        output: Captured stdout of a governed ``claude -p`` run.

    Returns:
        ``(events, None)`` with every parsed JSON object event in order,
        or ``([], reason)`` when the output is empty, is a whole-text
        non-object, or contains a malformed or non-object line.
    """
    if not isinstance(output, str) or not output.strip():
        return [], "Claude stream-json output was empty"
    try:
        whole = json.loads(output)
    except JSONDecodeError:
        whole = None
    if whole is not None:
        if not isinstance(whole, dict):
            return [], "Claude stream-json output was a JSON value, not an object"
        return [whole], None
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(output.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except JSONDecodeError:
            return [], (
                f"Claude stream-json output had malformed JSON on line "
                f"{line_number}"
            )
        if not isinstance(event, dict):
            return [], (
                "Claude stream-json output had a non-object JSON event on "
                f"line {line_number}"
            )
        events.append(event)
    return events, None


def _claude_cache_counters(
    mapping: dict[str, Any],
    *,
    subject: str,
) -> tuple[int | None, int | None, int | None]:
    """Validate optional cache and total evidence of one Claude usage mapping.

    Cache reads map to ``cached_input_tokens``. Cache-creation figures
    contribute to ``total_tokens`` and contradiction checking but are
    never surfaced as a schema field in the attempt-record v2 usage
    mapping. A reported total figure is validated as a nonnegative
    integer and checked against contributing components.

    Args:
        mapping: The ``modelUsage`` row or top-level ``usage`` mapping.
        subject: Human-readable evidence description for error messages.

    Returns:
        ``(cached_read, cache_write, total)`` where each component is a
        nonnegative integer when present, or ``None`` when absent.

    Raises:
        LLMRouterError: With ``FailureType.CONTRACT_VIOLATION`` when a
            present counter is not a nonnegative integer or alias keys
            conflict.
    """
    cached_value = _consistent_claude_usage_value((mapping,), _CLAUDE_CACHED_KEYS)
    cache_write_value = _consistent_claude_usage_value(
        (mapping,), _CLAUDE_CACHE_WRITE_KEYS
    )
    total_value = _consistent_claude_usage_value((mapping,), _CLAUDE_TOTAL_KEYS)
    for name, value in (
        ("cacheReadInputTokens", cached_value),
        ("cacheCreationInputTokens", cache_write_value),
        ("totalTokens", total_value),
    ):
        if value is not None and not _nonnegative_int(value):
            raise LLMRouterError(
                f"Claude result {subject} field {name!r} must be a "
                "nonnegative integer",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
    return cached_value, cache_write_value, total_value


def _usage_from_claude_model_usage(
    model_usage: dict[str, Any], event_total: int | None = None
) -> dict[str, Any]:
    """Sum Claude ``modelUsage`` model rows once into aggregate counters.

    Each row must report valid nonnegative integer ``inputTokens`` and
    ``outputTokens`` (snake aliases accepted). A row missing either
    fails closed as ``unavailable`` evidence; a row reporting an invalid
    value (bool, negative, fractional) raises. Cache reads map to
    ``cached_input_tokens`` and cache creation contributes to
    ``total_tokens`` (omitted from schema fields). If an optional cache
    component is absent in any contributing row, it is not assumed to
    be zero: ``cached_input_tokens`` stays null, and ``total_tokens``
    stays null unless authoritatively reported. The top-level result
    ``usage`` object is never added on top of ``modelUsage``.

    Args:
        model_usage: The result event's ``modelUsage`` mapping.

    Args:
        model_usage: The result event's ``modelUsage`` mapping.
        event_total: The result event's own total token figure when
            authoritatively reported, validated by the caller.

    Returns:
        A schema-valid v2 usage mapping: ``provider_reported`` with the
        reconciled rows, or ``unavailable`` with a specific reason when
        a row lacks required input/output counts.

    Raises:
        LLMRouterError: With ``FailureType.CONTRACT_VIOLATION`` when a
            row is not an object, a required counter is present but
            invalid, an optional cache counter or total is present but
            invalid, total contradicts components, a reported total is
            below the components its rows do report, the event total
            contradicts the reconciled row aggregate, or alias keys
            conflict.
    """
    input_tokens = 0
    output_tokens = 0
    cached_tokens_sum = 0
    cache_write_tokens_sum = 0

    cached_read_presence = 0
    cache_write_presence = 0

    # Per-row ``(exact_total, lower_bound)`` reconciliation figures: a
    # reported row total is exact for that row, and so is the component
    # sum of a row reporting both cache components; a row with neither
    # contributes only its known-component lower bound, because an
    # absent cache component is unknown and only adds tokens.
    row_bounds: list[tuple[int | None, int]] = []

    num_rows = len(model_usage)

    for model_id, row in model_usage.items():
        if not isinstance(row, dict):
            raise LLMRouterError(
                f"Claude result modelUsage row for model {model_id!r} "
                "must be an object",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
        input_value = _consistent_claude_usage_value((row,), _CLAUDE_INPUT_KEYS)
        output_value = _consistent_claude_usage_value((row,), _CLAUDE_OUTPUT_KEYS)
        if input_value is None or output_value is None:
            return _claude_unavailable_usage(
                f"Claude result modelUsage row for model {model_id!r} "
                "lacked valid inputTokens/outputTokens"
            )
        if not _nonnegative_int(input_value) or not _nonnegative_int(output_value):
            raise LLMRouterError(
                f"Claude result modelUsage row for model {model_id!r} "
                "must report nonnegative integer inputTokens/outputTokens",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
        cached_val, cache_write_val, row_total_val = _claude_cache_counters(
            row,
            subject=f"modelUsage row for model {model_id!r}",
        )
        # A reported row total is validated against complete row
        # components when every component is present, and against the
        # known-component lower bound otherwise: an absent cache
        # component is unknown, never zero, so it can only add tokens —
        # a row total below the components the row does report is a
        # contradiction that would silently discard known cache
        # evidence (Astra final re-review finding 1).
        row_known_components = input_value + output_value
        if cached_val is not None:
            row_known_components += cached_val
        if cache_write_val is not None:
            row_known_components += cache_write_val
        if row_total_val is not None:
            row_cache_complete = cached_val is not None and cache_write_val is not None
            if row_cache_complete:
                expected_row_total = (
                    input_value + output_value + cached_val + cache_write_val
                )
                if row_total_val != expected_row_total:
                    raise LLMRouterError(
                        f"Claude result modelUsage row for model {model_id!r} "
                        "total contradicts its components",
                        failure_type=FailureType.CONTRACT_VIOLATION,
                    )
            elif row_total_val < row_known_components:
                raise LLMRouterError(
                    f"Claude result modelUsage row for model {model_id!r} "
                    f"total {row_total_val} is below its known components "
                    f"({row_known_components}); the absent cache component "
                    "is unknown and cannot reduce the total",
                    failure_type=FailureType.CONTRACT_VIOLATION,
                )

        if row_total_val is not None:
            row_exact_total: int | None = row_total_val
            row_lower_bound = row_total_val
        elif cached_val is not None and cache_write_val is not None:
            row_exact_total = row_known_components
            row_lower_bound = row_known_components
        else:
            row_exact_total = None
            row_lower_bound = row_known_components
        row_bounds.append((row_exact_total, row_lower_bound))

        input_tokens += input_value
        output_tokens += output_value

        if cached_val is not None:
            cached_tokens_sum += cached_val
            cached_read_presence += 1

        if cache_write_val is not None:
            cache_write_tokens_sum += cache_write_val
            cache_write_presence += 1

    if cached_read_presence == num_rows:
        aggregate_cached_tokens: int | None = cached_tokens_sum
    else:
        aggregate_cached_tokens = None

    # D209 usage truth (Astra final-gate finding 1): the aggregate total
    # reconciles every reported row total with the other rows' known
    # component lower bounds. A row figure is exact when it reports a
    # total or reports both cache components; otherwise its known
    # components are only a lower bound. The aggregate total is exact
    # only when every row is exact — reported row totals are never
    # discarded just because other rows lack totals — and stays unknown
    # when any row's cache evidence is unknown and unreported.
    exact_row_totals: list[int] = []
    aggregate_lower_bound = 0
    all_rows_exact = True
    for row_exact_total, row_lower_bound in row_bounds:
        if row_exact_total is None:
            all_rows_exact = False
        else:
            exact_row_totals.append(row_exact_total)
        aggregate_lower_bound += row_lower_bound

    total_tokens: int | None = sum(exact_row_totals) if all_rows_exact else None

    # D209 usage truth: the event total can never contradict the
    # reconciled row aggregate. When every row figure is exact, the
    # summed row truth is the aggregate and the event total must equal
    # it. When some row's cache is unknown, the summed known figures are
    # only a lower bound — reported row totals and known cache evidence
    # can never be reduced by a smaller event total, so one below the
    # lower bound is rejected instead of silently discarding known row
    # evidence; a total at or above the lower bound is consistent and
    # becomes the aggregate.
    if event_total is not None:
        if total_tokens is not None and event_total != total_tokens:
            raise LLMRouterError(
                "Claude result event total contradicts its components",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
        if total_tokens is None:
            if event_total < aggregate_lower_bound:
                raise LLMRouterError(
                    f"Claude result event total {event_total} is below its "
                    f"known components ({aggregate_lower_bound}); reported "
                    "row totals and unknown cache components cannot be "
                    "reduced by a smaller aggregate",
                    failure_type=FailureType.CONTRACT_VIOLATION,
                )
            total_tokens = event_total

    return {
        "basis": "provider_reported",
        "source": CLAUDE_USAGE_SOURCE,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": aggregate_cached_tokens,
        "reasoning_tokens": None,
        "total_tokens": total_tokens,
    }


def _usage_from_claude_result_usage(
    usage: dict[str, Any], event_total: int | None = None
) -> dict[str, Any]:
    """Read the top-level Claude result ``usage`` object as v2 usage.

    Fallback path used only when ``modelUsage`` is absent. Snake/camel
    aliases are reconciled; missing input or output counts fail closed
    as ``unavailable`` while present-but-invalid values raise. Cache
    reads map to ``cached_input_tokens``; cache creation contributes to
    ``total_tokens`` contradiction checking. ``total_tokens`` stays
    unknown unless the source reports it or both cache components are
    reported and the component sum is calculable — an absent cache
    component is unknown, never zero.

    Args:
        usage: The result event's ``usage`` mapping.
        event_total: The result event's own total token figure when
            authoritatively reported, validated by the caller.

    Returns:
        A schema-valid v2 usage mapping.

    Raises:
        LLMRouterError: With ``FailureType.CONTRACT_VIOLATION`` when a
            present counter is not a nonnegative integer, total
            contradicts components, a reported total is below the
            components the source does report, the event total
            contradicts the usage evidence, or alias keys conflict.
    """
    input_value = _consistent_claude_usage_value((usage,), _CLAUDE_INPUT_KEYS)
    output_value = _consistent_claude_usage_value((usage,), _CLAUDE_OUTPUT_KEYS)
    if input_value is None or output_value is None:
        return _claude_unavailable_usage(
            "Claude result usage lacked valid input/output token counts"
        )
    if not _nonnegative_int(input_value) or not _nonnegative_int(output_value):
        raise LLMRouterError(
            "Claude result usage must report nonnegative integer "
            "input/output token counts",
            failure_type=FailureType.CONTRACT_VIOLATION,
        )
    cached_value, cache_write_value, total_value = _claude_cache_counters(
        usage,
        subject="usage",
    )
    # D209 usage truth: the total stays unknown unless the source reports
    # it or it is calculated from complete authoritative components.
    # Absent cache components are unknown, never zero, so a calculated
    # total requires both cache components to be reported.
    calculated_total = (
        input_value + output_value + cached_value + cache_write_value
        if cached_value is not None and cache_write_value is not None
        else None
    )
    # D209 usage truth: a reported total can never be below the components
    # the source does report — an absent cache component is unknown and
    # only ever adds tokens. A smaller total silently discards known
    # cache-write evidence (Astra final re-review finding 1), so it is
    # rejected rather than kept and billed without its cache component.
    known_components = input_value + output_value
    if cached_value is not None:
        known_components += cached_value
    if cache_write_value is not None:
        known_components += cache_write_value
    if total_value is not None:
        if calculated_total is not None and total_value != calculated_total:
            raise LLMRouterError(
                "Claude result usage total contradicts its components",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
        if total_value < known_components:
            raise LLMRouterError(
                f"Claude result usage total {total_value} is below its known "
                f"components ({known_components}); the absent cache component "
                "is unknown and cannot reduce the total",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
    if event_total is not None:
        authoritative_total = (
            total_value if total_value is not None else calculated_total
        )
        if authoritative_total is not None and event_total != authoritative_total:
            raise LLMRouterError(
                "Claude result event total contradicts its components",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
        if authoritative_total is None and event_total < known_components:
            raise LLMRouterError(
                f"Claude result event total {event_total} is below its known "
                f"components ({known_components}); the absent cache component "
                "is unknown and cannot reduce the total",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
    total_tokens = (
        total_value
        if total_value is not None
        else (calculated_total if calculated_total is not None else event_total)
    )
    return {
        "basis": "provider_reported",
        "source": CLAUDE_USAGE_SOURCE,
        "input_tokens": input_value,
        "output_tokens": output_value,
        "cached_input_tokens": cached_value,
        "reasoning_tokens": None,
        "total_tokens": total_tokens,
    }


def capture_claude_usage(output: str) -> dict[str, Any]:
    """Parse authoritative Claude Code usage from captured stream output.

    Implements the P1-4c usage parser against the governed argv
    ``claude -p --output-format stream-json`` (source string
    :data:`CLAUDE_USAGE_SOURCE`). Only the last terminal event with
    ``type: result`` is read — it is the session summary, and its
    counters are never added to anything else. When the result event
    carries ``modelUsage``, the per-model rows are the sole aggregate
    token source: each row is summed exactly once and the top-level
    result ``usage`` object is not added on top; a present-but-empty
    ``modelUsage`` is therefore unusable evidence and fails closed as
    ``unavailable`` rather than falling through. Only when the
    ``modelUsage`` key is absent entirely does a top-level ``usage``
    object with snake/camel aliases become the fallback. Cache reads
    map to ``cached_input_tokens``; cache creation contributes to
    ``total_tokens`` (omitted from schema fields). ``total_tokens`` is
    the observed-component sum (input + output + cache reads + cache
    creation). When an optional cache component is absent in any
    contributing row, it is not assumed to be zero:
    ``cached_input_tokens`` stays null, and ``total_tokens`` stays null
    unless authoritatively reported. Contradictory terminal evidence
    fails closed; missing, malformed, or unrelated output fails closed
    as ``unavailable`` with a specific reason. No token figure is ever
    estimated from text, context length, cost, or elapsed time.

    Args:
        output: Captured stdout of a governed ``claude -p`` run — one
            JSON object or JSON lines.

    Returns:
        A usage mapping that is directly schema-valid against the
        attempt-record v2 ``$defs/usage`` subschema (P1-5): ``basis``,
        ``source`` or ``unavailable_reason``, and the token counters
        ``input_tokens``, ``output_tokens``, ``cached_input_tokens``,
        ``reasoning_tokens``, ``total_tokens`` — nothing else. With
        valid terminal evidence, ``basis`` is ``provider_reported``
        with the exact source :data:`CLAUDE_USAGE_SOURCE`;
        ``reasoning_tokens`` stays ``null`` (Claude result events do
        not report a separate reasoning figure), and
        ``cached_input_tokens`` stays ``null`` when no model row or the
        top-level usage reports one. A genuine source-reported zero
        stays zero and is never confused with unknown. When usage is
        missing or required counters are absent, ``basis`` is
        ``unavailable`` with a specific ``unavailable_reason`` and
        ``None`` counters — never default zeros.

    Raises:
        LLMRouterError: With ``FailureType.CONTRACT_VIOLATION`` when the
            result event's ``modelUsage`` or ``usage`` field is not an
            object, a ``modelUsage`` row is not an object, a present
            token counter is not a nonnegative integer (bool, negative,
            or fractional), total contradicts components, a reported
            total is below the components the source does report, the
            event total contradicts the aggregate, or alias keys for
            one counter family conflict.
    """
    events, unusable_reason = _parse_claude_events(output)
    result_event = next(
        (
            event
            for event in reversed(events)
            if event.get("type") == _CLAUDE_RESULT_EVENT_TYPE
        ),
        None,
    )
    if result_event is None:
        if unusable_reason is not None:
            return _claude_unavailable_usage(unusable_reason)
        if not events:
            return _claude_unavailable_usage(
                "Claude stream-json output contained no parseable JSON events"
            )
        return _claude_unavailable_usage(
            "Claude stream-json output contained no terminal result event"
        )

    # The result event's own total is extracted and range-validated up
    # front so each aggregate builder can reconcile it against the cache
    # evidence its rows report; contradiction checks belong to the
    # builders because only they know the summed cache components.
    event_total = _consistent_claude_usage_value((result_event,), _CLAUDE_TOTAL_KEYS)
    if event_total is not None and not _nonnegative_int(event_total):
        raise LLMRouterError(
            "Claude result event field 'totalTokens' must be a nonnegative integer",
            failure_type=FailureType.CONTRACT_VIOLATION,
        )

    model_usage = result_event.get("modelUsage")
    if model_usage is not None and not isinstance(model_usage, dict):
        raise LLMRouterError(
            "Claude result modelUsage field must be an object",
            failure_type=FailureType.CONTRACT_VIOLATION,
        )
    if isinstance(model_usage, dict):
        # modelUsage is the sole aggregate token source when present: an
        # empty mapping is unusable evidence, not a signal to fall
        # through to the top-level usage object, and the summed rows are
        # never added on top of it.
        if not model_usage:
            return _claude_unavailable_usage(
                "Claude result event modelUsage was present but empty"
            )
        usage_result = _usage_from_claude_model_usage(
            model_usage, event_total=event_total
        )
    else:
        raw_usage = result_event.get("usage")
        if raw_usage is not None and not isinstance(raw_usage, dict):
            raise LLMRouterError(
                "Claude result usage field must be an object",
                failure_type=FailureType.CONTRACT_VIOLATION,
            )
        if isinstance(raw_usage, dict):
            usage_result = _usage_from_claude_result_usage(
                raw_usage, event_total=event_total
            )
        else:
            return _claude_unavailable_usage(
                "Claude result event carried no modelUsage or usage token evidence"
            )

    return usage_result


def claude_aggregate_models(output: str) -> tuple[str, ...] | None:
    """Return the distinct ``modelUsage`` model ids of the terminal result event.

    Billing-relevance evidence for governed ``run`` cost (Astra final review
    finding 1): the selected dated price terms price exactly the selected
    route's model, so a usage aggregate that spans multiple models cannot
    be priced at that single rate. The stream is read with the same lenient
    parser as :func:`capture_claude_usage`; ``None`` is returned when the
    stream has no parsable terminal result event or the event carries no
    non-empty ``modelUsage`` mapping. This helper never raises: the usage
    parser owns every contradiction and malformed-shape failure, and a
    missing model fact must not mask the captured usage itself.

    Args:
        output: Captured stdout of a governed ``claude -p`` run — one
            JSON object or JSON lines.

    Returns:
        The sorted distinct ``modelUsage`` model ids, or ``None`` when no
        model evidence exists in the terminal result event.
    """
    events, _ = _parse_claude_events(output)
    result_event = next(
        (
            event
            for event in reversed(events)
            if event.get("type") == _CLAUDE_RESULT_EVENT_TYPE
        ),
        None,
    )
    if result_event is None:
        return None
    model_usage = result_event.get("modelUsage")
    if not isinstance(model_usage, dict) or not model_usage:
        return None
    return tuple(sorted(str(model_id) for model_id in model_usage))


def _snippet(text: str, limit: int = 200) -> str:
    normalized = " ".join(text.split())
    return normalized[:limit]
