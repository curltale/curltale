"""Collections and who may use them. The two-collection set-up here (A and B, each with a marker nobody else may see) is the base of the
isolation tests: every Library feature added later gets its entry points checked against it."""
import os
import pytest
from studio import auth, db
from studio.library import scope as S

PW = 'library-test-password'
MARK_A, MARK_B = 'MARKER-ALPHA-7731', 'MARKER-BRAVO-9915'


def _user(name, role='viewer', extra=()):
    with db.conn() as c:
        c.execute('INSERT INTO app_user (username,password_hash,role,extra_roles) VALUES (%s,%s,%s,%s) ON CONFLICT (username) DO UPDATE SET password_hash=EXCLUDED.password_hash, role=EXCLUDED.role, extra_roles=EXCLUDED.extra_roles, must_change=false',
                  (name, auth.hash_password(PW), role, list(extra)))
        c.execute('DELETE FROM login_attempt WHERE username=%s', (name,))


def _as(client, name, pw=PW):
    client.post('/api/logout')
    assert client.post('/api/login', json={'username': name, 'password': pw}).status_code == 200


def _admin(client):
    _as(client, 'admin', os.environ['INITIAL_ADMIN_PASSWORD'])


@pytest.fixture
def world(client):
    """Collections A (with marker in its name) and B; ann has manage on A, bob read on A, eve nothing; an API token reads B."""
    for n in ('ann', 'bob', 'eve', 'ed'):
        _user(n, extra=('editor',) if n in ('ann', 'ed') else ())
    _admin(client)
    a = client.post('/api/library/collections', json={'name': f'Alpha {MARK_A}', 'usage': 'public', 'manager': 'ann'}).json()
    b = client.post('/api/library/collections', json={'name': f'Bravo {MARK_B}', 'usage': 'internal'}).json()
    assert client.put(f"/api/library/collections/{a['id']}/grants", json={'username': 'bob', 'level': 'read'}).status_code == 403   # admin holds no grant: not even an admin manages without one
    client.post(f"/api/library/collections/{a['id']}/self-grant", json={'level': 'manage'})
    assert client.put(f"/api/library/collections/{a['id']}/grants", json={'username': 'bob', 'level': 'read'}).status_code == 200
    tid = client.post('/api/admin/tokens', json={'name': 'lib-token'}).json()
    with db.conn() as c:
        tok = c.execute("SELECT id FROM api_token WHERE name='lib-token'").fetchone()['id']
    client.post(f"/api/library/collections/{b['id']}/self-grant", json={'level': 'manage'})
    assert client.put(f"/api/library/collections/{b['id']}/grants", json={'token_id': tok, 'level': 'read'}).status_code == 200
    client.post('/api/logout')
    with db.conn() as c:
        c.execute("DELETE FROM library.grant_ WHERE username='admin'")                 # set-up only: tests start with an admin who holds nothing
    yield {'a': a, 'b': b, 'token_id': tok}
    with db.conn() as c:
        c.execute('DELETE FROM library.collection')
        c.execute("DELETE FROM api_token WHERE name='lib-token'")


def test_a_user_sees_only_the_collections_they_hold(client, world):
    _as(client, 'bob')
    names = [c['name'] for c in client.get('/api/library/collections').json()['collections']]
    assert names == [f'Alpha {MARK_A}']
    _as(client, 'eve')
    assert client.get('/api/library/collections').json()['collections'] == []


def test_a_collection_you_hold_nothing_on_does_not_exist_for_you(client, world):
    _as(client, 'bob')
    for r in (client.get(f"/api/library/collections/{world['b']['id']}/grants"), client.patch(f"/api/library/collections/{world['b']['id']}", json={'name': 'x'}),
              client.delete(f"/api/library/collections/{world['b']['id']}"), client.put(f"/api/library/collections/{world['b']['id']}/grants", json={'username': 'bob', 'level': 'manage'})):
        assert r.status_code == 404 and MARK_B not in r.text
    r = client.get(f"/api/library/collections/{world['a']['id']}/grants")                     # read is not enough to manage
    assert r.status_code == 403


def test_an_admin_sees_that_collections_exist_but_holds_no_access_until_a_logged_self_grant(client, world):
    _admin(client)
    rows = {c['name']: c['level'] for c in client.get('/api/library/collections').json()['collections']}
    assert rows == {f'Alpha {MARK_A}': None, f'Bravo {MARK_B}': None}
    assert client.get(f"/api/library/collections/{world['a']['id']}/grants").status_code == 403
    assert client.post(f"/api/library/collections/{world['a']['id']}/self-grant", json={'level': 'read'}).status_code == 200
    with db.conn() as c:
        assert c.execute("SELECT 1 FROM audit_log WHERE action='self_grant' AND key=%s AND username='admin'", (world['a']['id'],)).fetchone()
    assert {c['name']: c['level'] for c in client.get('/api/library/collections').json()['collections']}[f'Alpha {MARK_A}'] == 'read'


def test_manage_needs_a_manage_grant_and_levels_above_read_need_the_editor_role(client, world):
    _as(client, 'ann')
    assert client.patch(f"/api/library/collections/{world['a']['id']}", json={'usage': 'internal'}).status_code == 200
    assert client.patch(f"/api/library/collections/{world['a']['id']}", json={'usage': 'secret'}).status_code == 422
    assert client.put(f"/api/library/collections/{world['a']['id']}/grants", json={'username': 'ed', 'level': 'write'}).status_code == 200
    ed = S.for_user({'username': 'ed', 'role': 'viewer', 'extra_roles': ['editor']})
    assert ed.level(world['a']['id']) == 'write'
    # bob holds write on paper but is no editor: he can only read
    assert client.put(f"/api/library/collections/{world['a']['id']}/grants", json={'username': 'bob', 'level': 'write'}).status_code == 200
    assert S.for_user({'username': 'bob', 'role': 'viewer', 'extra_roles': []}).level(world['a']['id']) == 'read'
    rows = {g['who']: g for g in client.get(f"/api/library/collections/{world['a']['id']}/grants").json()}
    assert rows['bob']['has_role'] is False and rows['ed']['has_role'] is True


def test_tokens_are_read_only_and_a_token_sees_only_its_collections(client, world):
    sc = S.for_token(world['token_id'], 'lib-token')
    assert sc.level(world['b']['id']) == 'read' and sc.level(world['a']['id']) is None
    assert [c['name'] for c in S.visible(sc)] == [f'Bravo {MARK_B}']
    with pytest.raises(Exception) as e:
        S.open_collection(sc, world['a']['id'])
    assert e.value.status_code == 404 and MARK_A not in str(e.value.detail)
    _as(client, 'ann')
    assert client.put(f"/api/library/collections/{world['a']['id']}/grants", json={'token_id': world['token_id'], 'level': 'write'}).status_code == 422


def test_the_last_manager_cannot_be_removed_and_deleting_a_user_or_token_removes_their_grants(client, world):
    _as(client, 'ann')
    g = client.get(f"/api/library/collections/{world['a']['id']}/grants").json()
    ann = next(x for x in g if x['who'] == 'ann')
    assert client.delete(f"/api/library/collections/{world['a']['id']}/grants/{ann['id']}").status_code == 422
    assert client.put(f"/api/library/collections/{world['a']['id']}/grants", json={'username': 'ann', 'level': 'read'}).status_code == 422
    with db.conn() as c:
        c.execute("DELETE FROM api_token WHERE id=%s", (world['token_id'],))
        assert c.execute('SELECT count(*) AS n FROM library.grant_ WHERE token_id=%s', (world['token_id'],)).fetchone()['n'] == 0


def test_the_library_can_be_switched_off(client, world):
    _admin(client)
    assert client.put('/api/admin/settings', json={'name': 'LIBRARY_ENABLED', 'value': False}).status_code == 200
    try:
        assert client.get('/api/library/collections').status_code == 404
        assert client.get('/api/status').json()['library'] is False
    finally:
        client.put('/api/admin/settings', json={'name': 'LIBRARY_ENABLED', 'value': True})
    assert client.get('/api/library/collections').status_code == 200


def test_a_user_with_a_name_in_a_collection_name_is_not_found_by_slug_guessing(client, world):
    _as(client, 'eve')
    assert client.patch('/api/library/collections/alpha-' + MARK_A.lower(), json={'name': 'x'}).status_code == 404
