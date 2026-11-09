import io, os, threading
import boto3, pytest
from moto import mock_aws
from studio import db
from studio.library import jobs, storage
from test_library_access import world, _as, _admin, _user, MARK_A, MARK_B, PW   # noqa: F401  (the two-collection set-up)


# ------------------------------------------------------------------ storage
def test_names_are_made_safe_and_uploads_are_size_limited():
    assert storage.clean_name('../../etc/passwd') == 'passwd' and storage.clean_name('a b/c\\d?.pdf') == 'd_.pdf' and storage.clean_name('') == 'file'
    fh, n, digest = storage.spool(io.BytesIO(b'hello'), 100)
    assert n == 5 and fh.read() == b'hello' and digest == '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824'
    with pytest.raises(ValueError):
        storage.spool(io.BytesIO(b'x' * 101), 100)


def test_local_store_keeps_files_under_its_root(tmp_path):
    st = storage.LocalStore(str(tmp_path))
    st.put('c1/s1/a.txt', io.BytesIO(b'one')); st.put('c1/s2/b.txt', io.BytesIO(b'two')); st.put('c2/s3/c.txt', io.BytesIO(b'three'))
    assert st.exists('c1/s1/a.txt') and st.open('c1/s1/a.txt').read() == b'one'
    with pytest.raises(storage.StorageError):
        st.put('../escape.txt', io.BytesIO(b'x'))
    with pytest.raises(storage.StorageError):
        st.open('c1/missing')
    assert st.delete_prefix('c1') == 2 and not st.exists('c1/s1/a.txt') and st.exists('c2/s3/c.txt')    # one collection's files go, not the other's
    assert 'writable' in st.check()


@mock_aws
def test_s3_store_deletes_every_version():
    s3 = boto3.client('s3', region_name='us-east-1')
    s3.create_bucket(Bucket='docs')
    s3.put_bucket_versioning(Bucket='docs', VersioningConfiguration={'Status': 'Enabled'})
    st = storage.S3Store('docs', prefix='lib', region='us-east-1', access_key='k', secret_key='s')
    st.put('c1/s1/a.txt', io.BytesIO(b'v1')); st.put('c1/s1/a.txt', io.BytesIO(b'v2')); st.put('c2/s9/z.txt', io.BytesIO(b'other'))
    assert st.open('c1/s1/a.txt').read() == b'v2' and st.exists('c1/s1/a.txt') and not st.exists('c1/nope')
    assert len(s3.list_object_versions(Bucket='docs', Prefix='lib/c1/')['Versions']) == 2
    s3.delete_object(Bucket='docs', Key='lib/c1/s1/a.txt')                                    # leaves a delete marker
    assert st.delete_prefix('c1') == 3
    left = s3.list_object_versions(Bucket='docs')
    assert [v['Key'] for v in left['Versions']] == ['lib/c2/s9/z.txt'] and not left.get('DeleteMarkers')
    assert 'versioning enabled' in st.check()
    with pytest.raises(storage.StorageError):
        storage.S3Store('')


# ------------------------------------------------------------------ jobs
@pytest.fixture
def col(client):
    with db.conn() as c:
        cid = c.execute("INSERT INTO library.collection (slug,name) VALUES ('jobs-test','Jobs test') RETURNING id").fetchone()['id']
    yield cid
    with db.conn() as c:
        c.execute('DELETE FROM library.collection WHERE id=%s', (cid,))
    jobs.HANDLERS.pop('test.ok', None); jobs.HANDLERS.pop('test.boom', None); jobs.HANDLERS.pop('test.slow', None)


def test_a_job_runs_reports_progress_and_finishes(col):
    @jobs.handler('test.ok')
    def ok(job, ctx):
        ctx.progress('working', 50, 'half way')
    jid = jobs.enqueue('test.ok', col, payload={'n': 1}, user='t')
    assert jobs.run_one('w1') == jid and jobs.run_one('w1') is None
    with db.conn() as c:
        r = c.execute('SELECT * FROM library.job WHERE id=%s', (jid,)).fetchone()
    assert r['status'] == 'done' and r['progress'] == {'step': 'working', 'pct': 50, 'message': 'half way'} and r['attempts'] == 1 and r['worker'] is None


def test_a_failing_job_is_retried_then_fails_with_its_error(col):
    @jobs.handler('test.boom')
    def boom(job, ctx):
        raise ValueError('nope')
    jid = jobs.enqueue('test.boom', col, max_attempts=2)
    jobs.run_one('w')
    with db.conn() as c:
        r = c.execute('SELECT status,attempts,error,run_after > now() AS later FROM library.job WHERE id=%s', (jid,)).fetchone()
        assert r['status'] == 'queued' and r['later'] and 'ValueError: nope' in r['error']          # waits before the second try
        c.execute("UPDATE library.job SET run_after=now() WHERE id=%s", (jid,))
    jobs.run_one('w')
    with db.conn() as c:
        r = c.execute('SELECT status,attempts FROM library.job WHERE id=%s', (jid,)).fetchone()
    assert r == {'status': 'failed', 'attempts': 2}


def test_two_workers_never_take_the_same_job(col):
    seen, lock = [], threading.Lock()

    @jobs.handler('test.ok')
    def ok(job, ctx):
        with lock:
            seen.append(job['id'])
    ids = [jobs.enqueue('test.ok', col) for _ in range(12)]
    ts = [threading.Thread(target=lambda n=n: [None for _ in iter(lambda: jobs.run_one(f'w{n}'), None)]) for n in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(seen) == sorted(ids)                                                        # each exactly once


def test_a_job_whose_worker_died_goes_back_to_the_queue(col):
    jid = jobs.enqueue('test.ok', col)
    assert jobs.claim('dead-worker')['id'] == jid
    with db.conn() as c:
        c.execute("UPDATE library.job SET lease_until = now() - interval '1 minute' WHERE id=%s", (jid,))
    jobs.reap()
    with db.conn() as c:
        assert c.execute('SELECT status,worker FROM library.job WHERE id=%s', (jid,)).fetchone() == {'status': 'queued', 'worker': None}


def test_a_cancelled_job_stops_at_its_next_progress_report(col):
    @jobs.handler('test.slow')
    def slow(job, ctx):
        jobs.cancel(job['id'], job['collection_id'])
        ctx.progress('still going')
    jid = jobs.enqueue('test.slow', col)
    jobs.run_one('w')
    with db.conn() as c:
        assert c.execute('SELECT status FROM library.job WHERE id=%s', (jid,)).fetchone()['status'] == 'cancelled'


# ------------------------------------------------------------------ uploads through the API
def _upload(client, ref, name='note.txt', data=b'some text for the library'):
    return client.post(f'/api/library/collections/{ref}/sources', files={'file': (name, data, 'text/plain')})


def test_uploading_stores_the_file_queues_a_job_and_the_worker_finishes_it(client, world):
    a = world['a']['id']
    _as(client, 'ann')
    r = _upload(client, a)
    assert r.status_code == 200 and r.json()['status'] == 'queued'
    sid = r.json()['id']
    assert os.path.isfile(os.path.join(os.environ['DATA_DIR'], 'library', a, sid, 'note.txt'))
    row = client.get(f'/api/library/collections/{a}/sources').json()['sources'][0]
    assert row['status'] == 'queued' and row['job']['status'] == 'queued'
    while jobs.run_one('t'):
        pass
    row = client.get(f'/api/library/collections/{a}/sources').json()['sources'][0]
    assert row['status'] == 'ready' and row['job']['status'] == 'done' and row['job']['pct'] == 100
    assert _upload(client, a).status_code == 409                                              # the same bytes twice
    assert _upload(client, a, 'x.exe', b'MZ').status_code == 422 and _upload(client, a, 'empty.txt', b'').status_code == 422
    assert client.delete(f'/api/library/collections/{a}/sources/{sid}').status_code == 200
    assert not os.path.exists(os.path.join(os.environ['DATA_DIR'], 'library', a, sid))
    assert client.get(f'/api/library/collections/{a}/sources').json()['sources'] == []


def test_a_failed_source_can_be_retried(client, world):
    a = world['a']['id']
    _as(client, 'ann')
    sid = _upload(client, a, 'again.txt', b'content to damage').json()['id']
    path = os.path.join(os.environ['DATA_DIR'], 'library', a, sid, 'again.txt')
    open(path, 'wb').write(b'damaged')
    with db.conn() as c:
        c.execute("UPDATE library.job SET max_attempts=1 WHERE source_id=%s", (sid,))
    jobs.run_one('t')
    row = client.get(f'/api/library/collections/{a}/sources').json()['sources'][0]
    assert row['status'] == 'failed' and 'does not match' in row['error']
    open(path, 'wb').write(b'content to damage')
    assert client.post(f'/api/library/collections/{a}/sources/{sid}/retry').status_code == 200
    jobs.run_one('t')
    assert client.get(f'/api/library/collections/{a}/sources').json()['sources'][0]['status'] == 'ready'


def test_only_writers_upload_and_nobody_reaches_another_collections_files(client, world):
    a, b = world['a']['id'], world['b']['id']
    _as(client, 'ann'); sid = _upload(client, a, 'secret.txt', MARK_A.encode()).json()['id']
    _as(client, 'bob')                                                                         # read on A: may list, may not change
    assert client.get(f'/api/library/collections/{a}/sources').status_code == 200
    assert _upload(client, a, 'bob.txt', b'bob').status_code == 403
    assert client.delete(f'/api/library/collections/{a}/sources/{sid}').status_code == 403
    assert client.post(f'/api/library/collections/{a}/sources/{sid}/retry').status_code == 403
    for r in (client.get(f'/api/library/collections/{b}/sources'), _upload(client, b, 'b.txt', b'b'), client.delete(f'/api/library/collections/{b}/sources/{sid}')):
        assert r.status_code == 404 and MARK_B not in r.text and MARK_A not in r.text
    _as(client, 'ann')                                                                         # ann manages A but holds nothing on B; a source id from A means nothing under B
    assert client.delete(f'/api/library/collections/{b}/sources/{sid}').status_code == 404
    assert client.get(f'/api/library/collections/{a}/sources').json()['sources'][0]['name'] == 'secret.txt'


def test_job_payloads_hold_ids_not_text(client, world):
    a = world['a']['id']
    _as(client, 'ann'); _upload(client, a, 'doc.txt', b'text that must not be copied into the queue ' + MARK_A.encode())
    with db.conn() as c:
        rows = c.execute('SELECT payload::text AS p, progress::text AS g, error FROM library.job').fetchall()
    assert rows and all(MARK_A not in (r['p'] + r['g'] + (r['error'] or '')) for r in rows)


def test_deleting_a_collection_deletes_its_files_and_a_connector_cannot_read_them(client, world):
    from studio import drivers
    a = world['a']['id']
    _as(client, 'ann'); sid = _upload(client, a, 'gone.txt', b'bye bye').json()['id']
    path = os.path.join(os.environ['DATA_DIR'], 'library', a, sid, 'gone.txt')
    with pytest.raises(ValueError, match='Library files'):
        drivers._safe_path(path)
    assert client.delete(f'/api/library/collections/{a}').status_code == 200
    assert not os.path.exists(os.path.join(os.environ['DATA_DIR'], 'library', a))
    with db.conn() as c:
        assert c.execute('SELECT count(*) AS n FROM library.job').fetchone()['n'] == 0
