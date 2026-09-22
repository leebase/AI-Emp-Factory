#!/bin/sh
# Coherent backup of the ACTUAL agent-board user service using supported tools.
#
# Backs up the reviewed binary, the user unit, the protected environment and
# auth files (with their modes preserved), and the SQLite database via the
# standard-runtime SQLite online backup API. An integrated Clerk cutover may
# also name the canonical record, activation evidence, credential, and endpoint
# packet. Every named artifact gets a manifest row, including an explicit
# ABSENT row, so rollback can remove artifacts created by the failed operation.
#
# Dry-run by default. Pass --execute to perform the source-read-only backup.
# It never prints secret values.
#
# Usage: backup.sh [--unit PATH] --out DIR [--auth-config-file PATH]
#                 [--registry-record PATH] [--activation-evidence PATH]
#                 [--credential-file PATH] [--endpoint-file PATH] [--execute]
#                 [--artifact-dir PATH]
#                 [--filesystem-plan-stdin] (cutover's validated metadata plan)

set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=lib.sh
. "$here/lib.sh"

UNIT="$(default_unit)"
OUT=""
AUTH_OVERRIDE=""
REGISTRY_RECORD=""
ACTIVATION_EVIDENCE=""
CREDENTIAL_FILE=""
ENDPOINT_FILE=""
ARTIFACT_DIR=""
FILESYSTEM_PLAN=0
EXECUTE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --unit) UNIT="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --auth-config-file) AUTH_OVERRIDE="$2"; shift 2 ;;
    --registry-record) REGISTRY_RECORD="$2"; shift 2 ;;
    --activation-evidence) ACTIVATION_EVIDENCE="$2"; shift 2 ;;
    --credential-file) CREDENTIAL_FILE="$2"; shift 2 ;;
    --endpoint-file) ENDPOINT_FILE="$2"; shift 2 ;;
    --artifact-dir) ARTIFACT_DIR="$2"; shift 2 ;;
    --filesystem-plan-stdin) FILESYSTEM_PLAN=1; shift ;;
    --execute) EXECUTE=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done
[ -n "$OUT" ] || die "--out DIR is required"
[ -f "$UNIT" ] || die "user unit not found: $UNIT"

BINARY="$(unit_exec_start "$UNIT")"
ENV_FILE="$(unit_environment_file "$UNIT")"
DB="$(unit_env_value "$UNIT" AGENT_BOARD_DB)"
AUTH_FILE="$(unit_env_value "$UNIT" AGENT_BOARD_AUTH_CONFIG_FILE)"
[ -n "$AUTH_FILE" ] || AUTH_FILE="$(env_file_value "$ENV_FILE" AGENT_BOARD_AUTH_CONFIG_FILE 2>/dev/null || true)"
[ -n "$AUTH_OVERRIDE" ] && AUTH_FILE="$AUTH_OVERRIDE"

printf 'backup plan -> %s\n' "$OUT"
printf '  binary   %s\n' "$BINARY"
printf '  unit     %s\n' "$UNIT"
printf '  env      %s\n' "$ENV_FILE"
  printf '  auth     %s\n' "${AUTH_FILE:-<none configured>}"
  printf '  db       %s\n' "$DB"
  printf '  registry record     %s\n' "${REGISTRY_RECORD:-<not included>}"
  printf '  activation evidence %s\n' "${ACTIVATION_EVIDENCE:-<not included>}"
  printf '  credential          %s\n' "${CREDENTIAL_FILE:-<not included>}"
  printf '  endpoint            %s\n' "${ENDPOINT_FILE:-<not included>}"

if [ "$EXECUTE" != 1 ]; then
  printf 'dry-run: no files written. Re-run with --execute.\n'
  exit 0
fi

require_cmd cp
require_cmd python3
[ -x "$BINARY" ] || die "binary is not executable: $BINARY"
[ -f "$DB" ] || die "database not found: $DB"

if [ -e "$OUT" ] || [ -L "$OUT" ]; then
  [ ! -L "$OUT" ] || die "backup output must not be a symlink"
  [ -d "$OUT" ] || die "backup output is not a directory: $OUT"
  [ -z "$(find "$OUT" -mindepth 1 -print -quit)" ] || \
    die "refusing non-empty backup output; use a new checkpoint path"
fi

umask 077
mkdir -p "$OUT/bin" "$OUT/etc" "$OUT/db" "$OUT/factory"
chmod 0700 "$OUT" "$OUT/factory"
cp -p "$BINARY" "$OUT/bin/agent-board"
cp -p "$UNIT" "$OUT/etc/agent-board.service"
[ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ] && cp -p "$ENV_FILE" "$OUT/etc/agent-board.env"

: > "$OUT/manifest.sha256"
: > "$OUT/manifest.meta"
chmod 0600 "$OUT/manifest.sha256" "$OUT/manifest.meta"

record_present() {
  target="$1"
  [ -e "$target" ] || [ -L "$target" ]
}

record_file() {
  target="$1"
  rel="$2"
  kind="${3:-file}"
  if record_present "$target"; then
    [ ! -L "$target" ] || die "refusing symlink artifact: $target"
    [ -f "$target" ] || die "artifact is not a regular file: $target"
    destination="$OUT/$rel"
    mkdir -p "$(dirname -- "$destination")"
    if [ "$kind" = "db" ]; then
      "$here/sqlite-online-backup.py" backup \
        --source "$DB" --destination "$destination" >&2
    else
      cp -p "$target" "$destination"
    fi
    [ -f "$destination" ] || die "backup did not create $rel"
    printf '%s  %s\n' "$(sha256_file "$destination")" "$rel" >> "$OUT/manifest.sha256"
    printf 'PRESENT\t%s\t%s\t%s\t%s\t%s\t-\n' \
      "$(file_mode "$target")" "$(file_uid "$target")" "$(file_gid "$target")" "$target" "$rel" \
      >> "$OUT/manifest.meta"
  else
    # This is deliberate: absence is part of the rollback contract.
    printf 'ABSENT\t-\t-\t-\t%s\t%s\t-\n' "$target" "$rel" >> "$OUT/manifest.meta"
  fi
}

record_directory() {
  target="$1"
  if [ -L "$target" ]; then
    die "refusing symlink directory artifact: $target"
  elif [ -d "$target" ]; then
    printf 'DIRECTORY\t%s\t%s\t%s\t%s\t-\t-\n' \
      "$(file_mode "$target")" "$(file_uid "$target")" "$(file_gid "$target")" "$target" \
      >> "$OUT/manifest.meta"
  else
    printf 'DIR_ABSENT\t-\t-\t-\t%s\t-\t-\n' "$target" >> "$OUT/manifest.meta"
  fi
}

record_db_sidecar() {
  target="$1"
  if record_present "$target"; then
    [ ! -L "$target" ] || die "refusing symlink database sidecar: $target"
    [ -f "$target" ] || die "database sidecar is not a regular file: $target"
    # The online backup is standalone. Keep source sidecar metadata for the
    # complete audit manifest, but never copy or restore WAL/SHM bytes.
    printf 'SIDECAR\t%s\t%s\t%s\t%s\t-\t%s\n' \
      "$(file_mode "$target")" "$(file_uid "$target")" "$(file_gid "$target")" \
      "$target" "$(sha256_file "$target")" >> "$OUT/manifest.meta"
  else
    printf 'ABSENT\t-\t-\t-\t%s\t-\t-\n' "$target" >> "$OUT/manifest.meta"
  fi
}

record_file "$BINARY" "bin/agent-board"
record_file "$UNIT" "etc/agent-board.service"
[ -n "$ENV_FILE" ] && record_file "$ENV_FILE" "etc/agent-board.env" || true
[ -n "$AUTH_FILE" ] && record_file "$AUTH_FILE" "etc/auth.json" || true
record_file "$DB" "db/agent-board.db" db
record_db_sidecar "$DB-wal"
record_db_sidecar "$DB-shm"
[ -n "$REGISTRY_RECORD" ] && record_file "$REGISTRY_RECORD" "factory/registry-record.json" || true
[ -n "$ACTIVATION_EVIDENCE" ] && record_file "$ACTIVATION_EVIDENCE" "factory/activation-evidence.json" || true
if [ "$FILESYSTEM_PLAN" = 0 ] && [ -n "$ACTIVATION_EVIDENCE" ]; then
  record_directory "$(dirname -- "$ACTIVATION_EVIDENCE")"
fi
if [ -n "$CREDENTIAL_FILE" ]; then
  record_file "$CREDENTIAL_FILE" "factory/credential"
fi
if [ -n "$ENDPOINT_FILE" ]; then
  record_file "$ENDPOINT_FILE" "factory/endpoint.json"
fi
if [ "$FILESYSTEM_PLAN" = 0 ] && [ -n "$CREDENTIAL_FILE" ]; then
  record_directory "$(dirname -- "$CREDENTIAL_FILE")"
  record_directory "$(dirname -- "$(dirname -- "$CREDENTIAL_FILE")")"
fi
if [ "$FILESYSTEM_PLAN" = 0 ] && [ -n "$ENDPOINT_FILE" ]; then
  record_directory "$(dirname -- "$ENDPOINT_FILE")"
  record_directory "$(dirname -- "$(dirname -- "$ENDPOINT_FILE")")"
fi
if [ "$FILESYSTEM_PLAN" = 0 ] && [ -n "$ARTIFACT_DIR" ]; then
  # Cutover creates only the directory, never report contents. Later work
  # artifacts are evidence; rollback refuses a nonempty newly created directory.
  record_directory "$ARTIFACT_DIR"
fi

if [ "$FILESYSTEM_PLAN" = 1 ]; then
  python3 "$here/filesystem-preflight.py" record --backup "$OUT"
  printf '%s  filesystem-plan.json\n' "$(sha256_file "$OUT/filesystem-plan.json")" >> "$OUT/manifest.sha256"
fi

# Verify the manifest against the files just written.
while read -r expected rel; do
  [ -n "$expected" ] || continue
  actual="$(sha256_file "$OUT/$rel")"
  [ "$expected" = "$actual" ] || die "backup readback mismatch for $rel"
done < "$OUT/manifest.sha256"

"$here/sqlite-online-backup.py" verify \
  --database "$OUT/db/agent-board.db" \
  --expect-table machines --expect-table tasks >/dev/null

seal_tmp="$OUT/backup.complete.tmp"
[ ! -e "$seal_tmp" ] && [ ! -L "$seal_tmp" ] || die "backup completion temporary already exists"
[ ! -e "$OUT/backup.complete" ] && [ ! -L "$OUT/backup.complete" ] || \
  die "backup completion seal already exists"
umask 077
{
  printf 'format=agent-board-backup/2\n'
  printf 'manifest_sha256=%s\n' "$(sha256_file "$OUT/manifest.sha256")"
  printf 'manifest_meta_sha256=%s\n' "$(sha256_file "$OUT/manifest.meta")"
  printf 'database_rel=db/agent-board.db\n'
  printf 'database_sha256=%s\n' "$(sha256_file "$OUT/db/agent-board.db")"
  printf 'database_integrity=ok\n'
  printf 'database_sidecars=removed-on-restore\n'
} > "$seal_tmp"
chmod 0600 "$seal_tmp"
mv -- "$seal_tmp" "$OUT/backup.complete"

printf 'backup complete and verified: %s\n' "$OUT"
