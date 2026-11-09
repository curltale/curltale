import json
import pytest
from studio import access, conformance, datastore, settings, tools

BASIC = access.normalize_user({})                                   # what a logged-in user gets: no restricted data
FULL = access.FULL
NOFIN = access.normalize_user({'confidential': False})


def test_every_entity_is_a_table_with_data(datadb):
    ov = datastore.overview(datadb['cat'])
    assert len(ov) == 23 and all(t['rows'] > 0 for t in ov)
    assert datadb['result']['rows'] > 500


@pytest.mark.parametrize('scale,seed', [('small', 1), ('small', 2), ('medium', 7)])
def test_no_table_is_empty_whatever_the_seed(datadb, scale, seed):
    from studio.synth.generic import Generator
    rows = Generator(datadb['onto'], seed=seed, scale=scale).build()
    assert [k for k, v in rows.items() if not v] == []


def test_generated_data_is_deterministic(datadb):
    from studio.synth.generic import Generator
    a = Generator(datadb['onto'], seed=3, scale='small').build()
    b = Generator(datadb['onto'], seed=3, scale='small').build()
    assert {k: len(v) for k, v in a.items()} == {k: len(v) for k, v in b.items()}
    assert [r['id'] for r in a['customer']] == [r['id'] for r in b['customer']]


def test_generated_data_is_identical_for_the_same_seed(datadb):
    from studio.synth.generic import Generator
    onto = datadb['onto']
    strip = lambda R: {k: [{c: v for c, v in r.items() if onto['entities'][k]['attributes'][c]['role'] != 'lineage'} for r in rs] for k, rs in R.items()}
    assert strip(Generator(onto, seed=9, scale='small').build()) == strip(Generator(onto, seed=9, scale='small').build())


@pytest.mark.parametrize('scale,seed', [('small', 4), ('medium', 7)])
def test_links_cover_parents_and_chains_agree(datadb, scale, seed):
    """Every parent gets a child when there are enough children, required links are never empty, and two routes to the same ancestor meet."""
    from studio.synth.generic import Generator
    g = Generator(datadb['onto'], seed=seed, scale=scale)
    rows = g.build()
    rep = g.link_report()
    assert [r for r in rep if r['required'] and r['empty']] == []
    full = [r for r in rep if r['children'] >= r['parents']]
    assert sum(r['parents'] - r['childless'] for r in full) / sum(r['parents'] for r in full) > 0.85
    assert g.chain_conflicts() == 0 and g.broken == 0
    parent = {r['id']: r.get('parent_customer_id') for r in rows['customer']}         # the customer hierarchy is a tree: no loops
    assert any(parent.values())
    for c in parent:
        seen = set()
        while c is not None:
            assert c not in seen
            seen.add(c)
            c = parent.get(c)
        assert len(seen) <= 4                                                          # shallow: groups, not chains
    assert 0.2 < sum(v is None for v in parent.values()) / len(parent) < 0.7


def test_chains_agree_in_the_built_database(datadb):
    """A payment allocation's invoice and payment are on the same billing account, and a subscription's billing account belongs to its own customer
    (checked in SQL on the loaded data)."""
    sql = ('SELECT count(*) AS n, count(*) FILTER (WHERE i.billing_account_id <> p.billing_account_id) AS bad '
           'FROM billing.payment_allocation pa JOIN billing.invoice i ON i.id = pa.invoice_id JOIN billing.payment p ON p.id = pa.payment_id')
    r = datastore.console(sql, FULL)['rows'][0]
    assert r['n'] > 0 and r['bad'] == 0
    sql = ('SELECT count(*) AS n, count(*) FILTER (WHERE b.customer_id <> s.customer_id) AS bad '
           'FROM sales.subscription s JOIN billing.billing_account b ON b.id = s.billing_account_id')
    r = datastore.console(sql, FULL)['rows'][0]
    assert r['n'] > 0 and r['bad'] == 0


def test_the_data_fits_the_model(datadb):
    """Structure and every join hold on generated data. Business rules (the entities' checks) are the company's: random data need not respect them."""
    res = conformance.run_all(datadb['cat'])
    failed = [c for c in res['checks'] if c['status'] == 'fail' and c['group'] in ('Structure', 'Relationships')]
    assert not failed, [(c['id'], c['detail']) for c in failed]
    assert res['summary']['pass'] > 30


def test_generated_values_follow_the_model(datadb):
    """Keys follow the field's example, allowed values are respected, links point at real rows."""
    from studio.synth.generic import Generator
    onto = datadb['onto']
    rows = Generator(onto, seed=5, scale='small').build()
    ex = onto['entities']['customer']['attributes']['id'].get('example')
    if ex:
        assert all(len(str(r['id'])) == len(ex) and str(r['id'])[0] == ex[0] for r in rows['customer'])
    for k, e in onto['entities'].items():
        for c, a in e['attributes'].items():
            if a.get('allowed_values') and rows[k] and a['role'] not in ('foreign_key', 'primary_key') and (k, c) not in {(x, y) for x, y, _t in __import__('studio.synth.ddl', fromlist=['x']).foreign_keys(onto)} and a['type'] == 'text':
                assert {r[c] for r in rows[k] if r.get(c) is not None} <= set(a['allowed_values']), (k, c)
    ids = {r['id'] for r in rows['customer']}
    assert all(r['customer_id'] in ids for r in rows['billing_account'] if r.get('customer_id'))


# ---------------- the console is read-only and respects entitlements
def test_console_reads_but_never_writes(datadb):
    assert datastore.console('SELECT count(*) AS n FROM crm.customer', BASIC)['rows'][0]['n'] > 0
    for bad in ('DELETE FROM crm.customer', 'UPDATE crm.customer SET customer_name=\'x\'', 'DROP TABLE crm.customer', 'INSERT INTO org.employee(id) VALUES (\'x\')',
                'SELECT 1; DROP TABLE crm.customer', 'CREATE TABLE x(a int)', 'TRUNCATE crm.customer'):
        with pytest.raises(datastore.DataError):
            datastore.console(bad, FULL)           # even the fully entitled role is read-only
    with pytest.raises(datastore.DataError):        # a write hidden inside a CTE
        datastore.console('WITH d AS (DELETE FROM org.employee RETURNING id) SELECT * FROM d', FULL)
    assert datastore.console('SELECT count(*) AS n FROM org.employee', FULL)['rows'][0]['n'] > 0


def test_console_cannot_reach_files_other_databases_or_run_forever(datadb):
    for bad in ("SELECT pg_read_file('/etc/passwd')", "SELECT * FROM pg_authid", "SELECT pg_sleep(30)", "SELECT lo_import('/etc/passwd')"):
        with pytest.raises(datastore.DataError):
            datastore.console(bad, BASIC)
    with pytest.raises(datastore.DataError):
        datastore.console('SELECT * FROM studio.public.ontology_version', BASIC)


def test_restricted_columns_follow_the_entitlement(datadb):
    cat = datadb['cat']
    plain = datastore.page(cat, 'crm.person', BASIC)
    assert 'passport_number' not in plain['rows'][0] and any(c['hidden'] for c in plain['columns'])
    full = datastore.page(cat, 'crm.person', FULL)
    assert 'passport_number' in full['rows'][0]
    with pytest.raises(datastore.DataError, match='restricted'):
        datastore.console('SELECT * FROM crm.person', BASIC)                       # refused by the database itself
    assert datastore.console('SELECT id, full_name FROM crm.person LIMIT 3', BASIC)['rows']
    assert datastore.console('SELECT passport_number FROM crm.person WHERE passport_number IS NOT NULL LIMIT 1', FULL)['rows']
    with pytest.raises(datastore.DataError):
        datastore.console('SELECT passport_number FROM crm.person', BASIC)
    with pytest.raises(datastore.DataError):                                              # filtering on a hidden column is no side channel
        datastore.page(cat, 'crm.person', BASIC, filters=[{'col': 'passport_number', 'op': 'contains', 'value': 'P'}])


def test_browser_validates_names_and_navigates_keys(datadb):
    cat = datadb['cat']
    for bad in ('crm.customer; DROP TABLE x', 'nope.nothing'):
        with pytest.raises(datastore.DataError):
            datastore.page(cat, bad, False)
    with pytest.raises(datastore.DataError):                                              # column names come from the model, never from the request
        datastore.page(cat, 'crm.customer', BASIC, filters=[{'col': 'x"; --', 'op': 'eq', 'value': '1'}])
    assert datastore.page(cat, 'crm.customer', BASIC, sort='id; DROP TABLE x')['total'] > 0   # unknown sort column is ignored
    p = datastore.page(cat, 'billing.billing_account', BASIC, size=5)
    fk = next(c for c in p['columns'] if c['name'] == 'customer_id')
    assert fk['fk']['table'] == 'crm.customer'
    cust = p['rows'][0]['customer_id']
    d = datastore.row_detail(cat, 'crm.customer', cust, BASIC)
    assert d['row']['id'] == cust and any(r['table'] == 'billing.billing_account' for r in d['referenced_by'])
    filt = datastore.page(cat, 'billing.billing_account', BASIC, filters=[{'col': 'customer_id', 'op': 'eq', 'value': cust}])
    assert filt['total'] == next(r['rows'] for r in d['referenced_by'] if r['table'] == 'billing.billing_account' and r['column'] == 'customer_id')


# ---------------- the data tools agents use
@pytest.fixture
def caller(datadb):
    def _as(ent, user=True):
        access.CALLER.set(('tester', access.normalize(ent, access.USER_DEFAULT if user else access.DEFAULT)))
    tok = access.CALLER.set(('anonymous', access.DEFAULT))
    yield _as
    access.CALLER.reset(tok)


def _a_customer(cat=None):
    with datastore.connection(FULL) as c:
        return c.execute("SELECT c.id, c.customer_name FROM crm.customer c JOIN sales.subscription s ON s.customer_id = c.id GROUP BY 1, 2 ORDER BY count(*) DESC LIMIT 1").fetchone()


def test_tokens_without_the_data_entitlement_get_no_data(caller):
    caller({}, user=False)                                                # an MCP token with defaults
    assert tools.query_data('SELECT 1')['error'] == 'not entitled'
    assert tools.plan_query(entities=['customer'])['sql_skeleton']        # planning describes structure only: no data needed
    assert tools.get_access()['entitlements']['data'] is False


def test_one_customer_overview_is_plan_then_query(caller):
    """A customer overview across five entities is a plan and ONE statement, not a dozen hand-written queries."""
    caller(BASIC)
    c = _a_customer()
    p = tools.plan_query(question='open deals, pending orders, unpaid invoices and open complaints of one customer',
                         entities=['customer', 'opportunity', 'sales_order', 'invoice', 'complaint'], grain='customer')
    sk = p['sql_skeleton'].replace("-- WHERE customer.id = '<id>'", f"WHERE customer.id = '{c['id']}'")
    json.dumps(p)
    for definition in ("opp_status = 'Open'", "order_status NOT IN", 'amount_due > 0', 'closed_at IS NULL AND resolved_at IS NULL'):
        assert definition in sk, definition                                              # the model's definitions are in the skeleton
    res = tools.query_data(sk)
    assert 'error' not in res and res['row_count'] == 1 and res['rows'][0]['customer_name'] == c['customer_name'], res
    assert {'n_opportunity', 'n_sales_order', 'n_invoice', 'n_complaint'} <= set(res['columns'])


def test_filters_are_model_data_not_code(caller):
    caller(BASIC)
    d = tools.describe('open complaint')
    assert d['resolved_as'] == 'filter' and d['filters'][0]['filter'] == 'closed_at IS NULL AND resolved_at IS NULL'
    assert d['filters'][0]['status'] == 'governed' and d['filters'][0]['entity'] == 'Complaint'
    assert tools.describe('unpaid')['filters'][0]['entity'] == 'Invoice'                      # found by synonym
    assert len(tools.describe(None, 'filter')['filters']) >= 20
    # a modeller's edit changes the skeleton: the planner reads the entity's filter, not a constant
    from studio import planner
    onto = tools._onto(tools._vid())
    patched = {**onto, 'entities': {k: ({**e, 'filters': [{**f, 'filter': "category = 'Other'"} if f['name'] == 'open complaint' else f for f in e['filters']]} if k == 'complaint' else e)
                                    for k, e in onto['entities'].items()}}
    f = planner.derive_filters(patched, 'open complaints per customer')
    assert f['support.complaint'][0] == "category = 'Other'"
    assert planner.derive_filters(onto, 'complaints per customer') == {}                          # vague: nothing pre-filled


def test_query_data_follows_entitlements_and_stays_read_only(caller):
    caller(BASIC)
    r = tools.query_data('SELECT id, customer_name FROM crm.customer LIMIT 5')
    assert r['row_count'] == 5
    json.dumps(tools.query_data('SELECT now() AS t, current_date AS d, 1.5::numeric AS n'))
    assert 'error' in tools.query_data('DELETE FROM crm.customer')
    assert 'not available' in tools.query_data('SELECT passport_number FROM crm.person')['error']          # restricted
    caller(NOFIN)
    assert 'not available' in tools.query_data('SELECT invoice_amount FROM billing.invoice LIMIT 1')['error']   # confidential
    assert tools.query_data('SELECT id, billing_account_id FROM billing.invoice LIMIT 1')['row_count'] == 1
    caller(access.FULL)
    assert tools.query_data('SELECT passport_number FROM crm.person WHERE passport_number IS NOT NULL LIMIT 1')['row_count'] == 1


def test_a_result_cut_at_the_agent_limit_says_how_many_rows_there_are(caller, monkeypatch):
    caller(BASIC)
    monkeypatch.setattr(settings, 'get', lambda n, _g=settings.get: 10 if n == 'AGENT_MAX_ROWS' else _g(n))
    total = tools.query_data('SELECT count(*) AS n FROM crm.customer')['rows'][0]['n']
    r = tools.query_data('SELECT id FROM crm.customer')
    assert r['row_count'] == 10 and len(r['rows']) == 10 and r['truncated'] and r['total_rows'] == total
    assert f'{total} rows' in r['truncated_note'] and 'no particular order' in r['truncated_note'] and 'GROUP BY' in r['truncated_note']
    assert list(r)[-1] == 'rows'                                       # the notes come first: a long result cut by size keeps them
    r = tools.query_data('WITH c AS (SELECT id FROM crm.customer) SELECT id FROM c ORDER BY id')
    assert r['total_rows'] == total and 'no particular order' not in r['truncated_note']
    assert tools.query_data('SELECT id FROM crm.customer LIMIT 25')['total_rows'] == 25
    small = tools.query_data('SELECT id FROM crm.customer LIMIT 10')
    assert not small.get('truncated') and 'total_rows' not in small


# ---------------- plan_query: from the semantic layer to one runnable statement
COMBOS = [
    ('customer', ['customer', 'subscription', 'complaint', 'invoice']),
    ('customer', ['customer', 'sales_order', 'opportunity', 'support_case']),
    ('billing_account', ['billing_account', 'subscription', 'invoice', 'payment']),
    ('customer', ['customer', 'employee', 'billing_account']),        # employee sits on the 'one' side: a plain LEFT JOIN
    ('customer', ['customer', 'address', 'person']),                  # not directly keyed to the customer: joined through a chain
    ('subscription', ['subscription', 'usage_monthly', 'subscription_price', 'incident']),
]


@pytest.mark.parametrize('grain,ents', COMBOS)
def test_plan_skeleton_is_valid_sql_that_runs(caller, grain, ents):
    caller(access.FULL)
    p = tools.plan_query(entities=ents, grain=grain)
    assert 'error' not in p, p
    assert p['grain']['entity'] == grain
    res = tools.query_data(p['sql_skeleton'])                         # the skeleton as delivered, placeholders are comments
    assert 'error' not in res, (res.get('error'), p['sql_skeleton'])
    assert res['row_count'] > 0
    assert len(json.dumps(p)) < 14000                                  # must fit in what the model is given


def test_plan_chooses_the_grain_and_warns_about_fan_out(caller):
    caller(BASIC)
    assert tools.plan_query(entities=['invoice', 'customer', 'billing_account'])['grain']['entity'] == 'customer'    # the one the others hang off
    p = tools.plan_query(entities=['opportunity', 'sales_order'], grain='opportunity')
    assert any('unreliable' in w and 'Order' in w for w in p['warnings'])   # the model marks sales_order.opportunity_id unreliable
    assert tools.plan_query(entities=['nonsense'])['error']


def test_plan_carries_governed_definitions_instead_of_guesses(caller):
    caller(BASIC)
    p = tools.plan_query(question='For each SMB customer show open complaints and unpaid invoices', entities=['customer', 'complaint', 'invoice'])
    assert any('open complaints' in c['name'].lower() for c in p['calculated_fields'])                               # the related calculated value
    complaint = next(e for e in p['entities'] if e['entity'] == 'complaint')
    assert any(c.startswith('closed_at') for c in complaint['columns'])
    customer = next(e for e in p['entities'] if e['entity'] == 'customer')
    assert any(c.startswith('segment ') and 'SMB' in c for c in customer['columns'])                                # the exact spelling of SMB


def test_plan_follows_entitlements(caller):
    caller(BASIC)
    cols = ' '.join(c for e in tools.plan_query(entities=['person', 'address'], grain='person')['entities'] for c in e['columns'])
    assert 'passport_number' not in cols and 'national_id' not in cols and 'identity_verified' not in cols
    caller(NOFIN)
    inv = next(e for e in tools.plan_query(entities=['customer', 'invoice'])['entities'] if e['entity'] == 'invoice')
    assert not any(c.startswith('invoice_amount') for c in inv['columns'])                                           # confidential
    caller(access.FULL)
    assert 'passport_number' in ' '.join(c for e in tools.plan_query(question='passport number of the contact', entities=['person'])['entities'] for c in e['columns'])


def test_plan_catches_a_wrong_entity_choice(caller):
    """'open complaints' answered from Support Case would count every kind of case: the plan adds Complaint and explains the supertype."""
    caller(BASIC)
    p = tools.plan_query(question='For each SMB customer show open complaints and unpaid invoices', entities=['customer', 'support_case', 'invoice'])
    assert 'complaint' in [e['entity'] for e in p['entities']]                                     # added because the question says so
    assert any("the question says" in w and 'Complaint' in w and 'was added' in w for w in p['warnings'])
    assert any('supertype' in w and 'case_type' in w and "'complaint'" in w for w in p['warnings'])  # and the supertype is explained
    assert any('open complaints' in c['name'].lower() for c in p['calculated_fields'])
    # no noise when the entities already match the question
    q = tools.plan_query(question='open complaints per customer', entities=['customer', 'complaint'])
    assert not any("the question says" in w for w in q['warnings'])
    # plain words that happen to be entity names are not injected
    r = tools.plan_query(question='list customers in order of unpaid invoices, by case of segment', entities=['customer', 'invoice'])
    assert [e['entity'] for e in r['entities']] == ['customer', 'invoice']


def test_plan_prefills_the_models_own_definitions(caller):
    """'open', 'active', 'unpaid' arrive as filters in the skeleton, taken from the entities' filters, so they cannot be guessed wrong."""
    caller(BASIC)
    p = tools.plan_query(question='For each SMB customer: active subscriptions, open complaints and unpaid invoices', entities=['customer', 'subscription', 'complaint', 'invoice'])
    sk = p['sql_skeleton']
    assert 'WHERE closed_at IS NULL AND resolved_at IS NULL' in sk                              # open complaint
    assert "WHERE subscription_status = 'Active'" in sk                                          # active subscription
    assert 'amount_due > 0' in sk                                                               # unpaid invoice
    assert sk.count('definition ') >= 3 and 'governed definition' in sk and 'working definition' in sk
    res = tools.query_data(sk)                                                                   # and it still runs as delivered
    assert 'error' not in res, res
    # a vague question pre-fills nothing it cannot be sure of
    q = tools.plan_query(question='complaints per customer', entities=['customer', 'complaint'])['sql_skeleton']
    assert 'definition' not in q and '-- WHERE <filter on complaint columns>' in q


def test_plan_adds_the_entity_a_definition_points_to(caller):
    """'monthly fee' and 'billing' are not entity names: the model's filters and synonyms map them to Subscription Price and Invoice. Deterministic, no embeddings."""
    caller(BASIC)
    p = tools.plan_query(question='total monthly fee of the active subscriptions per customer', entities=['customer', 'subscription'])
    assert 'subscription_price' in [e['entity'] for e in p['entities']]
    assert any("'monthly fee'" in w and 'was added' in w for w in p['warnings'])
    assert 'error' not in tools.query_data(p['sql_skeleton'])        # (which columns to sum is up to the model: role fact_measure, or names and units that say amount)
    b = tools.plan_query(question='overview of open deals, pending orders and billing for one customer', entities=['customer', 'opportunity', 'sales_order'], grain='customer')
    assert 'invoice' in [e['entity'] for e in b['entities']]                                      # billing -> Invoice
    # the word 'open' alone must not drag in unrelated entities
    assert 'support_case' not in [e['entity'] for e in b['entities']]


def test_query_errors_say_where_the_column_really_is(caller):
    caller(BASIC)
    r = tools.query_data('SELECT monthly_fee FROM catalog.product')
    assert 'does not exist' in r['error'] and 'catalog.price_plan.monthly_fee' in r['hint']
    assert 'tables:' in tools.query_data('SELECT 1 FROM crm.customers')['hint']
    assert 'passport_number' not in str(tools.query_data('SELECT passport FROM crm.person').get('hint', ''))        # hints respect entitlements


def test_plan_points_to_the_domain_when_the_question_names_a_topic(caller):
    """'support' is a topic, not an entity: a plan with nothing from the Support domain must say where support lives."""
    caller(BASIC)
    p = tools.plan_query(question='how are the usage and support of one customer trending in the last year', entities=['customer', 'customer_score'])
    note = next(w for w in p['warnings'] if "mentions 'support'" in w or "says 'support'" in w)
    assert 'Complaint' in note
    ok = tools.plan_query(question='open deals and support', entities=['customer', 'opportunity', 'complaint'])
    assert not any("mentions 'support'" in w for w in ok['warnings'])                         # complaint is in the plan: no noise
    assert not any("mentions 'support'" in w for w in tools.plan_query(question='unpaid invoices', entities=['customer', 'invoice'])['warnings'])


def test_sql_that_does_not_run_is_advice_not_a_blocker(datadb):
    """A filter or check whose SQL does not run on the connected database is reported, but never blocks publishing: the model may be ahead of the data."""
    from studio import editing, ontology, versions
    d = versions.create_draft('0.9-filters')
    try:
        editing.upsert_entity('t', 'complaint', {'filters': [{'name': 'broken', 'filter': 'no_such_column = 1'}]})
        editing.upsert_entity('t', 'customer', {'checks': [{'id': 'X1', 'rule': 'a broken check', 'sql': 'SELECT nope FROM crm.customer'}]})
        warns = editing.sql_warnings(ontology.assemble(d))
        assert any("filter 'broken'" in w for w in warns) and any('check X1' in w for w in warns)
        assert editing.validate(d) == []
    finally:
        versions.discard_draft()


def test_the_ai_step_feeds_realistic_values_and_failures_fall_back(datadb):
    from studio.synth.generic import Generator, ai_suggester
    calls = []

    def fake_model(system, user):
        import json as j
        calls.append(j.loads(user)['entity'])
        cols = j.loads(user)['columns']
        return 'here: ' + j.dumps({c['column']: [f"AI-{c['column']}-{i}" for i in range(3)] for c in cols})
    rows = Generator(datadb['onto'], seed=1, scale='small', suggest=ai_suggester(fake_model)).build()
    assert calls and any(str(v).startswith('AI-') for r in rows['customer'] for v in r.values())
    broken = Generator(datadb['onto'], seed=1, scale='small', suggest=lambda *a: (_ for _ in ()).throw(RuntimeError('down'))).build()
    assert all(v for v in broken.values())                                     # no suggestions: plain values, nothing missing


# ---------------- filter_matches: a lookup by a descriptive value that selects several records is reported, never refused
def _shared_name():
    return datastore.console('SELECT customer_name AS v, count(*) AS n FROM crm.customer GROUP BY customer_name HAVING count(*) > 1 ORDER BY count(*) DESC, customer_name LIMIT 1', FULL)['rows'][0]


def test_a_lookup_that_matches_several_records_is_reported(caller):
    caller(BASIC)
    v = _shared_name()
    name = v['v'].replace("'", "''")
    cte = (f"WITH c AS (SELECT id FROM crm.customer WHERE customer_name ILIKE '%{name}%') "
           "SELECT o.id, o.customer_id FROM sales.sales_order o JOIN c ON c.id = o.customer_id")
    r = tools.query_data(cte)
    m = r['filter_matches'][0]
    assert m['entity'] == 'customer' and m['column'] == 'customer_name' and m['distinct_records'] >= v['n']
    assert 0 < len(m['examples']) <= 5 and all(e['customer_name'] for e in m['examples']) and 'cover all of them' in m['note']
    joined = f"SELECT o.id FROM sales.sales_order o JOIN crm.customer c ON c.id = o.customer_id WHERE c.customer_name = '{name}'"
    assert tools.query_data(joined)['filter_matches'][0]['distinct_records'] == v['n']
    sub = f"SELECT o.id FROM sales.sales_order o WHERE o.customer_id IN (SELECT id FROM crm.customer WHERE customer_name = '{name}')"
    assert tools.query_data(sub)['filter_matches'][0]['distinct_records'] == v['n']


def test_plain_filters_are_not_reported(caller):
    caller(BASIC)
    name = _shared_name()['v'].replace("'", "''")
    assert 'filter_matches' not in tools.query_data(f"SELECT id, customer_name FROM crm.customer WHERE customer_name = '{name}'")   # the rows already show every match
    one = datastore.console('SELECT id FROM crm.customer ORDER BY id LIMIT 1', FULL)['rows'][0]['id']
    assert 'filter_matches' not in tools.query_data(f"SELECT o.id FROM sales.sales_order o JOIN crm.customer c ON c.id = o.customer_id WHERE c.id = '{one}'")   # a key
    from studio.synth import ddl
    onto = datastore.current_catalog().onto
    k, c, child, fk, val = next((k, c, ck, cc, a['allowed_values'][0]) for ck, cc, k in ddl.foreign_keys(onto) for c, a in onto['entities'][k]['attributes'].items()
                                if ck != k and a.get('allowed_values') and a['type'] == 'text' and a['role'] == 'attribute' and not a.get('sensitivity') in ('confidential', 'personal', 'restricted'))
    pe, ce = onto['entities'][k], onto['entities'][child]
    coded = tools.query_data(f"SELECT x.{ddl.pk_col(ce)} FROM {ce['semantic_table']} x JOIN {pe['semantic_table']} p ON p.{ddl.pk_col(pe)} = x.{fk} WHERE p.{c} = '{val}'")
    assert 'error' not in coded and 'filter_matches' not in coded                                                                         # a code list
