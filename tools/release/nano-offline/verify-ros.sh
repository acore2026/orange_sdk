set -e
source /home/unitree/unitree/nav/install/setup.bash
source /home/unitree/cyclonedds_ws/install/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
python3 - <<'PY'
from unitree_go.msg import WirelessController, SportModeState
from unitree_api.msg import Request
from go2_base.go2_base import Go2BaseNode
from simple_controls.log_state import main
from nav_stubbing.nav_stubbing_enhanced_planB import main
from go2_start_button_sender.start_button_node import StartButtonSender
from rosidl_generator_py import import_type_support
for pkg in ['unitree_go','unitree_api','unitree_hg','calib_srv']: import_type_support(pkg)
print('ROS nodes and generated message native libraries import OK')
PY
