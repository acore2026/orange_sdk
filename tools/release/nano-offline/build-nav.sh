#!/bin/bash
set -eo pipefail
source /opt/ros/foxy/setup.bash
source /home/unitree/cyclonedds_ws/install/setup.bash
export CMAKE_BUILD_PARALLEL_LEVEL=2 MAKEFLAGS=-j2
cd /home/unitree/unitree/nav
# Never reuse NX build/install caches; system Python is 3.8.
colcon build --executor sequential --cmake-args -DCMAKE_BUILD_TYPE=Release -DPython3_EXECUTABLE=/usr/bin/python3 -DBUILD_TESTING=OFF
source install/setup.bash
for pkg in nav go2_base simple_controls go2_start_button_sender go2_slam go2_perception clients unitree_api unitree_go; do ros2 pkg prefix "$pkg"; done
