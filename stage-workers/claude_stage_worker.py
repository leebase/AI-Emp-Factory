#!/usr/bin/env python3
"""Stage worker wrapping Claude Code for auto-orch routing.

Invoked by auto-orch routing as:

    claude_stage_worker.py <stage> <prompt_path> <response_path>
    claude_stage_worker.py --preflight

Reads the stage prompt, runs Claude Code headless, extracts the first JSON
object from stdout, and writes it verbatim to the response path. Schema
validation stays owned by the B3 routing adapter. ``--preflight`` proves
auth/entitlement/model-route readiness through the same binary/model/effort
as a real stage call, without requiring a prompt file.

Environment knobs:
    CLAUDE_STAGE_WORKER_BINARY  claude executable (default: "claude")
    CLAUDE_STAGE_WORKER_MODEL   forwarded as --model (default: "opus")
    CLAUDE_STAGE_WORKER_EFFORT  forwarded as --effort (default: "high")
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

VALID_STAGES = frozenset({"author", "envision", "ideate", "reconsider", "score"})

SYSTEM_PROMPT = (
    "You are a headless auto-orch stage worker. Output only the JSON response "
    "the prompt asks for - no prose, no code fences."
)
PREFLIGHT_PROMPT = (
    "This is a bounded readiness probe. Reply with exactly READY. "
    "Do not inspect files or use tools."
)


def _fail(message: str) -> int:
    print(f"claude-stage-worker: {message}", file=sys.stderr)
    return 1


def _build_argv() -> list[str]:
    binary = os.environ.get("CLAUDE_STAGE_WORKER_BINARY", "claude")
    model = os.environ.get("CLAUDE_STAGE_WORKER_MODEL", "opus")
    effort = os.environ.get("CLAUDE_STAGE_WORKER_EFFORT", "high")
    return [
        binary,
        "--print",
        "--model",
        model,
        "--effort",
        effort,
        "--output-format",
        "text",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--tools",
        "",
        "--system-prompt",
        SYSTEM_PROMPT,
    ]


def _preflight() -> int:
    """Prove current auth, entitlement, and model-route readiness."""
    argv = _build_argv()
    try:
        timeout = float(
            os.environ.get("CLAUDE_STAGE_WORKER_PREFLIGHT_TIMEOUT_SECONDS", "25")
        )
        completed = subprocess.run(
            argv,
            input=PREFLIGHT_PROMPT,
            capture_output=True,
            text=True,
            timeout=max(1.0, min(timeout, 25.0)),
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return _fail(f"readiness probe could not run ({argv[0]!r}): {exc}")
    if completed.returncode != 0:
        stderr_tail = completed.stderr.strip().splitlines()[-3:]
        return _fail(
            f"readiness probe exited {completed.returncode}: " + " | ".join(stderr_tail)
        )
    print("claude-stage-worker: configured model route is ready")
    return 0


def _extract_json_object(text: str) -> str | None:
    decoder = json.JSONDecoder()
    index = text.find("{")
    while index != -1:
        candidate = text[index:]
        try:
            value, end = decoder.raw_decode(candidate)
        except ValueError:
            pass
        else:
            if isinstance(value, dict):
                return candidate[:end]
        index = text.find("{", index + 1)
    return None


def main(argv: list[str]) -> int:
    if argv == ["--preflight"]:
        return _preflight()
    if len(argv) != 3:
        return _fail(
            "usage: claude_stage_worker.py --preflight | "
            "<stage> <prompt_path> <response_path>"
        )
    stage, prompt_path_raw, response_path_raw = argv
    if stage not in VALID_STAGES:
        return _fail(
            f"unknown stage {stage!r} (expected one of {sorted(VALID_STAGES)})"
        )

    prompt_path = Path(prompt_path_raw)
    try:
        prompt_text = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        return _fail(f"could not read prompt file {prompt_path}: {exc}")

    claude_argv = _build_argv()
    try:
        completed = subprocess.run(
            claude_argv,
            input=prompt_text,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return _fail(f"could not start claude ({claude_argv[0]!r}): {exc}")
    if completed.returncode != 0:
        stderr_tail = completed.stderr.strip().splitlines()[-3:]
        return _fail(
            f"claude exited {completed.returncode} for stage {stage}: "
            + " | ".join(stderr_tail)
        )

    extracted = _extract_json_object(completed.stdout)
    if extracted is None:
        return _fail(f"no JSON object found in claude output for stage {stage}")

    response_path = Path(response_path_raw)
    try:
        response_path.write_text(extracted, encoding="utf-8")
    except OSError as exc:
        return _fail(f"could not write response file {response_path}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
