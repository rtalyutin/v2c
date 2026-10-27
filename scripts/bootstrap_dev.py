"""Create a disposable local developer environment using committed locks."""

import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    if sys.version_info < (3, 12):
        raise SystemExit("Python 3.12 or newer is required")
    environment = ROOT / ".venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    subprocess.run(
        [str(python), "-m", "pip", "install", "-r", "requirements.lock"], cwd=ROOT, check=True
    )
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", "-e", "."], cwd=ROOT, check=True
    )
    print(f"Ready. Run: {python} scripts/check.py")


if __name__ == "__main__":
    main()
