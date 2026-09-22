#!/bin/sh
# One coherent staged cutover for the ACTUAL agent-board user service, and the
# single generic "commission this employee instance" operation.
#
# Dry-run by default. In instance-commissioning mode the operation backs up
# every live artifact before mutation (including absence metadata), activates
# the declared employee's canonical record through the Factory's generic
# lifecycle transition, generates a fresh protected per-instance credential,
# provisions the exact auth.Machine entry, writes the declared endpoint packet,
# updates the user environment, installs the reviewed binary, and performs one
# daemon-reload/restart. Board loads the active registry and auth snapshot in
# that same restart. Any later failure invokes verified rollback; a rollback
# failure is a distinct fatal exit and is never downgraded to a warning.
#
# Every instance-specific value -- employee id, board/principal/agent/machine
# binding, capability grant, lifecycle target, canonical record/evidence paths
# and protected credential/endpoint paths -- comes from one validated
# declaration (see instance-declaration.py and instances/). There is no
# employee-specific branch, constant or activation function anywhere below.
#
# Usage: cutover.sh --candidate PATH --auth-config-file PATH [--staged-auth PATH]
#                   [--instance PATH --factory-root PATH]
#                   [--backup DIR] [--unit PATH] [--activation-actor REF]
#                   [--activation-reason TEXT] [--execute]
#                   [--artifact-dir PATH]
#                   [--expected-pid PID]

set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=lib.sh
. "$here/lib.sh"

UNIT="$(default_unit)"
CANDIDATE=""
AUTH_FILE=""
STAGED_AUTH=""
INSTANCE=""
FACTORY_ROOT=""
ARTIFACT_DIR=""
EXPECTED_PID=""
ACTIVATION_ACTOR="principal:lee"
ACTIVATION_REASON=""
BACKUP_DIR=""
EXECUTE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --unit) UNIT="$2"; shift 2 ;;
    --candidate) CANDIDATE="$2"; shift 2 ;;
    --auth-config-file) AUTH_FILE="$2"; shift 2 ;;
    --staged-auth) STAGED_AUTH="$2"; shift 2 ;;
    --instance) INSTANCE="$2"; shift 2 ;;
    --factory-root) FACTORY_ROOT="$2"; shift 2 ;;
    --artifact-dir) ARTIFACT_DIR="$2"; shift 2 ;;
    --expected-pid) EXPECTED_PID="$2"; shift 2 ;;
    --activation-actor) ACTIVATION_ACTOR="$2"; shift 2 ;;
    --activation-reason) ACTIVATION_REASON="$2"; shift 2 ;;
    --backup) BACKUP_DIR="$2"; shift 2 ;;
    --execute) EXECUTE=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -f "$UNIT" ] || die "user unit not found: $UNIT"
[ -n "$CANDIDATE" ] || die "--candidate is required"
[ -n "$AUTH_FILE" ] || die "--auth-config-file is required"

BINARY="$(unit_exec_start "$UNIT")"
ENV_FILE="$(unit_environment_file "$UNIT")"
DB="$(unit_env_value "$UNIT" AGENT_BOARD_DB)"
ARTIFACT_DIR="${ARTIFACT_DIR:-$(dirname -- "$DB")/agent-board-artifacts}"
BIND_HOST="$(unit_env_value "$UNIT" AGENT_BOARD_BIND_HOST)"
PORT="$(unit_env_value "$UNIT" AGENT_BOARD_PORT)"
[ -n "$BIND_HOST" ] && [ "$BIND_HOST" != "0.0.0.0" ] || BIND_HOST="127.0.0.1"
[ -n "$PORT" ] || PORT=8787
SERVICE="$(unit_service_name "$UNIT")"
BACKUP_DIR="${BACKUP_DIR:-$HOME/.local/share/agent-board-backups/$(date -u +%Y%m%dT%H%M%SZ)}"

require_cmd python3

EMPLOYEE_ID=""
REGISTRY=""
REGISTRY_RECORD=""
ACTIVATION_EVIDENCE=""
CREDENTIAL_FILE=""
ENDPOINT_FILE=""
ENDPOINT_URL=""
LIFECYCLE_FROM=""
LIFECYCLE_TARGET=""

PARTICIPANT_MODE=0
if [ -n "$INSTANCE" ]; then
  PARTICIPANT_MODE=1
  [ -n "$FACTORY_ROOT" ] || die "instance commissioning requires --factory-root"
  [ -f "$FACTORY_ROOT/scripts/employee_registry.py" ] || die "Factory registry operation not found"
  declaration_export="$(python3 "$here/instance-declaration.py" export --instance "$INSTANCE")" || \
    die "instance declaration refused: $INSTANCE"
  while IFS='	' read -r key value; do
    case "$key" in
      EMPLOYEE_ID) EMPLOYEE_ID="$value" ;;
      RECORDS_DIR) REGISTRY="$value" ;;
      RECORD_PATH) REGISTRY_RECORD="$value" ;;
      EVIDENCE_PATH) ACTIVATION_EVIDENCE="$value" ;;
      CREDENTIAL_PATH) CREDENTIAL_FILE="$value" ;;
      ENDPOINT_PATH) ENDPOINT_FILE="$value" ;;
      ENDPOINT_URL) ENDPOINT_URL="$value" ;;
      LIFECYCLE_FROM) LIFECYCLE_FROM="$value" ;;
      LIFECYCLE_TARGET) LIFECYCLE_TARGET="$value" ;;
    esac
  done <<EOF
$declaration_export
EOF
  [ -n "$EMPLOYEE_ID" ] && [ -n "$REGISTRY_RECORD" ] && [ -n "$LIFECYCLE_TARGET" ] || \
    die "instance declaration did not resolve the required commissioning inputs"
  ACTIVATION_REASON="${ACTIVATION_REASON:-reviewed generic commissioning of employee instance $EMPLOYEE_ID}"
elif [ -n "$FACTORY_ROOT" ]; then
  die "--factory-root requires --instance"
fi

# The reachability gate is before even the read-only filesystem plan: an
# unreachable commissioning authority must stop the transaction at its
# declared external boundary, before any backup or local mutation.
if [ "$EXECUTE" = 1 ]; then
  require_cmd curl
  if [ "$PARTICIPANT_MODE" = 1 ]; then
    board_health_url="${ENDPOINT_URL%/}/health"
    board_health_code="$(curl -fsS --max-time 5 -o /dev/null -w '%{http_code}' \
      -H 'User-Agent: agent-board-factory-client/2' "$board_health_url" 2>/dev/null || true)"
    [ "$board_health_code" = 200 ] || \
      die "Board reachability precheck failed; no commissioning mutation was performed"
    printf '  Board reachability precheck: health 200 (no mutation yet)\n'
  fi
fi

# Inspect the complete destination set before creating even the backup directory.
# Missing future staging is reported as pending (exit2), never synthetic readiness.
fs_status=0
FS_PLAN="$(python3 "$here/filesystem-preflight.py" plan \
  --unit "$UNIT" --binary "$BINARY" --env "$ENV_FILE" --db "$DB" \
  --candidate "$CANDIDATE" --auth "$AUTH_FILE" --staged "$STAGED_AUTH" \
  --record "$REGISTRY_RECORD" --evidence "$ACTIVATION_EVIDENCE" \
  --credential "$CREDENTIAL_FILE" --endpoint "$ENDPOINT_FILE" \
  --artifacts "$ARTIFACT_DIR" --backup "$BACKUP_DIR")" || fs_status=$?
printf '%s\n' "$FS_PLAN" | python3 "$here/filesystem-preflight.py" report
if [ -n "$EXPECTED_PID" ]; then
  actual_pid="$(systemctl --user show "$SERVICE" -p MainPID --value)"
  printf '  service PID: observed=%s expected=%s\n' "$actual_pid" "$EXPECTED_PID"
  [ "$actual_pid" = "$EXPECTED_PID" ] || die "service PID changed; reconcile the actual baseline"
fi
[ "$fs_status" = 0 ] || exit "$fs_status"

if [ "$PARTICIPANT_MODE" = 1 ]; then
  # Resolve missing final paths and symlinked ancestors before the plan is
  # accepted. Evidence must never enter the direct *.json record namespace.
  python3 - "$REGISTRY" "$REGISTRY_RECORD" "$ACTIVATION_EVIDENCE" <<'PY'
import os
import sys

registry, record, evidence = (os.path.realpath(path) for path in sys.argv[1:])
if not os.path.isabs(registry) or not os.path.isabs(record) or not os.path.isabs(evidence):
    raise SystemExit("registry, record, and activation evidence must be absolute paths")
if os.path.dirname(record) != registry:
    raise SystemExit("canonical record must be a direct child of the registry directory")
if evidence == registry or evidence.startswith(registry + os.sep):
    raise SystemExit("activation evidence must be outside the direct registry record scan")
if not evidence.endswith(".json"):
    raise SystemExit("activation evidence must name a JSON evidence file")
PY
  # Prove the canonical record already carries the declared complete binding and
  # is still in the declared pre-activation state, BEFORE anything is mutated.
  python3 "$here/instance-declaration.py" verify-record \
    --instance "$INSTANCE" --record "$REGISTRY_RECORD" \
    --expect-state "$LIFECYCLE_FROM" > /dev/null || \
    die "canonical record does not match the declared instance"
fi

printf 'cutover plan\n'
printf '  unit           %s\n' "$UNIT"
printf '  service        %s\n' "$SERVICE"
printf '  binary         %s -> %s\n' "$BINARY" "$CANDIDATE"
printf '  env file       %s\n' "$ENV_FILE"
printf '  auth config    %s\n' "$AUTH_FILE"
printf '  registry       %s\n' "${REGISTRY:-<unchanged>}"
printf '  database       %s\n' "$DB"
printf '  artifacts      %s\n' "$ARTIFACT_DIR"
printf '  backup         %s\n' "$BACKUP_DIR"
printf '  verify         http://%s:%s/health (User-Agent agent-board-factory-client/2)\n' "$BIND_HOST" "$PORT"
if [ "$PARTICIPANT_MODE" = 1 ]; then
  printf '  instance       %s (%s)\n' "$INSTANCE" "$EMPLOYEE_ID"
  printf '  record         %s (%s -> %s before restart)\n' "$REGISTRY_RECORD" "$LIFECYCLE_FROM" "$LIFECYCLE_TARGET"
  printf '  evidence       %s\n' "$ACTIVATION_EVIDENCE"
  printf '  credential     %s (value withheld; newly generated after backup)\n' "$CREDENTIAL_FILE"
  printf '  endpoint       %s\n' "$ENDPOINT_FILE"
  python3 "$here/instance-declaration.py" report --instance "$INSTANCE" | sed 's/^/  /'
fi

if [ "$EXECUTE" != 1 ]; then
  printf 'dry-run: no change. Re-run with --execute only in the reviewed window.\n'
  exit 0
fi

systemctl_user_available || die "systemctl --user is unavailable; cannot cut over safely"

ROLLBACK_ARMED=0
CREDENTIAL_TMP=""
VERIFY_STARTED=0
preserve_failed_board_state() {
  receipt="$BACKUP_DIR/commissioning-failure.json"
  [ -f "$receipt" ] || return 0
  if ! python3 - "$receipt" <<'PY'
import json
import sys

body = json.load(open(sys.argv[1], encoding="utf-8"))
if body.get("status") != "failed":
    raise SystemExit(1)
if body.get("task_id") and body.get("task_disposition") not in ("cancelled", "failed"):
    raise SystemExit(1)
PY
  then
    return 1
  fi
  # The verifier has already canceled/failed the task through the supported
  # API. Refresh only the private backup snapshot through the existing
  # read-only SQLite online-backup tool, then reseal its two hashes. Rollback
  # will therefore restore the old files and the dispositioned Board task,
  # instead of deleting the task by restoring a pre-task database snapshot.
  refreshed="$BACKUP_DIR/db/agent-board.db.failed"
  [ ! -e "$refreshed" ] && [ ! -L "$refreshed" ] || return 1
  "$here/sqlite-online-backup.py" backup --source "$DB" --destination "$refreshed" >/dev/null || return 1
  mv -- "$refreshed" "$BACKUP_DIR/db/agent-board.db" || return 1
  if ! python3 - "$BACKUP_DIR" <<'PY'
import hashlib
import os
from pathlib import Path
import tempfile
import sys

backup = Path(sys.argv[1])
database = backup / "db/agent-board.db"
database_hash = hashlib.sha256(database.read_bytes()).hexdigest()

manifest = backup / "manifest.sha256"
lines = manifest.read_text(encoding="utf-8").splitlines()
updated = []
found = False
for line in lines:
    value, separator, rel = line.partition("  ")
    if separator and rel == "db/agent-board.db":
        value = database_hash
        found = True
    updated.append(value + separator + rel if separator else line)
if not found:
    raise SystemExit("database entry missing from backup manifest")

def replace_private(path, data):
    fd, temporary = tempfile.mkstemp(prefix=".failed-state-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

replace_private(manifest, "\n".join(updated) + "\n")
manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
seal = backup / "backup.complete"
seal_lines = []
for line in seal.read_text(encoding="utf-8").splitlines():
    key, separator, value = line.partition("=")
    if key == "manifest_sha256":
        value = manifest_hash
    elif key == "database_sha256":
        value = database_hash
    seal_lines.append(key + separator + value)
replace_private(seal, "\n".join(seal_lines) + "\n")
PY
  then
    return 1
  fi
}
on_exit() {
  status=$?
  trap - EXIT HUP INT TERM
  [ -z "$CREDENTIAL_TMP" ] || rm -f -- "$CREDENTIAL_TMP"
  if [ "$ROLLBACK_ARMED" = 1 ]; then
    log "cutover failed after backup; invoking verified rollback"
  # Board writes are durable audit history. If the dispositioned commissioning
  # task cannot be preserved into the sealed backup, rollback MUST NOT run:
  # restoring the pre-task snapshot would delete that history (D345 point 4).
  # Leave the mutated service and the backup in place for operator
  # reconciliation and report the condition instead.
  # The gate is whether Board verification was ever STARTED, not whether a
  # receipt file exists: a task can be created and dispositioned on the Board
  # and the local receipt write can still fail afterwards. Once verification
  # started, a missing receipt means the Board state is unknown, and rollback
  # is withheld rather than risk restoring a pre-task snapshot.
  if [ "$VERIFY_STARTED" = 1 ]; then
    if [ ! -f "$BACKUP_DIR/commissioning-failure.json" ]; then
      log "FATAL: Board verification started but no failure receipt exists; Board task state is unknown, rollback withheld so Board audit history is not deleted. Operator reconciliation required: $BACKUP_DIR"
      exit 70
    fi
    if ! preserve_failed_board_state; then
      log "FATAL: could not preserve the dispositioned Board task; rollback withheld so Board audit history is not deleted. Operator reconciliation required: $BACKUP_DIR"
      exit 70
    fi
  fi
  rollback_status=0
  "$here/rollback.sh" --unit "$UNIT" --backup "$BACKUP_DIR" --execute || rollback_status=$?
  if [ "$rollback_status" = 0 ]; then
    if [ -f "$BACKUP_DIR/commissioning-failure.json" ]; then
      log "non-secret commissioning failure receipt retained: $BACKUP_DIR/commissioning-failure.json"
    fi
    exit "$status"
  fi
  log "FATAL: verified rollback failed with exit $rollback_status; service state is not accepted"
  exit 70
  fi
  exit "$status"
}
trap on_exit EXIT HUP INT TERM

if [ "$PARTICIPANT_MODE" = 1 ]; then
  printf '%s\n' "$FS_PLAN" | "$here/backup.sh" --unit "$UNIT" --out "$BACKUP_DIR" \
    --auth-config-file "$AUTH_FILE" \
    --registry-record "$REGISTRY_RECORD" \
    --activation-evidence "$ACTIVATION_EVIDENCE" \
    --credential-file "$CREDENTIAL_FILE" \
    --endpoint-file "$ENDPOINT_FILE" --artifact-dir "$ARTIFACT_DIR" --filesystem-plan-stdin --execute
else
  printf '%s\n' "$FS_PLAN" | "$here/backup.sh" --unit "$UNIT" --out "$BACKUP_DIR" \
    --auth-config-file "$AUTH_FILE" --artifact-dir "$ARTIFACT_DIR" --filesystem-plan-stdin --execute
fi
ROLLBACK_ARMED=1
python3 "$here/filesystem-preflight.py" apply --plan-file "$BACKUP_DIR/filesystem-plan.json"

if [ "$PARTICIPANT_MODE" = 1 ]; then
  # The Factory operation is the only activation authority. Its output is
  # non-secret and retained beside the backup for review, never in a model log.
  python3 "$FACTORY_ROOT/scripts/employee_registry.py" activate \
    --records-dir "$REGISTRY" \
    --employee "$EMPLOYEE_ID" --to "$LIFECYCLE_TARGET" \
    --actor "$ACTIVATION_ACTOR" --reason "$ACTIVATION_REASON" \
    --evidence-out "$ACTIVATION_EVIDENCE" --execute \
    > "$BACKUP_DIR/activation-result.json"

  if [ -n "$STAGED_AUTH" ]; then
    install -m 0600 "$STAGED_AUTH" "$AUTH_FILE"
  fi

  credential_parent="$(dirname -- "$CREDENTIAL_FILE")"
  if [ -L "$credential_parent" ] || [ -e "$credential_parent" ] && [ ! -d "$credential_parent" ]; then
    die "credential parent is not a real directory"
  fi
  CREDENTIAL_TMP="$CREDENTIAL_FILE.tmp"
  [ ! -e "$CREDENTIAL_TMP" ] || die "credential temporary path already exists"
  umask 077
  "$CANDIDATE" auth generate-machine-secret > "$CREDENTIAL_TMP" 2> "$BACKUP_DIR/credential-generator.stderr"
  chmod 0600 "$CREDENTIAL_TMP"
  [ "$(file_mode "$CREDENTIAL_TMP")" = "600" ] || die "generated credential is not mode 0600"
  mv -- "$CREDENTIAL_TMP" "$CREDENTIAL_FILE"
  CREDENTIAL_TMP=""

  "$here/provision-participant.sh" \
    --instance "$INSTANCE" \
    --auth-config-file "$AUTH_FILE" \
    --credential-file "$CREDENTIAL_FILE" \
    --record-file "$REGISTRY_RECORD" --execute

  endpoint_parent="$(dirname -- "$ENDPOINT_FILE")"
  if [ -L "$endpoint_parent" ] || [ -e "$endpoint_parent" ] && [ ! -d "$endpoint_parent" ]; then
    die "endpoint parent is not a real directory"
  fi
  python3 - "$here/instance-declaration.py" "$INSTANCE" "$ENDPOINT_FILE" "$CREDENTIAL_FILE" "$EMPLOYEE_ID" <<'PY'
import importlib.util
import json
import os
import stat
import sys

validator_path, instance_path, endpoint_path, credential_path, employee_id = sys.argv[1:]
spec = importlib.util.spec_from_file_location("commission_instance_declaration", validator_path)
if spec is None or spec.loader is None:
    raise SystemExit("instance declaration validator is unavailable")
validator = importlib.util.module_from_spec(spec)
previous_bytecode_setting = sys.dont_write_bytecode
try:
    sys.dont_write_bytecode = True
    spec.loader.exec_module(validator)
finally:
    sys.dont_write_bytecode = previous_bytecode_setting
declaration = validator.load(instance_path)
protected = declaration["protected"]
if (
    declaration["employee_id"] != employee_id
    or protected["endpoint_path"] != endpoint_path
    or protected["credential_path"] != credential_path
):
    raise SystemExit("validated endpoint packet inputs drifted from the declaration")
board_url = protected["endpoint_url"]
parent = os.path.dirname(os.path.abspath(endpoint_path))
info = os.stat(parent)
if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
    raise SystemExit("endpoint parent must be an owned mode-0700 directory")
body = {
    "board_url": board_url.rstrip("/"),
    "credential_ref": credential_path,
    "employee_id": employee_id,
}
temporary = endpoint_path + ".tmp"
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(body, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, endpoint_path)
except Exception:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
os.chmod(endpoint_path, 0o600)
print("endpoint packet written; credential reference withheld from output")
PY
fi

install -m 0755 "$CANDIDATE" "$BINARY"

REGISTRY_ARG=""
[ -n "$REGISTRY" ] && REGISTRY_ARG="AGENT_BOARD_EMPLOYEE_REGISTRY_DIR=$REGISTRY"
python3 - "$ENV_FILE" "AGENT_BOARD_AUTH_CONFIG_FILE=$AUTH_FILE" "$REGISTRY_ARG" "AGENT_BOARD_ARTIFACT_DIR=$ARTIFACT_DIR" <<'PY'
import os
import stat
import sys

path, pairs = sys.argv[1], sys.argv[2:]
fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
info = os.fstat(fd)
if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
    os.close(fd)
    raise SystemExit("env file must be an owned regular mode-0600 file")
with os.fdopen(fd, "r", encoding="utf-8") as handle:
    lines = handle.read().splitlines()
updates = dict(pair.split("=", 1) for pair in pairs if pair)
seen = set()
out = []
for line in lines:
    key = line.split("=", 1)[0].strip() if "=" in line else None
    if key == "AGENT_BOARD_TOKEN":
        # bootstrap-auth migrated this secret into the protected auth file.
        # Do not keep exporting it into the new service environment.
        continue
    if key in updates:
        out.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
tmp = path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    handle.write("\n".join(out) + "\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(tmp, path)
os.chmod(path, 0o600)
PY

systemctl --user daemon-reload
systemctl --user restart "$SERVICE"
wait_for_healthy "$BIND_HOST" "$PORT" "$SERVICE" 30 || die "cutover health verification failed"
if [ "$PARTICIPANT_MODE" = 1 ]; then
  # Local service health is necessary but never sufficient. This helper uses
  # the protected participant credential to register, reads the exact binding
  # back as the operator, claims an operator-assigned task, uploads a
  # non-secret receipt, submits review, and waits for operator approval and
  # completion. It writes its success/failure receipt in the sealed backup so
  # rollback cannot erase the evidence.
  VERIFY_STARTED=1
  python3 "$here/commission-verify.py" \
    --instance "$INSTANCE" --record "$REGISTRY_RECORD" \
    --credential "$CREDENTIAL_FILE" --auth "$AUTH_FILE" \
    --binary "$CANDIDATE" \
    --receipt "$BACKUP_DIR/commissioning-verification.json" \
    --failure-receipt "$BACKUP_DIR/commissioning-failure.json"
fi
ROLLBACK_ARMED=0
printf 'cutover complete; active healthy service verified. Backup: %s\n' "$BACKUP_DIR"
