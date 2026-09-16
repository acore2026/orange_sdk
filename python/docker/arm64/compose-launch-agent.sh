#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SERVICE="${1:-}"
if [ "$#" -gt 0 ]; then
    shift
fi

case "${SERVICE}" in
    agent-a) READY_EVENT=GROUP_CREATED ;;
    agent-b) READY_EVENT=B_READY ;;
    *)
        printf 'internal error: unsupported service %s\n' "${SERVICE}" >&2
        exit 2
        ;;
esac

usage() {
    printf '%s\n' \
        "Usage: ./start-${SERVICE}.sh fresh-register|force-register" \
        '' \
        "Start or restart the ${SERVICE} Docker Compose service and wait for ${READY_EVENT}." \
        '  fresh-register  stop gracefully, deregister the saved identity, then register again' \
        '  force-register  kill without deregistration, reset local state 1, then register again' \
        '' \
        'Optional environment:' \
        '  AGENT_ENV_FILE       Compose env file; default: .env beside this script' \
        '  AGENT_COMPOSE_FILE   Compose file; default: docker-compose.same-host.yml' \
        '  AGENT_IMAGE_ARCHIVE  Local .tar.gz image archive used when the image is absent' \
        '  AGENT_START_TIMEOUT  Readiness timeout in seconds; default: 120'
}

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    usage
    exit 0
fi
if [ "$#" -ne 1 ]; then
    usage >&2
    exit 2
fi

REGISTRATION_MODE="$1"
case "${REGISTRATION_MODE}" in
    fresh-register|force-register) ;;
    *)
        printf 'unknown registration mode: %s\n' "${REGISTRATION_MODE}" >&2
        usage >&2
        exit 2
        ;;
esac

COMPOSE_FILE="${AGENT_COMPOSE_FILE:-${SCRIPT_DIR}/docker-compose.same-host.yml}"
ENV_FILE="${AGENT_ENV_FILE:-${SCRIPT_DIR}/.env}"
SDK_VERSION="${AGENT_SDK_VERSION:-0.17.9}"
START_TIMEOUT="${AGENT_START_TIMEOUT:-120}"

case "${START_TIMEOUT}" in
    ''|*[!0-9]*|0)
        printf 'AGENT_START_TIMEOUT must be a positive integer: %s\n' "${START_TIMEOUT}" >&2
        exit 2
        ;;
esac

for required_command in docker gzip awk grep; do
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
if [ ! -f "${ENV_FILE}" ]; then
    if [ "${ENV_FILE}" = "${SCRIPT_DIR}/.env" ] && [ -f "${SCRIPT_DIR}/same-host.env.example" ]; then
        cp "${SCRIPT_DIR}/same-host.env.example" "${ENV_FILE}"
        printf 'Created %s from same-host.env.example.\n' "${ENV_FILE}"
    else
        printf 'Compose env file not found: %s\n' "${ENV_FILE}" >&2
        exit 1
    fi
fi

export AGENT_REGISTRATION_MODE="${REGISTRATION_MODE}"
compose() {
    docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

IMAGE="$(compose config --images | awk 'NF { print; exit }')"
if [ -z "${IMAGE}" ]; then
    printf 'Compose configuration did not resolve an image.\n' >&2
    exit 1
fi

if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    IMAGE_ARCHIVE="${AGENT_IMAGE_ARCHIVE:-}"
    if [ -z "${IMAGE_ARCHIVE}" ]; then
        for candidate in \
            "${SCRIPT_DIR}/agent-connect-sdk-${SDK_VERSION}-linux-arm64.tar.gz" \
            "${SCRIPT_DIR}/../../dist/arm64/agent-connect-sdk-${SDK_VERSION}-linux-arm64.tar.gz"
        do
            if [ -f "${candidate}" ]; then
                IMAGE_ARCHIVE="${candidate}"
                break
            fi
        done
    fi
    if [ -z "${IMAGE_ARCHIVE}" ] || [ ! -f "${IMAGE_ARCHIVE}" ]; then
        printf 'Docker image is absent: %s\n' "${IMAGE}" >&2
        printf 'Set AGENT_IMAGE_ARCHIVE to the ARM64 image archive.\n' >&2
        exit 1
    fi
    if [ -f "${IMAGE_ARCHIVE}.sha256" ] && command -v sha256sum >/dev/null 2>&1; then
        (
            cd "$(dirname -- "${IMAGE_ARCHIVE}")"
            sha256sum -c "$(basename -- "${IMAGE_ARCHIVE}.sha256")"
        )
    fi
    printf 'Importing %s ...\n' "${IMAGE_ARCHIVE}"
    gzip -dc "${IMAGE_ARCHIVE}" | docker load
fi

PLATFORM="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "${IMAGE}")"
if [ "${PLATFORM}" != "linux/arm64" ]; then
    printf 'Image %s has platform %s; expected linux/arm64.\n' "${IMAGE}" "${PLATFORM}" >&2
    exit 1
fi

if [ "${REGISTRATION_MODE}" = "force-register" ] \
    && [ -n "$(compose ps -aq "${SERVICE}")" ]; then
    printf 'Force-stopping the old %s container without running its deregistration hook ...\n' "${SERVICE}"
    compose kill "${SERVICE}" >/dev/null 2>&1 || true
    compose rm -f "${SERVICE}" >/dev/null
fi

printf 'Starting or restarting %s with %s using %s ...\n' \
    "${SERVICE}" "${REGISTRATION_MODE}" "${IMAGE}"
compose up -d --no-deps --force-recreate "${SERVICE}"
CONTAINER_ID="$(compose ps -q "${SERVICE}")"
if [ -z "${CONTAINER_ID}" ]; then
    printf 'Compose did not return a container ID for %s.\n' "${SERVICE}" >&2
    exit 1
fi

elapsed=0
while [ "${elapsed}" -lt "${START_TIMEOUT}" ]; do
    logs="$(docker logs "${CONTAINER_ID}" 2>&1 || true)"
    if printf '%s\n' "${logs}" | grep -F '"event": "TEST_FAILED"' >/dev/null 2>&1; then
        printf '%s\n' "${logs}" >&2
        printf '%s failed before becoming ready.\n' "${SERVICE}" >&2
        exit 1
    fi
    if printf '%s\n' "${logs}" | grep -F "\"event\": \"${READY_EVENT}\"" >/dev/null 2>&1; then
        compose ps "${SERVICE}"
        compose logs --tail 100 "${SERVICE}"
        printf '%s is ready (%s).\n' "${SERVICE}" "${READY_EVENT}"
        exit 0
    fi
    running="$(docker inspect --format '{{.State.Running}}' "${CONTAINER_ID}" 2>/dev/null || true)"
    if [ "${running}" != "true" ]; then
        printf '%s\n' "${logs}" >&2
        printf '%s stopped before becoming ready.\n' "${SERVICE}" >&2
        exit 1
    fi
    sleep 1
    elapsed=$((elapsed + 1))
done

compose logs --tail 200 "${SERVICE}" >&2 || true
printf 'Timed out after %s seconds waiting for %s from %s.\n' \
    "${START_TIMEOUT}" "${READY_EVENT}" "${SERVICE}" >&2
exit 1
