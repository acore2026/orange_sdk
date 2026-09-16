#!/bin/sh
set -eu

printf '%s\n' \
    'Agent Connect SDK ARM64 runtime image' \
    '' \
    'Run one role per container:' \
    '  start-agent-a.sh [fresh-register|force-register]' \
    '  start-agent-b.sh [fresh-register|force-register]' \
    '' \
    'Required environment variables:' \
    '  AGENT_RUNTIME_IP, MASQUE_URL' \
    '' \
    'Test lifecycle defaults:' \
    '  command argument: fresh-register' \
    '  AGENT_FORCE_REGISTRATION=false' \
    '  AGENT_FRESH_REGISTRATION=true' \
    '  AGENT_DEREGISTER_ON_EXIT=true' \
    '  Persisted state is used only to deregister leftovers; it is not reused.' \
    '  Force mode resets local state without deregistering the old identity.' \
    '' \
    'Required Docker options for the real SDK flow:' \
    '  --cap-add NET_ADMIN --device /dev/net/tun' \
    '  Agent B loops the bundled local MP4; a V4L2 camera is optional.' \
    '  Give every Agent its own bridge/container network namespace.' \
    '  Do not use --network host when A and B run on the same host.' \
    '' \
    'Use start-agent-a.sh --help or start-agent-b.sh --help for role options.'
