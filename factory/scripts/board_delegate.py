#!/usr/bin/env python3
"""Create and account for delegated work on the Agent Board.

This is a small manager-side API client, not an orchestration engine.  The
Board task is the canonical delegation record; execution remains with the
worker and its existing router.  The request callable is deliberately
injectable so tests can exercise the contract without opening a socket.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pwd
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO
from urllib.parse import urlsplit


BOARD_URL = "https://127.0.0.1:8787"

HEADER_START = "BEGIN BOARD DELEGATION"
HEADER_END = "END BOARD DELEGATION"


class BoardDelegateError(Exception):
    """An error safe to show to an operator."""


def _trusted_account_home() -> str:
    """Return the process account home without consulting process inputs."""

    try:
        entry = pwd.getpwuid(os.getuid())
    except (KeyError, OSError) as exc:
        raise BoardDelegateError("trusted account home is unavailable") from exc
    home = getattr(entry, "pw_dir", None)
    if not isinstance(home, str) or "\x00" in home or not os.path.isabs(home):
        raise BoardDelegateError("trusted account home must be an absolute path")
    return home


def _account_board_dir(component: str) -> str:
    return os.path.join(_trusted_account_home(), ".config", "agent-board", component)


# This fixed operator seam is derived once from the effective OS account
# database.  HOME, XDG variables, argv, cwd, and configuration cannot select
# it.  Tests may replace these internal constants before calling main().
OPERATOR_DIR = _account_board_dir("manager")
CREDENTIAL_NAME = "credential"
CREDENTIAL_FILE = OPERATOR_DIR + "/" + CREDENTIAL_NAME


RequestCallable = Callable[
    [str, str, Mapping[str, Any] | None, Mapping[str, str]], Any
]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # noqa: D401
        raise BoardDelegateError("Board refused an HTTP redirect")


_OPENER = urllib.request.build_opener(_NoRedirect)


def _open_security_root() -> int:
    """Open and verify the fixed manager directory descriptor."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    try:
        fd = os.open(OPERATOR_DIR, flags)
    except OSError as exc:
        raise BoardDelegateError("fixed manager credential root is unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise BoardDelegateError("fixed manager credential root is not a directory")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise BoardDelegateError("fixed manager credential root must be mode 0700")
        if info.st_uid != os.getuid():
            raise BoardDelegateError("fixed manager credential root is not owner-held")
    except Exception:
        os.close(fd)
        raise
    return fd


def _read_credential_child(root_fd: int) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(CREDENTIAL_NAME, flags, dir_fd=root_fd)
    except OSError as exc:
        raise BoardDelegateError("fixed manager credential is unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BoardDelegateError("fixed manager credential is not a regular file")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise BoardDelegateError("fixed manager credential must be mode 0600")
        if info.st_uid != os.getuid():
            raise BoardDelegateError("fixed manager credential is not owner-held")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            value = handle.read().decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise BoardDelegateError("fixed manager credential is not valid text") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if not value:
        raise BoardDelegateError("fixed manager credential is empty")
    return value


def read_authority() -> str:
    """Read the operator/manager bearer credential without exposing it."""

    root_fd = _open_security_root()
    try:
        return _read_credential_child(root_fd)
    finally:
        os.close(root_fd)


def _validate_board_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise BoardDelegateError("Board URL must be an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BoardDelegateError("Board URL must not contain credentials or a query")
    if parsed.scheme == "http" and parsed.hostname not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise BoardDelegateError("plain HTTP is allowed only for loopback Board tests")
    return value.rstrip("/")


def _http_request(
    method: str,
    path: str,
    payload: Mapping[str, Any] | None,
    headers: Mapping[str, str],
    *,
    board_url: str,
) -> tuple[int, str]:
    data = None
    request_headers = dict(headers)
    if payload is not None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        board_url + path, data=data, method=method, headers=request_headers
    )
    try:
        with _OPENER.open(req, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        raise BoardDelegateError("Board transport failed") from exc


def _decode_response_body(value: Any) -> Any:
    if value is None or value == "":
        return {}
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise BoardDelegateError("Board returned a non-JSON response") from exc
    return value


def _unpack_response(value: Any) -> tuple[int, Any]:
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], int):
        return value[0], _decode_response_body(value[1])
    if isinstance(value, Mapping) and "status_code" in value:
        return int(value["status_code"]), _decode_response_body(value.get("body"))
    return 200, _decode_response_body(value)


class BoardClient:
    """Manager-scoped Board transport with server-derived actor identity."""

    def __init__(
        self,
        request: RequestCallable | None = None,
        *,
        board_url: str = BOARD_URL,
    ) -> None:
        self.board_url = _validate_board_url(board_url)
        self.credential = read_authority()
        if not self.credential:
            raise BoardDelegateError("manager authority is empty")
        self.request = request or self._live_request

    def _live_request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None,
        headers: Mapping[str, str],
    ) -> tuple[int, str]:
        return _http_request(
            method, path, payload, headers, board_url=self.board_url
        )

    def call(
        self, method: str, path: str, payload: Mapping[str, Any] | None = None
    ) -> Any:
        headers = {
            "Authorization": "Bearer " + self.credential,
            "Accept": "application/json",
            "User-Agent": "agent-board-factory-delegation/1",
        }
        try:
            response = self.request(method, path, payload, headers)
        except BoardDelegateError as exc:
            safe = str(exc).replace(self.credential, "[REDACTED]")
            raise BoardDelegateError(safe or "Board request failed") from exc
        except Exception as exc:
            # Do not relay an arbitrary transport exception: a test double or
            # HTTP library must never be able to echo the bearer credential.
            raise BoardDelegateError(
                f"Board request failed for {method} {path}"
            ) from exc
        status, body = _unpack_response(response)
        if status < 200 or status >= 300:
            raise BoardDelegateError(f"Board rejected {method} {path} (HTTP {status})")
        return body

    def safe(self, value: Any) -> str:
        """Render a value without allowing the bearer to reach a terminal."""

        return str(value).replace(self.credential, "[REDACTED]")


def _read_regular(path: Path, label: str) -> bytes:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(path), flags)
    except OSError as exc:
        raise BoardDelegateError(f"{label} could not be read") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BoardDelegateError(f"{label} must be a regular file")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            return handle.read()
    finally:
        if fd >= 0:
            os.close(fd)


def _required(value: str | None, label: str) -> str:
    if value is None or not value.strip():
        raise BoardDelegateError(f"{label} is required")
    return value.strip()


def _positive_minutes(value: str) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError) as exc:
        raise BoardDelegateError("bound-minutes must be a positive integer") from exc
    if minutes <= 0:
        raise BoardDelegateError("bound-minutes must be a positive integer")
    return minutes


def _acceptance_criteria(instructions: str, supplied: list[str] | None) -> list[str]:
    if supplied:
        criteria = [_required(item, "acceptance-criteria item") for item in supplied]
        return criteria

    lines = instructions.splitlines()
    found: list[str] = []
    collecting = False
    for line in lines:
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("acceptance criteria:"):
            value = stripped.split(":", 1)[1].strip()
            if value:
                found.append(value)
            collecting = True
            continue
        if collecting:
            if not stripped:
                break
            if stripped.startswith(("-", "*")):
                found.append(stripped[1:].strip())
            else:
                found.append(stripped)
    return found or ["See instructions text below"]


def _make_header(args: argparse.Namespace, direction_path: str, digest: str, instructions: str) -> str:
    data = {
        "schema": "board-delegation/v1",
        "delegator": _required(args.delegator, "delegator"),
        "decision_ref": _required(args.decision_ref, "decision-ref"),
        "direction_file": direction_path,
        "direction_sha256": digest,
        "reviewer": _required(args.reviewer, "reviewer"),
        "bound_minutes": _positive_minutes(args.bound_minutes),
        "acceptance_criteria": _acceptance_criteria(
            instructions, getattr(args, "acceptance_criteria", None)
        ),
        # This is a provenance assertion, not a Board ownership transfer.
        "accountability": _required(args.delegator, "delegator"),
    }
    return "\n".join(
        (
            HEADER_START,
            json.dumps(data, sort_keys=True, separators=(",", ":")),
            HEADER_END,
        )
    )


def _task_id(body: Any) -> str:
    if isinstance(body, Mapping):
        if isinstance(body.get("id"), str) and body["id"]:
            return body["id"]
        if isinstance(body.get("task_id"), str) and body["task_id"]:
            return body["task_id"]
        nested = body.get("task")
        if isinstance(nested, Mapping):
            return _task_id(nested)
    raise BoardDelegateError("Board create response did not contain a task id")


def create_ticket(args: argparse.Namespace, client: BoardClient, out: TextIO) -> str:
    direction = Path(_required(args.direction_file, "direction-file"))
    direction_bytes = _read_regular(direction, "direction file")
    digest = hashlib.sha256(direction_bytes).hexdigest()
    try:
        instructions = _read_regular(
            Path(_required(args.instructions_file, "instructions-file")),
            "instructions file",
        ).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BoardDelegateError("instructions file is not UTF-8 text") from exc
    header = _make_header(args, str(direction.resolve()), digest, instructions)
    body = {
        "title": _required(args.title, "title"),
        "mission": _required(args.mission, "mission"),
        "priority": _required(args.priority, "priority"),
        "assigned_agent_id": _required(args.assignee, "assignee"),
        "parent_task_id": args.parent_task.strip() if args.parent_task else "",
        "instructions": header + "\n\n" + instructions,
    }
    result = client.call("POST", "/tasks", body)
    task_id = _task_id(result)
    print(f"task id: {client.safe(task_id)}", file=out)
    print(client.safe(header), file=out)
    return task_id


def _append_bytes(path: Path, data: bytes) -> None:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(path), flags)
    except OSError as exc:
        raise BoardDelegateError("direction file could not be opened") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BoardDelegateError("direction file must be a regular file")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            prior = handle.read()
    finally:
        if fd >= 0:
            os.close(fd)

    separator = b"" if not prior or prior.endswith(b"\n") else b"\n"
    try:
        fd = os.open(
            str(path),
            os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise BoardDelegateError("direction file could not be appended") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BoardDelegateError("direction file must be a regular file")
        remaining = separator + data
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:  # pragma: no cover - regular files should progress.
                raise BoardDelegateError("direction file append made no progress")
            remaining = remaining[written:]
    except OSError as exc:
        raise BoardDelegateError("direction file append failed") from exc
    finally:
        os.close(fd)


def record_direction(args: argparse.Namespace, out: TextIO) -> None:
    path = Path(_required(args.direction_file, "direction-file"))
    task_id = _required(args.task_id, "task-id")
    attributed = _required(
        getattr(args, "attributed_by", None) or getattr(args, "delegator", None),
        "attributed-by",
    )
    entry = _required(
        getattr(args, "entry", None) or getattr(args, "note", None), "entry"
    )
    timestamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    block = (
        f"## Board delegation record — {timestamp}\n"
        f"- task_id: {task_id}\n"
        f"- attributed_to: {attributed}\n"
        f"- entry: {entry}\n"
    ).encode("utf-8")
    _append_bytes(path, block)
    print(f"recorded task: {task_id}", file=out)


def _object_body(body: Any, key: str) -> Mapping[str, Any]:
    if isinstance(body, Mapping) and isinstance(body.get(key), Mapping):
        return body[key]
    if isinstance(body, Mapping):
        return body
    raise BoardDelegateError(f"Board response did not contain {key}")


def _events_body(body: Any) -> list[Mapping[str, Any]]:
    if isinstance(body, Mapping) and isinstance(body.get("events"), list):
        body = body["events"]
    if not isinstance(body, list):
        raise BoardDelegateError("Board events response was not a list")
    return [item for item in body if isinstance(item, Mapping)]


def _header_from_instructions(instructions: str) -> Mapping[str, Any]:
    try:
        start = instructions.index(HEADER_START) + len(HEADER_START)
        end = instructions.index(HEADER_END, start)
        raw = instructions[start:end].strip()
        value = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise BoardDelegateError("task has no valid delegation provenance header") from exc
    if not isinstance(value, Mapping):
        raise BoardDelegateError("task delegation provenance header is not an object")
    return value


def _last_event(events: list[Mapping[str, Any]]) -> str:
    if not events:
        return "none"
    # The native endpoint is ordered, but sorting when timestamps are present
    # keeps the projection correct for injected transports too.
    latest = max(events, key=lambda event: str(event.get("created_at", "")))
    return str(latest.get("event_type") or latest.get("type") or latest.get("action") or "unknown")


def status_projection(args: argparse.Namespace, client: BoardClient, out: TextIO) -> None:
    task_id = _required(args.task_id, "task-id")
    task = _object_body(client.call("GET", f"/tasks/{task_id}"), "task")
    events = _events_body(client.call("GET", f"/tasks/{task_id}/events"))
    header = _header_from_instructions(str(task.get("instructions", "")))
    delegator = str(header.get("delegator") or getattr(args, "delegator", ""))
    delegator = _required(delegator, "delegator in task provenance")
    reviewer = _required(str(header.get("reviewer", "")), "reviewer in task provenance")
    assignee = str(task.get("assigned_agent_id", ""))
    state = str(task.get("status") or task.get("state") or "unknown")
    print(f"owner: {client.safe(delegator)}", file=out)
    print(f"assignee: {client.safe(assignee)}", file=out)
    print(f"state: {client.safe(state)}", file=out)
    print(f"last event: {client.safe(_last_event(events))}", file=out)
    print(f"reviewer: {client.safe(reviewer)}", file=out)
    print(f"accountability: {client.safe(delegator)}", file=out)


def _review_submitted(task: Mapping[str, Any], events: list[Mapping[str, Any]]) -> bool:
    state = str(task.get("status") or task.get("state") or "").lower()
    if state in {"review", "done", "completed"}:
        return True
    for event in events:
        event_type = str(event.get("event_type") or event.get("type") or "").lower()
        if "review" in event_type and (
            "submit" in event_type or "request" in event_type or "approv" in event_type
        ):
            return True
    return False


def close_task(args: argparse.Namespace, client: BoardClient, out: TextIO) -> None:
    task_id = _required(args.task_id, "task-id")
    cancel = bool(args.cancel)
    approve = bool(args.approve)
    complete = bool(args.complete)
    if cancel and (approve or complete):
        raise BoardDelegateError("close accepts cancel or approve/complete, not both")
    if not cancel and not (approve or complete):
        raise BoardDelegateError("close requires --approve, --complete, or --cancel")
    reason = getattr(args, "reason", None)
    if cancel and not _required(reason, "reason"):
        raise BoardDelegateError("cancel requires --reason")

    task = _object_body(client.call("GET", f"/tasks/{task_id}"), "task")
    events = _events_body(client.call("GET", f"/tasks/{task_id}/events"))
    if not cancel and not _review_submitted(task, events):
        raise BoardDelegateError(
            "task review has not been submitted; use --cancel --reason for cancellation"
        )

    result: Any = task
    if cancel:
        result = client.call(
            "POST", f"/tasks/{task_id}/cancel", {"reason": reason.strip()}
        )
    else:
        if approve:
            result = client.call("POST", f"/tasks/{task_id}/approve", {})
        if complete:
            result = client.call("POST", f"/tasks/{task_id}/complete", {})
    result_task = _object_body(result, "task") if isinstance(result, Mapping) else {}
    state = str(result_task.get("status") or result_task.get("state") or "accepted")
    print(f"task: {client.safe(task_id)}", file=out)
    print(f"state: {client.safe(state)}", file=out)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create_parser = commands.add_parser("create")
    create_parser.add_argument("--delegator", required=True)
    create_parser.add_argument("--assignee", required=True)
    create_parser.add_argument("--parent-task", dest="parent_task")
    create_parser.add_argument("--direction-file", required=True)
    create_parser.add_argument("--title", required=True)
    create_parser.add_argument("--mission", required=True)
    create_parser.add_argument("--priority", required=True)
    create_parser.add_argument("--instructions-file", required=True)
    create_parser.add_argument("--reviewer", required=True)
    create_parser.add_argument("--bound-minutes", required=True)
    create_parser.add_argument("--decision-ref", required=True)
    create_parser.add_argument("--acceptance-criteria", action="append")
    create_parser.add_argument("--board-url", default=BOARD_URL)

    record_parser = commands.add_parser("record")
    record_parser.add_argument("--direction-file", required=True)
    record_parser.add_argument("--task-id", required=True)
    record_parser.add_argument("--attributed-by", "--delegator", dest="attributed_by", required=True)
    record_parser.add_argument("--entry", "--note", dest="entry", required=True)

    status_parser = commands.add_parser("status")
    status_parser.add_argument("--task-id", required=True)
    status_parser.add_argument("--delegator")
    status_parser.add_argument("--board-url", default=BOARD_URL)

    close_parser = commands.add_parser("close")
    close_parser.add_argument("--task-id", required=True)
    close_parser.add_argument("--approve", action="store_true")
    close_parser.add_argument("--complete", action="store_true")
    close_parser.add_argument("--cancel", action="store_true")
    close_parser.add_argument("--reason")
    close_parser.add_argument("--board-url", default=BOARD_URL)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    request: RequestCallable | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "record":
            record_direction(args, out)
            return 0
        client = BoardClient(
            request=request,
            board_url=args.board_url,
        )
        if args.command == "create":
            create_ticket(args, client, out)
        elif args.command == "status":
            status_projection(args, client, out)
        elif args.command == "close":
            close_task(args, client, out)
        else:  # pragma: no cover - argparse constrains the command set.
            raise BoardDelegateError("unsupported command")
        return 0
    except BoardDelegateError as exc:
        # Only controlled, non-secret messages reach stderr.  In particular,
        # never print a raw HTTP body or an arbitrary transport exception.
        print(str(exc), file=err)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
