#!/bin/sh
set -eu

if [ "${1:-}" = "--help" ]; then
    exec python /opt/agent-sdk/examples/agent_b_test.py --help
fi
if [ "$#" -ne 0 ]; then
    printf '%s\n' 'start-agent-b.sh accepts configuration through environment variables only' >&2
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
require_value MASQUE_URL "${MASQUE_URL:-}"

if [ ! -c /dev/net/tun ]; then
    printf '%s\n' '/dev/net/tun is unavailable; start the container with --device /dev/net/tun' >&2
    exit 1
fi

AGENT_LOG_FILE="${AGENT_LOG_FILE:-/var/log/agent-sdk/agent-b.log}"
AGENT_CAMERA_ID="${AGENT_CAMERA_ID:-0}"
AGENT_VIDEO_SOURCE="${AGENT_VIDEO_SOURCE:-file}"
AGENT_VIDEO_FILE="${AGENT_VIDEO_FILE:-/opt/agent-sdk/examples/assets/video-offload-test.mp4}"
case "${AGENT_VIDEO_SOURCE}" in
    file)
        if [ ! -f "${AGENT_VIDEO_FILE}" ]; then
            printf 'local test video is unavailable: %s\n' "${AGENT_VIDEO_FILE}" >&2
            exit 1
        fi
        ;;
    camera)
        if [ ! -c "/dev/video${AGENT_CAMERA_ID}" ]; then
            printf 'camera is unavailable inside the container: /dev/video%s\n' "${AGENT_CAMERA_ID}" >&2
            printf '%s\n' 'Map a V4L2 camera into the container before starting Agent B.' >&2
            exit 1
        fi
        ;;
    *)
        printf 'AGENT_VIDEO_SOURCE must be file or camera: %s\n' "${AGENT_VIDEO_SOURCE}" >&2
        exit 2
        ;;
esac
mkdir -p "$(dirname -- "${AGENT_LOG_FILE}")" "${XDG_STATE_HOME:-/var/lib/agent-sdk}"

set -- \
    --runtime-ip "${AGENT_RUNTIME_IP}" \
    --runtime-port "${AGENT_RUNTIME_PORT:-8080}" \
    --tcp-port "${AGENT_TCP_PORT:-4001}" \
    --udp-port "${AGENT_UDP_PORT:-28443}" \
    --masque-url "${MASQUE_URL}" \
    --tun-name "${AGENT_TUN_NAME:-agent_tun_b}" \
    --tun-mtu "${AGENT_TUN_MTU:-1280}" \
    --agent-name "${AGENT_NAME:-Agent-B}" \
    --owner "${AGENT_OWNER:-ab-test-owner-b}" \
    --description "${AGENT_DESCRIPTION:-Agent B video offload producer test}" \
    --region "${AGENT_REGION:-CN}" \
    --capability "${AGENT_CAPABILITY:-video_rendering}" \
    --priority "${AGENT_PRIORITY:-1}" \
    --wait-timeout "${AGENT_WAIT_TIMEOUT:-0}" \
    --video-source "${AGENT_VIDEO_SOURCE}" \
    --video-file "${AGENT_VIDEO_FILE}" \
    --camera-id "${AGENT_CAMERA_ID}" \
    --video-width "${AGENT_VIDEO_WIDTH:-1280}" \
    --video-height "${AGENT_VIDEO_HEIGHT:-720}" \
    --video-fps "${AGENT_VIDEO_FPS:-30}" \
    --video-bitrate-kbps "${AGENT_VIDEO_BITRATE_KBPS:-2500}" \
    --media-timeout "${AGENT_MEDIA_TIMEOUT:-30}" \
    --session-close-timeout "${AGENT_SESSION_CLOSE_TIMEOUT:-60}" \
    --max-sessions "${AGENT_MAX_SESSIONS:-1}" \
    --log-file "${AGENT_LOG_FILE}" \
    --log-level "${AGENT_LOG_LEVEL:-INFO}"

if [ -n "${MASQUE_TOKEN:-}" ]; then
    set -- "$@" --masque-token "${MASQUE_TOKEN}"
fi
if [ -n "${AGENT_THIRD_PARTY_PRIVATE_KEY:-}" ]; then
    set -- "$@" --third-party-private-key "${AGENT_THIRD_PARTY_PRIVATE_KEY}"
fi
if ! is_true "${AGENT_LOOP_VIDEO:-true}"; then
    set -- "$@" --no-loop-video
fi
if is_true "${AGENT_FRESH_REGISTRATION:-true}"; then
    set -- "$@" --fresh-registration
fi
if is_true "${AGENT_DEREGISTER_ON_EXIT:-true}"; then
    set -- "$@" --deregister-on-exit
fi

exec python /opt/agent-sdk/examples/agent_b_test.py "$@"
