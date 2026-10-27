"""Independent bad/good controls show the repository gates actually reject drift."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def architecture(root):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_architecture.py"), "--root", str(root)],
        text=True,
        capture_output=True,
    )


def test_architecture_gate_rejects_forbidden_dependency_and_cycle(tmp_path):
    (tmp_path / "workflow").mkdir()
    domain = tmp_path / "workflow" / "rules.py"
    domain.write_text("from v2c.adapters.cloud import request\n")
    result = architecture(tmp_path)
    assert result.returncode != 0 and "Forbidden dependency" in result.stderr
    domain.write_text(
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from v2c.adapters.cloud import Request\n"
    )
    assert architecture(tmp_path).returncode == 0
    (tmp_path / "workflow" / "__init__.py").write_text("from v2c.adapters.cloud import request\n")
    result = architecture(tmp_path)
    assert result.returncode != 0 and "Forbidden dependency" in result.stderr
    (tmp_path / "workflow" / "__init__.py").write_text("")
    (tmp_path / "a.py").write_text("from .b import value\n")
    (tmp_path / "b.py").write_text("from .a import value\n")
    result = architecture(tmp_path)
    assert result.returncode != 0 and "Import cycle" in result.stderr


def test_ruff_and_type_gate_reject_invalid_control_files(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("value = undefined_name\n")
    lint = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--isolated", str(bad)], capture_output=True
    )
    assert lint.returncode != 0 and b"F821" in lint.stdout
    bad.write_text("def wrong() -> int:\n    return 'not an integer'\n")
    typed = subprocess.run(
        [sys.executable, "-m", "mypy", "--no-incremental", "--strict", str(bad)],
        cwd=tmp_path,
        capture_output=True,
    )
    assert typed.returncode != 0 and b"return-value" in typed.stdout


def test_migration_gate_rejects_unmigrated_model_column(tmp_path):
    environment = dict(os.environ, V2C_DATABASE_URL=f"sqlite:///{tmp_path / 'migration.db'}")

    def run(*arguments):
        return subprocess.run(
            [sys.executable, *arguments],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
        )

    upgraded = run("-m", "alembic", "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    clean = run("-m", "alembic", "check")
    assert clean.returncode == 0, clean.stdout + clean.stderr

    # Add only an in-memory mapping in a fresh interpreter: neither the checked-in
    # models nor another test's metadata may be modified by this negative control.
    drift = run(
        "-c",
        "from alembic.config import CommandLine\n"
        "from sqlalchemy import String\n"
        "from sqlalchemy.orm import mapped_column\n"
        "from v2c.models import Run\n"
        "Run.maintenance_drift_probe = mapped_column(String(60), nullable=True)\n"
        "CommandLine().main(['-c', 'alembic.ini', 'check'])\n",
    )
    output = drift.stdout + drift.stderr
    assert drift.returncode != 0, output
    assert "New upgrade operations detected" in output
    assert "add_column" in output and "maintenance_drift_probe" in output
