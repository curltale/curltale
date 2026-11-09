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
