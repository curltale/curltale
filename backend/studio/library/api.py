"""Library endpoints: collections and who may use them."""
import hashlib
import json
import re
from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from .. import access, auth, db, llm, ratelimit, settings
from . import fetch as fetching, jobs, netguard, pipeline, retrieval, scope as S, storage, topics as topics_mod, vocab, health as health_mod, bridge, interview as interview_mod, export as export_mod  # noqa: F401  (pipeline registers the job handlers)

router = APIRouter(prefix='/api/library')


def _on():
    if not settings.get('LIBRARY_ENABLED'):
        raise HTTPException(404, 'the Library is switched off')


def scope_of(u=Depends(auth.current_user)):
    _on()
    return S.for_user(u)


def _audit(user, action, cid, before=None, after=None):
    with db.conn() as c:
        c.execute("INSERT INTO audit_log (username,action,kind,key,before,after) VALUES (%s,%s,'collection',%s,%s,%s)",
                  (user, action, str(cid), db.J(before) if before is not None else None, db.J(after) if after is not None else None))


def _ingest_limit(user):
    ok, wait = ratelimit.INGEST.allow(user['username'])
    if not ok:
        raise HTTPException(429, f"You can start {settings.get('INGEST_PER_MIN')} heavy actions a minute: try again in {wait} s", headers={'Retry-After': str(wait)})


def _slug(name):
    s = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:60]
    return s or 'collection'


class NewCollection(BaseModel):
    name: str
    description: str = ''
    usage: str = 'internal'
    allow_ai: bool = True
    agents_may_read: bool = True
    manager: str | None = None


class CollectionPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    usage: str | None = None
    allow_ai: bool | None = None
    agents_may_read: bool | None = None


class GrantBody(BaseModel):
    username: str | None = None
    token_id: int | None = None
    level: str


@router.get('/collections')
def list_collections(sc=Depends(scope_of)):
    return {'collections': S.visible(sc), 'usage_help': {'public': 'May be shared with customers.', 'internal': "For the agent's own understanding: do not quote it or refer to it with customers."}}


@router.post('/collections')
def create_collection(body: NewCollection, u=Depends(auth.admin_user)):
    _on()
    name = body.name.strip()
    if not name:
        raise HTTPException(422, 'a name is required')
    if body.usage not in ('public', 'internal'):
        raise HTTPException(422, 'usage must be public or internal')
    with db.conn() as c:
        if body.manager and not c.execute('SELECT 1 FROM app_user WHERE username=%s', (body.manager,)).fetchone():
            raise HTTPException(422, f'no such user: {body.manager}')
        base, slug, n = _slug(name), None, 1
        while True:
            slug = base if n == 1 else f'{base}-{n}'
            if not c.execute('SELECT 1 FROM library.collection WHERE slug=%s', (slug,)).fetchone():
                break
            n += 1
        r = c.execute('INSERT INTO library.collection (slug,name,description,usage,allow_ai,agents_may_read,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id', (slug, name, body.description.strip(), body.usage, body.allow_ai, body.agents_may_read, u['username'])).fetchone()
        if body.manager:
            c.execute("INSERT INTO library.grant_ (collection_id,username,level,granted_by) VALUES (%s,%s,'manage',%s)", (r['id'], body.manager, u['username']))
    vocab.seed(r['id'])
    _audit(u['username'], 'create', r['id'], after={'name': name, 'usage': body.usage, 'allow_ai': body.allow_ai, 'agents_may_read': body.agents_may_read, 'manager': body.manager})
    return {'id': str(r['id']), 'slug': slug}


@router.patch('/collections/{ref}')
def patch_collection(ref: str, body: CollectionPatch, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col = S.open_collection(sc, ref, 'manage')
    if body.usage is not None and body.usage not in ('public', 'internal'):
        raise HTTPException(422, 'usage must be public or internal')
    if body.name is not None and not body.name.strip():
        raise HTTPException(422, 'a name is required')
    after = {k: (v.strip() if isinstance(v, str) else v) for k, v in body.model_dump().items() if v is not None}
    if after:
        with db.conn() as c:
            c.execute('UPDATE library.collection SET name=coalesce(%s,name), description=coalesce(%s,description), usage=coalesce(%s,usage), allow_ai=coalesce(%s,allow_ai), agents_may_read=coalesce(%s,agents_may_read) WHERE id=%s', (after.get('name'), after.get('description'), after.get('usage'), after.get('allow_ai'), after.get('agents_may_read'), col.id))
        _audit(u['username'], 'update', col.id, before={'name': col.name, 'description': col.description, 'usage': col.usage, 'allow_ai': col.allow_ai, 'agents_may_read': col.agents_may_read}, after=after)
    return {'ok': True}


@router.delete('/collections/{ref}')
def delete_collection(ref: str, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col = S.open_collection(sc, ref, 'manage')
    try:
        storage.store().delete_prefix(col.id)                                         # the files first: if the store is unreachable nothing is half deleted
    except Exception as e:
        raise HTTPException(502, f'the stored files could not be removed ({type(e).__name__}); the collection was not deleted')
    with db.conn() as c:
        c.execute('DELETE FROM library.collection WHERE id=%s', (col.id,))          # content tables cascade from here as they are added
    _audit(u['username'], 'delete', col.id, before={'name': col.name})
    return {'ok': True}


@router.get('/collections/{ref}/grants')
def grants(ref: str, sc=Depends(scope_of)):
    col = S.open_collection(sc, ref, 'manage')
    with db.conn() as c:
        rows = c.execute("SELECT g.id, g.username, g.token_id, t.name AS token_name, g.level, g.granted_by, g.granted_at, u.role AS user_role, u.extra_roles "
                         "FROM library.grant_ g LEFT JOIN app_user u USING (username) LEFT JOIN api_token t ON t.id=g.token_id WHERE g.collection_id=%s ORDER BY g.id", (col.id,)).fetchall()
    return [{'id': r['id'], 'kind': 'user' if r['username'] else 'token', 'who': r['username'] or r['token_name'], 'token_id': r['token_id'], 'level': r['level'],
             'needs_role': ('editor' if r['level'] != 'read' else 'any') if r['username'] else 'read only',
             'has_role': ('editor' in auth.roles_of({'role': r['user_role'], 'extra_roles': r['extra_roles']})) if r['username'] else None,
             'granted_by': r['granted_by'], 'granted_at': r['granted_at']} for r in rows]


@router.put('/collections/{ref}/grants')
def set_grant(ref: str, body: GrantBody, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col = S.open_collection(sc, ref, 'manage')
    if body.level not in S.LEVELS:
        raise HTTPException(422, f"level must be one of {', '.join(S.LEVELS)}")
    if (body.username is None) == (body.token_id is None):
        raise HTTPException(422, 'give either a username or a token_id')
    if body.token_id is not None and body.level != 'read':
        raise HTTPException(422, 'API tokens are read only')
    with db.conn() as c:
        if body.username and not c.execute('SELECT 1 FROM app_user WHERE username=%s', (body.username,)).fetchone():
            raise HTTPException(422, f'no such user: {body.username}')
        if body.token_id is not None and not c.execute('SELECT 1 FROM api_token WHERE id=%s', (body.token_id,)).fetchone():
            raise HTTPException(422, 'no such token')
        key, val = ('username', body.username) if body.username else ('token_id', body.token_id)
        old = c.execute(f'SELECT level FROM library.grant_ WHERE collection_id=%s AND {key}=%s', (col.id, val)).fetchone()
        if old and old['level'] == 'manage' and body.level != 'manage' and c.execute("SELECT count(*) AS n FROM library.grant_ WHERE collection_id=%s AND level='manage'", (col.id,)).fetchone()['n'] < 2:
            raise HTTPException(422, 'this is the only manager of the collection: appoint another first')
        c.execute(f'INSERT INTO library.grant_ (collection_id,{key},level,granted_by) VALUES (%s,%s,%s,%s) ON CONFLICT (collection_id,{key}) WHERE {key} IS NOT NULL DO UPDATE SET level=EXCLUDED.level, granted_by=EXCLUDED.granted_by, granted_at=now()',
                  (col.id, val, body.level, u['username']))
    _audit(u['username'], 'grant', col.id, before={'level': old['level'] if old else None}, after={key: val, 'level': body.level})
    return {'ok': True}


@router.delete('/collections/{ref}/grants/{gid}')
def remove_grant(ref: str, gid: int, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col = S.open_collection(sc, ref, 'manage')
    with db.conn() as c:
        g = c.execute('SELECT * FROM library.grant_ WHERE id=%s AND collection_id=%s', (gid, col.id)).fetchone()
        if not g:
            raise HTTPException(404, 'no such grant')
        if g['level'] == 'manage' and c.execute("SELECT count(*) AS n FROM library.grant_ WHERE collection_id=%s AND level='manage'", (col.id,)).fetchone()['n'] < 2:
            raise HTTPException(422, 'this is the only manager of the collection: appoint another first')
        c.execute('DELETE FROM library.grant_ WHERE id=%s', (gid,))
    _audit(u['username'], 'revoke', col.id, before={'username': g['username'], 'token_id': g['token_id'], 'level': g['level']})
    return {'ok': True}


class SelfGrant(BaseModel):
    level: str = 'read'


@router.post('/collections/{ref}/self-grant')
def self_grant(ref: str, body: SelfGrant, u=Depends(auth.admin_user), sc=Depends(scope_of)):
    """An administrator reads or edits a collection's content only after granting themselves access, and that is written to the audit log."""
    col = S.open_collection(sc, ref, metadata_only=True)
    if body.level not in S.LEVELS:
        raise HTTPException(422, f"level must be one of {', '.join(S.LEVELS)}")
    with db.conn() as c:
        c.execute("INSERT INTO library.grant_ (collection_id,username,level,granted_by) VALUES (%s,%s,%s,%s) ON CONFLICT (collection_id,username) WHERE username IS NOT NULL DO UPDATE SET level=EXCLUDED.level, granted_by=EXCLUDED.granted_by, granted_at=now()",
                  (col.id, u['username'], body.level, u['username']))
    _audit(u['username'], 'self_grant', col.id, after={'username': u['username'], 'level': body.level})
    return {'ok': True}


# ------------------------------------------------------------------ sources
EXTENSIONS = {'pdf', 'docx', 'pptx', 'xlsx', 'csv', 'tsv', 'txt', 'md', 'html', 'htm', 'json', 'xml', 'png', 'jpg', 'jpeg', 'webp', 'gif', 'tif', 'tiff', 'bmp'}


def _src(r, st):
    return {'id': str(r['id']), 'name': r['name'], 'kind': r['kind'], 'url': r['url'], 'size': r['size'], 'status': r['status'], 'title': r['title'], 'stats': r['stats'], 'wiki_status': r['wiki_status'], 'error': r['error'], 'created_by': r['created_by'], 'created_at': r['created_at'],
            'job': st.get(str(r['id']))}


@router.get('/collections/{ref}/sources')
def list_sources(ref: str, sc=Depends(scope_of)):
    col = S.open_collection(sc, ref)
    with db.conn() as c:
        rows = c.execute('SELECT id,name,kind,url,size,status,title,stats,wiki_status,error,created_by,created_at FROM library.source WHERE collection_id=%s ORDER BY created_at DESC LIMIT 500', (col.id,)).fetchall()
    st = jobs.status_of([r['id'] for r in rows])
    return {'collection': {'id': col.id, 'name': col.name, 'usage': col.usage, 'level': col.level, 'allow_ai': col.allow_ai}, 'sources': [_src(r, st) for r in rows]}


@router.post('/collections/{ref}/sources')
def upload_source(ref: str, file: UploadFile = File(...), u=Depends(auth.current_user), sc=Depends(scope_of)):
    col = S.open_collection(sc, ref, 'write')
    _ingest_limit(u)
    name = storage.clean_name(file.filename)
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext not in EXTENSIONS:
        raise HTTPException(422, f"files of type '.{ext}' are not supported" if ext else 'the file needs an extension such as .pdf or .docx')
    try:
        fh, size, digest = storage.spool(file.file, settings.get('LIBRARY_MAX_UPLOAD_MB') * 1024 * 1024)
    except ValueError as e:
        raise HTTPException(413, str(e))
    if size == 0:
        raise HTTPException(422, 'the file is empty')
    with fh, db.conn() as c:
        dup = c.execute('SELECT id,name FROM library.source WHERE collection_id=%s AND sha256=%s', (col.id, digest)).fetchone()
        if dup:
            raise HTTPException(409, f"this file is already in the collection as '{dup['name']}'")
        r = c.execute('INSERT INTO library.source (collection_id,name,kind,mime,size,sha256,storage_key,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                      (col.id, name, 'file', file.content_type, size, digest, 'pending', u['username'])).fetchone()
        key = f"{col.id}/{r['id']}/{name}"
        try:
            storage.store().put(key, fh)
        except Exception as e:
            c.execute('DELETE FROM library.source WHERE id=%s', (r['id'],))
            raise HTTPException(502, f'the file could not be stored ({type(e).__name__}: {str(e)[:120]}). Check Settings, Storage')
        c.execute('UPDATE library.source SET storage_key=%s WHERE id=%s', (key, r['id']))
    jobs.enqueue('source.ingest', col.id, r['id'], user=u['username'])
    _audit(u['username'], 'upload', col.id, after={'source': str(r['id']), 'size': size})
    return {'id': str(r['id']), 'name': name, 'status': 'queued'}


@router.post('/collections/{ref}/sources/{sid}/retry')
def retry_source(ref: str, sid: str, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col = S.open_collection(sc, ref, 'write')
    with db.conn() as c:
        r = c.execute("UPDATE library.source SET status='queued', error=NULL WHERE id::text=%s AND collection_id=%s RETURNING id, kind", (sid, col.id)).fetchone()
    if not r:
        raise HTTPException(404, 'no such source')
    jobs.enqueue('source.fetch' if r['kind'] == 'url' else 'source.ingest', col.id, r['id'], user=u['username'])
    return {'ok': True}


@router.delete('/collections/{ref}/sources/{sid}')
def delete_source(ref: str, sid: str, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col = S.open_collection(sc, ref, 'write')
    with db.conn() as c:
        r = c.execute('SELECT id,name,storage_key FROM library.source WHERE id::text=%s AND collection_id=%s', (sid, col.id)).fetchone()
        if not r:
            raise HTTPException(404, 'no such source')
        try:
            storage.store().delete_prefix(f"{col.id}/{r['id']}")
        except Exception as e:
            raise HTTPException(502, f'the stored file could not be removed ({type(e).__name__}); nothing was deleted')
        topics_mod.withdraw_source(col.id, r['id'])                        # what the topics learned from it no longer stands
        c.execute('DELETE FROM library.source WHERE id=%s', (r['id'],))
    topics_mod.queue_revisions(col.id, u['username'])
    _audit(u['username'], 'delete_source', col.id, before={'source': str(r['id'])})
    return {'ok': True}


@router.get('/collections/{ref}/sources/{sid}/passages')
def source_passages(ref: str, sid: str, offset: int = 0, limit: int = 20, sc=Depends(scope_of)):
    col = S.open_collection(sc, ref)
    with db.conn() as c:
        if not c.execute('SELECT 1 FROM library.source WHERE id::text=%s AND collection_id=%s', (sid, col.id)).fetchone():
            raise HTTPException(404, 'no such source')
        rows = c.execute('SELECT idx,text,tokens,location,heading_path, embedding IS NOT NULL AS embedded FROM library.source_chunk WHERE source_id::text=%s AND collection_id=%s ORDER BY idx OFFSET %s LIMIT %s',
                         (sid, col.id, max(0, offset), min(max(1, limit), 100))).fetchall()
        total = c.execute('SELECT count(*) AS n FROM library.source_chunk WHERE source_id::text=%s AND collection_id=%s', (sid, col.id)).fetchone()['n']
    return {'total': total, 'passages': rows}


@router.get('/collections/{ref}/search')
def search_collection(ref: str, q: str, k: int = 8, sc=Depends(scope_of)):
    col = S.open_collection(sc, ref)
    return {'collection': col.name, 'usage': col.usage, 'usage_note': S.USAGE_NOTE[col.usage], 'query': q, 'passages': retrieval.search(col, q, k)}


@router.get('/collections/{ref}/sources/{sid}/wiki')
def source_wiki(ref: str, sid: str, version: int | None = None, sc=Depends(scope_of)):
    """The current (or a given) version of the source page, the checks made on each version, and the list of versions."""
    col = S.open_collection(sc, ref)
    with db.conn() as c:
        src = c.execute('SELECT id,name,title,wiki_status,status FROM library.source WHERE id::text=%s AND collection_id=%s', (sid, col.id)).fetchone()
        if not src:
            raise HTTPException(404, 'no such source')
        versions = c.execute('SELECT version,origin,created_by,created_at FROM library.wiki_version WHERE source_id=%s AND collection_id=%s ORDER BY version DESC', (src['id'], col.id)).fetchall()
        cur = next((v for v in versions if v['version'] == version), versions[0] if versions and version is None else None)
        page = c.execute('SELECT markdown FROM library.wiki_version WHERE source_id=%s AND collection_id=%s AND version=%s', (src['id'], col.id, cur['version'])).fetchone() if cur else None
        checks = c.execute('SELECT version,round,verdict,issues,tool_calls,at FROM library.wiki_check WHERE source_id=%s AND collection_id=%s ORDER BY id', (src['id'], col.id)).fetchall()
    return {'source': {'id': str(src['id']), 'name': src['name'], 'title': src['title'], 'status': src['status'], 'wiki_status': src['wiki_status']},
            'collection': {'name': col.name, 'usage': col.usage, 'usage_note': S.USAGE_NOTE[col.usage], 'level': col.level},
            'version': cur['version'] if cur else None, 'markdown': page['markdown'] if page else None, 'versions': versions, 'checks': checks}


@router.post('/collections/{ref}/sources/{sid}/wiki/regenerate')
def regenerate_wiki(ref: str, sid: str, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col = S.open_collection(sc, ref, 'write')
    _ingest_limit(u)
    if not col.allow_ai:
        raise HTTPException(422, 'this collection does not allow AI to read its documents (collection settings)')
    with db.conn() as c:
        r = c.execute("UPDATE library.source SET wiki_status=wiki_status WHERE id::text=%s AND collection_id=%s AND status='ready' RETURNING id", (sid, col.id)).fetchone()
    if not r:
        raise HTTPException(404, 'no such source, or it is not ready yet')
    jobs.enqueue('source.wiki', col.id, r['id'], user=u['username'])
    _audit(u['username'], 'regenerate_wiki', col.id, after={'source': str(r['id'])})
    return {'ok': True}


@router.post('/collections/{ref}/sources/{sid}/wiki/approve')
def approve_wiki(ref: str, sid: str, u=Depends(auth.current_user), sc=Depends(scope_of)):
    """A person accepts the current page as it is (after reading the checker's remaining objections)."""
    col = S.open_collection(sc, ref, 'write')
    with db.conn() as c:
        r = c.execute("UPDATE library.source SET wiki_status='approved' WHERE id::text=%s AND collection_id=%s AND wiki_status='needs_review' RETURNING id", (sid, col.id)).fetchone()
    if not r:
        raise HTTPException(409, 'only a page waiting for review can be approved')
    topics_mod.queue(col.id, r['id'], u['username'])
    _audit(u['username'], 'approve_wiki', col.id, after={'source': str(r['id'])})
    return {'ok': True}


class PageBody(BaseModel):
    markdown: str


@router.put('/collections/{ref}/sources/{sid}/wiki')
def edit_wiki(ref: str, sid: str, body: PageBody, u=Depends(auth.current_user), sc=Depends(scope_of)):
    """A person's version of the page. With AI available it is checked against the document (and approved if it passes); without, the person's approval is all there is."""
    col = S.open_collection(sc, ref, 'write')
    _ingest_limit(u)
    md = body.markdown.strip()
    if not md:
        raise HTTPException(422, 'the page cannot be empty')
    if len(md) > 300000:
        raise HTTPException(413, 'the page is too long')
    with db.conn() as c:
        src = c.execute("SELECT id FROM library.source WHERE id::text=%s AND collection_id=%s AND status='ready'", (sid, col.id)).fetchone()
    if not src:
        raise HTTPException(404, 'no such source, or it is not ready yet')
    from . import wiki
    ver = wiki.save_version(col.id, src['id'], md, 'edited', u['username'])
    checked = col.allow_ai and llm.available()
    with db.conn() as c:
        c.execute('UPDATE library.source SET wiki_status=%s WHERE id=%s', ('needs_review' if checked else 'approved', src['id']))
    if checked:
        jobs.enqueue('source.wiki_check', col.id, src['id'], user=u['username'])
    _audit(u['username'], 'edit_wiki', col.id, after={'source': str(src['id']), 'version': ver, 'checked': bool(checked)})
    return {'version': ver, 'checking': bool(checked)}


def _download(name, media, chunks):
    safe = storage.clean_name(name)
    return StreamingResponse(chunks, media_type=media, headers={'Content-Disposition': f'attachment; filename="{safe}"', 'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store'})


@router.get('/collections/{ref}/sources/{sid}/download/original')
def download_original(ref: str, sid: str, sc=Depends(scope_of)):
    col = S.open_collection(sc, ref)
    with db.conn() as c:
        r = c.execute('SELECT name, storage_key FROM library.source WHERE id::text=%s AND collection_id=%s', (sid, col.id)).fetchone()
    if not r or not r['storage_key'].startswith(col.id + '/'):
        raise HTTPException(404, 'no such source')
    try:
        fh = storage.store().open(r['storage_key'])
    except storage.StorageError as e:
        raise HTTPException(502, str(e))

    def gen():
        with fh:
            yield from iter(lambda: fh.read(1024 * 1024), b'')
    return _download(r['name'], 'application/octet-stream', gen())


@router.get('/collections/{ref}/sources/{sid}/download/page')
def download_page(ref: str, sid: str, version: int | None = None, sc=Depends(scope_of)):
    col = S.open_collection(sc, ref)
    with db.conn() as c:
        r = c.execute('SELECT s.name, w.markdown FROM library.source s JOIN library.wiki_version w ON w.source_id=s.id AND w.collection_id=s.collection_id '
                      'WHERE s.id::text=%s AND s.collection_id=%s AND (%s::int IS NULL OR w.version=%s) ORDER BY w.version DESC LIMIT 1', (sid, col.id, version, version)).fetchone()
    if not r:
        raise HTTPException(404, 'no page')
    return _download(r['name'].rsplit('.', 1)[0] + '.md', 'text/markdown; charset=utf-8', iter([r['markdown'].encode()]))


def review_count(sc):
    """What waits for this person across their collections: held changes, pages with objections, suggested model links and failed documents where they can write,
    and the analyzer's vocabulary proposals where they manage."""
    if sc.kind != 'user' or 'editor' not in sc.roles:
        return 0
    ids = [cid for cid in sc.grants if sc.level(cid) in ('write', 'manage')]
    mine = [cid for cid in ids if sc.level(cid) == 'manage']
    if not ids:
        return 0
    with db.conn() as c:
        q = lambda sql, arg: c.execute(sql, (arg,)).fetchone()['n']
        return (q("SELECT count(*) AS n FROM library.source WHERE collection_id::text = ANY(%s) AND wiki_status='needs_review'", ids)
                + q("SELECT count(*) AS n FROM library.topic_conflict WHERE collection_id::text = ANY(%s) AND status='open'", ids)
                + q("SELECT count(*) AS n FROM library.topic_model_ref r JOIN library.topic t ON t.id=r.topic_id AND t.collection_id=r.collection_id WHERE r.collection_id::text = ANY(%s) AND r.status='suggested' AND t.status='active'", ids)
                + q("SELECT count(*) AS n FROM library.source WHERE collection_id::text = ANY(%s) AND status='failed'", ids)
                + (q("SELECT count(*) AS n FROM library.vocab_proposal WHERE collection_id::text = ANY(%s) AND status='pending'", mine) if mine else 0))


@router.get('/collections/{ref}/inbox')
def collection_inbox(ref: str, sc=Depends(scope_of)):
    """Everything in this collection that waits for a person. The queue belongs to the collection: everyone who can write in it sees the same items, and the collection's
    manager also decides the vocabulary."""
    col = S.open_collection(sc, ref, 'write')
    with db.conn() as c:
        held = c.execute("SELECT k.id, k.kind, k.description, k.created_at, t.slug, t.title FROM library.topic_conflict k JOIN library.topic t ON t.id=k.topic_id AND t.collection_id=k.collection_id WHERE k.collection_id=%s AND k.status='open' ORDER BY k.id DESC", (col.id,)).fetchall()
        pages = c.execute("SELECT id, name, title FROM library.source WHERE collection_id=%s AND wiki_status='needs_review' ORDER BY created_at DESC", (col.id,)).fetchall()
        links = c.execute("SELECT t.slug, t.title, r.kind, r.ref, r.relation, r.confidence, r.reason FROM library.topic_model_ref r JOIN library.topic t ON t.id=r.topic_id AND t.collection_id=r.collection_id "
                          "WHERE r.collection_id=%s AND r.status='suggested' AND t.status='active' ORDER BY t.title", (col.id,)).fetchall()
        failed = c.execute("SELECT id, name, error, kind FROM library.source WHERE collection_id=%s AND status='failed' ORDER BY created_at DESC", (col.id,)).fetchall()
        props = c.execute("SELECT id, kind, name, reason, description FROM library.vocab_proposal WHERE collection_id=%s AND status='pending' ORDER BY id", (col.id,)).fetchall()
    onto = bridge._onto() or {}
    for l in links:
        e = (onto.get('entities') or {}).get(l['ref'].split('.')[0])
        l['name'] = e['name'] if e and l['kind'] == 'entity' else l['ref']
        l['label'] = bridge.LABEL[l['relation']]
    return {'collection': {'id': col.id, 'name': col.name, 'usage': col.usage, 'level': col.level},
            'held_changes': held, 'pages': [{**p, 'id': str(p['id'])} for p in pages], 'suggested_links': links, 'failed_sources': [{**f, 'id': str(f['id'])} for f in failed],
            'vocabulary': {'can_decide': col.level == 'manage', 'proposals': props}}


class UrlBody(BaseModel):
    url: str


@router.post('/collections/{ref}/sources/url')
def add_url(ref: str, body: UrlBody, u=Depends(auth.current_user), sc=Depends(scope_of)):
    """Capture a web address as a source. The address is checked now (public http/https only), and again for every redirect when it is fetched."""
    col = S.open_collection(sc, ref, 'write')
    _ingest_limit(u)
    url = body.url.strip()
    if not url or len(url) > 2000:
        raise HTTPException(422, 'give a web address')
    try:
        netguard.resolve(url, fetching.allowed_hosts())
    except netguard.Refused as e:
        raise HTTPException(422, str(e))
    import hashlib
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    name = (parts.hostname + (parts.path if parts.path != '/' else '') + (('?' + parts.query) if parts.query else ''))[:120]
    with db.conn() as c:
        dup = c.execute('SELECT name FROM library.source WHERE collection_id=%s AND url=%s', (col.id, url)).fetchone()
        if dup:
            raise HTTPException(409, 'this address is already in the collection')
        r = c.execute("INSERT INTO library.source (collection_id,name,kind,url,mime,size,sha256,storage_key,created_by) VALUES (%s,%s,'url',%s,'text/html',0,%s,'pending',%s) RETURNING id",
                      (col.id, name, url, 'url:' + hashlib.sha256(url.encode()).hexdigest(), u['username'])).fetchone()
    jobs.enqueue('source.fetch', col.id, r['id'], user=u['username'])
    _audit(u['username'], 'add_url', col.id, after={'source': str(r['id'])})
    return {'id': str(r['id']), 'name': name, 'status': 'queued'}


@router.post('/collections/{ref}/sources/{sid}/refresh')
def refresh_url(ref: str, sid: str, u=Depends(auth.current_user), sc=Depends(scope_of)):
    """Capture the address again. If the content has not changed nothing is redone."""
    col = S.open_collection(sc, ref, 'write')
    _ingest_limit(u)
    with db.conn() as c:
        r = c.execute("UPDATE library.source SET status='queued', error=NULL WHERE id::text=%s AND collection_id=%s AND kind='url' RETURNING id", (sid, col.id)).fetchone()
    if not r:
        raise HTTPException(404, 'no such web source')
    jobs.enqueue('source.fetch', col.id, r['id'], user=u['username'])
    return {'ok': True}


# ------------------------------------------------------------------ topics
@router.get('/collections/{ref}/topics')
def list_topics(ref: str, q: str | None = None, type: str | None = None, limit: int = 100, offset: int = 0, sc=Depends(scope_of)):
    col = S.open_collection(sc, ref)
    if q and q.strip():
        rows = topics_mod.search(col.id, q, min(limit, 30))
        return {'collection': {'name': col.name, 'usage': col.usage, 'usage_note': S.USAGE_NOTE[col.usage]}, 'total': len(rows), 'topics': rows}
    with db.conn() as c:
        rows = c.execute("SELECT t.slug, t.title, t.topic_type, t.summary, t.needs_revision, (SELECT count(*) FROM library.claim x WHERE x.topic_id=t.id AND x.status='active') AS claims, "
                         "(SELECT count(*) FROM library.topic_source s WHERE s.topic_id=t.id) AS sources FROM library.topic t WHERE t.collection_id=%s AND t.status='active' AND (%s::text IS NULL OR t.topic_type=%s) "
                         "ORDER BY lower(t.title) OFFSET %s LIMIT %s", (col.id, type, type, max(0, offset), min(max(1, limit), 500))).fetchall()
        total = c.execute("SELECT count(*) AS n FROM library.topic WHERE collection_id=%s AND status='active' AND (%s::text IS NULL OR topic_type=%s)", (col.id, type, type)).fetchone()['n']
        types = c.execute("SELECT topic_type AS name, count(*) AS n FROM library.topic WHERE collection_id=%s AND status='active' GROUP BY 1 ORDER BY 2 DESC", (col.id,)).fetchall()
        ids = {r['slug']: r['id'] for r in c.execute("SELECT slug, id FROM library.topic WHERE collection_id=%s AND slug = ANY(%s)", (col.id, [r['slug'] for r in rows])).fetchall()}
    kinds = bridge.kinds_of(col.id, list(ids.values()))
    for r in rows:
        r['kind_of'] = kinds.get(ids.get(r['slug']), [])
    return {'collection': {'name': col.name, 'usage': col.usage, 'usage_note': S.USAGE_NOTE[col.usage]}, 'total': total, 'types': types, 'topics': rows}


@router.get('/collections/{ref}/topics/{slug}')
def get_topic(ref: str, slug: str, version: int | None = None, sc=Depends(scope_of), u=Depends(auth.current_user)):
    """A topic page with its claims (each with its quote, the source wiki version and passage it comes from), links, aliases, sources and versions."""
    col = S.open_collection(sc, ref)
    t = topics_mod.find(col.id, slug)
    if not t:
        raise HTTPException(404, 'no such topic')
    moved_from = slug if t['slug'] != topics_mod.slugify(slug) and t['title'].lower() != slug.lower() and t['slug'] != slug else None
    with db.conn() as c:
        versions = c.execute("SELECT version, update_reason, created_at FROM library.topic_version WHERE topic_id=%s AND collection_id=%s AND status='published' ORDER BY version DESC", (t['id'], col.id)).fetchall()
        page = next((v for v in versions if v['version'] == version), versions[0] if versions and version is None else None)
        md = c.execute('SELECT markdown FROM library.topic_version WHERE topic_id=%s AND collection_id=%s AND version=%s', (t['id'], col.id, page['version'])).fetchone()['markdown'] if page else None
        claims = c.execute("SELECT id, n, text, kind, status FROM library.claim WHERE topic_id=%s AND collection_id=%s AND status='active' ORDER BY n", (t['id'], col.id)).fetchall()      # held (pending) statements are not published
        # each quote with the source wiki version it was taken from and the wiki passage that holds it
        ev = c.execute("SELECT cs.claim_id, cs.source_id, coalesce(s.title, s.name) AS source, s.kind AS source_kind, s.url, cs.quote, cs.wiki_version, cs.wiki_chunk, "
                       "(SELECT left(w.text, 900) FROM library.wiki_chunk w WHERE w.source_id=cs.source_id AND w.version=cs.wiki_version AND w.idx=cs.wiki_chunk) AS passage "
                       "FROM library.claim_source cs LEFT JOIN library.source s ON s.id=cs.source_id AND s.collection_id=cs.collection_id WHERE cs.claim_id = ANY(%s) AND cs.collection_id=%s ORDER BY cs.id", ([x['id'] for x in claims], col.id)).fetchall()
        out = c.execute("SELECT l.relation, l.context, o.slug, o.title, o.topic_type FROM library.topic_link l JOIN library.topic o ON o.id=l.to_topic AND o.collection_id=l.collection_id WHERE l.from_topic=%s AND l.collection_id=%s ORDER BY o.title", (t['id'], col.id)).fetchall()
        inc = c.execute("SELECT l.relation, l.context, o.slug, o.title, o.topic_type FROM library.topic_link l JOIN library.topic o ON o.id=l.from_topic AND o.collection_id=l.collection_id WHERE l.to_topic=%s AND l.collection_id=%s ORDER BY o.title", (t['id'], col.id)).fetchall()
        aliases = [r['alias'] for r in c.execute('SELECT alias FROM library.topic_alias WHERE topic_id=%s AND collection_id=%s ORDER BY alias', (t['id'], col.id)).fetchall()]
        sources = c.execute("SELECT s.id, coalesce(s.title, s.name) AS name, s.kind, s.url, ts.wiki_version FROM library.topic_source ts JOIN library.source s ON s.id=ts.source_id AND s.collection_id=ts.collection_id WHERE ts.topic_id=%s AND ts.collection_id=%s ORDER BY s.name", (t['id'], col.id)).fetchall()
    for x in claims:
        x['evidence'] = [{**e, 'source_id': str(e['source_id']) if e['source_id'] else None, 'source': e['source'] or '(deleted source)'} for e in ev if e['claim_id'] == x['id']]
        for e in x['evidence']:
            e.pop('claim_id', None)
    mine = bridge.resolve(bridge.refs_of(t['id'], col.id, include_rejected=False), access.normalize_user(u.get('entitlements')))
    return {'collection': {'name': col.name, 'usage': col.usage, 'usage_note': S.USAGE_NOTE[col.usage], 'level': col.level}, 'model_links': mine,
            'redirected_from': moved_from, 'topic': {'slug': t['slug'], 'title': t['title'], 'topic_type': t['topic_type'], 'kind_of': bridge.kinds_of(col.id, [t['id']]).get(t['id'], []), 'summary': t['summary'], 'needs_revision': t['needs_revision'], 'updated_at': t['updated_at']},
            'version': page['version'] if page else None, 'markdown': md, 'versions': versions, 'claims': claims, 'links': out, 'backlinks': inc, 'aliases': aliases,
            'sources': [{**s_, 'id': str(s_['id'])} for s_ in sources]}


@router.get('/collections/{ref}/vocabulary')
def get_vocabulary(ref: str, sc=Depends(scope_of)):
    col = S.open_collection(sc, ref)
    with db.conn() as c:
        props = c.execute("SELECT id, kind, name, description, reason, status, created_at FROM library.vocab_proposal WHERE collection_id=%s ORDER BY id DESC LIMIT 100", (col.id,)).fetchall()
        tcount = {r['topic_type']: r['n'] for r in c.execute("SELECT topic_type, count(*) AS n FROM library.topic WHERE collection_id=%s AND status<>'archived' GROUP BY 1", (col.id,)).fetchall()}
        lcount = {r['relation']: r['n'] for r in c.execute("SELECT relation, count(*) AS n FROM library.topic_link WHERE collection_id=%s GROUP BY 1", (col.id,)).fetchall()}
    v = vocab.get(col.id, include_rejected=True)
    return {'topic_types': [{**t, 'uses': tcount.get(t['name'], 0)} for t in v['topic_types']], 'relation_types': [{**r, 'uses': lcount.get(r['name'], 0)} for r in v['relation_types']],
            'proposals': props, 'protected': list(vocab.PROTECTED)}


class Decision(BaseModel):
    approve: bool


@router.post('/collections/{ref}/vocabulary/proposals/{pid}')
def decide_proposal(ref: str, pid: int, body: Decision, u=Depends(auth.current_user), sc=Depends(scope_of)):
    """The manager of the collection accepts or rejects a kind the analyzer proposed. Accepting makes it available to the whole collection."""
    col = S.open_collection(sc, ref, 'manage')
    with db.conn() as c:
        p = c.execute("UPDATE library.vocab_proposal SET status=%s WHERE id=%s AND collection_id=%s AND status='pending' RETURNING kind, name", ('approved' if body.approve else 'rejected', pid, col.id)).fetchone()
        if not p:
            raise HTTPException(404, 'no such pending proposal')
        c.execute(f"UPDATE library.{p['kind']} SET status=%s WHERE collection_id=%s AND name=%s", ('approved' if body.approve else 'rejected', col.id, p['name']))
        if not body.approve and p['kind'] == 'topic_type':
            c.execute("UPDATE library.topic SET topic_type='Topic' WHERE collection_id=%s AND topic_type=%s", (col.id, p['name']))
    _audit(u['username'], 'vocabulary_approved' if body.approve else 'vocabulary_rejected', col.id, after={'kind': p['kind'], 'name': p['name']})
    return {'ok': True}


# ------------------------------------------------------------------ the review gate
@router.get('/collections/{ref}/conflicts')
def list_conflicts(ref: str, status: str = 'open', sc=Depends(scope_of)):
    col = S.open_collection(sc, ref, 'write')                  # held changes are unpublished: only those who decide on them see them
    with db.conn() as c:
        rows = c.execute("SELECT k.id, k.kind, k.description, k.status, k.created_at, k.decided_by, k.decided_at, t.slug, t.title, d.slug AS duplicate_slug, d.title AS duplicate_title "
                         "FROM library.topic_conflict k JOIN library.topic t ON t.id=k.topic_id AND t.collection_id=k.collection_id LEFT JOIN library.topic d ON d.id=k.duplicate_of AND d.collection_id=k.collection_id "
                         "WHERE k.collection_id=%s AND (%s='all' OR k.status=%s) ORDER BY k.id DESC LIMIT 200", (col.id, status, status)).fetchall()
    return {'collection': {'name': col.name, 'level': col.level}, 'conflicts': rows}


@router.get('/collections/{ref}/conflicts/{kid}')
def get_conflict(ref: str, kid: int, sc=Depends(scope_of)):
    """A held change: the page as it is, the page as it would become, the new statements with their evidence, and what it clashes with."""
    col = S.open_collection(sc, ref, 'write')
    k = topics_mod.conflict_detail(col.id, kid)
    if not k:
        raise HTTPException(404, 'no such conflict')
    with db.conn() as c:
        t = c.execute('SELECT slug, title, topic_type, status FROM library.topic WHERE id=%s', (k['topic_id'],)).fetchone()
        held = c.execute('SELECT markdown FROM library.topic_version WHERE topic_id=%s AND version=%s', (k['topic_id'], k['version'])).fetchone()
        new = c.execute("SELECT cl.id, cl.n, cl.text, cl.status FROM library.claim cl WHERE cl.topic_id=%s AND cl.collection_id=%s AND EXISTS (SELECT 1 FROM library.claim_source s WHERE s.claim_id=cl.id AND s.source_id=%s) AND cl.status IN ('pending','active','rejected','superseded') ORDER BY cl.n",
                        (k['topic_id'], col.id, k['source_id'])).fetchall() if k['source_id'] else []
        ev = c.execute("SELECT cs.claim_id, s.name AS source, cs.quote, cs.location FROM library.claim_source cs JOIN library.source s ON s.id=cs.source_id AND s.collection_id=cs.collection_id WHERE cs.claim_id = ANY(%s)", ([x['id'] for x in new],)).fetchall()
        clash = c.execute("SELECT n, text FROM library.claim WHERE topic_id=%s AND n=%s", (k['topic_id'], k['contradicts_n'])).fetchone() if k['contradicts_n'] is not None else None
        dup = c.execute('SELECT slug, title, summary FROM library.topic WHERE id=%s AND collection_id=%s', (k['duplicate_of'], col.id)).fetchone() if k['duplicate_of'] else None
        src = c.execute('SELECT id, name FROM library.source WHERE id=%s AND collection_id=%s', (k['source_id'], col.id)).fetchone() if k['source_id'] else None
        names = {str(r['id']): r['name'] for r in c.execute('SELECT s.id, coalesce(s.title, s.name) AS name FROM library.source s WHERE s.collection_id=%s AND (s.id=%s OR s.id IN (SELECT source_id FROM library.topic_source WHERE topic_id=%s))',
                                                            (col.id, k['source_id'], k['topic_id'])).fetchall()}
    for x in new:
        x['evidence'] = [{kk: vv for kk, vv in e.items() if kk != 'claim_id'} for e in ev if e['claim_id'] == x['id']]
    return {'collection': {'name': col.name, 'level': col.level, 'usage': col.usage}, 'conflict': {k_: v for k_, v in k.items() if k_ not in ('collection_id',)}, 'topic': t,
            'current': topics_mod.current_markdown(k['topic_id']), 'proposed': held['markdown'] if held else None, 'new_claims': new, 'contradicts': clash, 'duplicate_of': dup,
            'source': {'id': str(src['id']), 'name': src['name']} if src else None, 'source_names': names}


def _decide(col, kid, u, fn, action, **kw):
    try:
        fn(col.id, kid, u['username'], **kw)
    except LookupError:
        raise HTTPException(404, 'no such open conflict')
    _audit(u['username'], action, col.id, after={'conflict': kid})
    return {'ok': True}


@router.post('/collections/{ref}/conflicts/{kid}/approve')
def approve_conflict(ref: str, kid: int, u=Depends(auth.current_user), sc=Depends(scope_of)):
    return _decide(S.open_collection(sc, ref, 'write'), kid, u, topics_mod.approve, 'approve_change')


@router.post('/collections/{ref}/conflicts/{kid}/reject')
def reject_conflict(ref: str, kid: int, u=Depends(auth.current_user), sc=Depends(scope_of)):
    return _decide(S.open_collection(sc, ref, 'write'), kid, u, topics_mod.reject, 'reject_change')


class MergeBody(BaseModel):
    into: str | None = None


@router.post('/collections/{ref}/conflicts/{kid}/redirect')
def redirect_conflict(ref: str, kid: int, body: MergeBody, u=Depends(auth.current_user), sc=Depends(scope_of)):
    """A probable duplicate is the same subject as an existing topic: fold the new topic into it."""
    col = S.open_collection(sc, ref, 'write')
    k = topics_mod.conflict_detail(col.id, kid)
    if not k or k['status'] != 'open' or k['kind'] != 'duplicate_topic':
        raise HTTPException(404, 'no such open duplicate')
    target = topics_mod.find(col.id, body.into) if body.into else None
    if body.into and not target:
        raise HTTPException(422, 'no such topic to merge into')
    into = target['id'] if target else k['duplicate_of']
    if not into:
        raise HTTPException(422, 'say which topic to merge into')
    if k['source_id']:                                                     # the held draft's source counts for the topic it is folded into
        with db.conn() as c:
            c.execute('INSERT INTO library.topic_source (collection_id,topic_id,source_id,wiki_version) SELECT %s,%s,%s,max(version) FROM library.wiki_version WHERE source_id=%s '
                      'ON CONFLICT (topic_id, source_id) DO NOTHING', (col.id, k['topic_id'], k['source_id'], k['source_id']))
    slug = topics_mod.merge(col.id, k['topic_id'], into, u['username'], 'merged as a duplicate')
    _audit(u['username'], 'merge_topics', col.id, after={'conflict': kid, 'into': slug})
    return {'ok': True, 'slug': slug}


@router.post('/collections/{ref}/topics/{slug}/merge')
def merge_topic(ref: str, slug: str, body: MergeBody, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col = S.open_collection(sc, ref, 'write')
    src, dst = topics_mod.find(col.id, slug, follow=False), topics_mod.find(col.id, body.into or '')
    if not src or not dst:
        raise HTTPException(404, 'no such topic')
    try:
        into = topics_mod.merge(col.id, src['id'], dst['id'], u['username'], f"merged by {u['username']}")
    except ValueError as e:
        raise HTTPException(422, str(e))
    _audit(u['username'], 'merge_topics', col.id, after={'from': src['slug'], 'into': into})
    return {'ok': True, 'slug': into}


@router.post('/collections/{ref}/topics/{slug}/revise')
def revise_topic_now(ref: str, slug: str, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col = S.open_collection(sc, ref, 'write')
    _ingest_limit(u)
    t = topics_mod.find(col.id, slug, follow=False)
    if not t:
        raise HTTPException(404, 'no such topic')
    with db.conn() as c:
        c.execute('UPDATE library.topic SET needs_revision=true WHERE id=%s', (t['id'],))
    return {'queued': topics_mod.queue_revisions(col.id, u['username'])}


# ------------------------------------------------------------------ health
@router.get('/collections/{ref}/health')
def get_health(ref: str, sc=Depends(scope_of)):
    col = S.open_collection(sc, ref)
    with db.conn() as c:
        r = c.execute('SELECT at, by, findings FROM library.health_run WHERE collection_id=%s ORDER BY id DESC LIMIT 1', (col.id,)).fetchone()
    return {'collection': {'name': col.name, 'level': col.level}, 'run': r}


@router.post('/collections/{ref}/health')
def run_health(ref: str, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col = S.open_collection(sc, ref, 'write')
    return {'findings': health_mod.run(col.id, u['username'])}


@router.get('/collections/{ref}/graph')
def topic_graph(ref: str, model: bool = False, sc=Depends(scope_of), u=Depends(auth.current_user)):
    """The topics of a collection and the links between them (never across collections)."""
    col = S.open_collection(sc, ref)
    with db.conn() as c:
        nodes = c.execute("SELECT slug AS key, title AS name, topic_type AS domain FROM library.topic WHERE collection_id=%s AND status='active' ORDER BY title LIMIT 400", (col.id,)).fetchall()
        edges = c.execute("SELECT f.slug AS \"from\", t.slug AS \"to\", l.relation FROM library.topic_link l JOIN library.topic f ON f.id=l.from_topic AND f.collection_id=l.collection_id JOIN library.topic t ON t.id=l.to_topic AND t.collection_id=l.collection_id "
                          "WHERE l.collection_id=%s AND f.status='active' AND t.status='active'", (col.id,)).fetchall()
    keys = {n['key'] for n in nodes}
    edges = [e for e in edges if e['from'] in keys and e['to'] in keys]
    if model:                                                       # the entities these topics are about, drawn in the same graph (their names are model structure)
        with db.conn() as c:
            refs = c.execute("SELECT t.slug, r.ref, r.relation FROM library.topic_model_ref r JOIN library.topic t ON t.id=r.topic_id AND t.collection_id=r.collection_id WHERE r.collection_id=%s AND r.kind='entity' AND r.status='confirmed' AND t.status='active'", (col.id,)).fetchall()
        onto = bridge._onto()
        known = {r['ref'] for r in refs if onto and r['ref'] in onto['entities']}
        nodes += [{'key': 'entity:' + k, 'name': onto['entities'][k]['name'], 'domain': 'Data model', 'kind': 'entity', 'entity': k} for k in sorted(known)]
        edges += [{'from': r['slug'], 'to': 'entity:' + r['ref'], 'status': 'confirmed', 'relation': r['relation']} for r in refs if r['ref'] in known and r['slug'] in keys]
    return {'collection': {'name': col.name, 'usage': col.usage}, 'nodes': nodes, 'edges': edges}


# ------------------------------------------------------------------ the bridge to the model
class LinkBody(BaseModel):
    kind: str = 'entity'
    ref: str
    relation: str = 'about'


class LinkDecision(BaseModel):
    kind: str = 'entity'
    ref: str
    accept: bool = True


def _topic_for_write(ref, slug, sc):
    col = S.open_collection(sc, ref, 'write')
    t = topics_mod.find(col.id, slug, follow=False)
    if not t:
        raise HTTPException(404, 'no such topic')
    return col, t


@router.put('/collections/{ref}/topics/{slug}/model-links')
def put_model_link(ref: str, slug: str, body: LinkBody, u=Depends(auth.current_user), sc=Depends(scope_of)):
    """A person says this topic is about a model entity (or field). The link is kept in the collection; the model is not changed."""
    col, t = _topic_for_write(ref, slug, sc)
    if body.kind not in ('entity', 'field'):
        raise HTTPException(422, 'kind must be entity or field')
    try:
        ref_ = bridge.set_link(col.id, t['id'], body.kind, body.ref, body.relation, u['username'])
    except ValueError as e:
        raise HTTPException(422, str(e))
    _audit(u['username'], 'link_model', col.id, after={'topic': t['slug'], 'kind': body.kind, 'ref': ref_})
    return {'ok': True, 'ref': ref_}


@router.post('/collections/{ref}/topics/{slug}/model-links/decide')
def decide_model_link(ref: str, slug: str, body: LinkDecision, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col, t = _topic_for_write(ref, slug, sc)
    try:
        bridge.decide(col.id, t['id'], body.kind, body.ref, body.accept, u['username'])
    except LookupError:
        raise HTTPException(404, 'no such link')
    return {'ok': True}


@router.delete('/collections/{ref}/topics/{slug}/model-links')
def delete_model_link(ref: str, slug: str, kind: str, link: str, sc=Depends(scope_of)):
    col, t = _topic_for_write(ref, slug, sc)
    bridge.remove(col.id, t['id'], kind, link)
    return {'ok': True}


@router.post('/collections/{ref}/topics/{slug}/model-links/suggest')
def suggest_model_links(ref: str, slug: str, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col, t = _topic_for_write(ref, slug, sc)
    _ingest_limit(u)
    if not col.allow_ai or not llm.available():
        raise HTTPException(422, 'AI is not available for this collection')
    topics_mod.queue_model_links(col.id, [str(t['id'])], u['username'])
    return {'queued': True}


# ------------------------------------------------------------------ interviews
class SeedBody(BaseModel):
    seed: str


class MessageBody(BaseModel):
    content: str


class ClaimBody(BaseModel):
    text: str
    category: str | None = None


class DraftBody(BaseModel):
    markdown: str


def _interview(col, iid, u):
    """An interview of this collection that this person may use: their own, or any if they manage the collection."""
    with db.conn() as c:
        it = c.execute('SELECT * FROM library.interview WHERE id::text=%s AND collection_id=%s', (iid, col.id)).fetchone()
    if not it or (it['created_by'] != u['username'] and col.level != 'manage'):
        raise HTTPException(404, 'no such interview')
    return it


def _interview_access(ref, sc):
    col = S.open_collection(sc, ref, 'write')
    if not col.allow_ai:
        raise HTTPException(422, 'this collection does not allow AI to read its documents: an interview needs a model (collection settings)')
    if not llm.available():
        raise HTTPException(422, 'no AI endpoint is set (Settings, Alfred and AI)')
    return col


@router.get('/collections/{ref}/interviews')
def list_interviews(ref: str, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col = S.open_collection(sc, ref, 'write')
    with db.conn() as c:
        rows = c.execute("SELECT i.id, i.seed, i.status, i.created_by, i.created_at, i.source_id, (SELECT count(*) FROM library.interview_claim x WHERE x.interview_id=i.id AND x.status='active') AS claims, "
                         "(SELECT count(*) FROM library.interview_turn t WHERE t.interview_id=i.id) AS turns FROM library.interview i WHERE i.collection_id=%s AND (i.created_by=%s OR %s) ORDER BY i.created_at DESC LIMIT 100",
                         (col.id, u['username'], col.level == 'manage')).fetchall()
    return {'collection': {'name': col.name, 'allow_ai': col.allow_ai}, 'interviews': [{**r, 'id': str(r['id']), 'source_id': str(r['source_id']) if r['source_id'] else None} for r in rows]}


@router.post('/collections/{ref}/interviews')
def start_interview(ref: str, body: SeedBody, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col = _interview_access(ref, sc)
    if not body.seed.strip():
        raise HTTPException(422, 'say what you want to document')
    with db.conn() as c:
        r = c.execute('INSERT INTO library.interview (collection_id,seed,created_by) VALUES (%s,%s,%s) RETURNING id', (col.id, body.seed.strip()[:4000], u['username'])).fetchone()
    _audit(u['username'], 'start_interview', col.id, after={'interview': str(r['id'])})
    return {'id': str(r['id'])}


@router.get('/collections/{ref}/interviews/{iid}')
def get_interview(ref: str, iid: str, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col = S.open_collection(sc, ref, 'write')
    it = _interview(col, iid, u)
    with db.conn() as c:
        turns = c.execute('SELECT id, role, content, created_at FROM library.interview_turn WHERE interview_id=%s AND collection_id=%s ORDER BY id', (it['id'], col.id)).fetchall()
        claims = c.execute("SELECT id, text, category FROM library.interview_claim WHERE interview_id=%s AND collection_id=%s AND status='active' ORDER BY id", (it['id'], col.id)).fetchall()
        draft = c.execute('SELECT version, markdown, origin, created_at FROM library.interview_draft WHERE interview_id=%s AND collection_id=%s ORDER BY version DESC LIMIT 1', (it['id'], col.id)).fetchone()
        pending = c.execute("SELECT kind FROM library.job WHERE collection_id=%s AND kind IN ('interview.extract','interview.draft') AND payload->>'interview_id'=%s AND status IN ('queued','running')", (col.id, str(it['id']))).fetchall()
    return {'interview': {'id': str(it['id']), 'seed': it['seed'], 'status': it['status'], 'source_id': str(it['source_id']) if it['source_id'] else None, 'created_by': it['created_by']},
            'collection': {'name': col.name, 'usage': col.usage}, 'turns': turns, 'claims': claims, 'draft': draft,
            'working_on': sorted({p['kind'].split('.')[1] for p in pending})}


@router.post('/collections/{ref}/interviews/{iid}/turns')
def interview_turn(ref: str, iid: str, body: MessageBody, u=Depends(auth.current_user), sc=Depends(scope_of)):
    """The person's message; the answer streams back as text pieces (server-sent events)."""
    col = _interview_access(ref, sc)
    it = _interview(col, iid, u)
    if it['status'] in ('ingesting', 'ingested'):
        raise HTTPException(409, 'this interview has been added to the collection')
    if not body.content.strip():
        raise HTTPException(422, 'the message is empty')
    _ingest_limit(u)
    from .. import activity
    activity.session('library', u['username'], hashlib.sha1(str(it['id']).encode()).hexdigest()[:16], title=f"interview {str(it['id'])[:8]}")

    def events():
        try:
            for piece in interview_mod.reply_stream(col.id, it['id'], body.content.strip()[:20000]):
                yield f'event: content\ndata: {json.dumps({"delta": piece})}\n\n'
            yield 'event: done\ndata: {}\n\n'
        except Exception as e:
            yield f'event: error\ndata: {json.dumps({"message": f"The interviewer could not answer ({type(e).__name__}). Try again."})}\n\n'
    return StreamingResponse(events(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@router.put('/collections/{ref}/interviews/{iid}/claims/{claim_id}')
def edit_claim(ref: str, iid: str, claim_id: int, body: ClaimBody, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col = S.open_collection(sc, ref, 'write')
    it = _interview(col, iid, u)
    if it['status'] in ('ingesting', 'ingested') or not body.text.strip():
        raise HTTPException(409 if it['status'] in ('ingesting', 'ingested') else 422, 'this fact cannot be changed')
    with db.conn() as c:
        r = c.execute("UPDATE library.interview_claim SET text=%s, category=coalesce(%s, category) WHERE id=%s AND interview_id=%s AND collection_id=%s AND status='active' RETURNING id",
                      (body.text.strip()[:1000], body.category, claim_id, it['id'], col.id)).fetchone()
    if not r:
        raise HTTPException(404, 'no such fact')
    return {'ok': True}


@router.delete('/collections/{ref}/interviews/{iid}/claims/{claim_id}')
def remove_claim(ref: str, iid: str, claim_id: int, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col = S.open_collection(sc, ref, 'write')
    it = _interview(col, iid, u)
    if it['status'] in ('ingesting', 'ingested'):
        raise HTTPException(409, 'this interview has been added to the collection')
    with db.conn() as c:
        r = c.execute("UPDATE library.interview_claim SET status='removed' WHERE id=%s AND interview_id=%s AND collection_id=%s AND status='active' RETURNING id", (claim_id, it['id'], col.id)).fetchone()
    if not r:
        raise HTTPException(404, 'no such fact')
    return {'ok': True}


@router.post('/collections/{ref}/interviews/{iid}/draft')
def make_draft(ref: str, iid: str, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col = _interview_access(ref, sc)
    _ingest_limit(u)
    it = _interview(col, iid, u)
    if it['status'] in ('ingesting', 'ingested'):
        raise HTTPException(409, 'this interview has been added to the collection')
    with db.conn() as c:
        if not c.execute("SELECT 1 FROM library.interview_claim WHERE interview_id=%s AND collection_id=%s AND status='active' LIMIT 1", (it['id'], col.id)).fetchone():
            raise HTTPException(422, 'there are no facts to write from yet: carry on with the interview first')
    jobs.enqueue('interview.draft', col.id, None, {'interview_id': str(it['id'])}, u['username'])
    return {'queued': True}


@router.put('/collections/{ref}/interviews/{iid}/draft')
def edit_draft(ref: str, iid: str, body: DraftBody, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col = S.open_collection(sc, ref, 'write')
    it = _interview(col, iid, u)
    if it['status'] in ('ingesting', 'ingested') or not body.markdown.strip():
        raise HTTPException(409 if it['status'] in ('ingesting', 'ingested') else 422, 'the draft cannot be changed')
    return {'version': interview_mod.save_draft(col.id, it['id'], body.markdown.strip(), 'edited')}


@router.post('/collections/{ref}/interviews/{iid}/finalize')
def finalize_interview(ref: str, iid: str, u=Depends(auth.current_user), sc=Depends(scope_of)):
    """The draft joins the collection as a source: it is read, indexed and turned into topics like any document."""
    col = S.open_collection(sc, ref, 'write')
    it = _interview(col, iid, u)
    _ingest_limit(u)
    try:
        sid = interview_mod.finalize(col.id, it['id'], u['username'])
    except LookupError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    _audit(u['username'], 'finalize_interview', col.id, after={'interview': str(it['id']), 'source': str(sid)})
    return {'source_id': str(sid)}


@router.delete('/collections/{ref}/interviews/{iid}')
def delete_interview(ref: str, iid: str, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col = S.open_collection(sc, ref, 'write')
    it = _interview(col, iid, u)
    with db.conn() as c:
        c.execute('DELETE FROM library.interview WHERE id=%s AND collection_id=%s', (it['id'], col.id))
    return {'ok': True}


# ------------------------------------------------------------------ the vocabulary of a collection
class VocabItem(BaseModel):
    template: str | None = None
    name: str
    description: str = ''
    inverse_name: str = ''


class VocabPatch(BaseModel):
    template: str | None = None
    description: str | None = None
    inverse_name: str | None = None


def _vocab_kind(kind):
    if kind not in ('topic_type', 'relation_type'):
        raise HTTPException(404, 'kind must be topic_type or relation_type')
    return kind


@router.post('/collections/{ref}/vocabulary/{kind}')
def add_vocab(ref: str, kind: str, body: VocabItem, u=Depends(auth.current_user), sc=Depends(scope_of)):
    """The manager adds a kind of topic (or of relation) to this collection's vocabulary."""
    col = S.open_collection(sc, ref, 'manage')
    kind = _vocab_kind(kind)
    name = body.name.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _/&-]{0,59}", name):
        raise HTTPException(422, 'a name is letters, numbers, spaces and _ - / &, up to 60 characters')
    with db.conn() as c:
        if c.execute(f'SELECT 1 FROM library.{kind} WHERE collection_id=%s AND lower(name)=lower(%s)', (col.id, name)).fetchone():
            raise HTTPException(409, 'this collection already has that one')
        if kind == 'topic_type':
            c.execute("INSERT INTO library.topic_type (collection_id,name,description,template) VALUES (%s,%s,%s,%s)", (col.id, name, body.description.strip()[:2000], (body.template or '').strip()[:6000]))
        else:
            c.execute("INSERT INTO library.relation_type (collection_id,name,description,inverse_name) VALUES (%s,%s,%s,%s)", (col.id, name, body.description.strip()[:2000], body.inverse_name.strip()[:60]))
    _audit(u['username'], 'vocab_add', col.id, after={'kind': kind, 'name': name})
    return {'ok': True}


@router.patch('/collections/{ref}/vocabulary/{kind}')
def edit_vocab(ref: str, kind: str, name: str, body: VocabPatch, u=Depends(auth.current_user), sc=Depends(scope_of)):
    col = S.open_collection(sc, ref, 'manage')
    kind = _vocab_kind(kind)
    with db.conn() as c:
        if kind == 'topic_type':
            r = c.execute("UPDATE library.topic_type SET description=coalesce(%s, description), template=coalesce(%s, template) WHERE collection_id=%s AND name=%s RETURNING name",
                          (body.description[:2000] if body.description is not None else None, body.template[:6000] if body.template is not None else None, col.id, name)).fetchone()
        else:
            r = c.execute("UPDATE library.relation_type SET description=coalesce(%s, description), inverse_name=coalesce(%s, inverse_name) WHERE collection_id=%s AND name=%s RETURNING name", (body.description, body.inverse_name, col.id, name)).fetchone()
    if not r:
        raise HTTPException(404, 'no such kind')
    _audit(u['username'], 'vocab_edit', col.id, after={'kind': kind, 'name': name})
    return {'ok': True}


def _uses(c, col_id, kind, name):
    if kind == 'topic_type':
        return c.execute("SELECT count(*) AS n FROM library.topic WHERE collection_id=%s AND topic_type=%s", (col_id, name)).fetchone()['n']
    return c.execute("SELECT count(*) AS n FROM library.topic_link WHERE collection_id=%s AND relation=%s", (col_id, name)).fetchone()['n']


@router.delete('/collections/{ref}/vocabulary/{kind}')
def delete_vocab(ref: str, kind: str, name: str, u=Depends(auth.current_user), sc=Depends(scope_of)):
    """Only a kind nothing uses can be deleted; otherwise merge it into another."""
    col = S.open_collection(sc, ref, 'manage')
    kind = _vocab_kind(kind)
    if name in vocab.PROTECTED:
        raise HTTPException(422, f"'{name}' is the fallback kind and stays")
    with db.conn() as c:
        n = _uses(c, col.id, kind, name)
        if n:
            raise HTTPException(409, f'{n} {"topics are" if kind == "topic_type" else "links are"} of this kind: merge it into another kind first')
        r = c.execute(f'DELETE FROM library.{kind} WHERE collection_id=%s AND name=%s RETURNING name', (col.id, name)).fetchone()
    if not r:
        raise HTTPException(404, 'no such kind')
    _audit(u['username'], 'vocab_delete', col.id, before={'kind': kind, 'name': name})
    return {'ok': True}


class MergeKind(BaseModel):
    into: str


@router.post('/collections/{ref}/vocabulary/{kind}/merge')
def merge_vocab(ref: str, kind: str, name: str, body: MergeKind, u=Depends(auth.current_user), sc=Depends(scope_of)):
    """Everything of one kind becomes another kind, and the first kind is removed."""
    col = S.open_collection(sc, ref, 'manage')
    kind = _vocab_kind(kind)
    if name in vocab.PROTECTED or name == body.into:
        raise HTTPException(422, 'that kind cannot be merged away' if name in vocab.PROTECTED else 'choose another kind to merge into')
    with db.conn() as c:
        if not c.execute(f"SELECT 1 FROM library.{kind} WHERE collection_id=%s AND name=%s AND status='approved'", (col.id, body.into)).fetchone() or not c.execute(f'SELECT 1 FROM library.{kind} WHERE collection_id=%s AND name=%s', (col.id, name)).fetchone():
            raise HTTPException(404, 'no such kind to merge, or the target is not approved')
        if kind == 'topic_type':
            c.execute('UPDATE library.topic SET topic_type=%s WHERE collection_id=%s AND topic_type=%s', (body.into, col.id, name))
        else:
            c.execute('DELETE FROM library.topic_link a USING library.topic_link b WHERE a.collection_id=%s AND a.relation=%s AND b.collection_id=a.collection_id AND b.relation=%s AND b.from_topic=a.from_topic AND b.to_topic=a.to_topic', (col.id, name, body.into))
            c.execute('UPDATE library.topic_link SET relation=%s WHERE collection_id=%s AND relation=%s', (body.into, col.id, name))
        c.execute(f'DELETE FROM library.{kind} WHERE collection_id=%s AND name=%s', (col.id, name))
    _audit(u['username'], 'vocab_merge', col.id, after={'kind': kind, 'from': name, 'into': body.into})
    return {'ok': True}


# ------------------------------------------------------------------ find (the command palette)
@router.get('/find')
def find_everywhere(q: str, sc=Depends(scope_of)):
    """Collections, sources and topics, in the collections this person can read, for a few typed letters. Each result says where it lives."""
    q = q.strip()
    if len(q) < 2:
        return {'collections': [], 'sources': [], 'topics': []}
    like = '%' + q.lower().replace('%', '').replace('_', r'\_') + '%'
    ids = list(sc.grants)
    out = {'collections': [], 'sources': [], 'topics': []}
    with db.conn() as c:
        cols = c.execute('SELECT id, name, usage FROM library.collection WHERE id::text = ANY(%s) AND lower(name) LIKE %s ORDER BY lower(name) LIMIT 5', (ids, like)).fetchall()
        out['collections'] = [{'id': str(r['id']), 'name': r['name'], 'usage': r['usage']} for r in cols]
        out['sources'] = [{'id': str(r['id']), 'name': r['name'], 'collection': r['cname'], 'collection_id': str(r['cid']), 'usage': r['usage']} for r in c.execute(
            "SELECT s.id, s.name, c.name AS cname, c.id AS cid, c.usage FROM library.source s JOIN library.collection c ON c.id=s.collection_id WHERE s.collection_id::text = ANY(%s) AND (lower(s.name) LIKE %s OR lower(coalesce(s.title,'')) LIKE %s) ORDER BY s.created_at DESC LIMIT 6",
            (ids, like, like)).fetchall()]
        names = {str(r['id']): (r['name'], r['usage']) for r in c.execute('SELECT id, name, usage FROM library.collection WHERE id::text = ANY(%s)', (ids,)).fetchall()}
    seen = set()
    for cid, (cname, usage) in names.items():
        for t in topics_mod.search(cid, q, 4):
            if (cid, t['slug']) not in seen and len(out['topics']) < 10:
                seen.add((cid, t['slug']))
                out['topics'].append({'slug': t['slug'], 'title': t['title'], 'type': t['topic_type'], 'collection': cname, 'collection_id': cid, 'usage': usage})
    return out


@router.get('/collections/{ref}/export')
def export_collection(ref: str, u=Depends(auth.current_user), sc=Depends(scope_of)):
    """The whole collection as one zip, for its manager: originals, source pages, topics with their evidence, links, vocabulary and a manifest."""
    col = S.open_collection(sc, ref, 'manage')
    ok, wait = ratelimit.INGEST.allow('export:' + u['username'])
    if not ok:
        raise HTTPException(429, f'wait {wait} s before another export', headers={'Retry-After': str(wait)})
    try:
        fh, manifest = export_mod.build(col, u['username'])
    except ValueError as e:
        raise HTTPException(413, str(e))
    _audit(u['username'], 'export', col.id, after={'counts': manifest['counts'], 'missing_files': len(manifest['files_missing_from_storage'])})
    name = f"{storage.clean_name(col.slug)}-export-{manifest['exported_at'][:10]}.zip"
    return StreamingResponse(export_mod.stream(fh), media_type='application/zip', headers={'Content-Disposition': f'attachment; filename="{name}"', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
