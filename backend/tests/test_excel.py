import io

from openpyxl import load_workbook

from studio import editing, excel_io, ontology, versions


def _model():
    return ontology.assemble(ontology.published_id())


def _norm(v):
    """Cells are trimmed on the way in, so compare with surrounding whitespace removed."""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, list):
        return [_norm(x) for x in v]
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    return v


def _core(m):
    """What must survive a round trip (derived keys excluded)."""
    ents = {}
    for k, e in m['entities'].items():
        x = {a: b for a, b in e.items() if a not in ('relationships_out', 'relationships_in', 'stats', 'subtypes')}
        ents[k] = x
    return {'entities': ents, 'relationships': m['relationships'], 'examples': m['examples'], 'access_policy': m['access_policy']}


def test_export_and_upload_lose_nothing(client):
    m = _model()
    data = excel_io.build_workbook(m)
    back, rep = excel_io.read_workbook(data)
    assert not rep.errors, rep.errors[:5]
    a, b = _norm(_core(m)), _norm(_core(back))
    assert a['access_policy'] == b['access_policy']
    assert list(a['entities']) == list(b['entities'])
    for k in a['entities']:
        x, y = a['entities'][k], b['entities'][k]
        for part in ('attributes', 'calculated'):
            bad = [(c, {f: (x[part][c].get(f), y[part][c].get(f)) for f in set(x[part][c]) | set(y[part][c]) if x[part][c].get(f) != y[part][c].get(f)}) for c in x[part] if x[part][c] != y[part].get(c)]
            assert not bad, (k, part, [(c, {f: (repr(v[0])[-60:], repr(v[1])[-60:], len(str(v[0])), len(str(v[1]))) for f, v in d.items()}) for c, d in bad[:2]])
        rx, ry = {p: x[p] for p in x if p not in ('attributes', 'calculated')}, {p: y[p] for p in y if p not in ('attributes', 'calculated')}
        assert rx == ry, (k, [{q: (a1.get(q), b1.get(q)) for q in set(a1) | set(b1) if a1.get(q) != b1.get(q)} for a1, b1 in zip(rx.get('filters', []), ry.get('filters', [])) if a1 != b1][:1] or {f: (rx.get(f), ry.get(f)) for f in set(rx) | set(ry) if rx.get(f) != ry.get(f)})
    assert a['relationships'] == b['relationships']
    bad = [(x['id'], {q: (x.get(q), y.get(q)) for q in set(x) | set(y) if x.get(q) != y.get(q)}) for x, y in zip(a['examples'], b['examples']) if x != y]
    assert not bad, str(bad[:2])[:600]


def test_exported_model_passes_the_publish_checks(client):
    back, rep = excel_io.read_workbook(excel_io.build_workbook(_model()))
    assert editing.validate_model(back) == []


def test_blank_template_has_every_sheet_and_dropdowns():
    wb = load_workbook(io.BytesIO(excel_io.build_workbook()))
    assert [n for n in wb.sheetnames if not n.startswith('_')] == excel_io.SHEETS
    assert wb['Fields'].data_validations.dataValidation
    assert wb['Fields']['A1'].comment is not None


def test_problems_are_reported_with_sheet_row_and_column(client):
    wb = load_workbook(io.BytesIO(excel_io.build_workbook(_model())))
    ws = wb['Fields']
    head = [c.value for c in ws[1]]
    ws.cell(2, head.index('entity_key') + 1, 'no_such_entity')
    ws.cell(3, head.index('sensitivity') + 1, 'restricted'); ws.cell(3, head.index('agent_access') + 1, 'full')
    ws.cell(4, head.index('role') + 1).value = None
    buf = io.BytesIO(); wb.save(buf)
    out = versions.import_excel(buf.getvalue(), 'tester', dry_run=True)
    where = {(e['sheet'], e['row'], e['column']) for e in out['errors']}
    assert ('Fields', 2, 'entity_key') in where and ('Fields', 3, 'agent_access') in where and ('Fields', 4, 'role') in where
    assert out['created'] is None


def test_upload_creates_a_draft_and_refuses_to_overwrite_one(client):
    data = excel_io.build_workbook(_model())
    out = versions.import_excel(data, 'tester', dry_run=False, replace_draft=True)
    assert out['created'] and not out['errors']
    assert ontology.draft_id() == out['created']
    try:
        versions.import_excel(data, 'tester', dry_run=False)
        raise AssertionError('should have refused')
    except editing.EditError:
        pass
    versions.discard_draft()


def test_an_industry_standard_mapping_is_optional(client):
    from studio.chunker import build_chunks
    m = _model()
    for e in m['entities'].values():
        e.pop('tmf', None)
        for a in e['attributes'].values():
            a.pop('tmf_name', None)
    assert editing.validate_model(m) == []
    assert all('Industry standard' not in c['text'] for c in build_chunks(m) if c['type'] == 'entity')
    back, rep = excel_io.read_workbook(excel_io.build_workbook(m))
    assert not rep.errors and all('tmf' not in e for e in back['entities'].values())


def test_any_extra_column_becomes_a_custom_property(client):
    wb = load_workbook(io.BytesIO(excel_io.build_workbook()))
    model_rows = wb['Model']
    model_rows.append(['name', 'Tiny'])
    e = wb['Entities']
    head = [c.value for c in e[1]]
    e.cell(1, len(head) + 1, 'owner team')
    row = {'key': 'thing', 'name': 'Thing', 'domain': 'Ops', 'kind': 'master', 'semantic_table': 'ops.thing', 'business_key': 'id', 'system_of_record': 'ERP'}
    e.append([row.get(h) for h in head] + ['Data office'])
    f = wb['Fields']
    fh = [c.value for c in f[1]]
    f.cell(1, len(fh) + 1, 'source system')
    for col, role in (('id', 'primary_key'), ('total_value', 'calculated')):
        r = {'entity_key': 'thing', 'column': col, 'role': role, 'business_name': col, 'type': 'text' if role != 'calculated' else 'decimal', 'sensitivity': 'internal',
             'sql': 'SELECT sum(x) FROM ops.thing' if role == 'calculated' else None}
        f.append([r.get(h) for h in fh] + ['ERP v2'])
    buf = io.BytesIO(); wb.save(buf)
    m, rep = excel_io.read_workbook(buf.getvalue())
    assert not rep.errors, rep.errors
    t = m['entities']['thing']
    assert t['custom'] == {'owner team': 'Data office'} and t['attributes']['id']['custom'] == {'source system': 'ERP v2'}
    assert t['calculated']['total_value']['sql'].startswith('SELECT') and t['attributes']['id']['agent_access'] == 'full'     # defaults filled in
    again = load_workbook(io.BytesIO(excel_io.build_workbook(m)))
    assert 'owner team' in [c.value for c in again['Entities'][1]] and 'source system' in [c.value for c in again['Fields'][1]]
