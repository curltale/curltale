"""Administering users without an email service: reset a password, disable, delete, and the way back in from the server."""
import os

import pytest

from studio import auth, db, passwd


def _admin(client):
    client.post('/api/logout')
    assert client.post('/api/login', json={'username': 'admin', 'password': os.environ['INITIAL_ADMIN_PASSWORD']}).status_code == 200


def _make(client, name, role='viewer', pw='temporary-password-1'):
    r = client.post('/api/admin/users', json={'username': name, 'password': pw, 'role': role})
    assert r.status_code == 200, r.text


def _audit(client, action, target):
    rows = client.get('/api/admin/audit', params={'kind': 'user'}).json()
    return [r for r in rows if r['action'] == action and r['key'] == f'user:{target}']


def test_an_admin_resets_a_password_and_it_is_shown_once(client):
    _admin(client)
    _make(client, 'forgetful')
    token, _u = auth.login('forgetful', 'temporary-password-1')
    for _ in range(auth.LOCK_FAILS):
        auth.login('forgetful', 'wrong-password', '10.9.9.9')                         # the account is locked out now
    r = client.post('/api/admin/users/forgetful/password')                              # nothing given: a password is generated
    assert r.status_code == 200 and r.json()['generated'] is True
    pw = r.json()['password']
    assert len(pw) == 20 and auth.password_problem(pw, 'forgetful') is None
    assert auth.user_from_token(token) is None                                          # their sessions ended
    got = auth.login('forgetful', pw, '10.9.9.8')                                       # the lockout is lifted and the new password works
    assert got is not None and got[1]['must_change'] is True                            # ...but they must choose their own
    assert auth.login('forgetful', 'temporary-password-1', '10.9.9.8') is None          # the old one is gone
    rows = _audit(client, 'password_reset', 'forgetful')
    assert rows and pw not in str(rows)                                                 # recorded, and the password is not
    r = client.post('/api/admin/users/forgetful/password', json={'password': 'chosen-by-the-admin-1'})
    assert r.json()['generated'] is False and r.json()['password'] == 'chosen-by-the-admin-1'
    assert client.post('/api/admin/users/forgetful/password', json={'password': 'short'}).status_code == 422
    assert client.post('/api/admin/users/admin/password').status_code == 422            # your own: use Change password
    assert client.post('/api/admin/users/nobody/password').status_code == 404


def test_only_admins_reset_passwords(client):
    _admin(client)
    _make(client, 'plain-user')
    client.post('/api/logout')
    assert client.post('/api/login', json={'username': 'plain-user', 'password': 'temporary-password-1'}).status_code == 200
    assert client.post('/api/me/password', json={'current': 'temporary-password-1', 'new': 'my-own-password-123'}).status_code == 200
    assert client.post('/api/admin/users/plain-user/password').status_code == 403
    assert client.put('/api/admin/users/plain-user/disabled', json={'disabled': True}).status_code == 403
    assert client.delete('/api/admin/users/plain-user').status_code == 403
    client.post('/api/logout')


def test_a_disabled_user_cannot_sign_in_and_their_session_ends(client):
    _admin(client)
    _make(client, 'leaver', 'modeller')
    token, _u = auth.login('leaver', 'temporary-password-1')
    assert client.put('/api/admin/users/leaver/disabled', json={'disabled': True}).json() == {'ok': True, 'disabled': True}
    assert auth.user_from_token(token) is None
    bad = client.post('/api/login', json={'username': 'leaver', 'password': 'temporary-password-1'})
    unknown = client.post('/api/login', json={'username': 'never-existed', 'password': 'temporary-password-1'})
    assert bad.status_code == unknown.status_code == 401 and bad.json() == unknown.json()          # it looks like any wrong password
    row = next(u for u in client.get('/api/admin/users').json() if u['username'] == 'leaver')
    assert row['disabled'] is True and row['role'] == 'modeller'                                   # role and history stay
    assert client.put('/api/admin/users/leaver/disabled', json={'disabled': False}).status_code == 200
    assert auth.login('leaver', 'temporary-password-1') is not None
    assert _audit(client, 'disable', 'leaver') and _audit(client, 'enable', 'leaver')


def test_the_last_administrator_who_can_sign_in_cannot_be_removed_by_any_route(client):
    _admin(client)
    assert client.put('/api/admin/users/admin/disabled', json={'disabled': True}).status_code == 422       # not even yourself
    _make(client, 'second-admin', 'admin')
    assert client.put('/api/admin/users/second-admin/disabled', json={'disabled': True}).status_code == 200
    assert client.put('/api/admin/users/admin/roles', json={'role': 'viewer'}).status_code == 422          # the other administrator is disabled: this is the last one
    assert client.put('/api/admin/users/second-admin/disabled', json={'disabled': False}).status_code == 200
    assert client.put('/api/admin/users/second-admin/disabled', json={'disabled': True}).status_code == 200  # two administrators: one may go
    assert client.delete('/api/admin/users/admin').status_code == 422
    assert client.delete('/api/admin/users/second-admin').status_code == 200
    assert client.put('/api/admin/users/admin/roles', json={'role': 'viewer'}).status_code == 422


def test_creating_an_existing_name_is_refused_instead_of_overwriting(client):
    _admin(client)
    _make(client, 'taken-name', 'viewer')
    r = client.post('/api/admin/users', json={'username': 'taken-name', 'password': 'another-password-12', 'role': 'admin'})
    assert r.status_code == 409 and 'already exists' in r.json()['detail']
    assert auth.login('taken-name', 'temporary-password-1') is not None                                   # untouched
    assert client.post('/api/admin/users', json={'username': ' padded', 'password': 'another-password-12', 'role': 'viewer'}).status_code == 422
    assert _audit(client, 'create', 'taken-name')


def test_a_user_is_deleted_only_when_disabled_and_not_the_only_manager_of_a_collection(client):
    _admin(client)
    _make(client, 'manager-1', 'viewer')
    c = client.post('/api/library/collections', json={'name': 'Users admin test', 'manager': 'manager-1'}).json()
    assert client.delete('/api/admin/users/manager-1').status_code == 422                                  # still enabled
    client.put('/api/admin/users/manager-1/disabled', json={'disabled': True})
    r = client.delete('/api/admin/users/manager-1')
    assert r.status_code == 422 and 'Users admin test' in r.json()['detail']                               # the only manager of a collection
    _make(client, 'manager-2', 'viewer')
    client.post(f"/api/library/collections/{c['id']}/self-grant", json={'level': 'manage'})
    assert client.put(f"/api/library/collections/{c['id']}/grants", json={'username': 'manager-2', 'level': 'manage'}).status_code == 200
    assert client.delete('/api/admin/users/manager-1').status_code == 200
    assert not any(u['username'] == 'manager-1' for u in client.get('/api/admin/users').json())
    with db.conn() as cx:
        assert cx.execute("SELECT count(*) AS n FROM library.grant_ WHERE username='manager-1'").fetchone()['n'] == 0     # the grants went with the account
    assert _audit(client, 'delete', 'manager-1')                                                           # the history keeps the name
    assert client.delete(f"/api/library/collections/{c['id']}").status_code == 200


def test_deleting_a_token_is_audited(client):
    _admin(client)
    tok = client.post('/api/admin/tokens', json={'name': 'to-delete'}).json()
    tid = next(t['id'] for t in client.get('/api/admin/tokens').json() if t['name'] == 'to-delete')
    assert client.delete(f'/api/admin/tokens/{tid}').status_code == 200 and tok['token']
    rows = client.get('/api/admin/audit', params={'kind': 'token'}).json()
    assert any(r['action'] == 'delete' and r['key'] == 'token:to-delete' for r in rows)


def test_the_server_command_gives_the_way_back_in(client, capsys):
    _admin(client)
    _make(client, 'only-admin', 'admin')
    client.put('/api/admin/users/only-admin/disabled', json={'disabled': True})
    token, _u = auth.login('admin', os.environ['INITIAL_ADMIN_PASSWORD'])
    assert passwd.main(['--list']) == 0 and 'only-admin' in capsys.readouterr().out
    assert passwd.main(['only-admin']) == 0
    out = capsys.readouterr().out
    pw = out.split(': ', 1)[1].split('\n', 1)[0]
    got = auth.login('only-admin', pw)
    assert got is not None and got[1]['must_change'] is True                                               # enabled again, new temporary password
    assert passwd.main(['no-such-user']) == 1 and passwd.main([]) == 2
    with db.conn() as c:
        row = c.execute("SELECT username, after FROM audit_log WHERE action='password_reset' AND key='user:only-admin' ORDER BY id DESC LIMIT 1").fetchone()
    assert row['username'] == 'server command' and pw not in str(row)
    assert auth.user_from_token(token) is not None                                                         # other accounts are not touched
    client.post('/api/logout')


def test_generated_passwords_are_strong_and_different():
    seen = {auth.generate_password() for _ in range(50)}
    assert len(seen) == 50 and all(len(p) == 20 and auth.password_problem(p) is None for p in seen)
    assert not any(ch in ''.join(seen) for ch in 'lI0O1')                                                  # no look-alikes
