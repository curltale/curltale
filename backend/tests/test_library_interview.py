import json
import pytest
from studio import db, llm
from studio.library import jobs
from test_library_access import world, _as, _admin, MARK_A, MARK_B   # noqa: F401
from test_library_wiki import _run, _src, _wiki, ai   # noqa: F401
from test_library_topics import TopicFake, analysis

OPS = [[{'op': 'add', 'claim_text': 'A refund request starts when the customer writes to the shared inbox.', 'category': 'trigger'},
        {'op': 'add', 'claim_text': 'The agent checks the invoice date first.', 'category': 'step'}],
       [{'op': 'update', 'claim_id': 1, 'claim_text': 'A refund request starts when the customer writes to the refunds inbox.', 'category': 'trigger'},
        {'op': 'add', 'claim_text': 'Requests older than 30 days are refused.', 'category': 'decision'}]]


class Interviewer(TopicFake):
    def __init__(self):
        super().__init__([analysis()])
        self.seen, self.ops, self.writer_calls = [], [list(o) for o in OPS], 0

    def chat_stream(self, messages, role='assistant', **kw):
        self.seen.append(messages)
        for piece in ('And what ', 'happens next?'):
            yield piece

    def chat(self, messages, role='assistant', **kw):
        if role == 'interview':                                                      # the fact extractor
            ops = self.ops.pop(0) if self.ops else []
            return 'Here are the operations:\n' + json.dumps(ops)
        if role == 'topic_writer' and messages[0]['content'].startswith('You are a technical writer'):
            return '# Refund handling\n\n## Overview\n\nA refund request starts in the refunds inbox. Requests older than 30 days are refused.'
        if role == 'writer':
            self.writer_calls += 1
        return super().chat(messages, role, **kw)


@pytest.fixture
def talk(client, world, ai, monkeypatch):
    f = ai(Interviewer())
    monkeypatch.setattr(llm, 'chat_stream', f.chat_stream)
    return f


def _start(client, a, seed='How we handle refund requests. It is mostly tribal knowledge.'):
    r = client.post(f'/api/library/collections/{a}/interviews', json={'seed': seed})
    assert r.status_code == 200, r.text
    return r.json()['id']


def _say(client, a, iid, text):
    r = client.post(f'/api/library/collections/{a}/interviews/{iid}/turns', json={'content': text})
    assert r.status_code == 200, r.text
    return r.text


def _get(client, a, iid):
    return client.get(f'/api/library/collections/{a}/interviews/{iid}').json()


def test_an_interview_builds_memory_a_draft_and_finally_a_source(client, world, talk):
    a = world['a']['id']
    _as(client, 'ann')
    iid = _start(client, a)
    body = _say(client, a, iid, 'A customer writes in, we check the invoice date.')
    assert 'event: content' in body and 'happens next?' in body and 'event: done' in body                      # the answer streams back in pieces
    s = _get(client, a, iid)
    assert [t['role'] for t in s['turns']] == ['user', 'agent'] and s['turns'][1]['content'] == 'And what happens next?' and s['claims'] == []
    with db.conn() as c:
        p = c.execute("SELECT payload::text AS p FROM library.job WHERE kind='interview.extract'").fetchone()['p']
    assert 'tribal' not in p and 'invoice' not in p                                                              # the queue holds ids, never what was said
    _run()
    s = _get(client, a, iid)
    assert [(c['category'], c['text'][:30]) for c in s['claims']] == [('trigger', 'A refund request starts when t'), ('step', 'The agent checks the invoice d')]
    _say(client, a, iid, 'Actually it is the refunds inbox. And anything older than 30 days is refused.')
    prompt = talk.seen[-1][0]['content']
    assert 'tribal knowledge' in prompt and 'The agent checks the invoice date first.' in prompt and 'shared inbox' in prompt     # the seed and every fact so far are in the interviewer's memory
    assert talk.seen[-1][-1] == {'role': 'user', 'content': 'Actually it is the refunds inbox. And anything older than 30 days is refused.'}
    _run()
    s = _get(client, a, iid)
    assert len(s['claims']) == 3 and 'refunds inbox' in s['claims'][0]['text']                                      # a correction updates the fact in place
    # the person tidies the facts, then asks for the draft
    assert client.put(f"/api/library/collections/{a}/interviews/{iid}/claims/{s['claims'][1]['id']}", json={'text': 'The agent checks the invoice date and the amount first.'}).status_code == 200
    assert client.delete(f"/api/library/collections/{a}/interviews/{iid}/claims/{s['claims'][2]['id']}").status_code == 200
    assert client.post(f'/api/library/collections/{a}/interviews/{iid}/draft').status_code == 200
    _run()
    s = _get(client, a, iid)
    assert s['draft']['version'] == 1 and s['draft']['markdown'].startswith('# Refund handling') and s['interview']['status'] == 'draft_ready'
    assert client.put(f'/api/library/collections/{a}/interviews/{iid}/draft', json={'markdown': s['draft']['markdown'] + '\n\nOwner: the refunds team.'}).json()['version'] == 2
    r = client.post(f'/api/library/collections/{a}/interviews/{iid}/finalize')
    assert r.status_code == 200
    _run()
    src = [x for x in client.get(f'/api/library/collections/{a}/sources').json()['sources'] if x['id'] == r.json()['source_id']][0]
    assert src['kind'] == 'interview' and src['status'] == 'ready' and src['wiki_status'] == 'approved' and src['created_by'] == 'ann'
    w = _wiki(client, a, src['id'])
    assert 'Owner: the refunds team.' in w['markdown'] and w['versions'][0]['origin'] == 'edited' and talk.writer_calls == 0           # the reviewed draft is the page: the writer is not called again
    assert client.get(f'/api/library/collections/{a}/search', params={'q': 'refunds inbox'}).json()['passages']
    assert _get(client, a, iid)['interview']['status'] == 'ingested'
    assert client.post(f'/api/library/collections/{a}/interviews/{iid}/finalize').status_code == 409
    assert client.post(f'/api/library/collections/{a}/interviews/{iid}/turns', json={'content': 'more'}).status_code == 409


def test_a_draft_needs_facts_and_a_finalize_needs_a_draft(client, world, talk):
    a = world['a']['id']
    _as(client, 'ann'); iid = _start(client, a)
    assert client.post(f'/api/library/collections/{a}/interviews/{iid}/draft').status_code == 422
    assert client.post(f'/api/library/collections/{a}/interviews/{iid}/finalize').status_code == 422
    assert client.post(f'/api/library/collections/{a}/interviews', json={'seed': '  '}).status_code == 422
    assert client.post(f'/api/library/collections/{a}/interviews/{iid}/turns', json={'content': ' '}).status_code == 422


def test_who_may_interview_and_who_sees_an_interview(client, world, talk):
    a, b = world['a']['id'], world['b']['id']
    _as(client, 'ann'); iid = _start(client, a)
    _as(client, 'bob')                                                                                           # read access only
    assert client.post(f'/api/library/collections/{a}/interviews', json={'seed': 'x'}).status_code == 403
    with db.conn() as c:
        c.execute("INSERT INTO library.grant_ (collection_id, username, level) VALUES (%s,'ed','write') ON CONFLICT DO NOTHING", (a,))
    _as(client, 'ed')                                                                                            # another writer: the conversation is not theirs
    assert client.get(f'/api/library/collections/{a}/interviews/{iid}').status_code == 404
    assert client.get(f'/api/library/collections/{a}/interviews').json()['interviews'] == []
    assert client.post(f'/api/library/collections/{a}/interviews/{iid}/turns', json={'content': 'hello'}).status_code == 404
    _as(client, 'ann')                                                                                           # the collection's manager sees all of them
    assert [i['id'] for i in client.get(f'/api/library/collections/{a}/interviews').json()['interviews']] == [iid]
    _as(client, 'eve')
    assert client.get(f'/api/library/collections/{a}/interviews').status_code == 404
    assert client.get(f'/api/library/collections/{b}/interviews/{iid}').status_code == 404


def test_an_interview_needs_a_model_it_may_use(client, world, talk):
    a = world['a']['id']
    _as(client, 'ann'); iid = _start(client, a)
    client.patch(f'/api/library/collections/{a}', json={'allow_ai': False})
    assert client.post(f'/api/library/collections/{a}/interviews', json={'seed': 'x'}).status_code == 422
    assert client.post(f'/api/library/collections/{a}/interviews/{iid}/turns', json={'content': 'hi'}).status_code == 422
    assert client.delete(f'/api/library/collections/{a}/interviews/{iid}').status_code == 200                      # an interview can always be thrown away
