"""Static checks connect the real composition root to consumer-owned ports.

Mypy checks these assignments without instantiating network clients or adding
runtime imports from contracts to adapters/configuration.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from v2c.adapters.model import OpenAICompatibleModel
    from v2c.adapters.runner import HTTPVMProvider, RunnerClient
    from v2c.config import Settings
    from v2c.contracts.model import ModelPort
    from v2c.contracts.ports import PipelineSettings, RunnerPort, VMProviderPort
    from v2c.contracts.storage import ArtifactStore
    from v2c.storage import LocalArtifactStore, S3ArtifactStore

    def model_port(client: OpenAICompatibleModel) -> ModelPort:
        return client

    def local_store(client: LocalArtifactStore) -> ArtifactStore:
        return client

    def s3_store(client: S3ArtifactStore) -> ArtifactStore:
        return client

    def runner_port(client: RunnerClient) -> RunnerPort:
        return client

    def provider_port(client: HTTPVMProvider) -> VMProviderPort:
        return client

    def pipeline_settings(settings: Settings) -> PipelineSettings:
        return settings
