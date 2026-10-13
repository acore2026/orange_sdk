#!/usr/bin/env bash
# Durable console + command trace, including preflight failures.
set -uo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")" || exit 1
[[ $EUID == 0 ]] || { echo 'Run with sudo bash install.sh [check|install]'; exit 1; }
[[ $(cat /sys/class/net/eth0/address 2>/dev/null || true) != 4c:bb:47:ab:e4:10 ]] || { echo 'Protected source robot'; exit 1; }
MODE=${1:-check}
[[ $MODE == check || $MODE == install ]] || { echo 'Usage: sudo bash install.sh [check|install]'; exit 1; }
umask 077
LOGROOT=/var/log/orange-offline
mkdir -p "$LOGROOT" || { LOGROOT=/tmp/orange-offline; mkdir -p "$LOGROOT"; } || exit 1
RUN="$LOGROOT/$(date -u +%Y%m%dT%H%M%SZ)-$$-$MODE"
mkdir "$RUN" || exit 1
printf 'running\n' >"$RUN/status"
printf '%s\n' "$RUN" >"$LOGROOT/latest"
export ORANGE_INSTALL_RUN="$RUN"
export PS4='+ ${EPOCHREALTIME} ${BASH_SOURCE}:${LINENO}: '
finish() {
  rc=$?
  trap - EXIT
  printf 'exit_code=%s\nended_utc=%s\n' "$rc" "$(date -u +%FT%TZ)" >"$RUN/status"
  echo "Installation log: $RUN (exit=$rc)"
  echo "To export diagnostics: sudo bash collect-diagnostics.sh"
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
{
  date -u +%FT%TZ
  printf 'mode=%s\nbundle=%s\n' "$MODE" "$PWD"
  uname -a
  df -h / /tmp
  free -h
  cat /etc/os-release /etc/nv_tegra_release 2>/dev/null
} >"$RUN/environment.txt" 2>&1
for file in manifest.json SHA256SUMS; do [[ ! -f $file ]] || cp "$file" "$RUN/"; done
# xtrace records script/line/command. deploy.env is parsed as data, never sourced.
bash -x ./install-main.sh "$MODE" 2>&1 | tee "$RUN/console.log"
results=("${PIPESTATUS[@]}")
rc=${results[0]}
if [[ ${results[1]} != 0 ]]; then echo 'ERROR: log writing failed (check free disk).'; [[ $rc != 0 ]] || rc=74; fi
exit "$rc"
