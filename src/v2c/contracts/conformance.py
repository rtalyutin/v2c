"""Static checks connect the real composition root to consumer-owned ports.

Mypy checks these assignments without instantiating network clients or adding
runtime imports from contracts to adapters/configuration.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..adapters.runner import HTTPVMProvider, RunnerClient
    from ..config import Settings
    from .ports import PipelineSettings, RunnerPort, VMProviderPort

    def runner_port(client: RunnerClient) -> RunnerPort:
        return client

    def provider_port(client: HTTPVMProvider) -> VMProviderPort:
        return client

    def pipeline_settings(settings: Settings) -> PipelineSettings:
        return settings
