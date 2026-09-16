#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
MOCK_VERSION="${MOCK_VERSION:-0.2.0}"
IMAGE_ARCHIVE="${SANDBOX_IMAGE_ARCHIVE:-${REPOSITORY_ROOT}/dist/compute-mock/agent-compute-sandbox-mock-${MOCK_VERSION}-linux-arm64.tar.gz}"
OUTPUT_PATH="${SANDBOX_BUNDLE_OUTPUT:-${REPOSITORY_ROOT}/dist/sandbox.tar.gz}"
STAGING_ROOT="${REPOSITORY_ROOT}/dist/.sandbox-package-$$"
BUNDLE_ROOT="${STAGING_ROOT}/sandbox"

cleanup() {
    rm -rf -- "${STAGING_ROOT}"
}
trap cleanup EXIT

for required_command in tar gzip sha256sum docker; do
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
    docker image inspect --format '{{.Os}}/{{.Architecture}}' \
        "agent-compute-sandbox-mock:${MOCK_VERSION}-arm64"
)"
if [[ "${actual_platform}" != "linux/arm64" ]]; then
    printf 'Sandbox image platform is %s; expected linux/arm64.\n' "${actual_platform}" >&2
    exit 1
fi

mkdir -p "${BUNDLE_ROOT}/source" "$(dirname -- "${OUTPUT_PATH}")"
install -m 0755 "${SCRIPT_DIR}/start_sandbox.sh" "${BUNDLE_ROOT}/start_sandbox.sh"
install -m 0644 "${SCRIPT_DIR}/docker-compose.n6.yml" "${BUNDLE_ROOT}/docker-compose.n6.yml"
install -m 0644 "${SCRIPT_DIR}/README.md" "${BUNDLE_ROOT}/README.md"
install -m 0644 "${IMAGE_ARCHIVE}" "${BUNDLE_ROOT}/$(basename -- "${IMAGE_ARCHIVE}")"
install -m 0644 "${IMAGE_ARCHIVE}.sha256" "${BUNDLE_ROOT}/$(basename -- "${IMAGE_ARCHIVE}.sha256")"

for source_file in \
    Dockerfile \
    build-image.sh \
    docker-compose.n6.yml \
    entrypoint.sh \
    requirements.txt \
    server.py \
    smoke_client.py
do
    install -m 0644 "${SCRIPT_DIR}/${source_file}" "${BUNDLE_ROOT}/source/${source_file}"
done
chmod 0755 \
    "${BUNDLE_ROOT}/source/build-image.sh" \
    "${BUNDLE_ROOT}/source/entrypoint.sh" \
    "${BUNDLE_ROOT}/source/smoke_client.py"

(
    cd -- "${BUNDLE_ROOT}"
    find . -type f ! -name manifest.sha256 -print0 \
        | sort -z \
        | xargs -0 sha256sum >manifest.sha256
)

temporary_output="${OUTPUT_PATH}.tmp.$$"
tar -C "${STAGING_ROOT}" -czf "${temporary_output}" sandbox
mv -f -- "${temporary_output}" "${OUTPUT_PATH}"
(
    cd -- "$(dirname -- "${OUTPUT_PATH}")"
    sha256sum "$(basename -- "${OUTPUT_PATH}")" \
        >"$(basename -- "${OUTPUT_PATH}").sha256"
)

printf 'bundle=%s\nchecksum=%s.sha256\nplatform=%s\n' \
    "${OUTPUT_PATH}" "${OUTPUT_PATH}" "${actual_platform}"
