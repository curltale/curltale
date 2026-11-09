import json
import pytest
from fastapi.testclient import TestClient
from studio import access, auth, search, tools, ontology as O

NONE = {'confidential': False, 'personal': False, 'restricted': False}
FULL = {'confidential': True, 'personal': True, 'restricted': True}


@pytest.fixture(autouse=True)
def reset_caller(dbmod):
    tok = access.CALLER.set(('anonymous', access.DEFAULT))
    yield
    access.CALLER.reset(tok)


def as_(ent):
    access.set_caller('t', ent)


def cols(entity='person'):
    return {a['column'] for a in tools.get_entity(entity)['attributes']}


def test_unknown_caller_fails_closed():
    assert access.caller()[1]['restricted'] is False
    assert 'passport_number' not in cols()


def test_restricted_only_with_explicit_grant():
    as_(access.DEFAULT)
    assert not cols() & {'passport_number', 'national_id'}
    assert tools.get_attribute('person', 'passport_number')['error'].startswith('field')   # existence not confirmed
    as_(FULL)
    assert {'passport_number', 'national_id'} <= cols()
    assert tools.get_attribute('person', 'passport_number')['agent_access'] == 'none'


def test_search_and_terms_follow_entitlement():
    vid = O.published_id()
    q = 'passport number'
    as_(access.DEFAULT)
    ids = [r['id'] for r in search.search(vid, q, k=25)]
    assert 'attr:person.passport_number' not in ids
    assert 'error' in tools.describe('person.passport_number')
    as_(FULL)
    assert any(r['id'] == 'attr:person.passport_number' for r in search.search(vid, q, k=10))
    assert 'error' not in tools.describe('person.passport_number') and tools.describe('person.passport_number')['agent_access'] == 'none'
    assert 'attr:person.passport_number' not in [r['id'] for r in search.search(vid, q, k=10, ent=access.DEFAULT)]   # explicit ent beats caller


def test_confidential_and_personal_are_separate_switches():
    as_(NONE)
    assert tools.get_attribute('person', 'full_name')['error'] == 'not entitled'
    assert 'full_name' not in cols()
    assert tools.get_entity('person')['withheld_attributes']
    without = len(tools.get_calculated()['calculated_fields'])
    as_({**NONE, 'confidential': True})
    assert len(tools.get_calculated()['calculated_fields']) > without
    as_({**NONE, 'personal': True})
    assert tools.get_attribute('person', 'full_name')['business_name']


def test_get_access_reports_the_caller():
    as_(NONE)
    r = tools.get_access()
    assert r['entitlements'] == {'internal': True, **NONE, 'data': False} and 'NOT GRANTED' in r['meaning']['restricted']
    as_(FULL)
    assert tools.get_access()['entitlements']['restricted'] is True


# ---- end to end through the real MCP endpoint: the token decides what the agent sees


def mcp_call(client, token, name, args=None):
    h = {'authorization': f'Bearer {token}', 'content-type': 'application/json', 'accept': 'application/json, text/event-stream'}
    r = client.post('/mcp', headers=h, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args or {}}})
    assert r.status_code == 200, r.text
    return json.loads(r.json()['result']['content'][0]['text'])


def test_mcp_token_carries_entitlements(client):
    basic = auth.create_api_token('basic', {'restricted': False})
    god = auth.create_api_token('granted', {'restricted': True})
    assert client.post('/mcp', json={}).status_code == 401
    assert mcp_call(client, basic, 'get_access')['caller'] == 'basic'
    assert mcp_call(client, basic, 'get_access')['entitlements']['restricted'] is False
    assert 'error' in mcp_call(client, basic, 'describe', {'name': 'person.passport_number'})
    assert mcp_call(client, god, 'get_access')['entitlements']['restricted'] is True
    assert mcp_call(client, god, 'describe', {'name': 'person.passport_number'})['sensitivity'] == 'restricted'
    # the two callers interleave without leaking into each other
    assert 'error' in mcp_call(client, basic, 'describe', {'name': 'person.passport_number'})


def test_token_admin_api(client):
    import os
    pw = os.environ['INITIAL_ADMIN_PASSWORD']                                   # set by conftest
    assert client.post('/api/login', json={'username': 'admin', 'password': pw}).status_code == 200
    r = client.post('/api/admin/tokens', json={'name': 'api-test', 'entitlements': {'restricted': True}})
    assert r.status_code == 200 and r.json()['entitlements']['restricted'] is True and r.json()['token'].startswith('ss_')
    tok = client.get('/api/admin/tokens').json()
    tid = [t['id'] for t in tok if t['name'] == 'api-test'][0]
    assert client.put(f'/api/admin/tokens/{tid}/entitlements', json={'entitlements': {'restricted': False}}).json()['restricted'] is False
    assert client.delete(f'/api/admin/tokens/{tid}').status_code == 200
    assert 'api-test' not in [t['name'] for t in client.get('/api/admin/tokens').json()]
    # every grant is in the audit log
    assert [a for a in client.get('/api/audit').json() if a['action'] == 'grant' and a['key'] == 'token:api-test']


# ---- what an external agent receives from the MCP server, with nothing else in its prompt
def _rpc(client, token, method, params=None, id=1):
    h = {'authorization': f'Bearer {token}', 'content-type': 'application/json', 'accept': 'application/json, text/event-stream'}
    r = client.post('/mcp', headers=h, json={'jsonrpc': '2.0', 'id': id, 'method': method, 'params': params or {}})
    assert r.status_code == 200, r.text
    return r.json()['result']


def test_instructions_carry_the_workflow(client):
    tok = auth.create_api_token('ctx-check', {'data': True})
    ins = _rpc(client, tok, 'initialize', {'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '1'}})['instructions']
    for needle in ('get_access', 'plan_query', 'query_data', 'describe', 'join_path', 'ONE SQL', 'pending_confirmation', 'synthetic'):
        assert needle in ins, needle
    for needle in ('Never search for restricted data', 'optional or unreliable', 'pre-aggregated'):          # the behaviour that used to be pinned rules is part of the guide
        assert needle.lower() in ins.lower(), needle
    assert 'The rules every answer must respect' not in ins         # rules, terms and processes are documents now, not part of the model
    assert any(k['id'] == 'EX01' for k in tools.get_entity('subscription')['examples'])      # query examples come with the entity
    assert len(ins) < 10000                                         # instructions are loaded into every session: keep them small


def test_every_tool_is_documented_and_read_only(client):
    tok = auth.create_api_token('ctx-check2', {'data': True})
    tools_ = _rpc(client, tok, 'tools/list')['tools']
    assert sorted(t['name'] for t in tools_) == ['describe', 'get_access', 'join_path', 'plan_query', 'query_data', 'search']
    for t in tools_:
        assert t['annotations']['readOnlyHint'] is True and t['annotations']['destructiveHint'] is False and t.get('title'), t['name']
        assert len(t['description']) > 60, t['name']
        for pname, prop in t['inputSchema'].get('properties', {}).items():
            assert pname != 'ctx'                                   # the internal request context never leaks into the public schema
            assert prop.get('description'), f"{t['name']}.{pname} has no description"
    by = {t['name']: t for t in tools_}
    assert 'entity' in by['search']['inputSchema']['properties']['types']['description']                # valid types are listed
    assert by['describe']['inputSchema']['properties']['kind']['enum'][0] == 'auto' and 'example' in by['describe']['inputSchema']['properties']['kind']['enum']


def test_prompts_and_resources_are_offered(client):
    tok = auth.create_api_token('ctx-check3', {'data': True})
    names = {p['name'] for p in _rpc(client, tok, 'prompts/list')['prompts']}
    assert {'explore_model', 'data_question'} <= names
    text = _rpc(client, tok, 'prompts/get', {'name': 'data_question', 'arguments': {'question': 'open complaints per customer'}})['messages'][0]['content']['text']
    assert 'plan_query' in text and 'open complaints per customer' in text
    uris = {r['uri'] for r in _rpc(client, tok, 'resources/list')['resources']}
    assert 'ontology://guide' in uris and 'ontology://rules/agent' not in uris
    guide = _rpc(client, tok, 'resources/read', {'uri': 'ontology://guide'})['contents'][0]['text']
    assert 'plan_query' in guide
