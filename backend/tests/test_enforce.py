import pytest

from studio import access, enforce, ontology

BASIC = access.normalize_user({})                       # logged-in user: confidential + personal, no restricted
NOPERS = access.normalize_user({'personal': False})
FULL = access.FULL


@pytest.fixture(scope='module')
def schema(client):
    return enforce.Schema(ontology.assemble(ontology.published_id())['entities'])


def ok(schema, sql, ent=BASIC):
    return enforce.check(sql, schema, ent)


def refused(schema, sql, ent=BASIC, match=None):
    with pytest.raises(enforce.Refused, match=match):
        enforce.check(sql, schema, ent)


def test_a_normal_query_passes_and_is_regenerated(schema):
    out, info = ok(schema, 'select segment, count(*) as n from crm.customer group by segment order by n desc')
    assert out.lower().startswith('select') and info['hashed'] == []


def test_only_one_read_only_statement(schema):
    refused(schema, 'select 1; select 2', match='one statement')
    for bad in ('drop table crm.customer', 'update crm.customer set segment = 1', "copy crm.customer to '/tmp/x'", 'insert into crm.customer values (1)', 'select * into x from crm.customer'):
        refused(schema, bad, match='read-only|cannot parse')


def test_only_modelled_tables_and_no_system_functions(schema):
    refused(schema, 'select * from pg_catalog.pg_user', match='does not exist|Use only')
    refused(schema, 'select table_name from information_schema.tables', match='does not exist|Use only')
    refused(schema, "select pg_read_file('/etc/passwd')", match='not available')
    refused(schema, "select current_setting('is_superuser')", match='not available')


def test_restricted_columns_need_the_restricted_entitlement(schema):
    refused(schema, 'select passport_number from crm.person', match='not available to this credential')
    refused(schema, 'select * from crm.person', match='not available to this credential')
    ok(schema, 'select passport_number from crm.person', FULL)


def test_columns_are_traced_through_aliases_ctes_and_subqueries(schema):
    refused(schema, 'select p.passport_number from crm.person as p', match='not available')
    refused(schema, 'with x as (select passport_number as pn from crm.person) select pn from x', match='not available')
    refused(schema, 'select pn from (select passport_number pn from crm.person) t', match='not available')


def test_a_confidential_column_needs_its_entitlement(schema):
    refused(schema, 'select invoice_amount from billing.invoice', access.normalize_user({'confidential': False}), match='not available')
    ok(schema, 'select invoice_amount from billing.invoice')


def test_masked_columns_are_hashed_without_the_entitlement(schema):
    out, info = ok(schema, 'select id, email from crm.customer', NOPERS)
    assert info['hashed'] == ['customer.email'] and 'sha256' in out.lower() and '"email"' in out
    raw, info2 = ok(schema, 'select id, email from crm.customer', BASIC)           # entitled: the raw value
    assert info2['hashed'] == [] and 'sha256' not in raw.lower()


def test_masked_columns_can_be_counted_grouped_and_ordered_but_not_filtered_or_transformed(schema):
    ok(schema, 'select count(distinct email) from crm.customer', NOPERS)
    ok(schema, 'select email, count(*) from crm.customer group by email order by email', NOPERS)
    refused(schema, "select id from crm.customer where email = 'a@b.c'", NOPERS, match='masked')
    refused(schema, 'select lower(email) from crm.customer', NOPERS, match='masked')
    refused(schema, 'select max(email) from crm.customer', NOPERS, match='masked')
    refused(schema, 'select e from (select email as e from crm.customer) t', NOPERS, match='masked')
    refused(schema, "select id from crm.customer where email like '%gmail%'", NOPERS, match='masked')


def test_star_is_refused_when_a_table_has_columns_the_caller_may_not_read(schema):
    refused(schema, 'select * from crm.person', match='not available')


def test_a_whole_row_is_refused_because_it_would_carry_unchecked_columns(schema):
    """`SELECT c FROM crm.customer c` returns every column of the row, restricted and masked ones included: only named columns pass."""
    for sql in ('select c from crm.customer c', 'select to_json(c) from crm.customer c', 'select row_to_json(c.*) from crm.customer c',
                'select (select t from crm.customer t limit 1)', 'select count(c.*) from crm.customer c', 'select (c).email from crm.customer c',
                'select id from crm.customer c where c is not null'):
        refused(schema, sql, BASIC)
        refused(schema, sql, NOPERS)
    from studio import drivers
    duck = drivers.DuckDBDriver.__new__(drivers.DuckDBDriver)
    for sql in ("select columns('.*') from crm.customer", 'select c from crm.customer c', 'select struct_pack(c) from crm.customer c'):
        with pytest.raises(enforce.Refused):
            enforce.check(sql, schema, FULL, drv=duck)                                 # DuckDB spells a whole row its own ways
    ok(schema, 'select count(*) from crm.customer')                                   # the one star that names no column
    ok(schema, 'select row_to_json(row(c.id, c.status)) from crm.customer c')  # explicit columns in a row are checked like any other


def test_functions_that_run_sql_or_reach_the_server_are_refused(schema):
    """A function that executes a query of its own steps around every column check: only functions that compute from their arguments can be called."""
    for sql in ("select query_to_xml('select * from crm.customer', true, false, '')", "select pg_catalog.query_to_xml('select 1', true, false, '')",
                "select table_to_xml('crm.customer'::regclass, true, false, '')", "select database_to_xml(true, false, '')",
                "select ts_stat('select to_tsvector(email) from crm.customer')", "select ts_rewrite('a'::tsquery, 'select 1')",
                "select lo_import('/etc/passwd')", "select lo_get(1)", "select inet_server_addr()", "select txid_current()", "select current_query()",
                "select dblink_connect('x')", "select nextval('s')", 'select "query_to_xml"(\'select 1\', true, false, \'\')', "select my_own_function(id) from crm.customer"):
        refused(schema, sql, FULL, match='not available')
    for fn in ('query_to_xml', 'table_to_xml', 'lo_import', 'inet_server_addr', 'txid_current', 'current_query', 'ts_stat', 'dblink_connect', 'pg_read_file', 'cursor_to_xml'):
        assert enforce._default_driver().forbidden.match(fn), fn                    # the prefixes really are prefixes
    for sql in ("select age(now(), created_at) from crm.customer", "select date_trunc('month', created_at), count(*) from crm.customer group by 1",
                "select coalesce(status, 'x'), upper(status), length(status) from crm.customer",
                "select jsonb_build_object('id', id) from crm.customer", "select id, row_number() over (order by created_at) from crm.customer"):
        ok(schema, sql)


def test_a_masked_column_cannot_be_matched_against_values_or_readable_columns(schema):
    """Joining a masked column to values the caller wrote (or to a readable column) is a lookup: it would say which row holds which value."""
    refused(schema, "select s.v, c.id from crm.customer c join (values ('a@b.c')) as s(v) on c.email = s.v", NOPERS, match='masked')
    refused(schema, "with g as (select 'a@b.c' as v) select c.id from crm.customer c join g on c.email = g.v", NOPERS, match='masked')
    refused(schema, 'select c.id from crm.customer c join crm.customer d on c.email = d.status', NOPERS, match='masked')
    ok(schema, 'select c.id, d.id from crm.customer c join crm.customer d on c.email = d.email', NOPERS)      # two masked columns: equal values already show as equal hashes
    ok(schema, "select s.v, c.id from crm.customer c join (values ('a@b.c')) as s(v) on c.email = s.v", BASIC)  # entitled: an ordinary join


def test_ordering_by_a_masked_column_orders_by_its_hash(schema):
    """The order of the real values would give away their rank; the hash orders them without saying anything."""
    out, _ = ok(schema, 'select id from crm.customer order by email', NOPERS)
    assert 'ORDER BY' in out and 'sha256' in out.lower().split('order by')[1]
    out, _ = ok(schema, 'select id, row_number() over (order by email) from crm.customer', NOPERS)
    assert 'sha256' in out.lower().split('over')[1]
    raw, _ = ok(schema, 'select id from crm.customer order by email', BASIC)
    assert 'sha256' not in raw.lower()
