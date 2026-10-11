#!/usr/bin/env python3
"""Archive clean, committed demo sources and pinned deployment inputs.

The bundle contains source archives, not patches or a copied site-packages tree.
Run after committing each repository; relevant uncommitted changes are rejected
so the recorded commit IDs identify the actual payload.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import subprocess
import tarfile
import urllib.request
from pathlib import Path


HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[2]
REPOSITORIES = {
    "sdk": WORKSPACE / "sdk",
    "dog": WORKSPACE / "compute_backend/dog",
    "dog-ctrl": WORKSPACE / "compute_backend/dog-ctrl",
    "nav": WORKSPACE / "compute_backend/nav",
}
UNITREE_COMMIT = "794fb2b3fd9165fd245a7b568698d9e97d8ac0a0"
UNITREE_ARCHIVE_SHA256 = "7f1a663d22011cc374ea7529f87e413c41a27f9fad0016f03b72e34ee095e765"
UNITREE_ARCHIVE = (
    f"https://codeload.github.com/unitreerobotics/unitree_sdk2_python/tar.gz/{UNITREE_COMMIT}"
)


def run(*args: str, cwd: Path) -> bytes:
    return subprocess.check_output(args, cwd=cwd)


def add_bytes(archive: tarfile.TarFile, name: str, data: bytes, *, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = 0
    archive.addfile(info, io.BytesIO(data))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build(output: Path, *, wheel: Path, rayneo_apk: Path | None, phone_apk: Path | None,
          unitree_archive: Path | None) -> None:
    source_archives = {}
    commits = {}
    for name, repo in REPOSITORIES.items():
        dirty = run("git", "status", "--porcelain", "--untracked-files=all", cwd=repo).decode().splitlines()
        # The SDK checkout also contains unrelated sandbox packaging edits;
        # they are intentionally outside this release and untouched here.
        relevant = [line for line in dirty if name != "sdk" or not line[3:].startswith("mock-video-server/")]
        if relevant:
            raise SystemExit(f"Uncommitted release changes in {repo}: {relevant[:8]}")
        commits[name] = run("git", "rev-parse", "HEAD", cwd=repo).decode().strip()
        source_archives[name] = run(
            "git", "archive", "--format=tar", f"--prefix=sources/{name}/", "HEAD", cwd=repo
        )

    if not wheel.is_file():
        raise SystemExit(f"Python SDK wheel missing: {wheel}")
    binary_inputs = {"artifacts/" + wheel.name: wheel.read_bytes()}
    for name, candidate in (("rayneo.apk", rayneo_apk), ("phone-controller.apk", phone_apk)):
        if candidate is not None:
            if not candidate.is_file():
                raise SystemExit(f"APK missing: {candidate}")
            binary_inputs["artifacts/" + name] = candidate.read_bytes()

    if unitree_archive is None:
        with urllib.request.urlopen(UNITREE_ARCHIVE, timeout=40) as response:
            unitree_source = response.read()
    else:
        unitree_source = unitree_archive.read_bytes()
    if sha256(unitree_source) != UNITREE_ARCHIVE_SHA256:
        raise SystemExit("Unitree source archive checksum changed; refusing to package")
    binary_inputs["vendor/unitree_sdk2_python.tar.gz"] = unitree_source

    manifest = {
        "format": 1,
        "source_commits": commits,
        "source_archive_sha256": {name: sha256(data) for name, data in source_archives.items()},
        "artifacts_sha256": {name: sha256(data) for name, data in binary_inputs.items()},
        "unitree_sdk_source_commit": UNITREE_COMMIT,
        "platform": "Jetson aarch64 / CPython 3.12.13; ROS Foxy uses system Python 3.8",
        "cyclonedds_c_library": "0.10.5 (external prerequisite)",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as bundle:
                for name, data in source_archives.items():
                    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as source:
                        for member in source:
                            body = source.extractfile(member) if member.isfile() else None
                            bundle.addfile(member, body)
                for name, data in sorted(binary_inputs.items()):
                    add_bytes(bundle, name, data)
                add_bytes(bundle, "release-manifest.json", json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n")
    print(output)
    print("sha256", sha256(output.read_bytes()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--rayneo-apk", type=Path)
    parser.add_argument("--phone-apk", type=Path)
    parser.add_argument("--unitree-archive", type=Path,
                        help="Cached archive of the pinned Unitree commit; SHA256 is checked")
    args = parser.parse_args()
    build(args.output, wheel=args.wheel, rayneo_apk=args.rayneo_apk,
          phone_apk=args.phone_apk, unitree_archive=args.unitree_archive)


if __name__ == "__main__":
    main()
