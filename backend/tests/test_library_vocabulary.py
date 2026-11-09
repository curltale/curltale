import pytest
from studio import db
from studio.library import topics
from test_library_access import world, _as, _admin, MARK_A, MARK_B   # noqa: F401
from test_library_wiki import _run, ai   # noqa: F401
from test_library_storage_jobs import _upload
from test_library_topics import REFUNDS, PREMIUM, TopicFake, analysis, DOC


def _vocab(client, a):
    return client.get(f'/api/library/collections/{a}/vocabulary').json()


def test_the_manager_shapes_the_vocabulary_of_the_collection(client, world):
    a, b = world['a']['id'], world['b']['id']
    url = f'/api/library/collections/{a}/vocabulary'
    _as(client, 'ann')                                                                                    # manager of A
    assert client.post(f'{url}/topic_type', json={'name': 'Escalation path', 'description': 'Who to call when'}).status_code == 200
    assert client.post(f'{url}/topic_type', json={'name': 'escalation PATH'}).status_code == 409                # names are unique whatever the case
    assert client.post(f'{url}/topic_type', json={'name': '<script>'}).status_code == 422
    assert client.post(f'{url}/relation_type', json={'name': 'escalates_to', 'description': 'hands over to', 'inverse_name': 'receives_from'}).status_code == 200
    assert client.post(f'{url}/colour', json={'name': 'x'}).status_code == 404
    v = _vocab(client, a)
    assert next(t for t in v['topic_types'] if t['name'] == 'Escalation path')['uses'] == 0 and next(r for r in v['relation_types'] if r['name'] == 'escalates_to')['inverse_name'] == 'receives_from'
    assert client.patch(f'{url}/topic_type', params={'name': 'Escalation path'}, json={'description': 'Who to call, and when'}).status_code == 200
    assert next(t for t in _vocab(client, a)['topic_types'] if t['name'] == 'Escalation path')['description'] == 'Who to call, and when'
    assert client.patch(f'{url}/topic_type', params={'name': 'Nope'}, json={'description': 'x'}).status_code == 404
    assert client.delete(f'{url}/topic_type', params={'name': 'Escalation path'}).status_code == 200
    assert client.delete(f'{url}/topic_type', params={'name': 'Topic'}).status_code == 422 and client.delete(f'{url}/relation_type', params={'name': 'related_to'}).status_code == 422    # the fallbacks stay
    assert all(t['name'] != 'Escalation path' for t in _vocab(client, a)['topic_types'])
    with db.conn() as c:                                                                                      # B never saw any of it
        assert c.execute("SELECT count(*) AS n FROM library.relation_type WHERE collection_id=%s AND name='escalates_to'", (b,)).fetchone()['n'] == 0
    _admin(client)
    assert {'vocab_add', 'vocab_edit', 'vocab_delete'} <= {r['action'] for r in client.get('/api/admin/audit?kind=collection').json()}


def test_a_kind_in_use_is_merged_not_deleted_and_only_managers_change_anything(client, world, ai):
    a = world['a']['id']
    ai(TopicFake([analysis(dict(REFUNDS, links=[{'target_title': 'Premium plan', 'relation_type': 'governs', 'context': 'x'}, {'target_title': 'Premium plan', 'relation_type': 'depends_on', 'context': 'y'}]), PREMIUM)]))
    _as(client, 'ann'); _upload(client, a, 'a.md', DOC)
    _run()
    url = f'/api/library/collections/{a}/vocabulary'
    v = _vocab(client, a)
    assert next(t for t in v['topic_types'] if t['name'] == 'Policy')['uses'] == 1 and next(r for r in v['relation_types'] if r['name'] == 'governs')['uses'] == 1
    r = client.delete(f'{url}/topic_type', params={'name': 'Policy'})
    assert r.status_code == 409 and '1 topics are' in r.json()['detail']
    assert client.post(f'{url}/topic_type/merge', params={'name': 'Policy'}, json={'into': 'Policy'}).status_code == 422
    assert client.post(f'{url}/topic_type/merge', params={'name': 'Policy'}, json={'into': 'Nonexistent'}).status_code == 404
    assert client.post(f'{url}/topic_type/merge', params={'name': 'Topic'}, json={'into': 'Regulation'}).status_code == 422
    assert client.post(f'{url}/topic_type/merge', params={'name': 'Policy'}, json={'into': 'Regulation'}).status_code == 200
    assert client.get(f'/api/library/collections/{a}/topics/refund-policy').json()['topic']['topic_type'] == 'Regulation'
    assert all(t['name'] != 'Policy' for t in _vocab(client, a)['topic_types'])
    assert client.post(f'{url}/relation_type/merge', params={'name': 'governs'}, json={'into': 'depends_on'}).status_code == 200      # two links between the same topics become one
    assert [(l['relation']) for l in client.get(f'/api/library/collections/{a}/topics/refund-policy').json()['links']] == ['depends_on']
    _as(client, 'bob')                                                                                    # a reader looks, never changes
    assert _vocab(client, a)['topic_types'] and client.post(f'{url}/topic_type', json={'name': 'Mine'}).status_code == 403
    assert client.delete(f'{url}/topic_type', params={'name': 'Rule'}).status_code == 403
    _as(client, 'eve')
    assert client.get(url).status_code == 404 and client.post(f'{url}/topic_type', json={'name': 'Mine'}).status_code == 404


def test_the_palette_finds_only_what_the_person_can_read(client, world, ai):
    a, b = world['a']['id'], world['b']['id']
    ai(TopicFake([analysis(REFUNDS, PREMIUM)]))
    with db.conn() as c:
        c.execute("INSERT INTO library.grant_ (collection_id, username, level) VALUES (%s,'ed','write') ON CONFLICT DO NOTHING", (b,))
    _as(client, 'ann'); _upload(client, a, 'refund-guide.md', DOC)
    _as(client, 'ed'); _upload(client, b, 'secret-playbook.md', DOC + f' {MARK_B}'.encode())
    _run()
    _as(client, 'bob')                                                                                    # reads A only
    r = client.get('/api/library/find', params={'q': 'refund'}).json()
    assert [t['slug'] for t in r['topics']] == ['refund-policy'] and r['topics'][0]['usage'] == 'public' and [s['name'] for s in r['sources']] == ['refund-guide.md']
    assert MARK_B not in str(r) and client.get('/api/library/find', params={'q': 'secret'}).json() == {'collections': [], 'sources': [], 'topics': []}
    assert [c['name'] for c in client.get('/api/library/find', params={'q': 'alpha'}).json()['collections']] == [f'Alpha {MARK_A}']
    assert client.get('/api/library/find', params={'q': 'r'}).json()['topics'] == []                    # a letter or two is not a search
    _as(client, 'eve')
    assert client.get('/api/library/find', params={'q': 'refund'}).json() == {'collections': [], 'sources': [], 'topics': []}
