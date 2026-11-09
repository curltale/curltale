import io, json, os, zipfile
import pytest
from studio import db
from test_library_access import world, _as, _admin, MARK_A, MARK_B   # noqa: F401
from test_library_wiki import _run, ai   # noqa: F401
from test_library_storage_jobs import _upload
from test_library_topics import REFUNDS, PREMIUM, TopicFake, analysis, DOC
from test_library_bridge import Linker, MODEL_LINKS   # noqa: F401


def _zip(r):
    assert r.status_code == 200, r.text
    return zipfile.ZipFile(io.BytesIO(r.content))


def test_the_manager_exports_a_collection_as_one_zip(client, world, ai):
    a, b = world['a']['id'], world['b']['id']
    ai(Linker([analysis(dict(REFUNDS, links=[{'target_title': 'Premium plan', 'relation_type': 'governs', 'context': 'x'}], model_links=[{'entity': 'customer', 'relation': 'governs', 'confidence': 0.9, 'reason': 'x'}]), PREMIUM)]))
    with db.conn() as c:
        c.execute("INSERT INTO library.grant_ (collection_id, username, level) VALUES (%s,'ed','write') ON CONFLICT DO NOTHING", (b,))
    _as(client, 'ann'); sid = _upload(client, a, 'Refund guide.md', DOC).json()['id']
    _as(client, 'ed'); _upload(client, b, 'b.md', DOC + f' {MARK_B}'.encode())
    _run()
    _as(client, 'ann')
    r = client.get(f'/api/library/collections/{a}/export')
    assert r.headers['content-type'] == 'application/zip' and 'attachment' in r.headers['content-disposition'] and '-export-' in r.headers['content-disposition']
    z = _zip(r)
    names = set(z.namelist())
    assert {'manifest.json', 'README.txt', 'collection.json', 'vocabulary.json', 'links.json', f'sources/{sid}.json', f'pages/{sid}/v1.md', f'pages/{sid}/checks.json', 'topics/refund-policy.md', 'topics/refund-policy.json', 'topics/premium-plan.md'} <= names
    assert z.read(f'originals/{sid}-Refund guide.md') == DOC                                                    # the file exactly as uploaded
    man = json.loads(z.read('manifest.json'))
    assert man['format'] == 2 and man['exported_by'] == 'ann' and man['counts'] == {'sources': 1, 'topics': 2} and man['files_missing_from_storage'] == [] and man['collection'] == f'Alpha {MARK_A}'
    md = z.read('topics/refund-policy.md').decode()
    assert md.startswith('---\ntitle: "Refund policy"') and 'governs: premium-plan' in md and 'governs: entity customer' in md and '`[source: ' in md
    t = json.loads(z.read('topics/refund-policy.json'))
    assert t['claims'][0]['n'] == 1 and t['claims'][0]['evidence'][0]['source'] == 'Refund guide.md' and t['claims'][0]['evidence'][0]['quote'].startswith('Yearly plans') and t['claims'][0]['evidence'][0]['wiki_version'] == 1 and t['claims'][0]['evidence'][0]['wiki_passage'] == 0 and t['aliases'] == ['Cancellation policy', 'Refund policy']
    assert json.loads(z.read('links.json')) == [{'from': 'refund-policy', 'relation': 'governs', 'to': 'premium-plan', 'context': 'x'}]
    assert any(v['name'] == 'Policy' for v in json.loads(z.read('vocabulary.json'))['topic_types'])
    assert MARK_B not in b''.join(z.read(n) for n in z.namelist()).decode(errors='ignore')                   # nothing of the other collection
    _admin(client)
    assert 'export' in {x['action'] for x in client.get('/api/admin/audit?kind=collection').json()}


def test_held_changes_and_interviews_stay_out_and_a_lost_file_is_reported(client, world, ai):
    a = world['a']['id']
    ai(TopicFake([analysis(dict(REFUNDS, links=[]))]))
    _as(client, 'ann'); sid = _upload(client, a, 'g.md', DOC).json()['id']
    _run()
    iid = client.post(f'/api/library/collections/{a}/interviews', json={'seed': 'PRIVATE-INTERVIEW-SEED'}).json()['id']
    with db.conn() as c:
        c.execute("INSERT INTO library.interview_turn (collection_id,interview_id,role,content) VALUES (%s,%s,'user','PRIVATE-INTERVIEW-TALK')", (a, iid))
    os.remove(os.path.join(os.environ['DATA_DIR'], 'library', a, sid, 'g.md'))                               # the stored file has gone missing
    z = _zip(client.get(f'/api/library/collections/{a}/export'))
    man = json.loads(z.read('manifest.json'))
    assert [m['source'] for m in man['files_missing_from_storage']] == [sid] and f'sources/{sid}.json' in z.namelist() and not any(n.startswith('originals/') for n in z.namelist())
    assert 'PRIVATE-INTERVIEW' not in b''.join(z.read(n) for n in z.namelist()).decode(errors='ignore')


def test_only_the_manager_exports_and_not_too_often(client, world):
    a = world['a']['id']
    _as(client, 'bob')
    assert client.get(f'/api/library/collections/{a}/export').status_code == 403
    with db.conn() as c:
        c.execute("INSERT INTO library.grant_ (collection_id, username, level) VALUES (%s,'ed','write') ON CONFLICT DO NOTHING", (a,))
    _as(client, 'ed')
    assert client.get(f'/api/library/collections/{a}/export').status_code == 403                           # an editor writes; only a manager takes it all out
    _as(client, 'eve')
    assert client.get(f'/api/library/collections/{a}/export').status_code == 404
    _admin(client)
    assert client.get(f'/api/library/collections/{a}/export').status_code == 403                           # an admin must first grant themselves access, which is logged
    from studio import ratelimit, settings
    settings.put('INGEST_PER_MIN', 1, 't'); ratelimit.reconfigure(); ratelimit.INGEST.buckets.clear()
    try:
        _as(client, 'ann')
        assert client.get(f'/api/library/collections/{a}/export').status_code == 200
        assert client.get(f'/api/library/collections/{a}/export').status_code == 429
    finally:
        settings.reset('INGEST_PER_MIN', 't'); ratelimit.reconfigure(); ratelimit.INGEST.buckets.clear()
