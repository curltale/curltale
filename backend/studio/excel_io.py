"""The Excel template: build it (blank, or filled from a model), and read a filled workbook back into a model with a precise problem report.

One sheet per concept. List cells use ' | ' between items. A column the template does not define is a CUSTOM PROPERTY: kept as written on the
item, shown in the app, searchable and given to agents. Exporting a model and uploading it again loses nothing. Reading never writes: it returns the
model dict and a report; the caller decides.
"""
import io
import json
import re
from datetime import date, datetime

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from . import editing
from .ontology import REL_STATUS

SEP = ' | '
KEY_RE = re.compile(r'^[a-z][a-z0-9_]*$')
ENUMS = {
    'kind': ['master', 'reference', 'transaction', 'fact', 'relationship', 'bridge', 'agreement', 'supertype', 'derived', 'technical'],
    'model_status': ['confirmed', 'new', 'placeholder'],
    'role': ['attribute', 'primary_key', 'foreign_key', 'alternate_reference', 'dimension', 'fact_measure', 'derived_attribute', 'lineage', 'calculated'],
    'sensitivity': ['internal', 'confidential', 'personal', 'restricted'],
    'agent_access': ['full', 'masked', 'none'],
    'review_status': sorted(editing.REVIEW),
    'filter_status': ['governed', 'working'],
    'rel_status': list(REL_STATUS),
    'cardinality': ['many-to-one', 'one-to-many', 'one-to-one', 'many-to-many', 'many-to-zero-or-one (optional)', 'subtype-of'],
    'kb_type': ['rule', 'example', 'process', 'term', 'note'],
    'yesno': ['yes', 'no'],
    'type': ['text', 'integer', 'decimal', 'boolean', 'date', 'timestamp', 'text[]'],
}

# column spec: (name, required, kind, help); kind: text | list | lines | json | enum:<name> | key | ref:Entities
S = {}
S['Entities'] = [
    ('key', 1, 'key', 'Short lower_snake_case id, used everywhere else in this workbook (customer, order_line).'),
    ('name', 1, 'text', 'Business name shown to people and agents.'),
    ('domain', 1, 'text', 'Business domain: Sales, Finance, Operations...'),
    ('kind', 1, 'enum:kind', 'What sort of concept it is.'),
    ('definition', 0, 'text', 'One or two sentences: what one row of this entity is.'),
    ('semantic_table', 1, 'text', 'schema.table in the database that holds it.'),
    ('primary_key', 0, 'text', 'Column that identifies a row.'),
    ('business_key', 1, 'text', 'Column(s) people use to identify it (customer number, order number).'),
    ('system_of_record', 1, 'text', 'The system that owns the data.'),
    ('synonyms', 0, 'list', 'Other words for it, separated by " | ".'),
    ('not_to_confuse_with', 0, 'list', 'Things it is often mistaken for, with the difference. Separated by " | ".'),
    ('belongs_to', 0, 'text', 'Parent concept, if any.'),
    ('subtype_of', 0, 'text', 'Entity key of the supertype, if any.'),
    ('model_status', 0, 'enum:model_status', 'How settled this entity is in the model.'),
    ('standard_resource', 0, 'text', 'Optional. Name of the matching concept in an industry standard you follow.'),
    ('standard_reference', 0, 'text', 'Optional. Which standard / document it comes from.'),
    ('standard_detail', 0, 'text', 'Optional. Any further reference (API, section).'),
    ('lifecycle_states', 0, 'lines', 'One line per status column: "status: draft > active > closed".'),
    ('example_questions', 0, 'list', 'Typical questions this entity answers. Separated by " | ".'),
]
S['Fields'] = [
    ('entity_key', 1, 'ref:Entities', 'Entity this field belongs to.'),
    ('column', 1, 'key', 'Column name in the entity\'s table; for a calculated field, a short code.'),
    ('role', 1, 'enum:role', 'primary_key / foreign_key / attribute ... or calculated: a value computed from the data, not a column.'),
    ('business_name', 1, 'text', 'Name people use.'),
    ('type', 1, 'enum:type', 'Data type.'),
    ('description', 0, 'text', 'What the value means (for a calculated field: how it is computed, in words).'),
    ('sensitivity', 1, 'enum:sensitivity', 'internal / confidential / personal / restricted.'),
    ('agent_access', 0, 'enum:agent_access', 'full, masked (shown as a stable hash to callers without the class) or none. Default: none for restricted, otherwise full.'),
    ('review_status', 0, 'enum:review_status', 'How settled the definition is. Default needs_review.'),
    ('references', 0, 'text', 'For foreign keys: the entity NAME it points to.'),
    ('format', 0, 'text', 'Pattern, e.g. two letters + 6 digits.'),
    ('example', 0, 'text', 'A sample value (never a restricted one).'),
    ('business_term', 0, 'text', 'Glossary name of this field.'),
    ('business_term_description', 0, 'text', 'Glossary definition.'),
    ('standard_name', 0, 'text', 'Optional. The name in the industry standard you follow.'),
    ('unit', 0, 'text', 'Unit of the value: count, EUR, hours...'),
    ('sql', 0, 'text', 'Calculated fields: optional SQL that computes the value. Context for agents, never run by the app.'),
]
S['Values'] = [
    ('entity_key', 1, 'ref:Entities', 'Entity of the field.'),
    ('column', 1, 'key', 'Field whose values these are.'),
    ('value', 1, 'text', 'An allowed value (the code stored in the column).'),
    ('label', 0, 'text', 'What it reads as to people.'),
    ('note', 0, 'text', 'Anything worth knowing about this value.'),
]
S['Filters'] = [
    ('entity_key', 1, 'ref:Entities', 'Entity the filter applies to.'),
    ('name', 1, 'text', 'The words people use: open order, unpaid invoice.'),
    ('filter', 1, 'text', 'SQL condition on the entity\'s table, columns unqualified (closed_at IS NULL, amount_due > 0).'),
    ('synonyms', 0, 'list', 'Other ways to say it. Separated by " | ".'),
    ('description', 0, 'text', 'What it means.'),
    ('status', 0, 'enum:filter_status', 'governed (agreed) or working (not confirmed yet). Default working.'),
    ('source', 0, 'text', 'Where the definition comes from.'),
]
S['Checks'] = [
    ('entity_key', 1, 'ref:Entities', 'Entity the check is about.'),
    ('id', 1, 'text', 'Check id, unique per entity (DQ01).'),
    ('rule', 1, 'text', 'The data quality rule in words.'),
    ('sql', 0, 'text', 'Optional SQL returning one column n = number of violations (must be 0); run by the Model check.'),
    ('note', 0, 'text', 'Caveats.'),
]
S['Relationships'] = [
    ('id', 0, 'text', 'R-001... Leave empty to number automatically.'),
    ('from', 1, 'text', 'Entity NAME (as in the Entities sheet) where the link starts.'),
    ('predicate', 1, 'text', 'The verb: places, belongs to, is billed on.'),
    ('to', 1, 'text', 'Entity NAME where it ends.'),
    ('cardinality', 1, 'enum:cardinality', 'many-to-one...'),
    ('join', 1, 'text', 'Exact join: a.col = b.col'),
    ('status', 1, 'enum:rel_status', 'confirmed, optional (may be empty) or unreliable (often empty or wrong).'),
    ('status_detail', 0, 'text', 'Why it has that status.'),
    ('sentence', 0, 'text', 'The link as a sentence. Default: built from the names.'),
    ('note', 0, 'text', 'Caveats.'),
]
S['Examples'] = [
    ('id', 0, 'text', 'EX01, EX02... Leave empty to number automatically.'),
    ('title', 1, 'text', 'The question the example answers.'),
    ('body', 0, 'text', 'A note on the path it takes or what to watch for.'),
    ('sql', 1, 'text', 'The query, with :parameters.'),
    ('params', 0, 'json', '{"customer_id": "customer"} parameter -> entity key.'),
    ('sample_sql', 0, 'text', 'A query returning one row of parameter values, so the example can be tried on real data.'),
    ('applies_to', 0, 'list', 'Entity keys this is about. Separated by " | ".'),
    ('topic', 0, 'text', 'Optional grouping.'),
    ('source', 0, 'text', 'Where it comes from.'),
]
# files made before rules, terms, processes and notes moved to the document Library carry a Knowledge sheet: its examples are still read, the rest is reported and left out
LEGACY_KNOWLEDGE = [
    ('id', 0, 'text', ''), ('type', 1, 'enum:kb_type', ''), ('title', 1, 'text', ''), ('body', 0, 'text', ''), ('pinned', 0, 'enum:yesno', ''), ('applies_to', 0, 'list', ''),
    ('sql', 0, 'text', ''), ('params', 0, 'json', ''), ('sample_sql', 0, 'text', ''), ('topic', 0, 'text', ''), ('synonyms', 0, 'list', ''), ('source', 0, 'text', ''),
]
MODEL_ROWS = [('name', 'Name of the ontology'), ('id', 'Short id'), ('version', 'Version number'), ('date', 'Date'), ('source', 'Where it comes from'), ('status', 'Review status text'),
              ('standards', 'Standards followed, separated by " | "'), ('access.internal', 'What internal means'), ('access.confidential', 'What confidential means'),
              ('access.personal', 'What personal means'), ('access.restricted', 'What restricted means'), ('access.row_level', 'Row-level rule, in words'),
              ('how_to_read', 'Reading notes, separated by " | "')]
SHEETS = ['Instructions', 'Model', *S]
HEAD = PatternFill('solid', fgColor='1F3A5F'); REQ = PatternFill('solid', fgColor='9B2C2C'); CUSTOM = PatternFill('solid', fgColor='5B6470'); ID_FONT = Font(color='FFFFFF', bold=True)
CUSTOM_HELP = 'Custom property: a column of your own. Kept as written on the item, shown in the app and given to agents.'


# ------------------------------------------------------------------ build
def _cell(v, kind='text'):
    if v is None:
        return None
    if kind == 'list':
        return SEP.join(str(x) for x in v) if isinstance(v, list) else str(v)
    if kind == 'lines':
        return '\n'.join(f"{k}: {' > '.join(map(str, x))}" for k, x in v.items()) if isinstance(v, dict) else str(v)
    if kind == 'json':
        return json.dumps(v, ensure_ascii=False) if v not in ({}, [], '') else None
    if kind == 'enum:yesno':
        return 'yes' if v else 'no'
    return v if isinstance(v, (int, float, bool)) else str(v)


def _custom_keys(items):
    keys = []
    for it in items:
        for k in (it.get('custom') or {}):
            if k not in keys:
                keys.append(k)
    return keys


def _sheet(wb, name, items, widths=None):
    """items: dicts keyed by the sheet's column names (+ 'custom')."""
    ws = wb.create_sheet(name)
    custom = [k for k in _custom_keys(items) if k not in {c for c, *_ in S[name]}]
    for i, (col, req, kind, help_) in enumerate(S[name], 1):
        c = ws.cell(1, i, col)
        c.fill, c.font, c.alignment = (REQ if req else HEAD), ID_FONT, Alignment(vertical='top')
        c.comment = Comment(f"{'REQUIRED. ' if req else ''}{help_}", 'template', width=320, height=110)
        ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(col, 22 if kind in ('text', 'list', 'lines') else 16)
    base = len(S[name])
    for j, k in enumerate(custom, base + 1):
        c = ws.cell(1, j, k)
        c.fill, c.font = CUSTOM, ID_FONT
        c.comment = Comment(CUSTOM_HELP, 'template', width=320, height=80)
        ws.column_dimensions[get_column_letter(j)].width = 22
    ws.freeze_panes = 'A2'
    for it in items:
        ws.append([_cell(it.get(c), k) for c, _, k, _h in S[name]] + [(it.get('custom') or {}).get(k) for k in custom])
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical='top')
    return ws


def _validations(wb):
    """Dropdowns for closed lists (a warning, not a block: new values are allowed but flagged on upload) and entity keys taken from the Entities sheet."""
    lists = wb.create_sheet('_lists'); lists.sheet_state = 'hidden'
    for j, (name, vals) in enumerate(ENUMS.items(), 1):
        for i, v in enumerate(vals, 1):
            lists.cell(i, j, v)
    pos = {name: get_column_letter(j) for j, name in enumerate(ENUMS, 1)}
    for sheet, spec in S.items():
        ws = wb[sheet]
        for i, (col, _, kind, _h) in enumerate(spec, 1):
            letter = get_column_letter(i)
            if kind.startswith('enum:'):
                en = kind[5:]
                f = f"=_lists!${pos[en]}$1:${pos[en]}${len(ENUMS[en])}"
            elif kind.startswith('ref:') and sheet != 'Entities':
                f = "=Entities!$A$2:$A$2000"
            else:
                continue
            dv = DataValidation(type='list', formula1=f, allow_blank=True, errorStyle='warning', showErrorMessage=True,
                                errorTitle='Not in the list', error='This value is not one of the usual ones. You can keep it, but the upload will flag it.')
            ws.add_data_validation(dv)
            dv.add(f'{letter}2:{letter}3000')


def _instructions(wb):
    ws = wb['Instructions']
    ws.column_dimensions['A'].width = 22; ws.column_dimensions['B'].width = 30; ws.column_dimensions['C'].width = 9; ws.column_dimensions['D'].width = 90
    lines = [
        ('Curltale AI: model template', None),
        ('How it works', 'Fill one sheet per concept: Entities, Fields (+ Values), Filters, Checks, Relationships, Examples. Row 1 of each sheet names the columns: dark red = required, dark blue = optional (hover for help).'),
        ('', 'Calculated fields (values computed from the data, not columns) go in the Fields sheet with role "calculated"; their optional SQL is context for agents.'),
        ('', 'Add any column you need to a sheet: it becomes a custom property of the item (grey header on export), shown in the app and given to agents.'),
        ('', 'Upload the file on the Versions page. It is checked first (a report lists every problem with sheet, row and column); nothing changes until you confirm, and the result is a DRAFT you review and publish.'),
        ('', 'List cells: separate items with " | ". Entities are referred to by their key (entity_key, applies_to); relationships and references use entity names. Do not rename sheets.'),
        ('', None),
    ]
    for a, b in lines:
        ws.append([a, b])
    ws['A1'].font = Font(bold=True, size=14)
    for r in range(2, 7):
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=4); ws.cell(r, 2).alignment = Alignment(wrap_text=True, vertical='top'); ws.row_dimensions[r].height = 34
    ws.append(['Sheet', 'Column', 'Needed', 'Meaning'])
    for c in ws[ws.max_row]:
        c.fill, c.font = HEAD, ID_FONT
    ws.append(['Model', 'rows: ' + ', '.join(k for k, _ in MODEL_ROWS[:8]) + '...', 'no', 'Settings of the whole model: name, version, what each sensitivity class means. Extra rows are custom properties.'])
    for sheet, spec in S.items():
        for col, req, kind, help_ in spec:
            extra = f" Allowed: {', '.join(ENUMS[kind[5:]])}." if kind.startswith('enum:') else ''
            ws.append([sheet, col, 'REQUIRED' if req else 'optional', help_ + extra])
    for row in ws.iter_rows(min_row=9):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical='top')


def _model_rows(m):
    o, ap = m.get('ontology', {}), m.get('access_policy', {})
    vals = {'name': o.get('name'), 'id': o.get('id'), 'version': o.get('version'), 'date': o.get('date'), 'source': o.get('source'), 'status': o.get('status'),
            'standards': _cell(o.get('standards'), 'list'), 'how_to_read': _cell(o.get('how_to_read'), 'list'),
            **{f'access.{k}': ap.get(k) for k in ('internal', 'confidential', 'personal', 'restricted', 'row_level')}}
    rows = [(k, vals.get(k), h) for k, h in MODEL_ROWS]
    rows += [(k, v, 'custom property') for k, v in (o.get('custom') or {}).items()]
    return rows


def _field_item(key, col, f):
    lab, notes = f.get('value_labels') or {}, f.get('value_notes') or {}
    return {**f, 'entity_key': key, 'column': col, 'standard_name': f.get('tmf_name')}, \
        [{'entity_key': key, 'column': col, 'value': v, 'label': lab.get(v), 'note': notes.get(v)} for v in f.get('allowed_values') or []]


def build_workbook(m=None):
    """A workbook: blank (m is None) or filled from an assembled model."""
    wb = Workbook(); wb.active.title = 'Instructions'
    _instructions(wb)
    ws = wb.create_sheet('Model')
    for i, h in enumerate(('setting', 'value', 'meaning'), 1):
        c = ws.cell(1, i, h); c.fill, c.font = HEAD, ID_FONT
    ws.column_dimensions['A'].width, ws.column_dimensions['B'].width, ws.column_dimensions['C'].width = 22, 80, 50
    items = {n: [] for n in S}
    for r in _model_rows(m or {}):
        ws.append(list(r))
    if m:
        for key, e in m['entities'].items():
            tmf = e.get('tmf') or {}
            items['Entities'].append({**e, 'key': key, 'standard_resource': tmf.get('resource'), 'standard_reference': tmf.get('reference'), 'standard_detail': tmf.get('open_api')})
            for part in ('attributes', 'calculated'):
                for col, f in (e.get(part) or {}).items():
                    row, values = _field_item(key, col, f)
                    items['Fields'].append(row)
                    items['Values'] += values
            items['Filters'] += [{**f, 'entity_key': key} for f in e.get('filters', [])]
            items['Checks'] += [{**c, 'entity_key': key} for c in e.get('checks', [])]
        items['Relationships'] = list(m['relationships'])
        items['Examples'] = list(m.get('examples', []))
    ws.freeze_panes = 'A2'
    wide = {'definition': 50, 'description': 50, 'body': 70, 'sql': 60, 'filter': 40, 'rule': 60, 'sentence': 50, 'value': 28}
    for name in S:
        _sheet(wb, name, items[name], wide)
    _validations(wb)
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ read
class Report:
    def __init__(self):
        self.errors, self.warnings = [], []

    def err(self, sheet, row, col, msg):
        self.errors.append({'sheet': sheet, 'row': row, 'column': col, 'message': msg})

    def warn(self, sheet, row, col, msg):
        self.warnings.append({'sheet': sheet, 'row': row, 'column': col, 'message': msg})


def _s(v):
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.date().isoformat() if isinstance(v, datetime) else v.isoformat()
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return s or None


def _read_sheet(wb, name, rep, spec=None):
    if name not in wb.sheetnames:
        rep.err(name, None, None, f'sheet "{name}" is missing: start from the template')
        return []
    ws = wb[name]
    head = [_s(c.value) for c in ws[1]]
    spec = spec or S[name]
    known = {c for c, *_ in spec}
    for col, req, *_ in spec:
        if req and col not in head:
            rep.err(name, 1, col, f'required column "{col}" is missing in row 1: start from the template')
    idx = {h: i for i, h in enumerate(head) if h}
    custom_cols = [(h, i) for h, i in idx.items() if h not in known]
    out = []
    for r, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if all(v in (None, '') for v in row):
            continue
        d = {'_row': r}
        for col, req, kind, _h in spec:
            if col not in idx:
                d[col] = None
                continue
            v = _s(row[idx[col]] if idx[col] < len(row) else None)
            if v is None:
                if req:
                    rep.err(name, r, col, 'required, but empty')
                d[col] = None
                continue
            try:
                if kind == 'list':
                    v = [x.strip() for x in re.split(r'\s*\|\s*|\n', v) if x.strip()]
                elif kind == 'lines':
                    lines = {}
                    for ln in v.split('\n'):
                        if ln.strip():
                            k, _, rest = ln.partition(':')
                            lines[k.strip()] = [x.strip() for x in rest.split('>') if x.strip()]
                    v = lines
                elif kind == 'json':
                    v = json.loads(v)
                elif kind == 'enum:yesno':
                    low = v.lower()
                    if low not in ('yes', 'no', 'true', 'false', '1', '0'):
                        rep.err(name, r, col, f'"{v}": use yes or no')
                    v = low in ('yes', 'true', '1')
                elif kind.startswith('enum:'):
                    if v not in ENUMS[kind[5:]]:
                        if kind == 'enum:rel_status':
                            rep.err(name, r, col, f'"{v}" is not a relationship status: use {", ".join(ENUMS["rel_status"])}')
                        else:
                            rep.warn(name, r, col, f'"{v}" is not one of the usual values ({", ".join(ENUMS[kind[5:]])}); kept as is')
                elif kind == 'key':
                    if not KEY_RE.match(v) and name == 'Entities':
                        rep.err(name, r, col, f'"{v}": use lower_snake_case (letters, digits, underscores; starts with a letter)')
            except json.JSONDecodeError as e:
                rep.err(name, r, col, f'not valid JSON ({e.msg})')
                v = None
            d[col] = v
        custom = {h: _s(row[i]) for h, i in custom_cols if i < len(row) and _s(row[i]) is not None}
        if custom:
            d['custom'] = custom
        out.append(d)
    return out


def _dup(rep, sheet, rows, keyf, what):
    seen = {}
    for d in rows:
        k = keyf(d)
        if None in k:
            continue
        if k in seen:
            rep.err(sheet, d['_row'], None, f'{what} {"/".join(k)} is also on row {seen[k]}')
        seen[k] = d['_row']


def _pick(d, keys):
    return {k: d[k] for k in keys if d.get(k) not in (None, [], {})}


def read_workbook(data):
    """bytes of an .xlsx -> (model dict | None, report). The model has the assembled shape (see ontology.py)."""
    rep = Report()
    try:
        wb = load_workbook(io.BytesIO(data), data_only=True)
    except Exception as e:
        rep.err(None, None, None, f'this is not a readable .xlsx file ({type(e).__name__})')
        return None, rep
    legacy = 'Examples' not in wb.sheetnames and 'Knowledge' in wb.sheetnames
    sheets = {n: _read_sheet(wb, n, rep) for n in S if not (n == 'Examples' and legacy)}
    if legacy:
        old = _read_sheet(wb, 'Knowledge', rep, LEGACY_KNOWLEDGE)
        sheets['Examples'] = [{**d, 'sql': d.get('sql')} for d in old if d.get('type') == 'example']
        left = len(old) - len(sheets['Examples'])
        if left:
            rep.warn('Knowledge', None, None, f'{left} rule, term, process or note row(s) of the old Knowledge sheet were left out: they belong in the document Library now. Only the examples were read.')
    if rep.errors and any(e['row'] in (None, 1) for e in rep.errors):
        return None, rep                          # the layout is wrong: row-level messages would be noise
    _dup(rep, 'Entities', sheets['Entities'], lambda d: (d['key'],), 'entity key')
    ents, names = {}, {}
    for d in sheets['Entities']:
        if not d.get('key'):
            continue
        x = _pick(d, ('name', 'domain', 'kind', 'definition', 'semantic_table', 'primary_key', 'business_key', 'system_of_record', 'belongs_to', 'subtype_of',
                      'model_status', 'not_to_confuse_with', 'example_questions', 'lifecycle_states', 'custom'))
        x['id'] = d['key']
        x['synonyms'] = d.get('synonyms') or []
        std = {k: v for k, v in (('resource', d.get('standard_resource')), ('reference', d.get('standard_reference')), ('open_api', d.get('standard_detail'))) if v}
        if std:
            x['tmf'] = std
        x.update(attributes={}, calculated={}, filters=[], checks=[])
        ents[d['key']] = x
        if x.get('name'):
            if x['name'] in names:
                rep.err('Entities', d['_row'], 'name', f'entity name "{x["name"]}" is also used by {names[x["name"]]}')
            names[x['name']] = d['key']

    def entity(sheet, d):
        k = d.get('entity_key')
        if k and k not in ents:
            rep.err(sheet, d['_row'], 'entity_key', f'unknown entity "{k}" (not in the Entities sheet)')
            return None
        return ents.get(k)

    _dup(rep, 'Fields', sheets['Fields'], lambda d: (d.get('entity_key'), d.get('column')), 'field')
    for d in sheets['Fields']:
        e = entity('Fields', d)
        if e is None or not d.get('column'):
            continue
        f = _pick(d, ('role', 'type', 'business_name', 'description', 'sensitivity', 'agent_access', 'review_status', 'references', 'format', 'example',
                      'business_term', 'business_term_description', 'unit', 'sql', 'custom'))
        if d.get('standard_name'):
            f['tmf_name'] = d['standard_name']
        f = editing.with_defaults(f)
        if f.get('sensitivity') == 'restricted' and f.get('agent_access') != 'none':
            rep.err('Fields', d['_row'], 'agent_access', 'a restricted field must have agent_access none')
        if f.get('example') and f.get('sensitivity') == 'restricted':
            rep.warn('Fields', d['_row'], 'example', 'a restricted field should not carry an example value')
        (e['calculated'] if f.get('role') == 'calculated' else e['attributes'])[d['column']] = f
    for d in sheets['Values']:
        e = entity('Values', d)
        if e is None or not d.get('column') or d.get('value') is None:
            continue
        a = e['attributes'].get(d['column']) or e['calculated'].get(d['column'])
        if a is None:
            rep.err('Values', d['_row'], 'column', f'{d["entity_key"]}.{d["column"]} is not in the Fields sheet')
            continue
        a.setdefault('allowed_values', []).append(d['value'])
        if d.get('label'):
            a.setdefault('value_labels', {})[d['value']] = d['label']
        if d.get('note'):
            a.setdefault('value_notes', {})[d['value']] = d['note']
    for sheet, field, keys in (('Filters', 'filters', ('name', 'filter', 'description', 'source', 'custom')), ('Checks', 'checks', ('id', 'rule', 'sql', 'note', 'custom'))):
        key = (lambda d: (d.get('entity_key'), (d.get('name') or '').lower() or None)) if sheet == 'Filters' else (lambda d: (d.get('entity_key'), d.get('id')))
        _dup(rep, sheet, sheets[sheet], key, sheet[:-1].lower())
        for d in sheets[sheet]:
            e = entity(sheet, d)
            if e is not None:
                x = _pick(d, keys)
                if sheet == 'Filters':
                    x.update(synonyms=d.get('synonyms') or [], status=d.get('status') or 'working')
                e[field].append(x)
    rels, used = [], set()
    for d in sheets['Relationships']:
        for end in ('from', 'to'):
            if d.get(end) and not editing.ref_ok(d[end], names):
                rep.err('Relationships', d['_row'], end, f'unknown entity name "{d[end]}": use a name from the Entities sheet')
        if d.get('id'):
            if d['id'] in used:
                rep.err('Relationships', d['_row'], 'id', f'relationship id {d["id"]} is used twice')
            used.add(d['id'])
        rels.append(d)
    n, relationships = 0, []
    for d in rels:
        rid = d.get('id')
        while not rid:
            n += 1
            rid = f'R-{n:03d}'
            if rid in used:
                rid = None
        used.add(rid)
        r = {'id': rid, **_pick(d, ('from', 'predicate', 'to', 'cardinality', 'join', 'status', 'status_detail', 'sentence', 'note', 'custom'))}
        relationships.append(r)
    exs, kids, n = [], set(), 0
    for d in sheets['Examples']:
        if d.get('id'):
            if d['id'] in kids:
                rep.err('Examples', d['_row'], 'id', f'example id {d["id"]} is used twice')
            kids.add(d['id'])
    for d in sheets['Examples']:
        kid = d.get('id')
        while not kid:
            n += 1
            kid = f'EX{n:02d}'
            if kid in kids:
                kid = None
        kids.add(kid)
        k = {'id': kid, 'title': d.get('title'), 'body': d.get('body') or '', 'applies_to': d.get('applies_to') or [], **_pick(d, ('sql', 'params', 'sample_sql', 'topic', 'source', 'custom'))}
        if not k.get('sql'):
            rep.err('Examples', d['_row'], 'sql', 'an example needs its SQL')
        for a in k['applies_to']:
            if a not in ents:
                rep.err('Examples', d['_row'], 'applies_to', f'unknown entity key "{a}"')
        exs.append(k)
    # model settings: the known rows, and any other row as a custom property of the model
    rows = {}
    if 'Model' in wb.sheetnames:
        for row in wb['Model'].iter_rows(min_row=2, values_only=True):
            if row and row[0] and _s(row[0]):
                rows[str(row[0]).strip()] = _s(row[1]) if len(row) > 1 else None
    known = {k for k, _ in MODEL_ROWS}
    onto = {k: rows[k] for k in ('name', 'id', 'version', 'date', 'source', 'status') if rows.get(k)}
    for k in ('standards', 'how_to_read'):
        if rows.get(k):
            onto[k] = [x.strip() for x in rows[k].split('|') if x.strip()]
    custom = {k: v for k, v in rows.items() if k not in known and v is not None}
    if custom:
        onto['custom'] = custom
    access_policy = {k: rows[f'access.{k}'] for k in ('internal', 'confidential', 'personal', 'restricted', 'row_level') if rows.get(f'access.{k}')}
    if not onto.get('name'):
        rep.err('Model', None, 'name', 'the Model sheet needs a name')
    if not ents:
        rep.err('Entities', None, None, 'no entities found')
    return {'ontology': onto, 'access_policy': access_policy, 'entities': ents, 'relationships': relationships, 'examples': exs}, rep
