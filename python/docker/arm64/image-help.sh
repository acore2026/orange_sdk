#!/bin/sh
set -eu

printf '%s\n' \
    'Agent Connect SDK ARM64 runtime image' \
    '' \
    'Run one role per container:' \
    '  start-agent-a.sh' \
    '  start-agent-b.sh' \
    '' \
    'Required environment variables:' \
    '  AGENT_RUNTIME_IP, LOCAL_VLAN_IP, MASQUE_URL' \
    '' \
    'Test lifecycle defaults:' \
    '  AGENT_FRESH_REGISTRATION=true' \
    '  AGENT_DEREGISTER_ON_EXIT=true' \
    '  Persisted state is used only to deregister leftovers; it is not reused.' \
    '' \
    'Required Docker options for the real SDK flow:' \
    '  --cap-add NET_ADMIN --device /dev/net/tun' \
    '  Give every Agent its own bridge/container network namespace.' \
    '  Do not use --network host when A and B run on the same host.' \
    '' \
    'Use start-agent-a.sh --help or start-agent-b.sh --help for role options.'
