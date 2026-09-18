from __future__ import annotations

import gzip
import os
import subprocess
from pathlib import Path


SANDBOX_DIR = Path(__file__).resolve().parents[1]


def test_launcher_creates_network_and_replaces_existing_container(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "docker-calls.log"
    archive = tmp_path / "agent-compute-sandbox-mock-0.3.0-linux-arm64.tar.gz"
    with gzip.open(archive, "wb") as stream:
        stream.write(b"mock image archive")
    checksum = subprocess.check_output(["sha256sum", archive.name], cwd=tmp_path, text=True)
    Path(f"{archive}.sha256").write_text(checksum)

    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' "$*" >>"${FAKE_DOCKER_LOG}"
case "$*" in
  "compose version") exit 0 ;;
  "image inspect agent-compute-sandbox-mock:0.3.0-arm64") exit 0 ;;
  "image inspect --format {{.Os}}/{{.Architecture}} agent-compute-sandbox-mock:0.3.0-arm64")
    printf '%s\\n' 'linux/arm64'
    ;;
  "network inspect compose_n6") exit 1 ;;
  "network create --driver bridge --subnet 172.30.0.0/24 --gateway 172.30.0.1 compose_n6")
    printf '%s\\n' 'new-network'
    ;;
  "ps -aq --filter name=^/agent-sdk-mock-video-server$")
    printf '%s\\n' 'old-container'
    ;;
  "compose -f "*" ps -q mock-video-server")
    printf '%s\\n' 'new-container'
    ;;
  "inspect --format {{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}} new-container")
    printf '%s\\n' 'healthy'
    ;;
esac
"""
    )
    docker.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "FAKE_DOCKER_LOG": str(call_log),
            "SANDBOX_IMAGE_ARCHIVE": str(archive),
            "SANDBOX_START_TIMEOUT": "2",
        }
    )
    result = subprocess.run(
        [str(SANDBOX_DIR / "start_sandbox.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert "Sandbox management is healthy at http://172.30.0.10:28501." in result.stdout
    assert "Sandbox user plane is healthy at http://172.30.0.10:28502." in result.stdout
    calls = call_log.read_text()
    assert (
        "network create --driver bridge --subnet 172.30.0.0/24 "
        "--gateway 172.30.0.1 compose_n6"
    ) in calls
    assert "rm -f old-container" in calls
    assert "up -d --no-build --force-recreate mock-video-server" in calls
