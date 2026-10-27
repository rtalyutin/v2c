"""Compatibility entrypoint for the isolated VM runner.

Implementations live in ``v2c.runner``. Keep this facade while deployed systemd
units and integrations use ``python -m v2c.runner_service``.
"""

from .runner.api import create_app, main
from .runner.archive import safe_extract
from .runner.docker import DockerBackend, ExecutionBackend
from .runner.journal import TERMINAL, Journal
from .runner.service import RunnerService
from .runner.settings import RunnerSettings

__all__ = [
    "create_app",
    "main",
    "safe_extract",
    "DockerBackend",
    "ExecutionBackend",
    "Journal",
    "TERMINAL",
    "RunnerService",
    "RunnerSettings",
]

if __name__ == "__main__":
    main()
