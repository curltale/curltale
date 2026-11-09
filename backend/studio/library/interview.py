"""Interviews: a person documents what they know by being asked questions. Two layers of memory: the conversation (recent turns go to the model) and the claims
(every fact established, always given in full). When the person is ready a draft is written from the claims; once they approve it, it joins the collection as a
source like any other, and the topic wiki is built from it."""
import hashlib
import io
import json
from collections import defaultdict
from pathlib import Path
from .. import llm
from ..db import conn
from . import jobs, scope as S, storage, wiki

PROMPTS = Path(__file__).parent / 'prompts'
TURNS_IN_CONTEXT = 20


def _claims(c, iid):
    return c.execute("SELECT id, text, category FROM library.interview_claim WHERE interview_id=%s AND status='active' ORDER BY id", (iid,)).fetchall()


def render_claims(claims):
    if not claims:
        return '(none yet)'
    return '\n'.join(f"- {'[' + c['category'] + '] ' if c.get('category') else ''}{c['text']}  (id: {c['id']})" for c in claims)


def messages_for(iid, cid, content):
    """The model's context for the next reply: the seed and all claims in the system prompt, the last turns as messages, then the new message."""
    with conn() as c:
        it = c.execute('SELECT seed FROM library.interview WHERE id=%s AND collection_id=%s', (iid, cid)).fetchone()
        turns = c.execute('SELECT role, content FROM library.interview_turn WHERE interview_id=%s AND collection_id=%s ORDER BY id DESC LIMIT %s', (iid, cid, TURNS_IN_CONTEXT)).fetchall()[::-1]
        claims = _claims(c, iid)
    system = (PROMPTS / 'interview.md').read_text().replace('{{SEED}}', it['seed']).replace('{{CLAIMS}}', render_claims(claims))
    return [{'role': 'system', 'content': system}] + [{'role': 'user' if t['role'] == 'user' else 'assistant', 'content': t['content']} for t in turns] + [{'role': 'user', 'content': content}]


def reply_stream(cid, iid, content):
    """Store the person's message, stream the interviewer's reply, store it, and ask for the facts of this exchange to be extracted (a background job).
    Yields text pieces. What was written is kept even if the reader leaves half way."""
    msgs = messages_for(iid, cid, content)                   # (built before the new message is stored: it is appended once, at the end)
    with conn() as c:
        c.execute("INSERT INTO library.interview_turn (collection_id,interview_id,role,content) VALUES (%s,%s,'user',%s)", (cid, iid, content))
    parts, saved = [], False

    def save():
        nonlocal saved
        text = ''.join(parts).strip()
        if saved or not text:
            return None
        saved = True
        with conn() as c:
            tid = c.execute("INSERT INTO library.interview_turn (collection_id,interview_id,role,content) VALUES (%s,%s,'agent',%s) RETURNING id", (cid, iid, text)).fetchone()['id']
        jobs.enqueue('interview.extract', cid, None, {'interview_id': str(iid), 'turn_id': tid})
        return tid

    try:
        for piece in llm.chat_stream(msgs, role='interview', max_tokens=1200):
            parts.append(piece)
            yield piece
    finally:
        save()


def extract(cid, iid, turn_id):
    """Apply the facts established in one exchange to the claim store."""
    with conn() as c:
        turn = c.execute("SELECT id, content FROM library.interview_turn WHERE id=%s AND interview_id=%s AND collection_id=%s AND role='agent'", (turn_id, iid, cid)).fetchone()
        if not turn:
            return 0
        user = c.execute("SELECT content FROM library.interview_turn WHERE interview_id=%s AND collection_id=%s AND role='user' AND id<%s ORDER BY id DESC LIMIT 1", (iid, cid, turn_id)).fetchone()
        claims = _claims(c, iid)
    prompt = (PROMPTS / 'interview_claims.md').read_text().replace('{{CLAIMS}}', render_claims(claims)).replace('{{USER_TURN}}', user['content'] if user else '').replace('{{AGENT_TURN}}', turn['content'])
    ops = None
    for _ in range(2):
        data = wiki._json_array(llm.chat([{'role': 'user', 'content': prompt}], role='interview', max_tokens=1500, temperature=0))
        if isinstance(data, list):
            ops = data
            break
    if ops is None:
        raise RuntimeError('the extractor did not return a usable answer')
    known = {str(c['id']) for c in claims}                                  # the model may give an id as a number or as text
    n = 0
    with conn() as c:
        for op in ops:
            if not isinstance(op, dict) or not str(op.get('claim_text', '')).strip():
                continue
            text, cat = str(op['claim_text']).strip()[:1000], (str(op.get('category') or '').strip() or None)
            if op.get('op') in ('update', 'supersede') and str(op.get('claim_id')) in known:                    # the extractor says 'supersede'
                c.execute("UPDATE library.interview_claim SET text=%s, category=coalesce(%s, category) WHERE id=%s AND interview_id=%s AND collection_id=%s", (text, cat, int(op['claim_id']), iid, cid))
            elif op.get('op') in ('add', 'update', 'supersede'):
                c.execute('INSERT INTO library.interview_claim (collection_id,interview_id,text,category) VALUES (%s,%s,%s,%s)', (cid, iid, text, cat))
            else:
                continue
            n += 1
    return n


def write_draft(cid, iid):
    with conn() as c:
        it = c.execute('SELECT seed FROM library.interview WHERE id=%s AND collection_id=%s', (iid, cid)).fetchone()
        claims = _claims(c, iid)
    if not claims:
        raise ValueError('there are no facts to write from yet: carry on with the interview first')
    grouped = defaultdict(list)
    for cl in claims:
        grouped[cl['category'] or 'general'].append(cl['text'])
    body = '\n'.join(f'### {k.title()}\n' + '\n'.join(f'- {t}' for t in v) + '\n' for k, v in grouped.items())
    md = wiki._strip_preamble(llm.chat([{'role': 'user', 'content': (PROMPTS / 'interview_draft.md').read_text().replace('{{SEED}}', it['seed']).replace('{{CLAIMS_BY_CATEGORY}}', body).replace('{{CLAIMS}}', body)}], role='topic_writer', max_tokens=8000, temperature=0.3))
    if not md.strip():
        raise RuntimeError('the writer returned nothing')
    return save_draft(cid, iid, md, 'written')


def save_draft(cid, iid, markdown, origin):
    with conn() as c:
        v = c.execute('SELECT coalesce(max(version),0)+1 AS v FROM library.interview_draft WHERE interview_id=%s', (iid,)).fetchone()['v']
        c.execute('INSERT INTO library.interview_draft (collection_id,interview_id,version,markdown,origin) VALUES (%s,%s,%s,%s,%s)', (cid, iid, v, markdown, origin))
        c.execute("UPDATE library.interview SET status='draft_ready' WHERE id=%s AND collection_id=%s AND status='active'", (iid, cid))
    return v


def finalize(cid, iid, user):
    """The approved draft becomes a source of the collection. It is read and indexed like any file; its page is the draft itself (a person has already reviewed it), and the topic wiki is built from it."""
    with conn() as c:
        it = c.execute("UPDATE library.interview SET status='ingesting' WHERE id=%s AND collection_id=%s AND status IN ('active','draft_ready') RETURNING seed", (iid, cid)).fetchone()
        if not it:
            raise LookupError('this interview is already being added or was added')
        draft = c.execute('SELECT markdown FROM library.interview_draft WHERE interview_id=%s AND collection_id=%s ORDER BY version DESC LIMIT 1', (iid, cid)).fetchone()
        if not draft:
            c.execute("UPDATE library.interview SET status='active' WHERE id=%s", (iid,))
            raise ValueError('write a draft first')
    try:
        data = draft['markdown'].encode()
        name = f'interview-{str(iid)[:8]}.md'
        title = (it['seed'].split('.')[0].split('\n')[0].strip()[:120]) or 'Interview'
        with conn() as c:
            sid = c.execute("INSERT INTO library.source (collection_id,name,kind,mime,size,sha256,storage_key,created_by,title) VALUES (%s,%s,'interview','text/markdown',%s,%s,'pending',%s,%s) RETURNING id",
                            (cid, name, len(data), hashlib.sha256(data).hexdigest(), user, title)).fetchone()['id']
            key = f'{cid}/{sid}/{name}'
            storage.store().put(key, io.BytesIO(data))
            c.execute('UPDATE library.source SET storage_key=%s WHERE id=%s', (key, sid))
            c.execute("UPDATE library.interview SET source_id=%s, status='ingested' WHERE id=%s", (sid, iid))
        jobs.enqueue('source.ingest', cid, sid, user=user)
        return sid
    except Exception:
        with conn() as c:
            c.execute("UPDATE library.interview SET status='draft_ready' WHERE id=%s AND status='ingesting'", (iid,))
        raise
