#!/usr/bin/env bash
# Install only on a NEW Nano flashed with the documented vendor image.
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
BUNDLE=$PWD
MODE=${1:-check}
fail() { echo "ERROR: $*" >&2; exit 1; }
[[ $MODE == check || $MODE == install ]] || fail 'Usage: sudo bash install.sh [check|install]'
[[ $EUID == 0 ]] || fail 'Run with sudo.'
[[ $(cat /sys/class/net/eth0/address 2>/dev/null || true) != 4c:bb:47:ab:e4:10 ]] || { echo "Protected source robot hardware address" >&2; exit 1; }
[[ $(uname -m) == aarch64 ]] || fail 'Requires ARM64 Nano.'
[[ -r /proc/device-tree/model ]] || fail 'Jetson device tree not found.'
MODEL=$(tr -d '\0' </proc/device-tree/model)
[[ ${MODEL,,} == *orin*nano* ]] || fail "Requires Orin Nano, found: $MODEL"
. /etc/os-release
[[ $VERSION_ID == 20.04 ]] || fail 'Requires Ubuntu 20.04.'
grep -q 'R35 (release), REVISION: 3.1' /etc/nv_tegra_release || fail 'Requires L4T R35.3.1.'
[[ -f SHA256SUMS && -f manifest.json ]] || fail 'Incomplete bundle.'
sha256sum --quiet -c SHA256SUMS || fail 'Bundle checksum failure.'
python3 - <<'PY'
import json
m=json.load(open('manifest.json'))
assert m['complete'] is True, 'Bundle preparation is not complete'
PY
[[ -f /opt/ros/foxy/setup.bash ]] || fail 'ROS Foxy missing.'
for cmd in colcon cmake gcc g++ make gdb nmcli ip tar python3 sudo curl; do command -v "$cmd" >/dev/null || fail "Missing base-image tool: $cmd"; done
python3 verify_base.py
[[ -r deploy.env ]] || fail 'Copy deploy.env.example to deploy.env and set core/camera/network values.'
# Configuration is parsed as data, never sourced as shell commands.
python3 configure.py --check deploy.env
[[ $(df -Pk / | awk 'NR==2 {print $4}') -gt 8388608 ]] || fail 'At least 8 GiB free disk required.'
[[ ! -e /var/lib/orange-offline/installed.json ]] || fail 'Already installed; retain current files and use the documented backup to rollback before reinstalling.'
[[ ! -e /root/.local/state/agent-sdk ]] || fail 'Existing Agent identity detected; use a fresh target, never overwrite identities.'
for path in /home/aicor/compute_network/sandbox-demo /home/unitree/unitree/nav /opt/orange-runtime /home/unitree/cyclonedds /home/unitree/unitree_sdk2_python; do
  [[ ! -e $path ]] || fail "Existing application directory: $path; this installer requires a fresh target."
done
[[ $MODE == install ]] || { echo 'Preflight passed. Run sudo bash install.sh install to deploy.'; exit 0; }
# No modifications above this point.
STAMP=$(date +%Y%m%d-%H%M%S)-$$
BACKUP=/var/backups/orange-offline/$STAMP
mkdir -p "$BACKUP" /var/lib/orange-offline
systemctl is-enabled dog-init.service >"$BACKUP/service-enabled.txt" 2>&1 || true
systemctl is-active dog-init.service >"$BACKUP/service-active.txt" 2>&1 || true
paths=(etc/systemd/system/dog-init.service etc/systemd/system/dog-init.service.d etc/orange-demo home/unitree/xpd/script home/aicor/compute_network/sandbox-demo home/unitree/unitree/nav home/unitree/cyclonedds home/unitree/unitree_sdk2_python home/unitree/cyclonedds_ws/cyclonedds.xml var/lib/orange-video-capture etc/logrotate.d/orange-demo)
existing=()
for path in "${paths[@]}"; do [[ ! -e /$path ]] || existing+=("$path"); done
printf '%s\n' "${existing[@]}" >"$BACKUP/paths.txt"
if ((${#existing[@]})); then tar -C / -czf "$BACKUP/before.tar.gz" "${existing[@]}"; fi
if systemctl cat dog-init.service >/dev/null 2>&1; then
  systemctl stop dog-init.service
  systemctl disable dog-init.service
  if systemctl is-active --quiet dog-init.service; then fail "Could not stop target dog-init"; fi
fi
trap 'rc=$?; printf "FAILED line=%s exit=%s command=%s backup=%s\n" "$LINENO" "$rc" "$BASH_COMMAND" "$BACKUP" >&2' ERR
# payload contains application/runtime files only: no kernel, /etc network, or identity.
tar --no-same-owner -xzf payload.tar.gz -C /
PYTHON=/opt/orange-runtime/python/bin/python3.12
BASE=/home/aicor/compute_network/sandbox-demo
export PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
export CYCLONEDDS_HOME=/home/unitree/cyclonedds/install
for service in dog dog-ctrl; do
  "$PYTHON" -m venv "$BASE/$service/.venv"
  "$BASE/$service/.venv/bin/python" -m pip install --no-index --find-links "$BUNDLE/wheelhouse" -r "$BUNDLE/requirements-$service.lock"
  "$BASE/$service/.venv/bin/python" -m pip install --no-index --find-links "$BUNDLE/wheelhouse" unitree-sdk2py==1.0.1
  if [[ $service == dog ]]; then "$BASE/$service/.venv/bin/python" -m pip install --no-index --find-links "$BUNDLE/wheelhouse" -c "$BUNDLE/requirements-dog.lock" agent-connect-sdk==0.17.11; fi
  "$BASE/$service/.venv/bin/python" -m pip check
done
# Build ROS with system Python 3.8, without Python 3.12 paths.
env -i HOME=/root PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 /bin/bash "$BUNDLE/build-nav.sh"
python3 configure.py deploy.env
install -d -m 0700 /var/lib/orange-video-capture
install -m 0644 config/watch.py /var/lib/orange-video-capture/watch.py
install -d /etc/systemd/system/dog-init.service.d /var/log/orange-demo/ros
install -m 0644 config/dog-init.service /etc/systemd/system/dog-init.service
install -m 0644 config/*.conf /etc/systemd/system/dog-init.service.d/
install -m 0644 config/logrotate /etc/logrotate.d/orange-demo
systemctl daemon-reload
# Deliberately leave startup to a separate, explicit operator step after checks.
systemctl disable dog-init.service >/dev/null
python3 verify_runtime.py
env -i HOME=/root PATH=/usr/bin:/bin LANG=C.UTF-8 /bin/bash "$BUNDLE/verify-ros.sh"
cp manifest.json /var/lib/orange-offline/installed.json
printf '%s\n' "$BACKUP" >/var/lib/orange-offline/backup-path
trap - ERR
echo 'Installed and offline-checked. Services are STOPPED.'
echo 'After configuring a distinct core allocation (or disconnecting the old dog), run: sudo systemctl enable --now dog-init.service'
echo "Backup: $BACKUP"
