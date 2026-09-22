#!/usr/bin/env python3
"""Run the authenticated, non-secret Board commissioning verification.

The helper is intentionally a client of the supported Board HTTP surface. It
never opens the Board database, never prints a bearer value, and only writes a
small receipt containing identifiers, digests, timestamps and boolean checks.
The operator credential is read from the protected local-auth file; the
participant credential is read from the declared protected credential seam.
"""

import argparse
import base64
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid


class CommissionError(Exception):
    """An error whose text is safe to put in a non-secret failure receipt."""


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def read_private(path, expected_mode):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CommissionError(f"cannot open protected input {path}: {exc.strerror}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CommissionError(f"protected input is not a regular file: {path}")
        if stat.S_IMODE(info.st_mode) != expected_mode:
            raise CommissionError(f"protected input has wrong mode: {path}")
        if info.st_uid != os.getuid():
            raise CommissionError(f"protected input is not owner-held: {path}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            return handle.read()
    finally:
        if fd >= 0:
            os.close(fd)


def load_json(path, mode=None):
    raw = read_private(path, mode) if mode is not None else Path(path).read_bytes()
    try:
        return raw, json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommissionError(f"invalid JSON input: {path}") from exc


def load_validated_declaration(path):
    """Load a declaration through the commissioning declaration contract."""

    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise CommissionError(f"cannot read instance declaration: {path}") from exc

    validator_path = Path(__file__).resolve().with_name("instance-declaration.py")
    spec = importlib.util.spec_from_file_location(
        "commission_instance_declaration", validator_path
    )
    if spec is None or spec.loader is None:
        raise CommissionError("instance declaration validator is unavailable")
    validator = importlib.util.module_from_spec(spec)
    previous_bytecode_setting = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(validator)
    except Exception as exc:
        raise CommissionError("instance declaration validator is unavailable") from exc
    finally:
        sys.dont_write_bytecode = previous_bytecode_setting
    try:
        declaration = validator.load(path)
    except validator.DeclarationError as exc:
        raise CommissionError(f"instance declaration refused: {exc}") from exc
    try:
        confirmed_raw = Path(path).read_bytes()
    except OSError as exc:
        raise CommissionError(f"cannot reread instance declaration: {path}") from exc
    if confirmed_raw != raw:
        raise CommissionError("instance declaration changed during validation")
    return confirmed_raw, declaration


def endpoint_url(value, *, allow_http):
    try:
        parsed = urllib.parse.urlsplit(value)
        parsed.port
    except (TypeError, ValueError) as exc:
        raise CommissionError("Board endpoint must be an absolute protected URL") from exc
    allowed = ("https", "http") if allow_http else ("https",)
    if (
        not isinstance(value, str)
        or parsed.scheme not in allowed
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not value.isprintable()
    ):
        raise CommissionError("Board endpoint must be an absolute protected URL")
    return value.rstrip("/")


def resolve_endpoint(declaration, *, override=None, allow_http=False):
    """Keep the explicit test override separate from validated configuration."""

    if override:
        return endpoint_url(override, allow_http=allow_http)
    return declaration["protected"]["endpoint_url"]


class BoardClient:
    def __init__(self, endpoint, *, operator_token, participant_token):
        self.endpoint = endpoint
        self.operator_token = operator_token
        self.participant_token = participant_token

    def request(self, method, path, token, *, body=None, headers=None, expected=(200,), raw=False):
        payload = None
        request_headers = {"User-Agent": "agent-board-commissioner/1"}
        if token:
            request_headers["Authorization"] = "Bearer " + token
        if headers:
            request_headers.update(headers)
        if body is not None:
            payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            self.endpoint + "/" + path.lstrip("/"),
            data=payload,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
                content = response.read()
        except urllib.error.HTTPError as exc:
            # Do not include the response body: an operator endpoint must never
            # turn an unexpected payload into a log or receipt leak.
            status = exc.code
            content = exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CommissionError(f"Board request {method} {path} could not connect") from exc
        if status not in expected:
            raise CommissionError(f"Board request {method} {path} returned HTTP {status}")
        if raw:
            return status, content
        if not content:
            return status, None
        try:
            return status, json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CommissionError(f"Board request {method} {path} returned invalid JSON") from exc

    def health(self):
        _, body = self.request("GET", "/health", None)
        if not isinstance(body, dict) or body.get("status") != "ok":
            raise CommissionError("Board health response was not status=ok")
        return body

    def upload(self, task_id, summary, content):
        boundary = "----agent-board-commission-" + uuid.uuid4().hex
        parts = []

        def field(name, value):
            parts.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    str(value).encode("utf-8"),
                    b"\r\n",
                ]
            )

        field("task_id", task_id)
        field("kind", "commissioning-receipt")
        field("summary", summary)
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="file"; filename="commissioning-receipt.json"\r\n',
                b"Content-Type: application/json\r\n\r\n",
                content,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        request = urllib.request.Request(
            self.endpoint + "/artifacts/upload",
            data=b"".join(parts),
            headers={
                "Authorization": "Bearer " + self.participant_token,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "agent-board-commissioner/1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_body = exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CommissionError("Board artifact upload could not connect") from exc
        if status != 200:
            raise CommissionError(f"Board request POST /artifacts/upload returned HTTP {status}")
        try:
            return json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CommissionError("Board artifact upload returned invalid JSON") from exc


def require(condition, message):
    if not condition:
        raise CommissionError(message)


def operator_credential(auth):
    machines = auth.get("machines")
    if not isinstance(machines, dict):
        raise CommissionError("local auth has no machines map")
    candidates = []
    for machine_id, machine in machines.items():
        if not isinstance(machine, dict) or not machine.get("enabled"):
            continue
        roles = machine.get("roles")
        if not isinstance(roles, list) or not ("operator" in roles or "admin" in roles):
            continue
        secret = machine.get("api_secret")
        if isinstance(secret, str) and secret:
            candidates.append((machine_id, secret))
    if not candidates:
        raise CommissionError("local auth has no enabled operator authority")
    candidates.sort(key=lambda item: item[0])
    return candidates[0]


def board_version(binary):
    safe_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    try:
        result = subprocess.run(
            [binary, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env=safe_env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CommissionError("candidate could not report its Board version") from exc
    value = result.stdout.strip()
    if not value or "\n" in value:
        raise CommissionError("candidate reported an invalid Board version")
    return value


def write_receipt(path, body):
    destination = Path(path)
    parent = destination.parent
    if not parent.is_dir() or destination.is_symlink():
        raise CommissionError(f"receipt destination is not a safe existing path: {path}")
    encoded = (json.dumps(body, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".commission-receipt-", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def disposition_failure(client, task_id, reason):
    if not task_id:
        return None
    safe_reason = "commissioning verification failed: " + reason[:240]
    for action in ("cancel", "fail"):
        try:
            _, task = client.request(
                "POST",
                f"/tasks/{task_id}/{action}",
                client.operator_token,
                body={"reason": safe_reason},
            )
            if isinstance(task, dict) and task.get("status") in ("cancelled", "failed"):
                return task.get("status")
        except CommissionError:
            continue
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint")
    parser.add_argument("--allow-http", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--record", required=True)
    parser.add_argument("--credential", required=True)
    parser.add_argument("--auth", required=True)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--failure-receipt", required=True)
    parser.add_argument("--fail-after-task", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    declaration_raw, declaration = load_validated_declaration(args.instance)
    record_raw, record = load_json(args.record)
    credential_raw = read_private(args.credential, 0o600)
    credential = credential_raw.decode("utf-8").strip()
    require(credential != "", "participant credential seam is empty")
    _, auth = load_json(args.auth, mode=0o600)
    require(isinstance(declaration, dict), "instance declaration is not an object")
    require(isinstance(record, dict), "canonical record is not an object")
    identity = declaration.get("board_identity")
    grant = declaration.get("grant")
    protected = declaration.get("protected")
    require(isinstance(identity, dict) and isinstance(grant, dict), "instance identity/grant is incomplete")
    require(isinstance(protected, dict), "instance protected seam is incomplete")
    employee_id = declaration.get("employee_id")
    agent_id = identity.get("agent_id")
    machine_id = identity.get("machine_id")
    require(isinstance(employee_id, str) and employee_id, "instance employee_id is missing")
    require(isinstance(agent_id, str) and agent_id, "instance agent_id is missing")
    require(isinstance(machine_id, str) and machine_id, "instance machine_id is missing")
    require(record.get("employee_id") == employee_id, "canonical record employee_id drift")
    require(record.get("board_identity") == identity, "canonical record Board binding drift")

    # The normal path is accepted only by instance-declaration.py. In
    # particular, HTTP is limited to literal loopback there; --allow-http
    # remains an explicit test-only override and cannot widen declarations.
    endpoint = resolve_endpoint(
        declaration, override=args.endpoint, allow_http=args.allow_http
    )
    operator_id, operator_token = operator_credential(auth)
    participant = BoardClient(endpoint, operator_token=operator_token, participant_token=credential)
    version = board_version(args.binary)
    started_at = now()
    task_id = ""
    lease_id = ""
    artifact_id = ""
    checks = {}

    try:
        participant.health()
        checks["board_health"] = True

        _, machines = participant.request("GET", "/machines", participant.operator_token)
        require(isinstance(machines, list), "operator machine readback was not a list")
        matching_machines = [
            item for item in machines if isinstance(item, dict) and item.get("id") == machine_id
        ]
        require(len(matching_machines) <= 1, "operator machine readback found duplicate machine ids")
        if matching_machines:
            existing_machine = matching_machines[0]
            machine_registration = {"id": machine_id}
            for field in ("hostname", "os", "arch", "capabilities"):
                require(field in existing_machine, f"existing machine readback omitted {field}")
                machine_registration[field] = existing_machine[field]
        else:
            machine_registration = {
                "id": machine_id,
                "hostname": "",
                "os": "",
                "arch": "",
                "capabilities": [],
            }
        _, registered_machine = participant.request(
            "POST",
            "/machines/register",
            participant.operator_token,
            body=machine_registration,
        )
        require(
            isinstance(registered_machine, dict) and registered_machine.get("id") == machine_id,
            "operator machine registration returned no machine id",
        )
        _, machines = participant.request("GET", "/machines", participant.operator_token)
        matching_machines = [
            item for item in machines if isinstance(item, dict) and item.get("id") == machine_id
        ]
        require(len(matching_machines) == 1, "operator could not read back the registered machine")
        machine_readback = matching_machines[0]
        for field, expected in machine_registration.items():
            require(machine_readback.get(field) == expected, f"registered machine {field} drift")
        checks["machine_registered"] = True

        participant.request(
            "POST",
            "/agents/register",
            participant.participant_token,
            body={
                "id": agent_id,
                "machine_id": machine_id,
                "kind": grant.get("agent_kind"),
                "role": grant.get("agent_role"),
                "capabilities": grant.get("agent_capabilities"),
            },
        )
        checks["participant_register"] = True

        _, agents = participant.request("GET", "/agents", participant.operator_token)
        require(isinstance(agents, list), "operator agent readback was not a list")
        matching = [item for item in agents if isinstance(item, dict) and item.get("id") == agent_id]
        require(len(matching) == 1, "operator could not read back the registered agent")
        registered = matching[0]
        require(registered.get("machine_id") == machine_id, "registered agent machine binding drift")
        require(registered.get("kind") == grant.get("agent_kind"), "registered agent kind drift")
        require(registered.get("role") == grant.get("agent_role"), "registered agent role drift")
        require(registered.get("capabilities") == grant.get("agent_capabilities"), "registered agent grant drift")
        checks["operator_binding_readback"] = True

        _, task = participant.request(
            "POST",
            "/tasks",
            participant.operator_token,
            body={
                "title": f"Commissioning verification: {employee_id}",
                "mission": "employee-zero-commissioning",
                "priority": "P1",
                "assigned_agent_id": agent_id,
                "required_capabilities": grant.get("agent_capabilities"),
                "machine_constraints": [machine_id],
                "instructions": "Claim this bounded task, upload the non-secret commissioning receipt, and submit it for operator review.",
            },
        )
        require(isinstance(task, dict) and task.get("id"), "operator task creation returned no task id")
        task_id = task["id"]
        require(task.get("assigned_agent_id") == agent_id, "commissioning task was not assigned to the participant")
        checks["task_created_assigned"] = True

        _, participant_task = participant.request("GET", f"/tasks/{task_id}", participant.participant_token)
        require(participant_task.get("id") == task_id and participant_task.get("assigned_agent_id") == agent_id, "participant could not read its assigned task")
        checks["participant_task_readback"] = True

        if args.fail_after_task:
            raise CommissionError("injected post-mutation verification failure")

        _, claimed = participant.request(
            "POST",
            "/poll",
            participant.participant_token,
            body={"agent_id": agent_id, "machine_id": machine_id, "lease_seconds": 120},
        )
        claimed_task = claimed.get("task") if isinstance(claimed, dict) else None
        lease = claimed.get("lease") if isinstance(claimed, dict) else None
        require(isinstance(claimed_task, dict) and claimed_task.get("id") == task_id, "participant did not claim the commissioning task")
        require(isinstance(lease, dict) and lease.get("id"), "participant claim returned no lease")
        lease_id = lease["id"]
        require(lease.get("agent_id") == agent_id and lease.get("machine_id") == machine_id, "lease binding drift")
        checks["claim_lease"] = True

        receipt = {
            "schema_version": "agent-board-commission-verification/1",
            "status": "passed",
            "employee_id": employee_id,
            "board_ref": identity.get("board_ref"),
            "principal_ref": identity.get("principal_ref"),
            "agent_id": agent_id,
            "machine_id": machine_id,
            "task_id": task_id,
            "lease_id": lease_id,
            "artifact_id": "pending",
            "operator": {"actor_type": "machine", "actor_id": operator_id},
            "participant": {"actor_type": "agent", "actor_id": agent_id},
            "record_digest": digest(record_raw),
            "declaration_digest": digest(declaration_raw),
            "board_version": version,
            "started_at": started_at,
            "receipt_at": now(),
            "checks": {
                "board_health": True,
                "registered_binding": True,
                "assigned_task": True,
                "claim_and_lease": True,
                "artifact_is_non_secret": True,
            },
        }
        receipt_bytes = (json.dumps(receipt, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        require(credential not in receipt_bytes.decode("utf-8"), "credential appeared in commissioning receipt")
        artifact = participant.upload(task_id, "non-secret commissioning verification receipt", receipt_bytes)
        require(isinstance(artifact, dict) and artifact.get("id"), "artifact upload returned no artifact id")
        artifact_id = artifact["id"]
        receipt["artifact_id"] = artifact_id
        checks["artifact_upload"] = True

        _, artifacts = participant.request("GET", f"/tasks/{task_id}/artifacts", participant.operator_token)
        found = [item for item in artifacts if isinstance(item, dict) and item.get("id") == artifact_id]
        require(len(found) == 1, "operator could not read back the commissioning artifact")
        _, downloaded = participant.request("GET", found[0]["path_or_url"], participant.operator_token, raw=True)
        require(downloaded == receipt_bytes, "operator artifact readback differs from uploaded receipt")
        checks["artifact_readback"] = True

        _, reviewed = participant.request(
            "POST",
            f"/tasks/{task_id}/review",
            participant.participant_token,
            body={"reason": "commissioning receipt uploaded; requesting operator review"},
        )
        require(reviewed.get("status") == "review", "participant review submission did not enter review")
        checks["review_submission"] = True

        _, review_task = participant.request("GET", f"/tasks/{task_id}", participant.operator_token)
        require(review_task.get("status") == "review", "operator readback did not observe review status")
        checks["operator_review_readback"] = True
        _, approved = participant.request(
            "POST",
            f"/tasks/{task_id}/approve",
            participant.operator_token,
            body={"reason": "commissioning receipt assertions passed"},
        )
        require(approved.get("status") == "review", "operator approval changed review status unexpectedly")
        checks["operator_approval"] = True
        _, completed = participant.request(
            "POST",
            f"/tasks/{task_id}/complete",
            participant.operator_token,
            body={"reason": "commissioning verification complete"},
        )
        require(completed.get("status") == "done", "operator completion did not finish the task")
        checks["operator_completion"] = True

        _, final_task = participant.request("GET", f"/tasks/{task_id}", participant.operator_token)
        require(final_task.get("status") == "done", "operator final task readback is not done")
        _, events = participant.request("GET", f"/tasks/{task_id}/events", participant.operator_token)
        required_events = {
            ("created", "machine", operator_id),
            ("claimed", "agent", agent_id),
            ("submitted_for_review", "agent", agent_id),
            ("review_approved", "machine", operator_id),
            ("completed", "machine", operator_id),
        }
        observed = {(item.get("event_type"), item.get("actor_type"), item.get("actor_id")) for item in events if isinstance(item, dict)}
        require(required_events.issubset(observed), "commissioning task history lost actor attribution")
        checks["attributed_history"] = True

        wrong_token = base64.urlsafe_b64encode(secrets.token_bytes(24)).decode("ascii")
        wrong_status, _ = participant.request(
            "POST",
            "/agents/register",
            wrong_token,
            body={"id": agent_id, "machine_id": machine_id},
            expected=(401,),
        )
        require(wrong_status == 401, "wrong credential was accepted")
        wrong_principal_status, _ = participant.request(
            "POST",
            "/agents/register",
            participant.participant_token,
            body={"id": agent_id + "-other", "machine_id": machine_id},
            expected=(403,),
        )
        require(wrong_principal_status == 403, "wrong principal was accepted")
        # The probe agent must exist for the Board to accept an assignment to it;
        # the operator registers it on the same physical machine. It is a
        # synthetic, isolated negative fixture, never a commissioned employee.
        participant.request(
            "POST",
            "/agents/register",
            participant.operator_token,
            body={
                "id": agent_id + "-other",
                "machine_id": machine_id,
                "kind": "denial-probe",
                "role": "producer",
                "capabilities": [],
            },
        )
        _, cross_task = participant.request(
            "POST",
            "/tasks",
            participant.operator_token,
            body={
                "title": "Commissioning cross-identity denial probe",
                "mission": "employee-zero-commissioning-negative",
                "assigned_agent_id": agent_id + "-other",
                "instructions": "This task is an isolated denial probe and must not be claimed.",
            },
        )
        cross_task_id = cross_task.get("id") if isinstance(cross_task, dict) else ""
        require(cross_task_id, "cross-identity denial probe has no task id")
        cross_status, _ = participant.request(
            "GET", f"/tasks/{cross_task_id}", participant.participant_token, expected=(404,)
        )
        require(cross_status == 404, "participant read crossed an identity boundary")
        participant.request(
            "POST", f"/tasks/{cross_task_id}/cancel", participant.operator_token,
            body={"reason": "cross-identity denial probe complete"},
        )
        checks["wrong_credential_principal_cross_identity_denied"] = True

        receipt["checks"].update(checks)
        receipt["receipt_at"] = now()
        write_receipt(args.receipt, receipt)
        print(f"commissioning verification passed for {employee_id}; task and artifact ids recorded")
        return 0
    except Exception as exc:
        # Unexpected parser/type failures still need the same fail-closed task
        # disposition. Only the exception class is retained for this branch so
        # a future library error can never echo a protected value.
        reason = str(exc) if isinstance(exc, CommissionError) else f"unexpected verifier error: {type(exc).__name__}"
        disposition = disposition_failure(participant, task_id, reason)
        failure = {
            "schema_version": "agent-board-commission-failure/1",
            "status": "failed",
            "employee_id": employee_id,
            "agent_id": agent_id,
            "machine_id": machine_id,
            "task_id": task_id or None,
            "lease_id": lease_id or None,
            "artifact_id": artifact_id or None,
            "record_digest": digest(record_raw),
            "declaration_digest": digest(declaration_raw),
            "board_version": version,
            "failed_at": now(),
            "reason": reason,
            "task_disposition": disposition or "unknown",
            "checks": checks,
        }
        try:
            write_receipt(args.failure_receipt, failure)
        except CommissionError:
            # Preserve the original failure as the process result. The caller's
            # rollback diagnostic remains the source of truth if this path is
            # unavailable, and no secret is emitted here.
            pass
        print("commissioning verification failed; non-secret failure receipt retained", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
