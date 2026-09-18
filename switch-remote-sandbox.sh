#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $(basename "$0") <sandbox-ipv4>" >&2
  echo "Switch CMF to http://<sandbox-ipv4>:28501 and http://<sandbox-ipv4>:28502." >&2
}

valid_unicast_ipv4() {
  local address="$1"
  local first
  local -a octets

  IFS=. read -r -a octets <<<"${address}"
  [[ "${#octets[@]}" -eq 4 ]] || return 1
  for octet in "${octets[@]}"; do
    [[ "${octet}" =~ ^(0|[1-9][0-9]{0,2})$ ]] || return 1
    ((10#${octet} <= 255)) || return 1
  done
  first="${octets[0]}"
  ((10#${first} != 0 && 10#${first} != 127 && 10#${first} < 224)) || return 1
  [[ "${address}" != "255.255.255.255" ]]
}

if [[ "$#" -ne 1 ]]; then
  usage
  exit 2
fi

SANDBOX_IP="$1"
if ! valid_unicast_ipv4 "${SANDBOX_IP}"; then
  echo "Invalid Sandbox unicast IPv4 address: ${SANDBOX_IP}" >&2
  exit 2
fi

for command_name in curl docker; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Required command is not installed: ${command_name}" >&2
    exit 2
  fi
done
docker compose version >/dev/null

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../../.." && pwd)"
COMPOSE_ROOT="${REPO_ROOT}/free6gc-system/deployments/compose"
CMF_CONFIG="${COMPOSE_ROOT}/config/computing-cmf.yaml"
REMOTE_CORE_OVERRIDE="${COMPOSE_ROOT}/docker-compose.computing-remote-core.yaml"
MANAGEMENT_URL="http://${SANDBOX_IP}:28501"
SERVICE_ENDPOINT="http://${SANDBOX_IP}:28502"

if [[ "${FREE6GC_DEPLOYMENT_MODE:-joint}" == "atomic" ]]; then
  COMPOSE_FILE="${COMPOSE_ROOT}/docker-compose.atomic.yaml"
else
  COMPOSE_FILE="${COMPOSE_ROOT}/docker-compose.yaml"
fi

for required_file in "${CMF_CONFIG}" "${COMPOSE_FILE}" "${REMOTE_CORE_OVERRIDE}"; do
  if [[ ! -f "${required_file}" ]]; then
    echo "Required file does not exist: ${required_file}" >&2
    exit 2
  fi
done

echo "Checking remote Sandbox management endpoint: ${MANAGEMENT_URL}/healthz"
curl --fail --silent --show-error --max-time 10 "${MANAGEMENT_URL}/healthz" >/dev/null
echo "Checking remote Sandbox service endpoint: ${SERVICE_ENDPOINT}/healthz"
curl --fail --silent --show-error --max-time 10 "${SERVICE_ENDPOINT}/healthz" >/dev/null

backup="$(mktemp "${CMF_CONFIG}.backup.XXXXXX")"
replacement="$(mktemp "${CMF_CONFIG}.new.XXXXXX")"
cp -p "${CMF_CONFIG}" "${backup}"

cleanup() {
  rm -f "${backup}" "${replacement}"
}
trap cleanup EXIT

cat >"${replacement}" <<EOF
# Edit these two addresses before starting CMF when Sandbox runs remotely.
sandbox:
  # CMF-to-Sandbox management path. A DNS name or IP address is accepted.
  management_url: ${MANAGEMENT_URL}
  # UE-facing N6 path. Use an explicit IPv4 address and port.
  service_endpoint: ${SERVICE_ENDPOINT}
EOF
chmod --reference="${CMF_CONFIG}" "${replacement}"
mv "${replacement}" "${CMF_CONFIG}"

restore_previous_config() {
  echo "Restoring the previous CMF configuration" >&2
  mv "${backup}" "${CMF_CONFIG}"
  FREE6GC_COMPUTING_CMF_CONFIG_FILE="${CMF_CONFIG}" \
    docker compose \
      -f "${COMPOSE_FILE}" \
      -f "${REMOTE_CORE_OVERRIDE}" \
      up -d --no-deps --no-build --force-recreate --wait free6gc-computing-cmf || true
}

if ! FREE6GC_COMPUTING_CMF_CONFIG_FILE="${CMF_CONFIG}" \
  docker compose -f "${COMPOSE_FILE}" -f "${REMOTE_CORE_OVERRIDE}" config --quiet; then
  restore_previous_config
  echo "The generated CMF configuration is invalid" >&2
  exit 1
fi

echo "Recreating CMF with the new remote Sandbox address"
if ! FREE6GC_COMPUTING_CMF_CONFIG_FILE="${CMF_CONFIG}" \
  docker compose \
    -f "${COMPOSE_FILE}" \
    -f "${REMOTE_CORE_OVERRIDE}" \
    up -d --no-deps --no-build --force-recreate --wait free6gc-computing-cmf; then
  restore_previous_config
  echo "CMF failed to start with the new Sandbox address" >&2
  exit 1
fi

# The remote-core override excludes this service from normal startup. Remove a
# local Sandbox left behind by an earlier e2e_compute.sh run.
FREE6GC_COMPUTING_CMF_CONFIG_FILE="${CMF_CONFIG}" \
  docker compose \
    -f "${COMPOSE_FILE}" \
    -f "${REMOTE_CORE_OVERRIDE}" \
    rm -sf free6gc-computing-sandbox >/dev/null

echo "CMF now uses:"
echo "  management_url: ${MANAGEMENT_URL}"
echo "  service_endpoint: ${SERVICE_ENDPOINT}"
