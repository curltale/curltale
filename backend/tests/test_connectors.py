from urllib.parse import urlparse

import pytest

from studio import config, connectors, editing, ontology, reverse, versions


@pytest.fixture
def conn_id(datadb):
    """A connector pointed at the generated test database, with the account the test suite already has."""
    u = urlparse(config.DATABASE_URL)
    c = connectors.create('test-db', {'host': u.hostname, 'port': u.port, 'database': config.DATA_DB, 'user': u.username, 'sslmode': 'disable'}, u.password)
    yield c['id']
    connectors.delete(c['id'])


def test_password_is_encrypted_and_never_returned(conn_id):
    row = connectors.get(conn_id)
    assert row['secret'] and 'password' not in str(connectors.public(row)).lower().replace('has_password', '')
    assert connectors.decrypt(row['secret']) == urlparse(config.DATABASE_URL).password


def test_the_app_own_database_is_refused():
    u = urlparse(config.DATABASE_URL)
    with pytest.raises(connectors.ConnectorError, match='own database'):
        connectors.create('self', {'host': u.hostname, 'port': u.port, 'database': u.path.lstrip('/'), 'user': u.username}, u.password)


def test_connection_test_reports_account_risks(conn_id):
    r = connectors.test(conn_id)
    assert r['ok'] and r['tables_visible'] >= 23 and isinstance(r['warnings'], list)


def test_a_wrong_password_gives_a_clean_message(conn_id):
    connectors.update(conn_id, password='definitely-wrong')
    r = connectors.test(conn_id)
    assert not r['ok'] and 'definitely-wrong' not in r['error']


def test_introspection_reads_tables_keys_and_links(conn_id):
    cat = connectors.introspect(conn_id)
    tabs = {f"{t['schema']}.{t['name']}": t for t in cat['tables']}
    assert len(tabs) >= 23 and tabs['crm.customer']['pk'] == ['id']
    assert any(fk['ref_table'] == 'customer' for t in tabs.values() for fk in t['fks'])
    assert any(c.get('values') for t in tabs.values() for c in t['columns']), 'small value sets come from the statistics'


def test_a_draft_is_generated_that_passes_the_publish_checks(conn_id):
    cat = connectors.introspect(conn_id)
    model, notes = reverse.generate(cat, 'test-db', [f"{t['schema']}.{t['name']}" for t in cat['tables'] if t['schema'] not in ('analytics', 'reference')])
    assert len(model['entities']) == 21 and len(model['relationships']) >= 30
    assert editing.validate_model(model) == []
    cust = model['entities']['customer']
    assert cust['semantic_table'] == 'crm.customer' and cust['attributes']['id']['role'] == 'primary_key'
    assert cust['attributes']['account_manager_id']['role'] == 'foreign_key' and cust['attributes']['account_manager_id']['references'] == 'Employee'
    assert all(a['agent_access'] == 'none' for e in model['entities'].values() for a in e['attributes'].values() if a['sensitivity'] == 'restricted')
    assert any(a['sensitivity'] == 'personal' and a['agent_access'] == 'masked' for e in model['entities'].values() for a in e['attributes'].values())


def test_generating_makes_a_draft_and_will_not_overwrite_one(conn_id):
    cat = connectors.introspect(conn_id)
    model, _ = reverse.generate(cat, 'test-db', ['crm.customer', 'org.employee'])
    vid = versions.create_draft_from_model(model, 'tester', 'from db', replace_draft=True)
    assert ontology.draft_id() == vid
    with pytest.raises(editing.EditError):
        versions.create_draft_from_model(model, 'tester', 'again')
    versions.discard_draft()


def _activate(cid):
    from studio import datastore, db
    with db.conn() as c:
        c.execute('UPDATE connector SET active=false WHERE active')
        c.execute('UPDATE connector SET active=true WHERE id=%s', (cid,))
    datastore.reset_pools()


def _deactivate():
    from studio import datastore, db
    with db.conn() as c:
        c.execute('UPDATE connector SET active=false WHERE active')
    datastore.reset_pools()


def test_queries_run_through_the_active_connector_with_enforcement_and_masking(conn_id):
    from studio import access, datastore, tools
    _activate(conn_id)
    try:
        nopers = access.normalize_user({'personal': False})
        r = datastore.console('select id, email from crm.customer order by id limit 3', nopers)
        assert r['masked_columns'] == ['customer.email']
        assert all(row['email'] is None or str(row['email']).startswith('h_') for row in r['rows'])
        r2 = datastore.console('select id, email from crm.customer order by id limit 3', nopers)
        assert r['rows'] == r2['rows'], 'a stable hash: the same value gives the same hash every time'
        with pytest.raises(datastore.DataError, match='masked'):
            datastore.console("select id from crm.customer where email = 'x'", nopers)
        with pytest.raises(datastore.DataError, match='not available'):
            datastore.console('select passport_number from crm.person', nopers)
        assert datastore.console('select count(*) n from crm.customer', nopers)['rows'][0]['n'] > 0
        ov = datastore.overview(datastore.current_catalog())
        assert len(ov) == 23 and all(t['rows'] > 0 for t in ov)
        pg = datastore.page(datastore.current_catalog(), 'crm.customer', nopers, size=5)
        assert any(c['masked'] for c in pg['columns']) and all(str(row['email']).startswith('h_') for row in pg['rows'] if row.get('email'))
    finally:
        _deactivate()


def test_a_connector_can_forbid_agents_from_reading_rows(conn_id):
    from studio import access, tools
    connectors.update(conn_id, options={'agents_may_read_rows': False})
    _activate(conn_id)
    try:
        access.set_caller('agent', {'data': True})
        r = tools.query_data('select count(*) from crm.customer')
        assert r['error'] == 'not allowed'
    finally:
        _deactivate()
