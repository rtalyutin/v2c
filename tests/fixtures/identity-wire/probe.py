"""Compare stable identities and frozen schema serialization across revisions."""

import json

from v2c.adapters.runner import CommandRequest, PrepareRequest, VMAllocation
from v2c.engine import digest

request = CommandRequest(
    command_id="cmd-qa",
    workspace_id="work-qa",
    attempt_id="attempt-qa",
    generation=3,
    argv=["python3", "-c", "print(42)"],
    timeout_seconds=20,
)
data = {
    "digest_null": digest({"optional": None, "nested": {"юникод": [1, True, None]}}),
    "digest_absent": digest({"nested": {"юникод": [1, True, None]}}),
    "command_json": request.model_dump_json(),
    "command_hash": request.content_hash(),
    "schemas": {
        model.__name__: model.model_json_schema()
        for model in [CommandRequest, PrepareRequest, VMAllocation]
    },
}
print(json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False))
