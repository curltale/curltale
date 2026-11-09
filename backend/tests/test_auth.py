import time

import pytest

from studio import auth, db, versions


def _user(name, pw, role='modeller'):
    with db.conn() as c:
        c.execute('INSERT INTO app_user (username,password_hash,role) VALUES (%s,%s,%s) ON CONFLICT (username) DO UPDATE SET password_hash=EXCLUDED.password_hash, role=EXCLUDED.role, must_change=false',
                  (name, auth.hash_password(pw), role))
        c.execute('DELETE FROM login_attempt WHERE username=%s', (name,))


def _login(client, name, pw):
    return client.post('/api/login', json={'username': name, 'password': pw})


def test_repeated_failures_lock_the_username_even_for_the_right_password(client):
    _user('lock-me', 'a-long-enough-password')
    for _ in range(auth.LOCK_FAILS):
        assert auth.login('lock-me', 'wrong', '10.0.0.1') is None
    with pytest.raises(auth.LoginBlocked):
        auth.login('lock-me', 'a-long-enough-password', '10.0.0.2')
    r = _login(client, 'lock-me', 'a-long-enough-password')
    assert r.status_code == 429 and 'try again' in r.json()['detail']
    with db.conn() as c:
        c.execute("DELETE FROM login_attempt WHERE username='lock-me'")
    assert auth.login('lock-me', 'a-long-enough-password', '10.0.0.2') is not None


def test_an_unknown_username_looks_like_a_wrong_password(client):
    r = _login(client, 'nobody-here', 'whatever-password')
    assert r.status_code == 401 and r.json()['detail'] == 'wrong username or password'


def test_a_short_password_must_be_changed_before_anything_else(client):
    _user('shorty', 'short1')
    r = _login(client, 'shorty', 'short1')
    assert r.status_code == 200 and r.json()['must_change'] is True
    assert client.get('/api/status').status_code == 403
    assert client.get('/api/me').status_code == 200
    assert client.post('/api/me/password', json={'current': 'short1', 'new': 'too-short'}).status_code == 422
    assert client.post('/api/me/password', json={'current': 'wrong', 'new': 'a-much-better-password'}).status_code == 422
    assert client.post('/api/me/password', json={'current': 'short1', 'new': 'a-much-better-password'}).status_code == 200
    assert client.get('/api/status').status_code == 200
    client.post('/api/logout')


def test_changing_the_password_ends_the_other_sessions(client):
    _user('two-sessions', 'first-password-is-long')
    other, _u = auth.login('two-sessions', 'first-password-is-long')
    assert auth.user_from_token(other)
    _login(client, 'two-sessions', 'first-password-is-long')
    assert client.post('/api/me/password', json={'current': 'first-password-is-long', 'new': 'second-password-is-long'}).status_code == 200
    assert auth.user_from_token(other) is None and client.get('/api/me').status_code == 200
    client.post('/api/logout')


def test_roles_viewer_reads_modeller_edits_publisher_publishes(client):
    for name, role in (('v1', 'viewer'), ('m1', 'modeller'), ('p1', 'publisher')):
        _user(name, 'role-test-password', role)
    _login(client, 'v1', 'role-test-password')
    assert client.get('/api/entities').status_code == 200
    assert client.post('/api/versions/draft', json={'label': 'x'}).status_code == 403
    _login(client, 'm1', 'role-test-password')
    assert client.post('/api/versions/draft', json={'label': 'roles'}).status_code == 200
    assert client.post('/api/versions/publish').status_code == 403
    assert client.delete('/api/versions/draft').status_code == 403
    _login(client, 'p1', 'role-test-password')
    assert client.delete('/api/versions/draft').status_code == 200
    client.post('/api/logout')


def test_four_eyes_stops_the_editor_from_publishing_their_own_draft(client):
    _user('p2', 'role-test-password', 'publisher')
    with db.conn() as c:
        c.execute("INSERT INTO app_setting (key,value) VALUES ('four_eyes','true') ON CONFLICT (key) DO UPDATE SET value='true'")
    try:
        _login(client, 'p2', 'role-test-password')
        assert client.post('/api/versions/draft', json={'label': '4eyes'}).status_code == 200
        k = client.get('/api/examples?version=draft').json()[0]
        assert client.put(f"/api/examples/{k['id']}", json={'data': {**k, 'body': k['body'] + ' edited'}}).status_code == 200
        r = client.post('/api/versions/publish')
        assert r.status_code == 422 and 'four-eyes' in r.json()['detail']
        assert 'p2' in client.get('/api/status').json()['draft_editors']
    finally:
        with db.conn() as c:
            c.execute("UPDATE app_setting SET value='false' WHERE key='four_eyes'")
        versions.discard_draft()
        client.post('/api/logout')


def test_an_admin_set_password_is_temporary(client):
    _login(client, 'admin', __import__('os').environ['INITIAL_ADMIN_PASSWORD'])
    assert client.post('/api/admin/users', json={'username': 'newbie', 'password': 'temporary-password-1', 'role': 'viewer'}).status_code == 200
    assert client.post('/api/admin/users', json={'username': 'newbie2', 'password': 'short', 'role': 'viewer'}).status_code == 422
    assert client.post('/api/admin/users', json={'username': 'newbie3', 'password': 'temporary-password-1', 'role': 'emperor'}).status_code == 422
    client.post('/api/logout')
    assert _login(client, 'newbie', 'temporary-password-1').json()['must_change'] is True
    client.post('/api/logout')


def test_extra_role_editor_is_separate_from_the_ladder_and_admin_has_it(client):
    assert auth.roles_of({'role': 'viewer', 'extra_roles': []}) == ['viewer']
    assert auth.roles_of({'role': 'publisher', 'extra_roles': ['editor']}) == ['viewer', 'modeller', 'publisher', 'editor']
    assert 'editor' in auth.roles_of({'role': 'admin', 'extra_roles': []})
    assert auth.roles_of({'role': 'modeller', 'extra_roles': ['wizard']}) == ['viewer', 'modeller']          # unknown extras are ignored
    _login(client, 'admin', __import__('os').environ['INITIAL_ADMIN_PASSWORD'])
    assert client.post('/api/admin/users', json={'username': 'ed1', 'password': 'temporary-password-1', 'role': 'viewer', 'extra_roles': ['editor']}).status_code == 200
    assert client.post('/api/admin/users', json={'username': 'ed2', 'password': 'temporary-password-1', 'role': 'viewer', 'extra_roles': ['wizard']}).status_code == 422
    row = next(u for u in client.get('/api/admin/users').json() if u['username'] == 'ed1')
    assert row['roles'] == ['viewer', 'editor']
    assert client.put('/api/admin/users/ed1/roles', json={'role': 'modeller', 'extra_roles': []}).json()['roles'] == ['viewer', 'modeller']
    assert client.put('/api/admin/users/nobody/roles', json={'role': 'viewer'}).status_code == 404
    assert client.put('/api/admin/users/admin/roles', json={'role': 'viewer'}).status_code == 422            # never remove the last admin
    client.post('/api/logout')
    _user('plain', 'role-test-password', 'modeller')
    r = _login(client, 'plain', 'role-test-password').json()
    assert r['roles'] == ['viewer', 'modeller'] and client.get('/api/me').json()['roles'] == ['viewer', 'modeller']
    client.post('/api/logout')
