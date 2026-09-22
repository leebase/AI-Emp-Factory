# Shared helpers for the agent-board user-service operator scripts.
#
# These scripts target the ACTUAL installed user service (systemctl --user),
# not the historical /opt system-service layout. They never print secret values
# and never assume root. Source this file; do not execute it directly.

set -eu

log() { printf '%s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

default_unit() {
  printf '%s\n' "${AGENT_BOARD_UNIT:-$HOME/.config/systemd/user/agent-board.service}"
}

# Binary named by ExecStart (first token), with the leading '-' stripped.
unit_exec_start() {
  sed -n 's/^[[:space:]]*ExecStart=//p' "$1" | head -n 1 | awk '{print $1}'
}

# EnvironmentFile path named by the unit (leading '-' ignored).
unit_environment_file() {
  sed -n 's/^[[:space:]]*EnvironmentFile=//p' "$1" | head -n 1 | sed 's/^-//'
}

# Print the value of an Environment=KEY=... line for a NON-SECRET key only.
unit_env_value() {
  sed -n "s/^[[:space:]]*Environment=$2=//p" "$1" | head -n 1
}

# Print only key names present in a protected env file (never values).
env_file_key_names() {
  [ -f "$1" ] || return 0
  sed -n 's/^[[:space:]]*\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$1" | sort -u
}

file_mode() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"; }
file_uid() { stat -c '%u' "$1" 2>/dev/null || stat -f '%u' "$1"; }
file_gid() { stat -c '%g' "$1" 2>/dev/null || stat -f '%g' "$1"; }

# Read one key from a protected env file WITHOUT printing its value.
env_file_value() {
  sed -n "s/^[[:space:]]*$2=//p" "$1" | head -n 1 | sed 's/^"//; s/"$//'
}

assert_regular() {
  [ -f "$1" ] || die "not a regular file: $1"
}

assert_mode() {
  actual="$(file_mode "$1")"
  [ "$actual" = "$2" ] || die "$1 must be mode $2 (found $actual)"
}

systemctl_user_available() {
  systemctl --user show-environment >/dev/null 2>&1
}

# Print whether a systemd user unit is active; empty when the bus is absent.
unit_active_state() {
  systemctl --user is-active "$1" 2>/dev/null || true
}

unit_service_name() {
  basename "$1"
}

wait_for_healthy() {
  host="$1"
  port="$2"
  service="$3"
  attempts="${4:-30}"
  i=0
  while [ "$i" -lt "$attempts" ]; do
    state="$(systemctl --user is-active "$service" 2>/dev/null || true)"
    code="$(curl -s --max-time 2 -o /dev/null -w '%{http_code}' \
      -H 'User-Agent: agent-board-factory-client/2' \
      "http://$host:$port/health" 2>/dev/null || true)"
    if [ "$state" = "active" ] && [ "$code" = "200" ]; then
      return 0
    fi
    i=$((i + 1))
    sleep 1
  done
  log "service $service did not become active and healthy"
  return 1
}

# sha256 of a file, portable across Linux/macOS.
sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}
