"""Explicit model contract for synthetic fixtures; no production fallback."""

from v2c.adapters.model import EnvSecretResolver, OpenAICompatibleModel
from v2c.contracts.model import ModelDescription, ModelReadiness, PreparedModelRequest


class FixtureModelContract:
    is_test = True
    profile = None

    def describe(self):
        if self.profile is not None:
            description = OpenAICompatibleModel(self.profile, EnvSecretResolver()).describe()
            return ModelDescription(
                description.version, description.manifest, description.max_input_bytes, self.is_test
            )
        return ModelDescription(
            "test",
            {
                "version": "test",
                "adapter_type": "test-double",
                "implementation": type(self).__module__ + "." + type(self).__qualname__,
            },
            262144,
            True,
        )

    def readiness(self):
        if not self.is_test and self.profile is not None:
            return OpenAICompatibleModel(self.profile, EnvSecretResolver()).readiness()
        return ModelReadiness(True)

    def prepare(self, request):
        if not self.is_test and self.profile is not None:
            return OpenAICompatibleModel(self.profile, EnvSecretResolver()).prepare(request)
        return PreparedModelRequest(request.context.encode())
