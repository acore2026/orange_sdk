#!/bin/bash
set -uo pipefail
# Read-only: no patrol request, SportClient call, or camera stream.
status=0
systemctl --no-pager status dog-init.service || status=1
systemctl show dog-init.service -p MainPID -p Result -p NeedDaemonReload
curl --fail --max-time 5 http://127.0.0.1:8080/health || status=1
printf '\n'
curl --fail --max-time 5 http://127.0.0.1:8080/status || status=1
printf '\n'
ss -lnt | awk 'NR==1 || /:8080 |:10088 |:18892 /'
find /dev -maxdepth 1 -name 'video*' -ls
if [[ -f /home/aicor/compute_network/sandbox-demo/.dog-services/orange-dog.ready ]]; then
  stat /home/aicor/compute_network/sandbox-demo/.dog-services/orange-dog.ready
else
  echo 'Agent ready file missing'; status=1
fi
if [[ -f /home/unitree/unitree/nav/install/setup.bash ]]; then
  nodes=$(timeout 15 env -i HOME=/root ROS_LOG_DIR=/var/log/orange-demo/ros PATH=/usr/bin:/bin \
    /bin/bash -c 'source /home/unitree/unitree/nav/install/setup.bash; source /home/unitree/cyclonedds_ws/install/setup.bash; export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp CYCLONEDDS_URI=/home/unitree/cyclonedds_ws/cyclonedds.xml; ros2 node list --no-daemon --spin-time 5') || status=1
  printf '%s\n' "$nodes"
  for node in /go2_base /log_state /nav_stubbing_enhanced_planB /start_button_sender; do
    grep -qx "$node" <<<"$nodes" || { echo "Missing ROS node: $node"; status=1; }
  done
fi
journalctl -u dog-init.service -n 40 --no-pager
exit "$status"
