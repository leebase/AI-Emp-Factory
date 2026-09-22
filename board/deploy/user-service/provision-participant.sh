#!/bin/sh
# Board-owned, supported provisioner for ONE declared employee instance.
#
# It reads a validated instance declaration, a newly generated protected
# credential file and the canonical activated DeploymentRecord, then atomically
# adds the exact accepted local-auth Machine schema. Identity, grant and
# destination come only from the operator-owned declaration and the canonical
# record; nothing is derived from a participant or from a role-specific
# constant. The credential is never placed in argv, the environment, logs, or
# output. No manual auth-file editing is supported.
#
# Usage: provision-participant.sh --instance PATH --auth-config-file PATH \
#          --credential-file PATH --record-file PATH [--execute]
#
# --credential-file/--record-file are optional; when given they must match the
# declaration exactly, so an operator cannot substitute another instance's
# secret or record.

set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=lib.sh
. "$here/lib.sh"

INSTANCE=""
AUTH_FILE=""
CREDENTIAL_FILE=""
RECORD_FILE=""
EXECUTE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --instance) INSTANCE="$2"; shift 2 ;;
    --auth-config-file) AUTH_FILE="$2"; shift 2 ;;
    --credential-file) CREDENTIAL_FILE="$2"; shift 2 ;;
    --record-file) RECORD_FILE="$2"; shift 2 ;;
    --execute) EXECUTE=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -n "$INSTANCE" ] || die "--instance is required"
[ -n "$AUTH_FILE" ] || die "--auth-config-file is required"
require_cmd python3

# Read the declaration through the validator; refuse everything it refuses.
EMPLOYEE_ID=""
DECLARED_CREDENTIAL=""
DECLARED_RECORD=""
declaration_export="$(python3 "$here/instance-declaration.py" export --instance "$INSTANCE")" || \
  die "instance declaration refused: $INSTANCE"
while IFS='	' read -r key value; do
  case "$key" in
    EMPLOYEE_ID) EMPLOYEE_ID="$value" ;;
    CREDENTIAL_PATH) DECLARED_CREDENTIAL="$value" ;;
    RECORD_PATH) DECLARED_RECORD="$value" ;;
  esac
done <<EOF
$declaration_export
EOF
[ -n "$EMPLOYEE_ID" ] || die "instance declaration did not resolve an employee_id"

[ -n "$CREDENTIAL_FILE" ] || CREDENTIAL_FILE="$DECLARED_CREDENTIAL"
[ -n "$RECORD_FILE" ] || RECORD_FILE="$DECLARED_RECORD"
[ "$CREDENTIAL_FILE" = "$DECLARED_CREDENTIAL" ] || \
  die "credential file does not match the declared instance path"
[ "$RECORD_FILE" = "$DECLARED_RECORD" ] || \
  die "record file does not match the declared instance path"

[ -f "$AUTH_FILE" ] || die "auth config file not found: $AUTH_FILE"
[ -f "$CREDENTIAL_FILE" ] || die "credential file not found: $CREDENTIAL_FILE"
[ -f "$RECORD_FILE" ] || die "deployment record not found: $RECORD_FILE"
[ ! -L "$AUTH_FILE" ] || die "auth config file must not be a symlink"
[ ! -L "$CREDENTIAL_FILE" ] || die "credential file must not be a symlink"
[ ! -L "$RECORD_FILE" ] || die "deployment record must not be a symlink"
[ "$(file_mode "$AUTH_FILE")" = "600" ] || die "auth config file must be mode 0600"
[ "$(file_mode "$CREDENTIAL_FILE")" = "600" ] || die "credential file must be mode 0600"

printf 'participant provision plan\n'
printf '  instance     %s (%s)\n' "$INSTANCE" "$EMPLOYEE_ID"
printf '  auth config  %s\n' "$AUTH_FILE"
printf '  credential   %s (value withheld)\n' "$CREDENTIAL_FILE"
printf '  record       %s\n' "$RECORD_FILE"

if [ "$EXECUTE" != 1 ]; then
  printf 'dry-run: no auth file written. Re-run with --execute.\n'
  exit 0
fi

python3 - "$AUTH_FILE" "$CREDENTIAL_FILE" "$RECORD_FILE" "$INSTANCE" "$here" <<'PY'
import hashlib
import json
import os
import stat
import sys

import importlib.util

# Do not leave a __pycache__ tree beside the operator scripts.
sys.dont_write_bytecode = True

auth_path, credential_path, record_path, instance_path, here = sys.argv[1:6]

spec = importlib.util.spec_from_file_location(
    "instance_declaration", os.path.join(here, "instance-declaration.py")
)
instance_declaration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(instance_declaration)

declaration = instance_declaration.load(instance_path)
employee_id = declaration["employee_id"]
identity = declaration["board_identity"]
grant = declaration["grant"]
active_states = set(instance_declaration.ACTIVATION_TARGETS)


def open_checked(path, *, mode=None, owner=True):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit(f"protected input is not a regular file: {path}")
        if mode is not None and stat.S_IMODE(info.st_mode) != mode:
            raise SystemExit(f"protected input has wrong mode: {path}")
        if owner and info.st_uid != os.getuid():
            raise SystemExit(f"protected input is not owned by this user: {path}")
        return fd
    except Exception:
        os.close(fd)
        raise


def read_checked(path, *, mode=None):
    fd = open_checked(path, mode=mode)
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


credential = read_checked(credential_path, mode=0o600).decode("utf-8").strip()
if not credential:
    raise SystemExit("credential file is empty")

record_raw = read_checked(record_path)
try:
    record = json.loads(record_raw)
except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"deployment record is not valid JSON: {exc}") from exc
if record_raw != canonical_json(record):
    raise SystemExit("deployment record is not canonical JSON")
if record.get("lifecycle_state") not in active_states:
    raise SystemExit("participant record must be active (pilot or scheduled)")
try:
    # Employee id, display name, the complete four-field binding and each
    # declared reference must all agree with the canonical record. A partial
    # canonical identity or a foreign binding refuses here.
    instance_declaration.verify_record(
        declaration, record_path, expect_state=record.get("lifecycle_state")
    )
except instance_declaration.DeclarationError as exc:
    raise SystemExit(f"declared instance does not match the canonical record: {exc}") from exc
record_digest = "sha256:" + hashlib.sha256(record_raw).hexdigest()

auth_raw = read_checked(auth_path, mode=0o600)
try:
    auth = json.loads(auth_raw)
except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"auth config is not valid JSON: {exc}") from exc
if not isinstance(auth, dict) or not isinstance(auth.get("machines"), dict):
    raise SystemExit("auth config must contain a machines object")
if not isinstance(auth.get("users"), dict) or not auth.get("session_secret"):
    raise SystemExit("auth config must be a complete local-auth snapshot")
principal_ref = identity["principal_ref"]
if principal_ref in auth["machines"]:
    raise SystemExit(
        f"participant auth entry already exists for {principal_ref}; refusing overwrite"
    )
for machine_key, machine in auth["machines"].items():
    if not isinstance(machine, dict):
        continue
    if machine.get("api_secret") == credential:
        raise SystemExit(f"credential collides with existing machine {machine_key}")
    # Two enabled participants may never share an employee, board_ref or agent
    # id: that would make actor-attributed Board history ambiguous.
    if not machine.get("participant"):
        continue
    for field, value in (
        ("employee_id", employee_id),
        ("board_ref", identity["board_ref"]),
        ("agent_id", identity["agent_id"]),
    ):
        if machine.get(field) == value:
            raise SystemExit(
                f"{field} {value!r} is already bound to participant {machine_key}"
            )

# This is the source auth.Machine schema. principal_ref is deliberately not an
# auth property: the map key is the canonical principal_ref and is passed to
# the Board binder as principalID.
auth["machines"][principal_ref] = {
    "name": declaration["display_name"],
    "roles": ["participant"],
    "enabled": True,
    "api_secret": credential,
    "participant": True,
    "employee_id": employee_id,
    "board_ref": identity["board_ref"],
    "agent_id": identity["agent_id"],
    "machine_id": identity["machine_id"],
    "record_digest": record_digest,
    "agent_kind": grant["agent_kind"],
    "agent_role": grant["agent_role"],
    "agent_capabilities": list(grant["agent_capabilities"]),
}

directory = os.path.dirname(os.path.abspath(auth_path))
if not os.path.isdir(directory) or os.path.islink(directory):
    raise SystemExit("auth config parent must be a real directory")
directory_info = os.stat(directory)
if directory_info.st_uid != os.getuid() or stat.S_IMODE(directory_info.st_mode) & 0o077:
    raise SystemExit("auth config parent must be owned by this user and private")
temporary = auth_path + ".participant.tmp"
try:
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(fd, "wb") as handle:
        handle.write(canonical_json(auth))
        handle.write(b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, auth_path)
    os.chmod(auth_path, 0o600)
except Exception:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise

info = os.stat(auth_path)
if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
    raise SystemExit("provisioned auth file failed protection checks")
print(f"participant auth provisioned for {employee_id}; credential value withheld")
PY

printf 'participant provision complete for %s\n' "$EMPLOYEE_ID"
