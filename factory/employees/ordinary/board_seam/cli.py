#!/usr/bin/env python3
"""Thin command wrapper for a package-bound :class:`BoardClient`.

The employee package must bind ``employee_id`` in code (or pass a client to
``main``); this wrapper never accepts an identity, endpoint, or credential
path in argv or the environment.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .client import BoardClient, BoardError, UsageError, _redact


def _emit(value: Any, secret: str = "") -> int:
    rendered = json.dumps(value, indent=2, sort_keys=True, default=str)
    print(_redact(rendered, secret))
    return 0


def main(
    argv: list[str] | None = None,
    *,
    employee_id: str | None = None,
    client: BoardClient | None = None,
    workspace_root: str | None = None,
    request=None,
) -> int:
    """Dispatch a Clerk-shaped command using a package-bound client."""

    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        raise UsageError("usage: register|list|read|poll|renew|artifact|review|ask|ack|inbox|events")
    if client is None:
        if employee_id is None:
            raise UsageError("employee identity must be bound by the employee package")
        client = BoardClient(employee_id, workspace_root=workspace_root, request=request)

    command, rest = args[0], args[1:]
    if command in {"list", "self"} and not rest:
        return _emit(client.list_tasks(), getattr(client, "_secret", ""))
    if command == "register" and not rest:
        return _emit(client.register(), getattr(client, "_secret", ""))
    if command in {"read"} and len(rest) == 1:
        return _emit(client.read_task(rest[0]), getattr(client, "_secret", ""))
    if command in {"poll", "claim"} and not rest:
        return _emit(client.poll(), getattr(client, "_secret", ""))
    if command in {"renew", "renew-lease"} and len(rest) == 1:
        return _emit(client.renew_lease(rest[0]), getattr(client, "_secret", ""))
    if command in {"artifact", "upload"} and len(rest) == 3:
        return _emit(
            client.upload_artifact(rest[0], rest[1], rest[2]),
            getattr(client, "_secret", ""),
        )
    if command in {"review", "submit-review"} and 1 <= len(rest) <= 2:
        return _emit(
            client.submit_review(rest[0], rest[1] if len(rest) == 2 else None),
            getattr(client, "_secret", ""),
        )
    if command in {"ask", "send-ask", "message"} and len(rest) == 2:
        return _emit(client.send_ask(rest[0], rest[1]), getattr(client, "_secret", ""))
    if command in {"ack", "ack-message"} and len(rest) == 1:
        return _emit(client.ack_message(rest[0]), getattr(client, "_secret", ""))
    if command == "inbox" and not rest:
        return _emit(client.inbox(), getattr(client, "_secret", ""))
    if command == "events" and len(rest) == 1:
        return _emit(client.events(rest[0]), getattr(client, "_secret", ""))
    raise UsageError(f"unknown or malformed command: {command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UsageError as error:
        print(f"usage error: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    except BoardError as error:
        print(f"board error: {error}", file=sys.stderr)
        raise SystemExit(3) from None
