#!/bin/sh
set -eu

usage() {
    printf '%s\n' \
        'Usage: start-agent-a.sh [fresh-register|force-register]' \
        '' \
        '  fresh-register  deregister a persisted network identity, then register again' \
        '  force-register  reset local state to lifecycle state 1 without deregistration, then register again' \
        '' \
        'With no argument, AGENT_FORCE_REGISTRATION/AGENT_FRESH_REGISTRATION remain supported.'
}

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    usage
    printf '\nUnderlying Agent A options:\n'
    exec python /opt/agent-sdk/examples/agent_a_test.py --help
fi
if [ "$#" -gt 1 ]; then
    usage >&2
    exit 2
fi
REGISTRATION_MODE="${1:-}"
case "${REGISTRATION_MODE}" in
    ''|fresh-register|force-register) ;;
    *)
        printf 'unknown registration mode: %s\n' "${REGISTRATION_MODE}" >&2
        usage >&2
        exit 2
        ;;
esac

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
require_value MASQUE_URL "${MASQUE_URL:-}"

if [ ! -c /dev/net/tun ]; then
    printf '%s\n' '/dev/net/tun is unavailable; start the container with --device /dev/net/tun' >&2
    exit 1
fi

AGENT_LOG_FILE="${AGENT_LOG_FILE:-/var/log/agent-sdk/agent-a.log}"
AGENT_COMPUTE_CAPABILITY_ID="${AGENT_COMPUTE_CAPABILITY_ID:-dog-vision}"
AGENT_MESSAGE_JSON="${AGENT_MESSAGE_JSON:-}"
if [ -z "${AGENT_MESSAGE_JSON}" ]; then
    AGENT_MESSAGE_JSON='{"type":"text","content":"hello Agent B from Agent A"}'
fi
mkdir -p "$(dirname -- "${AGENT_LOG_FILE}")" "${XDG_STATE_HOME:-/var/lib/agent-sdk}"

set -- \
    --runtime-ip "${AGENT_RUNTIME_IP}" \
    --runtime-port "${AGENT_RUNTIME_PORT:-8080}" \
    --tcp-port "${AGENT_TCP_PORT:-4001}" \
    --udp-port "${AGENT_UDP_PORT:-28443}" \
    --masque-url "${MASQUE_URL}" \
    --tun-name "${AGENT_TUN_NAME:-agent_tun_a}" \
    --tun-mtu "${AGENT_TUN_MTU:-1280}" \
    --agent-name "${AGENT_NAME:-Agent-A}" \
    --owner "${AGENT_OWNER:-ab-test-owner-a}" \
    --description "${AGENT_DESCRIPTION:-Agent A video offload consumer test}" \
    --region "${AGENT_REGION:-CN}" \
    --target-capability "${AGENT_TARGET_CAPABILITY:-robot dog}" \
    --priority "${AGENT_PRIORITY:-1}" \
    --task-id "${AGENT_TASK_ID:-agent-a-to-b-test}" \
    --task-description "${AGENT_TASK_DESCRIPTION:-discover a video offload Agent B}" \
    --discovery-scope "${AGENT_DISCOVERY_SCOPE:-intra_plmn}" \
    --max-results "${AGENT_MAX_RESULTS:-10}" \
    --group-name "${AGENT_GROUP_NAME:-agent-a-b-test-group}" \
    --dnn "${AGENT_DNN:-internet}" \
    --group-scope "${AGENT_GROUP_SCOPE:-private}" \
    --group-timeout "${AGENT_GROUP_TIMEOUT:-60}" \
    --message "${AGENT_MESSAGE_JSON}" \
    --message-type "${AGENT_MESSAGE_TYPE:-text}" \
    --message-timeout "${AGENT_MESSAGE_TIMEOUT:-10}" \
    --compute-capability-id "${AGENT_COMPUTE_CAPABILITY_ID}" \
    --compute-cpu-millicores "${AGENT_COMPUTE_CPU_MILLICORES:-2000}" \
    --compute-memory-mib "${AGENT_COMPUTE_MEMORY_MIB:-4096}" \
    --compute-timeout "${AGENT_COMPUTE_TIMEOUT:-30}" \
    --sandbox-timeout "${AGENT_SANDBOX_TIMEOUT:-15}" \
    --recognition-target "${AGENT_RECOGNITION_TARGET:-寻找穿红色衣服的人}" \
    --control-text "${AGENT_CONTROL_TEXT:-向左移动一米}" \
    --control-language "${AGENT_CONTROL_LANGUAGE:-zh-CN}" \
    --terminal-action "${AGENT_COMPUTE_TERMINAL_ACTION:-release}" \
    --media-timeout "${AGENT_MEDIA_TIMEOUT:-120}" \
    --frame-count "${AGENT_PROCESSED_FRAME_COUNT:-1}" \
    --frame-timeout "${AGENT_PROCESSED_FRAME_TIMEOUT:-30}" \
    --log-file "${AGENT_LOG_FILE}" \
    --log-level "${AGENT_LOG_LEVEL:-INFO}"

if [ -n "${MASQUE_TOKEN:-}" ]; then
    set -- "$@" --masque-token "${MASQUE_TOKEN}"
fi
if [ -n "${AGENT_TARGET_ID:-}" ]; then
    set -- "$@" --target-agent-id "${AGENT_TARGET_ID}"
fi
if [ -n "${AGENT_COMPUTE_API_VERSION:-}" ]; then
    set -- "$@" --compute-api-version "${AGENT_COMPUTE_API_VERSION}"
fi
if [ -n "${AGENT_COMPUTE_IMAGE_ID:-}" ]; then
    set -- "$@" --compute-image-id "${AGENT_COMPUTE_IMAGE_ID}"
fi
if [ -n "${AGENT_COMPUTE_GPU_COUNT:-}" ]; then
    set -- "$@" --compute-gpu-count "${AGENT_COMPUTE_GPU_COUNT}"
fi
if [ -n "${AGENT_COMPUTE_GPU_MODEL:-}" ]; then
    set -- "$@" --compute-gpu-model "${AGENT_COMPUTE_GPU_MODEL}"
fi
if [ -n "${AGENT_COMPUTE_SNSSAI:-}" ]; then
    set -- "$@" --compute-snssai "${AGENT_COMPUTE_SNSSAI}"
fi
if [ -n "${AGENT_COMPUTE_MAX_DURATION_MS:-}" ]; then
    set -- "$@" --compute-max-duration-ms "${AGENT_COMPUTE_MAX_DURATION_MS}"
fi
if [ -n "${AGENT_COMPUTE_PLACEMENT_REGION:-}" ]; then
    set -- "$@" --compute-placement-region "${AGENT_COMPUTE_PLACEMENT_REGION}"
fi
if [ -n "${AGENT_COMPUTE_DATA_RESIDENCY_REGION:-}" ]; then
    set -- "$@" --compute-data-residency-region "${AGENT_COMPUTE_DATA_RESIDENCY_REGION}"
fi
if [ -n "${AGENT_COMPUTE_UI_LOCALE:-}" ]; then
    set -- "$@" --compute-ui-locale "${AGENT_COMPUTE_UI_LOCALE}"
fi
if [ -n "${AGENT_COMPUTE_REQUEST_ID:-}" ]; then
    set -- "$@" --compute-request-id "${AGENT_COMPUTE_REQUEST_ID}"
fi
if ! is_true "${AGENT_COMPUTE_ALLOW_BASE_QOS:-true}"; then
    set -- "$@" --no-allow-base-qos
fi
if ! is_true "${AGENT_COMPUTE_QUERY_SESSION:-true}"; then
    set -- "$@" --no-query-session
fi
if is_true "${AGENT_FULL_INTERFACE_SUITE:-true}"; then
    set -- "$@" --full-interface-suite
else
    set -- "$@" --no-full-interface-suite
fi
if [ -z "${REGISTRATION_MODE}" ]; then
    if is_true "${AGENT_FORCE_REGISTRATION:-false}"; then
        REGISTRATION_MODE=force-register
    elif is_true "${AGENT_FRESH_REGISTRATION:-true}"; then
        REGISTRATION_MODE=fresh-register
    fi
fi
case "${REGISTRATION_MODE}" in
    force-register) set -- "$@" --force-registration ;;
    fresh-register) set -- "$@" --fresh-registration ;;
esac
if is_true "${AGENT_DEREGISTER_ON_EXIT:-true}"; then
    set -- "$@" --deregister-on-exit
fi

exec python /opt/agent-sdk/examples/agent_a_test.py "$@"
