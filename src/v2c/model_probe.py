"""One synthetic request through the actual configured model adapter.

No repositories, account creation, retry loop or paid fallback are involved.
An absent key is detected before making a network request. A successful probe
qualifies this small text/JSON exchange, not the model's refactoring quality.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from typing import Any

from .adapters.model import AdapterError, ModelRequest
from .config import Settings, build_model


PROBE_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {'probe': {'type': 'string', 'const': 'v2c'},
                   'answer': {'type': 'integer', 'const': 2}},
    'required': ['probe', 'answer'],
}


async def probe(settings: Settings, *, model: Any = None) -> dict:
    injected = model is not None
    report: dict[str, Any] = {'status': 'NEEDS_PROFILE', 'network_request_performed': False,
                              'validated_capabilities': [], 'schema_valid': False,
                              'dependency_injected': injected, 'provider_exchange_verified': False}
    try:
        model = model or build_model(settings)
    except (TypeError, ValueError):
        return {**report, 'status': 'INVALID_CONFIG', 'reason_code': 'MODEL_PROFILE_INVALID'}
    if model is None:
        return {**report, 'reason_code': 'MODEL_PROFILE_REQUIRED'}
    profile = model.profile
    test_adapter = bool(getattr(model, 'is_test', False))
    report['test_adapter'] = test_adapter
    report['profile'] = {key: getattr(profile, key) for key in
                         ('version', 'adapter_type', 'endpoint', 'model', 'max_input_bytes',
                          'max_output_tokens', 'timeout_seconds')}
    report['profile']['declared_capabilities'] = sorted(profile.capabilities)
    env_name = settings.model_api_key_env
    if not env_name or not env_name.replace('_', '').isalnum():
        return {**report, 'status': 'INVALID_CONFIG', 'reason_code': 'MODEL_SECRET_REFERENCE_INVALID'}
    if not os.environ.get(env_name):
        return {**report, 'status': 'NEEDS_KEY', 'reason_code': 'MODEL_CREDENTIAL_REQUIRED', 'key_env': env_name}
    request = ModelRequest(request_id='v2c_probe_' + uuid.uuid4().hex,
        task='Synthetic compatibility probe. Return exactly {"probe":"v2c","answer":2}. Compute 1 + 1.',
        context='Synthetic constants only. No project data.', response_schema=PROBE_SCHEMA,
        required_capabilities=frozenset({'text', 'structured'}))
    try:
        # The adapter makes exactly one request and owns its bounded HTTP client.
        # Never print exception payloads, headers, credentials or model text.
        report['adapter_invoked'] = True
        report['network_request_performed'] = None
        result = await model.generate(request)
    except AdapterError as error:
        return {**report, 'status': 'REJECTED', 'reason_code': error.category.value,
                'network_request_performed': (False if test_adapter or error.category.value == 'UNSUPPORTED_CAPABILITY' else
                                              True if error.category.value in {'AUTH', 'RATE_LIMIT', 'PROVIDER_FAILURE'} else None),
                'delivery_unknown': error.delivery_unknown, 'usage_unknown': error.usage_unknown,
                'validated_capabilities': []}
    valid = result.structured == {'probe': 'v2c', 'answer': 2}
    return {**report, 'status': ('LOCAL_VERIFIED' if test_adapter else 'VERIFIED') if valid else 'REJECTED',
            'network_request_performed': not test_adapter,
            'provider_exchange_verified': valid and not test_adapter and not injected,
            'reason_code': None if valid else 'INVALID_RESPONSE', 'schema_valid': valid,
            'validated_capabilities': ['structured', 'text'] if valid else [],
            'usage': result.usage.model_dump(),
            'scope': 'single_synthetic_text_and_schema_exchange',
            'billing_plan_verified': False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Probe the explicitly configured model with one synthetic JSON request.')
    parser.parse_args(argv)
    try:
        result = asyncio.run(probe(Settings.from_env()))
    except (TypeError, ValueError):
        result = {'status': 'INVALID_CONFIG', 'reason_code': 'ENVIRONMENT_CONFIG_INVALID',
                  'network_request_performed': False, 'validated_capabilities': [], 'schema_valid': False}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result['status'] == 'VERIFIED' else 2 if result['status'] in {'NEEDS_KEY', 'NEEDS_PROFILE'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
