"""Small read helpers over the assembled model (see ontology.py), shared by tools, planner, MCP, conformance and the synthetic-data code."""


def calculated(onto):
    """{code: calculated field} with its entity: name, entity (entity name), entity_key. Codes are unique per entity; a code used by two entities is
    listed as entity_key.code for the second one."""
    out = {}
    for k, e in onto['entities'].items():
        for code, m in e.get('calculated', {}).items():
            out[code if code not in out else f'{k}.{code}'] = {**m, 'name': m['business_name'], 'entity': e['name'], 'entity_key': k}
    return out


def examples(onto):
    return [k for k in onto.get('examples', []) if k.get('sql')]


def examples_for(onto, entity_keys):
    """Query examples that apply to any of the entities."""
    keys = set(entity_keys)
    return [k for k in examples(onto) if keys & set(k.get('applies_to', []))]


def domains(onto):
    """{domain name: [entity keys]}: the domain is a property of the entity."""
    out = {}
    for k, e in onto['entities'].items():
        out.setdefault(e.get('domain') or 'Other', []).append(k)
    return out


def filters(onto):
    """[(entity key, filter)] for every named filter of every entity."""
    return [(k, f) for k, e in onto['entities'].items() for f in e.get('filters', [])]
