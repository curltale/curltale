import json, re
import pytest
from studio import db, embeddings, llm
from studio.library import jobs, scope as S, topics, vocab
from test_library_access import world, _as, _admin, MARK_A, MARK_B   # noqa: F401
from test_library_storage_jobs import _upload
from test_library_wiki import Fake, PASS, ai, _run, _run_only, _src, _wiki   # noqa: F401
from test_library_ingest import embedded   # noqa: F401

PAGE = '# Refunds\n\nYearly plans are refundable within thirty days. Monthly plans end with the billing period. The premium plan includes unlimited seats.'
DOC = b'Refund rules. Yearly plans can be cancelled within thirty days for a full refund. Monthly plans end with the billing period. The premium plan includes unlimited seats.'


def analysis(*topics_, proposals=()):
    return {'topics': list(topics_), 'vocabulary_proposals': list(proposals)}


REFUNDS = {'title': 'Refund policy', 'slug': 'refund-policy', 'action': 'create', 'existing_slug': '', 'topic_type': 'Policy', 'summary': 'Rules for cancelling and refunds.', 'aliases': ['Cancellation policy'],
           'claims': [{'claim_text': 'A yearly plan is refundable within thirty days.', 'claim_type': 'rule', 'source_quote': 'Yearly plans are refundable within thirty days'},
                      {'claim_text': 'A monthly plan ends with the billing period.', 'claim_type': 'fact', 'source_quote': 'Monthly plans end with the billing period'},
                      {'claim_text': 'Refunds are paid in cash.', 'claim_type': 'fact', 'source_quote': 'Refunds are paid in cash'}],          # the quote is not on the page: dropped
           'links': [{'target_title': 'Premium plan', 'relation_type': 'governs', 'context': 'The refund rule covers the premium plan.'}, {'target_title': 'Premium plan', 'relation_type': 'rewards', 'context': 'x'}]}
PREMIUM = {'title': 'Premium plan', 'slug': 'premium-plan', 'action': 'create', 'existing_slug': '', 'topic_type': 'Gadget', 'topic_type_fallback': 'Product', 'summary': 'The top plan.',
           'aliases': [], 'claims': [{'claim_text': 'The premium plan includes unlimited seats.', 'claim_type': 'fact', 'source_quote': 'The premium plan includes unlimited seats'}], 'links': []}


class TopicFake(Fake):
    """The analyzer returns the scripted analyses; the topic writer receives the candidate JSON and the full verified source wiki,
    may call its tools, and writes a page citing `[source: <id>]`."""
    def __init__(self, analyses, writer_tools=(), **kw):
        super().__init__([PASS], pages=[PAGE], **kw)
        self.analyses, self.searches, self.writer_inputs, self.writer_tool_results, self.surgical_inputs = list(analyses), [], [], [], []
        self.writer_tools = list(writer_tools)

    def chat_tools(self, messages, tools, run_tool, role='assistant', **kw):
        if role == 'analyzer':
            self.searches.append(json.loads(run_tool('search_topics', {'query': 'refund premium secret'})))
            a = self.analyses.pop(0) if len(self.analyses) > 1 else self.analyses[0]
            return (a if isinstance(a, str) else json.dumps(a)), 1
        if role == 'topic_writer':
            user = messages[1]['content']
            if 'Claims that are no longer attributed' in user:                                  # the surgical rewrite: drop the lines that rest on withdrawn claims
                self.surgical_inputs.append(user)
                page = user.split('Current topic page:\n\n', 1)[1].split('\n\n---\n\n', 1)[0]
                gone = re.findall(r'- claim: (.+)', user)
                return '\n'.join(l for l in page.splitlines() if not any(g in l for g in gone)), 0
            self.writer_inputs.append(user)
            for name, args in self.writer_tools:
                self.writer_tool_results.append(json.loads(run_tool(name, args)))
            if 'WRITER_BREAKS' in user:
                raise RuntimeError('writer down')
            cand = json.loads(user.split('Topic candidate JSON:\n', 1)[1].split('\n\nVerified source wiki evidence:', 1)[0])
            sid = re.search(r'Source id: (\S+)', user).group(1)
            old = user.split('Existing topic page (enrich and amend this', 1)[1].split('\n\n', 1)[1].split('\n\n---', 1)[0] if 'Existing topic page' in user else ''
            keep = [l for l in old.splitlines() if l.startswith('- ')]
            facts = [f"- {c['claim_text']} `[source: {sid}]`" for c in cand['claims']]
            return f"Sure!\n\n# {cand['title']}\n\n## Key Facts\n\n" + '\n'.join(dict.fromkeys(keep + facts)) + f"\n\n## Sources\n\n- `[source: {sid}]`\n", 0
        return super().chat_tools(messages, tools, run_tool, role, **kw)


@pytest.fixture
def tfake(ai):
    return lambda analyses, **kw: ai(TopicFake(analyses, **kw))


def _topics(client, a):
    return {t['slug']: t for t in client.get(f'/api/library/collections/{a}/topics').json()['topics']}


def test_an_approved_source_creates_topics_with_checked_claims_and_cited_pages(client, world, tfake, embedded):
    a = world['a']['id']
    f = tfake([analysis(REFUNDS, PREMIUM)])
    _as(client, 'ann'); sid = _upload(client, a, 'refunds.md', DOC).json()['id']
    _run()
    assert _src(client, a)['wiki_status'] == 'approved' and _src(client, a)['stats']['topics']['created'] == ['refund-policy', 'premium-plan']
    ts = _topics(client, a)
    assert ts['refund-policy']['claims'] == 2 and ts['premium-plan']['topic_type'] == 'Product'                          # the claim without a real quote was dropped; the unknown type fell back
    # the writer gets the candidate AND the full verified source wiki
    assert all('Verified source wiki evidence:' in w and 'Yearly plans are refundable within thirty days' in w and 'Topic candidate JSON:' in w for w in f.writer_inputs)
    t = client.get(f'/api/library/collections/{a}/topics/refund-policy').json()
    assert t['topic']['title'] == 'Refund policy' and t['aliases'] == ['Cancellation policy', 'Refund policy'] and t['version'] == 1
    assert f'`[source: {sid}]`' in t['markdown'] and 'Sure!' not in t['markdown']                                        # sources cited, preamble stripped
    c1 = t['claims'][0]
    assert c1['n'] == 1 and c1['evidence'][0]['source'] == 'refunds.md' and c1['evidence'][0]['quote'].startswith('Yearly plans') and c1['evidence'][0]['source_id'] == sid
    assert c1['evidence'][0]['wiki_version'] == 1 and c1['evidence'][0]['wiki_chunk'] == 0 and 'thirty days' in c1['evidence'][0]['passage']    # the quote's wiki version and passage
    assert [(l['relation'], l['slug']) for l in t['links']] == [('governs', 'premium-plan'), ('related_to', 'premium-plan')]
    assert t['sources'][0]['wiki_version'] == 1
    v = client.get(f'/api/library/collections/{a}/vocabulary').json()
    assert {(p['kind'], p['name'], p['status']) for p in v['proposals']} >= {('topic_type', 'Gadget', 'pending'), ('relation_type', 'rewards', 'pending')}
    assert any(r['name'] == 'Gadget' and r['status'] == 'pending' for r in v['topic_types'])
    assert client.get(f'/api/library/collections/{a}/topics', params={'type': 'Policy'}).json()['total'] == 1
    with db.conn() as c:                                                                                                    # every layer is chunked and embedded
        tid = topics.find(a, 'refund-policy')['id']
        assert c.execute('SELECT count(*) AS n FROM library.wiki_chunk WHERE source_id=%s AND embedding IS NOT NULL', (sid,)).fetchone()['n'] >= 1
        assert c.execute('SELECT count(*) AS n FROM library.topic_chunk WHERE topic_id=%s AND version=1 AND embedding IS NOT NULL', (tid,)).fetchone()['n'] >= 1
        assert c.execute('SELECT embedding IS NOT NULL AS e FROM library.topic WHERE id=%s', (tid,)).fetchone()['e']
        assert c.execute('SELECT count(*) AS n FROM library.topic_link WHERE from_topic=%s AND embedding IS NOT NULL', (tid,)).fetchone()['n'] >= 1


def test_a_second_source_extends_a_topic_and_reading_a_source_again_keeps_claim_numbers(client, world, tfake):
    a = world['a']['id']
    ext = dict(REFUNDS, action='update', existing_slug='refund-policy', claims=[{'claim_text': 'Refund requests must be made in writing.', 'claim_type': 'rule', 'source_quote': 'Yearly plans are refundable within thirty days'}], links=[], aliases=[])
    f = tfake([analysis(REFUNDS), analysis(ext), analysis(REFUNDS)])    # the third is source one read again: it states its two claims once more
    _as(client, 'ann'); first = _upload(client, a, 'one.md', DOC).json()['id']
    _run()
    two = _upload(client, a, 'two.md', DOC + b' Requests must be made in writing.').json()['id']
    _run()
    t = client.get(f'/api/library/collections/{a}/topics/refund-policy').json()
    assert [c['n'] for c in t['claims']] == [1, 2, 3] and t['version'] == 2 and [s['name'] for s in t['sources']] == ['one.md', 'two.md']
    assert 'Existing topic page (enrich and amend this' in f.writer_inputs[-1] and 'A yearly plan is refundable' in f.writer_inputs[-1]      # the old page reaches the writer
    assert f'Source id: {two}' in f.writer_inputs[-1] and 'Verified source wiki evidence:\n\n# Refunds' in f.writer_inputs[-1]   # with the new source's full wiki
    client.post(f'/api/library/collections/{a}/sources/{first}/wiki/regenerate'); _run()                                  # read again: the same claims are re-attached, not duplicated
    t = client.get(f'/api/library/collections/{a}/topics/refund-policy').json()
    assert [c['n'] for c in t['claims']] == [1, 2, 3] and t['version'] == 3
    assert 'Claims from the previous version of this source' in f.writer_inputs[-1]                                       # the writer sees what this source used to say


def test_removing_a_source_withdraws_what_only_it_supported(client, world, tfake):
    a = world['a']['id']
    shared = dict(REFUNDS, claims=[REFUNDS['claims'][0]], links=[], aliases=[])
    only_two = dict(REFUNDS, action='update', existing_slug='refund-policy', claims=[REFUNDS['claims'][0], REFUNDS['claims'][1]], links=[], aliases=[])
    tfake([analysis(shared), analysis(only_two)])
    _as(client, 'ann'); one = _upload(client, a, 'one.md', DOC).json()['id']
    _run()
    two = _upload(client, a, 'two.md', DOC + b' Extra words so that the file differs.').json()['id']
    _run()
    assert client.delete(f'/api/library/collections/{a}/sources/{one}').status_code == 200
    t = client.get(f'/api/library/collections/{a}/topics/refund-policy').json()
    assert [c['n'] for c in t['claims']] == [1, 2] and t['topic']['needs_revision'] is True                                # claim 1 is still supported by 'two', so both stay
    assert client.delete(f'/api/library/collections/{a}/sources/{two}').status_code == 200
    t = client.get(f'/api/library/collections/{a}/topics/refund-policy').json()
    assert t['claims'] == [] and t['topic']['needs_revision'] is True and t['sources'] == []                                # nothing supports it any more: flagged, not silently kept


def test_a_failing_writer_still_leaves_a_page_made_from_the_candidate(client, world, tfake):
    a = world['a']['id']
    tfake([analysis(dict(REFUNDS, title='WRITER_BREAKS topic', slug='wb'))])
    _as(client, 'ann'); _upload(client, a, 'r.md', DOC)
    _run()
    t = client.get(f'/api/library/collections/{a}/topics/wb').json()
    assert t['markdown'].startswith('# WRITER_BREAKS topic') and '## Key Facts' in t['markdown'] and '- A yearly plan is refundable within thirty days. `[source: ' in t['markdown']


def test_an_unusable_analysis_falls_back_to_one_topic_from_the_page(client, world, tfake):
    a = world['a']['id']
    tfake(['no json at all'])
    _as(client, 'ann'); _upload(client, a, 'r.md', DOC)
    _run()
    s = _src(client, a)
    assert s['status'] == 'ready' and s['wiki_status'] == 'approved'
    ts = _topics(client, a)                                                                                                 # the page's title and first paragraph
    assert list(ts) == ['refunds'] and ts['refunds']['claims'] == 1


def test_a_collection_that_forbids_ai_builds_no_topics(client, world, tfake):
    a = world['a']['id']
    tfake([analysis(REFUNDS)])
    _as(client, 'ann'); client.patch(f'/api/library/collections/{a}', json={'allow_ai': False}); _upload(client, a, 'r.md', DOC)
    _run()
    assert _topics(client, a) == {}


def test_every_collection_has_its_own_vocabulary_and_only_its_manager_decides(client, world, tfake):
    a, b = world['a']['id'], world['b']['id']
    tfake([analysis(PREMIUM)])
    _as(client, 'ann'); _upload(client, a, 'r.md', DOC)
    _run()
    va = client.get(f'/api/library/collections/{a}/vocabulary').json()
    assert {r['name'] for r in va['topic_types']} >= {'Topic', 'Policy', 'Product', 'Gadget'}
    pid = next(p['id'] for p in va['proposals'] if p['name'] == 'Gadget')
    _as(client, 'bob')
    assert client.post(f'/api/library/collections/{a}/vocabulary/proposals/{pid}', json={'approve': True}).status_code == 403
    _as(client, 'ann')
    assert client.post(f'/api/library/collections/{a}/vocabulary/proposals/{pid}', json={'approve': True}).status_code == 200
    assert next(r for r in client.get(f'/api/library/collections/{a}/vocabulary').json()['topic_types'] if r['name'] == 'Gadget')['status'] == 'approved'
    assert client.post(f'/api/library/collections/{a}/vocabulary/proposals/{pid}', json={'approve': True}).status_code == 404         # already decided
    with db.conn() as c:
        assert c.execute("SELECT count(*) AS n FROM library.topic_type WHERE collection_id=%s AND name='Gadget'", (b,)).fetchone()['n'] == 0        # B never heard of it
    _admin(client)
    assert 'vocabulary_approved' in {r['action'] for r in client.get('/api/admin/audit?kind=collection').json()}


def test_topics_never_cross_collections(client, world, tfake):
    a, b = world['a']['id'], world['b']['id']
    secret = dict(PREMIUM, title=f'Secret {MARK_B}', slug='secret-b', topic_type='Product', claims=[], summary=f'The {MARK_B} summary about premium refunds')
    f = tfake([analysis(secret), analysis(REFUNDS)])
    with db.conn() as c:
        c.execute("INSERT INTO library.grant_ (collection_id, username, level) VALUES (%s,'ed','write') ON CONFLICT DO NOTHING", (b,))
    _as(client, 'ed'); _upload(client, b, 'b.md', DOC + MARK_B.encode())
    _run()
    assert 'secret-b' in _topics(client, b)
    _as(client, 'ann'); _upload(client, a, 'a.md', DOC)
    _run()
    assert all(MARK_B not in json.dumps(s) for s in f.searches[1:])                                                       # the analyzer working in A never saw B's topic
    assert 'secret-b' not in _topics(client, a)
    assert client.get(f'/api/library/collections/{a}/topics/secret-b').status_code == 404
    assert topics.find(a, 'secret-b') is None and topics.search(a, 'premium refunds secret') and MARK_B not in json.dumps(topics.search(a, f'{MARK_B} premium'))
    _as(client, 'eve')
    assert client.get(f'/api/library/collections/{a}/topics').status_code == 404 and client.get(f'/api/library/collections/{b}/topics/secret-b').status_code == 404


def test_the_starter_vocabulary_is_seeded_for_new_and_older_collections(client, world):
    a = world['a']['id']
    assert len(vocab.get(a)['topic_types']) >= 15
    with db.conn() as c:
        c.execute('DELETE FROM library.topic_type WHERE collection_id=%s', (a,))
    vocab.seed_all()
    assert len(vocab.get(a)['topic_types']) >= 15


def test_the_topic_writer_reads_other_approved_source_wikis_of_its_own_collection_only(client, world, tfake):
    a, b = world['a']['id'], world['b']['id']
    with db.conn() as c:
        c.execute("INSERT INTO library.grant_ (collection_id, username, level) VALUES (%s,'ed','write') ON CONFLICT DO NOTHING", (b,))
    tfake([analysis(dict(REFUNDS, links=[]))])
    _as(client, 'ed'); other_b = _upload(client, b, 'b.md', DOC + MARK_B.encode()).json()['id']
    _as(client, 'ann'); one = _upload(client, a, 'one.md', DOC).json()['id']
    _run()
    f = tfake([analysis(dict(REFUNDS, action='update', existing_slug='refund-policy', links=[]))],
              writer_tools=[('search_source_wikis', {'query': 'yearly plans refundable'}), ('get_source_wiki', {'source_id': one}), ('get_source_wiki', {'source_id': other_b})])
    two = _upload(client, a, 'two.md', DOC + b' Second copy.').json()['id']
    _run()
    found, read_one, read_b = f.writer_tool_results[-3:]
    assert [s['source_id'] for s in found['sources']] == [one] and MARK_B not in json.dumps(found)          # the other approved wiki of A; not the current one, not B's
    assert read_one['markdown'].startswith('# Refunds') and read_one['source_id'] == one
    assert 'error' in read_b and MARK_B not in json.dumps(read_b)                                            # B's source under A's collection: not found
    assert 'already provided' in json.loads(topics.TopicWriterTools(a, two)('get_source_wiki', {'source_id': two}))['error']


def test_a_topic_the_source_no_longer_covers_is_rewritten_surgically_and_archived_when_nothing_supports_it(client, world, tfake):
    a = world['a']['id']
    f = tfake([analysis(dict(REFUNDS, links=[]), dict(PREMIUM, topic_type='Product'))])
    _as(client, 'ann'); one = _upload(client, a, 'one.md', DOC).json()['id']
    _upload(client, a, 'two.md', DOC + b' Also about premium.')
    _run()                                                                                                   # both sources feed both topics
    with db.conn() as c:
        premium = topics.find(a, 'premium-plan')['id']
        assert c.execute('SELECT count(*) AS n FROM library.topic_source WHERE topic_id=%s', (premium,)).fetchone()['n'] == 2
    tfake([analysis(dict(REFUNDS, action='update', existing_slug='refund-policy', links=[]))])
    client.post(f'/api/library/collections/{a}/sources/{one}/wiki/regenerate'); _run_only(one)               # source one is read again and no longer mentions premium
    t = client.get(f'/api/library/collections/{a}/topics/premium-plan').json()
    assert [s['name'] for s in t['sources']] == ['two.md'] and [c['n'] for c in t['claims']] == [1]          # the premium claim is shared with source two: it stays active
    two = [x for x in client.get(f'/api/library/collections/{a}/sources').json()['sources'] if x['name'] == 'two.md'][0]['id']
    client.post(f'/api/library/collections/{a}/sources/{two}/wiki/regenerate'); _run_only(two)               # now nothing mentions premium any more
    assert client.get(f'/api/library/collections/{a}/topics/premium-plan').status_code == 404                # archived (kept in the database, not served)
    with db.conn() as c:
        assert c.execute('SELECT status FROM library.topic WHERE id=%s', (premium,)).fetchone()['status'] == 'archived'
        assert c.execute("SELECT status FROM library.claim WHERE topic_id=%s", (premium,)).fetchone()['status'] == 'superseded'


def test_deleting_a_source_keeps_its_quotes_for_the_lint_pass_which_rewrites_surgically(client, world, tfake):
    a = world['a']['id']
    only_one = dict(REFUNDS, links=[], claims=[REFUNDS['claims'][1]])
    both = dict(REFUNDS, action='update', existing_slug='refund-policy', links=[], claims=[REFUNDS['claims'][0]])
    f = tfake([analysis(only_one), analysis(both)])
    _as(client, 'ann'); one = _upload(client, a, 'one.md', DOC).json()['id']
    _run()
    _upload(client, a, 'two.md', DOC + b' Another file.')
    _run()
    assert client.delete(f'/api/library/collections/{a}/sources/{one}').status_code == 200
    with db.conn() as c:                                                                                     # the quote survives with no source
        r = c.execute("SELECT cl.status, cs.source_id, cs.quote FROM library.claim cl JOIN library.claim_source cs ON cs.claim_id=cl.id WHERE cl.text='A monthly plan ends with the billing period.'").fetchone()
    assert r['status'] == 'superseded' and r['source_id'] is None and r['quote'].startswith('Monthly plans')
    _run()                                                                                                   # the lint pass (topic.revise)
    assert f.surgical_inputs and 'A monthly plan ends with the billing period.' in f.surgical_inputs[-1] and 'quote: Monthly plans' in f.surgical_inputs[-1]
    t = client.get(f'/api/library/collections/{a}/topics/refund-policy').json()
    assert 'monthly plan' not in t['markdown'].lower() and 'yearly plan' in t['markdown'].lower() and t['topic']['needs_revision'] is False
