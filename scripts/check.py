"""One check entry point; integration and browser requirements are explicit."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*arguments: str) -> None:
    print("+ " + " ".join(arguments), flush=True)
    subprocess.run(arguments, cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--integration", action="store_true", help="require PostgreSQL and Docker; no skipped tests"
    )
    parser.add_argument(
        "--web", action="store_true", help="also run formatting and real browser tests"
    )
    args = parser.parse_args()
    if args.integration:
        if not os.environ.get("V2C_TEST_POSTGRES_URL"):
            raise SystemExit("--integration requires a disposable migrated V2C_TEST_POSTGRES_URL")
        if not shutil.which("docker"):
            raise SystemExit("--integration requires Docker Compose")
        run("docker", "compose", "version")
    python = sys.executable
    run(python, "-m", "ruff", "check", ".")
    run(python, "-m", "ruff", "format", "--check", ".")
    with tempfile.TemporaryDirectory(prefix="v2c-types-") as cache:
        run(python, "-m", "mypy", "--cache-dir", cache)
    run(python, "scripts/check_architecture.py")
    run(python, "scripts/check_docs.py")
    run(python, "scripts/export_openapi.py", "--check")
    with tempfile.TemporaryDirectory(prefix="v2c-check-") as directory:
        report = Path(directory) / "pytest.xml"
        run(python, "-m", "pytest", "-q", "-rs", f"--junitxml={report}")
        if args.integration:
            suites = ET.parse(report).getroot().iter("testsuite")
            skipped = sum(int(suite.attrib.get("skipped", 0)) for suite in suites)
            if skipped:
                raise SystemExit(f"Integration gate failed: {skipped} skipped tests")
        # Validate the migration path even in the fast SQLite profile. This is
        # separate from tests that create disposable schemas through metadata.
        environment = dict(
            os.environ, V2C_DATABASE_URL=f"sqlite:///{Path(directory) / 'migration.db'}"
        )
        subprocess.run(
            [python, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=environment, check=True
        )
        subprocess.run([python, "-m", "alembic", "check"], cwd=ROOT, env=environment, check=True)
    if args.integration:
        run(python, "-m", "alembic", "check")
    if args.web:
        run("npm", "run", "check:web")
        run("npm", "run", "test:ui")


if __name__ == "__main__":
    main()
