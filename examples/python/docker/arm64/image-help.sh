#!/bin/sh
set -eu

printf '%s\n' \
    'Agent Connect SDK ARM64 runtime image' \
    '' \
    'Container role commands:' \
    '  run-agent-a.sh [fresh-register|force-register]' \
    '  run-agent-b.sh [fresh-register|force-register]' \
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
    'Host-side start-agent-a.sh/start-agent-b.sh launch these through Docker Compose.' \
    'Use run-agent-a.sh --help or run-agent-b.sh --help for container role options.'
