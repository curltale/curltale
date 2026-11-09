import json
import pytest
from conftest import fixture_model
from studio import editing, graph, ontology as O, search, tools, versions
from studio.chunker import build_chunks


def _core(e):
    return {k: v for k, v in e.items() if k not in ('relationships_out', 'relationships_in', 'id', 'subtypes')}


def test_roundtrip_is_lossless(dbmod):
    """uploaded file -> database -> assemble gives back exactly the uploaded model, custom properties included."""
    expected = fixture_model()
    got = O.assemble(O.published_id())
    assert {k: _core(e) for k, e in got['entities'].items()} == {k: _core(e) for k, e in expected['entities'].items()}
    for k in ('relationships', 'examples', 'access_policy'):
        assert got[k] == expected[k], k
    assert got['entities']['customer'].get('custom') and next(iter(got['entities']['customer']['attributes'].values())).get('custom')


def test_counts(dbmod):
    c = O.assemble(O.published_id())['ontology']['counts']
    assert c == {'entities': 23, 'attributes': 144, 'calculated': 7, 'relationships': 34, 'examples': 3}


def test_the_model_has_four_concepts(dbmod):
    m = O.assemble(O.published_id())
    assert set(m) == {'ontology', 'access_policy', 'entities', 'relationships', 'examples'}
    e = m['entities']['complaint']
    assert [f['name'] for f in e['filters']] == ['open complaint', 'closed complaint', 'billing complaint', 'service complaint']
    assert m['entities']['subscription']['calculated']['total_monthly_fee']['description'] and m['entities']['customer']['checks'][0]['id'] == 'DQ01'
    assert {r['status'] for r in m['relationships']} <= set(O.REL_STATUS)
    assert all(k.get('sql') and k['title'] for k in m['examples'])                       # the model holds query examples only: rules, terms and processes live in the Library


def test_join_path_prefers_confirmed_and_warns(dbmod):
    vid = O.published_id()
    p = graph.find_path(vid, 'Opportunity', 'Sales Order')
    assert p['best']['hops'][0]['status'] != 'confirmed' and p['best']['hops'][0]['warning']
    # the only all-confirmed route is via the shared Customer: a fan-out, so it must NOT be offered as a confirmed alternative
    assert 'confirmed_only' not in p and 'note' in p
    approx = p['approximate_via_shared_parent']
    assert approx['fan_out'] and approx['all_confirmed'] and any('FAN-OUT' in (h['warning'] or '') for h in approx['hops'])
    chain = graph.find_path(vid, 'Customer', 'Invoice')['best']
    # a true hierarchy walk (Customer -> Billing Account -> Invoice): confirmed, no sibling join
    assert len(chain['hops']) == 2 and chain['hops'][-1]['to'] == 'invoice' and chain['all_confirmed'] and not chain['fan_out']
    assert graph.find_path(vid, 'nonsense', 'Invoice')['error']
    assert graph.resolve_entity(vid, 'cost center') == 'billing_account'


def test_join_path_has_no_rules_of_its_own_any_more(dbmod):
    r = tools.join_path('Opportunity', 'Sales Order')
    assert 'rules_about_this_join' not in r and r['best']['hops']                                    # what the documents say about the ends comes as related_documents, when the caller may read any
    assert tools.join_path('customer')['resolved_as'] == 'neighbors'


def test_agent_tools_hide_restricted(dbmod):
    person = tools.get_entity('person')
    cols = {a['column'] for a in person['attributes']}
    assert 'full_name' in cols and not cols & {'national_id', 'passport_number', 'identity_verified', 'national_id_expiry'}
    assert 'error' in tools.get_attribute('person', 'passport_number')
    assert 'error' not in tools.get_attribute('person', 'full_name')
    blob = json.dumps(tools.search_ontology('passport number national id expiry', limit=25)) + json.dumps(tools.describe('National ID')) + json.dumps(tools.describe('person'))
    assert 'passport_number' not in blob and 'national_id_expiry' not in blob and 'identity_verified' not in blob


def test_search_finds_codes_and_terms(dbmod):
    vid = O.published_id()
    assert search.search(vid, 'total_monthly_fee', k=3, expand=False)[0]['id'] == 'calc:subscription.total_monthly_fee'
    assert any(r['id'] == 'ex:EX02' for r in search.search(vid, 'unpaid invoices of a customer', k=6, expand=False))        # query examples are searchable too
    assert any(r['id'].startswith('filter:complaint') for r in search.search(vid, 'open complaint', k=10, expand=False))


def test_describe_an_entity_brings_everything_that_applies(dbmod):
    e = tools.describe('subscription')
    assert e['resolved_as'] == 'entity' and e['filters'][0]['name'] == 'active subscription' and any(m['code'] == 'total_monthly_fee' for m in e['calculated_fields'])
    assert [k['id'] for k in e['examples']] == ['EX01'] and 'knowledge' not in e                                  # the query examples that apply to the entity
    assert tools.describe('EX02')['resolved_as'] == 'example' and tools.describe('billing')['resolved_as'] == 'entity'        # a concept synonym
    assert tools.describe('open complaint')['resolved_as'] == 'filter' and tools.describe('total_monthly_fee')['resolved_as'] == 'calculated'
    assert tools.describe('usage_monthly.metric')['allowed_values'][0] == 'api_calls'                                            # code lists live on the field
    assert tools.describe('client')['resolved_as'] == 'entity'                                                                   # an entity synonym
    assert tools.describe('ARR').get('error') and 'found_in_documents' not in tools.describe('ARR')                           # a word only the documents know: nothing here without document access


def test_draft_edit_publish_cycle(dbmod):
    with pytest.raises(editing.EditError):
        editing.upsert_entity('t', 'billing_account', {'definition': 'x'})          # no draft yet
    pub = O.published_id()
    d = versions.create_draft('0.52-test')
    with pytest.raises(editing.EditError):
        versions.create_draft('again')                                    # only one draft
    a = O.assemble(d)['entities']['billing_account']['attributes']['account_name']
    with pytest.raises(editing.EditError):
        editing.upsert_field('t', 'billing_account', 'account_name', {**a, 'sensitivity': 'restricted'})  # restricted needs access none
    with pytest.raises(editing.EditError):
        editing.upsert_field('t', 'billing_account', 'account_name', {**a, 'review_status': 'bogus'})
    editing.upsert_field('t', 'billing_account', 'account_name', {**a, 'review_status': 'needs_review'})
    editing.upsert_entity('t', 'billing_account', {'synonyms': ['cost center', 'ZZTOPSYN']})
    with pytest.raises(editing.EditError):
        editing.delete_entity('t', 'customer')                            # still used by relationships
    changes = {(x['kind'], x['key']) for x in versions.diff(pub, d)}
    assert changes == {('field', 'billing_account.account_name'), ('entity', 'billing_account')}
    assert editing.validate(d) == []
    # draft edits are invisible to agents until published
    assert 'ZZTOPSYN' not in json.dumps(tools.get_entity('billing_account'))
    res = versions.publish('t')
    tools.invalidate()
    assert res['version'] == d and res['chunks'] == len(build_chunks(O.assemble(d)))
    assert 'ZZTOPSYN' in json.dumps(tools.get_entity('billing_account'))
    assert search.search(d, 'ZZTOPSYN', k=3, expand=False)[0]['id'] == 'entity:billing_account'
    assert [v['status'] for v in versions.list_versions()] == ['published', 'archived']


def test_publish_blocked_by_dangling_reference(dbmod):
    d = versions.create_draft('0.53-bad')
    with pytest.raises(editing.EditError):
        editing.upsert_relationship('t', 'R-999', {'from': 'Customer', 'to': 'Nope', 'predicate': 'x', 'cardinality': 'many-to-one', 'join': 'a=b',
                                                   'status': 'confirmed', 'status_detail': 'ok', 'sentence': 's'})
    with pytest.raises(editing.EditError):
        editing.upsert_example('t', 'X1', {'title': 't', 'sql': 'select 1', 'applies_to': ['no_such_entity']})
    with pytest.raises(editing.EditError):
        editing.upsert_example('t', 'X2', {'title': 't', 'body': 'b', 'sql': ' '})                      # an example needs its SQLs SQL
    versions.discard_draft()


def test_tools_survive_sloppy_model_arguments(dbmod):
    for types in ('["entity"]', 'entity,calculated', ['entity'], None, [], '[]'):
        assert 'results' in tools.call('search', {'query': 'customer', 'types': types})
    assert 'results' in tools.call('search', {'query': 'customer', 'limit': 'ten'})
    assert 'error' in tools.call('describe', {'nope': 1})            # wrong arguments come back as an error, never as an exception
    assert 'error' in tools.call('no_such_tool', {})
