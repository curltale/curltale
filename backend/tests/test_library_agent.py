"""What agents can reach: the document tools (library_context first) through the real MCP endpoint, and as Alfred, collection by collection."""
import json
import pytest
from studio import auth, db, tools
from studio.library import agent, rerank, scope as S
from test_library_access import world, _as, _admin, MARK_A, MARK_B   # noqa: F401
from test_library_storage_jobs import _upload
from test_library_wiki import _run, ai   # noqa: F401
from test_library_ingest import embedded   # noqa: F401
from test_library_topics import REFUNDS, PREMIUM, TopicFake, analysis, DOC
from test_access import mcp_call, _rpc


def mcp_text(client, token, name, args=None):
    h = {'authorization': f'Bearer {token}', 'content-type': 'application/json', 'accept': 'application/json, text/event-stream'}
    r = client.post('/mcp', headers=h, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args or {}}})
    res = r.json()['result']
    assert len(res['content']) == 1 and 'structuredContent' not in res                     # one text block, no structured duplicate
    return res['content'][0]['text']


def doc(result, e):
    """The document a piece of evidence points to (documents are listed once per result)."""
    return next(d for d in result['documents'] if d['doc'] == e['doc'])


def usage_everywhere(x):
    """Every block that names a collection must carry its usage tag and note."""
    if isinstance(x, dict):
        if 'collection' in x:
            assert x.get('usage') in ('public', 'internal') and x.get('usage_note') and x['usage_note'] == S.USAGE_NOTE[x['usage']], x
        for v in x.values():
            usage_everywhere(v)
    elif isinstance(x, list):
        for v in x:
            usage_everywhere(v)


@pytest.fixture
def library(client, world, ai, embedded):
    """A holds the refund topics (public); B holds an internal topic with a marker; three tokens: A only, A and B, none."""
    a, b = world['a']['id'], world['b']['id']
    secret = dict(PREMIUM, title=f'Escalation {MARK_B}', slug='escalation', topic_type='Process', summary=f'Internal escalation steps {MARK_B}', topic_type_fallback='Topic',
                  claims=[{'claim_text': f'Escalate refund disputes to the duty manager {MARK_B}.', 'claim_type': 'step', 'source_quote': 'Yearly plans are refundable within thirty days'}])
    fake = ai(TopicFake([analysis(dict(REFUNDS, aliases=['Cancellation policy']), PREMIUM), analysis(secret)]))
    with db.conn() as c:
        c.execute("INSERT INTO library.grant_ (collection_id, username, level) VALUES (%s,'ed','write') ON CONFLICT DO NOTHING", (b,))
    _as(client, 'ann'); _upload(client, a, 'a.md', DOC)
    _as(client, 'ed'); _upload(client, b, 'b.md', DOC + b' internal')
    # the internal collection is tagged internal
    _admin(client); client.post(f'/api/library/collections/{b}/self-grant', json={'level': 'manage'}); client.patch(f'/api/library/collections/{b}', json={'usage': 'internal'})
    _run()
    toks = {n: auth.create_api_token(f'agent-{n}', {'data': False, **({'sources': False} if n == 'quiet' else {})}) for n in ('a', 'ab', 'none', 'quiet')}
    with db.conn() as c:
        ids = {n: c.execute('SELECT id FROM api_token WHERE name=%s', (f'agent-{n}',)).fetchone()['id'] for n in toks}
        for cid, who in ((a, 'a'), (a, 'ab'), (b, 'ab'), (a, 'quiet')):
            c.execute('INSERT INTO library.grant_ (collection_id, token_id, level) VALUES (%s,%s,%s)', (cid, ids[who], 'read'))
    yield {**world, 'toks': toks, 'fake': fake}
    with db.conn() as c:
        c.execute("DELETE FROM api_token WHERE name LIKE 'agent-%'")


def test_document_tools_are_listed_only_to_credentials_that_can_read_something(client, library):
    names = lambda tok: sorted(t['name'] for t in _rpc(client, tok, 'tools/list')['tools'])
    assert names(library['toks']['none']) == ['describe', 'get_access', 'join_path', 'plan_query', 'query_data', 'search']
    assert names(library['toks']['a']) == sorted(['describe', 'get_access', 'join_path', 'plan_query', 'query_data', 'search'] + list(agent.LIBRARY))
    for t in _rpc(client, library['toks']['a'], 'tools/list')['tools']:
        if t['name'].startswith('library_'):
            assert 'public' in t['description'] and 'internal' in t['description'] and 'do not quote' in t['description']           # the rule is in every tool's description
            assert t['annotations']['readOnlyHint'] is True and 'ctx' not in t['inputSchema'].get('properties', {})
    ins = _rpc(client, library['toks']['a'], 'initialize', {'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '1'}})['instructions']
    assert 'library_context' in ins and 'answerable' in ins and 'never quote it' in ins and len(ins) < 12000                # ...and in the server instructions


def test_a_token_reaches_only_the_collections_it_was_granted(client, library):
    t = library['toks']
    cols = mcp_call(client, t['a'], 'library_collections')
    assert [c['collection'] for c in cols['collections']] == [f'Alpha {MARK_A}'] and cols['collections'][0]['usage'] == 'public' and cols['collections'][0]['topics'] == 2
    both = mcp_call(client, t['ab'], 'library_collections')
    assert {c['collection']: c['usage'] for c in both['collections']} == {f'Alpha {MARK_A}': 'public', f'Bravo {MARK_B}': 'internal'}
    assert mcp_call(client, t['none'], 'library_collections').get('error') or True
    for q in ('refund within thirty days', 'escalate disputes duty manager', MARK_B):
        r = mcp_call(client, t['a'], 'library_search', {'query': q})
        assert MARK_B not in json.dumps(r['results']) and all(b['collection'] == f'Alpha {MARK_A}' for b in r['results']), q          # (the query itself is echoed back, so look at the results)
        usage_everywhere(r)
    r = mcp_call(client, t['a'], 'library_topic', {'collection': f'Bravo {MARK_B}', 'topic': 'escalation'})
    assert 'error' in r and MARK_B not in json.dumps(r).replace(f'Bravo {MARK_B}', '')
    ghost = mcp_call(client, t['a'], 'library_topic', {'collection': 'No such collection', 'topic': 'x'})
    forbidden = mcp_call(client, t['a'], 'library_traverse', {'collection': f'Bravo {MARK_B}', 'topic': 'escalation'})
    assert 'no such collection' in ghost.get('error', '') + str(ghost) and 'no such collection' in str(forbidden)             # not allowed and not there look the same
    assert str(forbidden).replace(f'Bravo {MARK_B}', 'X') == str(forbidden)                                                    # and the name is not echoed back


def test_context_returns_full_pages_per_collection_never_merged(client, library):
    r = mcp_call(client, library['toks']['ab'], 'library_context', {'query': 'refund thirty days'})
    usage_everywhere(r)
    assert r['answerable'] is True and r['meta']['topics_found'] >= 2 and r['meta']['total_tokens'] > 0
    blocks = {b['collection']: b for b in r['results']}
    assert set(blocks) == {f'Alpha {MARK_A}', f'Bravo {MARK_B}'} and blocks[f'Bravo {MARK_B}']['usage'] == 'internal'
    top = blocks[f'Alpha {MARK_A}']['topics'][0]
    assert top['topic'] == 'refund-policy' and top['markdown'].startswith('# Refund policy') and top['link'].endswith(blocks[f'Alpha {MARK_A}']['collection_id'])   # the FULL page
    # citations are short references with one legend per collection, and nothing is lost: each reference names a real source of the collection
    assert '[source:' not in top['markdown'] and '[s1]' in top['markdown'] and 'context_chunks' not in top
    legend = blocks[f'Alpha {MARK_A}']['sources']
    assert [x['ref'] for x in legend] == ['s1'] and legend[0]['name'] == 'a.md' and legend[0]['link'] == f"/library/sources/{legend[0]['source_id']}?c={blocks[f'Alpha {MARK_A}']['collection_id']}"
    rel = blocks[f'Alpha {MARK_A}']['related']                                                # linked topics not already returned in full, and every typed link
    assert 'refund-policy' not in {n['topic'] for n in rel['nodes']} and {e['relation'] for e in rel['edges']} == {'governs', 'related_to'}
    raw = json.dumps(r)                                                                          # the text an MCP client receives is compact JSON
    t = mcp_text(client, library['toks']['ab'], 'library_context', {'query': 'refund thirty days'})
    assert '\n  ' not in t and ', "' not in t and json.loads(t) == json.loads(raw)
    assert not any(MARK_B in json.dumps(b) for n, b in blocks.items() if n.startswith('Alpha'))
    scoped = mcp_call(client, library['toks']['ab'], 'library_context', {'query': 'refund', 'collection': f'Bravo {MARK_B}'})
    assert [b['collection'] for b in scoped['results']] == [f'Bravo {MARK_B}']
    from studio import settings
    settings.put('LIBRARY_MIN_SIMILARITY', 0.99, 't')                                                             # nothing is that close to a nonsense word
    try:
        none = mcp_call(client, library['toks']['a'], 'library_context', {'query': 'zzqqxx'})
    finally:
        settings.reset('LIBRARY_MIN_SIMILARITY', 't')
    assert none['answerable'] is False and none['results'] == [] and 'do not answer from your own knowledge' in none['note']


def test_search_is_lightweight_and_topic_claims_sources_and_traverse_give_provenance_and_graph(client, library):
    tok, col = library['toks']['a'], f'Alpha {MARK_A}'
    s = mcp_call(client, tok, 'library_search', {'query': 'refund thirty days'})['results'][0]['topics']
    assert s[0]['topic'] == 'refund-policy' and 'markdown' not in s[0] and s[0]['top_chunk']['text']
    t = mcp_call(client, tok, 'library_topic', {'collection': 'alpha-marker-alpha-7731', 'topic': 'Cancellation policy'})          # by slug of the collection, by alias of the topic
    usage_everywhere(t)
    assert t['topic'] == 'refund-policy' and t['markdown'].startswith('# Refund policy') and t['sources'][0]['name'] == 'a.md' and t['sources'][0]['wiki_version'] == 1
    assert [(r['relation'], r['topic']) for r in t['related']] == [('governs', 'premium-plan'), ('related_to', 'premium-plan')]
    cl = mcp_call(client, tok, 'library_topic_claims', {'collection': col, 'topic': 'refund-policy'})
    first = cl['claims'][0]
    assert first['claim'] == 'A yearly plan is refundable within thirty days.' and first['sources'][0]['quote'].startswith('Yearly plans')
    assert first['sources'][0]['wiki_version'] == 1 and first['sources'][0]['wiki_passage'] == 0 and first['sources'][0]['name'] == 'a.md'
    src = mcp_call(client, tok, 'library_topic_sources', {'collection': col, 'topic': 'refund-policy'})['sources'][0]
    assert src['name'] == 'a.md' and src['wiki_version'] == 1 and src['wiki_is_current'] is True and src['link'].startswith('/library/sources/')
    tr = mcp_call(client, tok, 'library_traverse', {'collection': col, 'topic': ['refund-policy']})
    usage_everywhere(tr)
    assert {x['topic'] for x in tr['topics']} == {'refund-policy', 'premium-plan'} and {l['relation'] for l in tr['links']} == {'governs', 'related_to'}
    only = mcp_call(client, tok, 'library_traverse', {'collection': col, 'topic': 'refund-policy', 'relations': ['depends_on']})
    assert [x['topic'] for x in only['topics']] == ['refund-policy'] and only['links'] == []
    assert 'error' in mcp_call(client, tok, 'library_topic', {'collection': col, 'topic': 'nothing-like-it'})
    v = mcp_call(client, tok, 'library_vocabulary', {'collection': col})['results'][0]
    assert any(t['name'] == 'Policy' for t in v['topic_types']) and any(r['name'] == 'governs' and r['inverse'] == 'governed_by' for r in v['relation_types'])


def test_source_wikis_are_searchable_and_readable_and_stay_in_their_collection(client, library):
    tok, ab, col = library['toks']['a'], library['toks']['ab'], f'Alpha {MARK_A}'
    found = mcp_call(client, tok, 'library_source_search', {'query': 'yearly plans refundable'})
    usage_everywhere(found)
    hit = found['results'][0]['sources'][0]
    assert hit['name'] == 'a.md' and hit['wiki_version'] == 1 and 'thirty days' in hit['matched'] and MARK_B not in json.dumps(found)
    whole = mcp_call(client, tok, 'library_source', {'collection': col, 'source': hit['source_id']})
    assert whole['view'] == 'wiki' and whole['wiki_version'] == 1 and whole['markdown'].startswith('# Refunds') and whole['truncated'] is False
    q = mcp_call(client, tok, 'library_source', {'collection': col, 'source': 'A.MD', 'query': 'premium seats'})                     # by exact file name, any case
    assert q['passages'] and q['passages'][0]['match'] is True and 'seats' in json.dumps(q['passages'])
    orig = mcp_call(client, tok, 'library_source', {'collection': col, 'source': hit['source_id'], 'original': True, 'query': 'refund'})
    assert orig['view'] == 'original' and 'Refund rules' in json.dumps(orig['passages']) and 'not reviewed' in orig['note']
    assert 'no document' in mcp_call(client, tok, 'library_source', {'collection': col, 'source': 'nothing.pdf'}).get('error', '')
    b_src = mcp_call(client, ab, 'library_topic_sources', {'collection': f'Bravo {MARK_B}', 'topic': 'escalation'})['sources'][0]['source_id']
    r = mcp_call(client, tok, 'library_source', {'collection': f'Bravo {MARK_B}', 'source': b_src})
    assert 'no such collection' in r.get('error', '') and MARK_B not in json.dumps(r).replace(f'Bravo {MARK_B}', '')              # not allowed looks like not there
    r = mcp_call(client, tok, 'library_source', {'collection': col, 'source': b_src})                                             # B's id asked inside A's collection
    assert 'no document' in r.get('error', '') and MARK_B not in json.dumps(r)


def test_reading_inside_a_long_wiki_returns_matches_with_neighbours_within_a_budget(client, library, monkeypatch):
    col = library['a']['id']
    with db.conn() as c:
        sid = c.execute("INSERT INTO library.source (collection_id, name, sha256, storage_key, wiki_status) VALUES (%s, 'long.md', 'longdoc', 'x', 'approved') RETURNING id", (col,)).fetchone()['id']
    from studio.library import index
    md = '# Long\n\n' + '\n\n'.join(f'## Part {i}\n\nPart {i}. ' + ('zebracrossing tariff ' if i == 5 else 'filler words ') + 'x ' * 900 for i in range(10))
    from studio.library import wiki as W
    W.save_version(col, sid, md, 'written')
    args = {'collection': f'Alpha {MARK_A}', 'source': str(sid)}
    with db.conn() as c:
        hit = c.execute("SELECT idx FROM library.wiki_chunk WHERE source_id=%s AND text LIKE '%%zebracrossing%%'", (sid,)).fetchone()['idx']
    r = mcp_call(client, library['toks']['a'], 'library_source', {**args, 'query': 'zebracrossing tariff'})
    got = [(p['passage'], p['match']) for p in r['passages']]
    assert (hit, True) in got and (hit - 1, False) in got and (hit + 1, False) in got
    monkeypatch.setattr(agent, 'READ_CHARS', 1000)
    tight = mcp_call(client, library['toks']['a'], 'library_source', {**args, 'around': hit, 'window': 3})
    assert [p['passage'] for p in tight['passages']] == [hit] and tight['passages'][0].get('cut') and tight['not_included']    # one passage over the budget is cut, never dropped


def test_alfred_gets_chips_and_shrinks_long_results_in_layers(client, library):
    from studio import alfred
    acc = {}
    alfred._cites('library_source', {'link': '/library/sources/s?c=x', 'name': 'a.md'}, acc)
    alfred._cites('library_topic', {'link': '/library/topics/refund-policy?c=x', 'title': 'Refund policy'}, acc)
    alfred._cites('library_context', {'results': [{'topics': [{'link': '/library/topics/premium-plan?c=x', 'title': 'Premium plan'}]}]}, acc)
    alfred._cites('library_source', {'error': 'no document'}, acc)
    assert acc == {'/library/sources/s?c=x': 'a.md', '/library/topics/refund-policy?c=x': 'Refund policy', '/library/topics/premium-plan?c=x': 'Premium plan'}
    big = {'results': [{'topics': [{'topic': f't{i}', 'markdown': 'm' * 300} for i in range(6)],
                        'related': {'nodes': [{'topic': 'n', 'summary': 's' * 400}] * 8, 'edges': []}}]}
    out = json.loads(alfred.fit_result(big, 3500))
    assert all(t['markdown'] == 'm' * 300 for t in out['results'][0]['topics']) and all('summary' not in n for n in out['results'][0]['related']['nodes'])   # the pages stay
    assert 'related topics' in out['left_out'][0]
    assert alfred.fit_result({'a': 1}, 1000) == '{"a":1}'
    assert alfred.fit_result({'k': 'y' * 5000}, 500).endswith('[truncated: ask for something narrower]')


def test_a_collection_can_keep_agents_out_entirely(client, library):
    a = library['a']['id']
    _as(client, 'ann')
    assert client.patch(f'/api/library/collections/{a}', json={'agents_may_read': False}).status_code == 200
    assert mcp_call(client, library['toks']['a'], 'library_collections')['collections'] == []
    r = mcp_call(client, library['toks']['a'], 'library_context', {'query': 'refund'})
    assert r['results'] == [] and r['answerable'] is False
    assert 'library_context' not in [t['name'] for t in _rpc(client, library['toks']['a'], 'tools/list')['tools']]                                  # nothing readable: the tools are not even offered
    assert client.get(f'/api/library/collections/{a}/topics').status_code == 200                                                                    # people still read it in the UI
    assert [c['collection'] for c in mcp_call(client, library['toks']['ab'], 'library_collections')['collections']] == [f'Bravo {MARK_B}']


def test_alfred_reads_for_the_signed_in_user_and_only_where_ai_is_allowed(client, library):
    a, b = library['a']['id'], library['b']['id']
    ann = S.for_user({'username': 'ann', 'role': 'viewer', 'extra_roles': ['editor']})
    agent.set_principal(ann, 'alfred')
    assert [c['collection'] for c in tools.call('library_collections', {})['collections']] == [f'Alpha {MARK_A}']                                    # ann holds A only
    r = tools.call('library_context', {'query': 'refund thirty days'})
    assert MARK_B not in json.dumps(r) and r['results'][0]['usage'] == 'public' and r['answerable']
    _as(client, 'ann'); client.patch(f'/api/library/collections/{a}', json={'allow_ai': False})
    agent.set_principal(ann, 'alfred')
    assert tools.call('library_collections', {})['collections'] == []                                                                                # a model must not see documents that forbid AI
    agent.set_principal(ann, 'agent')
    assert len(tools.call('library_collections', {})['collections']) == 1                                                                           # an MCP client's own model is the token holder's decision
    agent.set_principal(S.for_user({'username': 'eve', 'role': 'viewer', 'extra_roles': []}), 'alfred')
    out = tools.call('library_context', {'query': 'refund'})
    assert out['results'] == [] and out['answerable'] is False and not agent.available()
    agent.CURRENT.set(None)
    assert 'not available' in tools.call('library_collections', {}).get('error', '')


def test_an_optional_reranker_reorders_topics_and_its_failure_changes_nothing(client, library, monkeypatch):
    base = mcp_call(client, library['toks']['a'], 'library_search', {'query': 'refund thirty days premium plan'})['results'][0]['topics']
    assert len(base) == 2
    from studio import settings
    settings.put('RERANK_URL', 'http://rerank.invalid/v1/rerank', 't')
    try:
        monkeypatch.setattr(rerank, 'rerank', lambda q, docs, top_n=None: list(reversed(range(len(docs)))))
        flipped = mcp_call(client, library['toks']['a'], 'library_search', {'query': 'refund thirty days premium plan'})['results'][0]['topics']
        assert [t['topic'] for t in flipped] == [t['topic'] for t in reversed(base)]
        monkeypatch.setattr(rerank, 'rerank', lambda q, docs, top_n=None: None)
        assert [t['topic'] for t in mcp_call(client, library['toks']['a'], 'library_search', {'query': 'refund thirty days premium plan'})['results'][0]['topics']] == [t['topic'] for t in base]
    finally:
        settings.reset('RERANK_URL', 't')
    assert rerank.rerank('q', ['a', 'b']) is None                                                                   # no endpoint configured


def test_the_graph_endpoint_stays_inside_the_collection(client, library):
    a, b = library['a']['id'], library['b']['id']
    _as(client, 'bob')
    g = client.get(f'/api/library/collections/{a}/graph').json()
    assert {n['key'] for n in g['nodes']} == {'refund-policy', 'premium-plan'} and {(e['from'], e['to']) for e in g['edges']} == {('refund-policy', 'premium-plan')} and MARK_B not in json.dumps(g)
    assert client.get(f'/api/library/collections/{b}/graph').status_code == 404


def test_an_external_agent_is_told_how_to_read_a_question_and_what_to_do_next(client, library):
    tok, none = library['toks']['a'], library['toks']['none']
    ins = _rpc(client, tok, 'initialize', {'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '1'}})['instructions']
    for needle in ('Understand the words', 'Route', 'BOTH', "topic's `model` list", 'library_context', 'answerable', 'never quote it', 'ONE statement'):
        assert needle in ins, needle
    assert len(ins) < 12000
    prompts = {p['name'] for p in _rpc(client, tok, 'prompts/list')['prompts']}
    assert prompts >= {'explore_model', 'data_question', 'documents_question', 'mixed_question'}
    descs = {t['name']: t['description'] for t in _rpc(client, tok, 'tools/list')['tools']}
    assert 'ONCE, first' in descs['get_access'] and 'never assume' in descs['describe'] and 'library_context first' in descs['plan_query']
    assert 'START HERE' in descs['library_context'] and 'policy, procedure, product' in descs['library_context']
    ctx = mcp_call(client, tok, 'library_context', {'query': 'refund thirty days'})
    assert 'library_topic_claims' in ctx['next'] and 'plan_query' in ctx['next']
    assert 'library_source' in mcp_call(client, tok, 'library_topic_claims', {'collection': f'Alpha {MARK_A}', 'topic': 'refund-policy'})['next']
    # the model tools point at the documents only for a credential that has them
    with_docs = mcp_call(client, tok, 'describe', {'name': 'zzqqx nothing like it'})
    without = mcp_call(client, none, 'describe', {'name': 'zzqqx nothing like it'})
    assert 'library_context' in with_docs['next']                                          # found in the documents, or pointed at them
    assert 'library_context' not in without['next'] and 'not known' in without['next'] and 'found_in_documents' not in without
    assert 'library_context' not in mcp_call(client, none, 'search', {'query': 'zzqqx nothing like it'}).get('next', '')


def _everything(x):
    """Every key and every string value anywhere in a result."""
    if isinstance(x, dict):
        for k, v in x.items():
            yield k
            yield from _everything(v)
    elif isinstance(x, list):
        for v in x:
            yield from _everything(v)
    elif isinstance(x, str):
        yield x


def test_a_credential_without_sources_gets_the_content_and_no_citation_apparatus(client, library):
    """The switch is per credential: the same tools, the same pages, without markers, source lists, links into the app and collection ids."""
    quiet, normal, col = library['toks']['quiet'], library['toks']['a'], f'Alpha {MARK_A}'
    # the tool list: one tool goes (documents behind a topic), the descriptions stop talking about sources
    tools_q = {x['name']: x for x in _rpc(client, quiet, 'tools/list')['tools']}
    tools_n = {x['name']: x for x in _rpc(client, normal, 'tools/list')['tools']}
    assert set(tools_n) - set(tools_q) == {'library_topic_sources'}
    from studio import mcp_server
    for name, old, _new in mcp_server.WITHOUT_SOURCES:                                   # every replacement still finds its text: a reworded description must fail here, not silently keep the old words
        assert old in ' '.join(tools_n[name]['description'].split()), (name, old)
    for name, prop, old, _new in mcp_server.WITHOUT_SOURCES_PARAMS:
        assert old in tools_n[name]['inputSchema']['properties'][prop]['description'], (name, prop)
    for name, x in tools_q.items():
        assert '[s1]' not in x['description'] and 'library_topic_sources' not in json.dumps(x), name
    assert '[s1]' in tools_n['library_context']['description'] and 'library_topic_sources' in tools_n['library_context']['description']      # the switch is per credential: nothing leaked across
    assert 'wiki_passage' in json.dumps(tools_n['library_source']['inputSchema']) and 'wiki_passage' not in json.dumps(tools_q['library_source']['inputSchema'])
    # a page keeps its words and loses its markers
    q = mcp_call(client, quiet, 'library_context', {'query': 'refund thirty days'})
    n = mcp_call(client, normal, 'library_context', {'query': 'refund thirty days'})
    block, nblock = q['results'][0], n['results'][0]
    assert 'sources' not in block and 'sources' in nblock and 'collection_id' not in block and 'collection_id' in nblock
    md_q, md_n = block['topics'][0]['markdown'], nblock['topics'][0]['markdown']
    assert '[s1]' in md_n and '[s' not in md_q and '[source:' not in md_q and md_q.count('thirty days') == md_n.count('thirty days')
    assert 'link' not in set(_everything(q)) and 'link' in set(_everything(n))
    assert ' .' not in md_q and ' ,' not in md_q                                                    # no gap left where a marker stood
    # topic, claims, sources
    t = mcp_call(client, quiet, 'library_topic', {'collection': col, 'topic': 'refund-policy'})
    assert 'sources' not in t and 'link' not in t and 'library_topic_sources' not in t['next'] and t['markdown'].startswith('# Refund policy') and '[s' not in t['markdown']
    cl = mcp_call(client, quiet, 'library_topic_claims', {'collection': col, 'topic': 'refund-policy'})
    first = cl['claims'][0]
    assert first['claim'] == 'A yearly plan is refundable within thirty days.' and first['quotes'][0].startswith('Yearly plans') and 'sources' not in first and 'link' not in set(_everything(cl))
    assert 'error' in mcp_call(client, quiet, 'library_topic_sources', {'collection': col, 'topic': 'refund-policy'})
    # the content tools still work, without the references: a search hit carries what library_source needs
    s = mcp_call(client, quiet, 'library_source_search', {'query': 'refundable thirty days'})
    hit = s['results'][0]['sources'][0]
    assert hit['source_id'] and hit['name'] == 'a.md' and 'link' not in hit and hit['matched']
    page = mcp_call(client, quiet, 'library_source', {'collection': col, 'source': hit['source_id'], 'query': 'refundable'})
    assert page['passages'] and 'link' not in page and 'url' not in page and 'collection_id' not in page
    assert 'collection_id' not in json.dumps(mcp_call(client, quiet, 'library_collections')) and 'link' not in set(_everything(mcp_call(client, quiet, 'library_search', {'query': 'refund'})))
    # the model tools that point at documents carry no paths into the app either
    sc = mcp_call(client, quiet, 'get_access')
    assert 'sources' in sc['meaning'] and 'off' in sc['meaning']['sources'] and 'sources' not in sc['entitlements']
    assert 'sources' not in mcp_call(client, normal, 'get_access')['meaning']
    # Alfred is a different credential: the signed-in user's own switch, on by default
    from studio import access
    access.set_caller('ann', {'data': True}, user=True)
    assert access.shows_sources() is True
    access.set_caller('ann', {'data': True, 'sources': False}, user=True)
    assert access.shows_sources() is False
    access.set_caller('ann', None, user=True)


def test_the_sources_switch_is_set_with_the_other_entitlements(client, library):
    from studio import access
    assert access.normalize(auth.api_token(library['toks']['quiet'])['entitlements'])['sources'] is False
    assert access.normalize(auth.api_token(library['toks']['a'])['entitlements'])['sources'] is True
    assert access.normalize({})['sources'] is True and access.normalize({'sources': False})['sources'] is False
    assert access.normalize_user({'sources': False})['sources'] is False and access.FULL['sources'] is True
