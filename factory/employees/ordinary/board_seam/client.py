#!/usr/bin/env python3
"""Fixed, per-instance participation seam for the Agent Board.

This module is a capability client, not a Board administration or workflow
client.  Its public operations are exactly the ordinary-instance obligations:

* ``register`` -> ``POST /agents/register``
* ``read_task`` / ``list_tasks`` -> ``GET /tasks/{id}`` / ``GET /tasks``
* ``poll`` -> ``POST /poll``
* ``renew_lease`` -> ``POST /leases/{id}/renew``
* ``upload_artifact`` -> ``POST /artifacts/upload``
* ``submit_review`` -> ``POST /tasks/{id}/review``
* ``send_ask`` / ``ack_message`` / ``inbox`` -> ``POST /messages``,
  ``POST /messages/{id}/ack``, and ``GET /inbox``
* ``events`` -> ``GET /tasks/{id}/events``

The Board derives the acting agent from the protected participant credential.
No operation accepts an actor identity.  Manager/operator operations such as
task creation, approval, completion, machine registration, and arbitrary HTTP
access deliberately have no representation here.
"""

from __future__ import annotations

import ipaddress
import json
import os
import pwd
import re
import socket
import stat
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlsplit


PACKAGE_ROOT = os.path.dirname(os.path.abspath(__file__))
ENDPOINT_NAME = "board-endpoint.json"
CREDENTIAL_NAME = "credential"
ALLOWED_UPLOAD_ROOTS = ("outputs", "state")
DEFAULT_LEASE_SECONDS = 900
_EMPLOYEE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_PACKET_KEYS = frozenset({"board_url", "credential_ref", "employee_id"})


class BoardError(Exception):
    """Base class for safe seam errors."""


class UsageError(BoardError):
    """The caller supplied an invalid operation argument."""


class ConfigError(BoardError):
    """The protected participant configuration is invalid or unavailable."""


class TransportError(BoardError):
    """The Board transport or response could not be used."""


class HTTPError(BoardError):
    """The Board returned an HTTP error, with secret-safe body text."""

    def __init__(self, status: int, body: str, secret: str = "") -> None:
        safe_body = _redact(body, secret)
        self.status = status
        self.body = safe_body
        super().__init__(f"HTTP {status}: {safe_body}")


def _trusted_account_home() -> str:
    """Return the process account home without consulting process inputs."""

    try:
        entry = pwd.getpwuid(os.getuid())
    except (KeyError, OSError) as exc:
        raise ConfigError("trusted account home is unavailable") from exc
    home = getattr(entry, "pw_dir", None)
    if not isinstance(home, str) or "\x00" in home or not os.path.isabs(home):
        raise ConfigError("trusted account home must be an absolute path")
    return home


def _account_board_dir(component: str) -> str:
    return os.path.join(_trusted_account_home(), ".config", "agent-board", component)


# This is a fixed security boundary.  It is derived once from the effective
# OS account database and is intentionally independent of HOME, XDG variables,
# argv, the current directory, and configuration.  Tests may replace this
# internal constant with an absolute temporary root; callers get no selector.
PARTICIPANT_ROOT = _account_board_dir("participants")


def _redact(value: Any, secret: str) -> str:
    """Return text with the credential removed, including from test failures."""

    text = str(value)
    return text.replace(secret, "[redacted]") if secret else text


def _validate_employee_id(employee_id: str) -> str:
    if not isinstance(employee_id, str) or not _EMPLOYEE_ID.fullmatch(employee_id):
        raise ConfigError("employee_id must be a lowercase hyphenated slug")
    return employee_id


def _participant_dir(employee_id: str) -> str:
    employee_id = _validate_employee_id(employee_id)
    return os.path.join(PARTICIPANT_ROOT, employee_id)


def endpoint_path(employee_id: str) -> str:
    """Return the derived endpoint path without accepting a path override."""

    return os.path.join(_participant_dir(employee_id), ENDPOINT_NAME)


def credential_path(employee_id: str) -> str:
    """Return the derived credential path without reading its contents."""

    return os.path.join(_participant_dir(employee_id), CREDENTIAL_NAME)


def _addr_is_loopback(address: str) -> bool:
    address = address.split("%", 1)[0]
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def _is_loopback_host(host: str) -> bool:
    """Accept HTTP only when every resolved address is loopback."""

    host = host.strip()
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    addresses = [info[4][0] for info in infos]
    return bool(addresses) and all(_addr_is_loopback(address) for address in addresses)


def _open_security_root(employee_id: str) -> int:
    """Open and verify the derived participant directory."""

    root = _participant_dir(employee_id)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(root, flags)
    except OSError as exc:
        raise ConfigError(
            "participant root must open as an owned mode-0700 directory without a symlink"
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise ConfigError("participant root must be a real directory")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise ConfigError("participant root must be mode 0700")
        if info.st_uid != os.getuid():
            raise ConfigError("participant root must be owned by this user")
    except Exception:
        os.close(fd)
        raise
    return fd


def _open_at(root_fd: int, name: str, field: str) -> int:
    """Open a protected child descriptor-relatively and validate its inode."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=root_fd)
    except OSError as exc:
        raise ConfigError(f"{field} must open without a symlink") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"{field} must be a regular file")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise ConfigError(f"{field} must be mode 0600")
        if info.st_uid != os.getuid():
            raise ConfigError(f"{field} must be owned by this user")
    except Exception:
        os.close(fd)
        raise
    return fd


def _read_at(root_fd: int, name: str, field: str) -> str:
    with os.fdopen(_open_at(root_fd, name, field), "r", encoding="utf-8") as handle:
        return handle.read()


def _load_endpoint(employee_id: str) -> tuple[str, str]:
    """Read the endpoint and credential from one verified instance root."""

    employee_id = _validate_employee_id(employee_id)
    root_fd = _open_security_root(employee_id)
    try:
        raw = _read_at(root_fd, ENDPOINT_NAME, "board endpoint packet")
        try:
            packet = json.loads(raw)
        except json.JSONDecodeError:
            # Do not include malformed packet text: it could contain a value
            # equal to the credential and must never reach an error message.
            raise ConfigError("board endpoint packet is not valid JSON") from None
        if not isinstance(packet, dict):
            raise ConfigError("board endpoint packet must be a JSON object")
        if set(packet) - _PACKET_KEYS:
            raise ConfigError("board endpoint packet has unknown key(s)")
        if "employee_id" in packet and packet.get("employee_id") != employee_id:
            raise ConfigError("board endpoint packet employee_id does not match this instance")

        board_url = packet.get("board_url")
        if not isinstance(board_url, str):
            raise ConfigError("board_url must be a string")
        parsed = urlsplit(board_url)
        if (
            parsed.scheme not in ("https", "http")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ConfigError("board_url must be an absolute http(s) URL without credentials or fragment")
        if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
            raise ConfigError("board_url must use https except for a loopback URL")

        expected_credential = credential_path(employee_id)
        if packet.get("credential_ref") != expected_credential:
            raise ConfigError("credential_ref must name this instance's fixed protected credential")

        secret = _read_at(root_fd, CREDENTIAL_NAME, "credential").strip()
        if not secret:
            raise ConfigError("credential file is empty")
        return board_url.rstrip("/"), secret
    finally:
        os.close(root_fd)


def _read_secret(employee_id: str) -> str:
    """Read the derived credential for security tests and commissioning tools."""

    employee_id = _validate_employee_id(employee_id)
    root_fd = _open_security_root(employee_id)
    try:
        secret = _read_at(root_fd, CREDENTIAL_NAME, "credential").strip()
    finally:
        os.close(root_fd)
    if not secret:
        raise ConfigError("credential file is empty")
    return secret


def _safe_relative_upload(relative_file: str, workspace_root: str = PACKAGE_ROOT) -> str:
    """Resolve one regular file under the workspace's outputs/state roots."""

    if not isinstance(relative_file, str) or not relative_file:
        raise UsageError("evidence file must be a non-empty relative path")
    if os.path.isabs(relative_file) or relative_file.startswith("~"):
        raise UsageError("evidence file must be a relative path without '~'")
    if ".." in relative_file.split("/"):
        raise UsageError("evidence file must not contain '..'")

    normalized = os.path.normpath(relative_file)
    if normalized in ("", "."):
        raise UsageError("evidence file must name a regular file")
    root = normalized.split(os.sep, 1)[0]
    if root not in ALLOWED_UPLOAD_ROOTS:
        raise UsageError("evidence file must live under outputs/ or state/")

    package_root = os.path.realpath(os.fspath(workspace_root))
    absolute = os.path.join(package_root, normalized)
    real_root = os.path.realpath(os.path.join(package_root, root))
    real_candidate = os.path.realpath(absolute)
    if real_candidate != real_root and not real_candidate.startswith(real_root + os.sep):
        raise UsageError("evidence file escapes outputs/ or state/")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(absolute, flags)
    except OSError as exc:
        raise UsageError("evidence file is not a safe regular file") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UsageError("evidence file must be a regular file")
    finally:
        os.close(fd)
    return absolute


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, including a same-origin redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise ConfigError("Board refused an HTTP redirect")


_OPENER = urllib.request.build_opener(_NoRedirect)


def _json_body(raw: Any, secret: str) -> Any:
    if isinstance(raw, (dict, list, int, float, bool)) or raw is None:
        return raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        raise TransportError("Board response was not JSON")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise TransportError("Board response was not valid JSON") from None


def _multipart_body(upload: Mapping[str, Any]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    fields = {key: value for key, value in upload.items() if key != "file"}
    file_field, file_path = upload["file"]
    try:
        with open(file_path, "rb") as handle:
            file_bytes = handle.read()
    except OSError as exc:
        raise UsageError("evidence file could not be read") from exc

    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
            f'filename="{os.path.basename(file_path)}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _path_segment(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or "/" in value or "\\" in value:
        raise UsageError(f"{field} must be a single non-empty path segment")
    return quote(value, safe="-._~")


RequestCallable = Callable[..., Any]


class BoardClient:
    """One commissioned employee's narrow, credential-backed Board client.

    ``request`` is an injectable transport for tests.  It receives
    ``(method, path, payload, headers)`` and may return an already-parsed body,
    a ``(status, body)`` pair, or a JSON string/bytes body.  Production callers
    should omit it so the no-redirect HTTP transport is used.
    """

    def __init__(
        self,
        employee_id: str,
        *,
        workspace_root: str | os.PathLike[str] | None = None,
        request: RequestCallable | None = None,
    ) -> None:
        self.employee_id = _validate_employee_id(employee_id)
        self._workspace_root = os.fspath(workspace_root or PACKAGE_ROOT)
        self._board_url, self._secret = _load_endpoint(self.employee_id)
        self._machine_id = socket.gethostname().strip().lower()
        if not self._machine_id:
            raise ConfigError("bound machine hostname is empty")
        self._transport = request or self._http_request

    def _http_request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None,
        headers: Mapping[str, str],
    ) -> Any:
        request_headers = dict(headers)
        data = None
        if payload is not None and "file" in payload:
            data, content_type = _multipart_body(payload)
            request_headers["Content-Type"] = content_type
        elif payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            self._board_url + path,
            data=data,
            method=method,
            headers=request_headers,
        )
        try:
            with _OPENER.open(request, timeout=30) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise HTTPError(exc.code, exc.read().decode("utf-8", "replace"), self._secret) from None
        except urllib.error.URLError as exc:
            raise TransportError("transport failure: " + _redact(exc.reason, self._secret)) from None
        if status >= 400:
            raise HTTPError(status, raw.decode("utf-8", "replace"), self._secret)
        return _json_body(raw, self._secret)

    def _call(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        headers = {
            "Authorization": "Bearer " + self._secret,
            "Accept": "application/json",
            "User-Agent": "agent-board-factory-client/2",
        }
        try:
            result = self._transport(method, path, payload, headers)
            if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
                status, body = result
                if status >= 400:
                    raise HTTPError(status, _redact(body, self._secret), self._secret)
                return _json_body(body, self._secret)
            return _json_body(result, self._secret)
        except BoardError as exc:
            if self._secret and self._secret in str(exc):
                raise TransportError(
                    "transport failure: " + _redact(exc, self._secret)
                ) from None
            raise
        except Exception as exc:
            # Do not preserve an injected transport exception as a traceback
            # cause: a test or adapter may have included the credential in it.
            raise TransportError("transport failure: " + _redact(exc, self._secret)) from None

    def register(self) -> Any:
        """Register this bound instance through ``POST /agents/register``."""

        return self._call("POST", "/agents/register", {"machine_id": self._machine_id})

    def read_task(self, task_id: str) -> Any:
        """Read one Board-assigned task through ``GET /tasks/{id}``."""

        return self._call("GET", f"/tasks/{_path_segment(task_id, 'task_id')}")

    def list_tasks(self) -> Any:
        """Read this participant's filtered task view through ``GET /tasks``."""

        return self._call("GET", "/tasks")

    def poll(self, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Any:
        """Claim assigned work through ``POST /poll`` and return its lease."""

        if not isinstance(lease_seconds, int) or lease_seconds < 0:
            raise UsageError("lease_seconds must be a non-negative integer")
        payload: dict[str, Any] = {"machine_id": self._machine_id}
        if lease_seconds:
            payload["lease_seconds"] = lease_seconds
        return self._call("POST", "/poll", payload)

    def renew_lease(self, lease_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Any:
        """Renew one held lease through ``POST /leases/{id}/renew``."""

        if not isinstance(lease_seconds, int) or lease_seconds < 0:
            raise UsageError("lease_seconds must be a non-negative integer")
        return self._call(
            "POST",
            f"/leases/{_path_segment(lease_id, 'lease_id')}/renew",
            {"lease_seconds": lease_seconds},
        )

    def upload_artifact(self, task_id: str, kind: str, relative_file: str) -> Any:
        """Upload safe ``outputs/`` or ``state/`` evidence to the assigned task."""

        absolute = _safe_relative_upload(relative_file, self._workspace_root)
        upload = {
            "task_id": task_id,
            "kind": kind,
            "file": ("file", absolute),
        }
        return self._call("POST", "/artifacts/upload", upload)

    def submit_review(self, task_id: str, reason: str | None = None) -> Any:
        """Submit the held task for independent review."""

        payload: dict[str, Any] = {}
        if reason is not None:
            payload["reason"] = reason
        return self._call(
            "POST",
            f"/tasks/{_path_segment(task_id, 'task_id')}/review",
            payload,
        )

    def send_ask(self, task_id: str, body: str) -> Any:
        """Ask human/Lee on a task, requiring acknowledgement."""

        if not isinstance(body, str) or not body:
            raise UsageError("ask body is required")
        return self._call(
            "POST",
            "/messages",
            {
                "task_id": task_id,
                "to_actor_type": "human",
                "to_actor_id": "lee",
                "kind": "question",
                "body": body,
                "requires_ack": True,
            },
        )

    def ack_message(self, message_id: str) -> Any:
        """Acknowledge a message addressed to this instance."""

        return self._call(
            "POST",
            f"/messages/{_path_segment(message_id, 'message_id')}/ack",
            {},
        )

    def inbox(self) -> Any:
        """Read the participant's own inbox through ``GET /inbox``."""

        return self._call("GET", "/inbox")

    def events(self, task_id: str) -> Any:
        """Read actor-attributed history through ``GET /tasks/{id}/events``."""

        return self._call("GET", f"/tasks/{_path_segment(task_id, 'task_id')}/events")


Client = BoardClient
