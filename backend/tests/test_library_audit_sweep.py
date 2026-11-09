"""Library events in the activity log and audit trail, and a sweep over every Library route that a caller with no access must not get anything from."""
import re, threading, time
from studio import activity, db, settings
from studio.api import app
from studio.library import jobs
from test_library_access import world, _as, _admin, MARK_A, MARK_B   # noqa: F401
from test_library_storage_jobs import _upload


def _flush():
    for _ in range(50):
        if activity._q.empty():
            break
        time.sleep(0.1)
    time.sleep(0.2)


def test_a_documents_processing_appears_in_the_activity_log_without_its_name(client, world):
    a = world['a']['id']
    _as(client, 'ann'); sid = _upload(client, a, f'{MARK_A}-private-name.txt', b'activity log content').json()['id']
    while jobs.run_one('t'):
        pass
    _flush()
    _admin(client)
    sessions = [s for s in client.get('/api/admin/activity/sessions?source=library').json() if sid[:8] in (s['title'] or '')]
    assert len(sessions) == 1 and sessions[0]['actor'] == 'ann' and MARK_A not in str(sessions[0])
    detail = client.get(f"/api/admin/activity/sessions/{sessions[0]['id']}").json()
    kinds = [(e['kind'], e['name']) for e in detail['events']]
    assert ('step', 'extracting the text') in kinds and ('job', 'source.ingest') in kinds
    assert MARK_A not in str(detail)


def test_the_audit_trail_names_who_did_what_but_never_a_file_and_only_admins_read_it(client, world):
    a = world['a']['id']
    _as(client, 'ann'); sid = _upload(client, a, f'{MARK_A}-secret-name.txt', b'audit content').json()['id']
    client.delete(f'/api/library/collections/{a}/sources/{sid}')
    _as(client, 'eve')                                                                           # holds nothing: sees none of it
    assert client.get('/api/admin/audit').status_code == 403
    assert 'collection' not in {r['kind'] for r in client.get('/api/audit').json()} and MARK_A not in client.get('/api/audit').text
    _admin(client)
    rows = client.get('/api/admin/audit?kind=collection').json()
    actions = {(r['action'], r['username']) for r in rows if r['key'] == a}
    assert {('create', 'admin'), ('self_grant', 'admin'), ('grant', 'admin'), ('upload', 'ann'), ('delete_source', 'ann')} <= actions
    assert 'secret-name' not in str(rows)


def _body(method, path):
    """A valid JSON body for the route, built from its own schema, so that input is never the reason for a refusal."""
    schemas = app.openapi()['components']['schemas']
    op = app.openapi()['paths'][path][method.lower()]
    ref = (((op.get('requestBody') or {}).get('content') or {}).get('application/json') or {}).get('schema', {}).get('$ref')
    if not ref:
        return {}
    sch = schemas[ref.split('/')[-1]]
    fill = {'string': 'read', 'integer': 1, 'boolean': False, 'array': []}
    out = {}
    for name in sch.get('required', []):
        t = sch['properties'][name].get('type', 'string')
        out[name] = 'read' if name == 'level' else fill.get(t, 'x')
    if 'level' in sch['properties']:
        out['level'] = 'read'
    if 'username' in sch['properties']:
        out['username'] = 'eve'
    return out


def test_every_library_route_refuses_a_caller_with_no_access_and_leaks_nothing(client, world):
    """Walks the routes themselves, so a route added later is covered without anyone remembering to test it."""
    a, b = world['a']['id'], world['b']['id']
    _as(client, 'ann'); sid = _upload(client, a, 'x.txt', b'sweep content ' + MARK_A.encode()).json()['id']
    spec = app.openapi()['paths']
    routes = [(m.upper(), path) for path, ops in spec.items() if path.startswith('/api/library') and '{ref}' in path for m in ops if m in ('get', 'post', 'put', 'patch', 'delete')]
    required = {(m.upper(), path): {p['name']: ('1' if p['schema'].get('type') == 'integer' else 'x') for p in ops[m].get('parameters', []) if p['in'] == 'query' and p.get('required')}
                for path, ops in spec.items() for m in ops if path.startswith('/api/library')}
    assert len(routes) >= 10
    for who in ('eve', 'bob'):                                                                   # eve has no grant at all, bob only reads A: both are strangers to B
        _as(client, who)
        for method, path in routes:
            url = re.sub(r'\{ref\}', b, path)
            url = re.sub(r'\{sid\}', sid, url)
            url = re.sub(r'\{(gid|pid|kid|claim_id)\}', '1', url)
            url = re.sub(r'\{slug\}', 'no-such-topic', url); url = re.sub(r'\{iid\}', '00000000-0000-0000-0000-000000000000', url); url = re.sub(r'\{kind\}', 'topic_type', url)
            kw = {'files': {'file': ('s.txt', b'try', 'text/plain')}} if url.endswith('/sources') and method == 'POST' else ({'json': _body(method, path)} if method in ('PUT', 'POST', 'PATCH') else {})
            r = client.request(method, url, params=required.get((method, path)), **kw)
            assert r.status_code in (404, 403), (who, method, url, r.status_code)          # never 422: input is not even looked at before access is
            assert MARK_B not in r.text and MARK_A not in r.text, (who, method, url)
    with db.conn() as c:                                                                         # nothing changed in B
        assert c.execute('SELECT count(*) AS n FROM library.source WHERE collection_id=%s', (b,)).fetchone()['n'] == 0
        assert c.execute('SELECT count(*) AS n FROM library.grant_ WHERE collection_id=%s AND username IN (%s,%s)', (b, 'eve', 'bob')).fetchone()['n'] == 0


def test_switching_the_library_off_stops_the_workers_without_deleting_anything(client, world):
    a = world['a']['id']
    _as(client, 'ann'); _upload(client, a, 'wait.txt', b'wait for the switch')
    _admin(client)
    client.put('/api/admin/settings', json={'name': 'LIBRARY_ENABLED', 'value': False})
    settings.forget()
    stop = threading.Event()
    t = threading.Thread(target=jobs.work_loop, args=('off-test', stop, 0.1)); t.start()
    time.sleep(1); stop.set(); t.join(5)
    try:
        with db.conn() as c:
            assert c.execute("SELECT count(*) AS n FROM library.job WHERE status='queued'").fetchone()['n'] >= 1
    finally:
        client.put('/api/admin/settings', json={'name': 'LIBRARY_ENABLED', 'value': True}); settings.forget()
    while jobs.run_one('t'):
        pass
