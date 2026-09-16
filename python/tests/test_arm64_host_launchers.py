from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ARM64_DIR = Path(__file__).resolve().parents[1] / "docker" / "arm64"


@pytest.mark.parametrize(
    ("service", "ready_event"),
    (("agent-a", "GROUP_CREATED"), ("agent-b", "B_READY")),
)
@pytest.mark.parametrize("mode", ("fresh-register", "force-register"))
def test_host_launcher_recreates_service_and_preserves_registration_semantics(
    tmp_path: Path,
    service: str,
    ready_event: str,
    mode: str,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "docker-calls.log"
    env_file = tmp_path / ".env"
    env_file.write_text("AGENT_IMAGE=agent-connect-sdk:0.17.9-arm64\n")
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
set -eu
printf '%s|%s\\n' "${AGENT_REGISTRATION_MODE:-unset}" "$*" >>"${FAKE_DOCKER_LOG}"
case "$*" in
  "compose version") exit 0 ;;
  *" config --images") printf '%s\\n' 'agent-connect-sdk:0.17.9-arm64' ;;
  "image inspect --format {{.Os}}/{{.Architecture}} agent-connect-sdk:0.17.9-arm64")
    printf '%s\\n' 'linux/arm64'
    ;;
  "image inspect agent-connect-sdk:0.17.9-arm64") exit 0 ;;
  *" ps -aq "*) printf '%s\\n' 'old-container' ;;
  *" ps -q "*) printf '%s\\n' 'new-container' ;;
  "logs new-container") printf '{\"event\": \"%s\"}\\n' "${FAKE_READY_EVENT}" ;;
  "inspect --format {{.State.Running}} new-container") printf '%s\\n' 'true' ;;
esac
"""
    )
    docker.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "AGENT_ENV_FILE": str(env_file),
            "AGENT_COMPOSE_FILE": str(ARM64_DIR / "docker-compose.same-host.yml"),
            "AGENT_START_TIMEOUT": "2",
            "FAKE_DOCKER_LOG": str(call_log),
            "FAKE_READY_EVENT": ready_event,
        }
    )
    result = subprocess.run(
        [str(ARM64_DIR / f"start-{service}.sh"), mode],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert f"{service} is ready ({ready_event})" in result.stdout
    calls = call_log.read_text()
    # `ps -aq` above represents an existing service container. Compose applies
    # --force-recreate to it whether Docker reports it as running or stopped.
    assert (
        f"{mode}|compose --env-file {env_file} "
        f"-f {ARM64_DIR / 'docker-compose.same-host.yml'} "
        f"up -d --no-deps --force-recreate {service}"
    ) in calls
    if mode == "force-register":
        assert f" kill {service}" in calls
        assert f" rm -f {service}" in calls
    else:
        assert f" kill {service}" not in calls
        assert f" rm -f {service}" not in calls


def test_compose_uses_internal_run_commands() -> None:
    compose = (ARM64_DIR / "docker-compose.same-host.yml").read_text()
    assert 'command: ["run-agent-a.sh",' in compose
    assert 'command: ["run-agent-b.sh",' in compose
    assert 'command: ["start-agent-' not in compose
