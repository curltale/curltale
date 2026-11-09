import pytest
from studio import config, db, ratelimit, settings


@pytest.fixture(autouse=True)
def clean(dbmod):
    yield
    with db.conn() as c:
        c.execute("DELETE FROM app_setting WHERE key LIKE 'cfg.%%'")
    settings.forget()


def test_page_value_wins_over_the_environment_and_reset_goes_back(dbmod):
    env = settings.get('AGENT_MAX_ROWS')
    settings.put('AGENT_MAX_ROWS', 40, 'tester')
    assert settings.get('AGENT_MAX_ROWS') == 40 and settings.source('AGENT_MAX_ROWS') == 'page'
    settings.reset('AGENT_MAX_ROWS', 'tester')
    assert settings.get('AGENT_MAX_ROWS') == env and settings.source('AGENT_MAX_ROWS') in ('env', 'default')
    with db.conn() as c:
        assert [r['key'] for r in c.execute("SELECT key FROM audit_log WHERE action='setting' AND key='AGENT_MAX_ROWS' ORDER BY id DESC LIMIT 2").fetchall()] == ['AGENT_MAX_ROWS'] * 2


@pytest.mark.parametrize('name,value', [('ALFRED_MAX_LOOKUPS', 99), ('ALFRED_MAX_LOOKUPS', 'ten'), ('ALFRED_MAX_LOOKUPS', 2.5), ('CHAT_REASONING', 'yes'),
                                        ('CHAT_MODEL', '  '), ('NOT_A_SETTING', 1), ('DATABASE_URL', 'x')])
def test_bad_values_are_refused(dbmod, name, value):
    with pytest.raises(settings.SettingError):
        settings.put(name, value, 'tester')


def test_the_api_key_is_stored_encrypted_and_never_listed(dbmod):
    settings.put('OPENROUTER_API_KEY', 'sk-test-123', 'tester')
    with db.conn() as c:
        raw = c.execute("SELECT value::text AS v FROM app_setting WHERE key='cfg.OPENROUTER_API_KEY'").fetchone()['v']
    assert 'sk-test-123' not in raw and settings.get('OPENROUTER_API_KEY') == 'sk-test-123'
    row = next(s for s in settings.listing()['settings'] if s['name'] == 'OPENROUTER_API_KEY')
    assert row['value'] is None and row['is_set'] is True and 'sk-test' not in str(settings.listing())


def test_rate_limits_apply_without_a_restart(dbmod):
    settings.put('ALFRED_PER_MIN', 2, 'tester')
    assert ratelimit.ALFRED.burst == 2
    assert [ratelimit.ALFRED.allow('settings-test')[0] for _ in range(3)] == [True, True, False]
    settings.reset('ALFRED_PER_MIN', 'tester')
    assert ratelimit.ALFRED.burst == settings.get('ALFRED_PER_MIN')


def test_settings_page_api(client):
    import os
    pw = os.environ['INITIAL_ADMIN_PASSWORD']                                   # set by conftest
    assert client.post('/api/login', json={'username': 'admin', 'password': pw}).status_code == 200
    names = {s['name'] for s in client.get('/api/admin/settings').json()['settings']}
    assert {'CHAT_MODEL', 'FOUR_EYES', 'RATE_PER_S'} <= names and not names & {'SECRET_KEY', 'DATABASE_URL', 'EMBEDDING_MODEL', 'TRUSTED_PROXIES'}
    r = client.put('/api/admin/settings', json={'name': 'SESSION_HOURS', 'value': 9999})
    assert r.status_code == 400 and 'between' in r.json()['detail']
    assert client.put('/api/admin/settings', json={'name': 'SESSION_HOURS', 'value': 8}).status_code == 200
    assert client.delete('/api/admin/settings/SESSION_HOURS').status_code == 200


def test_the_ai_endpoint_is_any_openai_compatible_server(client):
    from studio import llm
    base = settings.get('LLM_BASE_URL')
    try:
        settings.put('LLM_BASE_URL', 'http://ollama.internal:11434/v1/', 'tester')
        settings.put('OPENROUTER_API_KEY', 'sk-test-123', 'tester')
        assert llm.base_url() == 'http://ollama.internal:11434/v1' and not llm.is_openrouter()
        assert llm.headers()['Authorization'] == 'Bearer sk-test-123'
        assert llm.extras() == {}                                   # OpenRouter-only request fields are not sent to other servers
        settings.reset('OPENROUTER_API_KEY', 'tester')
        assert llm.available() and 'Authorization' not in llm.headers()     # a local server needs no key
        settings.reset('LLM_BASE_URL', 'tester')
        assert llm.is_openrouter() and not llm.available() and llm.extras() == {'reasoning': {'enabled': False}}   # OpenRouter does need one
        assert llm.model_for('writer') == settings.get('CHAT_MODEL')
    finally:
        settings.reset('LLM_BASE_URL', 'tester'); settings.reset('OPENROUTER_API_KEY', 'tester')


def test_the_endpoint_test_reports_failure_without_the_key(client):
    from studio import llm
    settings.put('LLM_BASE_URL', 'http://127.0.0.1:9/v1', 'tester'); settings.put('OPENROUTER_API_KEY', 'sk-secret-xyz', 'tester')
    try:
        r = llm.test()
        assert r['ok'] is False and 'sk-secret-xyz' not in str(r) and r['error']
    finally:
        settings.reset('LLM_BASE_URL', 'tester'); settings.reset('OPENROUTER_API_KEY', 'tester')


def test_vector_storage_follows_the_embedding_dimension():
    from studio import vectors
    assert vectors.spec(768) == {'type': 'vector(768)', 'ops': 'vector_cosine_ops', 'cast': 'vector(768)'}
    assert vectors.spec(2000)['type'] == 'vector(2000)' and vectors.spec(2001)['type'] == 'halfvec(2001)' and vectors.spec(4000)['ops'] == 'halfvec_cosine_ops'
    assert vectors.spec(4001)['ops'] is None                        # too big to index: exact search
    with pytest.raises(ValueError):
        vectors.spec(0)


def test_a_database_made_for_another_dimension_is_converted_and_its_vectors_emptied():
    from studio import vectors
    with db.conn() as c:
        c.execute('SAVEPOINT s')
        assert vectors.column_dim(c, 'chunk') == config.EMBEDDING_DIM
        assert vectors.ensure_dimension(c) is False
        other = config.EMBEDDING_DIM + 256
        assert vectors.ensure_dimension(c, other) is True
        assert vectors.column_dim(c, 'chunk') == other and vectors.column_dim(c, 'embedding_cache') == other
        assert c.execute('SELECT count(*) AS n FROM embedding_cache').fetchone()['n'] == 0
        c.execute('ROLLBACK TO SAVEPOINT s')                        # leave the test database as it was
        assert vectors.column_dim(c, 'chunk') == config.EMBEDDING_DIM
