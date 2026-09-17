"""Harness shims generation and management.

Provides templated command shims for Claude Code, Codex, OMP, OpenCode, and Pi.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Sequence

ENV_SHIM_HOME = "LEE_LLM_ROUTER_SHIM_HOME"

HARNESS_TAGS: tuple[str, ...] = ("claude-code", "codex", "omp", "opencode", "pi")

HARNESS_FORMS: dict[str, str] = {
    "claude-code": "Claude Code slash command",
    "codex": "Codex custom prompt",
    "omp": "OMP prompt template",
    "opencode": "OpenCode command file",
    "pi": "Pi prompt template",
}

HARNESS_FRONTMATTER_EXTRA: dict[str, str] = {
    "claude-code": (
        "argument-hint: auto|NAME\n" "allowed-tools: Bash(lee-llm-router:*)\n"
    ),
    "codex": "argument-hint: auto|NAME\n",
    "omp": "argument-hint: auto|NAME\n",
    "opencode": "",
    "pi": "argument-hint: auto|NAME\n",
}

#: Managed shim commands (Phase 3 P3-5, D213 ruling 1): ``crew`` is the
#: existing resolve-only staffing command; ``supervise`` is the second
#: managed command. Each has its own template, target filename, and marker
#: hash, so installed markers and paths are fully independent.
CREW_COMMAND = "crew"
SUPERVISE_COMMAND = "supervise"

COMMAND_NAMES: tuple[str, ...] = (CREW_COMMAND, SUPERVISE_COMMAND)

COMMAND_TEMPLATE_NAMES: dict[str, str] = {
    CREW_COMMAND: "crew.md.tmpl",
    SUPERVISE_COMMAND: "supervise.md.tmpl",
}

COMMAND_TARGET_FILENAMES: dict[str, str] = {
    CREW_COMMAND: "crew.md",
    SUPERVISE_COMMAND: "supervise.md",
}

COMMAND_FRONTMATTER_EXTRA: dict[str, dict[str, str]] = {
    CREW_COMMAND: HARNESS_FRONTMATTER_EXTRA,
    SUPERVISE_COMMAND: {
        "claude-code": (
            "argument-hint: <plan-path> [crew NAME | auto]\n"
            "allowed-tools: Bash(lee-llm-router:*)\n"
        ),
        "codex": (
            "argument-hint: <plan-path> [crew NAME | auto]\n"
            "# Codex invocation: /prompts:supervise <args> (custom prompts namespace)\n"
        ),
        "omp": "argument-hint: <plan-path> [crew NAME | auto]\n",
        "opencode": "",
        "pi": "argument-hint: <plan-path> [crew NAME | auto]\n",
    },
}

MARKER_RE = re.compile(
    r"^<!-- lee-llm-router shim v1 sha256=([0-9a-fA-F]{64}) -->(\r?\n)?",
    re.MULTILINE,
)


class ShimError(Exception):
    """Base exception for shims operations."""


class ShimUsageError(ShimError):
    """Usage error for shims operations (maps to exit 3)."""


@dataclass(frozen=True)
class ShimTarget:
    """Rendered target information for a harness shim."""

    harness: str
    path: Path
    form: str
    content: str
    body: str
    frontmatter: str
    marker_line: str
    marker_hash: str
    body_below_marker: str


Target = ShimTarget


def resolve_home(home: Path | str | None = None) -> Path:
    """Resolve the target home directory."""
    if home is not None:
        return Path(home).resolve()
    env_home = os.environ.get(ENV_SHIM_HOME)
    if env_home:
        return Path(env_home).resolve()
    return Path.home().resolve()


def resolve_project(project: Path | str | None = None) -> Path:
    """Resolve the target project directory (for omp)."""
    if project is not None:
        return Path(project).resolve()
    return Path.cwd().resolve()


def normalize_harnesses(harnesses: Sequence[str] | str | None) -> list[str]:
    """Normalize and validate harness tags."""
    if harnesses is None:
        return list(HARNESS_TAGS)
    if isinstance(harnesses, str):
        harnesses = [harnesses]
    result: list[str] = []
    for item in harnesses:
        for tag in item.split(","):
            tag = tag.strip()
            if not tag:
                continue
            if tag not in HARNESS_TAGS:
                raise ShimUsageError(
                    f"unknown harness {tag!r} (known: {', '.join(HARNESS_TAGS)})"
                )
            if tag not in result:
                result.append(tag)
    return result


def resolve_command(command: str | None) -> str:
    """Validate and default the managed command name.

    ``None`` means the original ``crew`` command, so existing callers keep
    their behavior.
    """
    if command is None:
        return CREW_COMMAND
    if command not in COMMAND_NAMES:
        raise ShimUsageError(
            f"unknown command {command!r} (known: {', '.join(COMMAND_NAMES)})"
        )
    return command


def target_path_for_harness(
    harness: str,
    project: Path | str | None = None,
    home: Path | str | None = None,
    command: str | None = CREW_COMMAND,
) -> Path:
    """Return the absolute installation target path for a harness/command."""
    h = resolve_home(home)
    p = resolve_project(project)
    cmd = resolve_command(command)
    filename = COMMAND_TARGET_FILENAMES[cmd]
    if harness == "claude-code":
        return h / ".claude" / "commands" / filename
    elif harness == "codex":
        return h / ".codex" / "prompts" / filename
    elif harness == "omp":
        return p / ".omp" / "prompts" / filename
    elif harness == "opencode":
        return h / ".config" / "opencode" / "command" / filename
    elif harness == "pi":
        return h / ".pi" / "agent" / "prompts" / filename
    else:
        raise ShimUsageError(
            f"unknown harness {harness!r} (known: {', '.join(HARNESS_TAGS)})"
        )


def load_template(command: str | None = CREW_COMMAND) -> str:
    """Load the raw template text for a managed command from package data."""
    cmd = resolve_command(command)
    template_name = COMMAND_TEMPLATE_NAMES[cmd]
    try:
        import importlib.resources as pkg_resources

        return (
            pkg_resources.files("lee_llm_router")
            .joinpath(f"templates/shims/{template_name}")
            .read_text(encoding="utf-8")
        )
    except Exception:
        tmpl_path = Path(__file__).parent / "templates" / "shims" / template_name
        return tmpl_path.read_text(encoding="utf-8")


def render_target(
    harness: str,
    project: Path | str | None = None,
    home: Path | str | None = None,
    command: str | None = CREW_COMMAND,
) -> ShimTarget:
    """Render the full target object for a given harness and command."""
    if harness not in HARNESS_TAGS:
        raise ShimUsageError(
            f"unknown harness {harness!r} (known: {', '.join(HARNESS_TAGS)})"
        )
    cmd = resolve_command(command)

    raw_tmpl = load_template(cmd)
    marker_placeholder = "<!-- lee-llm-router shim v1 sha256={marker_hash} -->"
    idx = raw_tmpl.find(marker_placeholder)
    if idx == -1:
        raise ShimError(
            f"Marker line placeholder not found in template: {marker_placeholder}"
        )

    prefix = raw_tmpl[:idx]
    rest = raw_tmpl[idx + len(marker_placeholder) :]
    if rest.startswith("\r\n"):
        body_tmpl = rest[2:]
    elif rest.startswith("\n"):
        body_tmpl = rest[1:]
    else:
        body_tmpl = rest

    body_below = body_tmpl.replace("{harness}", harness)
    marker_hash = hashlib.sha256(body_below.encode("utf-8")).hexdigest()
    marker_line = f"<!-- lee-llm-router shim v1 sha256={marker_hash} -->"
    extra = COMMAND_FRONTMATTER_EXTRA[cmd].get(harness, "")
    frontmatter = prefix.replace("{frontmatter_extra}", extra)

    content = f"{frontmatter}{marker_line}\n{body_below}"
    body = f"{marker_line}\n{body_below}"
    path = target_path_for_harness(harness, project=project, home=home, command=cmd)
    form = HARNESS_FORMS[harness]

    return ShimTarget(
        harness=harness,
        path=path,
        form=form,
        content=content,
        body=body,
        frontmatter=frontmatter,
        marker_line=marker_line,
        marker_hash=marker_hash,
        body_below_marker=body_below,
    )


def render_shim(harness: str, command: str | None = CREW_COMMAND) -> str:
    """Render the full shim file content for a harness."""
    return render_target(harness, command=command).content


def render_body(harness: str, command: str | None = CREW_COMMAND) -> str:
    """Render the shim body (marker line + body below) for a harness."""
    return render_target(harness, command=command).body


def render_body_below(harness: str, command: str | None = CREW_COMMAND) -> str:
    """Render the shim body below the marker line for a harness."""
    return render_target(harness, command=command).body_below_marker


render = render_shim


def get_targets(
    project: Path | str | None = None,
    home: Path | str | None = None,
    harnesses: Sequence[str] | str | None = None,
    command: str | None = CREW_COMMAND,
) -> list[ShimTarget]:
    """Return rendered targets for the selected harnesses and command."""
    tag_list = normalize_harnesses(harnesses)
    cmd = resolve_command(command)
    return [render_target(h, project=project, home=home, command=cmd) for h in tag_list]


get_shims = get_targets


def inspect_target(target: ShimTarget) -> str:
    """Inspect an existing target path and return the action to take.

    Returns one of:
        - 'create'
        - 'update'
        - 'unchanged'
        - 'refuse: not generated by lee-llm-router'
    """
    if not target.path.exists():
        return "create"
    try:
        existing = target.path.read_text(encoding="utf-8")
    except OSError:
        return "refuse: not generated by lee-llm-router"

    if existing == target.content:
        return "unchanged"

    m = MARKER_RE.search(existing)
    if not m:
        return "refuse: not generated by lee-llm-router"

    recorded_hash = m.group(1).lower()
    body_below = existing[m.end() :]
    actual_hash = hashlib.sha256(body_below.encode("utf-8")).hexdigest()
    if actual_hash != recorded_hash:
        return "refuse: not generated by lee-llm-router"

    return "update"


def install_shims(
    dry_run: bool = False,
    apply: bool = False,
    force: bool = False,
    harnesses: Sequence[str] | str | None = None,
    project: Path | str | None = None,
    home: Path | str | None = None,
    command: str | None = CREW_COMMAND,
) -> int:
    """Install shims for the selected harnesses and managed command.

    Returns 0 on success, 1 on refusal, 3 on usage error.
    """
    if (dry_run and apply) or (not dry_run and not apply):
        raise ShimUsageError("exactly one of --dry-run or --apply is required")

    targets = get_targets(
        project=project, home=home, harnesses=harnesses, command=command
    )

    if dry_run:
        for target in targets:
            action = inspect_target(target)
            print(f"target: {target.path}")
            print(f"action: {action}")
            print(target.content, end="" if target.content.endswith("\n") else "\n")
        return 0

    any_refused = False
    for target in targets:
        action = inspect_target(target)
        if action == "unchanged":
            print(f"unchanged: {target.path}")
        elif action == "create":
            target.path.parent.mkdir(parents=True, exist_ok=True)
            target.path.write_text(target.content, encoding="utf-8")
            print(f"create: {target.path}")
        elif action == "update":
            target.path.parent.mkdir(parents=True, exist_ok=True)
            target.path.write_text(target.content, encoding="utf-8")
            print(f"update: {target.path}")
        elif action == "refuse: not generated by lee-llm-router":
            if force:
                target.path.parent.mkdir(parents=True, exist_ok=True)
                target.path.write_text(target.content, encoding="utf-8")
                print(f"update: {target.path}")
            else:
                print(
                    f"refuse: not generated by lee-llm-router: {target.path}",
                    file=sys.stderr,
                )
                any_refused = True

    return 1 if any_refused else 0


def diff_shims(
    harnesses: Sequence[str] | str | None = None,
    project: Path | str | None = None,
    home: Path | str | None = None,
    command: str | None = CREW_COMMAND,
) -> int:
    """Show unified diff between installed shims and rendered templates.

    Returns 0 if no drift and none missing, 1 if drift or missing.
    """
    targets = get_targets(
        project=project, home=home, harnesses=harnesses, command=command
    )
    has_drift = False
    for target in targets:
        if not target.path.exists():
            print(f"missing: {target.path}")
            has_drift = True
            continue
        existing = target.path.read_text(encoding="utf-8")
        if existing == target.content:
            continue
        has_drift = True
        diff = difflib.unified_diff(
            existing.splitlines(keepends=True),
            target.content.splitlines(keepends=True),
            fromfile=str(target.path),
            tofile=f"{target.path} (rendered)",
        )
        sys.stdout.writelines(diff)
    return 1 if has_drift else 0
