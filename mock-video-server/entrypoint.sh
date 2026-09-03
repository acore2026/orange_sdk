#!/bin/sh
set -eu

ip route replace "${UE_INTERNET_CIDR:-10.60.0.0/16}" via "${UPF_N6_IP:-172.30.0.2}"
ip route replace "${UE_ACN_CIDR:-10.61.0.0/16}" via "${UPF_N6_IP:-172.30.0.2}"

exec python /opt/mock-video-server/server.py
