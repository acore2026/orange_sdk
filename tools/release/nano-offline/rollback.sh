#!/bin/bash
set -Eeuo pipefail
[[ $EUID == 0 ]] || { echo 'Run with sudo'; exit 1; }
[[ $(cat /sys/class/net/eth0/address 2>/dev/null || true) != 4c:bb:47:ab:e4:10 ]] || { echo "Protected source robot hardware address" >&2; exit 1; }
BACKUP=${1:-}
[[ $BACKUP == /var/backups/orange-offline/* && -d $BACKUP && -f $BACKUP/paths.txt ]] || { echo 'Usage: sudo bash rollback.sh /var/backups/orange-offline/TIMESTAMP'; exit 1; }
systemctl stop dog-init.service || true
systemctl disable dog-init.service || true
mkdir -p "$BACKUP/failed-install"
# Move installed files aside; do not erase independently created Agent identity.
paths=(etc/systemd/system/dog-init.service etc/systemd/system/dog-init.service.d etc/orange-demo home/unitree/xpd/script home/aicor/compute_network/sandbox-demo home/unitree/unitree/nav home/unitree/cyclonedds home/unitree/unitree_sdk2_python opt/orange-runtime var/lib/orange-video-capture etc/logrotate.d/orange-demo var/lib/orange-offline)
for path in "${paths[@]}"; do
 if [[ -e /$path ]]; then mkdir -p "$BACKUP/failed-install/$(dirname "$path")"; mv "/$path" "$BACKUP/failed-install/$path"; fi
done
if [[ -f $BACKUP/before.tar.gz ]]; then tar -xzf "$BACKUP/before.tar.gz" -C /; fi
systemctl daemon-reload
if grep -qx enabled "$BACKUP/service-enabled.txt"; then systemctl enable dog-init.service; fi
echo 'Previous files restored; services remain stopped. Review configuration before starting.'
