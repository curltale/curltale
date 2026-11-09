import threading
import time
from .db import conn, J
from . import editing, indexer, ontology as O


def list_versions():
    with conn() as c:
        return c.execute('SELECT v.id,v.label,v.status,v.note,v.created_at,v.published_at,'
                         '(SELECT count(*) FROM audit_log a WHERE a.version_id=v.id) AS edits FROM ontology_version v ORDER BY v.id DESC').fetchall()


def create_draft(label, note=None):
    with conn() as c:
        if c.execute("SELECT 1 FROM ontology_version WHERE status='draft'").fetchone():
            raise editing.EditError('a draft already exists')
        pub = O.published_id(c)
        if not pub:
            raise editing.EditError('nothing published yet to base the draft on')
        return O.clone_version(c, pub, label, note)


def discard_draft():
    with conn() as c:
        r = c.execute("DELETE FROM ontology_version WHERE status='draft' RETURNING id").fetchone()
        if not r:
            raise editing.EditError('no draft')
        return r['id']


def publish(user, progress=None):
    with conn() as c:
        r = c.execute("SELECT id FROM ontology_version WHERE status='draft'").fetchone()
        if not r:
            raise editing.EditError('no draft to publish')
        vid = r['id']
    errs = editing.validate(vid)
    if errs:
        raise editing.EditError('cannot publish:\n- ' + '\n- '.join(errs[:20]))
    stats = indexer.reindex(vid, progress=progress)          # embeddings are computed before the switch; failure leaves the old version live
    with conn() as c:
        c.execute("UPDATE ontology_version SET status='archived' WHERE status='published'")
        c.execute("UPDATE ontology_version SET status='published', published_at=now() WHERE id=%s", (vid,))
        c.execute("INSERT INTO audit_log (username,version_id,action,kind,key) VALUES (%s,%s,'publish','version',%s)", (user, vid, str(vid)))
        # old archived versions keep their rows but not their chunks (derived, rebuildable)
        c.execute("DELETE FROM chunk WHERE version_id IN (SELECT id FROM ontology_version WHERE status='archived')")
    return {'version': vid, **stats}


def _flat(vid):
    """(kind, key) -> data for every row of a version."""
    out = {}
    with conn() as c:
        for r in c.execute('SELECT * FROM entity WHERE version_id=%s', (vid,)).fetchall():
            out[('entity', r['key'])] = {**{k: r[k] for k in ('name', 'domain', 'kind', 'definition', 'semantic_table', 'primary_key', 'business_key', 'system_of_record', 'synonyms', 'filters', 'checks')},
                                         **(r['extra'] or {})}
        for r in c.execute('SELECT * FROM field WHERE version_id=%s', (vid,)).fetchall():
            out[('calculated' if r['role'] == 'calculated' else 'field', f"{r['entity_key']}.{r['col']}")] = O.field_dict(r)
        for r in c.execute('SELECT * FROM relationship WHERE version_id=%s', (vid,)).fetchall():
            out[('relationship', r['rid'])] = {'from': r['from_entity'], 'to': r['to_entity'], 'predicate': r['predicate'], 'cardinality': r['cardinality'],
                                               'join': r['join_cond'], 'status': r['status'], **(r['extra'] or {})}
        for r in c.execute('SELECT * FROM example WHERE version_id=%s', (vid,)).fetchall():
            out[('example', r['id'])] = {'title': r['title'], 'body': r['body'], 'applies_to': r['applies_to'], **(r['extra'] or {})}
    return out


def diff(a, b):
    """Changes going from version a to version b."""
    fa, fb = _flat(a), _flat(b)
    res = []
    for k in sorted(set(fa) | set(fb)):
        if k not in fa:
            res.append({'kind': k[0], 'key': k[1], 'change': 'added'})
        elif k not in fb:
            res.append({'kind': k[0], 'key': k[1], 'change': 'removed'})
        elif fa[k] != fb[k]:
            fields = {f: {'from': fa[k].get(f), 'to': fb[k].get(f)} for f in set(fa[k]) | set(fb[k]) if fa[k].get(f) != fb[k].get(f)}
            res.append({'kind': k[0], 'key': k[1], 'change': 'changed', 'fields': fields})
    return res


def audit(vid=None, limit=200):
    with conn() as c:
        if vid:
            return c.execute('SELECT * FROM audit_log WHERE version_id=%s ORDER BY id DESC LIMIT %s', (vid, limit)).fetchall()
        # Library events name collections and people: they are shown to administrators only (Activity page), never to every signed-in user
        return c.execute("SELECT * FROM audit_log WHERE coalesce(kind,'') <> 'collection' ORDER BY id DESC LIMIT %s", (limit,)).fetchall()


def import_excel(data, user, label=None, replace_draft=False, dry_run=True):
    """Read a filled template. Dry run: only the report. Otherwise create a DRAFT from it (never published); an existing draft must be replaced explicitly."""
    from . import excel_io
    model, rep = excel_io.read_workbook(data)
    out = {'errors': rep.errors, 'warnings': rep.warnings, 'counts': None, 'created': None}
    if model is None:
        return out
    problems = editing.validate_model(model)                       # the same checks as publishing
    out['errors'] = rep.errors + [{'sheet': None, 'row': None, 'column': None, 'message': p} for p in problems]
    ents = model['entities']
    out['counts'] = {'entities': len(ents), 'fields': sum(len(e['attributes']) for e in ents.values()), 'calculated': sum(len(e['calculated']) for e in ents.values()),
                     'filters': sum(len(e['filters']) for e in ents.values()), 'checks': sum(len(e['checks']) for e in ents.values()),
                     'relationships': len(model['relationships']), 'examples': len(model['examples'])}
    if dry_run or out['errors']:
        return out
    with conn() as c:
        if c.execute("SELECT 1 FROM ontology_version WHERE status='draft'").fetchone():
            if not replace_draft:
                raise editing.EditError('a draft already exists: publish or discard it first, or choose to replace it')
            c.execute("DELETE FROM ontology_version WHERE status='draft'")
    label = label or f"Excel upload {model['ontology'].get('version', '')}".strip()
    vid = O.import_model(model, label, note=f'uploaded from Excel by {user}', publish=False)
    with conn() as c:
        c.execute("INSERT INTO audit_log (username,version_id,action,kind,key) VALUES (%s,%s,'import','excel',%s)", (user, vid, label))
    out['created'] = vid
    out['sql_warnings'] = editing.sql_warnings(O.assemble(vid))    # advice: SQL in filters and checks that does not run on the connected database
    return out


def create_draft_from_model(model, user, label, replace_draft=False, note=None, source='import'):
    """A model dict becomes the draft (never published). An existing draft is replaced only when asked."""
    with conn() as c:
        if c.execute("SELECT 1 FROM ontology_version WHERE status='draft'").fetchone():
            if not replace_draft:
                raise editing.EditError('a draft already exists: publish or discard it first, or choose to replace it')
            c.execute("DELETE FROM ontology_version WHERE status='draft'")
    vid = O.import_model(model, label, note=note, publish=False)
    with conn() as c:
        c.execute("INSERT INTO audit_log (username,version_id,action,kind,key) VALUES (%s,%s,'import',%s,%s)", (user, vid, source, label))
    return vid


BACKUP_DIR = '/app/data/backups'


def reset_all(user, remove_demo=True):
    """Start from zero: save the current ontology as an Excel backup, delete every version, forget the demo database's builds and (optionally) drop it.
    Connectors, users, tokens, the activity log and the examples-free app settings stay."""
    import os
    import time
    from . import datastore, excel_io
    with conn() as c:
        row = c.execute("SELECT id FROM ontology_version ORDER BY (status='published') DESC, (status='draft') DESC, id DESC LIMIT 1").fetchone()
    backup = None
    if row:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        backup = f"ontology-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-v{row['id']}.xlsx"
        with open(os.path.join(BACKUP_DIR, backup), 'wb') as f:
            f.write(excel_io.build_workbook(O.assemble(row['id'])))
    with conn() as c:
        n = c.execute('SELECT count(*) AS n FROM ontology_version').fetchone()['n']
        c.execute('DELETE FROM ontology_version')                               # entities, fields, relationships, examples, meta and chunks go with it
        c.execute('DELETE FROM data_build')
        c.execute("INSERT INTO audit_log (username,action,kind,key) VALUES (%s,'reset','ontology',%s)", (user, backup or ''))
    if remove_demo:
        from .synth import load
        load.drop_builtin()
    datastore.reset_pools()
    datastore._cats.clear()
    from . import databuild, mcp_server, tools
    databuild._catalogs.clear()
    tools.invalidate()
    mcp_server.refresh_instructions()
    return {'versions_deleted': n, 'backup': backup, 'demo_database_removed': remove_demo}


def backups():
    import os
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if f.endswith('.xlsx'):
            st = os.stat(os.path.join(BACKUP_DIR, f))
            out.append({'name': f, 'bytes': st.st_size, 'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(st.st_mtime))})
    return out


# ---- publishing as a background job: embedding a whole ontology can take minutes, longer than a browser or proxy waits for one request
_job = {'state': 'idle'}
_job_lock = threading.Lock()


def publish_job():
    with _job_lock:
        return dict(_job)


def four_eyes():
    with conn() as c:
        r = c.execute("SELECT value FROM app_setting WHERE key='four_eyes'").fetchone()
    return bool(r and r['value'])


def editors(vid):
    """Who changed this draft (edits and uploads)."""
    with conn() as c:
        return [r['username'] for r in c.execute("SELECT DISTINCT username FROM audit_log WHERE version_id=%s AND action IN ('create','update','delete','import') ORDER BY 1", (vid,))]


def publish_start(user):
    """Validate the draft now (fast, errors come back at once), then publish in the background. The new version goes live only when it is fully indexed."""
    with _job_lock:
        if _job.get('state') == 'running':
            raise editing.EditError('a publish is already running')
    with conn() as c:
        r = c.execute("SELECT id FROM ontology_version WHERE status='draft'").fetchone()
    if not r:
        raise editing.EditError('no draft to publish')
    if four_eyes() and user in editors(r['id']):
        raise editing.EditError('four-eyes rule: you changed this draft, so someone else with the publisher role must publish it')
    errs = editing.validate(r['id'])
    if errs:
        raise editing.EditError('cannot publish:\n- ' + '\n- '.join(errs[:20]))
    with _job_lock:
        _job.clear()
        _job.update(state='running', version=r['id'], phase='building the search index', done=0, total=0, started=time.time(), user=user)
    threading.Thread(target=_publish_run, args=(user,), daemon=True).start()
    return publish_job()


def _publish_run(user):
    from . import mcp_server, tools

    def progress(done, total):
        with _job_lock:
            _job.update(phase='embedding the search index' if total else 'building the search index', done=done, total=total)
    try:
        res = publish(user, progress)
        tools.invalidate()
        mcp_server.refresh_instructions()
        with _job_lock:
            _job.update(state='done', result=res, finished=time.time())
    except Exception as e:                                         # the draft stays a draft; embeddings done so far are kept
        with _job_lock:
            _job.update(state='error', error=str(e)[:1500], finished=time.time())
