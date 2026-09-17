#!/usr/bin/env python3
"""B3 stage worker wrapping the pi CLI (Sprint 36 / C1).

Invoked by auto-orch routing as:

    pi_stage_worker.py <stage> <prompt_path> <response_path>

A no-call readiness mode is also available:

    pi_stage_worker.py --preflight

Reads the stage prompt, runs pi headless with the proven pi_cli adapter
flag shape (read-only tools — stage workers produce JSON text, they do not
edit files), extracts the JSON object from pi's stdout, and writes it
verbatim to the response path. Schema validation stays owned by the B3
adapter; this wrapper never repairs or fabricates content.

``--preflight`` proves readiness without any provider/model request: the
Pi binary is resolvable and executable, PI_STAGE_WORKER_PROVIDER and
PI_STAGE_WORKER_MODEL are set, optional PI_STAGE_WORKER_THINKING (when
present) is a real pi thinking level, the Pi provider auth file exists,
is readable, and holds an entry for the configured provider (contents are
never printed), and the configured provider/model exists in Pi's local
model store (``models-store.json``, with ``models.json`` custom providers
as a fallback). Exit 0 prints one readiness line; every failure exits
nonzero with one stderr line and invokes nothing.

Fail-closed: any problem (unknown stage, unreadable prompt, pi launch
failure, pi nonzero exit, no JSON object in output) exits nonzero without
writing a response file, which is exactly what the B3 adapter fails the
stage on.

Environment knobs (all optional):
    PI_STAGE_WORKER_BINARY    pi executable (default: "pi" from PATH)
    PI_STAGE_WORKER_PROVIDER  forwarded as --provider (required by --preflight)
    PI_STAGE_WORKER_MODEL     forwarded as --model (required by --preflight)
    PI_STAGE_WORKER_THINKING  forwarded as --thinking (validated by --preflight)
    PI_STAGE_WORKER_AUTH_FILE direct override of the auth.json path
    PI_STAGE_WORKER_MODEL_STORE
                              direct override of the models-store.json path

The auth and model-store files default to Pi's own agent directory:
``PI_CODING_AGENT_DIR`` when set, else ``~/.pi/agent``.

Contract: docs/sprint-36-real-mission-pilot-contract.md §1.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

VALID_STAGES = frozenset({"envision", "ideate", "reconsider", "score", "author"})

# The exact thinking levels pi's ``--thinking`` accepts (usage docs).
# Values are validated as-is; the wrapper never invents or maps them.
VALID_THINKING_LEVELS = frozenset(
    {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
)

# The Sprint 24 judge tool set: read-only by doctrine.
READ_ONLY_TOOLS = "read,grep,find,ls"

SYSTEM_PROMPT = (
    "You are a headless stage worker. Output only the JSON response the "
    "prompt asks for - no prose, no code fences."
)


def _fail(message: str) -> int:
    print(f"pi-stage-worker: {message}", file=sys.stderr)
    return 1


def _configured_binary() -> str:
    return os.environ.get("PI_STAGE_WORKER_BINARY", "").strip() or "pi"


def _resolve_binary() -> str | None:
    """Return the configured pi binary if resolvable and executable, else None."""
    configured = _configured_binary()
    if "/" in configured or "\\" in configured:
        path = Path(configured)
        if path.is_file() and os.access(path, os.X_OK):
            return configured
        return None
    return shutil.which(configured)


def _agent_dir() -> Path:
    override = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".pi" / "agent"


def _auth_path() -> Path:
    override = os.environ.get("PI_STAGE_WORKER_AUTH_FILE", "").strip()
    if override:
        return Path(override)
    return _agent_dir() / "auth.json"


def _model_store_path() -> Path:
    override = os.environ.get("PI_STAGE_WORKER_MODEL_STORE", "").strip()
    if override:
        return Path(override)
    return _agent_dir() / "models-store.json"


def _store_has_model(data: object, provider: str, model: str) -> bool:
    """True when ``data`` declares exactly ``provider``/``model`` locally."""
    if not isinstance(data, dict):
        return False
    entry = data.get(provider)
    if isinstance(entry, dict) and isinstance(entry.get("models"), list):
        return any(
            isinstance(m, dict) and m.get("id") == model for m in entry["models"]
        )
    return False


def _build_argv(prompt_text: str) -> list[str]:
    binary = os.environ.get("PI_STAGE_WORKER_BINARY", "pi")
    argv = [
        binary,
        "--print",
        "--mode",
        "text",
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--tools",
        READ_ONLY_TOOLS,
        "--system-prompt",
        SYSTEM_PROMPT,
    ]
    provider = os.environ.get("PI_STAGE_WORKER_PROVIDER")
    if provider:
        argv.extend(["--provider", provider])
    model = os.environ.get("PI_STAGE_WORKER_MODEL")
    if model:
        argv.extend(["--model", model])
    thinking = os.environ.get("PI_STAGE_WORKER_THINKING")
    if thinking:
        argv.extend(["--thinking", thinking])
    argv.append(prompt_text)
    return argv


def _extract_json_object(text: str) -> str | None:
    """Return the first JSON object in ``text`` verbatim, or None.

    Handles raw JSON, prose-wrapped JSON, and fenced JSON with one
    mechanism: try raw_decode at every ``{`` until an object parses.
    """
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


def _preflight() -> int:
    """Prove local readiness with no provider/model request of any kind."""
    binary = _resolve_binary()
    if binary is None:
        return _fail(f"pi binary {_configured_binary()!r} is not resolvable/executable")

    provider = os.environ.get("PI_STAGE_WORKER_PROVIDER", "").strip()
    if not provider:
        return _fail("PI_STAGE_WORKER_PROVIDER is required for --preflight")
    model = os.environ.get("PI_STAGE_WORKER_MODEL", "").strip()
    if not model:
        return _fail("PI_STAGE_WORKER_MODEL is required for --preflight")
    thinking = os.environ.get("PI_STAGE_WORKER_THINKING", "").strip()
    if thinking and thinking not in VALID_THINKING_LEVELS:
        return _fail(
            f"PI_STAGE_WORKER_THINKING {thinking!r} is invalid; expected one "
            f"of {sorted(VALID_THINKING_LEVELS)}"
        )

    auth_path = _auth_path()
    try:
        auth_text = auth_path.read_text(encoding="utf-8")
    except OSError as exc:
        return _fail(f"pi auth file is not readable ({auth_path}): {exc}")
    try:
        auth = json.loads(auth_text)
    except ValueError:
        return _fail(f"pi auth file is not valid JSON ({auth_path})")
    if not (isinstance(auth, dict) and auth.get(provider)):
        return _fail(
            f"pi auth file has no credentials entry for provider "
            f"{provider!r} ({auth_path})"
        )

    store_path = _model_store_path()
    model_found = False
    try:
        store = json.loads(store_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        store = None
    if _store_has_model(store, provider, model):
        model_found = True
    else:
        custom_path = _agent_dir() / "models.json"
        try:
            custom = json.loads(custom_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            custom = None
        if isinstance(custom, dict) and isinstance(custom.get("providers"), dict):
            model_found = _store_has_model(custom["providers"], provider, model)
    if not model_found:
        return _fail(
            f"model {model!r} for provider {provider!r} not found in pi "
            f"model store ({store_path})"
        )

    suffix = f" thinking={thinking}" if thinking else ""
    print(f"pi-stage-worker: ready provider={provider} model={model}{suffix}")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--preflight"]:
        return _preflight()
    if len(argv) != 3:
        return _fail("usage: pi_stage_worker.py <stage> <prompt_path> <response_path>")
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

    pi_argv = _build_argv(prompt_text)
    try:
        completed = subprocess.run(pi_argv, capture_output=True, text=True)
    except OSError as exc:
        return _fail(f"could not start pi ({pi_argv[0]!r}): {exc}")
    if completed.returncode != 0:
        stderr_tail = completed.stderr.strip().splitlines()[-3:]
        return _fail(
            f"pi exited {completed.returncode} for stage {stage}: "
            + " | ".join(stderr_tail)
        )

    extracted = _extract_json_object(completed.stdout)
    if extracted is None:
        return _fail(f"no JSON object found in pi output for stage {stage}")

    response_path = Path(response_path_raw)
    try:
        response_path.write_text(extracted, encoding="utf-8")
    except OSError as exc:
        return _fail(f"could not write response file {response_path}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
