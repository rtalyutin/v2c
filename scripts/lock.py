"""Regenerate pinned Python 3.12 locks without implicit dependency upgrades."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the lock output has drifted")
    parser.add_argument(
        "--upgrade", action="store_true", help="explicitly resolve newer compatible versions"
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="v2c-lock-") as directory:
        outputs: dict[Path, str] = {}
        for filename, extras in (
            ("requirements-runtime.lock", []),
            ("requirements.lock", ["test", "dev"]),
        ):
            target = ROOT / filename
            output = Path(directory) / filename
            command = [
                sys.executable,
                "-m",
                "uv",
                "pip",
                "compile",
                "pyproject.toml",
                "--python-version",
                "3.12",
                "--no-annotate",
                "--custom-compile-command",
                "python scripts/lock.py",
                "--output-file",
                str(output),
                "--quiet",
            ]
            for extra in extras:
                command.extend(["--extra", extra])
            if not args.upgrade:
                command.extend(["--constraint", str(target)])
            subprocess.run(command, cwd=ROOT, check=True)
            outputs[target] = output.read_text()
        changed = [path.name for path, content in outputs.items() if path.read_text() != content]
        if args.check and changed:
            raise SystemExit("Dependency lock drift: " + ", ".join(changed))
        if not args.check:
            for path, content in outputs.items():
                path.write_text(content)
        print("Dependency locks: " + ("unchanged" if not changed else ", ".join(changed)))


if __name__ == "__main__":
    main()
