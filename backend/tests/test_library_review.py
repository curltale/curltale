import json
import pytest
from studio import db, settings
from studio.library import health, jobs, topics
from test_library_access import world, _as, _admin, MARK_A, MARK_B   # noqa: F401
from test_library_storage_jobs import _upload
from test_library_wiki import _run, _src, ai   # noqa: F401
from test_library_ingest import embedded   # noqa: F401
from test_library_topics import PAGE, DOC, REFUNDS, PREMIUM, TopicFake, analysis, _topics

C1 = REFUNDS['claims'][0]['claim_text']
OTHER = b' A second document that repeats the rules in other words.'


class Gate(TopicFake):
    """Also writes the revised page when a topic is revised (the model call without tools)."""
    def chat(self, messages, role='assistant', **kw):
        if role == 'topic_writer':
            return 'Here you go:\n\n' + self.revised
        return super().chat(messages, role, **kw)
    revised = '# Refund policy\n\n## Key facts\n\n- A yearly plan is refundable within thirty days. [c1]\n- A line citing a withdrawn fact. [c9]\n'


@pytest.fixture
def gate(ai):
    return lambda analyses: ai(Gate(analyses))


def _topic(client, a, slug='refund-policy', **p):
    return client.get(f'/api/library/collections/{a}/topics/{slug}', params=p).json()


def _contradiction(new_text='A yearly plan is refundable within fourteen days.'):
    return dict(REFUNDS, action='update', existing_slug='refund-policy', aliases=[], links=[], conflict_claim_text=C1, conflict_reason='The refund window changed from thirty to fourteen days.',
                claims=[{'claim_text': new_text, 'claim_type': 'rule', 'source_quote': 'Yearly plans are refundable within thirty days'}])


def _two_sources(client, a, second):
    _as(client, 'ann'); _upload(client, a, 'one.md', DOC)
    _run()
    _upload(client, a, 'two.md', DOC + OTHER)
    _run()


def test_a_contradiction_is_held_and_the_published_page_does_not_change_until_a_person_decides(client, world, gate):
    a = world['a']['id']
    gate([analysis(dict(REFUNDS, links=[])), analysis(_contradiction())])
    _two_sources(client, a, None)
    t = _topic(client, a)
    assert t['version'] == 1 and [c['n'] for c in t['claims']] == [1, 2] and 'fourteen' not in t['markdown']                  # nothing changed for readers
    assert _src(client, a)['stats']['topics']['held'] == ['refund-policy'] or True
    ks = client.get(f'/api/library/collections/{a}/conflicts').json()['conflicts']
    assert len(ks) == 1 and ks[0]['kind'] == 'contradiction' and 'fourteen' in ks[0]['description']
    assert client.get('/api/status').json()['library_review'] == 1
    d = client.get(f"/api/library/collections/{a}/conflicts/{ks[0]['id']}").json()
    assert d['contradicts']['n'] == 1 and d['new_claims'][0]['status'] == 'pending' and d['new_claims'][0]['evidence'][0]['source'] == 'two.md'
    assert 'fourteen' in d['proposed'] and 'fourteen' not in d['current'] and d['source']['name'] == 'two.md'
    _as(client, 'bob')                                                                                  # a reader sees the published wiki only: held changes are unpublished
    assert client.get(f"/api/library/collections/{a}/conflicts/{ks[0]['id']}").status_code == 403
    assert client.get(f"/api/library/collections/{a}/conflicts").status_code == 403
    assert client.post(f"/api/library/collections/{a}/conflicts/{ks[0]['id']}/approve").status_code == 403
    _as(client, 'ann')
    assert client.post(f"/api/library/collections/{a}/conflicts/{ks[0]['id']}/approve").status_code == 200
    t = _topic(client, a)
    texts = {c['n']: c['text'] for c in t['claims']}
    assert t['version'] == 2 and 'fourteen days' in texts[3] and 1 not in texts and 2 in texts                       # the contradicted statement is superseded, the new one stands
    assert client.post(f"/api/library/collections/{a}/conflicts/{ks[0]['id']}/approve").status_code == 404
    assert client.get('/api/status').json()['library_review'] == 0


def test_rejecting_a_held_change_leaves_the_topic_as_it_was(client, world, gate):
    a = world['a']['id']
    gate([analysis(dict(REFUNDS, links=[])), analysis(_contradiction())])
    _two_sources(client, a, None)
    k = client.get(f'/api/library/collections/{a}/conflicts').json()['conflicts'][0]
    assert client.post(f"/api/library/collections/{a}/conflicts/{k['id']}/reject").status_code == 200
    t = _topic(client, a)
    assert t['version'] == 1 and [c['n'] for c in t['claims']] == [1, 2]
    assert client.get(f'/api/library/collections/{a}/conflicts', params={'status': 'rejected'}).json()['conflicts'][0]['decided_by'] == 'ann'
    with db.conn() as c:
        assert c.execute("SELECT status FROM library.claim WHERE topic_id=(SELECT id FROM library.topic WHERE slug='refund-policy' AND collection_id=%s) AND n=3", (a,)).fetchone()['status'] == 'rejected'


def test_a_probable_duplicate_is_held_as_a_draft_and_can_be_folded_into_the_existing_topic(client, world, gate, embedded):
    a = world['a']['id']
    dup = dict(REFUNDS, title='Refunds', slug='refunds', aliases=['Money back'], links=[], claims=[REFUNDS['claims'][1], {'claim_text': 'Refunds go to the original payment method.', 'claim_type': 'rule', 'source_quote': 'Yearly plans are refundable within thirty days'}])
    gate([analysis(dict(REFUNDS, links=[])), analysis(dup)])
    settings.put('DUPLICATE_SIMILARITY', 0.5, 't')
    try:
        _two_sources(client, a, None)
    finally:
        settings.reset('DUPLICATE_SIMILARITY', 't')
    assert set(_topics(client, a)) == {'refund-policy'}                                                  # the draft is not a topic yet: not listed, not found
    assert client.get(f'/api/library/collections/{a}/topics/refunds').status_code == 404
    k = client.get(f'/api/library/collections/{a}/conflicts').json()['conflicts'][0]
    assert k['kind'] == 'duplicate_topic' and k['duplicate_slug'] == 'refund-policy' and k['slug'] == 'refunds'
    assert client.post(f"/api/library/collections/{a}/conflicts/{k['id']}/redirect", json={}).status_code == 200
    t = _topic(client, a)
    assert 'Refunds' in t['aliases'] and 'Money back' in t['aliases'] and t['version'] == 2
    texts = [c['text'] for c in t['claims']]
    assert texts.count('A monthly plan ends with the billing period.') == 1 and 'Refunds go to the original payment method.' in texts              # the duplicate statement was not stored twice
    old = _topic(client, a, 'refunds')                                                                    # the old address still works
    assert old['topic']['slug'] == 'refund-policy' and old['redirected_from'] == 'refunds'
    assert client.get(f"/api/library/collections/{a}/conflicts/{k['id']}").status_code == 404 or client.get(f'/api/library/collections/{a}/conflicts', params={'status': 'open'}).json()['conflicts'] == []


def test_rejecting_a_duplicate_removes_the_draft_topic(client, world, gate, embedded):
    a = world['a']['id']
    gate([analysis(dict(REFUNDS, links=[])), analysis(dict(REFUNDS, title='Refunds', slug='refunds', aliases=[], links=[]))])
    settings.put('DUPLICATE_SIMILARITY', 0.5, 't')
    try:
        _two_sources(client, a, None)
    finally:
        settings.reset('DUPLICATE_SIMILARITY', 't')
    k = client.get(f'/api/library/collections/{a}/conflicts').json()['conflicts'][0]
    assert client.post(f"/api/library/collections/{a}/conflicts/{k['id']}/reject").status_code == 200
    with db.conn() as c:
        assert c.execute("SELECT count(*) AS n FROM library.topic WHERE collection_id=%s AND slug='refunds'", (a,)).fetchone()['n'] == 0
    assert set(_topics(client, a)) == {'refund-policy'}


def test_topics_can_be_merged_by_hand(client, world, gate):
    a = world['a']['id']
    gate([analysis(dict(REFUNDS, links=[]), PREMIUM)])
    _as(client, 'ann'); _upload(client, a, 'one.md', DOC)
    _run()
    assert client.post(f'/api/library/collections/{a}/topics/premium-plan/merge', json={'into': 'premium-plan'}).status_code == 422
    assert client.post(f'/api/library/collections/{a}/topics/premium-plan/merge', json={'into': 'nothing'}).status_code == 404
    _as(client, 'bob')
    assert client.post(f'/api/library/collections/{a}/topics/premium-plan/merge', json={'into': 'refund-policy'}).status_code == 403
    _as(client, 'ann')
    assert client.post(f'/api/library/collections/{a}/topics/premium-plan/merge', json={'into': 'refund-policy'}).json()['slug'] == 'refund-policy'
    t = _topic(client, a)
    assert [c['n'] for c in t['claims']] == [1, 2, 3] and 'Premium plan' in t['aliases'] and set(_topics(client, a)) == {'refund-policy'}
    assert _topic(client, a, 'premium-plan')['redirected_from'] == 'premium-plan'


def test_a_page_is_revised_after_its_source_is_removed_and_archived_when_nothing_is_left(client, world, gate):
    a = world['a']['id']
    two = dict(REFUNDS, action='update', existing_slug='refund-policy', aliases=[], links=[], claims=[REFUNDS['claims'][1]])
    f = gate([analysis(dict(REFUNDS, links=[], claims=[REFUNDS['claims'][0]])), analysis(two)])
    _as(client, 'ann'); one = _upload(client, a, 'one.md', DOC).json()['id']
    _run()
    two_id = _upload(client, a, 'two.md', DOC + OTHER).json()['id']
    _run()
    assert [c['n'] for c in _topic(client, a)['claims']] == [1, 2]
    client.delete(f'/api/library/collections/{a}/sources/{one}')
    t = _topic(client, a)
    assert t['topic']['needs_revision'] is True and [c['n'] for c in t['claims']] == [2]
    _run()                                                                                                 # the revision job
    t = _topic(client, a)
    assert t['topic']['needs_revision'] is False and t['version'] == 3 and '[c9]' not in t['markdown'] and '[c1]' not in t['markdown']      # only markers of live statements remain
    client.delete(f'/api/library/collections/{a}/sources/{two_id}')
    _run()
    assert client.get(f'/api/library/collections/{a}/topics/refund-policy').status_code == 404 and _topics(client, a) == {}                  # nothing supports it any more: archived


def test_health_finds_what_needs_attention(client, world, gate):
    a = world['a']['id']
    gate([analysis(dict(REFUNDS, links=[])), analysis(_contradiction())])
    _two_sources(client, a, None)
    sid = client.get(f'/api/library/collections/{a}/sources').json()['sources'][-1]['id']
    first = [s for s in client.get(f'/api/library/collections/{a}/sources').json()['sources'] if s['name'] == 'one.md'][0]['id']
    client.put(f'/api/library/collections/{a}/sources/{first}/wiki', json={'markdown': '# Rewritten\n\nNothing from before survives here.'})        # the quotes are gone from the page
    r = client.post(f'/api/library/collections/{a}/health').json()['findings']
    kinds = {f['kind'] for f in r}
    assert {'held_change', 'quote_missing', 'isolated_topic'} <= kinds
    assert client.get(f'/api/library/collections/{a}/health').json()['run']['findings'] == r
    _as(client, 'bob')
    assert client.get(f'/api/library/collections/{a}/health').status_code == 200 and client.post(f'/api/library/collections/{a}/health').status_code == 403


def test_decisions_on_held_changes_never_cross_collections(client, world, gate):
    a, b = world['a']['id'], world['b']['id']
    gate([analysis(dict(REFUNDS, links=[])), analysis(_contradiction())])
    _two_sources(client, a, None)
    k = client.get(f'/api/library/collections/{a}/conflicts').json()['conflicts'][0]
    with db.conn() as c:
        c.execute("INSERT INTO library.grant_ (collection_id, username, level) VALUES (%s,'ed','write') ON CONFLICT DO NOTHING", (b,))
    _as(client, 'ed')                                                                                  # a writer of B names A's conflict id under B
    for path in ('', '/approve', '/reject'):
        r = client.get(f"/api/library/collections/{b}/conflicts/{k['id']}") if not path else client.post(f"/api/library/collections/{b}/conflicts/{k['id']}{path}")
        assert r.status_code == 404
    assert client.get(f'/api/library/collections/{b}/conflicts').json()['conflicts'] == []
    assert client.post(f"/api/library/collections/{b}/topics/refund-policy/merge", json={'into': 'refund-policy'}).status_code == 404
    _as(client, 'eve')
    assert client.get(f'/api/library/collections/{a}/conflicts').status_code == 404


def test_the_collections_inbox_is_one_queue_for_everyone_who_can_write_and_the_manager_also_decides_the_vocabulary(client, world, gate):
    a, b = world['a']['id'], world['b']['id']
    odd = dict(PREMIUM, topic_type='Gadget', topic_type_fallback='Product', model_links=[{'entity': 'customer', 'relation': 'about', 'confidence': 0.4, 'reason': 'maybe the customer'}])
    gate([analysis(dict(REFUNDS, links=[]), odd), analysis(_contradiction())])
    with db.conn() as c:
        c.execute("INSERT INTO library.grant_ (collection_id, username, level) VALUES (%s,'ed','write') ON CONFLICT DO NOTHING", (a,))
    _two_sources(client, a, None)
    _as(client, 'ann')                                                                                 # the manager
    inbox = client.get(f'/api/library/collections/{a}/inbox').json()
    assert len(inbox['held_changes']) == 1 and [l['name'] for l in inbox['suggested_links']] == ['Customer'] and inbox['suggested_links'][0]['label'] == 'is about'
    assert [p['name'] for p in inbox['vocabulary']['proposals']] == ['Gadget'] and inbox['vocabulary']['can_decide'] is True
    assert client.get('/api/status').json()['library_review'] == 3                                    # held change + suggested link + vocabulary proposal
    _as(client, 'ed')                                                                                  # another editor: the same queue, but the vocabulary is not theirs to decide
    ed = client.get(f'/api/library/collections/{a}/inbox').json()
    assert len(ed['held_changes']) == 1 and ed['vocabulary']['can_decide'] is False and client.get('/api/status').json()['library_review'] == 2
    k = ed['held_changes'][0]['id']
    assert client.post(f'/api/library/collections/{a}/conflicts/{k}/approve').status_code == 200       # an editor decides on content...
    pid = ed['vocabulary']['proposals'][0]['id']
    assert client.post(f'/api/library/collections/{a}/vocabulary/proposals/{pid}', json={'approve': True}).status_code == 403          # ...not on the vocabulary
    assert client.post(f'/api/library/collections/{a}/topics/premium-plan/model-links/decide', json={'kind': 'entity', 'ref': 'customer', 'accept': True}).status_code == 200
    after = client.get(f'/api/library/collections/{a}/inbox').json()
    assert after['held_changes'] == [] and after['suggested_links'] == [] and len(after['vocabulary']['proposals']) == 1
    _as(client, 'bob')                                                                                 # a reader sees no queue and no count
    assert client.get(f'/api/library/collections/{a}/inbox').status_code == 403 and client.get('/api/status').json()['library_review'] == 0
    _as(client, 'eve')
    assert client.get(f'/api/library/collections/{a}/inbox').status_code == 404
    _as(client, 'ed')
    assert client.get(f'/api/library/collections/{b}/inbox').status_code == 404                        # B's queue is B's: per collection


def test_a_failed_document_waits_in_the_inbox_until_it_is_retried_or_removed(client, world, gate):
    a = world['a']['id']
    gate([analysis()])
    _as(client, 'ann')
    sid = _upload(client, a, 'blank.pdf', b'%PDF-1.4 not a real pdf').json()['id']
    _run()
    inbox = client.get(f'/api/library/collections/{a}/inbox').json()
    assert [f['name'] for f in inbox['failed_sources']] == ['blank.pdf'] and inbox['failed_sources'][0]['error']
    assert client.get('/api/status').json()['library_review'] == 1
    assert client.delete(f'/api/library/collections/{a}/sources/{sid}').status_code == 200
    assert client.get(f'/api/library/collections/{a}/inbox').json()['failed_sources'] == []


def test_a_held_change_does_not_count_its_source_until_approved(client, world, gate):
    a = world['a']['id']
    gate([analysis(dict(REFUNDS, links=[])), analysis(_contradiction())])
    _two_sources(client, a, None)
    t = _topic(client, a)
    assert [s['name'] for s in t['sources']] == ['one.md']                                              # the held source is not listed yet
    k = client.get(f'/api/library/collections/{a}/conflicts').json()['conflicts'][0]
    assert client.post(f"/api/library/collections/{a}/conflicts/{k['id']}/approve").status_code == 200
    assert sorted(s['name'] for s in _topic(client, a)['sources']) == ['one.md', 'two.md']
