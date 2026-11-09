import os
import sqlite3

import pytest
import sqlglot

from studio import access, connectors, drivers, enforce, reverse
from studio.datastore import Catalog

FILES = os.environ['CONNECTOR_FILES_DIR']
NOPERS = access.normalize_user({'personal': False})
FULLU = access.normalize_user({})
KINDS = list(drivers.KINDS)


def _drv(kind):
    return drivers.KINDS[kind]({'id': 0, 'kind': kind, 'params': {}, 'options': {}, 'secret': None})


@pytest.mark.parametrize('kind', KINDS)
def test_every_dialect_has_a_parsable_stable_hash(kind):
    d = _drv(kind)
    from sqlglot import exp
    h = d.hash_expr(exp.column('email', table='c', quoted=True)).sql(dialect=d.dialect)
    assert 'email' in h and '__' not in h and drivers.mask_salt() in h or kind == 'sqlite'
    assert sqlglot.parse_one(h, read=d.dialect) is not None


@pytest.mark.parametrize('kind', KINDS)
def test_the_models_postgres_sql_translates_to_every_dialect(kind):
    d = _drv(kind)
    out = d.to_dialect("SELECT c.id, count(*) FILTER (WHERE x.s = 'open') AS n, c.name ILIKE '%a%' AS m FROM crm.customer c LEFT JOIN support.x ON x.cid = c.id GROUP BY 1, 3")
    assert out and 'customer' in out


def test_file_databases_are_confined_to_the_data_folder(tmp_path):
    with pytest.raises(connectors.ConnectorError, match='inside'):
        connectors.create('bad', {'path': '/etc/passwd'}, None, kind='sqlite')
    outside = tmp_path / 'x.sqlite'
    sqlite3.connect(outside).close()
    with pytest.raises(connectors.ConnectorError, match='inside'):
        connectors.create('bad', {'path': str(outside)}, None, kind='sqlite')
    link = os.path.join(FILES, 'link.sqlite')
    os.symlink('/etc/passwd', link)
    with pytest.raises(connectors.ConnectorError, match='inside'):
        connectors.create('bad', {'path': link}, None, kind='sqlite')


def _make_sqlite(name='t.sqlite'):
    path = os.path.join(FILES, name)
    if os.path.exists(path):
        os.remove(path)
    c = sqlite3.connect(path)
    c.executescript("""
      create table customer (id text primary key, name text not null, email text, segment text, amount numeric);
      create table orders (id integer primary key, customer_id text references customer(id), status text);
      insert into customer values ('c1','Acme','a@x.com','SMB',10.5), ('c2','Beta','b@x.com','ES',20);
      insert into orders values (1,'c1','open'), (2,'c1','closed'), (3,'c2','open');
    """)
    c.commit(); c.close()
    return path


def test_sqlite_connector_reads_structure_and_runs_read_only(client):
    path = _make_sqlite()
    c = connectors.create('t-sqlite', {'path': path}, None, kind='sqlite')
    try:
        assert connectors.test(c['id'])['ok']
        cat = connectors.introspect(c['id'])
        t = {x['name']: x for x in cat['tables']}
        assert t['customer']['pk'] == ['id'] and t['orders']['fks'][0]['ref_table'] == 'customer'
        drv = connectors.driver(connectors.get(c['id']))
        _cols, rows = drv.run('select count(*) n from orders where status = %s', ['open'])
        assert rows[0]['n'] == 2
        with pytest.raises(drivers.DriverError):
            drv.run("insert into customer values ('c3','x','x','x',1)")
        model, notes = reverse.generate(cat, 't-sqlite')
        assert set(model['entities']) == {'customer', 'orders'} and model['relationships'][0]['to'] == 'Customer'
        sc = Catalog(model).schema
        assert model['entities']['customer']['attributes']['email'].get('agent_access') == 'masked'        # guessed from the column name
        out, info = enforce.check('select id, email from main.customer order by id', sc, NOPERS, drv=drv)
        _c, rows = drv.run(out)
        assert info['hashed'] == ['customer.email'] and rows[0]['email'].startswith('h_') and rows[0]['email'] != rows[1]['email']
        out2, _ = enforce.check('select id, email from main.customer order by id', sc, FULLU, drv=drv)
        assert drv.run(out2)[1][0]['email'] == 'a@x.com'
    finally:
        connectors.delete(c['id'])


def test_duckdb_connector_cannot_read_files_or_write(client):
    import duckdb
    path = os.path.join(FILES, 't.duckdb')
    if os.path.exists(path):
        os.remove(path)
    d = duckdb.connect(path)
    d.execute("create schema crm; create table crm.customer (id varchar primary key, email varchar); insert into crm.customer values ('c1','a@x.com')")
    d.close()
    c = connectors.create('t-duck', {'path': path}, None, kind='duckdb')
    try:
        assert connectors.test(c['id'])['ok']
        cat = connectors.introspect(c['id'])
        assert {(t['schema'], t['name']) for t in cat['tables']} == {('crm', 'customer')}
        drv = connectors.driver(connectors.get(c['id']))
        assert drv.run('select id from crm.customer')[1] == [{'id': 'c1'}]
        for bad in ("select * from read_csv('/etc/passwd')", "copy crm.customer to '/tmp/x.csv'", "insert into crm.customer values ('c2','x')"):
            with pytest.raises(drivers.DriverError):
                drv.run(bad)
    finally:
        connectors.delete(c['id'])


def test_the_checker_refuses_file_and_system_functions_in_every_dialect(client):
    model = {'entities': {'customer': {'semantic_table': 'crm.customer', 'attributes': {'id': {'sensitivity': 'internal', 'agent_access': 'full'}}}}}
    sc = enforce.Schema(model['entities'])
    cases = {'duckdb': ["select * from read_csv('/etc/passwd')", "select read_text('/etc/passwd')"], 'clickhouse': ["select * from file('/etc/passwd')", "select * from url('http://x/y', CSV)"],
             'mysql': ["select load_file('/etc/passwd')", "select sleep(100)"], 'sqlite': ["select load_extension('x')"], 'postgres': ["select pg_sleep(100)"]}
    for kind, sqls in cases.items():
        for sql in sqls:
            with pytest.raises(enforce.Refused):
                enforce.check(sql, sc, FULLU, drv=_drv(kind))


@pytest.mark.parametrize('kind', ['mysql', 'clickhouse', 'duckdb', 'sqlite'])
def test_is_distinct_from_is_rewritten_for_databases_that_lack_it(kind):
    out = _drv(kind).to_dialect('SELECT 1 FROM t WHERE a IS DISTINCT FROM b').upper()
    assert 'DISTINCT FROM' not in out and 'IS NULL' in out


def test_epoch_differences_are_rewritten_for_mysql_and_clickhouse():
    q = 'SELECT avg(extract(epoch FROM resolved - reported) / 3600.0) FROM t'
    assert 'TIMESTAMPDIFF(SECOND, reported, resolved)' in _drv('mysql').to_dialect(q)
    ch = _drv('clickhouse').to_dialect(q).lower()
    assert 'date_diff' in ch.replace('datediff', 'date_diff') and 'reported, resolved' in ch


def test_a_database_that_is_not_the_modelled_one_says_so(client):
    from studio import datastore, db
    path = _make_sqlite('flat.sqlite')
    c = connectors.create('flat', {'path': path}, None, kind='sqlite')
    try:
        connectors.introspect(c['id'])
        cv = connectors.coverage(connectors.get(c['id']))
        assert cv['percent'] < 50 and 'crm.customer' in cv['missing']
        with db.conn() as cx:
            cx.execute('UPDATE connector SET active=true WHERE id=%s', (c['id'],))
        datastore.reset_pools()
        with pytest.raises(datastore.DataError, match='not the database the published model describes'):
            datastore.console('select id from crm.customer', FULLU)
    finally:
        with db.conn() as cx:
            cx.execute('UPDATE connector SET active=false WHERE active')
        datastore.reset_pools()
        connectors.delete(c['id'])
