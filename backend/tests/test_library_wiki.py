import json
import pytest
from studio import db, embeddings, llm, settings
from studio.library import jobs, scope as S, wiki
from test_library_access import world, _as, _admin, MARK_A, MARK_B   # noqa: F401
from test_library_storage_jobs import _upload

DOC = b'# Refund policy\n\nA yearly subscription can be cancelled within thirty days for a full refund. Monthly plans can be cancelled at any time and end with the billing period.'


class Fake:
    """A scripted stand-in for the models: the writer answers from `pages`, the checker from `verdicts`."""
    def __init__(self, verdicts, pages=('# Refunds\n\nYearly plans are refundable within thirty days.',), amended='# Refunds\n\nYearly plans are fully refundable within thirty days of purchase.'):
        self.verdicts, self.pages, self.amended, self.log, self.tool_results = list(verdicts), list(pages), amended, [], []

    def chat(self, messages, role='assistant', **kw):
        self.log.append(('chat', role, messages[0]['content'][:40]))
        p = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        if isinstance(p, Exception):
            raise p
        return 'Here is the page:\n\n' + p                                         # a preamble the code must strip

    def chat_tools(self, messages, tools, run_tool, role='assistant', **kw):
        self.log.append(('tools', role, messages[0]['content'][:40]))
        if role == 'verifier':
            if messages[-1]['role'] == 'user' and messages[-1]['content'] == wiki.REVIEW:                  # the self-review: genuine issues are kept
                return messages[-2]['content'], 0
            self.tool_results.append(run_tool('semantic_search_source', {'query': 'refund thirty days'}))
            self.tool_results.append(run_tool('list_source_outline', {}))
            v = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
            return (v if isinstance(v, str) else json.dumps(v)), 2
        return 'Sure.\n\n' + self.amended, 0


FAIL = {'verdict': 'FAIL', 'issues': [{'wiki_text': 'refundable', 'type': 'MISSING_QUALIFIER', 'reason': 'full refund, yearly only', 'evidence': ['A yearly subscription can be cancelled within thirty days for a full refund.']}]}
PASS = {'verdict': 'PASS', 'issues': []}


@pytest.fixture
def ai(monkeypatch):
    def use(fake):
        monkeypatch.setattr(llm, 'available', lambda: True)
        if embeddings.available.__module__ == 'studio.embeddings':               # keywords only (no embedding service in tests), unless a test already faked one
            monkeypatch.setattr(embeddings, 'available', lambda: False)
        monkeypatch.setattr(llm, 'chat', fake.chat)
        monkeypatch.setattr(llm, 'chat_tools', fake.chat_tools)
        return fake
    return use


def _run():
    while jobs.run_one('t'):
        pass


def _run_only(sid):
    """Process just this source's jobs (the others stay queued)."""
    with db.conn() as c:
        c.execute("UPDATE library.job SET run_after = now() + interval '1 hour' WHERE status='queued' AND source_id <> %s", (sid,))
    _run()
    with db.conn() as c:
        c.execute("UPDATE library.job SET run_after = now() WHERE status='queued'")


def _src(client, a):
    return client.get(f'/api/library/collections/{a}/sources').json()['sources'][0]


def _wiki(client, a, sid, **p):
    return client.get(f'/api/library/collections/{a}/sources/{sid}/wiki', params=p).json()


def test_a_page_that_passes_is_approved_and_the_checker_only_sees_its_own_document(client, world, ai):
    a, b = world['a']['id'], world['b']['id']
    f = ai(Fake([PASS]))
    _as(client, 'ann'); sid = _upload(client, a, 'policy.md', DOC).json()['id']
    _upload(client, a, 'other.md', b'# Other\n\nThe canteen opens at noon and serves soup.')              # in the same collection, uploaded before the first document is checked
    _run_only(sid)
    s = [x for x in client.get(f'/api/library/collections/{a}/sources').json()['sources'] if x['id'] == sid][0]
    assert s['status'] == 'ready' and s['wiki_status'] == 'approved'
    w = _wiki(client, a, sid)
    assert w['version'] == 1 and w['markdown'].startswith('# Refunds') and 'Here is the page' not in w['markdown']          # the preamble was stripped
    assert [(c['version'], c['round'], c['verdict']) for c in w['checks']] == [(1, 0, 'PASS')] and w['checks'][0]['tool_calls'] == 2
    seen = ' '.join(f.tool_results)
    assert 'thirty days' in seen and 'canteen' not in seen and 'soup' not in seen                                           # the other document of the same collection is out of reach


def test_the_page_is_corrected_until_it_passes_and_every_version_is_kept(client, world, ai):
    a = world['a']['id']
    ai(Fake([FAIL, PASS]))
    _as(client, 'ann'); sid = _upload(client, a, 'policy.md', DOC).json()['id']
    _run()
    assert _src(client, a)['wiki_status'] == 'approved'
    w = _wiki(client, a, sid)
    assert w['version'] == 2 and 'fully refundable' in w['markdown'] and [v['origin'] for v in w['versions']] == ['amended', 'written']
    assert [(c['version'], c['round'], c['verdict']) for c in w['checks']] == [(1, 0, 'FAIL'), (2, 1, 'PASS')]
    assert w['checks'][0]['issues'][0]['type'] == 'MISSING_QUALIFIER'
    assert 'Yearly plans are refundable' in _wiki(client, a, sid, version=1)['markdown']                                    # the first version is still readable


def test_a_page_that_never_passes_goes_to_a_person_who_can_approve_it(client, world, ai):
    a = world['a']['id']
    settings.put('MAX_AMENDMENT_ROUNDS', 1, 't')
    try:
        ai(Fake([FAIL]))
        _as(client, 'ann'); sid = _upload(client, a, 'policy.md', DOC).json()['id']
        _run()
    finally:
        settings.reset('MAX_AMENDMENT_ROUNDS', 't')
    assert _src(client, a)['wiki_status'] == 'needs_review'
    w = _wiki(client, a, sid)
    assert w['version'] == 2 and [c['verdict'] for c in w['checks']] == ['FAIL', 'FAIL']
    _as(client, 'bob')                                                                       # may read the page, not decide on it
    assert _wiki(client, a, sid)['markdown'] and client.post(f'/api/library/collections/{a}/sources/{sid}/wiki/approve').status_code == 403
    _as(client, 'ann')
    assert client.post(f'/api/library/collections/{a}/sources/{sid}/wiki/approve').status_code == 200
    assert _src(client, a)['wiki_status'] == 'approved'
    assert client.post(f'/api/library/collections/{a}/sources/{sid}/wiki/approve').status_code == 409
    _admin(client)
    assert 'approve_wiki' in {r['action'] for r in client.get('/api/admin/audit?kind=collection').json()}


def test_a_checker_that_returns_nonsense_sends_the_page_to_review(client, world, ai):
    a = world['a']['id']
    ai(Fake(['I think it looks fine!']))
    _as(client, 'ann'); sid = _upload(client, a, 'policy.md', DOC).json()['id']
    _run()
    assert _src(client, a)['wiki_status'] == 'needs_review' and _wiki(client, a, sid)['checks'][0]['verdict'] == 'ERROR'


def test_a_collection_that_forbids_ai_never_reaches_a_model(client, world, ai, monkeypatch):
    a = world['a']['id']
    called = []
    fake = Fake([PASS]); ai(fake)
    monkeypatch.setattr(llm, 'chat', lambda *x, **k: called.append(1)); monkeypatch.setattr(llm, 'chat_tools', lambda *x, **k: called.append(1))
    _as(client, 'ann')
    assert client.patch(f'/api/library/collections/{a}', json={'allow_ai': False}).status_code == 200
    sid = _upload(client, a, 'policy.md', DOC).json()['id']
    _run()
    s = _src(client, a)
    assert called == [] and s['status'] == 'ready' and s['wiki_status'] == 'skipped' and any('does not allow AI' in n for n in s['stats']['notes'])
    assert client.get(f'/api/library/collections/{a}/search', params={'q': 'refund'}).json()['passages']                    # still searchable
    assert client.post(f'/api/library/collections/{a}/sources/{sid}/wiki/regenerate').status_code == 422


def test_a_failed_writer_leaves_the_document_searchable_and_it_can_be_regenerated(client, world, ai):
    a = world['a']['id']
    f = ai(Fake([PASS], pages=[RuntimeError('model unavailable')]))
    _as(client, 'ann'); sid = _upload(client, a, 'policy.md', DOC).json()['id']
    _run()
    s = _src(client, a)
    assert s['status'] == 'ready' and s['wiki_status'] == 'failed' and any('model unavailable' in n for n in s['stats']['notes'])
    f.pages[:] = ['# Refunds\n\nYearly plans are refundable within thirty days.']
    assert client.post(f'/api/library/collections/{a}/sources/{sid}/wiki/regenerate').status_code == 200
    _run()
    assert _src(client, a)['wiki_status'] == 'approved' and _wiki(client, a, sid)['version'] == 1


def test_a_long_document_is_written_section_by_section(client, world, ai):
    a = world['a']['id']
    f = ai(Fake([PASS], pages=['## Part\n\nFacts.']))
    settings.put('WRITER_SECTION_TOKENS', 1000, 't')
    try:
        long = '\n\n'.join(f'Paragraph {i}: ' + 'The plan includes item number %d with a stated limit. ' % i * 8 for i in range(30)).encode()
        _as(client, 'ann'); sid = _upload(client, a, 'long.txt', long).json()['id']
        _run()
    finally:
        settings.reset('WRITER_SECTION_TOKENS', 't')
    writes = [x for x in f.log if x[0] == 'chat']
    assert len(writes) > 2 and all(x[2] == open(wiki.PROMPTS / 'writer_section.md').read()[:40] for x in writes)       # the section writer
    md = _wiki(client, a, sid)['markdown']
    assert md.startswith('# long') and md.count('## Part') == len(writes) and md.count('\n---\n') == len(writes) - 1        # sections joined with a rule


def test_the_models_tools_are_confined_to_one_source(client, world):
    a, b = world['a']['id'], world['b']['id']
    with db.conn() as c:
        c.execute("INSERT INTO library.grant_ (collection_id, username, level) VALUES (%s,'ed','write') ON CONFLICT DO NOTHING", (b,))
    _as(client, 'ann'); one = _upload(client, a, 'one.txt', b'Penguins live in the south.').json()['id']; two = _upload(client, a, 'two.txt', b'Penguins eat fish and krill.').json()['id']
    _as(client, 'ed'); other = _upload(client, b, 'b.txt', f'Penguins are birds {MARK_B}'.encode()).json()['id']
    _run()
    t = wiki.SourceTools(a, one)
    hits = json.loads(t('semantic_search_source', {'query': 'penguins'}))['anchors']
    assert hits and all('south' in h['snippet'] for h in hits)                                        # not 'fish and krill' (same collection), not B's text
    window = json.loads(t('read_source_window', {'chunk_index': 0, 'before': 4, 'after': 4}))
    assert 'krill' not in json.dumps(window) and MARK_B not in json.dumps(window)
    assert MARK_B not in t('list_source_outline', {}) and 'krill' not in t('list_source_outline', {})
    assert json.loads(wiki.SourceTools(a, other)('semantic_search_source', {'query': 'penguins'}))['anchors'] == []   # B's source id under A's collection finds nothing
    with pytest.raises(ValueError):
        t('drop_table', {})


# ------------------------------------------------------------------ a person edits, downloads, review counter
def test_a_persons_edit_is_checked_and_approved_when_it_passes(client, world, ai):
    a = world['a']['id']
    ai(Fake([FAIL, FAIL, PASS]))        # two checks while the page is written and corrected, then one for the person's edit
    settings.put('MAX_AMENDMENT_ROUNDS', 1, 't')
    try:
        _as(client, 'ann'); sid = _upload(client, a, 'policy.md', DOC).json()['id']
        _run()
    finally:
        settings.reset('MAX_AMENDMENT_ROUNDS', 't')
    assert _src(client, a)['wiki_status'] == 'needs_review' and client.get('/api/status').json()['library_review'] == 1
    r = client.put(f'/api/library/collections/{a}/sources/{sid}/wiki', json={'markdown': '# Refunds\n\nYearly plans are fully refundable within thirty days.'})
    assert r.status_code == 200 and r.json() == {'version': 3, 'checking': True}
    assert _src(client, a)['wiki_status'] == 'needs_review'                                  # until the check has run
    _run()
    w = _wiki(client, a, sid)
    assert _src(client, a)['wiki_status'] == 'approved' and w['version'] == 3 and w['versions'][0]['origin'] == 'edited' and w['versions'][0]['created_by'] == 'ann'
    assert w['checks'][-1]['version'] == 3 and w['checks'][-1]['verdict'] == 'PASS'
    assert client.get('/api/status').json()['library_review'] == 0
    assert client.put(f'/api/library/collections/{a}/sources/{sid}/wiki', json={'markdown': '   '}).status_code == 422
    _as(client, 'bob')                                                                       # read access: no edit, no counter
    assert client.put(f'/api/library/collections/{a}/sources/{sid}/wiki', json={'markdown': 'x'}).status_code == 403
    assert client.get('/api/status').json()['library_review'] == 0


def test_an_edit_in_a_collection_without_ai_is_the_persons_responsibility(client, world, ai):
    a = world['a']['id']
    ai(Fake([PASS]))
    _as(client, 'ann'); client.patch(f'/api/library/collections/{a}', json={'allow_ai': False})
    sid = _upload(client, a, 'policy.md', DOC).json()['id']
    _run()
    assert _src(client, a)['wiki_status'] == 'skipped'
    assert client.put(f'/api/library/collections/{a}/sources/{sid}/wiki', json={'markdown': '# Written by hand\n\nText.'}).json()['checking'] is False
    assert _src(client, a)['wiki_status'] == 'approved' and _wiki(client, a, sid)['markdown'].startswith('# Written by hand')


def test_the_original_and_the_page_can_be_downloaded_by_readers_only(client, world, ai):
    a, b = world['a']['id'], world['b']['id']
    ai(Fake([PASS]))
    _as(client, 'ann'); sid = _upload(client, a, 'My policy (v2).md', DOC).json()['id']
    _run()
    _as(client, 'bob')                                                                       # read access is enough to download
    r = client.get(f'/api/library/collections/{a}/sources/{sid}/download/original')
    assert r.status_code == 200 and r.content == DOC and 'attachment' in r.headers['content-disposition'] and r.headers['x-content-type-options'] == 'nosniff'
    r = client.get(f'/api/library/collections/{a}/sources/{sid}/download/page')
    assert r.status_code == 200 and r.text.startswith('# Refunds') and r.headers['content-disposition'].endswith('.md"')
    _as(client, 'eve')
    for kind in ('original', 'page'):
        assert client.get(f'/api/library/collections/{a}/sources/{sid}/download/{kind}').status_code == 404
        assert client.get(f'/api/library/collections/{b}/sources/{sid}/download/{kind}').status_code == 404


def test_a_fail_that_names_no_problem_is_asked_for_specifics_then_rechecked_never_amended_blindly(client, world, ai):
    """FAIL with no spans gets one in-conversation retry; still none -> the same version is verified again, no blind amendment."""
    a = world['a']['id']
    empty = {'verdict': 'FAIL', 'unsupported_spans': []}
    f = ai(Fake([empty, empty, PASS]))
    _as(client, 'ann'); sid = _upload(client, a, 'policy.md', DOC).json()['id']
    _run()
    w = _wiki(client, a, sid)
    assert w['source']['wiki_status'] == 'approved' and len(w['versions']) == 1                                   # no amended version was made
    checks = [(c['version'], c['round'], c['verdict']) for c in w['checks']]
    assert sorted(checks) == [(1, 0, 'FAIL'), (1, 1, 'PASS')]
    assert not any(x[1] == 'writer' and x[0] == 'tools' for x in f.log)                                 # the amendment writer never ran


def test_the_checker_reviews_its_own_findings_once_and_drops_the_ones_it_calls_supported(client, world, ai, monkeypatch):
    a = world['a']['id']
    padded = {'verdict': 'FAIL', 'unsupported_spans': [{'wiki_text': 'refundable', 'issue_type': 'OTHER', 'reason': 'This is supported; retained for completeness.', 'evidence_chunks': []}]}
    f = ai(Fake([padded]))
    real_tools = f.chat_tools

    def tools(messages, tools_, run_tool, role='assistant', **kw):
        if role == 'verifier' and messages[-1]['content'] == wiki.REVIEW:
            return json.dumps(PASS), 0                                                                    # on review, the padded entry is dropped
        return real_tools(messages, tools_, run_tool, role, **kw)
    monkeypatch.setattr(llm, 'chat_tools', tools)
    _as(client, 'ann'); sid = _upload(client, a, 'policy.md', DOC).json()['id']
    _run()
    w = _wiki(client, a, sid)
    assert w['source']['wiki_status'] == 'approved' and len(w['versions']) == 1 and [c['verdict'] for c in w['checks']] == ['PASS']
