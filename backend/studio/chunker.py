"""Turn an assembled model (see ontology.py) into search chunks. Pure function; chunk text is derived, never edited.

Chunk types: entity, attribute, calculated (a calculated field), relationship, filter (a named filter on an entity), check, example.
Chunk text only changes when the item changes, so embeddings are reused across versions. Custom properties are part of the text: they are searchable.
"""
import re
from collections import Counter


def slug(s):
    return re.sub(r'[^a-z0-9]+', '_', s.lower()).strip('_')


def clean(s):
    return re.sub(r'\s+', ' ', str(s)).strip()


def custom_text(x):
    """A company's own properties of an item, as text."""
    c = x.get('custom') or {}
    return (' ' + '; '.join(f'{k}: {str(v)[:300]}' for k, v in c.items()) + '.') if c else ''


def build_chunks(m):
    ents = m['entities']
    key_of = {x['name']: k for k, x in ents.items()}
    ch = []

    def add(cid, ctype, title, text, **meta):
        ch.append({'id': cid, 'type': ctype, 'title': title, 'text': clean(text),
                   'metadata': {k: v for k, v in meta.items() if v not in ('', None, [])}})

    # restricted columns (agent_access none) must not surface anywhere an agent can read: not their chunks, not the entity's attribute list
    for eid, x in ents.items():
        n = x['name']
        shown = [c for c, a in x['attributes'].items() if a.get('agent_access') != 'none']
        txt = (f"Entity {n} ({x['kind']}, domain {x['domain']}). {x.get('definition', '')} Also called: {', '.join(x.get('synonyms', []))}. "
               + (f"Industry standard: {x['tmf']['resource']} ({x['tmf'].get('open_api', x['tmf'].get('reference', ''))}). " if x.get('tmf', {}).get('resource') else '') + f"Table {x['semantic_table']}, business key {x['business_key']}, system of record {x['system_of_record']}. "
               f"Belongs to: {x.get('belongs_to', '-')}. " + (f"Not to confuse with: {'; '.join(x['not_to_confuse_with'])}. " if x.get('not_to_confuse_with') else '')
               + (f"Subtype of {x['subtype_of']}. " if x.get('subtype_of') else '') + (f"Subtypes: {', '.join(x['subtypes'])}. " if x.get('subtypes') else '')
               + (f"Lifecycle: {'; '.join(k + ': ' + ', '.join(v) for k, v in x['lifecycle_states'].items())}. " if x.get('lifecycle_states') else '')
               + f"Links to: {', '.join(r['to'] + ' (' + r['status'] + ')' for r in x.get('relationships_out', []))}. "
               + f"Key attributes: {', '.join(shown[:25])}. " + (f"Calculated fields: {', '.join(x['calculated'])}. " if x.get('calculated') else '')
               + (f"Typical questions: {' | '.join(x['example_questions'])}" if x.get('example_questions') else '') + custom_text(x))
        add(f'entity:{eid}', 'entity', n, txt, entity=eid, domain=x['domain'], table=x['semantic_table'], tmf=(x.get('tmf') or {}).get('resource'))
        for c, a in x['attributes'].items():
            if a['role'] == 'lineage':
                continue
            t = (f"Attribute {a['business_name']} ({x['semantic_table']}.{c}) of entity {n}. Type {a['type']}, role {a['role']}. {a.get('description', '')} "
                 + (f"Industry standard name {a['tmf_name']}. " if a.get('tmf_name') else '') + (f"Business term: {a['business_term']}. " if a.get('business_term') else '')
                 + (f"{a['business_term_description']} " if a.get('business_term_description') else '')
                 + (f"References {a['references']}. " if a.get('references') else '')
                 + (f"Allowed values: {', '.join(a['allowed_values'])}. " if a.get('allowed_values') else '')
                 + (f"Value labels: {'; '.join(k + ' = ' + v for k, v in a['value_labels'].items())}. " if a.get('value_labels') else '')
                 + (f"Format {a['format']}. " if a.get('format') else '') + (f"Example {a['example']}. " if a.get('example') and a.get('agent_access') == 'full' else '')
                 + f"Sensitivity {a['sensitivity']}, agent access {a['agent_access']}, review status {a['review_status']}." + custom_text(a))
            add(f'attr:{eid}.{c}', 'attribute', f"{n}.{a['business_name']}", t, entity=eid, column=f"{x['semantic_table']}.{c}",
                sensitivity=a['sensitivity'], agent_access=a['agent_access'], review_status=a['review_status'])
        for code, k in x.get('calculated', {}).items():
            add(f'calc:{eid}.{code}', 'calculated', f"{n}: {k['business_name']}",
                f"Calculated field {k['business_name']} ({code}) of entity {n}: not a column, a value computed from the data. {k.get('description', '')} "
                + (f"Unit {k['unit']}. " if k.get('unit') else '') + (f"How it is computed (SQL): {k['sql']} " if k.get('sql') else '')
                + f"Sensitivity {k['sensitivity']}, review status {k.get('review_status')}." + custom_text(k),
                entity=eid, sensitivity=k.get('sensitivity'), agent_access=k.get('agent_access'), review_status=k.get('review_status'))
        for f_ in x.get('filters', []):
            add(f"filter:{eid}.{slug(f_['name'])}", 'filter', f"Filter: {f_['name']}",
                f"Filter '{f_['name']}' on {n} ({f_.get('status', 'working')}): {f_.get('description', '')} Condition: {f_['filter']}."
                + (f" Also called: {', '.join(f_['synonyms'])}." if f_.get('synonyms') else '') + (f" Source: {f_['source']}." if f_.get('source') else ''),
                entity=eid, status=f_.get('status'))
        for c_ in x.get('checks', []):
            add(f"check:{eid}.{c_['id']}", 'check', f"Check {c_['id']} on {n}", f"Data quality check {c_['id']} on {n}: {c_['rule']}" + (f" Note: {c_['note']}" if c_.get('note') else ''), entity=eid)
    for r in m['relationships']:
        add(f"rel:{r['id']}", 'relationship', f"{r['from']} -> {r['to']}",
            f"Relationship {r['id']}: {r.get('sentence') or (r['from'] + ' ' + r.get('predicate', '') + ' ' + r['to'] + '.')} Join: {r['join']}. Cardinality {r['cardinality']}. Status {r['status']}"
            + (f" ({r['status_detail']})." if r.get('status_detail') else '.')
            + (f" Note: {r['note']}" if r.get('note') else '') + custom_text(r),
            from_entity=key_of.get(r['from'], r['from']), to_entity=key_of.get(r['to'], r['to']), status=r['status'])
    for k in m['examples']:
        about = [ents[a]['name'] for a in k.get('applies_to', []) if a in ents]
        text = (f"Example {k['id']}" + (f" ({k['topic'].replace('_', ' ')})" if k.get('topic') else '') + (f" about {', '.join(about)}" if about else ' (applies everywhere)')
                + f": {k.get('title', '')}. {k.get('body', '')}")
        if k.get('sql'):
            text += f" SQL: {k['sql']}"
        text += custom_text(k)
        add(f"ex:{k['id']}", 'example', f"Example: {k.get('title') or k['id']}", text, entity=(k.get('applies_to') or [None])[0])
    return ch


def summarize(ch):
    return Counter(c['type'] for c in ch)
