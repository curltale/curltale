"""Limits on what a request may send, and the secrets a deployment must set."""
import pytest
from studio import bodylimit, config
from test_library_access import _admin


def test_a_stranger_cannot_make_the_server_read_a_large_body(client):
    client.post('/api/logout')
    big = b'x' * (200 * 1024)
    r = client.post('/api/library/collections/any/sources', files={'file': ('a.md', big)})                 # an upload: refused before a byte is spooled
    assert r.status_code == 401 and 'sign in' in r.json()['detail']
    r = client.put('/api/admin/settings', content=big, headers={'content-type': 'application/json'})
    assert r.status_code == 401 and 'sign in' in r.json()['detail']
    r = client.post('/api/login', content=big, headers={'content-type': 'application/json'})                 # even the sign-in form has a size
    assert r.status_code == 413
    r = client.put('/api/admin/settings', content=iter([big[:50000], big[:50000]]), headers={'content-type': 'application/json'})   # no declared size: cut off while it arrives
    assert r.status_code in (400, 413)                                                                      # (the framework reports a body it could not finish reading as 400)
    assert client.post('/api/login', json={'username': 'nobody', 'password': 'x' * 20}).status_code == 401     # an ordinary sign-in still gets its ordinary answer


def test_a_signed_in_caller_has_a_limit_too(client, monkeypatch):
    _admin(client)
    monkeypatch.setattr(config, 'MAX_BODY_MB', 1)
    r = client.put('/api/admin/settings', content=b'{"name": "CHAT_MODEL", "value": "' + b'x' * (2 * 1024 * 1024) + b'"}', headers={'content-type': 'application/json'})
    assert r.status_code == 413 and '1 MB' in r.json()['detail']
    r = client.put('/api/admin/settings', content=b'{"name": "NO_SUCH", "value": "' + b'x' * (300 * 1024) + b'"}', headers={'content-type': 'application/json'})
    assert r.status_code == 400                                                                             # under the limit and signed in: the request reaches the app
    assert bodylimit.limit_for('/api/library/collections/abc/sources') > 50 * bodylimit.MB                    # uploads keep their own, larger limit
    assert bodylimit.limit_for('/api/admin/import/excel') == 21 * bodylimit.MB and bodylimit.limit_for('/api/login') == bodylimit.ANONYMOUS
    assert client.get('/api/health').status_code == 200


def test_the_app_refuses_to_start_with_guessable_secrets(monkeypatch):
    assert config.secret_problems() == []                                                                   # the suite's own settings pass
    for bad in ('', 'dev-secret', 'change-me-long-random', 'short-but-not-placeholder'):
        monkeypatch.setenv('SECRET_KEY', bad)
        assert any('SECRET_KEY' in p for p in config.secret_problems()), bad
        with pytest.raises(RuntimeError, match='will not start'):
            config.require_secrets()
    monkeypatch.setenv('SECRET_KEY', 'k' * 32)
    assert config.secret_problems() == []
    monkeypatch.setattr(config, 'ADMIN_PASSWORD', 'change-me-12-chars-or-more')
    assert any('INITIAL_ADMIN_PASSWORD' in p for p in config.secret_problems())
    monkeypatch.setattr(config, 'ADMIN_PASSWORD', 'a-real-password-123')
    monkeypatch.setattr(config, 'DATABASE_URL', 'postgresql://studio_app:change-me-too@db:5432/studio')
    assert any('database password' in p for p in config.secret_problems())
