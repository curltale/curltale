"""Finding 1 of the security review: SQL written in the model (checks, examples) must not become a way around the access rules,
and the Model check page, which every user can open, must not show values."""
import copy
import json

import pytest

from studio import access, conformance, datastore


@pytest.fixture
def secret(datadb):
    _c, rows = datastore.run('SELECT passport_number FROM crm.person WHERE passport_number IS NOT NULL LIMIT 1', None, None, access.FULL)
    return rows[0]['passport_number']


def _cat_with(datadb, check_sql=None, sample_sql=None):
    onto = copy.deepcopy(datadb['onto'])
    if check_sql:
        onto['entities']['customer']['checks'] = [{'id': 'EVIL', 'rule': 'x', 'sql': check_sql}]
    if sample_sql:
        onto['examples'] = [{'id': 'EXEVIL', 'title': 'evil', 'body': '', 'applies_to': [],
                              'sql': 'SELECT id FROM crm.customer WHERE id = :customer_id', 'params': {'customer_id': 'customer'}, 'sample_sql': sample_sql}]
    return datastore.Catalog(onto)


@pytest.mark.parametrize('kind', ['check', 'sample'])
def test_model_sql_cannot_read_restricted_columns_and_the_page_shows_no_values(datadb, secret, kind):
    evil = "SELECT count(*) AS n FROM crm.person WHERE passport_number = 'x'" if kind == 'check' else None
    sample = 'SELECT passport_number AS customer_id FROM crm.person' if kind == 'sample' else None
    res = conformance.run_all(_cat_with(datadb, evil, sample))
    dump = json.dumps(res, default=str)
    assert secret not in dump
    hit = [x for x in res['checks'] if 'refused by the access checker' in (x['detail'] or '')]
    assert hit and all(x['status'] in ('fail', 'warn') for x in hit)


def test_system_functions_are_refused_in_model_sql(datadb):
    res = conformance.run_all(_cat_with(datadb, check_sql="SELECT length(pg_read_file('/etc/hostname')) AS n"))
    x = next(x for x in res['checks'] if x['id'] == 'customer.EVIL')
    assert 'refused' in x['detail']


def test_errors_shown_on_the_page_do_not_echo_values(datadb, secret):
    res = conformance.run_all(_cat_with(datadb, check_sql=f"SELECT CAST('{secret}' AS integer) AS n"))
    x = next(x for x in res['checks'] if x['id'] == 'customer.EVIL')
    assert secret not in x['detail'] and 'does not run' in x['detail']        # the SQL itself is the modeller's own text; the error must not echo the value


def test_examples_show_counts_not_values(datadb):
    res = conformance.run_all(datadb['cat'])
    ex = [x for x in res['checks'] if x['group'] == 'Query examples' and x['status'] == 'pass']
    assert ex and all('{' not in x['detail'] for x in ex)
