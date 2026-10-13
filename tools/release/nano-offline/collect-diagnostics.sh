#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
[[ $EUID == 0 ]] || { echo 'Run with sudo bash collect-diagnostics.sh [output-directory]'; exit 1; }
exec /usr/bin/python3 ./collect_diagnostics.py --output-dir "${1:-$PWD/diagnostics}"
