#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
MOCK_VERSION="${MOCK_VERSION:-0.3.0}"
IMAGE_TAG="agent-compute-sandbox-mock:${MOCK_VERSION}-arm64"
OUTPUT_PATH="${REPOSITORY_ROOT}/dist/compute-mock/agent-compute-sandbox-mock-${MOCK_VERSION}-linux-arm64.tar.gz"
SMOKE_USER_PORT="${MOCK_SMOKE_USER_PORT:-38502}"
SMOKE_MANAGEMENT_PORT="${MOCK_SMOKE_MANAGEMENT_PORT:-38501}"
SMOKE_ASR_PORT="${MOCK_SMOKE_ASR_PORT:-39004}"
SMOKE_INTENT_PORT="${MOCK_SMOKE_INTENT_PORT:-38011}"
EXPORT_IMAGE=1

usage() {
    printf '%s\n' \
        "Usage: $(basename "$0") [--tag IMAGE] [--output FILE] [--smoke-user-port PORT] [--smoke-management-port PORT] [--no-export]" \
        '' \
        'Build, run the complete Sandbox smoke test and export the linux/arm64 image.'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag) IMAGE_TAG="$2"; shift 2 ;;
        --output) OUTPUT_PATH="$2"; shift 2 ;;
        --smoke-user-port) SMOKE_USER_PORT="$2"; shift 2 ;;
        --smoke-management-port) SMOKE_MANAGEMENT_PORT="$2"; shift 2 ;;
        --smoke-port) SMOKE_USER_PORT="$2"; shift 2 ;;
        --no-export) EXPORT_IMAGE=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

for command_name in docker curl gzip sha256sum; do
    command -v "${command_name}" >/dev/null 2>&1 || {
        printf 'required command not found: %s\n' "${command_name}" >&2
        exit 1
    }
done

docker info >/dev/null
docker buildx build \
    --platform linux/arm64 \
    --provenance=false \
    --load \
    --build-arg "MOCK_VERSION=${MOCK_VERSION}" \
    --file "${SCRIPT_DIR}/Dockerfile" \
    --tag "${IMAGE_TAG}" \
    "${SCRIPT_DIR}"

actual_platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "${IMAGE_TAG}")"
if [[ "${actual_platform}" != "linux/arm64" ]]; then
    printf 'built image has platform %s; expected linux/arm64\n' "${actual_platform}" >&2
    exit 1
fi

container_name="agent-compute-mock-smoke-$$"
cleanup() {
    docker rm -f "${container_name}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run --detach --rm \
    --platform linux/arm64 \
    --name "${container_name}" \
    --network host \
    --entrypoint python \
    -e SANDBOX_INTENT_URL="http://127.0.0.1:${SMOKE_INTENT_PORT}/api/v1/intent" \
    -e ASR_INTENT_URL="http://127.0.0.1:${SMOKE_INTENT_PORT}/api/v1/intent" \
    -e SANDBOX_ASR_URL="http://127.0.0.1:${SMOKE_ASR_PORT}/api/v1/transcribe" \
    "${IMAGE_TAG}" \
    /opt/mock-video-server/server.py \
    --host 127.0.0.1 \
    --user-port "${SMOKE_USER_PORT}" \
    --management-host 127.0.0.1 \
    --management-port "${SMOKE_MANAGEMENT_PORT}" \
    --asr-host 127.0.0.1 \
    --asr-port "${SMOKE_ASR_PORT}" \
    --intent-host 127.0.0.1 \
    --intent-port "${SMOKE_INTENT_PORT}" \
    --public-ip 127.0.0.1 >/dev/null

healthy=0
for _ in $(seq 1 240); do
    if curl --fail --silent "http://127.0.0.1:${SMOKE_USER_PORT}/healthz" >/dev/null \
        && curl --fail --silent "http://127.0.0.1:${SMOKE_MANAGEMENT_PORT}/healthz" >/dev/null \
        && curl --fail --silent "http://127.0.0.1:${SMOKE_ASR_PORT}/health" >/dev/null \
        && curl --fail --silent "http://127.0.0.1:${SMOKE_INTENT_PORT}/health" >/dev/null; then
        healthy=1
        break
    fi
    sleep 0.5
done
if [[ "${healthy}" -ne 1 ]]; then
    docker logs "${container_name}" >&2
    printf '%s\n' 'Sandbox mock did not become healthy' >&2
    exit 1
fi

docker run --rm \
    --platform linux/arm64 \
    --network host \
    --entrypoint python \
    "${IMAGE_TAG}" \
    /opt/mock-video-server/smoke_client.py \
    --management-url "http://127.0.0.1:${SMOKE_MANAGEMENT_PORT}" \
    --base-url "http://127.0.0.1:${SMOKE_USER_PORT}" \
    --asr-url "http://127.0.0.1:${SMOKE_ASR_PORT}" \
    --media-timeout 60

cleanup
trap - EXIT

if [[ "${EXPORT_IMAGE}" -eq 1 ]]; then
    mkdir -p "$(dirname -- "${OUTPUT_PATH}")"
    temporary_output="${OUTPUT_PATH}.tmp.$$"
    trap 'rm -f -- "${temporary_output}"' EXIT
    docker save "${IMAGE_TAG}" | gzip -1 >"${temporary_output}"
    mv -f -- "${temporary_output}" "${OUTPUT_PATH}"
    (
        cd -- "$(dirname -- "${OUTPUT_PATH}")"
        sha256sum "$(basename -- "${OUTPUT_PATH}")" \
            >"$(basename -- "${OUTPUT_PATH}").sha256"
    )
    trap - EXIT
    printf 'exported=%s\n' "${OUTPUT_PATH}"
fi

printf 'image=%s\nplatform=%s\n' "${IMAGE_TAG}" "${actual_platform}"
