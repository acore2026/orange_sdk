#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
IMAGE_TAG="${COMPUTE_MOCK_IMAGE:-agent-compute-sandbox-mock:0.3.0-arm64}"
IMAGE_ARCHIVE="${SANDBOX_IMAGE_ARCHIVE:-}"
COMPOSE_FILE="${SANDBOX_COMPOSE_FILE:-${SCRIPT_DIR}/docker-compose.n6.yml}"
CONTAINER_NAME="agent-sdk-mock-video-server"
NETWORK_NAME="compose_n6"
START_TIMEOUT="${SANDBOX_START_TIMEOUT:-120}"
RUN_SMOKE=0

usage() {
    printf '%s\n' \
        'Usage: ./start_sandbox.sh [--smoke]' \
        '' \
        'Import and start or restart the bundled ARM64 Compute Sandbox Mock.' \
        '  --smoke  run the complete HTTP/WebRTC smoke test after startup' \
        '' \
        'Optional environment:' \
        '  SANDBOX_IMAGE_ARCHIVE  image archive path' \
        '  SANDBOX_COMPOSE_FILE   Compose file path' \
        '  SANDBOX_START_TIMEOUT  health timeout in seconds; default: 120' \
        '  SANDBOX_RELOAD_IMAGE   set to 1 to reload an already installed image' \
        '  SANDBOX_SKIP_FIREWALL  set to 1 to skip the host firewall allowance'
}

case "${1:-}" in
    '') ;;
    --smoke) RUN_SMOKE=1 ;;
    -h|--help) usage; exit 0 ;;
    *)
        printf 'unknown option: %s\n' "$1" >&2
        usage >&2
        exit 2
        ;;
esac
if [ "$#" -gt 1 ]; then
    usage >&2
    exit 2
fi

case "${START_TIMEOUT}" in
    ''|*[!0-9]*|0)
        printf 'SANDBOX_START_TIMEOUT must be a positive integer: %s\n' "${START_TIMEOUT}" >&2
        exit 2
        ;;
esac

for required_command in docker gzip sha256sum; do
    if ! command -v "${required_command}" >/dev/null 2>&1; then
        printf 'required command is unavailable: %s\n' "${required_command}" >&2
        exit 1
    fi
done
docker compose version >/dev/null

if [ ! -f "${COMPOSE_FILE}" ]; then
    printf 'Compose file not found: %s\n' "${COMPOSE_FILE}" >&2
    exit 1
fi

if [ -f "${SCRIPT_DIR}/manifest.sha256" ]; then
    printf 'Verifying bundle manifest ...\n'
    (
        cd "${SCRIPT_DIR}"
        sha256sum -c manifest.sha256
    )
fi

if [ -z "${IMAGE_ARCHIVE}" ]; then
    for candidate in \
        "${SCRIPT_DIR}/agent-compute-sandbox-mock-0.3.0-linux-arm64.tar.gz" \
        "${SCRIPT_DIR}/../dist/compute-mock/agent-compute-sandbox-mock-0.3.0-linux-arm64.tar.gz"
    do
        if [ -f "${candidate}" ]; then
            IMAGE_ARCHIVE="${candidate}"
            break
        fi
    done
fi
if [ -z "${IMAGE_ARCHIVE}" ] || [ ! -f "${IMAGE_ARCHIVE}" ]; then
    printf 'Sandbox ARM64 image archive not found.\n' >&2
    printf 'Set SANDBOX_IMAGE_ARCHIVE or place it beside start_sandbox.sh.\n' >&2
    exit 1
fi
if [ ! -f "${IMAGE_ARCHIVE}.sha256" ]; then
    printf 'Image checksum file not found: %s.sha256\n' "${IMAGE_ARCHIVE}" >&2
    exit 1
fi
(
    cd "$(dirname -- "${IMAGE_ARCHIVE}")"
    sha256sum -c "$(basename -- "${IMAGE_ARCHIVE}.sha256")"
)

if ! docker image inspect "${IMAGE_TAG}" >/dev/null 2>&1 \
    || [ "${SANDBOX_RELOAD_IMAGE:-0}" = "1" ]; then
    printf 'Importing %s ...\n' "${IMAGE_ARCHIVE}"
    gzip -dc "${IMAGE_ARCHIVE}" | docker load
fi

PLATFORM="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "${IMAGE_TAG}")"
if [ "${PLATFORM}" != "linux/arm64" ]; then
    printf 'Image %s has platform %s; expected linux/arm64.\n' \
        "${IMAGE_TAG}" "${PLATFORM}" >&2
    exit 1
fi

if ! docker network inspect "${NETWORK_NAME}" >/dev/null 2>&1; then
    printf 'Creating Docker N6 network %s ...\n' "${NETWORK_NAME}"
    docker network create \
        --driver bridge \
        --subnet 172.30.0.0/24 \
        --gateway 172.30.0.1 \
        "${NETWORK_NAME}" >/dev/null
fi

existing_container="$(
    docker ps -aq --filter "name=^/${CONTAINER_NAME}$" 2>/dev/null || true
)"
if [ -n "${existing_container}" ]; then
    printf 'Removing existing Sandbox container %s ...\n' "${CONTAINER_NAME}"
    docker rm -f "${existing_container}" >/dev/null
fi

export COMPUTE_MOCK_IMAGE="${IMAGE_TAG}"
compose() {
    docker compose -f "${COMPOSE_FILE}" "$@"
}

# Docker 29 的 nftables 后端在 raw PREROUTING 为每个容器 IP 生成防伪 DROP
# （iifname != 所属网桥即丢弃），并且跨网桥 FORWARD 默认被隔离。CMF 等核心网
# 组件位于其他网桥时无法访问 Sandbox 的管理面/用户面。以下放行按 Sandbox 容器
# IP 精确限定、幂等可重跑；主机重启或 Docker 重建网络规则后重新执行本脚本即可。
ensure_sandbox_reachable() {
    if [ "${SANDBOX_SKIP_FIREWALL:-0}" = "1" ]; then
        printf 'SANDBOX_SKIP_FIREWALL=1: skipping host firewall allowance.\n'
        return 0
    fi
    if ! command -v iptables >/dev/null 2>&1; then
        printf 'WARNING: iptables is unavailable; cross-network access to the Sandbox may be blocked.\n' >&2
        return 0
    fi
    sandbox_ip="$(
        docker inspect \
            -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
            "${CONTAINER_NAME}" 2>/dev/null || true
    )"
    if [ -z "${sandbox_ip}" ]; then
        printf 'WARNING: Sandbox container IP not found; skipping firewall allowance.\n' >&2
        return 0
    fi
    bridge_if="$(
        docker network inspect "${NETWORK_NAME}" \
            -f '{{index .Options "com.docker.network.bridge.name"}}' 2>/dev/null || true
    )"
    if [ -z "${bridge_if}" ]; then
        network_id="$(docker network inspect "${NETWORK_NAME}" -f '{{.Id}}' 2>/dev/null || true)"
        if [ -n "${network_id}" ]; then
            bridge_if="br-$(printf '%s' "${network_id}" | cut -c1-12)"
        fi
    fi
    if [ -z "${bridge_if}" ]; then
        printf 'WARNING: bridge interface of %s not found; skipping firewall allowance.\n' \
            "${NETWORK_NAME}" >&2
        return 0
    fi
    if ! iptables -w -t raw -C PREROUTING -d "${sandbox_ip}" -j ACCEPT >/dev/null 2>&1; then
        iptables -w -t raw -I PREROUTING 1 -d "${sandbox_ip}" -j ACCEPT >/dev/null 2>&1 \
            || printf 'WARNING: failed to insert raw PREROUTING allowance for %s.\n' \
                "${sandbox_ip}" >&2
    fi
    if ! iptables -w -C DOCKER-USER -o "${bridge_if}" -d "${sandbox_ip}" -j ACCEPT >/dev/null 2>&1; then
        iptables -w -I DOCKER-USER 1 -o "${bridge_if}" -d "${sandbox_ip}" -j ACCEPT >/dev/null 2>&1 \
            || printf 'WARNING: failed to insert DOCKER-USER inbound allowance for %s.\n' \
                "${sandbox_ip}" >&2
    fi
    if ! iptables -w -C DOCKER-USER -i "${bridge_if}" -s "${sandbox_ip}" -j ACCEPT >/dev/null 2>&1; then
        iptables -w -I DOCKER-USER 1 -i "${bridge_if}" -s "${sandbox_ip}" -j ACCEPT >/dev/null 2>&1 \
            || printf 'WARNING: failed to insert DOCKER-USER outbound allowance for %s.\n' \
                "${sandbox_ip}" >&2
    fi
    printf 'Host firewall allowance ensured for %s via %s (raw PREROUTING + DOCKER-USER).\n' \
        "${sandbox_ip}" "${bridge_if}"
}

printf 'Starting Sandbox from %s ...\n' "${IMAGE_TAG}"
compose up -d --no-build --force-recreate mock-video-server
container_id="$(compose ps -q mock-video-server)"
if [ -z "${container_id}" ]; then
    printf 'Compose did not return a Sandbox container ID.\n' >&2
    exit 1
fi

elapsed=0
while [ "${elapsed}" -lt "${START_TIMEOUT}" ]; do
    health="$(
        docker inspect \
            --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
            "${container_id}" 2>/dev/null || true
    )"
    case "${health}" in
        healthy)
            compose ps mock-video-server
            ensure_sandbox_reachable
            printf 'Sandbox management is healthy at http://172.30.0.10:28501.\n'
            printf 'Sandbox user plane is healthy at http://172.30.0.10:28502.\n'
            if [ "${RUN_SMOKE}" -eq 1 ]; then
                printf 'Running complete HTTP/WebRTC smoke test ...\n'
                docker run --rm \
                    --platform linux/arm64 \
                    --network "${NETWORK_NAME}" \
                    --entrypoint python \
                    -e FREE6GC_COMPUTING_SANDBOX_MANAGEMENT_TOKEN="${SANDBOX_MANAGEMENT_TOKEN:-}" \
                    "${IMAGE_TAG}" \
                    /opt/mock-video-server/smoke_client.py \
                    --management-url http://172.30.0.10:28501 \
                    --base-url http://172.30.0.10:28502 \
                    --asr-url http://172.30.0.10:9004 \
                    --media-timeout 60
            fi
            exit 0
            ;;
        unhealthy|exited|dead)
            compose logs --tail 200 mock-video-server >&2 || true
            printf 'Sandbox entered state %s before becoming ready.\n' "${health}" >&2
            exit 1
            ;;
    esac
    sleep 1
    elapsed=$((elapsed + 1))
done

compose logs --tail 200 mock-video-server >&2 || true
printf 'Timed out after %s seconds waiting for Sandbox health.\n' "${START_TIMEOUT}" >&2
exit 1
