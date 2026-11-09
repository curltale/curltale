import re
import pytest
from conftest import fixture_model
from studio.chunker import build_chunks, summarize


@pytest.fixture(scope='module')
def model():
    new = fixture_model()
    nm = {e['name']: k for k, e in new['entities'].items()}
    for r in new['relationships']:                                   # assemble() derives these links; the chunker reads them
        s = new['entities'].get(nm.get(r['from']))
        if s is not None:
            s.setdefault('relationships_out', []).append({k: r[k] for k in ('id', 'predicate', 'to', 'cardinality', 'join', 'status', 'status_detail', 'note') if k in r})
    return new, None


def test_every_concept_becomes_chunks(model):
    new, _ = model
    c = dict(summarize(build_chunks(new)))
    assert set(c) == {'entity', 'attribute', 'calculated', 'relationship', 'filter', 'check', 'example'}
    assert c['entity'] == 23 and c['calculated'] == 7 and c['relationship'] == 34 and c['example'] == 3


def test_chunk_text_is_stable_and_carries_custom_properties(model):
    new, _ = model
    a, b = build_chunks(new), build_chunks(new)
    assert [x['text'] for x in a] == [x['text'] for x in b]                           # same model, same text: embeddings are reused
    cust = next(x for x in a if x['id'] == 'entity:customer')
    k, v = next(iter(new['entities']['customer']['custom'].items()))
    assert f'{k}: ' in cust['text']                                                     # a company's own properties are searchable


def test_restricted_columns_never_visible_to_agents(model):
    new, _ = model
    hidden = [(k, c) for k, e in new['entities'].items() for c, a in e['attributes'].items() if a['agent_access'] == 'none']
    assert hidden
    for ch in build_chunks(new):
        if ch['metadata'].get('agent_access') == 'none':
            continue
        for k, c in hidden:
            if len(c) > 6:   # whole-word column name; generic prose such as "identity values" in rule R01 is fine
                assert not re.search(rf'\b{re.escape(c)}\b', ch['text'], re.I) or ch['id'] == f'attr:{k}.{c}', (ch['id'], c)
