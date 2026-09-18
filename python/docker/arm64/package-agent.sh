#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SDK_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
REPOSITORY_ROOT="$(cd -- "${SDK_ROOT}/.." && pwd)"
SDK_VERSION="$(awk '$1 == "version" && $2 == "=" { print $3; exit }' "${SDK_ROOT}/setup.cfg")"
IMAGE_ARCHIVE="${AGENT_IMAGE_ARCHIVE:-${SDK_ROOT}/dist/arm64/agent-connect-sdk-${SDK_VERSION}-linux-arm64.tar.gz}"
OUTPUT_PATH="${AGENT_BUNDLE_OUTPUT:-${REPOSITORY_ROOT}/dist/agent.tar.gz}"
STAGING_ROOT="${REPOSITORY_ROOT}/dist/.agent-package-$$"
BUNDLE_ROOT="${STAGING_ROOT}/agent"

cleanup() {
    rm -rf -- "${STAGING_ROOT}"
}
trap cleanup EXIT

for required_command in tar gzip sha256sum docker awk; do
    command -v "${required_command}" >/dev/null 2>&1 || {
        printf 'required command is unavailable: %s\n' "${required_command}" >&2
        exit 1
    }
done

if [[ ! -f "${IMAGE_ARCHIVE}" || ! -f "${IMAGE_ARCHIVE}.sha256" ]]; then
    printf 'ARM64 image archive or checksum is missing: %s\n' "${IMAGE_ARCHIVE}" >&2
    printf 'Run %s/build-image.sh first.\n' "${SCRIPT_DIR}" >&2
    exit 1
fi
(
    cd -- "$(dirname -- "${IMAGE_ARCHIVE}")"
    sha256sum -c "$(basename -- "${IMAGE_ARCHIVE}.sha256")"
)

actual_platform="$(
    docker image inspect --platform linux/arm64 \
        --format '{{.Os}}/{{.Architecture}}' \
        "agent-connect-sdk:${SDK_VERSION}-arm64"
)"
if [[ "${actual_platform}" != "linux/arm64" ]]; then
    printf 'Agent image platform is %s; expected linux/arm64.\n' "${actual_platform}" >&2
    exit 1
fi

mkdir -p "${BUNDLE_ROOT}" "$(dirname -- "${OUTPUT_PATH}")"
install -m 0755 "${SCRIPT_DIR}/start-agent-a.sh" "${BUNDLE_ROOT}/start-agent-a.sh"
install -m 0755 "${SCRIPT_DIR}/start-agent-b.sh" "${BUNDLE_ROOT}/start-agent-b.sh"
install -m 0755 "${SCRIPT_DIR}/compose-launch-agent.sh" "${BUNDLE_ROOT}/compose-launch-agent.sh"
install -m 0644 "${SCRIPT_DIR}/docker-compose.same-host.yml" "${BUNDLE_ROOT}/docker-compose.same-host.yml"
install -m 0644 "${SCRIPT_DIR}/same-host.env.example" "${BUNDLE_ROOT}/same-host.env.example"
install -m 0644 "${SCRIPT_DIR}/README.md" "${BUNDLE_ROOT}/README.md"
install -m 0644 "${IMAGE_ARCHIVE}" "${BUNDLE_ROOT}/$(basename -- "${IMAGE_ARCHIVE}")"
install -m 0644 "${IMAGE_ARCHIVE}.sha256" "${BUNDLE_ROOT}/$(basename -- "${IMAGE_ARCHIVE}.sha256")"

(
    cd -- "${BUNDLE_ROOT}"
    find . -type f ! -name manifest.sha256 -print0 \
        | sort -z \
        | xargs -0 sha256sum >manifest.sha256
)

temporary_output="${OUTPUT_PATH}.tmp.$$"
tar -C "${STAGING_ROOT}" -czf "${temporary_output}" agent
mv -f -- "${temporary_output}" "${OUTPUT_PATH}"
(
    cd -- "$(dirname -- "${OUTPUT_PATH}")"
    sha256sum "$(basename -- "${OUTPUT_PATH}")" \
        >"$(basename -- "${OUTPUT_PATH}").sha256"
)

printf 'bundle=%s\nchecksum=%s.sha256\nplatform=%s\n' \
    "${OUTPUT_PATH}" "${OUTPUT_PATH}" "${actual_platform}"
