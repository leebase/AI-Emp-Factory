#!/bin/sh
# Independently executable rollback of the ACTUAL agent-board user service.
#
# Restores every artifact from a coherent backup produced by backup.sh,
# including explicit ABSENT entries for newly-created registry, activation,
# credential, and endpoint artifacts. It verifies a sealed complete checkpoint,
# restores bytes/modes/owners, removes stale SQLite sidecars, restarts the user
# unit, and requires an active healthy listener. It can be run independently.
#
# Usage: rollback.sh --backup DIR [--unit PATH] [--execute]

set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=lib.sh
. "$here/lib.sh"

UNIT="$(default_unit)"
BACKUP=""
EXECUTE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --unit) UNIT="$2"; shift 2 ;;
    --backup) BACKUP="$2"; shift 2 ;;
    --execute) EXECUTE=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done
[ -n "$BACKUP" ] && [ -d "$BACKUP" ] || die "--backup must name a backup directory"
[ -f "$UNIT" ] || die "user unit not found: $UNIT"
[ -f "$BACKUP/manifest.sha256" ] || die "backup manifest missing: $BACKUP/manifest.sha256"
[ ! -L "$BACKUP/manifest.sha256" ] || die "backup manifest must not be a symlink"
[ -f "$BACKUP/manifest.meta" ] || die "backup manifest.meta missing: refusing incomplete checkpoint"
[ ! -L "$BACKUP/manifest.meta" ] || die "backup metadata must not be a symlink"
[ -f "$BACKUP/backup.complete" ] || die "backup completion seal missing: refusing incomplete checkpoint"
[ ! -L "$BACKUP/backup.complete" ] || die "backup completion seal must not be a symlink"
[ "$(file_mode "$BACKUP/backup.complete")" = 600 ] || die "backup completion seal must be mode 0600"

seal_value() {
  key="$1"
  value="$(awk -F= -v wanted="$key" '$1 == wanted { value=substr($0, index($0, "=") + 1); count++ } END { if (count != 1) exit 1; print value }' "$BACKUP/backup.complete")" || \
    die "backup completion seal is invalid"
  [ -n "$value" ] || die "backup completion seal is incomplete"
  printf '%s\n' "$value"
}

[ "$(seal_value format)" = "agent-board-backup/2" ] || die "unsupported backup completion seal"
[ "$(seal_value manifest_sha256)" = "$(sha256_file "$BACKUP/manifest.sha256")" ] || \
  die "backup manifest seal mismatch"
[ "$(seal_value manifest_meta_sha256)" = "$(sha256_file "$BACKUP/manifest.meta")" ] || \
  die "backup metadata seal mismatch"
[ "$(seal_value database_rel)" = "db/agent-board.db" ] || die "backup database entry is invalid"
[ "$(seal_value database_integrity)" = ok ] || die "backup database integrity is not verified"
[ "$(seal_value database_sidecars)" = removed-on-restore ] || die "backup sidecar policy is invalid"
[ -f "$BACKUP/db/agent-board.db" ] || die "backup database snapshot missing"
[ ! -e "$BACKUP/db/agent-board.db-wal" ] && [ ! -e "$BACKUP/db/agent-board.db-shm" ] || \
  die "backup database has stale SQLite sidecars"

BINARY="$(unit_exec_start "$UNIT")"
ENV_FILE="$(unit_environment_file "$UNIT")"
DB="$(unit_env_value "$UNIT" AGENT_BOARD_DB)"
AUTH_FILE="$(unit_env_value "$UNIT" AGENT_BOARD_AUTH_CONFIG_FILE)"
BIND_HOST="$(unit_env_value "$UNIT" AGENT_BOARD_BIND_HOST)"
PORT="$(unit_env_value "$UNIT" AGENT_BOARD_PORT)"
[ -n "$BIND_HOST" ] && [ "$BIND_HOST" != "0.0.0.0" ] || BIND_HOST="127.0.0.1"
[ -n "$PORT" ] || PORT=8787
SERVICE="$(unit_service_name "$UNIT")"
[ -n "$DB" ] || die "user unit does not define AGENT_BOARD_DB"

while read -r expected rel; do
  [ -n "$expected" ] || continue
  [ -f "$BACKUP/$rel" ] || die "backup file missing: $rel"
  [ ! -L "$BACKUP/$rel" ] || die "backup file is a symlink: $rel"
  actual="$(sha256_file "$BACKUP/$rel")"
  [ "$expected" = "$actual" ] || die "backup file corrupt: $rel"
done < "$BACKUP/manifest.sha256"

meta_summary="$(awk -F '\t' -v database="$DB" '
  NF != 7 { exit 10 }
  $5 !~ /^\// { exit 11 }
  $1 == "PRESENT" && $6 == "db/agent-board.db" && $5 == database { db++ }
  ($5 == database "-wal" || $5 == database "-shm") && ($1 == "SIDECAR" || $1 == "ABSENT") { sidecars++ }
  $1 == "PRESENT" || $1 == "ABSENT" || $1 == "DIRECTORY" || $1 == "DIR_ABSENT" || $1 == "SIDECAR" { next }
  { exit 12 }
  END { if (db != 1 || sidecars != 2) exit 13; print "ok" }
' "$BACKUP/manifest.meta")" || die "backup metadata is incomplete or invalid"
[ "$meta_summary" = ok ] || die "backup metadata is incomplete or invalid"

"$here/sqlite-online-backup.py" verify \
  --database "$BACKUP/db/agent-board.db" \
  --expect-table machines --expect-table tasks >/dev/null

printf 'rollback plan (backup %s)\n' "$BACKUP"
printf '  unit     %s\n' "$UNIT"
printf '  binary   %s\n' "$BINARY"
printf '  env      %s\n' "$ENV_FILE"
printf '  db       %s\n' "$DB"
printf '  service  %s\n' "$SERVICE"

if [ "$EXECUTE" != 1 ]; then
  printf 'dry-run: no change. Re-run with --execute.\n'
  exit 0
fi

require_cmd curl
systemctl_user_available || die "systemctl --user is unavailable; cannot roll back safely"

# A supported SQLite backup is restored only while the service is stopped. This
# avoids mixing a live WAL/checkpoint with the coherent backup snapshot.
systemctl --user stop "$SERVICE"

# The snapshot is standalone. Remove every live sidecar before installing it;
# never let stale WAL/SHM frames be interpreted with restored main-file bytes.
for sidecar in "$DB-wal" "$DB-shm"; do
  if [ -L "$sidecar" ]; then
    die "refusing symlink SQLite sidecar: $sidecar"
  elif [ -e "$sidecar" ]; then
    [ -f "$sidecar" ] || die "SQLite sidecar is not a regular file: $sidecar"
    rm -f -- "$sidecar"
  fi
  [ ! -e "$sidecar" ] && [ ! -L "$sidecar" ] || die "SQLite sidecar remains: $sidecar"
done

# Restore to the exact recorded target path with its recorded owner/mode. No
# hard-coded modes: manifest.meta is written by backup.sh from the live files.
while IFS="	" read -r status mode uid gid original rel source_hash; do
  [ -n "$status" ] || continue
  case "$status" in
    PRESENT)
      src="$BACKUP/$rel"
      [ -f "$src" ] || die "backup file missing for manifest entry: $rel"
      [ ! -L "$original" ] || rm -f -- "$original"
      [ ! -e "$original" ] || [ -f "$original" ] || die "restore target is not a regular file: $original"
      mkdir -p "$(dirname -- "$original")"
      install -m "$mode" "$src" "$original"
      if [ "$(file_uid "$original")" != "$uid" ] || [ "$(file_gid "$original")" != "$gid" ]; then
        chown "$uid:$gid" "$original" || die "could not restore owner for $original"
      fi
      [ "$(file_mode "$original")" = "$mode" ] || die "mode restore mismatch for $original"
      [ "$(file_uid "$original")" = "$uid" ] || die "uid restore mismatch for $original"
      [ "$(file_gid "$original")" = "$gid" ] || die "gid restore mismatch for $original"
      [ "$(sha256_file "$original")" = "$(sha256_file "$src")" ] || die "byte restore mismatch for $original"
      ;;
    DIRECTORY)
      [ -d "$original" ] || die "directory restore target is missing: $original"
      [ ! -L "$original" ] || die "directory restore target is a symlink: $original"
      chmod "$mode" "$original" || die "could not restore directory mode for $original"
      if [ "$(file_uid "$original")" != "$uid" ] || [ "$(file_gid "$original")" != "$gid" ]; then
        chown "$uid:$gid" "$original" || die "could not restore directory owner for $original"
      fi
      [ "$(file_mode "$original")" = "$mode" ] || die "directory mode restore mismatch for $original"
      [ "$(file_uid "$original")" = "$uid" ] || die "directory uid restore mismatch for $original"
      [ "$(file_gid "$original")" = "$gid" ] || die "directory gid restore mismatch for $original"
      ;;
    DIR_ABSENT)
      if [ -L "$original" ]; then
        die "unexpected symlink at absent directory target: $original"
      elif [ -d "$original" ]; then
        rmdir -- "$original" || die "created directory is not empty: $original"
      elif [ -e "$original" ]; then
        die "absent directory target is not a directory: $original"
      fi
      [ ! -e "$original" ] && [ ! -L "$original" ] || die "absent directory remains after rollback: $original"
      ;;
    SIDECAR)
      [ "$rel" = "-" ] || die "SQLite sidecar must not have a restore artifact"
      [ "$source_hash" != "-" ] || die "SQLite sidecar source hash is missing"
      if [ -L "$original" ]; then
        die "refusing symlink SQLite sidecar: $original"
      elif [ -e "$original" ]; then
        [ -f "$original" ] || die "SQLite sidecar is not a regular file: $original"
        rm -f -- "$original"
      fi
      [ ! -e "$original" ] && [ ! -L "$original" ] || die "SQLite sidecar remains: $original"
      ;;
    ABSENT)
      if [ -L "$original" ]; then
        rm -f -- "$original"
      elif [ -e "$original" ]; then
        [ -f "$original" ] || die "absent restore target is not a regular file: $original"
        rm -f -- "$original"
      fi
      [ ! -e "$original" ] && [ ! -L "$original" ] || die "absent artifact remains after rollback: $original"
      ;;
    *) die "unknown manifest status: $status" ;;
  esac
done < "$BACKUP/manifest.meta"

systemctl --user daemon-reload
systemctl --user restart "$SERVICE"
wait_for_healthy "$BIND_HOST" "$PORT" "$SERVICE" 30 || die "rollback restart did not become healthy"
printf 'rollback complete; health verified\n'
