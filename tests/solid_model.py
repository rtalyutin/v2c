"""Independent model implemented solely from the documented neutral port."""

import json

from v2c.contracts.model import (
    ModelDescription,
    ModelPort,
    ModelReadiness,
    ModelRequest,
    ModelResult,
    PreparedModelRequest,
)


class ContractModel:
    """Independent implementation: only the four documented operations."""

    __slots__ = ("calls", "response")

    def __init__(self, response: dict[str, object]) -> None:
        self.calls: list[ModelRequest] = []
        self.response = response

    def describe(self) -> ModelDescription:
        return ModelDescription("contract-v1", {"version": "contract-v1"}, 262144, True)

    def readiness(self) -> ModelReadiness:
        return ModelReadiness(True)

    def prepare(self, request: ModelRequest) -> PreparedModelRequest:
        return PreparedModelRequest(request.model_dump_json().encode())

    async def generate(self, request: ModelRequest) -> ModelResult:
        self.calls.append(request)
        return ModelResult(
            text=json.dumps(self.response), structured=self.response, finish_reason="stop"
        )


def as_model(model: ContractModel) -> ModelPort:
    return model
