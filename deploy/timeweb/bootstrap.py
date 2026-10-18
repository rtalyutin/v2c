#!/usr/bin/env python3
"""Runs as root only inside a newly provisioned operator runner VM."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import sys


def run(argv: list[str]) -> None:
    subprocess.run(argv, check=True, stdin=subprocess.DEVNULL)


def main() -> None:
    if sys.version_info < (3, 12):
        raise ValueError("runner image must provide Python 3.12+")
    config = json.loads(Path("/etc/v2c-runner/bootstrap.json").read_text())
    root = Path("/opt/v2c")
    root.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="v2c-source-") as temporary:
        archive = Path(temporary) / "source.tar.gz"
        run(["curl", "--fail", "--silent", "--show-error", "--location", "--proto", "=https", "--tlsv1.2",
             "--connect-timeout", "10", "--max-time", "180", config["source_url"], "--output", str(archive)])
        if hashlib.sha256(archive.read_bytes()).hexdigest() != config["source_sha256"]:
            raise ValueError("source checksum differs")
        extracted = Path(temporary) / "source"
        extracted.mkdir()
        with tarfile.open(archive) as source:
            source.extractall(extracted, filter="data")
        candidates = [p.parent for p in extracted.rglob("pyproject.toml") if p.parent.joinpath("requirements.lock").is_file()]
        if len(candidates) != 1:
            raise ValueError("source must contain one v2c package")
        # Source is operator-pinned and checksum-verified before any build runs.
        run(["python3", "-m", "venv", str(root / "venv")])
        python = str(root / "venv/bin/python")
        run([python, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(candidates[0] / "requirements.lock")])
        run([python, "-m", "pip", "install", "--no-deps", str(candidates[0])])
    python = str(root / "venv/bin/python")
    # Confirms that the installed source and operator settings agree; no caller
    # supplied profile hash is accepted as the runner's own identity.
    check = "from pathlib import Path; import json; from v2c.runner_service import RunnerSettings; s=RunnerSettings.model_validate_json(Path('/etc/v2c-runner/runner.json').read_text()); e=json.loads(Path('/etc/v2c-runner/bootstrap.json').read_text())['expected_profile_sha256']; assert s.profile_sha256()==e"
    run([python, "-c", check])
    disk_root = Path("/var/lib/v2c-runner-disk")
    disk_root.mkdir(mode=0o700, exist_ok=True)
    disk = disk_root / "workspace.img"
    workspace = Path("/var/lib/v2c-runner")
    workspace.mkdir(mode=0o700, exist_ok=True)
    if disk.exists():
        raise ValueError("fresh VM template refuses to reuse an existing workspace disk")
    with disk.open("xb") as output:
        output.truncate(config["workspace_disk_bytes"])
    disk.chmod(0o600)
    run(["mkfs.ext4", "-q", "-F", str(disk)])
    entry = f"{disk} {workspace} ext4 loop,nosuid,nodev 0 0\n"
    with Path("/etc/fstab").open("a") as output:
        output.write(entry)
    run(["mount", str(workspace)])
    fs = os.statvfs(workspace)
    if fs.f_blocks * fs.f_frsize > config["workspace_disk_bytes"]:
        raise ValueError("workspace filesystem exceeds profile")
    run(["systemctl", "enable", "--now", "docker.service"])
    for image in config["images"]:
        run(["docker", "pull", image])
    # These public cloud firewall exceptions only activate default-deny filtering;
    # prohibit the two documentation destinations locally as an extra boundary.
    run(["ip", "route", "replace", "unreachable", "192.0.2.1/32"])
    run(["ip", "-6", "route", "replace", "unreachable", "2001:db8::1/128"])
    # Downloads finish before either application code or project commands can
    # execute. New outbound traffic is then blocked by the host firewall.
    run(["systemctl", "daemon-reload"])
    Path("/etc/v2c-runner/bootstrap-ready").touch(mode=0o600)
    run(["systemctl", "enable", "--now", "v2c-runner-firewall.service"])
    run(["systemctl", "enable", "--now", "v2c-runner.service"])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("V2C_RUNNER_BOOTSTRAP_FAILED", file=sys.stderr)
        raise SystemExit(1)
