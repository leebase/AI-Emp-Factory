"""Doctor CLI - config validation, environment diagnostics, and source export.

Commands:
    lee-llm-router doctor --config <path> [--role <role>] [--crews]
                          [--availability] [--catalog [--catalog-dir <dir>]]
    lee-llm-router crews list [--crews-file <path>] [--json]
    lee-llm-router crews page --out <path> [--crews-file <path>]
                          [--availability-file <path>] [--benchmark-file <path>]
                          [--events-file <path>]
    lee-llm-router staff --role ROLE --class CLASS [--mode auto|crew NAME|bind ROUTE]
    lee-llm-router shims install (--dry-run | --apply) [--command crew|supervise]
                                 [--force] [--harness <tag>] [--project <path>]
    lee-llm-router shims diff [--command crew|supervise] [--harness <tag>]
                              [--project <path>]
    lee-llm-router catalog explain --role ROLE --class CLASS
                                  [--at DATE] [--availability-file PATH]
                                  [--catalog-dir PATH]
                                  [--author-route ROUTE_ID] [--json]
    lee-llm-router run --role ROLE --class CLASS --packet FILE
                       --owned-paths PATH [--owned-paths PATH ...]
                       [--route ROUTE_ID] [--supervisor-route ROUTE_ID]
                       [--author-route ROUTE_ID]
                       [--oracle CMD] [--workdir DIR]
                       [--parent ATTEMPT_ID --escalation-reason R]
                       [--timeout S] [--at DATE] [--availability-file PATH]
                       [--catalog-dir PATH] [--json]
    lee-llm-router census [--json]
    lee-llm-router classify-failure [--record FILE] [--json]
                          [--exit-code N] [--timed-out]
                          [--stdout TEXT] [--stderr TEXT] [--error TEXT]
                          [--oracle-exit-code N] [--oracle-timed-out]
                          [--oracle-error TEXT] [--oracle-cmd CMD]
                          [--accounting-status STATUS] [--usage-basis BASIS]
                          [--unaccounted-spend] [--verdict TEXT]
                          [--judgment CLASS] [--review-verdict TEXT]
    lee-llm-router next-action --input FILE [--repair-count N]
    lee-llm-router template
    lee-llm-router trace --last N
    lee-llm-router evidence rollup
    lee-llm-router evidence report --month YYYY-MM [--json]
                          [--catalog-dir PATH] [--availability-file PATH]
    lee-llm-router evidence import (--benchmark PATH [--csv PATH] |
                                    --agent-orch PATH)
    lee-llm-router export-source --dest <path> [--force]
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse
    from datetime import datetime
    from pathlib import Path

MANIFEST_NAME = ".lee_llm_router_export.json"


def check_config(
    config_path: str,
    role: str | None = None,
) -> tuple[list[str], list[str]]:
    """Validate a config file and its environment.

    Returns:
        (errors, warnings) - errors are blocking; warnings are informational.
    """
    import os
    import shutil
    from pathlib import Path

    from lee_llm_router.config import ConfigError, load_config
    from lee_llm_router.providers.base import LLMRouterError
    from lee_llm_router.providers.registry import get as get_provider

    errors: list[str] = []
    warnings: list[str] = []

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        return [f"Config invalid: {exc}"], []
    except Exception as exc:
        return [f"Unexpected error loading config: {exc}"], []

    for pname, pcfg in config.providers.items():
        try:
            provider = get_provider(pcfg.type)()
            provider.validate_config(pcfg.raw)
        except KeyError:
            warnings.append(
                f"Provider {pname!r}: unknown type {pcfg.type!r} - cannot validate"
            )
            continue
        except LLMRouterError as exc:
            errors.append(f"Provider {pname!r}: {exc}")
            continue

        if pcfg.type in ("openrouter_http", "openai_http"):
            api_key_env = pcfg.raw.get("api_key_env")
            if api_key_env and not os.environ.get(api_key_env):
                errors.append(f"Provider {pname!r}: env var {api_key_env!r} is not set")
            if not pcfg.raw.get("base_url"):
                warnings.append(
                    f"Provider {pname!r}: 'base_url' not set - will use default"
                )

        elif pcfg.type in (
            "codex_cli",
            "gemini_cli",
            "gemini",
            "claude_code_cli",
            "claude_code",
            "claude",
        ):
            if pcfg.type.startswith("gemini"):
                default_command = "gemini"
            elif pcfg.type.startswith("claude"):
                default_command = "claude"
            else:
                default_command = "codex"
            command = pcfg.raw.get("command", default_command)
            if not shutil.which(command):
                errors.append(
                    f"Provider {pname!r}: binary {command!r} not found in PATH"
                )

        elif pcfg.type in (
            "openai_codex_subscription_http",
            "openai_codex_http",
            "chatgpt_subscription_http",
        ):
            access_token_env = pcfg.raw.get("access_token_env")
            if access_token_env:
                if not os.environ.get(access_token_env):
                    errors.append(
                        f"Provider {pname!r}: env var {access_token_env!r} is not set"
                    )
            else:
                codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
                auth_path = codex_home / "auth.json"
                if not auth_path.exists():
                    warnings.append(
                        "Provider "
                        f"{pname!r}: no access_token_env set and no auth file found at "
                        f"{auth_path}"
                    )

        elif pcfg.type == "mock":
            pass

    target_role = role or config.default_role
    if target_role not in config.roles:
        errors.append(
            f"Role {target_role!r} not found in config. "
            f"Known roles: {', '.join(sorted(config.roles))}"
        )
        return errors, warnings

    try:
        role_cfg = config.roles[target_role]
        provider_cfg = config.providers[role_cfg.provider]
        get_provider(provider_cfg.type)
    except KeyError as exc:
        errors.append(f"Role {target_role!r} references an unknown provider: {exc}")

    return errors, warnings


def get_template() -> str:
    """Return the contents of the bundled llm.example.yaml."""
    from pathlib import Path

    template_path = Path(__file__).parent / "templates" / "llm.example.yaml"
    return template_path.read_text(encoding="utf-8")


def export_source(dest: str | Path, force: bool = False) -> dict[str, str | None]:
    """Export the package tree as a vendorable source snapshot."""
    import json
    import shutil
    from datetime import datetime, timezone
    from pathlib import Path

    from lee_llm_router import __version__

    package_root = Path(__file__).resolve().parent
    repo_root = package_root.parent.parent
    destination = Path(dest).expanduser().resolve()

    if destination.exists() and not destination.is_dir():
        raise ValueError(f"Destination exists and is not a directory: {destination}")

    if destination.exists() and any(destination.iterdir()):
        if not force:
            raise FileExistsError(
                f"Destination is not empty: {destination}. "
                "Re-run with --force to overwrite."
            )
        shutil.rmtree(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        package_root,
        destination,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )

    manifest = {
        "package": "lee_llm_router",
        "version": __version__,
        "source_repo": str(repo_root),
        "source_subdir": "src/lee_llm_router",
        "source_commit": _get_git_commit(repo_root),
        "exported_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    manifest_path = destination / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    return {
        "destination": str(destination),
        "manifest": str(manifest_path),
        "version": __version__,
        "source_commit": manifest["source_commit"],
    }


def _get_git_commit(repo_root: Path) -> str | None:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    commit = result.stdout.strip()
    return commit or None


def check_availability(
    snapshot_file: str | Path | None = None,
    now: datetime | None = None,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Inspect the availability snapshot the refresh script writes.

    The staleness, clock-skew, and malformed-input rules all belong to
    :func:`lee_llm_router.availability.load_availability`; this check only
    renders that reader's verdict. Absence is a valid state: before the hourly
    refresh has ever run there is no snapshot, and channel-dependent
    staffing routes become ``unknown``/ineligible rather than failing, so a
    missing file is a warning. A snapshot that is merely stale — too old, or
    stamped beyond the tolerated future skew — is also a warning. Only a file
    that exists but cannot be used at all is an error.

    Args:
        snapshot_file: Explicit snapshot path, or None to resolve the default.
        now: Reference time for the age calculation. A naive value is read as
            UTC by the reader, never as host-local time.

    Returns:
        ``(errors, warnings, details)`` where ``details`` carries the resolved
        ``path``, the snapshot's ``written_at``/``observed_at`` as ISO strings,
        its ``age_minutes`` (or None), the ``bucket_count``, and a
        ``channels`` map of channel name to health.
    """
    from lee_llm_router.availability import (
        FUTURE_TIMESTAMP_REASON,
        load_availability,
        resolve_availability_path,
    )

    path = resolve_availability_path(snapshot_file)
    details: dict[str, Any] = {
        "path": str(path),
        "written_at": None,
        "observed_at": None,
        "age_minutes": None,
        "bucket_count": 0,
        "channels": {},
    }

    if not path.is_file():
        return (
            [],
            [f"availability snapshot missing: {path} (run refresh_availability.sh)"],
            details,
        )

    snapshot = load_availability(path, now=now)
    details["written_at"] = (
        snapshot.written_at.isoformat() if snapshot.written_at else None
    )
    details["observed_at"] = (
        snapshot.observed_at.isoformat() if snapshot.observed_at else None
    )
    details["age_minutes"] = snapshot.age_minutes
    details["bucket_count"] = _count_buckets(snapshot)
    details["channels"] = {
        name: headroom.health.value for name, headroom in snapshot.channels.items()
    }

    if snapshot.stale and snapshot.stale_reason == FUTURE_TIMESTAMP_REASON:
        return [], [_stale_warning(path, snapshot)], details
    if snapshot.problem is not None:
        return (
            [f"availability snapshot unusable: {path}: {snapshot.problem}"],
            [],
            details,
        )
    if snapshot.stale:
        return [], [_stale_warning(path, snapshot)], details
    return [], [], details


def _count_buckets(snapshot: Any) -> int:
    """Count the distinct source entries behind a snapshot's channels.

    One raw ``ai-subs`` entry can feed more than one channel, so the per-channel
    buckets are de-duplicated by their provider and bucket name.

    Args:
        snapshot: The :class:`~lee_llm_router.availability.AvailabilitySnapshot`.

    Returns:
        The number of distinct ``(provider, name)`` pairs.
    """
    seen = {
        (bucket.provider, bucket.name)
        for headroom in snapshot.channels.values()
        for bucket in headroom.buckets
    }
    return len(seen)


def _stale_warning(path: Path, snapshot: Any) -> str:
    """Render the one-line staleness warning for a snapshot.

    Args:
        path: The snapshot path, for the message.
        snapshot: The :class:`~lee_llm_router.availability.AvailabilitySnapshot`.

    Returns:
        A warning naming the reader's ``stale_reason`` and the age it measured.
    """
    reason = snapshot.stale_reason or "snapshot is stale"
    if snapshot.age_minutes is None:
        return f"availability snapshot stale ({reason}): {path}, age unknown"
    return (
        f"availability snapshot stale ({reason}): {path}, "
        f"age {snapshot.age_minutes:.0f} min"
    )


GOVERNED_HARNESSES: tuple[str, ...] = (
    "codex_cli",
    "claude_code",
    "claude_code_cli",
    "omp_cli",
    "pi_cli",
    "opencode_cli",
    "antigravity_cli",
)
"""Harness identifiers a crew's governed route may name."""


def _first_sentence(text: str) -> str:
    """Return the first sentence of a description, collapsed to one line."""
    collapsed = " ".join(text.split())
    if not collapsed:
        return ""
    head, sep, _ = collapsed.partition(". ")
    return head + ("." if sep else "")


def check_crews(
    crews_file: str | None = None,
) -> tuple[list[str], list[str], int, int, int]:
    """Resolve every worker declared in the crews file, not only stage refs.

    Every entry of the top-level ``workers`` map is resolved, so a malformed
    worker is reported even when no crew stage references it. A crew stage whose
    role class is coding and names a worker that resolves to a role-scoped model
    (or an unmapped stage name) is reported as a warning rather than an error: the
    crews file is Auto-Orch's, not ours.

    Args:
        crews_file: Explicit crews file path, or None to use the default.

    Returns:
        ``(errors, warnings, crew_count, resolved_worker_count,
        total_worker_count)``.
    """
    from lee_llm_router.crews import (
        ROLE_SCOPED_CITATION,
        CrewsConfigError,
        is_role_scoped,
        load_crews,
        resolve_worker,
        role_class,
    )

    try:
        crews_config = load_crews(crews_file)
    except CrewsConfigError as exc:
        return [f"Crews invalid: {exc}"], [], 0, 0, 0

    errors: list[str] = []
    warnings: list[str] = []
    resolved: set[str] = set()
    resolved_workers: dict[str, Any] = {}

    for worker_id, worker in crews_config.workers.items():
        try:
            resolved_worker = resolve_worker(worker)
        except CrewsConfigError as exc:
            errors.append(f"Worker {worker_id!r}: {exc}")
            continue
        resolved.add(worker_id)
        resolved_workers[worker_id] = resolved_worker

    for crew in crews_config.crews.values():
        for stage in crew.stages.values():
            try:
                stage_class = role_class(stage.name)
            except CrewsConfigError:
                warnings.append(
                    f"stage {stage.name!r} has no role class ({ROLE_SCOPED_CITATION})"
                )
                stage_class = None

            for worker_id in stage.workers:
                worker = crews_config.workers.get(worker_id)
                if worker is None:
                    errors.append(
                        f"Crew {crew.name!r} stage {stage.name!r}: unknown worker "
                        f"{worker_id!r}"
                    )
                    continue
                if worker_id not in resolved:
                    errors.append(
                        f"Crew {crew.name!r} stage {stage.name!r}: worker "
                        f"{worker_id!r} could not be resolved"
                    )
                    continue

                if stage_class == "coding":
                    resolved_worker = resolved_workers[worker_id]
                    if is_role_scoped(resolved_worker.model):
                        warnings.append(
                            f"Crew {crew.name!r} stage {stage.name!r} uses role-scoped "
                            f"model {resolved_worker.model!r} in a coding role; "
                            f"staffing will never auto-select it there "
                            f"({ROLE_SCOPED_CITATION})"
                        )

        for route in crew.governed.values():
            if route.harness not in GOVERNED_HARNESSES:
                errors.append(
                    f"Crew {crew.name!r} governed role {route.role!r}: unknown "
                    f"harness {route.harness!r} (known: "
                    f"{', '.join(GOVERNED_HARNESSES)})"
                )

    return (
        errors,
        warnings,
        len(crews_config.crews),
        len(resolved),
        len(crews_config.workers),
    )


def crews_summary(crews_file: str | None = None) -> list[dict[str, Any]]:
    """Build a JSON-serialisable summary of every crew.

    Args:
        crews_file: Explicit crews file path, or None to use the default.

    Returns:
        One entry per crew, with resolved stage workers and governed routes.
    """
    from lee_llm_router.crews import STAGE_NAMES, load_crews, resolve_worker

    crews_config = load_crews(crews_file)
    summary: list[dict[str, Any]] = []
    for crew in crews_config.crews.values():
        stages: dict[str, Any] = {}
        for stage_name in STAGE_NAMES:
            stage = crew.stages.get(stage_name)
            if stage is None:
                continue
            worker = crews_config.workers[stage.primary]
            resolved = resolve_worker(worker)
            stages[stage_name] = {
                "worker_id": resolved.worker_id,
                "provider": resolved.provider,
                "model": resolved.model,
                "effort": resolved.effort,
                "eligible": list(stage.workers),
            }
        summary.append(
            {
                "name": crew.name,
                "description": _first_sentence(crew.description),
                "stages": stages,
                "governed": {
                    role: {
                        "harness": route.harness,
                        "model": route.model,
                        "effort": route.effort,
                    }
                    for role, route in crew.governed.items()
                },
            }
        )
    return summary


def _format_crew(entry: dict[str, Any]) -> list[str]:
    """Render one crew summary entry as plain-text lines."""
    lines = [f"{entry['name']} — {entry['description']}".rstrip(" —")]
    for stage_name, stage in entry["stages"].items():
        model = stage["model"]
        if stage["effort"]:
            model = f"{model}/{stage['effort']}"
        lines.append(
            f"  {stage_name}: {stage['worker_id']} ({stage['provider']} {model})"
        )
    governed = entry["governed"]
    if governed:
        parts = []
        for role, route in governed.items():
            target = route["harness"]
            if route["model"]:
                target = f"{target}/{route['model']}"
            if route["effort"]:
                target = f"{target}/{route['effort']}"
            parts.append(f"{role}={target}")
        lines.append("  governed: " + ", ".join(parts))
    return lines


def _run_crews_list(args: argparse.Namespace) -> int:
    import json

    from lee_llm_router.crews import CrewsConfigError

    try:
        summary = crews_summary(getattr(args, "crews_file", None))
    except CrewsConfigError as exc:
        print(f"Crews invalid: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    for entry in summary:
        for line in _format_crew(entry):
            print(line)
    return 0


class _CrewPageInputError(ValueError):
    """Raised when a crew-page input cannot be used safely."""


def _discover_benchmark_file() -> Path | None:
    """Return the lexically latest optional staffing-evidence sidecar."""
    from pathlib import Path

    exports = Path.home() / "projects" / "ai-workforce-benchmark" / "exports"
    candidates = sorted(
        (path for path in exports.glob("staffing-evidence-*.json") if path.is_file()),
        key=lambda path: path.name,
    )
    return candidates[-1] if candidates else None


def _read_page_events(path: str | Path) -> None:
    """Validate a caller-supplied event ledger without using the default one."""
    from lee_llm_router.events import read_events

    try:
        records = read_events(path)
    except (OSError, UnicodeError) as exc:
        raise _CrewPageInputError(f"event file cannot be read: {exc}") from exc
    if any("_malformed" in record for record in records):
        raise _CrewPageInputError(f"event file is malformed JSONL: {path}")


def _run_crews_page(args: argparse.Namespace) -> int:
    """Generate the static crew page from explicitly selected input paths."""
    import stat
    from pathlib import Path

    from lee_llm_router.availability import (
        load_availability,
        resolve_availability_path,
    )
    from lee_llm_router.crew_page import (
        BenchmarkEvidenceError,
        load_benchmark_evidence,
        select_proposals,
        write_crew_page,
    )
    from lee_llm_router.crews import CrewsConfigError, load_crews

    try:
        config = load_crews(getattr(args, "crews_file", None))
        availability_path = resolve_availability_path(
            getattr(args, "availability_file", None)
        )
        try:
            availability_stat = availability_path.stat()
        except FileNotFoundError:
            if availability_path.is_symlink():
                raise _CrewPageInputError(
                    f"availability snapshot is not a regular file: {availability_path}"
                )
            availability_present = False
        except OSError as exc:
            raise _CrewPageInputError(
                f"availability snapshot cannot be inspected: {availability_path}: {exc}"
            ) from exc
        else:
            availability_present = True
            if not stat.S_ISREG(availability_stat.st_mode):
                raise _CrewPageInputError(
                    f"availability snapshot is not a regular file: {availability_path}"
                )

        availability = load_availability(availability_path)
        if availability.problem is not None and availability_present:
            raise _CrewPageInputError(
                f"availability snapshot unusable: {availability.path}: "
                f"{availability.problem}"
            )

        benchmark_path = getattr(args, "benchmark_file", None)
        if benchmark_path is None:
            benchmark_path = _discover_benchmark_file()
        evidence = load_benchmark_evidence(benchmark_path)

        events_path = getattr(args, "events_file", None)
        if events_path is not None:
            _read_page_events(events_path)

        proposals = select_proposals(config, evidence)
    except (
        BenchmarkEvidenceError,
        CrewsConfigError,
        _CrewPageInputError,
        OSError,
        UnicodeError,
    ) as exc:
        message = " ".join(str(exc).split())
        print(f"crews page: configuration error: {message}", file=sys.stderr)
        return 3

    try:
        written = write_crew_page(
            Path(args.out).expanduser(), config, availability, evidence, proposals
        )
    except (OSError, UnicodeError, ValueError) as exc:
        message = " ".join(str(exc).split())
        print(f"crews page: could not write output: {message}", file=sys.stderr)
        return 3

    print(written)
    return 0


def check_catalog(
    catalog_dir: str | Path,
    schema_dir: str | Path | None = None,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Validate and load the six staffing catalog documents in a directory.

    All shape validation, schema handling, and typed construction belong to
    :func:`lee_llm_router.staffing.catalog.load_staffing_catalog`; this check
    only renders that reader's verdict. The returned errors carry the loader's
    stable message, which always names the offending document and, when known,
    the JSON path of the field at fault.

    Args:
        catalog_dir: Directory holding the six YAML documents.
        schema_dir: Explicit schema directory, or None for the bundled default.

    Returns:
        ``(errors, warnings, details)`` where ``details`` carries the resolved
        ``path`` and, on success, per-document element counts under ``counts``.
    """
    from pathlib import Path

    from lee_llm_router.staffing.catalog import (
        StaffingCatalogError,
        load_staffing_catalog,
    )

    path = Path(catalog_dir)
    details: dict[str, Any] = {"path": str(path), "counts": {}}

    try:
        catalog = load_staffing_catalog(path, schema_dir=schema_dir)
    except StaffingCatalogError as exc:
        return [f"catalog invalid: {exc}"], [], details

    details["counts"] = {
        "routes": len(catalog.routes.routes),
        "channels": len(catalog.channels.channels),
        "terms": len(catalog.terms.terms),
        "crews": len(catalog.crews.crews),
    }
    return [], [], details


def _default_catalog_dir() -> Path:
    """Return the repository's bundled ``config/staffing`` catalog directory."""
    from pathlib import Path

    return Path(__file__).resolve().parents[2] / "config" / "staffing"


def _run_doctor(args: argparse.Namespace) -> int:
    config_path = args.config
    role = getattr(args, "role", None)

    if getattr(args, "catalog", False):
        catalog_dir = getattr(args, "catalog_dir", None)
        if catalog_dir is None:
            catalog_dir = _default_catalog_dir()
        catalog_errors, catalog_warnings, catalog_details = check_catalog(catalog_dir)
        for warning in catalog_warnings:
            print(f"  !  {warning}")
        for error in catalog_errors:
            print(f"  x  {error}", file=sys.stderr)
        if catalog_errors:
            print(
                f"\nStatus: {len(catalog_errors)} catalog error(s) found",
                file=sys.stderr,
            )
            return 3
        counts = catalog_details["counts"]
        print(
            f"OK catalog: {catalog_details['path']} "
            f"(routes {counts['routes']}, channels {counts['channels']}, "
            f"terms {counts['terms']}, crews {counts['crews']})"
        )
        if config_path is None and not any(
            getattr(args, flag, False) for flag in ("crews", "availability")
        ):
            return 0
        print()

    if getattr(args, "crews", False):
        (
            crew_errors,
            crew_warnings,
            crew_count,
            worker_count,
            total_workers,
        ) = check_crews(getattr(args, "crews_file", None))
        for warning in crew_warnings:
            print(f"  !  {warning}")
        for error in crew_errors:
            print(f"  x  {error}", file=sys.stderr)
        if crew_errors:
            print(f"\nStatus: {len(crew_errors)} crews error(s) found", file=sys.stderr)
            return 1
        print(
            f"OK crews: {crew_count} crews, "
            f"{worker_count}/{total_workers} workers resolved, "
            f"{len(crew_warnings)} role-scoped warning(s)"
        )
        if config_path is None and not getattr(args, "availability", False):
            return 0
        print()

    if getattr(args, "availability", False):
        avail_errors, avail_warnings, details = check_availability(
            getattr(args, "availability_file", None)
        )
        for warning in avail_warnings:
            print(f"  !  {warning}")
        for error in avail_errors:
            print(f"  x  {error}", file=sys.stderr)
        if avail_errors:
            print(
                f"\nStatus: {len(avail_errors)} availability error(s) found",
                file=sys.stderr,
            )
            return 1
        if not avail_warnings:
            age = details["age_minutes"] or 0.0
            print(
                f"OK availability: {details['path']}, age {age:.0f} min, "
                f"{details['bucket_count']} buckets"
            )
            for channel, health in details["channels"].items():
                print(f"  {channel}: {health}")
        if config_path is None:
            return 0
        print()

    if config_path is None:
        print(
            "doctor requires --config (or --crews / --availability / --catalog)",
            file=sys.stderr,
        )
        return 1

    print("Lee LLM Router Doctor")
    print(f"Config: {config_path}")
    print()

    errors, warnings = check_config(config_path, role)

    for warning in warnings:
        print(f"  !  {warning}")
    for error in errors:
        print(f"  x  {error}", file=sys.stderr)

    if not errors and not warnings:
        print("  OK  All checks passed")
    elif not errors:
        print(f"\n  OK  {len(warnings)} warning(s) - no blocking errors")

    if errors:
        print(f"\nStatus: {len(errors)} error(s) found", file=sys.stderr)
        return 1

    return 0


def _run_template(_args: argparse.Namespace) -> int:
    print(get_template(), end="")
    return 0


def _run_trace(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    trace_dir = Path(args.dir) if args.dir else Path(".lee-llm-router") / "traces"
    n = args.last

    if not trace_dir.exists():
        print(f"No trace directory found: {trace_dir}", file=sys.stderr)
        return 1

    trace_files = sorted(
        trace_dir.rglob("*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    if not trace_files:
        print("No traces found.")
        return 0

    for trace_file in trace_files[:n]:
        try:
            data = json.loads(trace_file.read_text(encoding="utf-8"))
            status = "ERROR" if data.get("error") else "OK"
            elapsed = f"{data.get('elapsed_ms') or 0:.0f}ms"
            attempt = int(data.get("attempt", 0) or 0)
            print(
                f"{str(data.get('request_id', '?'))[:8]}  "
                f"{str(data.get('started_at', '?'))[:19]}  "
                f"{str(data.get('role', '?')):<12}  "
                f"{str(data.get('provider', '?')):<20}  "
                f"a{attempt:<5}  "
                f"{str(data.get('model', '?')):<20}  "
                f"{status:<6}  {elapsed}"
            )
        except Exception as exc:
            print(f"  [could not parse {trace_file}: {exc}]", file=sys.stderr)

    return 0


def _run_evidence_rollup(_args: argparse.Namespace) -> int:
    """Read the configured validated attempt ledger and print its rollup."""
    from lee_llm_router.staffing.ledger import AttemptLedgerError
    from lee_llm_router.staffing.rollup import render_rollup, rollup_ledger

    try:
        summary = rollup_ledger()
    except (AttemptLedgerError, OSError, UnicodeError) as exc:
        message = " ".join(str(exc).split())
        print(f"evidence rollup: {message}", file=sys.stderr)
        return 3

    print(render_rollup(summary))
    return 0


def _run_evidence_report(args: argparse.Namespace) -> int:
    """Run ``evidence report``: print per-class/per-route evidence report."""
    from pathlib import Path

    from lee_llm_router.staffing.evidence_report import (
        build_evidence_report,
        render_evidence_report,
    )
    from lee_llm_router.staffing.json_int import dump_json

    def fail(message: str) -> int:
        print(f"evidence report: {message}", file=sys.stderr)
        return 3

    if args.month is None:
        return fail("--month is required (YYYY-MM)")

    catalog_dir = (
        Path(args.catalog_dir)
        if args.catalog_dir is not None
        else _default_catalog_dir()
    )
    availability_file = args.availability_file

    try:
        report = build_evidence_report(
            month=args.month,
            catalog_dir=catalog_dir,
            availability_file=availability_file,
        )
    except Exception as exc:
        message = " ".join(str(exc).split())
        return fail(message)

    if args.json:
        print(dump_json(report))
    else:
        print(render_evidence_report(report))
    return 0


def _run_evidence_ladders(args: argparse.Namespace) -> int:
    """Run ``evidence ladders --derive``: derive cheapest-first ladders
    from the attempt ledger, write the result, and diff against both
    hand-authored ladder sources.
    """
    from pathlib import Path

    from lee_llm_router.staffing import (
        StaffingCatalogError,
        load_staffing_catalog,
    )
    from lee_llm_router.staffing.json_int import dump_json
    from lee_llm_router.staffing.ladder_derivation import (
        derive_ladders,
        diff_against_benchmark,
        diff_against_crews,
        render_ladder_diff,
        write_derived_ladders,
    )

    def fail(message: str) -> int:
        print(f"evidence ladders: {message}", file=sys.stderr)
        return 3

    catalog_dir = (
        Path(args.catalog_dir)
        if args.catalog_dir is not None
        else _default_catalog_dir()
    )

    # Validate catalog dir early: an invalid --catalog-dir is a caller
    # error and hard-fails (same pattern as _run_price / _run_route_show
    # and P5-1's fix).
    try:
        load_staffing_catalog(catalog_dir)
    except StaffingCatalogError as exc:
        return fail(f"catalog invalid: {exc}")
    except Exception as exc:
        return fail(f"catalog invalid: {exc}")

    output_dir = Path(args.output_dir) if args.output_dir is not None else None

    try:
        derived = derive_ladders()
    except Exception as exc:
        message = " ".join(str(exc).split())
        return fail(message)

    # Write the derived ladders file
    try:
        written_path = write_derived_ladders(derived, output_dir=output_dir)
    except (OSError, UnicodeError) as exc:
        message = " ".join(str(exc).split())
        return fail(f"could not write output: {message}")

    # Diff against benchmark and crews
    try:
        benchmark_diffs = diff_against_benchmark(derived, catalog_dir=catalog_dir)
        crews_diffs = diff_against_crews(derived, catalog_dir=catalog_dir)
    except Exception as exc:
        message = " ".join(str(exc).split())
        return fail(f"diff failed: {message}")

    if args.json:
        # JSON output: include derived ladders and diffs
        output = {
            "derived_ladders": derived,
            "written_to": str(written_path),
            "benchmark_diffs": benchmark_diffs,
            "crews_diffs": crews_diffs,
        }
        print(dump_json(output))
    else:
        # Text output
        print(f"Evidence ladders derived and written to {written_path}")
        print()
        print(render_ladder_diff(benchmark_diffs, crews_diffs))

    return 0


def _run_evidence_import(args: argparse.Namespace) -> int:
    """Import benchmark or recent Agent-Orch evidence."""
    from lee_llm_router.staffing.import_evidence import (
        EvidenceImportError,
        import_agent_orch_evidence,
        import_benchmark_evidence,
    )
    from lee_llm_router.staffing.ledger import AttemptLedgerError, ShortWriteError

    try:
        if args.agent_orch is not None:
            if args.csv is not None:
                raise EvidenceImportError("--csv requires --benchmark")
            summary = import_agent_orch_evidence(args.agent_orch)
        else:
            summary = import_benchmark_evidence(args.benchmark, csv_path=args.csv)
    except (EvidenceImportError, AttemptLedgerError, ShortWriteError, OSError) as exc:
        message = " ".join(str(exc).split())
        print(f"evidence import: {message}", file=sys.stderr)
        return 3
    for issue in summary.issues:
        identity = f" run_id={issue.run_id}" if issue.run_id else ""
        print(
            f"evidence import: skipped row {issue.row_number}{identity}: "
            f"{issue.reason}",
            file=sys.stderr,
        )
    print(f"imported={summary.imported} skipped={summary.skipped}")
    return 0


def _run_export_source(args: argparse.Namespace) -> int:
    try:
        result = export_source(args.dest, force=args.force)
    except (FileExistsError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print("Lee LLM Router Source Export")
    print(f"Destination: {result['destination']}")
    print(f"Manifest: {result['manifest']}")
    print(f"Version: {result['version']}")
    print(f"Source commit: {result['source_commit'] or 'unknown'}")
    return 0


def _run_shims_install(args: argparse.Namespace) -> int:
    dry_run = getattr(args, "dry_run", False)
    apply = getattr(args, "apply", False)
    if (dry_run and apply) or (not dry_run and not apply):
        print(
            "shims install: exactly one of --dry-run or --apply is required",
            file=sys.stderr,
        )
        return 3

    from lee_llm_router import shims

    try:
        return shims.install_shims(
            dry_run=dry_run,
            apply=apply,
            force=getattr(args, "force", False),
            harnesses=getattr(args, "harness", None),
            project=getattr(args, "project", None),
            command=getattr(args, "shim_command", "crew"),
        )
    except shims.ShimUsageError as exc:
        print(f"shims install: {exc}", file=sys.stderr)
        return 3


def _run_shims_diff(args: argparse.Namespace) -> int:
    from lee_llm_router import shims

    try:
        return shims.diff_shims(
            harnesses=getattr(args, "harness", None),
            project=getattr(args, "project", None),
            command=getattr(args, "shim_command", "crew"),
        )
    except shims.ShimUsageError as exc:
        print(f"shims diff: {exc}", file=sys.stderr)
        return 3


def _run_shims_no_subcommand(_args: argparse.Namespace) -> int:
    print("shims: subcommand required (install, diff)", file=sys.stderr)
    return 3


# ---------------------------------------------------------------------------
# catalog explain (P0-5b): per-route eligibility report over the catalog
# ---------------------------------------------------------------------------

FLOORS_DISCLOSURE: str = "floors recorded, not enforced"
"""Chief round 15 (D86/D87) fact printed exactly once per explain run.

The committed catalog's ``role_floors`` records are recorded data only in
Phase 0: eligibility applies no floor check and excludes no route by one.
"""

INDEPENDENCE_NOT_EVALUATED: str = "independence not evaluated (no --author-route)"
"""Chief round 15 (packet D) absent-input disclosure, printed exactly once.

Independence is enforced only when explain receives ``--author-route``
(Chief round 15 ruling 2); when the input is absent this exact disclosure
replaces any exclusion — no route is excluded for independence without it.
"""

_INDEPENDENCE_APPLICABLE_ROLES: frozenset[str] = frozenset({"review", "judge"})
"""Roles for which author-route independence applies (review/judge only)."""


def _independence_record(
    role: str,
    author_route_id: str | None,
    catalog: Any,
) -> dict[str, Any]:
    """Build the deterministic top-level ``independence`` explain record.

    Absent input: ``evaluated`` false plus the exact absent-input
    disclosure. Supplied input for review/judge: ``evaluated`` true plus
    the author route, its derived family, and the family source. Supplied
    input for any other role: not applicable — ``evaluated`` stays false
    and the record says so rather than pretending the check ran. Raises
    :class:`StaffingEligibilityError` on an unknown author route id (the
    eligibility evaluation already fails closed on the same input).
    """
    from lee_llm_router.staffing.eligibility import (
        resolve_author_route,
        resolve_route_family,
    )

    applicable = role in _INDEPENDENCE_APPLICABLE_ROLES
    if author_route_id is None:
        return {
            "evaluated": False,
            "applicable": applicable,
            "disclosure": INDEPENDENCE_NOT_EVALUATED,
        }
    author_route = resolve_author_route(catalog, author_route_id)
    family, family_source = resolve_route_family(author_route)
    record: dict[str, Any] = {
        "evaluated": applicable,
        "applicable": applicable,
        "author_route": author_route_id,
        "author_family": family,
        "family_source": family_source,
    }
    if not applicable:
        record["disclosure"] = f"independence not applicable for role {role!r}"
    return record


def _independence_text_line(record: dict[str, Any]) -> str:
    """Render the one independence disclosure line for text output."""
    if record["evaluated"]:
        return (
            f"independence: author route {record['author_route']}, "
            f"family {record['author_family']} "
            f"(from {record['family_source']})"
        )
    if record.get("author_route") is not None:
        # Not-applicable-with-input case: record the supplied author route,
        # its derived family, and the source once, alongside the disclosure.
        return (
            f"{record['disclosure']} (author route {record['author_route']}, "
            f"family {record['author_family']} "
            f"(from {record['family_source']}))"
        )
    return record["disclosure"]


def _parse_class_string(
    class_string: str,
) -> tuple[str, str, tuple[str, ...], str, str]:
    """Parse the canonical five-segment class string into its fields.

    Grammar: ``role/oracle_type/domain_tags/size_band/language``; the
    domain-tags segment is ``+``-joined tags with the empty set written as
    the literal ``none``. Raises ``ValueError`` with a user-facing message
    for any non-canonical shape; committed enumerations and canonical-key
    equality are enforced by the eligibility API itself.
    """
    segments = class_string.split("/")
    if len(segments) != 5 or any(segment == "" for segment in segments):
        raise ValueError(
            "--class must be the canonical five-segment string "
            "role/oracle_type/domain_tags/size_band/language (e.g. "
            "impl/deterministic/none/s/python)"
        )
    role, oracle_type, tags_segment, size_band, language = segments
    if tags_segment == "none":
        domain_tags: tuple[str, ...] = ()
    else:
        domain_tags = tuple(tags_segment.split("+"))
        if any(not tag for tag in domain_tags):
            raise ValueError(
                "--class domain_tags segment is not a canonical '+ '-joined "
                "tag list or the literal 'none'"
            )
    return role, oracle_type, domain_tags, size_band, language


def _explain_sort_key(row: Any) -> tuple[int, float, float, str]:
    """Display order: marginal price ascending, unpriced last, id tie-break.

    Display-only ordering (allowed by the P0-5b packet); eligibility itself
    is never ranked, chosen, or filtered by this ordering.
    """
    pricing = row.pricing
    if pricing is None:
        return (1, 0.0, 0.0, row.route_id)
    return (
        0,
        pricing.marginal_input_usd_per_token,
        pricing.marginal_output_usd_per_token,
        row.route_id,
    )


def _explain_route_json(row: Any) -> dict[str, Any]:
    """JSON view of one eligibility row: committed fields only.

    Deliberately excludes the row's model, effort, harness, status_reason,
    and pricing-source machinery — the route id is the only route label.
    """
    pricing = row.pricing
    return {
        "route_id": row.route_id,
        "eligible": row.eligible,
        "channel": row.channel,
        "badge": row.availability_badge,
        "headroom": row.availability_headroom,
        "health": row.availability_health,
        "marginal_input_usd_per_token": (
            pricing.marginal_input_usd_per_token if pricing else None
        ),
        "marginal_output_usd_per_token": (
            pricing.marginal_output_usd_per_token if pricing else None
        ),
        "replacement_input_usd_per_token": (
            pricing.replacement_input_usd_per_token if pricing else None
        ),
        "replacement_output_usd_per_token": (
            pricing.replacement_output_usd_per_token if pricing else None
        ),
        "reasons": list(row.reasons),
    }


def _explain_table(rows: list[dict[str, Any]]) -> list[str]:
    """Render the aligned human-readable table for ordered route dicts."""

    def price(value: Any) -> str:
        return "-" if value is None else f"{value:.8g}"

    def headroom(value: Any) -> str:
        return "-" if value is None else f"{value * 100:.0f}%"

    headers = (
        "ROUTE ID",
        "STATUS",
        "CHANNEL",
        "BADGE",
        "HEADROOM",
        "HEALTH",
        "MARG IN $/TOK",
        "MARG OUT $/TOK",
        "REPL IN $/TOK",
        "REPL OUT $/TOK",
        "REASONS",
    )
    table_rows: list[tuple[str, ...]] = []
    for route in rows:
        table_rows.append(
            (
                route["route_id"],
                "eligible" if route["eligible"] else "excluded",
                route["channel"],
                route["badge"] or "-",
                headroom(route["headroom"]),
                route["health"],
                price(route["marginal_input_usd_per_token"]),
                price(route["marginal_output_usd_per_token"]),
                price(route["replacement_input_usd_per_token"]),
                price(route["replacement_output_usd_per_token"]),
                "; ".join(route["reasons"]) if route["reasons"] else "-",
            )
        )
    widths = [
        (
            max(len(headers[i]), *(len(r[i]) for r in table_rows))
            if table_rows
            else len(headers[i])
        )
        for i in range(len(headers))
    ]
    lines = [
        "  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip(),
        "  ".join("-" * w for w in widths),
    ]
    for table_row in table_rows:
        lines.append(
            "  ".join(cell.ljust(w) for cell, w in zip(table_row, widths)).rstrip()
        )
    return lines


def _selected_terms_view(
    catalog: Any,
    at_date: Any,
) -> dict[str, dict[str, Any]] | None:
    """Selected dated terms per channel via the committed ``terms_at`` lookup.

    Display-only projection of :func:`lee_llm_router.staffing.terms.terms_at`
    — no date selection is reimplemented here: each channel's selected
    ``effective_from``, ``fee_usd_month``, and term-kind tier label
    (``kind``, the TermsEntry field, verbatim) are copied unchanged, with
    the ``"unknown"`` literal shown without coercion. Returns ``None``
    when the committed lookup fails closed for the requested date (e.g. no
    entry yet); no value is ever invented. Channel order follows the
    committed channels document, so the view is deterministic.
    """
    from lee_llm_router.staffing import StaffingTermsError, terms_at

    try:
        selected = terms_at(at_date, catalog)
    except StaffingTermsError:
        return None
    return {
        channel: {
            "effective_from": entry.effective_from,
            "fee_usd_month": entry.fee_usd_month,
            "kind": entry.kind,
        }
        for channel, entry in selected.items()
    }


def _terms_lines(
    terms_view: dict[str, dict[str, Any]] | None,
    at_date: Any,
) -> list[str]:
    """Render the compact deterministic selected-terms lines for text output."""
    if terms_view is None:
        return [f"selected terms: unavailable at {at_date.isoformat()}"]
    lines = [f"selected terms at {at_date.isoformat()}"]
    for channel, entry in terms_view.items():
        lines.append(
            f"  {channel}: effective_from {entry['effective_from']}, "
            f"fee_usd_month {entry['fee_usd_month']}, "
            f"kind {entry['kind']}"
        )
    return lines


def _run_catalog_explain(args: argparse.Namespace) -> int:
    """Run ``catalog explain``: eligibility per route, ordered for display.

    The report also projects the dated terms the committed ``terms_at``
    lookup selects for the requested date (display only, in both text and
    ``--json`` output) so the selected ``effective_from``/``fee_usd_month``
    terms and the per-channel term-kind tier label (``kind``) are observable;
    it performs no choice, probability, or ladder.
    Per Chief round 15 (D86/D87) it discloses exactly once per run — as the
    ``floors recorded, not enforced`` text line and as the top-level
    ``floors_disclosure`` JSON value — that the catalog's ``role_floors``
    records are recorded data only in Phase 0 and exclude no route.
    Per Chief round 15 packet D it also discloses author-route independence
    exactly once: with no ``--author-route`` the exact absent-input line
    ``independence not evaluated (no --author-route)`` (text) and the
    top-level ``independence`` record (JSON, ``evaluated`` false); with an
    author route supplied for review/judge the author route, its derived
    family, and the family source are recorded once, and eligibility has
    already excluded that route and every same-family candidate with the
    reason ``independence``. For other roles the check is recorded as not
    applicable rather than evaluated. Independence is an author/candidate
    comparison, never a class-to-model preference (D206).
    """
    import json
    from datetime import date
    from pathlib import Path

    from lee_llm_router.availability import load_availability
    from lee_llm_router.staffing import (
        StaffingCatalogError,
        StaffingEligibilityError,
        evaluate_eligibility,
        load_staffing_catalog,
    )

    def fail(message: str) -> int:
        print(f"catalog explain: {message}", file=sys.stderr)
        return 3

    try:
        role, oracle_type, domain_tags, size_band, language = _parse_class_string(
            args.class_string
        )
    except ValueError as exc:
        return fail(str(exc))
    if role != args.role:
        return fail(
            f"--role {args.role!r} does not equal the --class role segment " f"{role!r}"
        )

    if args.at is not None:
        try:
            at_date = date.fromisoformat(args.at)
        except ValueError:
            return fail(f"--at: not an ISO date (YYYY-MM-DD): {args.at!r}")
    else:
        at_date = date.today()

    catalog_dir = (
        Path(args.catalog_dir)
        if args.catalog_dir is not None
        else _default_catalog_dir()
    )
    try:
        catalog = load_staffing_catalog(catalog_dir)
    except StaffingCatalogError as exc:
        return fail(f"catalog invalid: {exc}")
    except Exception as exc:  # no traceback on unexpected catalog problems
        return fail(f"catalog invalid: {exc}")

    availability = load_availability(args.availability_file)
    if args.availability_file is not None and availability.problem is not None:
        return fail(f"availability snapshot unusable: {availability.problem}")

    try:
        rows = evaluate_eligibility(
            catalog,
            role=role,
            oracle_type=oracle_type,
            size_band=size_band,
            language=language,
            domain_tags=domain_tags,
            class_key=args.class_string,
            author_route_id=args.author_route,
            availability=availability,
            at_date=at_date,
        )
    except StaffingEligibilityError as exc:
        return fail(str(exc))
    except Exception as exc:  # no traceback on unexpected evaluation errors
        return fail(f"evaluation failed: {exc}")

    try:
        independence = _independence_record(role, args.author_route, catalog)
    except StaffingEligibilityError as exc:
        return fail(str(exc))

    ordered = sorted(rows, key=_explain_sort_key)
    route_views = [_explain_route_json(row) for row in ordered]
    eligible_count = sum(1 for route in route_views if route["eligible"])
    terms_view = _selected_terms_view(catalog, at_date)

    if args.json:
        print(
            json.dumps(
                {
                    "role": role,
                    "class_key": args.class_string,
                    "at": at_date.isoformat(),
                    "terms": terms_view,
                    "floors_disclosure": FLOORS_DISCLOSURE,
                    "independence": independence,
                    "routes": route_views,
                },
                indent=2,
            )
        )
    else:
        print(f"catalog explain — class {args.class_string} at {at_date.isoformat()}")
        print()
        for line in _explain_table(route_views):
            print(line)
        print(
            f"\n{len(route_views)} routes: {eligible_count} eligible, "
            f"{len(route_views) - eligible_count} excluded "
            "(ordered by marginal price)"
        )
        for line in _terms_lines(terms_view, at_date):
            print(line)
        print(FLOORS_DISCLOSURE)
        print(_independence_text_line(independence))
    return 0


def _run_catalog_no_subcommand(_args: argparse.Namespace) -> int:
    print("catalog: subcommand required (explain)", file=sys.stderr)
    return 3


# ---------------------------------------------------------------------------
# staffing run (P1-5b2): select one route, dispatch it once, and record it
# ---------------------------------------------------------------------------


def _run_run(args: argparse.Namespace) -> int:
    """Run ``run``: select, dispatch, validate, append, and print one attempt.

    Corrected D209 contract (Chief ruling,
    ``docs/staffing/chief-answers-p1-1.md``, quoted):
    ``lee-llm-router run --role R --class C --packet FILE [--route ID]
    [--supervisor-route ROUTE_ID] [--author-route ROUTE_ID] [--workdir DIR]
    [--parent ATTEMPT_ID --escalation-reason R] [--timeout S] [--json]``.
    ``--role`` and ``--class`` are always
    required: they describe the work and drive eligibility (role scoping,
    never-automatic, headroom, don't-cheap-trial flag). Without ``--route``
    the selection basis is ``explain_cheapest_eligible`` for that role and
    class; with ``--route ID`` the basis is ``explicit`` and the route must
    be eligible under the same role/class eligibility path as ``catalog
    explain`` — an ineligible explicit route exits 3 with the explain
    reason. ``--author-route ROUTE_ID`` (Astra final-review finding 4) is
    forwarded to the same path: for a review/judge class the author route
    and every same-family candidate are excluded with the reason
    ``independence`` exactly as ``catalog explain --author-route`` reports,
    and an unknown author id fails closed. With ``--supervisor-route
    ROUTE_ID`` the caller attests its own route (P1-4 ruling 3); the id is
    validated as a governed identity (known, active, currently usable —
    unknown/inactive/exhausted ids exit 3 and launch nothing) without
    imposing the worker's role/class capability policy on the attested
    supervisor, and the attested run can reach ``verified_success: true``
    only when dispatch, oracle, and every evidence gate hold.

    All caller-controlled record metadata (the ``--parent``/
    ``--escalation-reason`` pair and ``attempt_id``) is validated against
    the committed attempt-record v2 value rules before anything is launched
    (Astra final-review finding 6): an invalid value is an exit-3 refusal
    that launches nothing and appends nothing. P1-5a dispatches exactly one
    provider ``build_command`` argv through
    the watchdog subprocess boundary, with the packet text as the prompt,
    ``cwd`` set to the workdir, and no shell anywhere. Pi runs its JSON event
    mode, Codex is forced to ``json_flag: --json``, and Claude reuses the
    committed governed stream-json capture. P1-5b1 adds one optional,
    parsed-with-shlex oracle after the worker. The oracle is argv-only, uses
    the workdir, and receives only the remaining timeout budget. Its result
    is verification evidence; it never retries or escalates. When the oracle
    cannot be launched or set up *after* the worker completed (for example a
    workdir deleted between the two boundaries), the completed worker is
    never dropped: exactly one schema-valid attempt is persisted preserving
    the worker evidence and the oracle failure with a truthful verdict,
    failure class, and ``verified_success``, then the governed failure is
    returned (exit 3). P1-5b2 now
    calculates cost, builds one v2 record, validates it, appends it once,
    and prints that record (or a compact equivalent). ``run`` never
    escalates: a supplied parent/reason pair is recorded as a link.

    Exit codes: 0 when the child exited 0; the child's exit code otherwise
    (124 on a ceiling timeout); 3 for every refusal before or around the
    launch (argument pairing, packet/workdir problems, catalog or snapshot
    errors, selection refusals, owned-path/registry refusals, dispatch
    wiring errors) — a refused run launches nothing.

    P3-4 (D213 ruling 4) adds the live-run registry: ``--owned-paths PATH``
    is repeatable and required (empty owned paths are refused because they
    cannot prove disjointness). Before launch the run atomically registers
    pid, route, packet id, the normalized owned paths, and its start under
    the per-host state directory; when any live registry record owns an
    intersecting owned path (equality or ancestor/descendant), the run is
    refused with exit 3 and launches nothing. The record carries a Linux
    start-time process identity when available (pid reuse is detected, not
    trusted) with a conservative portable fallback. Deregistration happens
    on every boundary after registration — normal exit, governed failure,
    exception, ceiling timeout, interrupt — best-effort, never masking the
    attempt record's truth. Concurrent runs are permitted only for disjoint
    owned paths.
    """
    import json
    from datetime import date
    from pathlib import Path

    from lee_llm_router.availability import load_availability
    from lee_llm_router.providers.base import LLMRouterError
    from lee_llm_router.staffing import (
        credentials,
        load_staffing_catalog,
    )
    from lee_llm_router.staffing.census import (
        RunRegistryError,
        deregister_run,
        normalize_owned_paths,
        register_run,
    )
    from lee_llm_router.staffing.json_int import dump_json
    from lee_llm_router.staffing.ledger import (
        AttemptLedgerError,
        append_attempt,
        read_attempts,
        resolve_attempts_path,
    )
    from lee_llm_router.staffing.run import (
        OracleOutcome,
        RunDispatchError,
        RunSelectionError,
        build_attempt_record,
        dispatch_route,
        no_work_evidence,
        oracle_timeout_seconds,
        packet_id_for_text,
        parse_oracle_command,
        run_oracle,
        run_summary_lines,
        select_route,
        unchanged_redispatch_refusal,
        validate_attempt_metadata,
    )

    def fail(
        message: str,
        *,
        as_json: bool = False,
        exit_code: int = 3,
        kind: str | None = None,
    ) -> int:
        print(f"run: {message}", file=sys.stderr)
        if as_json:
            payload = {"error": message, "exit_code": exit_code}
            if kind is not None:
                payload["kind"] = kind
            print(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        return exit_code

    as_json = bool(getattr(args, "json", False))
    route_id = getattr(args, "route", None)
    role = args.role
    class_string = args.class_string
    class_derivation = None

    # Optional parent/escalation pair: parsed, pairing-checked, and
    # value-validated against the committed attempt-record v2 rules before
    # anything is launched (Astra final-review finding 6).  ``run`` never
    # escalates; a supervisor supplies the link when it launches one.
    parent = getattr(args, "parent", None)
    escalation_reason = getattr(args, "escalation_reason", None)
    if (parent is None) != (escalation_reason is None):
        return fail(
            "--parent and --escalation-reason must be supplied together",
            as_json=as_json,
        )
    try:
        validate_attempt_metadata(
            parent_attempt_id=parent,
            escalation_reason=escalation_reason,
            attempt_id=getattr(args, "attempt_id", None),
        )
    except RunSelectionError as exc:
        return fail(str(exc), as_json=as_json, exit_code=exc.exit_code)

    packet_path = Path(args.packet).expanduser()
    try:
        prompt = packet_path.read_text(encoding="utf-8")
    except OSError as exc:
        return fail(f"packet file cannot be read: {exc}", as_json=as_json)
    if not prompt.strip():
        return fail(f"packet file is empty: {packet_path}", as_json=as_json)

    # --role and --class are always required (Chief's corrected D209
    # contract): they describe the work and drive eligibility even when
    # --route names the candidate explicitly. No stored or default class;
    # no eligibility check that omits class policy.
    try:
        class_role, oracle_type, domain_tags, size_band, language = _parse_class_string(
            class_string
        )
    except ValueError as exc:
        return fail(str(exc), as_json=as_json)
    if class_role != role:
        return fail(
            f"--role {role!r} does not equal the --class role segment "
            f"{class_role!r}",
            as_json=as_json,
        )

    derivation_path = getattr(args, "class_derivation", None)
    if derivation_path is not None:
        try:
            derivation_payload = json.loads(
                Path(derivation_path).expanduser().read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return fail(f"class derivation cannot be read: {exc}", as_json=as_json)
        if not isinstance(derivation_payload, dict):
            return fail("class derivation must be a JSON object", as_json=as_json)
        candidate = derivation_payload.get("class_derivation", derivation_payload)
        if not isinstance(candidate, dict):
            return fail("class_derivation must be a JSON object", as_json=as_json)
        derived_class = candidate.get("class")
        overrides = candidate.get("override_records")
        if (
            not isinstance(derived_class, dict)
            or derived_class.get("class_key") != class_string
            or not isinstance(overrides, list)
            or not all(isinstance(item, dict) for item in overrides)
        ):
            return fail(
                "class derivation must match --class and contain override_records",
                as_json=as_json,
            )
        class_derivation = candidate

    if args.at is not None:
        try:
            at_date = date.fromisoformat(args.at)
        except ValueError:
            return fail(
                f"--at: not an ISO date (YYYY-MM-DD): {args.at!r}", as_json=as_json
            )
    else:
        at_date = date.today()

    catalog_dir = (
        Path(args.catalog_dir)
        if args.catalog_dir is not None
        else _default_catalog_dir()
    )
    try:
        catalog = load_staffing_catalog(catalog_dir)
    except Exception as exc:
        return fail(f"catalog invalid: {exc}", as_json=as_json)

    availability = load_availability(args.availability_file)
    if args.availability_file is not None and availability.problem is not None:
        return fail(
            f"availability snapshot unusable: {availability.problem}", as_json=as_json
        )

    try:
        outcome = select_route(
            catalog,
            availability,
            role=role,
            oracle_type=oracle_type,
            size_band=size_band,
            language=language,
            domain_tags=domain_tags,
            class_key=class_string,
            at_date=at_date,
            author_route_id=getattr(args, "author_route", None),
            route_id=route_id,
            instance_id=getattr(args, "instance", None),
            supervisor_route_id=getattr(args, "supervisor_route", None),
            openrouter_snapshot_path=args.openrouter_snapshot,
            rate_table_path=args.rate_table,
        )
    except RunSelectionError as exc:
        return fail(str(exc), as_json=as_json, exit_code=exc.exit_code)

    credential_path: Path | None = None
    target_channel = next(
        (
            ch
            for ch in catalog.channels.channels
            if ch.channel_id == outcome.route.channel
        ),
        None,
    )
    if (
        target_channel is not None
        and target_channel.instances
        and outcome.channel_instance is not None
    ):
        matching_instance = next(
            (
                inst
                for inst in target_channel.effective_instances()
                if inst.instance_id == outcome.channel_instance
            ),
            None,
        )
        if matching_instance is None:
            return fail(
                f"credential staging failed: channel instance "
                f"{outcome.channel_instance!r} not found",
                as_json=as_json,
                exit_code=3,
            )
        try:
            credential_path = credentials.resolve_credential_path(
                matching_instance.credential_ref
            )
            credentials.stage_harness_home(
                outcome.route.harness,
                credential_path,
                provider_key=outcome.route.channel,
            )
        except credentials.CredentialStagingError as exc:
            return fail(
                f"credential staging failed: {exc}",
                as_json=as_json,
                exit_code=3,
            )

    # Parse and validate before the worker boundary. This is deliberately
    # after selection (so it cannot alter routing) but before any launch.
    oracle_argv = None
    if getattr(args, "oracle", None) is not None:
        try:
            oracle_argv = parse_oracle_command(args.oracle)
        except LLMRouterError as exc:
            return fail(f"oracle command invalid: {exc}", as_json=as_json)

    # P3-4 (D213 ruling 4): register this run in the live registry before
    # launch. Owned paths are normalized against --workdir, and an empty
    # owned-path set is refused because it cannot prove disjointness. The
    # check-and-register is atomic: when any live record owns an intersecting
    # path the run is refused here, launches nothing, and appends nothing.
    try:
        owned_paths = normalize_owned_paths(
            list(getattr(args, "owned_paths", None) or []),
            workdir=args.workdir,
        )
    except RunRegistryError as exc:
        return fail(f"owned paths invalid: {exc}", as_json=as_json)

    packet_id = packet_id_for_text(prompt)
    try:
        attempts = read_attempts(resolve_attempts_path())
    except FileNotFoundError:
        attempts = []
    except (AttemptLedgerError, OSError, UnicodeError) as exc:
        return fail(f"attempt ledger cannot be read: {exc}", as_json=as_json)
    refusal = unchanged_redispatch_refusal(
        attempts,
        packet_id,
        outcome.route.route_id,
        args.timeout,
        parent,
    )
    if refusal is not None:
        return fail(refusal, as_json=as_json, kind="unchanged_redispatch")

    try:
        registration = register_run(
            route_id=outcome.route.route_id,
            packet_id=packet_id,
            owned_paths=owned_paths,
            workdir=args.workdir,
        )
    except RunRegistryError as exc:
        return fail(f"run registry refused: {exc}", as_json=as_json)

    def _execute_registered() -> int:
        """Dispatch, record, and print — every exit deregisters the run."""
        try:
            raw_prog = getattr(args, "progress_minutes", 20.0)
            if credential_path is not None:
                with credentials.stage_harness_home(
                    outcome.route.harness,
                    credential_path,
                    provider_key=outcome.route.channel,
                ) as extra_env:
                    dispatch = dispatch_route(
                        outcome.route,
                        prompt,
                        workdir=args.workdir,
                        timeout_seconds=args.timeout,
                        stall_minutes=getattr(args, "stall_minutes", 10.0),
                        progress_minutes=(
                            raw_prog if raw_prog and raw_prog > 0 else None
                        ),
                        stall_action=getattr(args, "stall_action", "kill"),
                        watch_dirs=owned_paths,
                        extra_env=extra_env,
                    )
            else:
                dispatch = dispatch_route(
                    outcome.route,
                    prompt,
                    workdir=args.workdir,
                    timeout_seconds=args.timeout,
                    stall_minutes=getattr(args, "stall_minutes", 10.0),
                    progress_minutes=raw_prog if raw_prog and raw_prog > 0 else None,
                    stall_action=getattr(args, "stall_action", "kill"),
                    watch_dirs=owned_paths,
                )
        except (LLMRouterError, credentials.CredentialStagingError) as exc:
            return fail(f"dispatch failed: {exc}", as_json=as_json)

        oracle = None
        oracle_setup_error: str | None = None
        if oracle_argv is not None:
            try:
                oracle = run_oracle(
                    oracle_argv,
                    workdir=args.workdir,
                    timeout_seconds=oracle_timeout_seconds(
                        args.timeout, dispatch.duration_seconds
                    ),
                )
            except RunDispatchError as exc:
                # Astra final-review finding 6: the worker has completed, so
                # a governed oracle setup failure must never drop it. The
                # failure is recorded as failed oracle evidence
                # (launch/setup error, no exit code) and exactly one
                # schema-valid attempt preserving both the worker evidence
                # and the oracle failure is appended below; the governed
                # failure is then returned (exit 3).
                oracle_setup_error = str(exc)
                oracle = OracleOutcome(
                    argv=tuple(oracle_argv),
                    exit_code=None,
                    stdout="",
                    stderr="",
                    duration_seconds=0.0,
                    timed_out=False,
                    error=str(exc),
                )

        class_record = {
            "class_key": class_string,
            "role": class_role,
            "oracle_type": oracle_type,
            "domain_tags": list(domain_tags),
            "size_band": size_band,
            "language": language,
        }
        try:
            record = build_attempt_record(
                outcome,
                dispatch,
                oracle,
                packet_id=packet_id,
                class_record=class_record,
                availability=availability,
                at_date=at_date,
                oracle_cmd=getattr(args, "oracle", None),
                parent_attempt_id=parent,
                escalation_reason=escalation_reason,
                supervisor_route=outcome.supervisor_route,
                attempt_id=getattr(args, "attempt_id", None),
                class_derivation=class_derivation,
            )
        except (LLMRouterError, OSError, TypeError, ValueError) as exc:
            return fail(f"attempt record could not be built: {exc}", as_json=as_json)

        # append_attempt performs the one schema validation immediately
        # before its one O_APPEND write. There is no pre-write repair or
        # retry path.
        try:
            append_attempt(record)
        except (LLMRouterError, OSError, TypeError, ValueError) as exc:
            return fail(f"attempt record could not be appended: {exc}", as_json=as_json)

        if as_json:
            # One compact JSON object is both the command result and the
            # exact object encoded by append_attempt (the ledger adds only
            # its newline). This holds on the governed oracle-setup-failure
            # path too: the result is the persisted record; the failure is
            # reported on stderr. The bounded serializer keeps every huge
            # token counter an exact JSON integer, byte-identical to the
            # appended ledger line.
            print(dump_json(record))
        else:
            for line in run_summary_lines(outcome, dispatch, oracle, record=record):
                print(line)
        if oracle_setup_error is not None:
            # The one attempt record is already persisted; return the governed
            # failure for the oracle that could not run (never a fake success).
            print(f"run: oracle failed: {oracle_setup_error}", file=sys.stderr)
            return 3
        if no_work_evidence(dispatch, oracle):
            # The one truthful, schema-valid attempt is already persisted and
            # printed above. A clean exit with no oracle, no owned-path change,
            # and no result/answer in stdout is a governed ``platform_env``
            # failure, never a successful command, so the caller cannot mistake
            # it for work and re-dispatch the unchanged packet/route.
            print(
                "run: no effective work: exit 0 with no oracle, no owned-path "
                "change, and no result/answer in stdout; recorded "
                "failure_class=platform_env",
                file=sys.stderr,
            )
            return 3
        return dispatch.exit_code

    # Deregistration covers every boundary after registration — normal
    # return, governed failure exit, exception, ceiling timeout, and
    # interrupt — and is best-effort: a cleanup failure is a stderr warning
    # that never masks the attempt record's truth or the real exit code.
    try:
        return _execute_registered()
    finally:
        cleanup_note = deregister_run(registration)
        if cleanup_note is not None:
            print(f"run: registry cleanup warning: {cleanup_note}", file=sys.stderr)


# ---------------------------------------------------------------------------
# census (P3-4): live-run registry listing and stale cleanup (D213 ruling 4)
# ---------------------------------------------------------------------------


def _run_census(args: argparse.Namespace) -> int:
    """Run ``census``: list live runs, clean stale registry records.

    Contract (D213 ruling 4,
    ``docs/staffing/phase3-contracts.md`` §D213 rulings): ``lee-llm-router
    census [--json]`` lists every genuinely live run registered by ``run``
    (pid, identity, route, packet id, owned paths, start) under the per-host
    state directory, and cleans stale pid records with a note. A record is
    stale when its pid no longer exists, or when the pid exists but its
    current Linux start time differs from the recorded one (pid reuse);
    records whose staleness cannot be proven are conservatively kept. The
    pass is race-safe: cleanup holds the same exclusive registry lock as
    registration and removes only evaluated stale records by their unique
    registry id, so it can never delete a fresh replacement record for a
    reused pid. An unreadable or shape-invalid record fails closed (exit 3)
    rather than being silently skipped.
    """
    import json

    from lee_llm_router.staffing.census import (
        RunRegistryError,
        census_record_json,
        census_registry,
    )

    try:
        result = census_registry()
    except (RunRegistryError, OSError) as exc:
        print(f"census: {exc}", file=sys.stderr)
        return 3

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "live": [census_record_json(record) for record in result.live],
                    "cleaned": [
                        {
                            "pid": entry.record.pid,
                            "reason": entry.reason,
                            "registry_path": (
                                str(entry.record.path)
                                if entry.record.path is not None
                                else None
                            ),
                        }
                        for entry in result.cleaned
                    ],
                },
                indent=2,
            )
        )
        return 0

    print(
        f"census: {len(result.live)} live run(s), "
        f"{len(result.cleaned)} stale record(s) cleaned"
    )
    for record in result.live:
        identity = record.identity
        identity_text = (
            "identity unavailable"
            if identity is None
            else (
                f"{identity.kind}"
                + (
                    f" start={identity.start_time}"
                    if identity.start_time is not None
                    else ""
                )
            )
        )
        print(
            f"  pid {record.pid}  started {record.started_at}  "
            f"route {record.route_id}  ({identity_text})"
        )
        print(f"    packet {record.packet_id}")
        for path in record.owned_paths:
            print(f"    owned: {path}")
    for entry in result.cleaned:
        path_text = str(entry.record.path) if entry.record.path else "-"
        print(
            f"  cleaned stale record pid {entry.record.pid} "
            f"({entry.reason}): {path_text}"
        )
    return 0


def _run_staff(args: argparse.Namespace) -> int:
    """Run ``staff``: one deterministic staffing result; never dispatches.

    D211 ruling 5 CLI contract: ``lee-llm-router staff --role R --class C
    [--mode auto|crew NAME|bind ROUTE] [--author-route ID]
    [--supervisor-route ID] [--authorized-by ID] [--reason TEXT]
    [--at YYYY-MM-DD] [--json]``. The committed staffing catalog and the
    current availability snapshot are loaded with the accepted helpers and
    every decision is made by :func:`lee_llm_router.staffing.staff.staff` —
    the CLI only parses the mode grammar and forwards arguments. Text output
    is always the compact P2-4 block; ``--json`` emits the matching
    structured payload. A service refusal prints one concise stderr line and
    exits 3; success exits 0. Nothing is dispatched and no provider is
    prompted. A successful ``bind`` appends exactly one event through the
    normal event path (``$LEE_LLM_ROUTER_EVENTS_FILE`` or the per-host
    default), keeping its authorization boundary intact. Phase 3 additionally
    accepts ``staff --from-packet FILE`` for auto mode. Packet derivation is a
    class-metadata boundary only: the effective class is passed through that
    same staffing service, and no class field maps to a model or route (D206).
    """
    from datetime import date
    from pathlib import Path

    from lee_llm_router.availability import load_availability
    from lee_llm_router.staffing import StaffingCatalogError, load_staffing_catalog
    from lee_llm_router.staffing.derive_class import PacketClassError, derive_class
    from lee_llm_router.staffing.json_int import dump_json
    from lee_llm_router.staffing.ledger import resolve_attempts_path
    from lee_llm_router.staffing.staff import (
        MODE_AUTO,
        MODE_BIND,
        MODE_CREW,
        StaffServiceError,
        render_staff_json,
        render_staff_text,
        staff,
    )

    def fail(message: str) -> int:
        print(f"staff: {message}", file=sys.stderr)
        return 3

    words = args.mode
    if words is None or (len(words) == 1 and words[0] == MODE_AUTO):
        mode, crew_id, bind_route = MODE_AUTO, None, None
    elif len(words) == 2 and words[0] == MODE_CREW and words[1]:
        mode, crew_id, bind_route = MODE_CREW, words[1], None
    elif len(words) == 2 and words[0] == MODE_BIND and words[1]:
        mode, crew_id, bind_route = MODE_BIND, None, words[1]
    else:
        return fail(
            "--mode: use 'auto', 'crew NAME', or 'bind ROUTE_ID', got "
            + " ".join(words)
        )

    catalog_dir = (
        Path(args.catalog_dir)
        if args.catalog_dir is not None
        else _default_catalog_dir()
    )
    try:
        catalog = load_staffing_catalog(catalog_dir)
    except StaffingCatalogError as exc:
        return fail(f"catalog invalid: {exc}")
    except Exception as exc:  # no traceback on unexpected catalog problems
        return fail(f"catalog invalid: {exc}")

    availability = load_availability(args.availability_file)
    if args.availability_file is not None and availability.problem is not None:
        return fail(f"availability snapshot unusable: {availability.problem}")

    derived = None
    from_packet = getattr(args, "from_packet", None)
    packet_override_supplied = any(
        getattr(args, name, None) is not None
        for name in ("oracle_type", "domain_tags", "size_band", "language", "override")
    ) or bool(getattr(args, "domain_tag", None))
    if from_packet is None and packet_override_supplied:
        return fail("packet class overrides require --from-packet FILE")
    if from_packet is not None:
        if mode != MODE_AUTO:
            return fail("--from-packet is only valid with the auto mode")
        overrides: dict[str, object] = {}
        if args.class_string is not None:
            overrides["class_key"] = args.class_string
        if args.role is not None:
            overrides["role"] = args.role
        if getattr(args, "oracle_type", None) is not None:
            overrides["oracle_type"] = args.oracle_type
        if getattr(args, "size_band", None) is not None:
            overrides["size_band"] = args.size_band
        if getattr(args, "language", None) is not None:
            overrides["language"] = args.language
        if getattr(args, "domain_tags", None) is not None:
            overrides["domain_tags"] = args.domain_tags
        domain_tag_values = getattr(args, "domain_tag", None) or []
        if domain_tag_values:
            if "domain_tags" in overrides:
                return fail("use only one of --domain-tags or --domain-tag")
            overrides["domain_tags"] = domain_tag_values
        for raw_override in getattr(args, "override", None) or []:
            if "=" not in raw_override:
                return fail(
                    "--override must use FIELD=VALUE for role, oracle_type, "
                    "domain_tags, size_band, or language"
                )
            field, value = raw_override.split("=", 1)
            if not field.strip() or not value.strip():
                return fail("--override must use a non-empty FIELD=VALUE")
            overrides[field.strip()] = value.strip()
        try:
            derived = derive_class(
                from_packet,
                classes_path=catalog_dir / "classes.yaml",
                overrides=overrides,
            )
        except PacketClassError as exc:
            return fail(f"packet class invalid: {exc}")

    # The service owns the strict calendar-date boundary; an omitted --at
    # falls back to today exactly like ``catalog explain`` and ``run``.
    at_date: str | date = args.at if args.at is not None else date.today()

    arguments: dict[str, object] = {
        "mode": mode,
        "role": derived.role if derived is not None else args.role,
        "class_key": derived.class_key if derived is not None else args.class_string,
        "at_date": at_date,
    }
    if mode == MODE_AUTO:
        # Missing ledger files read as no evidence, exactly like the service
        # contract; the file environment variable stays the test seam.
        arguments["attempts_path"] = resolve_attempts_path()
        if args.author_route is not None:
            arguments["author_route_id"] = args.author_route
        if args.supervisor_route is not None:
            arguments["supervisor_route_id"] = args.supervisor_route
    elif mode == MODE_CREW:
        arguments["crew_id"] = crew_id
    else:
        arguments["bind_route"] = bind_route
        arguments["authorized_by"] = args.authorized_by
        arguments["reason"] = args.reason

    try:
        result = staff(catalog, availability, **arguments)
    except StaffServiceError as exc:
        return fail(str(exc))
    except Exception as exc:  # no traceback on unexpected service problems
        return fail(f"staffing service failed: {exc}")

    if derived is not None:
        derivation = derived.as_dict()
        payload = render_staff_json(result)
        payload["class_derivation"] = derivation
        payload["overrides"] = derivation["overrides"]
        if args.json:
            print(dump_json(payload))
        else:
            text = render_staff_text(result)
            text += f"\nClass derived from packet: {derived.class_key}"
            if derivation["overrides"]:
                text += "\nOverrides: " + ", ".join(
                    f"{field}={value}"
                    for field, value in derivation["overrides"].items()
                )
            print(text)
    elif args.json:
        print(dump_json(render_staff_json(result)))
    else:
        print(render_staff_text(result))
    return 0


# ---------------------------------------------------------------------------
# price (P4-1): list and marginal USD price for route and token counts
# ---------------------------------------------------------------------------


def _run_price(args: argparse.Namespace) -> int:
    """Run ``price``: compute total list and marginal USD for route and token counts."""
    from datetime import date
    from pathlib import Path

    from lee_llm_router.availability import load_availability
    from lee_llm_router.staffing import StaffingCatalogError, load_staffing_catalog
    from lee_llm_router.staffing.json_int import dump_json
    from lee_llm_router.staffing.price import PriceError, compute_price

    def fail(message: str) -> int:
        print(f"price: {message}", file=sys.stderr)
        return 3

    if not args.route:
        return fail("--route is required")
    for name, val in (("input", args.input), ("output", args.output)):
        if val is None:
            return fail(f"--{name} is required")

    counts: dict[str, int] = {}
    for name, raw in (
        ("input", args.input),
        ("output", args.output),
        ("cached", args.cached or "0"),
    ):
        try:
            n = int(raw)
        except (ValueError, TypeError):
            return fail(f"--{name} must be an integer, got {raw!r}")
        if n < 0:
            return fail(f"--{name} must be non-negative, got {n}")
        counts[name] = n

    if args.at is not None:
        try:
            at_date = date.fromisoformat(args.at)
        except ValueError:
            return fail(f"--at: not an ISO date (YYYY-MM-DD): {args.at!r}")
    else:
        at_date = date.today()

    catalog_dir = (
        Path(args.catalog_dir)
        if args.catalog_dir is not None
        else _default_catalog_dir()
    )
    try:
        catalog = load_staffing_catalog(catalog_dir)
    except StaffingCatalogError as exc:
        return fail(f"catalog invalid: {exc}")
    except Exception as exc:
        return fail(f"catalog invalid: {exc}")

    availability = load_availability(args.availability_file)
    if args.availability_file is not None and availability.problem is not None:
        return fail(f"availability snapshot unusable: {availability.problem}")

    try:
        result = compute_price(
            route_id=args.route,
            input_tokens=counts["input"],
            output_tokens=counts["output"],
            cached_tokens=counts["cached"],
            catalog=catalog,
            availability=availability,
            at_date=at_date,
        )
    except PriceError as exc:
        return fail(str(exc))
    except Exception as exc:
        return fail(f"price computation failed: {exc}")

    if args.json:
        print(dump_json(result.as_dict()))
    else:
        print(result.render_text())
    return 0


# ---------------------------------------------------------------------------
# route show (P4-10): disclose one route's full catalog record plus proof
# ---------------------------------------------------------------------------


def _run_route_show(args: argparse.Namespace) -> int:
    """Run ``route show``: disclose one route's full catalog record.

    Unlike ``catalog explain`` (which deliberately withholds model/effort/
    harness/status because "the route id is the only route label" for
    *selection*), this command's whole purpose is disclosure for a caller
    that must actually dispatch the route (chief-answers-p4-2.md item 1):
    it prints every field the catalog's ``routes.yaml`` records for the
    route id, plus its D211 ``proof_status`` from the attempts ledger. It
    performs no selection, ranking, or eligibility evaluation.
    """
    from pathlib import Path

    from lee_llm_router.staffing import StaffingCatalogError, load_staffing_catalog
    from lee_llm_router.staffing.json_int import dump_json
    from lee_llm_router.staffing.proof import ProofStatus, proof_status_ledger

    def fail(message: str) -> int:
        print(f"route show: {message}", file=sys.stderr)
        return 3

    catalog_dir = (
        Path(args.catalog_dir)
        if args.catalog_dir is not None
        else _default_catalog_dir()
    )
    try:
        catalog = load_staffing_catalog(catalog_dir)
    except StaffingCatalogError as exc:
        return fail(f"catalog invalid: {exc}")
    except Exception as exc:  # no traceback on unexpected catalog problems
        return fail(f"catalog invalid: {exc}")

    route = next(
        (r for r in catalog.routes.routes if r.route_id == args.route_id), None
    )
    if route is None:
        return fail(f"unknown route id: {args.route_id!r}")

    try:
        proof = proof_status_ledger().get(route.route_id, ProofStatus.UNPROVEN)
    except Exception as exc:  # ledger corruption never blocks disclosure
        return fail(f"attempts ledger unusable: {exc}")
    proof_value = proof.value if isinstance(proof, ProofStatus) else str(proof)

    record = {
        "route_id": route.route_id,
        "model": route.model,
        "effort": route.effort,
        "harness": route.harness,
        "channel": route.channel,
        "dispatch_template": route.dispatch_template,
        "usage_capture": route.usage_capture,
        "status": route.status,
        "status_reason": route.status_reason,
        "proof_status": proof_value,
    }

    if args.json:
        print(dump_json(record))
    else:
        print(f"route show — {route.route_id}")
        for key in (
            "model",
            "effort",
            "harness",
            "channel",
            "dispatch_template",
            "usage_capture",
            "status",
            "status_reason",
            "proof_status",
        ):
            print(f"{key}: {record[key]}")
    return 0


def _run_route_no_subcommand(_args: argparse.Namespace) -> int:
    print("route: subcommand required (show)", file=sys.stderr)
    return 3


# ---------------------------------------------------------------------------
# classify-failure (P3-1): deterministic D213 ruling 3 failure classification
# ---------------------------------------------------------------------------


def _run_classify_failure(args: argparse.Namespace) -> int:
    """Run ``classify-failure``: classify one attempt's evidence (D213 ruling 3).

    Contract: ``lee-llm-router classify-failure [--record FILE] [--json]``
    plus explicit evidence flags (``--exit-code``, ``--timed-out``,
    ``--stdout``, ``--stderr``, ``--error``, ``--oracle-exit-code``,
    ``--oracle-timed-out``, ``--oracle-error``, ``--oracle-cmd``,
    ``--accounting-status``, ``--usage-basis``, ``--unaccounted-spend``,
    ``--verdict``, ``--judgment``, ``--review-verdict``). The classification
    itself is made entirely by
    :func:`lee_llm_router.staffing.failure.classify_failure` — the CLI only
    parses arguments and forwards them; it never infers a class itself.
    Deterministic classes are ``platform_timeout``, ``platform_env``,
    ``unaccounted_spend``, ``oracle_failed``, and ``unknown``.
    ``spec_rejected``/``capability_rejected`` enter only through an explicit
    ``--judgment`` that names a review verdict (``--review-verdict`` or a
    record field); they are never inferred from text.

    Output: the failure class string, or ``none`` when no failure occurred
    (text) / ``{"failure_class": null}`` (JSON). A classification refusal —
    unreadable record, or an invalid judgment (unknown value, missing or
    contradictory review verdict) — prints one concise stderr line and exits
    3, matching the ``staff``/``run`` refusal convention. A successful
    classification exits 0 whether or not a failure was found.
    """
    import json
    from pathlib import Path

    from lee_llm_router.staffing.failure import (
        FailureClassificationError,
        classify_failure,
    )

    def fail(message: str) -> int:
        print(f"classify-failure: {message}", file=sys.stderr)
        return 3

    record: dict[str, Any] | None = None
    if args.record is not None:
        record_path = Path(args.record).expanduser()
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            return fail(f"record file cannot be read: {exc}")
        except json.JSONDecodeError as exc:
            return fail(f"record file is not valid JSON: {record_path}: {exc}")
        if not isinstance(record, dict):
            return fail(f"record file must contain one JSON object: {record_path}")

    try:
        failure_class = classify_failure(
            record,
            exit_code=args.exit_code,
            timed_out=args.timed_out,
            stdout=args.stdout or "",
            stderr=args.stderr or "",
            error=args.error,
            oracle_exit_code=args.oracle_exit_code,
            oracle_timed_out=args.oracle_timed_out,
            oracle_error=args.oracle_error,
            oracle_cmd=args.oracle_cmd,
            accounting_status=args.accounting_status,
            usage_basis=args.usage_basis,
            metered_route=args.metered_route,
            unaccounted_spend=args.unaccounted_spend,
            verdict=args.verdict,
            judgment=args.judgment,
            review_verdict=args.review_verdict,
        )
    except FailureClassificationError as exc:
        return fail(str(exc))
    except (TypeError, ValueError) as exc:
        return fail(f"invalid classification input: {exc}")

    if args.json:
        print(json.dumps({"failure_class": failure_class}, separators=(",", ":")))
    elif failure_class is None:
        print("none")
    else:
        print(failure_class)
    return 0


# ---------------------------------------------------------------------------
# next-action (P3-2): the /supervise loop step over the pure Phase 2 mapping
# ---------------------------------------------------------------------------


def _run_next_action(args: argparse.Namespace) -> int:
    """Run ``next-action``: map one classified failure to its next action.

    Contract (P3-2, D213 rulings 1 and 3,
    ``docs/staffing/phase3-contracts.md`` §D213 rulings): ``lee-llm-router
    next-action --input FILE [--repair-count N]`` consumes exactly the JSON
    object printed by ``classify-failure --json`` — ``{"failure_class":
    <string or null>}`` — from ``FILE``. ``--input -`` is the equally
    deterministic stdin contract: the same single JSON object read from
    standard input. The decision itself is made entirely by the accepted
    Phase 2 pure mapping
    :func:`lee_llm_router.staffing.next_action.next_action` — this CLI
    calls it, never duplicates or alters it, and it never dispatches,
    launches a run, registers a live run, or calls a provider.

    Repair-count boundary: ``--repair-count`` is the attempt's completed
    same-route repair count — ``0`` on the first capability rejection
    (nothing repaired yet), ``1`` after one same-route repair has already
    failed. It is forwarded to the pure mapping as its 1-based capability
    attempt number (``repair_count + 1``), preserving the pure mapping's
    exact boundary: count ``0`` → ``repair_same_route`` (one same-route
    capability repair precedes escalation, D213 execution invariants),
    count ``1`` or more → ``escalate``. A negative count is refused and a
    noninteger count is rejected by argument parsing before the mapping
    runs; an omitted count for ``capability_rejected`` fails closed to
    ``supervisor_judgment`` exactly as the pure mapping does, and the
    count is ignored for every other class.

    ``oracle_failed`` and ``unknown``: D213 ruling 3 classifies them
    deterministically, and the pure mapping — which owns the class→action
    decision — maps both to ``supervisor_judgment``. This CLI preserves
    that handling verbatim; no dispatch or policy is invented here.

    Output: one compact structured JSON object ``{"failure_class": ...,
    "repair_count": ..., "next_action": ...}`` suitable for the
    /supervise loop. Every mapped outcome — including
    ``supervisor_judgment`` — exits 0. Refusals (unreadable input,
    malformed JSON, a non-object payload, unexpected keys, a missing or
    non-string ``failure_class``, or a negative repair count) print one
    concise stderr line prefixed ``next-action:`` and exit 3.
    """
    import json
    from pathlib import Path

    from lee_llm_router.staffing.next_action import next_action

    def fail(message: str) -> int:
        print(f"next-action: {message}", file=sys.stderr)
        return 3

    if args.repair_count is not None and args.repair_count < 0:
        return fail(
            f"--repair-count must be a nonnegative integer, " f"got {args.repair_count}"
        )

    if args.input == "-":
        source = "<stdin>"
        raw = sys.stdin.read()
    else:
        input_path = Path(args.input).expanduser()
        source = str(input_path)
        try:
            raw = input_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return fail(f"input file cannot be read: {exc}")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return fail(f"input is not valid JSON: {source}: {exc}")
    if not isinstance(payload, dict):
        return fail(
            f"input must be one JSON object (the 'classify-failure --json' "
            f"output): {source}"
        )
    unexpected = sorted(set(payload) - {"failure_class"})
    if unexpected:
        return fail(
            f"input carries unexpected key(s) {unexpected}; only the "
            f"'classify-failure --json' output object is accepted"
        )
    if "failure_class" not in payload:
        return fail(f'input is missing "failure_class": {source}')
    failure_class = payload["failure_class"]
    if failure_class is not None and not isinstance(failure_class, str):
        return fail('"failure_class" must be a string or null')

    # The pure Phase 2 mapping owns the decision; the CLI only translates
    # its repair count into the mapping's 1-based capability attempt
    # number and forwards everything else unchanged.
    capability_attempt_number = (
        None if args.repair_count is None else args.repair_count + 1
    )
    action = next_action(failure_class, capability_attempt_number)

    print(
        json.dumps(
            {
                "failure_class": failure_class,
                "repair_count": args.repair_count,
                "next_action": action,
            },
            separators=(",", ":"),
        )
    )
    return 0


def main(argv: list[str] | None = None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="lee-llm-router",
        description="Lee LLM Router CLI tools",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.required = True

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Validate config file and environment; exit 0 if healthy",
    )
    doctor_parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="Path to config YAML (optional with --crews/--availability)",
    )
    doctor_parser.add_argument(
        "--role",
        metavar="ROLE",
        help="Role to dry-run (default: config.default_role)",
    )
    doctor_parser.add_argument(
        "--crews",
        action="store_true",
        help="Also resolve every worker referenced by every crew",
    )
    doctor_parser.add_argument(
        "--crews-file",
        metavar="PATH",
        default=None,
        help="Path to the crews YAML (default: the Auto-Orch crews file)",
    )
    doctor_parser.add_argument(
        "--availability",
        action="store_true",
        help="Also report the age and size of the availability snapshot",
    )
    doctor_parser.add_argument(
        "--availability-file",
        metavar="PATH",
        default=None,
        help=(
            "Path to the availability snapshot (default: "
            "~/.local/state/lee-llm-router/availability/<host>.json)"
        ),
    )
    doctor_parser.add_argument(
        "--catalog",
        action="store_true",
        help="Also validate and load the six staffing catalog documents",
    )
    doctor_parser.add_argument(
        "--catalog-dir",
        metavar="PATH",
        default=None,
        help=(
            "Directory holding routes.yaml, channels.yaml, terms.yaml, "
            "policy.yaml, classes.yaml, crews.yaml "
            "(default: the repo config/staffing directory)"
        ),
    )
    doctor_parser.set_defaults(func=_run_doctor)

    crews_parser = subparsers.add_parser(
        "crews",
        help="Inspect the Auto-Orch crews file",
    )
    crews_sub = crews_parser.add_subparsers(dest="crews_command", metavar="SUBCOMMAND")
    crews_sub.required = True
    crews_list_parser = crews_sub.add_parser(
        "list",
        help="List crews, their stage workers, and their governed routes",
    )
    crews_list_parser.add_argument(
        "--crews-file",
        metavar="PATH",
        default=None,
        help="Path to the crews YAML (default: the Auto-Orch crews file)",
    )
    crews_list_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON array instead of plain text",
    )
    crews_list_parser.set_defaults(func=_run_crews_list)

    crews_page_parser = crews_sub.add_parser(
        "page",
        help="Generate a self-contained HTML crew staffing page",
    )
    crews_page_parser.add_argument(
        "--out",
        required=True,
        metavar="PATH",
        help="Output HTML path; its parent directory must already exist",
    )
    crews_page_parser.add_argument(
        "--crews-file",
        metavar="PATH",
        default=None,
        help="Path to the crews YAML (default: the Auto-Orch crews file)",
    )
    crews_page_parser.add_argument(
        "--availability-file",
        metavar="PATH",
        default=None,
        help="Path to the availability snapshot (default: per-host default)",
    )
    crews_page_parser.add_argument(
        "--benchmark-file",
        metavar="PATH",
        default=None,
        help=(
            "Path to staffing evidence JSON (default: latest "
            "~/projects/ai-workforce-benchmark/exports/staffing-evidence-*.json)"
        ),
    )
    crews_page_parser.add_argument(
        "--events-file",
        metavar="PATH",
        default=None,
        help="Validate this JSONL event ledger without reading the default ledger",
    )
    crews_page_parser.set_defaults(func=_run_crews_page)

    shims_parser = subparsers.add_parser(
        "shims",
        help="Manage harness command shims (install, diff)",
    )
    shims_sub = shims_parser.add_subparsers(dest="shims_command", metavar="SUBCOMMAND")
    shims_sub.required = False
    shims_parser.set_defaults(func=_run_shims_no_subcommand)

    shims_install_parser = shims_sub.add_parser(
        "install",
        help="Install harness shims (--dry-run or --apply)",
    )
    shims_install_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions and rendered shim contents without writing",
    )
    shims_install_parser.add_argument(
        "--command",
        dest="shim_command",
        choices=("crew", "supervise"),
        default="crew",
        metavar="COMMAND",
        help="Managed command to install (default: crew)",
    )
    shims_install_parser.add_argument(
        "--apply",
        action="store_true",
        help="Write shims to their target paths",
    )
    shims_install_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite files whose marker/hash does not match",
    )
    shims_install_parser.add_argument(
        "--harness",
        action="append",
        default=None,
        metavar="TAG",
        help="Limit to named harness (claude-code, codex, omp, opencode; repeatable)",
    )
    shims_install_parser.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="Project directory for omp shim (default: cwd)",
    )
    shims_install_parser.set_defaults(func=_run_shims_install)

    shims_diff_parser = shims_sub.add_parser(
        "diff",
        help="Show diff between installed shims and rendered templates",
    )
    shims_diff_parser.add_argument(
        "--command",
        dest="shim_command",
        choices=("crew", "supervise"),
        default="crew",
        metavar="COMMAND",
        help="Managed command to compare (default: crew)",
    )
    shims_diff_parser.add_argument(
        "--harness",
        action="append",
        default=None,
        metavar="TAG",
        help="Limit to named harness (claude-code, codex, omp, opencode; repeatable)",
    )
    shims_diff_parser.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="Project directory for omp shim (default: cwd)",
    )
    shims_diff_parser.set_defaults(func=_run_shims_diff)

    catalog_parser = subparsers.add_parser(
        "catalog",
        help="Inspect the staffing catalog",
    )
    catalog_sub = catalog_parser.add_subparsers(
        dest="catalog_command", metavar="SUBCOMMAND"
    )
    catalog_sub.required = False
    catalog_parser.set_defaults(func=_run_catalog_no_subcommand)

    catalog_explain_parser = catalog_sub.add_parser(
        "explain",
        help="Report every route's eligibility for a class, ordered by marginal price",
    )
    catalog_explain_parser.add_argument(
        "--role",
        required=True,
        metavar="ROLE",
        help="Class role (impl, plan, review, judge, prose)",
    )
    catalog_explain_parser.add_argument(
        "--class",
        required=True,
        dest="class_string",
        metavar="CLASS",
        help=(
            "Canonical five-segment class string "
            "role/oracle_type/domain_tags/size_band/language "
            "(e.g. impl/deterministic/none/s/python)"
        ),
    )
    catalog_explain_parser.add_argument(
        "--at",
        default=None,
        metavar="DATE",
        help="ISO date (YYYY-MM-DD) for dated terms (default: today)",
    )
    catalog_explain_parser.add_argument(
        "--availability-file",
        metavar="PATH",
        default=None,
        help="Path to the availability snapshot (default: per-host default)",
    )
    catalog_explain_parser.add_argument(
        "--catalog-dir",
        metavar="PATH",
        default=None,
        help=(
            "Directory holding the six staffing catalog YAML documents "
            "(default: the repo config/staffing directory)"
        ),
    )
    catalog_explain_parser.add_argument(
        "--author-route",
        metavar="ROUTE_ID",
        default=None,
        help=(
            "Author route id for review/judge independence: exclude that "
            "route and every candidate whose model family equals the "
            "author's (reason 'independence'). Without it, independence is "
            "not evaluated (absent-input disclosure only). Unknown ids fail "
            "closed (exit 3)."
        ),
    )
    catalog_explain_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON object instead of a plain-text table",
    )
    catalog_explain_parser.set_defaults(func=_run_catalog_explain)

    route_parser = subparsers.add_parser(
        "route",
        help="Inspect one route's full catalog record",
    )
    route_sub = route_parser.add_subparsers(dest="route_command", metavar="SUBCOMMAND")
    route_sub.required = False
    route_parser.set_defaults(func=_run_route_no_subcommand)

    route_show_parser = route_sub.add_parser(
        "show",
        help="Disclose one route's model/effort/harness/channel/status/proof_status",
    )
    route_show_parser.add_argument(
        "route_id",
        metavar="ROUTE_ID",
        help="Route id to disclose",
    )
    route_show_parser.add_argument(
        "--catalog-dir",
        metavar="PATH",
        default=None,
        help=(
            "Directory holding the six staffing catalog YAML documents "
            "(default: the repo config/staffing directory)"
        ),
    )
    route_show_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON object instead of plain text",
    )
    route_show_parser.set_defaults(func=_run_route_show)

    run_parser = subparsers.add_parser(
        "run",
        help="Select, dispatch, and record one eligible staffing attempt",
    )
    run_parser.add_argument(
        "--role",
        required=True,
        metavar="ROLE",
        help="Class role (impl, plan, review, judge, prose); always required",
    )
    run_parser.add_argument(
        "--class",
        required=True,
        dest="class_string",
        metavar="CLASS",
        help=(
            "Canonical five-segment class string "
            "role/oracle_type/domain_tags/size_band/language "
            "(e.g. impl/deterministic/none/s/python); always required"
        ),
    )
    run_parser.add_argument(
        "--packet",
        required=True,
        metavar="FILE",
        help="Path to the packet file whose text is dispatched verbatim",
    )
    run_parser.add_argument(
        "--owned-paths",
        action="append",
        required=True,
        dest="owned_paths",
        metavar="PATH",
        help=(
            "File or directory this run owns; repeatable, at least one is "
            "required (P3-4, D213 ruling 4). Relative paths are normalized "
            "against --workdir (or the process working directory). Before "
            "launch the run atomically registers pid, route, packet id, "
            "these normalized paths, and its start in the live per-host "
            "registry; a run whose owned paths intersect a live run's "
            "(equality or ancestor/descendant) is refused with exit 3 and "
            "launches nothing"
        ),
    )
    run_parser.add_argument(
        "--class-derivation",
        default=None,
        metavar="FILE",
        help=(
            "JSON output from staff --from-packet; its effective class must "
            "match --class and its override records are preserved on the attempt"
        ),
    )
    run_parser.add_argument(
        "--route",
        default=None,
        metavar="ROUTE_ID",
        help=(
            "Explicit route id: selection basis 'explicit'; the route must "
            "be eligible under the same role/class path as catalog explain "
            "(an excluded route exits 3 and launches nothing). Without it, "
            "the first eligible route in catalog explain marginal-price "
            "order is selected (basis 'explain_cheapest_eligible')"
        ),
    )
    run_parser.add_argument(
        "--instance",
        default=None,
        dest="instance",
        metavar="INSTANCE_ID",
        help=(
            "Explicit channel instance id: pins an instance the way --route "
            "pins a route. The instance must exist, be enabled, and be "
            "currently eligible for the selected route's channel (an unknown, "
            "disabled, or ineligible instance exits 3 and launches nothing; "
            "specifying an instance for a non-subscription channel also exits 3). "
            "Without it, run selects the first eligible instance from the "
            "route's instance_headrooms (matching staff auto's selected_instance), "
            "or None when the channel has no instance concept"
        ),
    )
    run_parser.add_argument(
        "--supervisor-route",
        default=None,
        dest="supervisor_route",
        metavar="ROUTE_ID",
        help=(
            "Optional caller attestation of its own supervisor route "
            "(P1-4 ruling 3): the id must name a known, active, currently "
            "usable catalog route identity (unknown/inactive/exhausted ids "
            "exit 3 and launch nothing), but the worker's role/class "
            "capability policy is never imposed on the attested supervisor "
            "identity. When the attested run dispatches, the oracle passes, "
            "and every evidence gate holds, verified_success is true; "
            "otherwise verified_success_reason names the exact gap"
        ),
    )
    run_parser.add_argument(
        "--author-route",
        default=None,
        dest="author_route",
        metavar="ROUTE_ID",
        help=(
            "Author route id forwarded to the same catalog explain "
            "eligibility path: for a review/judge class the author route "
            "and every candidate whose model family equals the author's are "
            "excluded (reason 'independence'); an unknown id fails closed "
            "(exit 3). Without it, independence is not evaluated"
        ),
    )
    run_parser.add_argument(
        "--oracle",
        default=None,
        metavar="CMD",
        help=(
            "Optional verification command; parsed with shlex and run once "
            "without a shell after the worker"
        ),
    )
    run_parser.add_argument(
        "--workdir",
        default=None,
        metavar="DIR",
        help="Child and oracle working directory (must exist; default: inherit)",
    )
    run_parser.add_argument(
        "--parent",
        default=None,
        metavar="ATTEMPT_ID",
        help=(
            "Parent attempt id for an escalation-linked run; must be "
            "supplied together with --escalation-reason; recorded on the attempt"
        ),
    )
    run_parser.add_argument(
        "--escalation-reason",
        default=None,
        dest="escalation_reason",
        metavar="R",
        help=(
            "Escalation reason paired with --parent; both or neither; "
            "recorded on the attempt"
        ),
    )
    run_parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="S",
        help=(
            "Wall-clock ceiling in seconds (default: the dispatch "
            "boundary's standard ceiling)"
        ),
    )
    run_parser.add_argument(
        "--stall-minutes",
        type=float,
        default=10.0,
        metavar="N",
        help=(
            "Minutes of joint silence (no output and no file changes) "
            "before stall action (default: 10)"
        ),
    )
    run_parser.add_argument(
        "--progress-minutes",
        type=float,
        default=20.0,
        metavar="N",
        help=(
            "Minutes without owned-path file activity before kill "
            "regardless of output; 0 disables (default: 20)"
        ),
    )
    run_parser.add_argument(
        "--stall-action",
        choices=["kill", "warn"],
        default="kill",
        help="Action on worker stall: kill child process or warn only (default: kill)",
    )
    run_parser.add_argument(
        "--at",
        default=None,
        metavar="DATE",
        help="ISO date (YYYY-MM-DD) for dated terms (default: today)",
    )
    run_parser.add_argument(
        "--availability-file",
        metavar="PATH",
        default=None,
        help="Path to the availability snapshot (default: per-host default)",
    )
    run_parser.add_argument(
        "--catalog-dir",
        metavar="PATH",
        default=None,
        help=(
            "Directory holding the six staffing catalog YAML documents "
            "(default: the repo config/staffing directory)"
        ),
    )
    run_parser.add_argument(
        "--openrouter-snapshot",
        metavar="PATH",
        default=None,
        help="Injectable pinned OpenRouter snapshot path (testing override)",
    )
    run_parser.add_argument(
        "--rate-table",
        metavar="PATH",
        default=None,
        help="Injectable agent-orch rate-table path (testing override)",
    )
    run_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON summary object instead of plain text",
    )
    run_parser.set_defaults(func=_run_run)

    census_parser = subparsers.add_parser(
        "census",
        help="List live runs and clean stale records in the run registry",
    )
    census_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON object with 'live' rows and 'cleaned' stale records",
    )
    census_parser.set_defaults(func=_run_census)

    staff_parser = subparsers.add_parser(
        "staff",
        help="Compute one staffing result (auto, crew, bind); never dispatches",
    )
    staff_parser.add_argument(
        "--role",
        default=None,
        metavar="ROLE",
        help=(
            "Class role (impl, plan, review, judge, prose); required by auto "
            "and bind, an explicit override with --from-packet, unused by crew"
        ),
    )
    staff_parser.add_argument(
        "--class",
        default=None,
        dest="class_string",
        metavar="CLASS",
        help=(
            "Canonical five-segment class string "
            "role/oracle_type/domain_tags/size_band/language; required by "
            "auto and bind, a full override with --from-packet, unused by crew"
        ),
    )
    staff_parser.add_argument(
        "--from-packet",
        default=None,
        metavar="FILE",
        help=(
            "Derive the class conservatively from a Markdown packet; valid "
            "with auto mode and records the packet evidence in --json output"
        ),
    )
    staff_parser.add_argument(
        "--oracle-type",
        default=None,
        choices=("deterministic", "judge", "human", "none"),
        help="Explicit packet class override",
    )
    staff_parser.add_argument(
        "--domain-tags",
        default=None,
        metavar="TAGS",
        help="Explicit packet domain-tag override (comma- or +-separated)",
    )
    staff_parser.add_argument(
        "--domain-tag",
        action="append",
        default=None,
        metavar="TAG",
        help="Add an explicit packet domain tag; repeatable",
    )
    staff_parser.add_argument(
        "--size-band",
        default=None,
        choices=("xs", "s", "m", "l"),
        help="Explicit packet class override",
    )
    staff_parser.add_argument(
        "--language",
        default=None,
        choices=(
            "python",
            "typescript",
            "shell",
            "c",
            "sql",
            "yaml-config",
            "markdown",
            "mixed",
        ),
        help="Explicit packet class override",
    )
    staff_parser.add_argument(
        "--override",
        action="append",
        default=None,
        metavar="FIELD=VALUE",
        help="Explicit packet class override; repeatable",
    )
    staff_parser.add_argument(
        "--mode",
        nargs="+",
        default=None,
        metavar="MODE",
        help=(
            "Staffing mode: 'auto' (default), 'crew NAME' for the exact "
            "saved crew block, or 'bind ROUTE_ID' for the explicit human "
            "override (requires --authorized-by and --reason; never-"
            "automatic routes bind only with --authorized-by lee)"
        ),
    )
    staff_parser.add_argument(
        "--author-route",
        default=None,
        dest="author_route",
        metavar="ROUTE_ID",
        help=(
            "Author route id forwarded to the auto eligibility path: for a "
            "review/judge class the author route and every same-family "
            "candidate are excluded (reason 'independence'); an unknown id "
            "fails closed (exit 3)"
        ),
    )
    staff_parser.add_argument(
        "--supervisor-route",
        default=None,
        dest="supervisor_route",
        metavar="ROUTE_ID",
        help="Optional supervisor route id attested to the auto mode",
    )
    staff_parser.add_argument(
        "--authorized-by",
        default=None,
        metavar="ID",
        help=(
            "Bind mode only: who authorized the explicit override; a "
            "never-automatic route binds only with 'lee'"
        ),
    )
    staff_parser.add_argument(
        "--reason",
        default=None,
        metavar="TEXT",
        help="Bind mode only: the recorded reason for the explicit override",
    )
    staff_parser.add_argument(
        "--at",
        default=None,
        metavar="DATE",
        help="ISO date (YYYY-MM-DD) for dated terms (default: today)",
    )
    staff_parser.add_argument(
        "--availability-file",
        metavar="PATH",
        default=None,
        help="Path to the availability snapshot (default: per-host default)",
    )
    staff_parser.add_argument(
        "--catalog-dir",
        metavar="PATH",
        default=None,
        help=(
            "Directory holding the six staffing catalog YAML documents "
            "(default: the repo config/staffing directory)"
        ),
    )
    staff_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the structured JSON payload instead of the compact block",
    )
    staff_parser.set_defaults(func=_run_staff)

    price_parser = subparsers.add_parser(
        "price",
        help="Compute total list and marginal USD price for a route and token counts",
    )
    price_parser.add_argument(
        "--route",
        required=True,
        metavar="ROUTE_ID",
        help="Route id to price",
    )
    price_parser.add_argument(
        "--input",
        required=True,
        metavar="N",
        help="Input token count (non-negative integer)",
    )
    price_parser.add_argument(
        "--output",
        required=True,
        metavar="N",
        help="Output token count (non-negative integer)",
    )
    price_parser.add_argument(
        "--cached",
        default=None,
        metavar="N",
        help="Cached / read input token count (non-negative integer, default 0)",
    )
    price_parser.add_argument(
        "--at",
        default=None,
        metavar="DATE",
        help="ISO date (YYYY-MM-DD) for dated terms (default: today)",
    )
    price_parser.add_argument(
        "--availability-file",
        metavar="PATH",
        default=None,
        help="Path to the availability snapshot (default: per-host default)",
    )
    price_parser.add_argument(
        "--catalog-dir",
        metavar="PATH",
        default=None,
        help=(
            "Directory holding the six staffing catalog YAML documents "
            "(default: the repo config/staffing directory)"
        ),
    )
    price_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the structured JSON payload instead of plain text",
    )
    price_parser.set_defaults(func=_run_price)

    classify_failure_parser = subparsers.add_parser(
        "classify-failure",
        help="Deterministically classify one attempt's failure evidence",
    )
    classify_failure_parser.add_argument(
        "--record",
        default=None,
        metavar="FILE",
        help=(
            "Read evidence from one JSON attempt-record file (e.g. a ledger "
            "line); explicit flags override the record's fields"
        ),
    )
    classify_failure_parser.add_argument(
        "--exit-code",
        type=int,
        default=None,
        metavar="N",
        help="Worker dispatch exit code (124 = ceiling timeout)",
    )
    classify_failure_parser.add_argument(
        "--timed-out",
        action="store_true",
        dest="timed_out",
        help="The worker dispatch hit its watchdog ceiling",
    )
    classify_failure_parser.add_argument(
        "--stdout",
        default=None,
        metavar="TEXT",
        help="Worker stdout text (platform signatures are matched against it)",
    )
    classify_failure_parser.add_argument(
        "--stderr",
        default=None,
        metavar="TEXT",
        help="Worker stderr text (platform signatures are matched against it)",
    )
    classify_failure_parser.add_argument(
        "--error",
        default=None,
        metavar="TEXT",
        help="Worker error message",
    )
    classify_failure_parser.add_argument(
        "--oracle-exit-code",
        type=int,
        default=None,
        dest="oracle_exit_code",
        metavar="N",
        help="Oracle exit code (124 = timeout)",
    )
    classify_failure_parser.add_argument(
        "--oracle-timed-out",
        action="store_true",
        dest="oracle_timed_out",
        help="The oracle hit its timeout budget",
    )
    classify_failure_parser.add_argument(
        "--oracle-error",
        default=None,
        dest="oracle_error",
        metavar="TEXT",
        help="Oracle launch or setup error",
    )
    classify_failure_parser.add_argument(
        "--oracle-cmd",
        default=None,
        dest="oracle_cmd",
        metavar="CMD",
        help="The oracle command that was run, if any",
    )
    classify_failure_parser.add_argument(
        "--accounting-status",
        default=None,
        dest="accounting_status",
        metavar="STATUS",
        help=(
            "Usage accounting status (measured, unaccounted, not_applicable); "
            "unaccounted classifies as unaccounted_spend"
        ),
    )
    classify_failure_parser.add_argument(
        "--usage-basis",
        default=None,
        dest="usage_basis",
        metavar="BASIS",
        help=(
            "Usage basis (e.g. unavailable); unavailable usage on a metered "
            "route classifies as unaccounted_spend"
        ),
    )
    classify_failure_parser.add_argument(
        "--metered-route",
        action=argparse.BooleanOptionalAction,
        default=None,
        dest="metered_route",
        help="Attest whether the route is metered when no record route is supplied",
    )
    classify_failure_parser.add_argument(
        "--unaccounted-spend",
        action="store_true",
        dest="unaccounted_spend",
        help="Unaccounted spend was directly observed",
    )
    classify_failure_parser.add_argument(
        "--verdict",
        default=None,
        metavar="TEXT",
        help="Attempt verdict (pass, fail, or unverified)",
    )
    classify_failure_parser.add_argument(
        "--judgment",
        default=None,
        metavar="CLASS",
        help=(
            "Explicit supervisor judgment: spec_rejected or "
            "capability_rejected; requires a review verdict and is never "
            "inferred from text"
        ),
    )
    classify_failure_parser.add_argument(
        "--review-verdict",
        default=None,
        dest="review_verdict",
        metavar="TEXT",
        help=(
            "Review verdict justifying --judgment (e.g. fail); a passing "
            "review verdict cannot justify a failure judgment"
        ),
    )
    classify_failure_parser.add_argument(
        "--json",
        action="store_true",
        help=(
            'Emit a compact JSON object ({"failure_class": ...}) instead of '
            "the plain class string"
        ),
    )
    classify_failure_parser.set_defaults(func=_run_classify_failure)

    next_action_parser = subparsers.add_parser(
        "next-action",
        help=(
            "Map one classify-failure JSON class to its next staff action "
            "(no dispatch)"
        ),
    )
    next_action_parser.add_argument(
        "--input",
        required=True,
        metavar="FILE",
        help=(
            "File holding exactly the JSON object printed by "
            '"classify-failure --json" ({"failure_class": ...}); '
            "'-' reads the same single JSON object from stdin"
        ),
    )
    next_action_parser.add_argument(
        "--repair-count",
        type=int,
        default=None,
        metavar="N",
        help=(
            "The attempt's nonnegative repair count (0 on the first "
            "capability rejection); forwarded to the pure next-action "
            "mapping as its 1-based capability attempt number and ignored "
            "for non-capability classes"
        ),
    )
    next_action_parser.set_defaults(func=_run_next_action)

    evidence_parser = subparsers.add_parser(
        "evidence",
        help="Inspect staffing evidence",
    )
    evidence_sub = evidence_parser.add_subparsers(
        dest="evidence_command", metavar="SUBCOMMAND"
    )
    evidence_sub.required = True
    evidence_rollup_parser = evidence_sub.add_parser(
        "rollup",
        help="Aggregate validated attempt records by route and class",
    )
    evidence_rollup_parser.set_defaults(func=_run_evidence_rollup)
    evidence_report_parser = evidence_sub.add_parser(
        "report",
        help="Per-class/per-route evidence report for one calendar month",
    )
    evidence_report_parser.add_argument(
        "--month",
        required=True,
        metavar="YYYY-MM",
        help="Calendar month to report on (e.g. 2026-09)",
    )
    evidence_report_parser.add_argument(
        "--json",
        action="store_true",
        help="Output stable compact JSON",
    )
    evidence_report_parser.add_argument(
        "--catalog-dir",
        default=None,
        metavar="PATH",
        help="Override catalog directory (default: config/staffing)",
    )
    evidence_report_parser.add_argument(
        "--availability-file",
        default=None,
        metavar="PATH",
        help="Override availability snapshot path",
    )
    evidence_report_parser.set_defaults(func=_run_evidence_report)
    evidence_import_parser = evidence_sub.add_parser(
        "import",
        help="Import benchmark or recent Agent-Orch evidence into the attempt ledger",
    )
    evidence_sources = evidence_import_parser.add_mutually_exclusive_group(
        required=True
    )
    evidence_sources.add_argument(
        "--benchmark",
        default=None,
        metavar="PATH",
        help="Path to the benchmark v6 staffing-evidence sidecar",
    )
    evidence_sources.add_argument(
        "--agent-orch",
        dest="agent_orch",
        default=None,
        metavar="PATH",
        help=(
            "Agent-Orch projects directory, *-agent-orch-runs directory, "
            "or one run.json fixture"
        ),
    )
    evidence_import_parser.add_argument(
        "--csv",
        default=None,
        metavar="PATH",
        help="Override source CSV path (must match the sidecar SHA-256)",
    )
    evidence_import_parser.set_defaults(func=_run_evidence_import)

    evidence_ladders_parser = evidence_sub.add_parser(
        "ladders",
        help="Derive cheapest-first escalation ladders from the attempt ledger",
    )
    evidence_ladders_parser.add_argument(
        "--derive",
        action="store_true",
        required=True,
        help="Derive ladders, write output, and diff against hand-authored sources",
    )
    evidence_ladders_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output instead of plain text",
    )
    evidence_ladders_parser.add_argument(
        "--catalog-dir",
        metavar="PATH",
        default=None,
        help=(
            "Override catalog directory for route lookups and crews.yaml "
            "(default: config/staffing)"
        ),
    )
    evidence_ladders_parser.add_argument(
        "--output-dir",
        metavar="PATH",
        default=None,
        help=(
            "Output directory for the derived-ladders JSON file "
            "(default: config/staffing)"
        ),
    )
    evidence_ladders_parser.set_defaults(func=_run_evidence_ladders)

    template_parser = subparsers.add_parser(
        "template",
        help="Print example config YAML to stdout",
    )
    template_parser.set_defaults(func=_run_template)

    trace_parser = subparsers.add_parser(
        "trace",
        help="Show recent trace summaries",
    )
    trace_parser.add_argument(
        "--last",
        type=int,
        default=10,
        metavar="N",
        help="Number of traces to show (default: 10)",
    )
    trace_parser.add_argument(
        "--dir",
        metavar="DIR",
        default=None,
        help="Trace directory (default: .lee-llm-router/traces)",
    )
    trace_parser.set_defaults(func=_run_trace)

    export_parser = subparsers.add_parser(
        "export-source",
        help="Export the package as a vendorable source snapshot",
    )
    export_parser.add_argument(
        "--dest",
        required=True,
        metavar="PATH",
        help="Destination directory for the vendored lee_llm_router package",
    )
    export_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite a non-empty destination directory",
    )
    export_parser.set_defaults(func=_run_export_source)

    args = parser.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
