"""Model proposal context, durable dispatch, usage and response validation."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, cast

from pydantic import ValidationError

from ..adapters.model import AdapterError, ErrorCategory, ModelPort, ModelRequest
from ..contracts.workflow import JsonObject, RunClaim
from ..diagnostics import annotate_exception
from ..engine import DomainError, Engine, digest
from .archive import _SECRET
from .common import InputRequired, PipelineLimits, StepFailure, json_bytes, stable_id
from .journal import OperationJournal
from .schemas import DTO


class ModelOperations:
    def __init__(
        self,
        engine: Engine,
        journal: OperationJournal,
        model: ModelPort | None,
        limits: PipelineLimits,
    ):
        self.engine, self.journal, self.model, self.limits = engine, journal, model, limits

    def ensure_ready(self) -> None:
        """Known absent credentials cannot justify a VM allocation or API call."""
        if self.model is None:
            raise InputRequired("MODEL_PROFILE_REQUIRED")
        if getattr(self.model, "is_test", False):
            return
        profile = getattr(self.model, "profile", None)
        reference = getattr(profile, "secret_ref", "")
        if reference.startswith("env:"):
            name = reference[4:]
            if not name or not name.replace("_", "").isalnum():
                raise InputRequired("MODEL_SECRET_REFERENCE_INVALID")
            if not os.environ.get(name):
                raise InputRequired("MODEL_CREDENTIAL_REQUIRED", {"key_env": name})

    def fit_context(
        self, request_id: str, task: str, context: JsonObject, schema: JsonObject
    ) -> tuple[JsonObject, JsonObject]:
        """Reserve real task/schema overhead; select whole optional source files.

        Work/task/recipe semantics are retained. Partial source and inventory
        coverage is explicit and fingerprinted; there is no invisible text cut.
        """
        serializer = getattr(self.model, "request_body", None)
        limit = getattr(
            getattr(self.model, "profile", None),
            "max_input_bytes",
            self.limits.model_max_input_bytes,
        )

        def size(value: JsonObject) -> int:
            request = ModelRequest(
                request_id=request_id,
                task=task,
                context=json_bytes(value).decode(),
                response_schema=schema,
                required_capabilities=frozenset({"text", "structured"}),
            )
            return len(serializer(request)) if serializer else len(request.context.encode())

        original_digest = digest(context)
        if size(context) <= limit:
            return context, {
                "partial": False,
                "original_context_sha256": original_digest,
                "request_bytes": size(context),
            }
        if size({}) > limit:
            raise InputRequired(
                "MODEL_BASE_CONTEXT_LIMIT", {"required_bytes": size({}), "max_input_bytes": limit}
            )
        value = json.loads(json_bytes(context))
        groups: list[JsonObject] = []
        candidates: list[tuple[int, str, JsonObject, Any]] = []
        required_paths = set((context.get("work") or {}).get("files", []))

        def update(group: JsonObject) -> None:
            selected = {item["path"] for item in group["parent"]["files"]}
            omitted = [path for path in group["paths"] if path not in selected]
            group["parent"]["omitted_files"] = omitted[:5]
            group["parent"]["coverage"] = {
                "selected_files": len(selected),
                "omitted_file_count": len(omitted),
                "omitted_bytes_known": sum(group["sizes"].get(path, 0) for path in omitted),
                "omitted_paths_sha256": digest(omitted),
                "omitted_files_sample_only": len(omitted) > 5,
                "preselection_omitted_bytes_unknown": bool(group["unknown_sizes"]),
            }

        def compact(node: Any, path: tuple[str, ...] = ()) -> None:
            if isinstance(node, list):
                for item in node:
                    compact(item, path)
                return
            if not isinstance(node, dict):
                return
            node.pop("workspace_root", None)
            checkpoint = node.get("checkpoint")
            if isinstance(checkpoint, dict):
                node["checkpoint"] = {
                    k: checkpoint[k]
                    for k in ("key", "sha256", "size", "artifact_id")
                    if k in checkpoint
                }
            if isinstance(node.get("output"), str) and len(node["output"].encode()) > 256:
                body = node["output"]
                node["output"] = body.encode()[-256:].decode("utf-8", errors="replace")
                node["output_truncated"] = True
                node["full_output_sha256"] = hashlib.sha256(body.encode()).hexdigest()
            files = node.get("files")
            if isinstance(files, list) and all(
                isinstance(item, dict) and "path" in item for item in files
            ):
                source = any("text" in item for item in files) or (path and path[-1] == "source")
                omitted = node.get("omitted_files", [])
                omitted = omitted if isinstance(omitted, list) else []
                group: JsonObject = {
                    "parent": node,
                    "source": source,
                    "paths": [item["path"] for item in files] + omitted,
                    "sizes": {
                        item["path"]: len(item["text"].encode())
                        if "text" in item
                        else item.get("size", 0)
                        for item in files
                    },
                    "unknown_sizes": omitted,
                    "records": files,
                }
                node["files"] = []
                groups.append(group)
                update(group)
                for item in files:
                    priority = (
                        0 if source and item["path"] in required_paths else 2 if source else 4
                    )
                    candidates.append((priority, "file", group, item))
            instructions = node.get("instructions")
            if isinstance(instructions, dict) and all(
                isinstance(text, str) for text in instructions.values()
            ):
                node["instructions"] = {}
                node["instruction_coverage"] = {
                    "selected": 0,
                    "omitted": len(instructions),
                    "all_instructions_sha256": digest(instructions),
                }
                for name, text in instructions.items():
                    priority = 1 if "/change/" in name or "/verify/" in name else 3
                    candidates.append((priority, "instruction", node, (name, text)))
            for key, child in list(node.items()):
                if key not in {"files", "coverage", "instructions", "instruction_coverage"}:
                    compact(child, (*path, key))

        compact(value)
        if size(value) > limit:
            raise InputRequired(
                "MODEL_REQUIRED_CONTEXT_LIMIT",
                {"required_bytes": size(value), "max_input_bytes": limit},
            )
        # Source intake is bounded; this bound also prevents thousands of large
        # metadata inventories causing quadratic reserialization during packing.
        for _, kind, target, item in sorted(candidates, key=lambda candidate: candidate[0])[:128]:
            if kind == "file":
                target["parent"]["files"].append(item)
                update(target)
                if size(value) > limit:
                    target["parent"]["files"].pop()
                    update(target)
            else:
                name, text = item
                target["instructions"][name] = text
                target["instruction_coverage"]["selected"] += 1
                target["instruction_coverage"]["omitted"] -= 1
                if size(value) > limit:
                    del target["instructions"][name]
                    target["instruction_coverage"]["selected"] -= 1
                    target["instruction_coverage"]["omitted"] += 1
        for group in groups:
            if not group["source"]:
                continue
            selected = {item["path"] for item in group["parent"]["files"]}
            missing_required = required_paths.intersection(group["paths"]) - selected
            if missing_required:
                raise InputRequired(
                    "MODEL_REQUIRED_SOURCE_LIMIT",
                    {"files": sorted(missing_required), "max_input_bytes": limit},
                )
            if group["paths"] and not selected:
                raise InputRequired(
                    "MODEL_SOURCE_CONTEXT_LIMIT",
                    {"max_input_bytes": limit, "omitted_file_count": len(group["paths"])},
                )
        return value, {
            "partial": True,
            "original_context_sha256": original_digest,
            "sent_context_sha256": digest(value),
            "request_bytes": size(value),
            "max_input_bytes": limit,
        }

    async def generate(
        self, claim: RunClaim, key: str, task: str, context: JsonObject, schema: type[DTO]
    ) -> JsonObject:
        try:
            return await self._generate(claim, key, task, context, schema)
        except Exception as error:
            annotate_exception(error, command_id=stable_id(claim["attempt"]["id"], "model_" + key))
            raise

    async def _generate(
        self, claim: RunClaim, key: str, task: str, context: JsonObject, schema: type[DTO]
    ) -> JsonObject:
        self.ensure_ready()
        assert self.model is not None
        request_id = stable_id(claim["attempt"]["id"], "model_" + key)
        if _SECRET.search(json_bytes(context).decode()):
            raise InputRequired("MODEL_CONTEXT_SECRET_REVIEW_REQUIRED")
        try:
            context, context_selection = self.fit_context(
                request_id, task, context, schema.model_json_schema()
            )
        except AdapterError as error:
            # Pure serialization/capability rejection happens before a provider
            # dispatch and must not leave an unresumable ACTIVE attempt.
            raise InputRequired(error.category.value) from error
        context_json = json_bytes(context).decode()
        profile = getattr(self.model, "profile", None)
        profile_version = getattr(
            profile, "version", "test" if getattr(self.model, "is_test", False) else "unknown"
        )
        if profile is not None and hasattr(profile, "model_dump"):
            profile_manifest = profile.model_dump(mode="json")
            if "capabilities" in profile_manifest:
                profile_manifest["capabilities"] = sorted(profile_manifest["capabilities"])
        elif getattr(self.model, "is_test", False):
            profile_manifest = {
                "version": "test",
                "adapter_type": "test-double",
                "implementation": type(self.model).__module__ + "." + type(self.model).__qualname__,
            }
        else:
            raise InputRequired("MODEL_PROFILE_REQUIRED")
        try:
            profile_manifest = self.engine.bind_model_profile(
                claim["run"]["id"], profile_manifest, **self.journal.fence(claim)
            )
        except DomainError as error:
            if error.code not in {"MODEL_PROFILE_CHANGED", "INVALID_MODEL_PROFILE"}:
                raise
            raise InputRequired(error.code) from error
        op_id, row, existed = self.journal.prepare(
            claim,
            "model_" + key,
            {
                "operation": "model",
                "request_id": request_id,
                "task_hash": hashlib.sha256(task.encode()).hexdigest(),
                "context_hash": digest(context),
                "schema_hash": digest(schema.model_json_schema()),
                "profile_version": profile_version,
                "profile_manifest": profile_manifest,
                "profile_hash": digest(profile_manifest),
                "context_selection": context_selection,
            },
        )
        if row["status"] == "SUCCEEDED":
            return schema.model_validate(cast(JsonObject, row["result"])["structured"]).model_dump()
        if existed:
            self.journal.observe(
                claim, op_id, "STOPPED", {"provider_outcome": "UNKNOWN", "usage_unknown": True}
            )
            self.engine.record_usage(
                claim["run"]["id"],
                request_id,
                profile_version,
                {"usage_unknown": True, "delivery_unknown": True},
                claim["attempt"]["id"],
            )
            raise InputRequired("MODEL_OUTCOME_UNKNOWN", {"request_id": request_id})
        try:
            value = await self.model.generate(
                ModelRequest(
                    request_id=request_id,
                    task=task,
                    context=context_json,
                    response_schema=schema.model_json_schema(),
                    required_capabilities=frozenset({"text", "structured"}),
                )
            )
            # Cost is an observed fact even when response validation, cancellation
            # or lease fencing later rejects publication of its proposal.
            usage: JsonObject = (
                value.usage.model_dump()
                if hasattr(value.usage, "model_dump")
                else cast(JsonObject, value.usage)
            )
            self.engine.record_usage(
                claim["run"]["id"],
                request_id,
                profile_version,
                {
                    "usage": usage,
                    "provider_request_id": value.provider_request_id,
                    "profile_version": profile_version,
                    "delivery_unknown": False,
                    "usage_unknown": all(
                        usage.get(k) is None
                        for k in ("input_tokens", "output_tokens", "total_tokens")
                    ),
                    "test_adapter": bool(getattr(self.model, "is_test", False)),
                },
                claim["attempt"]["id"],
            )
            self.journal.guard(claim)
            structured = schema.model_validate(value.structured).model_dump()
            self.journal.observe(
                claim, op_id, "SUCCEEDED", {"structured": structured, "usage": usage}
            )
            return structured
        except AdapterError as error:
            self.engine.record_usage(
                claim["run"]["id"],
                request_id,
                profile_version,
                {
                    "usage_unknown": error.usage_unknown,
                    "delivery_unknown": error.delivery_unknown,
                    "provider_request_id": error.provider_request_id,
                    "category": error.category.value,
                },
                claim["attempt"]["id"],
            )
            self.journal.observe(
                claim,
                op_id,
                "FAILED",
                {"category": error.category.value, "delivery_unknown": error.delivery_unknown},
            )
            if (
                error.category
                in {
                    ErrorCategory.AUTH,
                    ErrorCategory.INVALID_CONFIG,
                    ErrorCategory.UNSUPPORTED_CAPABILITY,
                    ErrorCategory.CANCELLED,
                    ErrorCategory.RATE_LIMIT,
                }
                or error.delivery_unknown
            ):
                raise InputRequired(
                    error.category.value, {"usage_unknown": error.usage_unknown}
                ) from error
            raise StepFailure(
                error.category.value,
                retryable=error.retryable or error.category == ErrorCategory.INVALID_RESPONSE,
            ) from error
        except ValidationError as error:
            self.journal.observe(claim, op_id, "FAILED", {"category": "INVALID_RESPONSE"})
            raise StepFailure("INVALID_RESPONSE") from error
