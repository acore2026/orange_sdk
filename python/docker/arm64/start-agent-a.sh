#!/bin/sh
set -eu

if [ "${1:-}" = "--help" ]; then
    exec python /opt/agent-sdk/examples/agent_a_test.py --help
fi
if [ "$#" -ne 0 ]; then
    printf '%s\n' 'start-agent-a.sh accepts configuration through environment variables only' >&2
    exit 2
fi

require_value() {
    value_name="$1"
    value="$2"
    if [ -z "${value}" ]; then
        printf 'required environment variable is empty: %s\n' "${value_name}" >&2
        exit 2
    fi
}

is_true() {
    case "$1" in
        1|true|TRUE|yes|YES|on|ON) return 0 ;;
        *) return 1 ;;
    esac
}

require_value AGENT_RUNTIME_IP "${AGENT_RUNTIME_IP:-}"
require_value LOCAL_VLAN_IP "${LOCAL_VLAN_IP:-}"
require_value MASQUE_URL "${MASQUE_URL:-}"

if [ ! -c /dev/net/tun ]; then
    printf '%s\n' '/dev/net/tun is unavailable; start the container with --device /dev/net/tun' >&2
    exit 1
fi

if ! ip -o addr show | awk '{print $4}' | cut -d/ -f1 | grep -Fqx "${LOCAL_VLAN_IP}"; then
    printf 'LOCAL_VLAN_IP is not assigned inside this network namespace: %s\n' "${LOCAL_VLAN_IP}" >&2
    printf '%s\n' 'Pass the IPv4 address assigned to this container network namespace.' >&2
    exit 1
fi

AGENT_LOG_FILE="${AGENT_LOG_FILE:-/var/log/agent-sdk/agent-a.log}"
AGENT_MESSAGE_JSON="${AGENT_MESSAGE_JSON:-}"
if [ -z "${AGENT_MESSAGE_JSON}" ]; then
    AGENT_MESSAGE_JSON='{"type":"text","content":"hello Agent B from Agent A"}'
fi
mkdir -p "$(dirname -- "${AGENT_LOG_FILE}")" "${XDG_STATE_HOME:-/var/lib/agent-sdk}"

set -- \
    --runtime-ip "${AGENT_RUNTIME_IP}" \
    --runtime-port "${AGENT_RUNTIME_PORT:-8080}" \
    --local-vlan-ip "${LOCAL_VLAN_IP}" \
    --tcp-port "${AGENT_TCP_PORT:-4001}" \
    --udp-port "${AGENT_UDP_PORT:-28443}" \
    --masque-url "${MASQUE_URL}" \
    --tun-name "${AGENT_TUN_NAME:-agent_tun_a}" \
    --tun-mtu "${AGENT_TUN_MTU:-1280}" \
    --agent-name "${AGENT_NAME:-Agent-A}" \
    --owner "${AGENT_OWNER:-ab-test-owner-a}" \
    --description "${AGENT_DESCRIPTION:-Agent A capability discovery test}" \
    --region "${AGENT_REGION:-CN}" \
    --target-capability "${AGENT_TARGET_CAPABILITY:-text}" \
    --priority "${AGENT_PRIORITY:-1}" \
    --task-id "${AGENT_TASK_ID:-agent-a-to-b-test}" \
    --task-description "${AGENT_TASK_DESCRIPTION:-discover a text-capable Agent B}" \
    --discovery-scope "${AGENT_DISCOVERY_SCOPE:-intra_plmn}" \
    --max-results "${AGENT_MAX_RESULTS:-10}" \
    --group-name "${AGENT_GROUP_NAME:-agent-a-b-test-group}" \
    --dnn "${AGENT_DNN:-internet}" \
    --group-scope "${AGENT_GROUP_SCOPE:-private}" \
    --group-timeout "${AGENT_GROUP_TIMEOUT:-60}" \
    --message "${AGENT_MESSAGE_JSON}" \
    --message-type "${AGENT_MESSAGE_TYPE:-text}" \
    --message-timeout "${AGENT_MESSAGE_TIMEOUT:-10}" \
    --log-file "${AGENT_LOG_FILE}" \
    --log-level "${AGENT_LOG_LEVEL:-INFO}"

if [ -n "${MASQUE_TOKEN:-}" ]; then
    set -- "$@" --masque-token "${MASQUE_TOKEN}"
fi
if [ -n "${AGENT_TARGET_ID:-}" ]; then
    set -- "$@" --target-agent-id "${AGENT_TARGET_ID}"
fi
if is_true "${AGENT_FRESH_REGISTRATION:-true}"; then
    set -- "$@" --fresh-registration
fi
if is_true "${AGENT_DEREGISTER_ON_EXIT:-true}"; then
    set -- "$@" --deregister-on-exit
fi

exec python /opt/agent-sdk/examples/agent_a_test.py "$@"
