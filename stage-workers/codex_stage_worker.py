#!/usr/bin/env python3
"""Stage worker wrapping Codex CLI for auto-orch routing.

Invoked by auto-orch routing as:

    codex_stage_worker.py <stage> <prompt_path> <response_path>

Reads the stage prompt, runs ``codex exec`` directly, extracts the first JSON
object from the final response, and writes it verbatim to the response path.
Schema validation stays owned by the B3 routing adapter.

Environment knobs:
    CODEX_STAGE_WORKER_BINARY            codex executable (default: "codex")
    CODEX_STAGE_WORKER_MODEL             forwarded as --model (default: "gpt-5.5")
    CODEX_STAGE_WORKER_REASONING_EFFORT  config override (default: "high")
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
    print(f"codex-stage-worker: {message}", file=sys.stderr)
    return 1


def _build_argv(response_path: Path) -> list[str]:
    binary = os.environ.get("CODEX_STAGE_WORKER_BINARY", "codex")
    model = os.environ.get("CODEX_STAGE_WORKER_MODEL", "gpt-5.5")
    reasoning_effort = os.environ.get("CODEX_STAGE_WORKER_REASONING_EFFORT", "high")
    last_message_path = response_path.with_suffix(
        response_path.suffix + ".last-message"
    )
    return [
        binary,
        "exec",
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{reasoning_effort}"',
        "--sandbox",
        "read-only",
        "--cd",
        str(Path.cwd()),
        "--skip-git-repo-check",
        "--output-last-message",
        str(last_message_path),
        "-",
    ]


def _build_preflight_argv() -> list[str]:
    """Build a minimal call through the same binary/model route as real stages."""
    binary = os.environ.get("CODEX_STAGE_WORKER_BINARY", "codex")
    model = os.environ.get("CODEX_STAGE_WORKER_MODEL", "gpt-5.5")
    reasoning_effort = os.environ.get("CODEX_STAGE_WORKER_REASONING_EFFORT", "high")
    return [
        binary,
        "exec",
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{reasoning_effort}"',
        "--sandbox",
        "read-only",
        "--cd",
        str(Path.cwd()),
        "--skip-git-repo-check",
        "-",
    ]


def _preflight() -> int:
    """Prove current auth, entitlement, quota, and model-route readiness."""
    argv = _build_preflight_argv()
    try:
        timeout = float(
            os.environ.get("CODEX_STAGE_WORKER_PREFLIGHT_TIMEOUT_SECONDS", "25")
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
            f"readiness probe exited {completed.returncode}: "
            + " | ".join(stderr_tail)
        )
    print("codex-stage-worker: configured model route is ready")
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
            "usage: codex_stage_worker.py --preflight | "
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

    response_path = Path(response_path_raw)
    codex_argv = _build_argv(response_path)
    full_prompt = f"{SYSTEM_PROMPT}\n\n{prompt_text}"
    try:
        completed = subprocess.run(
            codex_argv,
            input=full_prompt,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return _fail(f"could not start codex ({codex_argv[0]!r}): {exc}")
    if completed.returncode != 0:
        stderr_tail = completed.stderr.strip().splitlines()[-3:]
        return _fail(
            f"codex exited {completed.returncode} for stage {stage}: "
            + " | ".join(stderr_tail)
        )

    last_message_path = response_path.with_suffix(
        response_path.suffix + ".last-message"
    )
    try:
        source_text = last_message_path.read_text(encoding="utf-8")
    except OSError:
        source_text = completed.stdout

    extracted = _extract_json_object(source_text)
    if extracted is None:
        return _fail(f"no JSON object found in codex output for stage {stage}")

    try:
        response_path.write_text(extracted, encoding="utf-8")
    except OSError as exc:
        return _fail(f"could not write response file {response_path}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
