import asyncio
import json

import httpx
import pytest

from v2c.adapters.model import AdapterError, EnvSecretResolver, ErrorCategory, ModelProfile, ModelRequest, OpenAICompatibleModel
from v2c.adapters.runner import HTTPVMProvider, MTLSConfig, RunnerClient, RunnerError


def profile(**updates):
    return ModelProfile(version='test-v1', adapter_type='openai-compatible', endpoint='https://example.test/v1',
        model='configured-model', secret_ref='env:TEST_MODEL_KEY', capabilities={'text','structured'},
        max_input_bytes=10000, max_output_tokens=100, max_response_bytes=10000, timeout_seconds=2, **updates)


def request(**updates):
    return ModelRequest(request_id='req1', task='describe', context='untrusted project source', **updates)


def execute(handler, req=None, configuration=None):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await OpenAICompatibleModel(configuration or profile(), lambda _: 'private-test-value', client).generate(req or request())
    return asyncio.run(run())


def test_structured_result_and_unknown_usage_are_not_zero():
    observed = []
    def provider(req):
        observed.append(req)
        assert req.url.path == '/v1/chat/completions'
        wire = json.loads(req.content)
        assert wire['stream'] is False and wire['max_tokens'] == 100
        assert wire['response_format']['type'] == 'json_schema'
        return httpx.Response(200, json={'choices':[{'message':{'content':'{"ok": true}'},'finish_reason':'stop'}]}, headers={'x-request-id':'provider1'})
    result = execute(provider, request(response_schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}))
    assert len(observed) == 1
    assert result.structured == {'ok':True}
    assert result.usage.total_tokens is None and result.usage.cost is None
    assert result.provider_request_id == 'provider1'
    assert 'private-test-value' not in result.model_dump_json()


@pytest.mark.parametrize('status,category,unknown', [(401,'AUTH',False),(429,'RATE_LIMIT',False),(500,'PROVIDER_FAILURE',True),(302,'INVALID_CONFIG',False)])
def test_status_is_normalized_without_hidden_retry(status,category,unknown):
    calls = []
    def provider(req):
        calls.append(req)
        return httpx.Response(status, headers={'Location':'https://other.test/'}, text='private provider content')
    with pytest.raises(AdapterError) as failure: execute(provider)
    assert failure.value.category == category
    assert failure.value.delivery_unknown is unknown
    assert 'private provider content' not in str(failure.value)
    assert len(calls) == 1


def test_timeout_delivery_is_unknown_and_no_hidden_retry():
    calls = []
    def provider(req):
        calls.append(req)
        raise httpx.ReadTimeout('sensitive url example', request=req)
    with pytest.raises(AdapterError) as failure: execute(provider)
    assert failure.value.category == 'TRANSPORT_OR_TIMEOUT'
    assert failure.value.delivery_unknown is True
    assert len(calls) == 1
    assert 'sensitive' not in str(failure.value)


def test_validation_and_caps_fail_closed():
    def provider(req):
        return httpx.Response(200, json={'choices':[{'message':{'content':'{"ok":"yes"}'},'finish_reason':'stop'}]})
    with pytest.raises(AdapterError) as failure:
        execute(provider, request(response_schema={'type':'object','properties':{'ok':{'type':'boolean'}}}))
    assert failure.value.category == 'INVALID_RESPONSE'
    calls=[]
    def never(req): calls.append(req); raise AssertionError('must not send')
    with pytest.raises(AdapterError) as failure:
        execute(never, request(required_capabilities={'streaming'}))
    assert failure.value.category == 'UNSUPPORTED_CAPABILITY' and not calls
    with pytest.raises(AdapterError) as failure:
        execute(never, request(response_schema={'$ref':'https://host.test/schema'}))
    assert failure.value.category == 'INVALID_CONFIG' and not calls
    with pytest.raises(AdapterError):
        execute(provider, configuration=profile().model_copy(update={'max_response_bytes':10}))


def test_env_secret_references_only(monkeypatch):
    monkeypatch.setenv('V2C_TEST_SECRET', 'this is a secret')
    assert EnvSecretResolver()('env:V2C_TEST_SECRET') == 'this is a secret'
    with pytest.raises(AdapterError): EnvSecretResolver()('https://bad.test/key')
    with pytest.raises(AdapterError): EnvSecretResolver()('env:V2C_MISSING_CREDENTIAL')


def tls_config():
    # Files are deliberately unused only by explicitly injected test transport.
    return MTLSConfig(endpoint='https://runner.test', ca_file='/test/ca', cert_file='/test/cert', key_file='/test/key',
                      bearer_secret='test-only-secret-value', timeout_seconds=1, max_response_bytes=2000)


def test_vm_destroy_acceptance_is_not_confirmed_destruction():
    calls=[]
    async def run():
        def handler(req):
            calls.append((req.method,req.url.path))
            return httpx.Response(200,json={'vm_id':'vm1','runner_endpoint':'https://runner.test','state':'DESTROYING'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await HTTPVMProvider(tls_config(),client).destroy('vm1')
    result=asyncio.run(run())
    assert result.state=='DESTROYING' and calls==[('DELETE','/v1/vms/vm1')]


def test_runner_timeout_requires_readback():
    calls=[]
    async def run():
        def handler(req):
            calls.append(req)
            raise httpx.ReadTimeout('accepted but response lost', request=req)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await RunnerClient(tls_config(),client).status('command1')
    with pytest.raises(RunnerError) as failure: asyncio.run(run())
    assert failure.value.delivery_unknown and len(calls)==1


def test_strict_schema_compilation_keeps_named_default_field_and_requires_properties():
    from v2c.adapters.model import strict_json_schema
    original={'type':'object','properties':{'default':{'type':'string','default':'x'},'mode':{'$ref':'#/$defs/Mode'}},
              'required':['default'],'additionalProperties':False,'$defs':{'Mode':{'type':'object','properties':{'cwd':{'type':'string','default':'.'}},'additionalProperties':False}}}
    compiled=strict_json_schema(original)
    assert compiled['required']==['default','mode']
    assert compiled['properties']['default']=={'type':'string'}
    assert compiled['$defs']['Mode']['required']==['cwd']
    assert 'default' not in compiled['$defs']['Mode']['properties']['cwd']
    assert original['properties']['default']['default']=='x'


def test_runner_preserves_specific_unsupported_code():
    async def run():
        def handler(req): return httpx.Response(422,json={'code':'UNSUPPORTED','detail':'private operator detail'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await RunnerClient(tls_config(),client).prepare(__import__('v2c.adapters.runner',fromlist=['PrepareRequest']).PrepareRequest(
                workspace_id='ws',attempt_id='attempt',generation=1,archive_b64='',archive_sha256='a'*64,input_manifest={}))
    with pytest.raises(RunnerError) as failure: asyncio.run(run())
    assert failure.value.code=='UNSUPPORTED' and not failure.value.delivery_unknown
    assert 'private operator detail' not in str(failure.value)
