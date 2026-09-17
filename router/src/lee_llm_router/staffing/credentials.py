"""Stage one channel instance credential for an isolated harness run.

Credential files remain in the router's state directory.  This module only
resolves those files and copies one of them into a temporary home using the
authentication layout expected by a supported harness.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CREDENTIALS_DIR = Path("~/.local/state/lee-llm-router/credentials")
"""Directory containing channel-instance credential JSON files."""

__all__ = [
    "DEFAULT_CREDENTIALS_DIR",
    "CredentialStagingError",
    "resolve_credential_path",
    "stage_harness_home",
]


class CredentialStagingError(Exception):
    """Raised when a credential cannot be resolved or staged safely."""

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


@dataclass(frozen=True)
class _HarnessAuthLayout:
    auth_relative_path: Path
    auth_type: str
    extra_env: tuple[tuple[str, Path], ...] = ()


# The paths are relative to the temporary HOME.  HOME is supplied for every
# harness; the second tuple item names any additional harness-specific
# environment variables and their relative directory under that HOME.
_HARNESS_AUTH_LAYOUTS: dict[str, _HarnessAuthLayout] = {
    "opencode": _HarnessAuthLayout(
        auth_relative_path=Path(".local/share/opencode/auth.json"),
        auth_type="api",
    ),
    "pi": _HarnessAuthLayout(
        auth_relative_path=Path(".pi/agent/auth.json"),
        auth_type="api_key",
        extra_env=(("PI_CODING_AGENT_DIR", Path(".pi/agent")),),
    ),
}


def resolve_credential_path(credential_ref: str, *, root: Path | None = None) -> Path:
    """Resolve and validate a channel instance credential reference.

    Args:
        credential_ref: Relative credential identifier, such as
            ``"opencode-go/b"``.
        root: Optional credential directory override, primarily for callers
            using an isolated state directory or tests.

    Returns:
        The existing regular JSON credential file.

    Raises:
        CredentialStagingError: If the referenced path escapes the root or is
            not a regular file.
    """
    if "\\" in credential_ref:
        raise CredentialStagingError(
            f"Invalid credential reference {credential_ref!r}: "
            f"contains backslash characters",
            kind="invalid_credential_ref",
        )
    resolved_root = (root or DEFAULT_CREDENTIALS_DIR).expanduser().resolve()
    candidate_path = (resolved_root / f"{credential_ref}.json").resolve()
    if (
        not candidate_path.is_relative_to(resolved_root)
        or candidate_path == resolved_root
    ):
        raise CredentialStagingError(
            f"Invalid credential reference {credential_ref!r}: "
            f"resolves outside credentials root {resolved_root}",
            kind="invalid_credential_ref",
        )
    if not candidate_path.is_file():
        raise CredentialStagingError(
            f"Credential file does not exist or is not a regular file: "
            f"{candidate_path}",
            kind="missing_credential",
        )
    return candidate_path


def stage_harness_home(
    harness: str,
    credential_path: Path,
    provider_key: str,
) -> AbstractContextManager[dict[str, str]]:
    """Stage one credential in a temporary home for a supported harness.

    Args:
        harness: Harness name.  Only ``"opencode"`` and ``"pi"`` are
            supported by this JSON-file staging mechanism.
        credential_path: An already-resolved regular credential file.
        provider_key: The channel ID to nest the credential under (e.g.
            ``"opencode-go"``).

    Returns:
        A context manager yielding environment overrides for the harness
        subprocess.

    Raises:
        CredentialStagingError: If ``harness`` is not supported.
    """
    layout = _HARNESS_AUTH_LAYOUTS.get(harness)
    if layout is None:
        raise CredentialStagingError(
            f"Credential staging is not supported for harness {harness!r}",
            kind="unsupported_harness",
        )
    return _stage_harness_home(credential_path, layout, provider_key=provider_key)


@contextmanager
def _stage_harness_home(
    credential_path: Path,
    layout: _HarnessAuthLayout,
    provider_key: str,
) -> Iterator[dict[str, str]]:
    """Stage a credential using a previously validated harness layout."""
    auth_relative_path = layout.auth_relative_path
    auth_type = layout.auth_type
    extra_env = layout.extra_env
    try:
        staging_home = Path(tempfile.mkdtemp(prefix="lee-llm-router-cred-"))
    except OSError as exc:
        raise CredentialStagingError(
            f"Failed to create temporary staging directory: {exc}",
            kind="staging_failed",
        ) from exc

    try:
        try:
            content = credential_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CredentialStagingError(
                f"Failed to read credential file {credential_path}: {exc}",
                kind="staging_failed",
            ) from exc
        except UnicodeDecodeError as exc:
            raise CredentialStagingError(
                f"Malformed credential file {credential_path}: invalid UTF-8: {exc}",
                kind="malformed_credential",
            ) from exc

        try:
            raw_data = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise CredentialStagingError(
                f"Malformed credential file {credential_path}: invalid JSON: {exc}",
                kind="malformed_credential",
            ) from exc

        if not isinstance(raw_data, dict) or not isinstance(raw_data.get("key"), str):
            raise CredentialStagingError(
                f"Malformed credential file {credential_path}: "
                f"expected JSON object with string field 'key'",
                kind="malformed_credential",
            )

        payload = {
            provider_key: {
                "type": auth_type,
                "key": raw_data["key"],
            }
        }
        serialized = json.dumps(payload)

        auth_path = staging_home / auth_relative_path
        try:
            auth_path.parent.mkdir(parents=True, exist_ok=True)
            auth_path.write_text(serialized, encoding="utf-8")
            os.chmod(auth_path, 0o600)
        except OSError as exc:
            raise CredentialStagingError(
                f"Failed to write staged auth file to {auth_path}: {exc}",
                kind="staging_failed",
            ) from exc

        env = {"HOME": str(staging_home)}
        env.update(
            {
                variable: str(staging_home / relative_directory)
                for variable, relative_directory in extra_env
            }
        )
        yield env
    finally:
        shutil.rmtree(staging_home, ignore_errors=True)
