"""Pure D211 proof-status calculation for staffing routes.

A route is proven exactly when at least one ``router_run`` record for that
route has usage basis ``observed`` or ``provider_reported``, or has
``verified_success`` set to the JSON boolean ``true``.  Imports and malformed
records are never evidence of proof.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import Enum
from pathlib import Path
from typing import Any

from lee_llm_router.staffing.ledger import read_attempts, resolve_attempts_path

__all__ = [
    "PROVEN_USAGE_BASES",
    "ProofStatus",
    "is_route_proven",
    "proof_status_by_route",
    "proof_status_ledger",
    "record_proves_route",
    "route_proof_status",
    "router_route_id",
]

PROVEN_USAGE_BASES: frozenset[str] = frozenset({"observed", "provider_reported"})
_ROUTER_RUN_KIND = "router_run"


class ProofStatus(str, Enum):
    """The D211 proof status of a route."""

    PROVEN = "proven"
    UNPROVEN = "unproven"


def router_route_id(record: object) -> str | None:
    """Return ``router_event.route_id`` when it is a usable route id.

    No alternate route-id location is accepted.  Empty and whitespace-only
    strings, non-strings, and missing or malformed mappings fail closed.

    Args:
        record: An attempt record or an arbitrary value to inspect.

    Returns:
        The verbatim route id, or ``None`` when it is unavailable or malformed.
    """
    if not isinstance(record, Mapping):
        return None
    router_event = record.get("router_event")
    if not isinstance(router_event, Mapping):
        return None
    route_id = router_event.get("route_id")
    if not isinstance(route_id, str) or not route_id.strip():
        return None
    return route_id


def _row_proves_route(record: Mapping[str, Any]) -> bool:
    """Return whether one router row satisfies either D211 proof condition."""
    usage = record.get("usage")
    basis = usage.get("basis") if isinstance(usage, Mapping) else None
    has_proven_usage = isinstance(basis, str) and basis in PROVEN_USAGE_BASES
    return has_proven_usage or record.get("verified_success") is True


def record_proves_route(record: object) -> str | None:
    """Return the route proved by one record, or ``None``.

    Only an exact ``record_kind`` of ``router_run`` can prove.  The route id
    comes only from ``router_event.route_id`` and the proof condition is the
    D211 disjunction implemented by :func:`_row_proves_route`.

    Args:
        record: An attempt record or an arbitrary value to inspect.

    Returns:
        The verbatim route id when the record proves it, otherwise ``None``.
    """
    if not isinstance(record, Mapping):
        return None
    if record.get("record_kind") != _ROUTER_RUN_KIND:
        return None
    route_id = router_route_id(record)
    if route_id is None or not _row_proves_route(record):
        return None
    return route_id


def proof_status_by_route(records: Iterable[object]) -> dict[str, ProofStatus]:
    """Return deterministic D211 proof status for every observed router route.

    A route is included when a well-formed ``router_run`` row identifies it,
    even if every such row is unproven.  Import rows, malformed rows, and rows
    with missing or malformed ``router_event.route_id`` are ignored.  Keys are
    inserted in ascending route-id order.

    Args:
        records: Attempt records, normally already validated by the ledger.

    Returns:
        A route-id-to-status mapping sorted by route id.  The empty input
        produces an empty mapping.
    """
    observed: set[str] = set()
    proven: set[str] = set()

    for record in records:
        if not isinstance(record, Mapping):
            continue
        if record.get("record_kind") != _ROUTER_RUN_KIND:
            continue
        route_id = router_route_id(record)
        if route_id is None:
            continue
        observed.add(route_id)
        if _row_proves_route(record):
            proven.add(route_id)

    return {
        route_id: (ProofStatus.PROVEN if route_id in proven else ProofStatus.UNPROVEN)
        for route_id in sorted(observed)
    }


def route_proof_status(records: Iterable[object], route_id: str) -> ProofStatus:
    """Return the D211 proof status for one exact route id.

    Args:
        records: Attempt records, normally already validated by the ledger.
        route_id: The exact route id to inspect; it is not normalised.

    Returns:
        ``PROVEN`` when any matching ``router_run`` proves the route;
        otherwise ``UNPROVEN``.  Invalid query ids fail closed.
    """
    if not isinstance(route_id, str) or not route_id.strip():
        return ProofStatus.UNPROVEN

    for record in records:
        if not isinstance(record, Mapping):
            continue
        if record.get("record_kind") != _ROUTER_RUN_KIND:
            continue
        if router_route_id(record) != route_id:
            continue
        if _row_proves_route(record):
            return ProofStatus.PROVEN
    return ProofStatus.UNPROVEN


def is_route_proven(records: Iterable[object], route_id: str) -> bool:
    """Return whether an exact route is proven under D211."""
    return route_proof_status(records, route_id) is ProofStatus.PROVEN


def proof_status_ledger(
    path: str | Path | None = None,
) -> dict[str, ProofStatus]:
    """Read an attempt ledger and return its deterministic route proof status.

    A missing ledger is treated as empty.  A present ledger is read through
    the normal strict ledger reader; its validation errors are not swallowed.

    Args:
        path: Optional explicit ledger path, otherwise the configured path.

    Returns:
        The same mapping returned by :func:`proof_status_by_route`.
    """
    ledger_path = resolve_attempts_path(path)
    try:
        records = read_attempts(ledger_path)
    except FileNotFoundError:
        records = []
    return proof_status_by_route(records)
