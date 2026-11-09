import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import access, activity, alfred, auth, clientinfo, config, ratelimit, connectors, reverse, databuild, datastore, db, editing, embeddings, graph, indexer, ontology as O, search as S, tools, versions, settings
from . import mcp_server
from .mcp_server import mcp
from . import conformance, llm

FRONTEND = Path(os.environ.get('FRONTEND_DIR', Path(__file__).resolve().parents[2] / 'frontend' / 'dist'))


def bootstrap():
    db.init_schema()
    from .library import vocab as library_vocab
    library_vocab.seed_all()                   # collections made before topics existed get the starter vocabulary
    auth.ensure_admin()
    activity.purge_every()                     # now and every hour
    pub = O.published_id()
    if pub and embeddings.available() and indexer.missing(pub):         # a new embedding model, or a crashed publish: fill the gaps without delaying startup
        threading.Thread(target=lambda: print('embedded', indexer.backfill(pub), 'chunks in the background'), daemon=True).start()
    if embeddings.available():
        threading.Thread(target=lambda: embeddings.embed_query('warm up'), daemon=True).start()      # load the local model now, not on the first question
    if embeddings.available():
        def _fill():
            from .library import pipeline
            n = pipeline.backfill_embeddings()
            from .library import topics as library_topics
            n += library_topics.embed_missing(None, 2000)
            if n:
                print('embedded', n, 'library passages in the background', flush=True)
        threading.Thread(target=_fill, daemon=True).start()               # after a changed vector size or a missing model
    if config.SERVICE_ROLE == 'all':
        from .library import jobs as library_jobs
        library_jobs.start_workers(settings.get('LIBRARY_WORKERS'))        # documents are processed in the background by threads here, or by separate worker containers
    mcp_server.refresh_instructions()          # the agent rules travel with the MCP instructions: read them once a version is published


@asynccontextmanager
async def lifespan(app):
    await run_in_threadpool(bootstrap)
    async with mcp.session_manager.run():
        yield


app = FastAPI(title='Curltale AI', lifespan=lifespan)
from .library.api import router as library_router
app.include_router(library_router)
app.router.routes.extend(r for r in mcp.streamable_http_app().routes if getattr(r, 'path', '') == '/mcp')


@app.middleware('http')
async def mcp_auth(request: Request, call_next):
    if request.url.path.rstrip('/') == '/mcp':
        tok = request.headers.get('authorization', '')[7:]
        if not await run_in_threadpool(auth.api_token_ok, tok):
            return JSONResponse({'error': 'missing or invalid bearer token'}, status_code=401)
    resp = await call_next(request)
    if request.url.path.startswith('/api/'):
        resp.headers['Cache-Control'] = 'no-store'          # live data: a browser or proxy must never replay an old answer
    return resp


SECURITY_HEADERS = {
    'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), payment=()',
    # everything the UI loads comes from this origin; inline style attributes are React's; images only from here or data: URLs (the icon)
    'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; "
                               "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
}


@app.middleware('http')
async def security_headers(request: Request, call_next):
    """The app sets its own security headers: they hold with any proxy in front, or none."""
    resp = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    if clientinfo.is_https(request):
        resp.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    return resp


@app.middleware('http')
async def rate_limit(request: Request, call_next):
    """Added last, so it runs first: a client over its limit is answered before any work (or any authentication) happens."""
    ok, wait, what = ratelimit.check(request.url.path, request.method, clientinfo.client_ip(request))
    if not ok:
        return JSONResponse({'detail': f'{what}: try again in {wait} s'}, status_code=429, headers={'Retry-After': str(wait)})
    return await call_next(request)


@app.exception_handler(editing.EditError)
async def edit_error(request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=422)


def vid_for(version: str = 'published'):
    if version == 'published':
        v = O.published_id()
    elif version == 'draft':
        v = O.draft_id()
    else:
        v = int(version)
    if not v:
        raise HTTPException(404, f'no {version} version')
    return v


# ---------------------------------------------------------------- auth
class Login(BaseModel):
    username: str
    password: str


@app.post('/api/login')
def login(body: Login, request: Request, response: Response):
    ip = clientinfo.client_ip(request)
    try:
        r = auth.login(body.username, body.password, ip)
    except auth.LoginBlocked as e:
        raise HTTPException(429, str(e))
    if not r:
        raise HTTPException(401, 'wrong username or password')
    token, user = r
    response.set_cookie('studio_session', token, httponly=True, samesite='lax', secure=clientinfo.is_https(request),
                        max_age=settings.get('SESSION_HOURS') * 3600)
    return user


@app.post('/api/logout')
def logout(request: Request, response: Response):
    t = request.cookies.get('studio_session')
    if t:
        auth.logout(t)
    response.delete_cookie('studio_session')
    return {'ok': True}


@app.get('/api/me')
def me(u=Depends(auth.current_user)):
    return {**u, 'entitlements': access.normalize_user(u.get('entitlements'))}


class PasswordBody(BaseModel):
    current: str
    new: str


@app.post('/api/me/password')
def change_password(body: PasswordBody, request: Request, u=Depends(auth.current_user)):
    try:
        auth.change_password(u['username'], body.current, body.new, keep_token=request.cookies.get('studio_session'))
    except ValueError as e:
        raise HTTPException(422, str(e))
    with db.conn() as c:
        c.execute("INSERT INTO audit_log (username,action,kind,key) VALUES (%s,'password','user',%s)", (u['username'], u['username']))
    return {'ok': True}


# ---------------------------------------------------------------- reading
def library_review(u):
    if not settings.get('LIBRARY_ENABLED'):
        return 0
    from .library import api as lib_api, scope as lib_scope
    try:
        return lib_api.review_count(lib_scope.for_user(u))
    except Exception:
        return 0


@app.get('/api/status')
def status(u=Depends(auth.current_user)):
    pub, dr = O.published_id(), O.draft_id()
    return {'published': pub, 'draft': dr, 'four_eyes': versions.four_eyes(), 'draft_editors': versions.editors(dr) if dr else [], 'embeddings_configured': embeddings.available(), 'alfred_available': llm.available(), 'library': bool(settings.get('LIBRARY_ENABLED')), 'library_review': library_review(u), 'chat_model': settings.get('CHAT_MODEL'),
            'embedding_model': config.EMBEDDING_MODEL, 'missing_embeddings': indexer.missing(pub) if pub else None,
            'versions': versions.list_versions()}


@app.get('/api/entities')
def entities(version: str = 'published', u=Depends(auth.current_user)):
    vid = vid_for(version)
    with db.conn() as c:
        rows = c.execute("SELECT e.key,e.name,e.domain,e.kind,e.semantic_table,e.definition, jsonb_array_length(e.filters) AS filters, jsonb_array_length(e.checks) AS checks, "
                         "(SELECT count(*) FROM field f WHERE f.version_id=e.version_id AND f.entity_key=e.key AND f.role<>'calculated') AS attributes, "
                         "(SELECT count(*) FROM field f WHERE f.version_id=e.version_id AND f.entity_key=e.key AND f.role='calculated') AS calculated, "
                         "(SELECT count(*) FROM field f WHERE f.version_id=e.version_id AND f.entity_key=e.key AND f.review_status IN ('needs_review','source_needed')) AS pending "
                         "FROM entity e WHERE e.version_id=%s ORDER BY e.position", (vid,)).fetchall()
    domains = list(dict.fromkeys(r['domain'] or 'Other' for r in rows))
    return {'version': vid, 'domains': [{'name': d} for d in domains],
            'entities': [{'key': r['key'], 'name': r['name'], 'domain': r['domain'] or 'Other', 'kind': r['kind'], 'table': r['semantic_table'], 'definition': r['definition'] or '',
                          'attributes': r['attributes'], 'calculated': r['calculated'], 'filters': r['filters'], 'checks': r['checks'], 'pending': r['pending']} for r in rows]}


@app.get('/api/entities/{key}/documents')
def entity_documents(key: str, u=Depends(auth.current_user)):
    """Documents (topics of the collections this user can read) that are about this entity. The model does not store these links: they come from the collections."""
    if not settings.get('LIBRARY_ENABLED'):
        return {'documents': []}
    from .library import bridge, scope as lib_scope
    return {'documents': bridge.documents_for(lib_scope.for_user(u), 'entity', key)}


@app.get('/api/entities/{key}')
def entity(key: str, version: str = 'published', u=Depends(auth.current_user)):
    vid = vid_for(version)
    with db.conn() as c:
        r = c.execute('SELECT key FROM entity WHERE version_id=%s AND key=%s', (vid, key)).fetchone()
        if not r:   # tolerate a table name or a table with its schema, as people and LLMs write them in joins
            r = c.execute("SELECT key FROM entity WHERE version_id=%s AND (lower(name)=lower(%s) OR semantic_table ILIKE %s) ORDER BY position LIMIT 1",
                          (vid, key, '%.' + key.split('.')[-1])).fetchone()
        if not r:
            raise HTTPException(404, 'entity not found')
        key = r['key']
        e = editing._entity(c, vid, key)
        fields = c.execute('SELECT * FROM field WHERE version_id=%s AND entity_key=%s ORDER BY position', (vid, key)).fetchall()
        rels = [{'id': x['rid'], 'from': x['from_entity'], 'to': x['to_entity'], 'predicate': x['predicate'], 'cardinality': x['cardinality'], 'join': x['join_cond'],
                 'status': x['status'], **(x['extra'] or {})} for x in c.execute(
                     'SELECT * FROM relationship WHERE version_id=%s AND (from_entity=%s OR to_entity=%s) ORDER BY position', (vid, e['name'], e['name'])).fetchall()]
        kb = c.execute("SELECT * FROM example WHERE version_id=%s AND applies_to ? %s ORDER BY position", (vid, key)).fetchall()
    return {'version': vid, 'id': key, **e,
            'attributes': [{'column': f['col'], **O.field_dict(f)} for f in fields if f['role'] != 'calculated'],
            'calculated': [{'code': f['col'], **O.field_dict(f)} for f in fields if f['role'] == 'calculated'],
            'relationships': rels,
            'examples': [{'id': k['id'], 'title': k['title'], 'body': k['body'], **(k['extra'] or {})} for k in kb]}


@app.get('/api/graph')
def graph_view(version: str = 'published', documents: bool = False, u=Depends(auth.current_user)):
    g = graph.graph(vid_for(version))
    if documents and settings.get('LIBRARY_ENABLED'):                      # topics of the collections this user can read, drawn over the model, joined to the entities they are about
        from .library import bridge, scope as lib_scope
        extra = bridge.graph_overlay(lib_scope.for_user(u), {n['key'] for n in g['nodes']})
        g = {'nodes': g['nodes'] + extra['nodes'], 'edges': g['edges'] + extra['edges']}
    return g


@app.get('/api/path')
def path(a: str, b: str, version: str = 'published', u=Depends(auth.current_user)):
    return graph.find_path(vid_for(version), a, b)


@app.get('/api/relationships')
def relationships(version: str = 'published', u=Depends(auth.current_user)):
    ents, rels = graph.load(vid_for(version))
    return rels


@app.get('/api/examples')
def examples(version: str = 'published', u=Depends(auth.current_user)):
    with db.conn() as c:
        rows = c.execute('SELECT * FROM example WHERE version_id=%s ORDER BY position', (vid_for(version),)).fetchall()
    return [{'id': r['id'], 'title': r['title'], 'body': r['body'], 'applies_to': r['applies_to'], **(r['extra'] or {})} for r in rows]


@app.get('/api/search')
def search(q: str, types: str | None = None, version: str = 'published', u=Depends(auth.current_user)):
    return S.search(vid_for(version), q, types=types.split(',') if types else None, k=20, use_vectors=True)


# ---------------------------------------------------------------- versions
@app.get('/api/versions/diff')
def diff(a: int, b: int, u=Depends(auth.current_user)):
    return versions.diff(a, b)


@app.get('/api/audit')
def audit(version: int | None = None, u=Depends(auth.current_user)):
    return versions.audit(version)


class DraftBody(BaseModel):
    label: str
    note: str | None = None


@app.post('/api/versions/draft')
def new_draft(body: DraftBody, u=Depends(auth.modeller_user)):
    return {'id': versions.create_draft(body.label, body.note)}


@app.delete('/api/versions/draft')
def drop_draft(u=Depends(auth.publisher_user)):
    return {'id': versions.discard_draft()}


@app.get('/api/versions/validate')
def validate_draft(u=Depends(auth.modeller_user)):
    vid = vid_for('draft')
    return {'errors': editing.validate(vid), 'warnings': editing.sql_warnings(O.assemble(vid))}


@app.post('/api/versions/publish')
async def publish(u=Depends(auth.publisher_user)):
    """Starts publishing in the background and returns at once; poll /api/versions/publish/status."""
    return await run_in_threadpool(versions.publish_start, u['username'])


@app.get('/api/versions/publish/status')
def publish_status(u=Depends(auth.current_user)):
    return versions.publish_job()


# ---------------------------------------------------------------- editing (draft only)
class Data(BaseModel):
    data: dict


@app.post('/api/entities/{key}')
def create_entity(key: str, body: Data, u=Depends(auth.modeller_user)):
    return editing.upsert_entity(u['username'], key, body.data, create=True)


@app.patch('/api/entities/{key}')
def patch_entity(key: str, body: Data, u=Depends(auth.modeller_user)):
    return editing.upsert_entity(u['username'], key, body.data)


@app.delete('/api/entities/{key}')
def remove_entity(key: str, u=Depends(auth.modeller_user)):
    editing.delete_entity(u['username'], key)
    return {'ok': True}


@app.put('/api/entities/{key}/fields/{col}')
def put_field(key: str, col: str, body: Data, u=Depends(auth.modeller_user)):
    return editing.upsert_field(u['username'], key, col, body.data)


@app.delete('/api/entities/{key}/fields/{col}')
def remove_field(key: str, col: str, u=Depends(auth.modeller_user)):
    editing.delete_field(u['username'], key, col)
    return {'ok': True}


@app.get('/api/relationships/next-id')
def next_rid(u=Depends(auth.current_user)):
    return {'id': editing.next_relationship_id()}


@app.put('/api/relationships/{rid}')
def put_relationship(rid: str, body: Data, u=Depends(auth.modeller_user)):
    return editing.upsert_relationship(u['username'], rid, body.data)


@app.delete('/api/relationships/{rid}')
def remove_relationship(rid: str, u=Depends(auth.modeller_user)):
    editing.delete_relationship(u['username'], rid)
    return {'ok': True}


@app.put('/api/examples/{kid}')
def put_example(kid: str, body: Data, u=Depends(auth.modeller_user)):
    return editing.upsert_example(u['username'], kid, body.data)


@app.delete('/api/examples/{kid}')
def remove_example(kid: str, u=Depends(auth.modeller_user)):
    editing.delete_example(u['username'], kid)
    return {'ok': True}


# ---------------------------------------------------------------- synthetic data (browse, console, checks)
def _ent(u):
    return access.normalize_user(u.get('entitlements'))


def _cat():
    row = datastore.active_connector()
    if row:                                                        # a connector is in use: the published model describes it, there is no build record
        return datastore.current_catalog(), {'id': 0, 'external': row['name'], 'version_id': O.published_id(), 'findings': []}
    cat, build = databuild.catalog()
    if not cat:
        raise HTTPException(404, 'no synthetic database yet: an admin can build one on the Data → Database page')
    return cat, build


@app.get('/api/data/status')
def data_status(u=Depends(auth.current_user)):
    row = datastore.active_connector()
    if row:
        o = {**connectors.DEFAULT_OPTIONS, **(row['options'] or {})}
        return {'exists': True, 'build': None, 'latest': None, 'progress': {'running': False, 'message': ''}, 'published_version': O.published_id(), 'stale': False,
                'restricted_visible': _ent(u)['restricted'],
                'connector': {'id': row['id'], 'name': row['name'], 'kind': row['kind'], 'row_limit': o['row_limit'], 'agents_may_read_rows': o['agents_may_read_rows'], 'coverage': connectors.coverage(row),
                              'target': row['params']['path'] if row['params'].get('path') else f"{row['params']['host']}:{row['params']['port']}" + (f"/{row['params']['database']}" if row['params'].get('database') else '')}}
    b = databuild.last_ok()
    cur = databuild.latest()
    pub = O.published_id()
    from . import llm
    n_ai = len(O.assemble(pub)['entities']) if pub else 0
    return {'exists': bool(b), 'build': b, 'latest': cur, 'progress': databuild.progress(), 'published_version': pub, 'ai_available': llm.available(), 'ai_calls': n_ai,
            'stale': bool(b and b['version_id'] != pub), 'restricted_visible': _ent(u)['restricted']}


class BuildBody(BaseModel):
    scale: str = 'medium'
    seed: int = 7
    ai: bool = False


@app.post('/api/admin/data/build')
def data_build(body: BuildBody, u=Depends(auth.admin_user)):
    if datastore.active_connector():
        raise HTTPException(409, 'a connector is in use: switch back to the built-in database (Connectors page) before rebuilding it')
    if body.scale not in ('small', 'medium', 'large'):
        raise HTTPException(422, 'scale must be small, medium or large')
    try:
        return {'id': databuild.start(body.scale, body.seed, u['username'], body.ai)}
    except RuntimeError as e:
        raise HTTPException(409, str(e))


def _data_guard(fn):
    try:
        return fn()
    except datastore.DataError as e:
        raise HTTPException(422, str(e))


@app.get('/api/data/tables')
def data_tables(u=Depends(auth.current_user)):
    cat, build = _cat()
    return {'build': build, 'tables': _data_guard(lambda: datastore.overview(cat))}


@app.get('/api/data/table')
def data_table(name: str, page: int = 1, size: int = 50, sort: str | None = None, desc: bool = False, filters: str | None = None, u=Depends(auth.current_user)):
    cat, _ = _cat()
    fl = json.loads(filters) if filters else []
    return _data_guard(lambda: datastore.page(cat, name, _ent(u), page, size, sort, desc, fl))


@app.get('/api/data/row')
def data_row(name: str, pk: str, u=Depends(auth.current_user)):
    cat, _ = _cat()
    return _data_guard(lambda: datastore.row_detail(cat, name, pk, _ent(u)))


class SqlBody(BaseModel):
    sql: str


@app.post('/api/data/sql')
def data_sql(body: SqlBody, u=Depends(auth.current_user)):
    _cat()
    return _data_guard(lambda: datastore.console(body.sql, _ent(u)))


@app.get('/api/data/conformance')
def data_conformance(refresh: bool = False, u=Depends(auth.current_user)):
    if refresh and auth.RANK.get(u['role'], 0) < auth.RANK['modeller']:
        raise HTTPException(403, 'running the Model check again needs the modeller role')
    cat, build = _cat()
    if build.get('external'):                                      # nothing is stored for a connector: the check runs on the real database every time
        return {'build': 0, 'external': build['external'], **conformance.run_all(cat, [])}
    if refresh:
        res = conformance.run_all(cat, build['findings'])
        with db.conn() as c:
            c.execute('UPDATE data_build SET checks=%s WHERE id=%s', (db.J(res), build['id']))
        return {'build': build['id'], **res}
    with db.conn() as c:
        res = c.execute('SELECT checks FROM data_build WHERE id=%s', (build['id'],)).fetchone()['checks']
    return {'build': build['id'], **res}


# ---------------------------------------------------------------- Alfred
class AlfredBody(BaseModel):
    messages: list[dict]
    page: dict | None = None
    conversation: str | None = None


def library_scope_for(u):
    from .library import scope as lib_scope
    return lib_scope.for_user(u) if settings.get('LIBRARY_ENABLED') else None


@app.post('/api/alfred')
async def alfred_chat(body: AlfredBody, u=Depends(auth.current_user)):
    ok, wait = ratelimit.ALFRED.allow(u['username'])
    if not ok:
        raise HTTPException(429, f'Alfred is limited to {settings.get('ALFRED_PER_MIN')} questions a minute per user: try again in {wait} s', headers={'Retry-After': str(wait)})
    lib_scope = await run_in_threadpool(library_scope_for, u)

    async def stream():
        async for ev in alfred.chat(body.messages, body.page, u['username'], access.normalize_user(u.get('entitlements')), body.conversation, lib_scope):
            yield f'data: {json.dumps(ev, ensure_ascii=False)}\n\n'
    return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ---------------------------------------------------------------- admin
@app.post('/api/admin/reindex')
async def reindex(u=Depends(auth.admin_user)):
    vid = O.published_id()
    return await run_in_threadpool(indexer.reindex, vid)


def _log_grant(user, what, ent):
    with db.conn() as c:
        c.execute("INSERT INTO audit_log (username,action,kind,key,after) VALUES (%s,'grant','entitlements',%s,%s)", (user, what, db.J(ent)))


@app.get('/api/admin/tokens')
def list_tokens(u=Depends(auth.admin_user)):
    with db.conn() as c:
        rows = c.execute('SELECT id,name,created_at,last_used,entitlements FROM api_token ORDER BY id').fetchall()
    return [{**r, 'entitlements': access.normalize(r['entitlements'])} for r in rows]


class TokenBody(BaseModel):
    name: str
    entitlements: dict | None = None


class EntBody(BaseModel):
    entitlements: dict


@app.post('/api/admin/tokens')
def new_token(body: TokenBody, u=Depends(auth.admin_user)):
    ent = access.normalize(body.entitlements)
    _log_grant(u['username'], f'token:{body.name}', ent)
    return {'token': auth.create_api_token(body.name, ent), 'entitlements': ent, 'note': 'shown once: store it now'}


@app.put('/api/admin/tokens/{tid}/entitlements')
def set_token_ent(tid: int, body: EntBody, u=Depends(auth.admin_user)):
    ent = access.normalize(body.entitlements)
    with db.conn() as c:
        r = c.execute('UPDATE api_token SET entitlements=%s WHERE id=%s RETURNING name', (db.J(ent), tid)).fetchone()
    if not r:
        raise HTTPException(404, 'no such token')
    _log_grant(u['username'], f"token:{r['name']}", ent)
    return ent


@app.delete('/api/admin/tokens/{tid}')
def del_token(tid: int, u=Depends(auth.admin_user)):
    with db.conn() as c:
        c.execute('DELETE FROM api_token WHERE id=%s', (tid,))
    return {'ok': True}


class UserBody(BaseModel):
    username: str
    password: str
    role: str = 'modeller'
    extra_roles: list[str] = []
    entitlements: dict | None = None


class RolesBody(BaseModel):
    role: str
    extra_roles: list[str] = []


def _check_roles(role, extra):
    if role not in auth.ROLES:
        raise HTTPException(422, f"role must be one of {', '.join(auth.ROLES)}")
    if any(x not in auth.EXTRA_ROLES for x in extra):
        raise HTTPException(422, f"extra roles can only be: {', '.join(auth.EXTRA_ROLES)}")
    return sorted(set(extra))


@app.get('/api/admin/users')
def list_users(u=Depends(auth.admin_user)):
    with db.conn() as c:
        rows = c.execute('SELECT username,role,extra_roles,created_at,entitlements,must_change,password_changed_at FROM app_user ORDER BY username').fetchall()
    return [{**r, 'roles': auth.roles_of(r), 'entitlements': access.normalize_user(r['entitlements'])} for r in rows]


@app.post('/api/admin/users')
def new_user(body: UserBody, u=Depends(auth.admin_user)):
    extra = _check_roles(body.role, body.extra_roles)
    problem = auth.password_problem(body.password, body.username)
    if problem:
        raise HTTPException(422, problem)
    must = body.username != u['username']                     # a password set by an admin is temporary: the user picks their own at the next login
    with db.conn() as c:
        c.execute('INSERT INTO app_user (username,password_hash,role,extra_roles,entitlements,must_change) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (username) DO UPDATE SET '
                  'password_hash=EXCLUDED.password_hash, role=EXCLUDED.role, extra_roles=EXCLUDED.extra_roles, entitlements=EXCLUDED.entitlements, must_change=EXCLUDED.must_change',
                  (body.username, auth.hash_password(body.password), body.role, extra, db.J(access.normalize(body.entitlements)), must))
        c.execute('DELETE FROM session WHERE username=%s', (body.username,)) if must else None
    _log_grant(u['username'], f'user:{body.username}', access.normalize_user(body.entitlements))
    return {'ok': True}


@app.put('/api/admin/users/{username}/roles')
def set_user_roles(username: str, body: RolesBody, u=Depends(auth.admin_user)):
    extra = _check_roles(body.role, body.extra_roles)
    with db.conn() as c:
        old = c.execute('SELECT role,extra_roles FROM app_user WHERE username=%s', (username,)).fetchone()
        if not old:
            raise HTTPException(404, 'no such user')
        if old['role'] == 'admin' and body.role != 'admin' and c.execute("SELECT count(*) AS n FROM app_user WHERE role='admin'").fetchone()['n'] < 2:
            raise HTTPException(422, 'this is the only admin: make someone else an admin first')
        c.execute('UPDATE app_user SET role=%s, extra_roles=%s WHERE username=%s', (body.role, extra, username))
        c.execute("INSERT INTO audit_log (username,action,kind,key,before,after) VALUES (%s,'grant','roles',%s,%s,%s)", (u['username'], f'user:{username}', db.J(dict(old)), db.J({'role': body.role, 'extra_roles': extra})))
    return {'ok': True, 'roles': auth.roles_of({'role': body.role, 'extra_roles': extra})}


@app.put('/api/admin/users/{username}/entitlements')
def set_user_ent(username: str, body: EntBody, u=Depends(auth.admin_user)):
    ent = access.normalize_user(body.entitlements)
    with db.conn() as c:
        r = c.execute('UPDATE app_user SET entitlements=%s WHERE username=%s RETURNING username', (db.J(ent), username)).fetchone()
    if not r:
        raise HTTPException(404, 'no such user')
    _log_grant(u['username'], f'user:{username}', ent)
    return ent


@app.get('/api/admin/questions')
def questions(u=Depends(auth.current_user)):
    with db.conn() as c:
        return c.execute('SELECT at,source,question FROM question_log WHERE answered=false ORDER BY id DESC LIMIT 100').fetchall()


@app.get('/api/template.xlsx')
def template_blank(u=Depends(auth.current_user)):
    from . import excel_io
    return Response(excel_io.build_workbook(), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': 'attachment; filename="ontology-template.xlsx"'})


@app.get('/api/export.xlsx')
def export_excel(version: str = 'published', u=Depends(auth.current_user)):
    from . import excel_io
    vid = vid_for(version)
    m = O.assemble(vid)
    ent = access.normalize_user(u.get('entitlements'))
    if not ent['restricted']:                                  # the workbook must not hand a restricted example value to someone who cannot see it
        for e in m['entities'].values():
            for a in e['attributes'].values():
                if a.get('agent_access') == 'none' or a.get('sensitivity') == 'restricted':
                    a.pop('example', None)
    return Response(excel_io.build_workbook(m), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename="ontology-v{vid}.xlsx"'})


@app.post('/api/admin/import/excel')
async def import_excel(request: Request, dry_run: bool = True, replace_draft: bool = False, label: str | None = None, u=Depends(auth.modeller_user)):
    data = await request.body()
    if not data:
        raise HTTPException(400, 'no file received')
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, 'file is larger than 20 MB')
    r = await run_in_threadpool(versions.import_excel, data, u['username'], label, replace_draft, dry_run)
    return r


class ConnectorBody(BaseModel):
    name: str | None = None
    kind: str = 'postgres'
    params: dict = {}
    password: str | None = None
    options: dict | None = None


def _conn_guard(fn):
    try:
        return fn()
    except connectors.ConnectorError as e:
        raise HTTPException(422, str(e))


@app.get('/api/admin/connectors')
def connectors_list(u=Depends(auth.admin_user)):
    return {'connectors': connectors.list_all(), 'builtin_active': connectors.active() is None, 'kinds': connectors.kinds()}


@app.post('/api/admin/connectors')
def connectors_create(body: ConnectorBody, u=Depends(auth.admin_user)):
    return _conn_guard(lambda: connectors.create(body.name, body.params, body.password, body.options, body.kind))


@app.put('/api/admin/connectors/{cid}')
def connectors_update(cid: int, body: ConnectorBody, u=Depends(auth.admin_user)):
    r = _conn_guard(lambda: connectors.update(cid, body.name, body.params, body.password, body.options))
    datastore.reset_pools()
    return r


@app.delete('/api/admin/connectors/{cid}')
def connectors_delete(cid: int, u=Depends(auth.admin_user)):
    _conn_guard(lambda: connectors.delete(cid))
    datastore.reset_pools()
    return {'ok': True}


@app.post('/api/admin/connectors/{cid}/test')
async def connectors_test(cid: int, u=Depends(auth.admin_user)):
    return await run_in_threadpool(lambda: _conn_guard(lambda: connectors.test(cid)))


@app.post('/api/admin/connectors/{cid}/activate')
async def connectors_activate(cid: int, force: bool = False, u=Depends(auth.admin_user)):
    def run():
        t = _conn_guard(lambda: connectors.test(cid))
        if not t['ok']:
            raise HTTPException(422, f"cannot activate: the connection fails ({t.get('error')})")
        try:
            _conn_guard(lambda: connectors.introspect(cid))                    # read the structure now: the check below must see the database as it is
        except HTTPException:
            pass
        cv = connectors.coverage(connectors.get(cid))
        if cv and cv['percent'] < 100 and not force:
            return {'ok': False, 'needs_confirmation': True, 'coverage': cv}
        with db.conn() as c:
            c.execute('UPDATE connector SET active=false WHERE active')
            c.execute('UPDATE connector SET active=true WHERE id=%s', (cid,))
            c.execute("INSERT INTO audit_log (username,action,kind,key) VALUES (%s,'activate','connector',%s)", (u['username'], str(cid)))
        datastore.reset_pools()
        return {'ok': True, 'warnings': t.get('warnings', [])}
    return await run_in_threadpool(run)


@app.post('/api/admin/connectors/deactivate')
def connectors_deactivate(u=Depends(auth.admin_user)):
    with db.conn() as c:
        c.execute('UPDATE connector SET active=false WHERE active')
        c.execute("INSERT INTO audit_log (username,action,kind,key) VALUES (%s,'deactivate','connector','builtin')", (u['username'],))
    datastore.reset_pools()
    return {'ok': True}


class IntrospectBody(BaseModel):
    schemas: list[str] | None = None


@app.post('/api/admin/connectors/{cid}/introspect')
async def connectors_introspect(cid: int, body: IntrospectBody, u=Depends(auth.admin_user)):
    cat = await run_in_threadpool(lambda: _conn_guard(lambda: connectors.introspect(cid, body.schemas)))
    return _structure_summary(cat)


def _structure_summary(cat):
    return {'schemas': cat['schemas'], 'at': cat['at'], 'tables': [{'table': f"{t['schema']}.{t['name']}", 'kind': t['kind'], 'columns': len(t['columns']), 'rows': t['rows'], 'pk': t['pk'], 'fks': len(t['fks'])} for t in cat['tables']]}


@app.get('/api/admin/connectors/{cid}/structure')
def connectors_structure(cid: int, u=Depends(auth.admin_user)):
    row = _conn_guard(lambda: connectors.get(cid))
    if not row['catalog']:
        raise HTTPException(404, 'structure not read yet')
    return _structure_summary(row['catalog'])


class DraftFromDbBody(BaseModel):
    tables: list[str] | None = None
    replace_draft: bool = False
    label: str | None = None


@app.post('/api/admin/connectors/{cid}/generate-draft')
async def connectors_generate(cid: int, body: DraftFromDbBody, u=Depends(auth.admin_user)):
    def run():
        row = _conn_guard(lambda: connectors.get(cid))
        if not row['catalog']:
            raise HTTPException(422, 'read the database structure first (Read structure)')
        model, notes = reverse.generate(row['catalog'], row['name'], body.tables)
        if not model['entities']:
            raise HTTPException(422, 'no tables selected')
        problems = editing.validate_model(model)
        vid = versions.create_draft_from_model(model, u['username'], body.label or f"From {row['name']}", body.replace_draft, note=f"generated from connector {row['name']}", source='connector')
        return {'draft': vid, 'entities': len(model['entities']), 'relationships': len(model['relationships']), 'notes': notes, 'problems': problems}
    return await run_in_threadpool(run)


class ResetBody(BaseModel):
    confirm: str
    remove_demo: bool = True


@app.post('/api/admin/reset-ontology')
async def reset_ontology(body: ResetBody, u=Depends(auth.admin_user)):
    if body.confirm != 'EMPTY':
        raise HTTPException(422, 'type EMPTY to confirm')
    return await run_in_threadpool(versions.reset_all, u['username'], body.remove_demo)


@app.get('/api/admin/backups')
def backups_list(u=Depends(auth.admin_user)):
    return versions.backups()


@app.get('/api/admin/backups/{name}')
def backups_get(name: str, u=Depends(auth.admin_user)):
    import os
    path = os.path.join(versions.BACKUP_DIR, os.path.basename(name))
    if os.path.basename(name) != name or not name.endswith('.xlsx') or not os.path.isfile(path):
        raise HTTPException(404, 'no such backup')
    return FileResponse(path, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', filename=name)


class SettingBody(BaseModel):
    name: str
    value: bool | int | float | str


@app.get('/api/admin/settings')
def settings_get(u=Depends(auth.admin_user)):
    return settings.listing()


@app.put('/api/admin/settings')
def settings_put(body: SettingBody, u=Depends(auth.admin_user)):
    try:
        settings.put(body.name, body.value, u['username'])
    except settings.SettingError as e:
        raise HTTPException(400, str(e))
    return settings.listing()


@app.post('/api/admin/storage/test')
async def storage_test(u=Depends(auth.admin_user)):
    """Try the saved storage settings: write a probe (local) or reach the bucket (S3)."""
    from .library import storage
    return await run_in_threadpool(storage.check)


@app.post('/api/admin/llm/test')
async def llm_test(u=Depends(auth.admin_user)):
    """Try the AI endpoint with the saved settings: one tiny request."""
    return await run_in_threadpool(llm.test)


@app.delete('/api/admin/settings/{name}')
def settings_reset(name: str, u=Depends(auth.admin_user)):
    try:
        settings.reset(name, u['username'])
    except settings.SettingError as e:
        raise HTTPException(400, str(e))
    return settings.listing()


@app.get('/api/admin/audit')
def admin_audit(kind: str | None = None, limit: int = 200, u=Depends(auth.admin_user)):
    """The governance trail: who changed what (collections, access, roles, settings, ...). Source names are never recorded in it."""
    with db.conn() as c:
        return c.execute("SELECT a.id,a.at,a.username,a.action,a.kind,a.key,a.before,a.after, col.name AS collection FROM audit_log a "
                         "LEFT JOIN library.collection col ON a.kind='collection' AND col.id::text=a.key WHERE a.version_id IS NULL AND (%s::text IS NULL OR a.kind=%s) ORDER BY a.id DESC LIMIT %s", (kind, kind, min(limit, 500))).fetchall()


@app.get('/api/admin/activity/summary')
def activity_summary(hours: int = 168, u=Depends(auth.admin_user)):
    since = "at > now() - make_interval(hours => %s)"
    with db.conn() as c:
        tools_ = c.execute(f"SELECT name, count(*) AS calls, count(*) FILTER (WHERE NOT ok) AS errors, percentile_cont(0.5) WITHIN GROUP (ORDER BY ms) AS p50, "
                           f"percentile_cont(0.95) WITHIN GROUP (ORDER BY ms) AS p95, avg((detail->>'chars')::int) AS avg_chars FROM activity_event WHERE kind='tool' AND {since} "
                           "GROUP BY name ORDER BY calls DESC", (hours,)).fetchall()
        llm = c.execute(f"SELECT name, count(*) AS calls, count(*) FILTER (WHERE NOT ok) AS errors, percentile_cont(0.5) WITHIN GROUP (ORDER BY ms) AS p50, "
                        f"percentile_cont(0.95) WITHIN GROUP (ORDER BY ms) AS p95, percentile_cont(0.5) WITHIN GROUP (ORDER BY (detail->>'ttft_ms')::int) AS ttft_p50, "
                        f"sum(tokens_in) AS tokens_in, sum(tokens_out) AS tokens_out, sum(cost) AS cost FROM activity_event WHERE kind='llm' AND {since} GROUP BY name", (hours,)).fetchall()
        sess = c.execute("SELECT source, count(*) AS sessions, count(DISTINCT actor) AS actors FROM activity_session WHERE last_at > now() - make_interval(hours => %s) GROUP BY source", (hours,)).fetchall()
    return {'hours': hours, 'tools': tools_, 'llm': llm, 'sessions': sess, 'retention_days': settings.get('ACTIVITY_DAYS')}


@app.get('/api/admin/activity/sessions')
def activity_sessions(hours: int = 168, source: str | None = None, actor: str | None = None, limit: int = 100, u=Depends(auth.admin_user)):
    with db.conn() as c:
        return c.execute(
            "SELECT s.id, s.source, s.actor, s.title, s.started_at, s.last_at, "
            "count(*) FILTER (WHERE e.kind='llm') AS llm_calls, count(*) FILTER (WHERE e.kind='tool') AS tool_calls, count(*) FILTER (WHERE NOT e.ok) AS errors, "
            "coalesce(sum(e.tokens_in),0) AS tokens_in, coalesce(sum(e.tokens_out),0) AS tokens_out, coalesce(sum(e.cost),0) AS cost, "
            "coalesce(sum(e.ms) FILTER (WHERE e.kind='llm'),0) AS llm_ms, coalesce(sum(e.ms) FILTER (WHERE e.kind='tool'),0) AS tool_ms, "
            "count(*) FILTER (WHERE e.kind='user') AS questions, "
            "(SELECT u.detail->>'text' FROM activity_event u WHERE u.session_id=s.id AND u.kind='user' ORDER BY u.id DESC LIMIT 1) AS last_question "
            "FROM activity_session s LEFT JOIN activity_event e ON e.session_id=s.id WHERE s.last_at > now() - make_interval(hours => %s) "
            "AND (%s::text IS NULL OR s.source=%s) AND (%s::text IS NULL OR s.actor=%s) GROUP BY s.id ORDER BY s.last_at DESC LIMIT %s",
            (hours, source, source, actor, actor, min(limit, 500))).fetchall()


@app.get('/api/admin/activity/sessions/{sid}')
def activity_session(sid: str, u=Depends(auth.admin_user)):
    with db.conn() as c:
        s = c.execute('SELECT * FROM activity_session WHERE id=%s', (sid,)).fetchone()
        if not s:
            raise HTTPException(404, 'no such session (it may have been purged)')
        # responses stay on the server until a fold is opened: a chat of several days would otherwise be megabytes in one request
        ev = c.execute("SELECT id, at, kind, name, ms, ok, tokens_in, tokens_out, cost, (detail - 'result') AS detail, (detail ? 'result') AS has_result "
                       "FROM activity_event WHERE session_id=%s ORDER BY id", (sid,)).fetchall()
    return {'session': s, 'events': ev}


@app.get('/api/admin/activity/events/{eid}/result')
def activity_result(eid: int, u=Depends(auth.admin_user)):
    with db.conn() as c:
        r = c.execute("SELECT detail->'result' AS result, (detail->>'truncated')::boolean AS truncated, (detail->>'chars')::int AS chars FROM activity_event WHERE id=%s AND detail ? 'result'", (eid,)).fetchone()
    if not r:
        raise HTTPException(404, 'no response kept for this step (it may have been purged)')
    return r


@app.get('/api/health')
def health():
    return {'ok': True}


# ---------------------------------------------------------------- frontend (single page app)
if FRONTEND.exists():
    @app.get('/{path:path}')
    def spa(path: str):
        f = (FRONTEND / path).resolve()
        if path and f.is_file() and FRONTEND.resolve() in f.parents:
            return FileResponse(f)
        return FileResponse(FRONTEND / 'index.html', headers={'Cache-Control': 'no-cache'})
