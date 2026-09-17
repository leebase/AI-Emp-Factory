#!/usr/bin/env python3
"""Stage worker wrapping OpenCode CLI for auto-orch routing.

Invoked by auto-orch routing as:

    opencode_stage_worker.py <stage> <prompt_path> <response_path>

Reads the stage prompt, runs ``opencode run`` headlessly, extracts the first
JSON object from streamed text events, and writes it verbatim to the response
path. Schema validation stays owned by the B3 routing adapter.

Environment knobs:
    OPENCODE_STAGE_WORKER_BINARY  opencode executable (default: "opencode")
    OPENCODE_STAGE_WORKER_MODEL   forwarded as --model
                                  (default: "opencode/deepseek-v4-flash-free")
    OPENCODE_STAGE_WORKER_AGENT   forwarded as --agent (default: "summary")
    OPENCODE_STAGE_WORKER_VARIANT forwarded as --variant. When unset, author
                                  uses "minimal" to preserve output budget.
    OPENCODE_STAGE_WORKER_DIR     forwarded as --dir (default: current cwd)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

VALID_STAGES = frozenset({"author", "envision", "ideate", "reconsider", "score"})

SYSTEM_PROMPT = (
    "You are a headless auto-orch stage worker. Output only the JSON response "
    "the prompt asks for - no prose, no code fences. Do not use tools."
)
PREFLIGHT_SYSTEM_PROMPT = (
    "You are a bounded OpenCode readiness probe. Reply with exactly READY and "
    "nothing else. Do not read or write files and do not use tools."
)
PREFLIGHT_PROMPT = "Reply with exactly READY and nothing else."


def _fail(message: str) -> int:
    print(f"opencode-stage-worker: {message}", file=sys.stderr)
    return 1


def _build_argv(
    stage: str,
    prompt_text: str,
    *,
    system_prompt: str = SYSTEM_PROMPT,
) -> list[str]:
    binary = os.environ.get("OPENCODE_STAGE_WORKER_BINARY", "opencode")
    model = os.environ.get(
        "OPENCODE_STAGE_WORKER_MODEL", "opencode/deepseek-v4-flash-free"
    )
    agent = os.environ.get("OPENCODE_STAGE_WORKER_AGENT", "summary")
    variant = os.environ.get("OPENCODE_STAGE_WORKER_VARIANT")
    if variant is None and stage == "author":
        variant = "minimal"
    workdir = os.environ.get("OPENCODE_STAGE_WORKER_DIR", str(Path.cwd()))
    argv = [
        binary,
        "run",
        "--agent",
        agent,
        "--model",
        model,
        "--format",
        "json",
        "--dir",
        workdir,
    ]
    if variant:
        argv.extend(["--variant", variant])
    argv.append(f"{system_prompt}\n\n{prompt_text}")
    return argv


def _event_text(stdout_text: str) -> str:
    chunks: list[str] = []
    for line in stdout_text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            chunks.append(line)
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "text":
            part = event.get("part")
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    return "\n".join(chunks)


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


def _extract_author_json_like_object(text: str) -> str | None:
    """Recover OpenCode author output with raw newlines inside playbook_yaml."""
    match = re.search(
        r'^\s*\{\s*"schema_version"\s*:\s*1\s*,\s*"playbook_yaml"\s*:\s*"'
        r"(?P<yaml>.*)"
        r'"\s*\}\s*$',
        text,
        flags=re.DOTALL,
    )
    if match is None:
        return None
    playbook_yaml = match.group("yaml")
    return json.dumps(
        {"schema_version": 1, "playbook_yaml": playbook_yaml},
        ensure_ascii=False,
        indent=2,
    )


def _fill_reasoning_alias(payload: dict, key: str) -> None:
    value = payload.get(key)
    if not isinstance(value, dict):
        return
    reasoning = value.get("reasoning")
    rationale = value.get("rationale")
    if isinstance(reasoning, str) and reasoning.strip():
        return
    if isinstance(rationale, str) and rationale.strip():
        value["reasoning"] = rationale


def _normalize_stage_payload(stage: str, extracted: str) -> str:
    payload = json.loads(extracted)
    if not isinstance(payload, dict):
        raise ValueError("extracted JSON was not an object")
    if stage == "envision":
        _fill_reasoning_alias(payload, "proposed_selection")
    elif stage == "reconsider":
        _fill_reasoning_alias(payload, "committed_selection")
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _write_failure_sidecars(
    response_path: Path,
    *,
    stdout_text: str,
    stderr_text: str,
    event_text: str | None = None,
) -> None:
    artifacts = {
        response_path.with_suffix(response_path.suffix + ".stdout.jsonl"): stdout_text,
        response_path.with_suffix(response_path.suffix + ".stderr.txt"): stderr_text,
    }
    if event_text is not None:
        artifacts[response_path.with_suffix(response_path.suffix + ".text.txt")] = (
            event_text
        )
    for path, text in artifacts.items():
        try:
            path.write_text(text, encoding="utf-8")
        except OSError:
            continue


def _preflight() -> int:
    """Run a bounded real model round trip for the configured OpenCode route."""
    argv = _build_argv(
        "score",
        PREFLIGHT_PROMPT,
        system_prompt=PREFLIGHT_SYSTEM_PROMPT,
    )
    try:
        timeout = float(
            os.environ.get("OPENCODE_STAGE_WORKER_PREFLIGHT_TIMEOUT_SECONDS", "25")
        )
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=max(1.0, min(timeout, 30.0)),
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return _fail(f"readiness probe could not run ({argv[0]!r}): {exc}")
    if completed.returncode != 0:
        stderr_tail = completed.stderr.strip().splitlines()[-3:]
        return _fail(
            f"readiness probe exited {completed.returncode}: " + " | ".join(stderr_tail)
        )
    response = _event_text(completed.stdout).strip()
    if response != "READY":
        return _fail(
            "readiness probe returned an unexpected response: " f"{response[:120]!r}"
        )
    print("opencode-stage-worker: configured model route is ready")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--preflight"]:
        return _preflight()
    if len(argv) != 3:
        return _fail(
            "usage: opencode_stage_worker.py --preflight | "
            "<stage> <prompt_path> <response_path>"
        )
    stage, prompt_path_raw, response_path_raw = argv
    response_path = Path(response_path_raw)
    if stage not in VALID_STAGES:
        return _fail(
            f"unknown stage {stage!r} (expected one of {sorted(VALID_STAGES)})"
        )

    prompt_path = Path(prompt_path_raw)
    try:
        prompt_text = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        return _fail(f"could not read prompt file {prompt_path}: {exc}")

    opencode_argv = _build_argv(stage, prompt_text)
    try:
        completed = subprocess.run(opencode_argv, capture_output=True, text=True)
    except OSError as exc:
        return _fail(f"could not start opencode ({opencode_argv[0]!r}): {exc}")
    if completed.returncode != 0:
        _write_failure_sidecars(
            response_path,
            stdout_text=completed.stdout,
            stderr_text=completed.stderr,
        )
        stderr_tail = completed.stderr.strip().splitlines()[-3:]
        return _fail(
            f"opencode exited {completed.returncode} for stage {stage}: "
            + " | ".join(stderr_tail)
        )

    source_text = _event_text(completed.stdout)
    extracted = _extract_json_object(source_text)
    if extracted is None and stage == "author":
        extracted = _extract_author_json_like_object(source_text)
    if extracted is None:
        _write_failure_sidecars(
            response_path,
            stdout_text=completed.stdout,
            stderr_text=completed.stderr,
            event_text=source_text,
        )
        return _fail(f"no JSON object found in opencode output for stage {stage}")
    try:
        response_text = _normalize_stage_payload(stage, extracted)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        _write_failure_sidecars(
            response_path,
            stdout_text=completed.stdout,
            stderr_text=completed.stderr,
            event_text=source_text,
        )
        return _fail(f"invalid JSON object found in opencode output for {stage}: {exc}")

    try:
        response_path.write_text(response_text, encoding="utf-8")
    except OSError as exc:
        return _fail(f"could not write response file {response_path}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
