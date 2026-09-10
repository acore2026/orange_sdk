#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SDK_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
SDK_VERSION="$(awk '$1 == "version" && $2 == "=" { print $3; exit }' "${SDK_ROOT}/setup.cfg")"
IMAGE_TAG="agent-connect-sdk:${SDK_VERSION}-arm64"
OUTPUT_PATH="${SDK_ROOT}/dist/arm64/agent-connect-sdk-${SDK_VERSION}-linux-arm64.tar.gz"
EXPORT_IMAGE=1

usage() {
    printf '%s\n' \
        "Usage: $(basename "$0") [--tag IMAGE] [--output FILE] [--no-export]" \
        '' \
        'Build and validate the Agent Connect SDK linux/arm64 image on x86_64 or arm64.'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag)
            [[ $# -ge 2 ]] || { printf '%s\n' '--tag requires a value' >&2; exit 2; }
            IMAGE_TAG="$2"
            shift 2
            ;;
        --output)
            [[ $# -ge 2 ]] || { printf '%s\n' '--output requires a value' >&2; exit 2; }
            OUTPUT_PATH="$2"
            shift 2
            ;;
        --no-export)
            EXPORT_IMAGE=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'unknown option: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

for command_name in docker awk gzip sha256sum; do
    command -v "${command_name}" >/dev/null 2>&1 || {
        printf 'required command not found: %s\n' "${command_name}" >&2
        exit 1
    }
done

docker info >/dev/null
docker buildx version >/dev/null
if ! docker buildx inspect --bootstrap 2>/dev/null \
    | sed -n 's/^Platforms:[[:space:]]*//p' \
    | tr ',' '\n' \
    | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' \
    | grep -Eq '^linux/arm64($|/)'; then
    printf '%s\n' 'Docker Buildx cannot execute linux/arm64; install QEMU/binfmt support first.' >&2
    exit 1
fi

docker buildx build \
    --platform linux/arm64 \
    --provenance=false \
    --load \
    --build-arg "SDK_VERSION=${SDK_VERSION}" \
    --file "${SCRIPT_DIR}/Dockerfile" \
    --tag "${IMAGE_TAG}" \
    "${SDK_ROOT}"

actual_platform="$(
    docker image inspect --platform linux/arm64 \
        --format '{{.Os}}/{{.Architecture}}' "${IMAGE_TAG}"
)"
if [[ "${actual_platform}" != "linux/arm64" ]]; then
    printf 'built image has platform %s; expected linux/arm64\n' "${actual_platform}" >&2
    exit 1
fi

docker run --rm --platform linux/arm64 --entrypoint python "${IMAGE_TAG}" \
    -c 'import agent_sdk; print(agent_sdk.__version__)'
docker run --rm --platform linux/arm64 --entrypoint python "${IMAGE_TAG}" \
    -m pip check
docker run --rm --platform linux/arm64 --entrypoint agent-sdk-self-check \
    "${IMAGE_TAG}"
docker run --rm --platform linux/arm64 --entrypoint sh "${IMAGE_TAG}" -c \
    'grep -F "AGENT_FRESH_REGISTRATION:-true" /usr/local/bin/start-agent-a.sh >/dev/null &&
     grep -F "AGENT_DEREGISTER_ON_EXIT:-true" /usr/local/bin/start-agent-a.sh >/dev/null &&
     grep -F "AGENT_FRESH_REGISTRATION:-true" /usr/local/bin/start-agent-b.sh >/dev/null &&
     grep -F "AGENT_DEREGISTER_ON_EXIT:-true" /usr/local/bin/start-agent-b.sh >/dev/null'
docker run --rm --platform linux/arm64 "${IMAGE_TAG}"

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
