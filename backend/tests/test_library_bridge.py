import json
import pytest
from studio import access, auth, db, llm, tools
from studio.library import agent, bridge, jobs, scope as S
from test_library_access import world, _as, _admin, MARK_A, MARK_B   # noqa: F401
from test_library_storage_jobs import _upload
from test_library_wiki import _run, ai   # noqa: F401
from test_library_topics import REFUNDS, PREMIUM, TopicFake, analysis, DOC
from test_library_agent import usage_everywhere
from test_access import mcp_call, _rpc


class Linker(TopicFake):
    """Answers the model-linker (a plain chat call) with a fixed list of links."""
    answer = {'links': [{'entity': 'customer', 'relation': 'governs', 'confidence': 0.9, 'reason': 'the policy applies to customers'},
                        {'entity': 'subscription', 'relation': 'about', 'confidence': 0.4, 'reason': 'maybe'},
                        {'entity': 'not_an_entity', 'relation': 'about', 'confidence': 0.99, 'reason': 'invented'},
                        {'entity': 'invoice', 'relation': 'rules', 'confidence': 0.9, 'reason': 'bad relation'}]}

    linker_calls = 0

    def chat(self, messages, role='assistant', **kw):
        if role == 'linker':
            Linker.linker_calls += 1
            return 'Here: ' + json.dumps(self.answer if 'Title: Refund policy' in messages[1]['content'] else {'links': []})
        return super().chat(messages, role, **kw)

DEFAULT_ANSWER = Linker.answer
MODEL_LINKS = [{'entity': 'customer', 'relation': 'governs', 'confidence': 0.9, 'reason': 'the policy applies to customers'},
               {'entity': 'subscription', 'relation': 'about', 'confidence': 0.4, 'reason': 'maybe'},
               {'entity': 'not_an_entity', 'relation': 'about', 'confidence': 0.99, 'reason': 'invented'},
               {'entity': 'invoice', 'relation': 'rules', 'confidence': 0.9, 'reason': 'bad relation'}]


@pytest.fixture
def linked(client, world, ai):
    a = world['a']['id']
    Linker.linker_calls = 0
    f = ai(Linker([analysis(dict(REFUNDS, links=[], model_links=MODEL_LINKS), PREMIUM)]))          # the analyzer, which was shown the model, says what each topic is about
    _as(client, 'ann'); _upload(client, a, 'a.md', DOC)
    _run()
    return f


def _links(client, a, slug='refund-policy'):
    return {(l['ref'], l['status']): l for l in client.get(f'/api/library/collections/{a}/topics/{slug}').json()['model_links']}


def test_topics_are_linked_to_model_entities_with_confidence_deciding_the_status(client, world, linked):
    a = world['a']['id']
    assert Linker.linker_calls == 0                                                                        # no extra model call: the analyzer did it while reading the source
    ls = _links(client, a)
    assert set(ls) == {('customer', 'confirmed'), ('subscription', 'suggested')}                          # invented entities and bad relations are dropped
    c = ls[('customer', 'confirmed')]
    assert c['name'] == 'Customer' and c['relation'] == 'governs' and c['label'] == 'governs' and c['origin'] == 'ai' and c['in_model'] is True and c['confidence'] == 0.9
    _as(client, 'ann')
    assert client.post(f'/api/library/collections/{a}/topics/refund-policy/model-links/decide', json={'kind': 'entity', 'ref': 'subscription', 'accept': True}).status_code == 200
    assert ('subscription', 'confirmed') in _links(client, a)
    assert client.post(f'/api/library/collections/{a}/topics/refund-policy/model-links/decide', json={'kind': 'entity', 'ref': 'customer', 'accept': False}).status_code == 200
    assert ('customer', 'confirmed') not in _links(client, a) and not any(k[0] == 'customer' for k in _links(client, a))               # rejected links are not shown
    # a later suggestion run does not bring a rejected link back
    from studio.library import topics
    tid = topics.find(a, 'refund-policy')['id']
    bridge.suggest(a, tid)
    assert not any(k[0] == 'customer' for k in _links(client, a))


def test_a_person_links_and_unlinks_and_the_model_is_checked(client, world, linked):
    a = world['a']['id']
    _as(client, 'ann')
    put = lambda **b: client.put(f'/api/library/collections/{a}/topics/premium-plan/model-links', json=b)
    assert put(kind='entity', ref='Customer', relation='about').json()['ref'] == 'customer'                   # a name or synonym finds the entity; the key is stored
    assert put(kind='entity', ref='client', relation='about').status_code == 200
    assert put(kind='entity', ref='no such thing').status_code == 422 and put(kind='entity', ref='customer', relation='worships').status_code == 422
    assert put(kind='field', ref='customer.segment').status_code == 200
    assert put(kind='field', ref='customer.email').status_code == 422 and put(kind='field', ref='customer.nothing').status_code == 422      # personal data structure is never linked
    got = _links(client, a, 'premium-plan')
    assert {k[0] for k in got} == {'customer', 'customer.segment'} and got[('customer', 'confirmed')]['origin'] == 'manual'
    _as(client, 'bob')
    assert put(kind='entity', ref='customer').status_code == 403
    _as(client, 'ann')
    assert client.delete(f'/api/library/collections/{a}/topics/premium-plan/model-links', params={'kind': 'field', 'link': 'customer.segment'}).status_code == 200
    assert {k[0] for k in _links(client, a, 'premium-plan')} == {'customer'}
    _admin(client)
    assert 'link_model' in {r['action'] for r in client.get('/api/admin/audit?kind=collection').json()}


def test_the_links_are_visible_only_through_the_collection_and_only_as_the_reader_may_see(client, world, linked):
    a = world['a']['id']
    docs = lambda who: (_as(client, who), client.get('/api/entities/customer/documents').json()['documents'])[1]
    assert docs('eve') == []                                                                              # no grant: the entity page shows nothing, not even that a collection exists
    d = docs('bob')                                                                                       # read access is enough
    assert [b['collection'] for b in d] == [f'Alpha {MARK_A}'] and d[0]['usage'] == 'public' and d[0]['usage_note']
    assert [(t['topic'], t['label']) for t in d[0]['topics']] == [('refund-policy', 'governs')] and d[0]['topics'][0]['link'].endswith(a)
    _admin(client)
    assert client.get('/api/entities/customer/documents').json()['documents'] == []                                                                            # an admin who has not granted themselves access sees none either
    assert client.get('/api/entities/subscription/documents').json()['documents'] == []                  # only confirmed links count (this one is only suggested)
    # the model itself is untouched: its entity carries no trace of the collection
    ent = client.get('/api/entities/customer').json()
    assert MARK_A not in json.dumps(ent) and 'Refund policy' not in json.dumps(ent) and 'refund-policy' not in json.dumps(ent)
    # a field link is hidden from a reader who may not see that field, even though it sits in the collection
    tid = db.conn().__enter__().execute("SELECT id FROM library.topic WHERE collection_id=%s AND slug='premium-plan'", (a,)).fetchone()['id']
    with db.conn() as c:
        c.execute("INSERT INTO library.topic_model_ref (collection_id,topic_id,kind,ref,status,origin) VALUES (%s,%s,'field','customer.email','confirmed','manual')", (a, tid))
    shown = lambda ent: [m['ref'] for m in bridge.resolve(bridge.refs_of(tid, a), access.normalize(ent))]
    assert 'customer.email' not in shown({'personal': False}) and 'customer.email' in shown({'personal': True})


def test_agents_see_the_links_and_the_model_tools_point_to_documents_they_may_read(client, world, linked):
    a, b = world['a']['id'], world['b']['id']
    tok = auth.create_api_token('agent-bridge', {'data': False, 'personal': False})
    none = auth.create_api_token('agent-bridge-none', {'data': False})
    with db.conn() as c:
        tid = c.execute("SELECT id FROM api_token WHERE name='agent-bridge'").fetchone()['id']
        c.execute("INSERT INTO library.grant_ (collection_id, token_id, level) VALUES (%s,%s,'read')", (a, tid))
    r = mcp_call(client, tok, 'library_search', {'query': 'refund thirty days'})
    topic = r['results'][0]['topics'][0]
    assert {m['ref'] for m in topic['model']} == {'customer'} and topic['model'][0]['relation'] == 'governs'
    d = mcp_call(client, tok, 'describe', {'name': 'customer'})
    assert d['related_documents'][0]['collection'] == f'Alpha {MARK_A}' and d['related_documents'][0]['topics'][0]['topic'] == 'refund-policy'
    usage_everywhere(d['related_documents'])
    assert 'related_documents' not in mcp_call(client, none, 'describe', {'name': 'customer'})              # a credential with no documents gets the model alone
    assert 'related_documents' not in mcp_call(client, tok, 'describe', {'name': 'subscription'})
    _as(client, 'ann'); client.patch(f'/api/library/collections/{a}', json={'agents_may_read': False})
    assert 'related_documents' not in mcp_call(client, tok, 'describe', {'name': 'customer'})              # a collection closed to agents does not leak through the model either
    with db.conn() as c:
        c.execute("DELETE FROM api_token WHERE name LIKE 'agent-bridge%'")


def test_the_model_graph_can_show_the_documents_a_reader_can_reach(client, world, linked):
    plain = client.get('/api/graph').json()
    _as(client, 'bob')
    g = client.get('/api/graph', params={'documents': 'true'}).json()
    topic_nodes = [n for n in g['nodes'] if n['kind'] == 'topic']
    assert {n['slug'] for n in topic_nodes} == {'refund-policy'} and topic_nodes[0]['domain'] == f'Documents: Alpha {MARK_A}'
    assert {(e['from'].split(':')[-1], e['to']) for e in g['edges'] if e['from'].startswith('topic:')} == {('refund-policy', 'customer')}
    assert len(g['nodes']) == len(plain['nodes']) + 1
    assert len(client.get('/api/graph').json()['nodes']) == len(plain['nodes'])
    _as(client, 'eve')
    assert all(n.get('kind') != 'topic' for n in client.get('/api/graph', params={'documents': 'true'}).json()['nodes'])


def test_a_collection_without_ai_gets_no_automatic_links(client, world, linked):
    a = world['a']['id']
    _as(client, 'ann')
    client.patch(f'/api/library/collections/{a}', json={'allow_ai': False})
    assert client.post(f'/api/library/collections/{a}/topics/refund-policy/model-links/suggest').status_code == 422


def test_a_topic_may_be_about_as_many_entities_as_it_really_is_not_just_three(client, world, ai):
    a = world['a']['id']
    keys = list(tools._onto(tools._vid())['entities'])[:6]
    ai(Linker([analysis(dict(REFUNDS, links=[], model_links=[{'entity': k, 'relation': 'uses', 'confidence': 0.9, 'reason': 'x'} for k in keys]))]))
    _as(client, 'ann'); _upload(client, a, 'a.md', DOC)
    _run()
    assert {k[0] for k in _links(client, a)} == set(keys)


def test_a_very_large_model_is_narrowed_to_likely_entities_before_the_linker_sees_it(client, world, monkeypatch):
    onto = tools._onto(tools._vid())
    assert len(onto['entities']) > 6
    monkeypatch.setattr(bridge, 'WHOLE_MODEL', 5)
    cat = bridge.entity_catalog('Customer. Companies and households that buy from us, their segment and status.')
    assert 0 < len(cat) < len(onto['entities']) and 'customer' in cat
    assert len(bridge.entity_catalog()) == 5                          # no text to narrow by: the first ones, never an error


def test_topics_link_to_each_other_inside_a_collection_and_the_graphs_show_both_kinds_of_link(client, world, ai):
    a = world['a']['id']
    about = [{'entity': 'customer', 'relation': 'about', 'confidence': 0.9, 'reason': 'x'}]
    ai(Linker([analysis(dict(REFUNDS, links=[{'target_title': 'Premium plan', 'relation_type': 'governs', 'context': 'x'}], model_links=about), dict(PREMIUM, model_links=about))]))
    _as(client, 'ann'); _upload(client, a, 'a.md', DOC)
    _run()
    own = client.get(f'/api/library/collections/{a}/graph').json()                                   # one graph per collection: topics and their links
    assert {(e['from'], e['to']) for e in own['edges']} == {('refund-policy', 'premium-plan')} and all(n['kind'] != 'entity' for n in own.get('nodes', []) if 'kind' in n)
    withmodel = client.get(f'/api/library/collections/{a}/graph', params={'model': 'true'}).json()
    assert 'entity:customer' in {n['key'] for n in withmodel['nodes']} and ('refund-policy', 'entity:customer') in {(e['from'], e['to']) for e in withmodel['edges']}
    _as(client, 'bob')                                                                               # the model graph with documents also draws the links between the topics
    g = client.get('/api/graph', params={'documents': 'true'}).json()
    topic_edges = {(e['from'].split(':')[-1], e['to'].split(':')[-1]) for e in g['edges'] if e['from'].startswith('topic:') and e['to'].startswith('topic:')}
    assert topic_edges == {('refund-policy', 'premium-plan')}


def test_what_the_model_says_a_topic_is_shows_as_its_kind(client, world, ai):
    a = world['a']['id']
    ai(Linker([analysis(dict(PREMIUM, model_links=[{'entity': 'subscription', 'relation': 'instance_of', 'confidence': 0.95, 'reason': 'a specific plan'}]),
                        dict(REFUNDS, links=[], model_links=[{'entity': 'customer', 'relation': 'governs', 'confidence': 0.9, 'reason': 'x'}]))]))
    _as(client, 'ann'); _upload(client, a, 'a.md', DOC)
    _run()
    rows = {t['slug']: t for t in client.get(f'/api/library/collections/{a}/topics').json()['topics']}
    assert rows['premium-plan']['kind_of'] == ['Subscription'] and rows['refund-policy']['kind_of'] == []                  # only 'is a kind of' says what a topic is
    assert client.get(f'/api/library/collections/{a}/topics/premium-plan').json()['topic']['kind_of'] == ['Subscription']
    with db.conn() as c:
        c.execute("INSERT INTO library.grant_ (collection_id, username, level) VALUES (%s,'eve','read') ON CONFLICT DO NOTHING", (a,))
    agent.set_principal(S.for_user({'username': 'eve', 'role': 'viewer', 'extra_roles': []}), 'agent')
    t = tools.call('library_search', {'query': 'premium plan unlimited seats'})['results'][0]['topics'][0]
    assert t['topic'] == 'premium-plan' and t['kind_of'] == ['Subscription']


def test_a_person_can_still_ask_the_ai_to_link_a_topic_that_came_before(client, world, ai):
    a = world['a']['id']
    Linker.linker_calls = 0
    ai(Linker([analysis(dict(REFUNDS, links=[]), PREMIUM)]))
    _as(client, 'ann'); _upload(client, a, 'a.md', DOC)
    _run()
    assert not _links(client, a, 'refund-policy') and Linker.linker_calls == 0
    assert client.post(f'/api/library/collections/{a}/topics/refund-policy/model-links/suggest').status_code == 200
    _run()
    assert Linker.linker_calls == 1 and ('customer', 'confirmed') in _links(client, a, 'refund-policy')


def _topic(title, slug, link, **kw):
    return {'title': title, 'slug': slug, 'action': 'create', 'existing_slug': '', 'topic_type': 'Rule', 'summary': f'{title} summary.', 'aliases': kw.get('aliases', []), 'links': [],
            'claims': [{'claim_text': f'{title} is stated here.', 'claim_type': 'rule', 'source_quote': 'Yearly plans are refundable within thirty days'}],
            'model_links': [{'entity': link, 'relation': 'governs', 'confidence': 0.9, 'reason': 'x'}] if link else []}


def test_plan_query_brings_every_relevant_document_briefly_not_just_three(client, world, ai):
    a = world['a']['id']
    ai(Linker([analysis(*[_topic(f'Rule number {i}', f'rule-{i}', 'customer') for i in range(6)], _topic('Unrelated canteen note', 'canteen', None))]))
    _as(client, 'ann'); _upload(client, a, 'a.md', DOC)
    _run()
    tok = auth.create_api_token('agent-plan', {'data': False})
    none = auth.create_api_token('agent-plan-none', {'data': False})
    with db.conn() as c:
        tid = c.execute("SELECT id FROM api_token WHERE name='agent-plan'").fetchone()['id']
        c.execute("INSERT INTO library.grant_ (collection_id, token_id, level) VALUES (%s,%s,'read')", (a, tid))
    try:
        plan = mcp_call(client, tok, 'plan_query', {'question': 'how many customers are active', 'entities': ['customer']})
        block = plan['documents'][0]
        usage_everywhere(plan['documents'])
        shown = {t['topic'] for t in block['topics']}
        assert {f'rule-{i}' for i in range(6)} <= shown and 'canteen' not in shown                       # all six linked topics, none cut at three; the unrelated one is not there
        t = block['topics'][0]
        assert set(t) == {'topic', 'title', 'type', 'summary', 'why', 'link'} and 'is about Customer' in t['why'] and len(t['summary']) <= 200      # brief: no statements, no page
        assert 'rules' not in plan and 'knowledge_matching_the_question' not in plan and 'documents_note' in plan
        assert 'documents' not in mcp_call(client, none, 'plan_query', {'question': 'how many customers are active', 'entities': ['customer']})      # no grants, no documents
    finally:
        with db.conn() as c:
            c.execute("DELETE FROM api_token WHERE name LIKE 'agent-plan%'")


def test_a_name_only_the_documents_know_resolves_through_the_topic_to_its_entity(client, world, ai):
    a = world['a']['id']
    ai(Linker([analysis(_topic('Cancellation notice period', 'cnap', 'subscription', aliases=['CNAP']))]))
    _as(client, 'ann'); _upload(client, a, 'a.md', DOC)
    _run()
    tok = auth.create_api_token('agent-term', {'data': False})
    with db.conn() as c:
        tid = c.execute("SELECT id FROM api_token WHERE name='agent-term'").fetchone()['id']
        c.execute("INSERT INTO library.grant_ (collection_id, token_id, level) VALUES (%s,%s,'read')", (a, tid))
    try:
        d = mcp_call(client, tok, 'describe', {'name': 'CNAP'})
        assert d['resolved_as'] == 'document'
        topic = d['found_in_documents'][0]['topics'][0]
        assert topic['title'] == 'Cancellation notice period' and topic['exact'] is True and [m['ref'] for m in topic['model']] == ['subscription']        # the entity to use is right there
        usage_everywhere(d['found_in_documents'])
        assert mcp_call(client, tok, 'describe', {'name': 'zzzz-unknown-word'}).get('error')
    finally:
        with db.conn() as c:
            c.execute("DELETE FROM api_token WHERE name='agent-term'")


def test_the_old_knowledge_table_is_converted_keeping_only_the_examples():
    """Opening a database made before rules, terms, processes and notes left the model: they are deleted, the examples are kept under their new name."""
    import re
    import psycopg
    sql = open(__import__('os').path.join(__import__('os').path.dirname(__import__('studio').__file__), 'schema.sql')).read()
    block = re.search(r'DO \$\$.*?\$\$;', sql, re.S).group(0)
    url = __import__('os').environ['DATABASE_URL'].rsplit('/', 1)[0] + '/studio_migration_test'
    admin = __import__('os').environ['DATABASE_URL'].rsplit('/', 1)[0] + '/postgres'
    with psycopg.connect(admin, autocommit=True) as a:
        a.execute('DROP DATABASE IF EXISTS studio_migration_test WITH (FORCE)'); a.execute('CREATE DATABASE studio_migration_test')
    try:
        with psycopg.connect(url, autocommit=True) as c:
            c.execute('CREATE TABLE ontology_version (id serial primary key)')
            c.execute("INSERT INTO ontology_version DEFAULT VALUES")
            c.execute("""CREATE TABLE knowledge (version_id int NOT NULL REFERENCES ontology_version ON DELETE CASCADE, id text NOT NULL, position int NOT NULL DEFAULT 0, type text NOT NULL, title text, body text,
                         pinned boolean NOT NULL DEFAULT false, governs jsonb NOT NULL DEFAULT '[]', extra json NOT NULL DEFAULT '{}', CONSTRAINT knowledge_v2_pk PRIMARY KEY (version_id, id))""")
            c.execute("CREATE TABLE chunk (version_id int NOT NULL, id text NOT NULL, type text NOT NULL, text text, PRIMARY KEY (version_id, id))")
            c.execute("INSERT INTO knowledge (version_id,id,type,title,pinned) VALUES (1,'R01','rule','a rule',true),(1,'T-x','term','a term',false),(1,'EX01','example','an example',false),(1,'PR1','process','p',false)")
            c.execute("INSERT INTO chunk VALUES (1,'kb:R01','knowledge','r'),(1,'kb:T-x','knowledge','t'),(1,'kb:EX01','knowledge','e'),(1,'entity:customer','entity','c')")
            c.execute(block)
            assert [r[0] for r in c.execute('SELECT id FROM example ORDER BY id').fetchall()] == ['EX01']
            assert c.execute("SELECT to_regclass('public.knowledge')").fetchone()[0] is None
            assert sorted(r[0] for r in c.execute("SELECT id FROM chunk").fetchall()) == ['entity:customer', 'ex:EX01']
            assert {r[0] for r in c.execute("SELECT column_name FROM information_schema.columns WHERE table_name='example'").fetchall()} == {'version_id', 'id', 'position', 'title', 'body', 'governs', 'extra'}
            c.execute(block)                                                                          # running it again changes nothing
    finally:
        with psycopg.connect(admin, autocommit=True) as a:
            a.execute('DROP DATABASE IF EXISTS studio_migration_test WITH (FORCE)')
