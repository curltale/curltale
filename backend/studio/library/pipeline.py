"""The ingestion pipeline: read the stored file, extract its text, cut it into passages that fit the embedding model, embed them.
The wiki steps (writing, verifying, finding topics) are added after this one. Each step reports progress through ctx."""
import hashlib
from .. import config, embeddings, llm, settings, vectors
from ..db import conn, J
from . import bridge, chunking, extract, fetch, interview, jobs, scope as S, storage, topics, wiki

BATCH = 16
MAX_FILE_BYTES = 500 * 1024 * 1024


def _fail(src_id, message):
    with conn() as c:
        c.execute("UPDATE library.source SET status='failed', error=%s, wiki_status=CASE WHEN wiki_status='none' THEN 'failed' ELSE wiki_status END WHERE id=%s", (message[:300], src_id))


def embed_chunks(rows, ctx=None, lo=50, hi=95):
    """Fill the vector of passages given as [(id, text)]. Returns how many were embedded."""
    cast = vectors.spec(config.EMBEDDING_DIM)['cast']
    done = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        vecs = embeddings.embed([t for _, t in batch])
        with conn() as c:
            for (cid, _), v in zip(batch, vecs):
                c.execute(f'UPDATE library.source_chunk SET embedding=%s::{cast} WHERE id=%s', (embeddings.vec_literal(v), cid))
        done += len(batch)
        if ctx:
            ctx.progress('embedding passages', lo + int((hi - lo) * done / len(rows)))
    return done


@jobs.handler('source.ingest')
def ingest(job, ctx):
    sid, col = job['source_id'], job['collection_id']
    with conn() as c:
        src = c.execute('SELECT * FROM library.source WHERE id=%s AND collection_id=%s', (sid, col)).fetchone()
        if not src:
            return                                                      # removed while it waited
        c.execute("UPDATE library.source SET status='processing', error=NULL WHERE id=%s", (sid,))
    try:
        ctx.progress('reading the stored file', 5)
        if src['size'] > MAX_FILE_BYTES:
            raise extract.ExtractError('the file is too large to read')
        h = hashlib.sha256()
        with storage.store().open(src['storage_key']) as f:
            data = f.read()
        h.update(data)
        if h.hexdigest() != src['sha256'] or len(data) != src['size']:
            raise RuntimeError('the stored file does not match what was uploaded')

        ctx.progress('extracting the text', 15)
        ocr_errors = []
        allow_ai = S.trusted(col).allow_ai

        def ocr(png, mime):
            try:
                return llm.ocr(png, mime)
            except Exception as e:                                       # one unreadable page must not lose the others
                ocr_errors.append(f'{type(e).__name__}: {str(e)[:120]}')
                return ''
        fname = src['storage_key'].rsplit('/', 1)[-1]                    # the stored file's own name decides how it is read (a web page is stored as page.html)
        res = extract.extract(fname, data, ocr if (llm.available() and allow_ai) else None, settings.get('OCR_MAX_PAGES'))
        if ocr_errors:
            res.notes.append(f'OCR failed on {len(ocr_errors)} page(s): {ocr_errors[0]}')

        ctx.progress('splitting into passages', 40)
        parts = chunking.chunk(res.text)
        with conn() as c:
            c.execute('DELETE FROM library.source_chunk WHERE source_id=%s AND collection_id=%s', (sid, col))
            c.execute('DELETE FROM library.source_text WHERE source_id=%s AND collection_id=%s', (sid, col))
            c.execute('INSERT INTO library.source_text (source_id,collection_id,title,text,extractor) VALUES (%s,%s,%s,%s,%s)', (sid, col, res.title, res.text, fname.rsplit('.', 1)[-1].lower()))
            ids = []
            for p in parts:
                ids.append(c.execute('INSERT INTO library.source_chunk (collection_id,source_id,idx,text,tokens,char_start,char_end,heading_path,location) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                                     (col, sid, p['idx'], p['text'], p['tokens'], p['char_start'], p['char_end'], p['heading_path'], p['location'])).fetchone()['id'])
        embedded = 0
        if embeddings.available():
            ctx.progress('embedding passages', 50)
            embedded = embed_chunks(list(zip(ids, [p['text'] for p in parts])), ctx)
        else:
            res.notes.append('passages are not embedded yet: no embedding model is available, so search uses keywords only')
        wiki_status = 'skipped'
        if src['kind'] == 'interview':                                      # a person has already reviewed this text: it is its own page
            wiki.save_version(col, sid, res.text, 'edited', src['created_by'])
            wiki_status = 'approved'
        elif not allow_ai:
            res.notes.append('this collection does not allow AI: no source page was written; passages are searchable')
        elif not llm.available():
            res.notes.append('no AI endpoint is set (Settings, Alfred and AI): no source page was written; passages are searchable')
        else:
            try:
                wiki_status = wiki.run(col, sid, res.text, res.title or fname.rsplit('.', 1)[0], parts, ctx)
            except jobs.Cancelled:
                raise
            except Exception as e:                                          # the passages are fine and searchable: only the page failed, and can be regenerated
                wiki_status = 'failed'
                res.notes.append(f'the source page could not be written: {type(e).__name__}: {str(e)[:200]}')
        stats = {'chars': len(res.text), 'passages': len(parts), 'embedded': embedded, 'pages': res.pages, 'ocr_pages': res.ocr_pages, 'notes': res.notes}
        with conn() as c:
            c.execute("UPDATE library.source SET status='ready', error=NULL, title=%s, stats=%s, wiki_status=%s WHERE id=%s", (res.title, J(stats), wiki_status, sid))
        ctx.progress('done', 100)
        if wiki_status == 'approved':
            topics.queue(col, sid, job['created_by'])
    except jobs.Cancelled:
        raise
    except extract.ExtractError as e:
        _fail(sid, str(e))
        raise jobs.Permanent(str(e))                                     # no point trying the same file again
    except Exception as e:
        _fail(sid, str(e))
        raise


def backfill_embeddings(limit=2000):
    """Passages without a vector (an embedding model that was missing, or a changed vector size): compute them. Returns how many."""
    if not embeddings.available():
        return 0
    with conn() as c:
        rows = c.execute('SELECT id, text FROM library.source_chunk WHERE embedding IS NULL ORDER BY id LIMIT %s', (limit,)).fetchall()
    return embed_chunks([(r['id'], r['text']) for r in rows]) if rows else 0


@jobs.handler('source.wiki')
def rewrite(job, ctx):
    """Write the source page again from the stored text (after a failure, a changed model, or a person's request)."""
    sid, col = job['source_id'], job['collection_id']
    with conn() as c:
        src = c.execute('SELECT s.name, s.title, t.text FROM library.source s JOIN library.source_text t ON t.source_id=s.id AND t.collection_id=s.collection_id WHERE s.id=%s AND s.collection_id=%s', (sid, col)).fetchone()
        if not src:
            return
        parts = c.execute('SELECT idx, char_start, char_end FROM library.source_chunk WHERE source_id=%s AND collection_id=%s ORDER BY idx', (sid, col)).fetchall()
    if not S.trusted(col).allow_ai or not llm.available():
        raise jobs.Permanent('AI is not available for this collection')
    try:
        status = wiki.run(col, sid, src['text'], src['title'] or src['name'].rsplit('.', 1)[0], parts, ctx)
    except jobs.Cancelled:
        raise
    except Exception as e:
        with conn() as c:
            c.execute("UPDATE library.source SET wiki_status='failed' WHERE id=%s", (sid,))
        raise
    with conn() as c:
        c.execute('UPDATE library.source SET wiki_status=%s WHERE id=%s', (status, sid))
    if status == 'approved':
        topics.queue(col, sid, job['created_by'])


@jobs.handler('source.wiki_check')
def recheck(job, ctx):
    """Check a page a person has edited (once, without correcting it: the text is theirs). Passing approves it; otherwise it waits for review with the objections."""
    sid, col = job['source_id'], job['collection_id']
    with conn() as c:
        v = c.execute('SELECT version, markdown FROM library.wiki_version WHERE source_id=%s AND collection_id=%s ORDER BY version DESC LIMIT 1', (sid, col)).fetchone()
        t = c.execute('SELECT text FROM library.source_text WHERE source_id=%s AND collection_id=%s', (sid, col)).fetchone()
    if not v or not t:
        return
    if not S.trusted(col).allow_ai or not llm.available():
        raise jobs.Permanent('AI is not available for this collection')
    ctx.progress('checking the edited page against the document', 40)
    tools = wiki.SourceTools(col, sid)
    verdict, issues, calls = wiki.check(tools, v['markdown'], t['text'] if chunking.tokens(t['text']) <= wiki.SMALL_SOURCE_TOKENS else None)
    wiki.save_check(col, sid, v['version'], 0, verdict, issues, calls)
    with conn() as c:
        c.execute('UPDATE library.source SET wiki_status=%s WHERE id=%s', ('approved' if verdict == 'PASS' else 'needs_review', sid))
    if verdict == 'PASS':
        topics.queue(col, sid, job['created_by'])


@jobs.handler('source.fetch')
def fetch_source(job, ctx):
    """Capture a web address: fetch it safely, store what came back as the source's file, then read it like any upload."""
    import hashlib as _h
    import io
    import httpx
    sid, col = job['source_id'], job['collection_id']
    with conn() as c:
        src = c.execute('SELECT * FROM library.source WHERE id=%s AND collection_id=%s', (sid, col)).fetchone()
        if not src or not src['url']:
            return
        c.execute("UPDATE library.source SET status='processing', error=NULL WHERE id=%s", (sid,))
    ctx.progress('fetching the web page', 5)
    try:
        got = fetch.fetch(src['url'])
    except fetch.FetchError as e:
        _fail(sid, str(e))
        raise jobs.Permanent(str(e))
    except httpx.HTTPError as e:                                               # a network hiccup: worth another try
        _fail(sid, f'the page could not be fetched ({type(e).__name__})')
        raise
    digest = _h.sha256(got.content).hexdigest()
    key = f'{col}/{sid}/{got.filename}'
    notes = ['captured with the browser service' if got.rendered else 'captured without running JavaScript']
    if got.final_url != src['url']:
        notes.append(f'the address redirected to {got.final_url}')
    with conn() as c:
        same = c.execute('SELECT 1 FROM library.source WHERE id=%s AND sha256=%s', (sid, digest)).fetchone()
        clash = c.execute('SELECT name FROM library.source WHERE collection_id=%s AND sha256=%s AND id<>%s', (col, digest, sid)).fetchone()
    if clash:
        _fail(sid, f"this page has the same content as '{clash['name']}', which is already in the collection")
        raise jobs.Permanent('duplicate content')
    if same and (src['stats'] or {}).get('passages') and src['wiki_status'] != 'failed':   # read before, and nothing changed since      # nothing changed since the last capture
        with conn() as c:
            c.execute("UPDATE library.source SET status='ready', stats = stats || %s::jsonb WHERE id=%s", (J({'recaptured_unchanged': True}), sid))
        ctx.progress('the page has not changed', 100)
        return
    ctx.progress('storing the page', 10)
    storage.store().put(key, io.BytesIO(got.content))
    with conn() as c:
        c.execute('UPDATE library.source SET sha256=%s, size=%s, storage_key=%s, title=coalesce(%s,title) WHERE id=%s', (digest, len(got.content), key, got.title, sid))
    ingest(job, ctx)
    if notes:
        with conn() as c:
            c.execute("UPDATE library.source SET stats = jsonb_set(stats, '{notes}', coalesce(stats->'notes','[]'::jsonb) || %s::jsonb) WHERE id=%s", (J(notes), sid))


@jobs.handler('source.topics')
def update_topics(job, ctx):
    """An approved source page changes the topic wiki of its collection."""
    sid, col = job['source_id'], job['collection_id']
    with conn() as c:
        src = c.execute("SELECT s.name, s.title, w.markdown FROM library.source s JOIN library.wiki_version w ON w.source_id=s.id AND w.collection_id=s.collection_id "
                        "WHERE s.id=%s AND s.collection_id=%s AND s.wiki_status='approved' ORDER BY w.version DESC LIMIT 1", (sid, col)).fetchone()
    if not src:
        return                                                          # removed, or no longer approved, while it waited
    if not S.trusted(col).allow_ai or not llm.available():
        raise jobs.Permanent('AI is not available for this collection')
    result = topics.run(col, sid, src['markdown'], src['title'] or src['name'], ctx, job['created_by'] or 'system')
    with conn() as c:
        c.execute("UPDATE library.source SET stats = stats || %s::jsonb WHERE id=%s", (J({'topics': result}), sid))
    ctx.progress('done', 100)


@jobs.handler('topic.revise')
def revise_topic(job, ctx):
    """Revise a topic page after a source it relied on was removed or replaced."""
    col, tid = job['collection_id'], (job['payload'] or {}).get('topic_id')
    if not tid:
        return
    if not S.trusted(col).allow_ai or not llm.available():
        raise jobs.Permanent('AI is not available for this collection')
    ctx.progress('revising the topic page', 30)
    result = topics.revise(col, tid)
    ctx.progress(result, 100)


@jobs.handler('topic.link_model')
def link_model(job, ctx):
    """Connect new and changed topics to the entities of the governed model they are about."""
    col = job['collection_id']
    if not S.trusted(col).allow_ai or not llm.available():
        raise jobs.Permanent('AI is not available for this collection')
    ids = (job['payload'] or {}).get('topic_ids') or []
    for i, tid in enumerate(ids):
        ctx.progress('linking topics to the model', int(100 * i / max(1, len(ids))))
        bridge.suggest(col, tid, job['created_by'] or 'system')


@jobs.handler('interview.extract')
def interview_extract(job, ctx):
    p = job['payload'] or {}
    if not S.trusted(job['collection_id']).allow_ai or not llm.available():
        raise jobs.Permanent('AI is not available for this collection')
    interview.extract(job['collection_id'], p['interview_id'], p['turn_id'])


@jobs.handler('interview.draft')
def interview_draft(job, ctx):
    p = job['payload'] or {}
    if not S.trusted(job['collection_id']).allow_ai or not llm.available():
        raise jobs.Permanent('AI is not available for this collection')
    ctx.progress('writing the draft', 30)
    try:
        interview.write_draft(job['collection_id'], p['interview_id'])
    except ValueError as e:
        raise jobs.Permanent(str(e))
