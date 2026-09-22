#!/usr/bin/env python3
"""Validated declared inputs for one generic employee-instance commissioning.

This module is the ONLY place instance identity, grant, lifecycle target and
protected/canonical destination paths enter the commissioning transaction. It
replaces the previous role-specific constants: there is no employee-specific
branch here and no `activate_<role>` seam anywhere downstream.

Everything is fail-closed. Unknown keys, missing keys, wrong types, empty
strings, control characters, relative or noncanonical paths, colliding paths,
an endpoint other than HTTPS or literal-loopback HTTP, a capability list that
is empty or duplicated, or a lifecycle target outside the two legal active
states all refuse. The HTTP exception accepts only an IP literal classified as
loopback; DNS names (including localhost) never select cleartext transport.

It reads no credential and prints no secret. `verify-record` additionally
proves that the declaration and the canonical DeploymentRecord agree before any
mutation; a partial canonical identity, a foreign binding or a lifecycle
mismatch refuses there rather than in the middle of the transaction.

Operations:
  validate      --instance PATH   canonical validated declaration on stdout
  export        --instance PATH   TAB-separated non-secret KEY/value rows
  report        --instance PATH   human-readable plan lines (no secrets)
  verify-record --instance PATH --record PATH [--expect-state STATE]
"""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

SCHEMA_VERSION = "agent-board-commission-instance/1"

# The two active lifecycle states the reviewed Factory transition accepts.
ACTIVATION_TARGETS = ("pilot", "scheduled")
# Activation is a transition OUT of the prepared-but-inactive state. Declaring
# any other origin would weaken the fail-closed check the Factory enforces.
ACTIVATION_ORIGIN = "commissioning"

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_REFERENCE = re.compile(r"^[a-z][a-z0-9]*:[A-Za-z0-9][A-Za-z0-9._-]*$")
_PLAIN = re.compile(r"^[^\x00-\x1f\x7f]+$")

_TOP_KEYS = {
    "schema_version",
    "employee_id",
    "display_name",
    "board_identity",
    "grant",
    "lifecycle",
    "registry",
    "protected",
}
_IDENTITY_KEYS = {"board_ref", "principal_ref", "agent_id", "machine_id"}
_GRANT_KEYS = {"agent_kind", "agent_role", "agent_capabilities"}
_LIFECYCLE_KEYS = {"from_state", "target_state"}
_REGISTRY_KEYS = {"records_dir", "record_path", "evidence_path"}
_PROTECTED_KEYS = {"credential_path", "endpoint_path", "endpoint_url"}

MAX_CAPABILITIES = 16


class DeclarationError(ValueError):
    """Raised when declared commissioning inputs cannot be trusted."""


def _object(body, field, keys):
    if not isinstance(body, dict):
        raise DeclarationError(f"{field} must be an object")
    unknown = sorted(str(key) for key in set(body) - keys)
    if unknown:
        raise DeclarationError(f"{field} has unknown field(s): {', '.join(unknown)}")
    missing = sorted(keys - set(body))
    if missing:
        raise DeclarationError(f"{field} is missing field(s): {', '.join(missing)}")
    return body


def _text(value, field, limit=128):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise DeclarationError(f"{field} must be a trimmed non-empty string")
    if len(value) > limit or not _PLAIN.match(value):
        raise DeclarationError(f"{field} must be printable and at most {limit} characters")
    return value


def _slug(value, field):
    _text(value, field, limit=64)
    if not _SLUG.match(value):
        raise DeclarationError(f"{field} must be a lowercase hyphenated slug")
    return value


def _reference(value, field):
    _text(value, field)
    if not _REFERENCE.match(value):
        raise DeclarationError(f"{field} must be a namespaced reference like 'kind:name'")
    return value


def _namespaced(value, field, namespace):
    _reference(value, field)
    if not value.startswith(namespace + ":"):
        raise DeclarationError(f"{field} must use the '{namespace}:' namespace")
    _slug(value.split(":", 1)[1], f"{field} name")
    return value


def _absolute(value, field):
    _text(value, field, limit=4096)
    if not value.startswith("/") or os.path.normpath(value) != value or value.endswith("/"):
        raise DeclarationError(f"{field} must be a canonical absolute path")
    if ".." in Path(value).parts:
        raise DeclarationError(f"{field} must not contain a parent reference")
    return value


def _identity(body):
    identity = _object(body, "board_identity", _IDENTITY_KEYS)
    board_ref = _namespaced(identity["board_ref"], "board_identity.board_ref", "board")
    principal_ref = _namespaced(
        identity["principal_ref"], "board_identity.principal_ref", "principal"
    )
    agent_id = _slug(identity["agent_id"], "board_identity.agent_id")
    machine_id = _text(identity["machine_id"], "board_identity.machine_id", limit=64)
    # The distinctness rules the Board binder and the Factory transition both
    # enforce. A principal that equals the physical host, or a board_ref reused
    # as the principal, collapses identities that must stay separate.
    if principal_ref == machine_id:
        raise DeclarationError("principal_ref and machine_id must be distinct")
    if board_ref == principal_ref:
        raise DeclarationError("board_ref and principal_ref must be distinct")
    if agent_id == machine_id:
        raise DeclarationError("agent_id and machine_id must be distinct")
    return {
        "board_ref": board_ref,
        "principal_ref": principal_ref,
        "agent_id": agent_id,
        "machine_id": machine_id,
    }


def _grant(body):
    grant = _object(body, "grant", _GRANT_KEYS)
    capabilities = grant["agent_capabilities"]
    if not isinstance(capabilities, list) or not capabilities:
        # The Board fails a participant closed when the approved grant carries
        # no capability, so an empty list is never a valid least privilege.
        raise DeclarationError("grant.agent_capabilities must be a non-empty array")
    if len(capabilities) > MAX_CAPABILITIES:
        raise DeclarationError(
            f"grant.agent_capabilities must hold at most {MAX_CAPABILITIES} entries"
        )
    resolved = [
        _slug(value, f"grant.agent_capabilities[{index}]")
        for index, value in enumerate(capabilities)
    ]
    if len(set(resolved)) != len(resolved):
        raise DeclarationError("grant.agent_capabilities must not repeat a capability")
    if resolved != sorted(resolved):
        raise DeclarationError("grant.agent_capabilities must be sorted for canonical review")
    return {
        "agent_kind": _slug(grant["agent_kind"], "grant.agent_kind"),
        "agent_role": _slug(grant["agent_role"], "grant.agent_role"),
        "agent_capabilities": resolved,
    }


def _lifecycle(body):
    lifecycle = _object(body, "lifecycle", _LIFECYCLE_KEYS)
    if lifecycle["from_state"] != ACTIVATION_ORIGIN:
        raise DeclarationError(
            f"lifecycle.from_state must be {ACTIVATION_ORIGIN!r}; activation never "
            "starts from an already-active or terminal state"
        )
    if lifecycle["target_state"] not in ACTIVATION_TARGETS:
        raise DeclarationError(
            "lifecycle.target_state must be " + " or ".join(ACTIVATION_TARGETS)
        )
    return {"from_state": ACTIVATION_ORIGIN, "target_state": lifecycle["target_state"]}


def _registry(body, employee_id):
    registry = _object(body, "registry", _REGISTRY_KEYS)
    records_dir = _absolute(registry["records_dir"], "registry.records_dir")
    record_path = _absolute(registry["record_path"], "registry.record_path")
    evidence_path = _absolute(registry["evidence_path"], "registry.evidence_path")
    # The canonical record filename is derived from the validated slug, so a
    # declaration can never point the transaction at another employee's record
    # or escape the records directory.
    expected = str(Path(records_dir) / f"{employee_id}.json")
    if record_path != expected:
        raise DeclarationError(
            f"registry.record_path must be the canonical {expected}"
        )
    if not evidence_path.endswith(".json"):
        raise DeclarationError("registry.evidence_path must name a JSON evidence file")
    if Path(evidence_path).is_relative_to(records_dir):
        raise DeclarationError(
            "registry.evidence_path must be outside the direct record scan directory"
        )
    if Path(evidence_path).name != f"{employee_id}.activation.json":
        raise DeclarationError(
            f"registry.evidence_path must be named {employee_id}.activation.json"
        )
    return {
        "records_dir": records_dir,
        "record_path": record_path,
        "evidence_path": evidence_path,
    }


def _protected(body, employee_id):
    protected = _object(body, "protected", _PROTECTED_KEYS)
    credential_path = _absolute(protected["credential_path"], "protected.credential_path")
    endpoint_path = _absolute(protected["endpoint_path"], "protected.endpoint_path")
    # Both protected files live in one per-instance private directory named for
    # the employee. A shared or foreign-named directory is a path mismatch and
    # refuses: it is how one instance would end up reading another's secret.
    credential_parent = Path(credential_path).parent
    if Path(endpoint_path).parent != credential_parent:
        raise DeclarationError(
            "protected.credential_path and protected.endpoint_path must share one "
            "per-instance directory"
        )
    if credential_parent.name != employee_id:
        raise DeclarationError(
            f"protected per-instance directory must be named {employee_id}"
        )
    url = _text(protected["endpoint_url"], "protected.endpoint_url", limit=2048)
    if not url.isprintable():
        raise DeclarationError("protected.endpoint_url must contain no control characters")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        # Accessing port is the parser's validation step for nonnumeric and
        # out-of-range ports; urlsplit alone deliberately leaves those latent.
        parsed.port
    except ValueError as error:
        raise DeclarationError(
            "protected.endpoint_url must be a well-formed absolute URL with a valid port"
        ) from error
    if parsed.scheme not in ("https", "http") or not parsed.netloc or not hostname:
        raise DeclarationError(
            "protected.endpoint_url must use HTTPS, except HTTP is allowed for an "
            "IP-literal loopback host"
        )
    if (
        parsed.username is not None
        or parsed.password is not None
        or "?" in url
        or "#" in url
    ):
        raise DeclarationError(
            "protected.endpoint_url must carry no credentials, query or fragment"
        )
    if (
        parsed.netloc.endswith(":")
        or "\\" in parsed.netloc
        or "%" in hostname
        or any(character.isspace() for character in parsed.netloc)
    ):
        raise DeclarationError("protected.endpoint_url has an ambiguous host or port")
    if parsed.scheme == "http":
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError as error:
            raise DeclarationError(
                "HTTP protected.endpoint_url must use an IP-literal loopback host"
            ) from error
        if (
            not address.is_loopback
            or isinstance(address, ipaddress.IPv6Address)
            and address.ipv4_mapped is not None
        ):
            raise DeclarationError(
                "HTTP protected.endpoint_url must use an IP-literal loopback host"
            )
    return {
        "credential_path": credential_path,
        "endpoint_path": endpoint_path,
        "endpoint_url": url.rstrip("/"),
    }


def load(path):
    """Read and fully validate one declaration file. Never reads a credential."""

    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise DeclarationError(f"cannot read instance declaration {path}: {error}") from error
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as error:
        raise DeclarationError(f"instance declaration is not valid JSON: {error}") from error
    body = _object(body, "instance declaration", _TOP_KEYS)
    if body["schema_version"] != SCHEMA_VERSION:
        raise DeclarationError(
            f"unsupported instance declaration schema: {body['schema_version']!r}"
        )
    employee_id = _slug(body["employee_id"], "employee_id")
    identity = _identity(body["board_identity"])
    if identity["agent_id"] != employee_id:
        # The Board agent id and the Factory employee id are separate concepts,
        # but the reviewed binding pins them together so history attribution and
        # registry lookup cannot drift apart.
        raise DeclarationError("board_identity.agent_id must equal employee_id")
    declaration = {
        "schema_version": SCHEMA_VERSION,
        "employee_id": employee_id,
        "display_name": _text(body["display_name"], "display_name"),
        "board_identity": identity,
        "grant": _grant(body["grant"]),
        "lifecycle": _lifecycle(body["lifecycle"]),
        "registry": _registry(body["registry"], employee_id),
        "protected": _protected(body["protected"], employee_id),
    }
    paths = [
        declaration["registry"]["record_path"],
        declaration["registry"]["evidence_path"],
        declaration["protected"]["credential_path"],
        declaration["protected"]["endpoint_path"],
    ]
    if len(set(paths)) != len(paths):
        raise DeclarationError("declared destination paths must be distinct")
    for destination in paths:
        if Path(destination).parent == Path(destination):
            raise DeclarationError(f"declared destination has no parent: {destination}")
    return declaration


def export_rows(declaration):
    """Non-secret TAB-separated rows for shell consumption (never eval'd)."""

    return [
        ("EMPLOYEE_ID", declaration["employee_id"]),
        ("DISPLAY_NAME", declaration["display_name"]),
        ("BOARD_REF", declaration["board_identity"]["board_ref"]),
        ("PRINCIPAL_REF", declaration["board_identity"]["principal_ref"]),
        ("AGENT_ID", declaration["board_identity"]["agent_id"]),
        ("MACHINE_ID", declaration["board_identity"]["machine_id"]),
        ("AGENT_KIND", declaration["grant"]["agent_kind"]),
        ("AGENT_ROLE", declaration["grant"]["agent_role"]),
        ("AGENT_CAPABILITIES", ",".join(declaration["grant"]["agent_capabilities"])),
        ("LIFECYCLE_FROM", declaration["lifecycle"]["from_state"]),
        ("LIFECYCLE_TARGET", declaration["lifecycle"]["target_state"]),
        ("RECORDS_DIR", declaration["registry"]["records_dir"]),
        ("RECORD_PATH", declaration["registry"]["record_path"]),
        ("EVIDENCE_PATH", declaration["registry"]["evidence_path"]),
        ("CREDENTIAL_PATH", declaration["protected"]["credential_path"]),
        ("ENDPOINT_PATH", declaration["protected"]["endpoint_path"]),
        ("ENDPOINT_URL", declaration["protected"]["endpoint_url"]),
    ]


def verify_record(declaration, record_path, expect_state=None):
    """Prove the canonical record agrees with the declaration before mutation."""

    try:
        raw = Path(record_path).read_bytes()
    except OSError as error:
        raise DeclarationError(f"cannot read canonical record {record_path}: {error}") from error
    try:
        record = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeclarationError(f"canonical record is not valid JSON: {error}") from error
    if not isinstance(record, dict):
        raise DeclarationError("canonical record must be an object")
    if record.get("employee_id") != declaration["employee_id"]:
        raise DeclarationError(
            "canonical record employee_id does not match the declared instance"
        )
    if record.get("display_name") != declaration["display_name"]:
        raise DeclarationError(
            "canonical record display_name does not match the declared instance"
        )
    identity = record.get("board_identity")
    if not isinstance(identity, dict):
        raise DeclarationError("canonical record carries no board identity")
    if set(identity) != _IDENTITY_KEYS:
        # A partial canonical identity (for example board_ref only) is the
        # prepared-but-unbound shape. It must never provision a participant.
        raise DeclarationError(
            "canonical record must carry the complete Board binding: "
            + ", ".join(sorted(_IDENTITY_KEYS))
        )
    for field in sorted(_IDENTITY_KEYS):
        if identity.get(field) != declaration["board_identity"][field]:
            raise DeclarationError(
                f"canonical record {field} does not match the declared binding"
            )
    state = record.get("lifecycle_state")
    expected = expect_state or declaration["lifecycle"]["from_state"]
    if state != expected:
        raise DeclarationError(
            f"canonical record lifecycle_state is {state!r}; expected {expected!r}"
        )
    return {"employee_id": declaration["employee_id"], "lifecycle_state": state}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("validate", "export", "report", "verify-record"))
    parser.add_argument("--instance", required=True)
    parser.add_argument("--record", default="")
    parser.add_argument("--expect-state", default="")
    args = parser.parse_args()

    declaration = load(args.instance)
    if args.operation == "validate":
        print(json.dumps(declaration, sort_keys=True, indent=2))
    elif args.operation == "export":
        for key, value in export_rows(declaration):
            print(f"{key}\t{value}")
    elif args.operation == "report":
        print(f"declared employee instance: {declaration['employee_id']} ({declaration['display_name']})")
        for key, value in export_rows(declaration)[2:]:
            print(f"  {key.lower()}: {value}")
    else:
        if not args.record:
            raise DeclarationError("verify-record requires --record")
        result = verify_record(declaration, args.record, args.expect_state or None)
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DeclarationError as error:
        print(f"instance declaration refused: {error}", file=sys.stderr)
        sys.exit(1)
