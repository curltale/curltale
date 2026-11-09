"""The source wiki: for each document a page written from it, checked claim by claim against the document by a second model, corrected
and checked again until it passes or a person has to look. Models only ever see the one document of the job (its text, its passages)."""
import json
import re
from pathlib import Path
from .. import llm, settings
from ..db import conn, J
from . import chunking, retrieval, scope as S

PROMPTS = Path(__file__).parent / 'prompts'
SMALL_SOURCE_TOKENS = 60000                    # source and page together this short go to the checker whole (when they fit the verifier's context)


def prompt(name):
    return (PROMPTS / f'{name}.md').read_text()


TOOLS = [
    # the verifier's tools: each is scoped to the one source of the job, so the model cannot cite unrelated material.
    {'type': 'function', 'function': {'name': 'semantic_search_source', 'description': 'Search the single source semantically and return likely anchor chunks.',
                                      'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}, 'max_anchors': {'type': 'integer', 'minimum': 1, 'maximum': 12}}, 'required': ['query']}}},
    {'type': 'function', 'function': {'name': 'read_source_window', 'description': 'Read an anchor source chunk plus previous and next chunks.',
                                      'parameters': {'type': 'object', 'properties': {'chunk_index': {'type': 'integer'}, 'before': {'type': 'integer', 'minimum': 0, 'maximum': 8},
                                                                                      'after': {'type': 'integer', 'minimum': 0, 'maximum': 8}}, 'required': ['chunk_index']}}},
    {'type': 'function', 'function': {'name': 'list_source_outline', 'description': 'Return a compact outline/snippet map of all chunks in the single source.', 'parameters': {'type': 'object', 'properties': {}}}},
]
AMENDMENT_TOOLS = [t for t in TOOLS if t['function']['name'] != 'list_source_outline']     # the amendment writer already has the page and the findings: no map
WINDOW_CHARS = 40000                           # one window never takes more than this


class SourceTools:
    """The tools a model may use on ONE source. Everything is filtered by collection and source, so a model cannot read anything else.
    Search is hybrid (meaning and words), which finds exact codes and numbers that a vector alone can miss."""

    def __init__(self, cid, sid):
        self.col, self.sid = S.trusted(cid), str(sid)

    def __call__(self, name, args):
        if name == 'semantic_search_source':
            k = max(1, min(int(args.get('max_anchors') or args.get('k') or 6), 12))
            hits = retrieval.search(self.col, str(args.get('query', '')), k, source_id=self.sid)
            return json.dumps({'anchors': [{'chunk_index': h['chunk_index'], 'heading_path': (h['section'] or '').split(' > ') if h['section'] else [], 'location': h['location'],
                                            'score': h['score'], 'snippet': h['text'][:700]} for h in hits]})
        if name == 'read_source_window':
            i, b, a = int(args['chunk_index']), min(max(int(args.get('before', 1)), 0), 8), min(max(int(args.get('after', 1)), 0), 8)
            with conn() as c:
                rows = c.execute('SELECT idx, location, heading_path, text FROM library.source_chunk WHERE collection_id=%s AND source_id=%s AND idx BETWEEN %s AND %s ORDER BY idx',
                                 (self.col.id, self.sid, max(0, i - b), i + a)).fetchall()
            out, used, cut = [], 0, False
            for r in rows:
                if out and used + len(r['text']) > WINDOW_CHARS:
                    cut = True
                    break
                used += len(r['text'])
                out.append({'chunk_index': r['idx'], 'heading_path': r['heading_path'], 'location': r['location'], 'text': r['text']})
            return json.dumps({'chunks': out, 'truncated': cut})
        if name == 'list_source_outline':
            with conn() as c:
                rows = c.execute('SELECT idx, location, heading_path, tokens, left(text, 160) AS start FROM library.source_chunk WHERE collection_id=%s AND source_id=%s ORDER BY idx', (self.col.id, self.sid)).fetchall()
            return json.dumps({'chunks': [{'chunk_index': r['idx'], 'heading_path': r['heading_path'], 'location': r['location'], 'token_count': r['tokens'], 'snippet': r['start']} for r in rows]})
        raise ValueError(f'unknown tool {name}')


# ---------------------------------------------------------------- writing
def _strip_preamble(md):
    """Models sometimes start with a sentence before the page: keep from the first heading."""
    md = md.strip()
    m = re.search(r'^#{1,6} ', md, re.M)
    return md[m.start():].strip() if m else md


def write(text, title, parts):
    """The first version of the page. A document that fits is written in one go; a longer one section by section (cut at passage boundaries), then joined."""
    limit = settings.get('WRITER_SECTION_TOKENS')
    head = f'Subject name hint (use it if the content does not name the subject better): {title}\n\n' if title else ''
    if chunking.tokens(text) <= limit:
        return _strip_preamble(llm.chat([{'role': 'system', 'content': prompt('writer')}, {'role': 'user', 'content': head + 'Source content:\n\n' + text}], role='writer', max_tokens=12000, temperature=0.2))
    groups, cur, size = [], [], 0
    for p in parts:
        t = chunking.tokens(text[p['char_start']:p['char_end']])
        if cur and size + t > limit:
            groups.append(cur); cur, size = [], 0
        cur.append(p); size += t
    groups.append(cur)
    sections = []
    for n, g in enumerate(groups, 1):                                      # slices of the text, so the overlap between passages is not written twice
        chunk = text[g[0]['char_start']:g[-1]['char_end']]
        user = f'This is section {n} of {len(groups)} from the source.\n\nSource content:\n\n{chunk}'
        sections.append(_strip_preamble(llm.chat([{'role': 'system', 'content': prompt('writer_section')}, {'role': 'user', 'content': user}], role='writer', max_tokens=8000, temperature=0.2)))
    # each section starts with ##; the joined page gets the source title as its # heading (sections are joined with a rule)
    return (f'# {title}\n\n' if title else '') + '\n\n---\n\n'.join(sections)


# ---------------------------------------------------------------- checking
def _json_object(text):
    text = (text or '').strip()
    text = re.sub(r'^```(?:json)?|```$', '', text, flags=re.M).strip()
    start = text.find('{')
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            depth += (text[i] == '{') - (text[i] == '}')
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except ValueError:
                    break
        start = text.find('{', start + 1)
    return None


def _json_array(text):
    text = re.sub(r'^```(?:json)?|```$', '', (text or '').strip(), flags=re.M).strip()
    i, j = text.find('['), text.rfind(']')
    if i == -1 or j < i:
        return None
    try:
        return json.loads(text[i:j + 1])
    except ValueError:
        return None


def _spans(res):
    spans = res.get('unsupported_spans')
    if spans is None:
        spans = res.get('issues')
    spans = spans if isinstance(spans, list) else ([spans] if spans else [])
    out = []
    for sp in spans:
        if isinstance(sp, dict):
            sp = dict(sp)
            if not isinstance(sp.get('evidence_chunks'), list):
                sp['evidence_chunks'] = []
            sp.setdefault('type', sp.get('issue_type') or 'OTHER')                     # the review screen reads `type`
            out.append(sp)
    return out


REVIEW = ('Review your unsupported_spans before they go to the writer. Remove every entry that is not a real problem: an entry whose reason says the text is '
          'supported, broadly or largely consistent, acceptable, a minor paraphrase, faithful, or "not an error", and any entry kept only for completeness. '
          'Keep only entries where the wiki text is unsupported, too broad, missing a material fact or qualifier, or numerically wrong. Return the corrected JSON '
          'verdict in the same format: if no entry remains, return {"verdict": "PASS", "unsupported_spans": []}.')


def check(tools, markdown, whole_source=None):
    """The verifier. -> (verdict 'PASS'|'FAIL'|'ERROR', unsupported_spans, tool_calls).
    A FAIL that names no problem is asked once for specifics; if it still names none, it comes back as FAIL with no spans, and the loop checks the same
    version again rather than letting the writer amend with nothing to act on. ERROR: no usable JSON even after two format corrections."""
    if whole_source:
        head = ('The full source content is included below. You have all the evidence you need — do NOT use any tools. Verify directly from the text and respond with your JSON verdict.\n\n'
                f'Full source content:\n\n{whole_source}\n\n')
        user = head + f'Wiki page to verify:\n\n{markdown}'
        model = llm.model_for('verifier')
        if 'anthropic/' in model or 'claude' in model:                                    # the document is the same in every round, the page is not: cache the document on its own
            user = [{'type': 'text', 'text': head, 'cache_control': {'type': 'ephemeral'}}, {'type': 'text', 'text': f'Wiki page to verify:\n\n{markdown}'}]
    else:
        user = f'Wiki page to verify:\n\n{markdown}'
    msgs = [{'role': 'system', 'content': prompt('verifier')}, {'role': 'user', 'content': user}]
    calls, format_retries, empty_retried, reviewed = 0, 0, False, False
    while True:
        text, n = llm.chat_tools(msgs, TOOLS, tools, role='verifier', max_rounds=30, max_tokens=6000)
        calls += n
        res = _json_object(text)
        if not (isinstance(res, dict) and res.get('verdict') in ('PASS', 'FAIL')):
            if isinstance(res, dict):                                                   # a JSON object with a bad verdict is a FAIL
                res = {**res, 'verdict': 'FAIL'}
            elif format_retries < 2:
                format_retries += 1
                msgs = msgs + [{'role': 'assistant', 'content': text or ''}, {'role': 'user', 'content': 'Your response could not be parsed as JSON. You must return only a JSON object in the exact format specified in your instructions: {"verdict": "PASS" | "FAIL", "unsupported_spans": [...]}. Output JSON only, with no surrounding text or code fences.'}]
                continue
            else:
                return 'ERROR', [], calls
        spans = _spans(res)
        if res['verdict'] == 'FAIL' and not spans and not empty_retried:
            empty_retried = True
            msgs = msgs + [{'role': 'assistant', 'content': text or ''}, {'role': 'user', 'content': 'You returned FAIL but provided no unsupported_spans entries. A FAIL verdict requires at least one entry describing what is wrong. Please re-evaluate the same wiki page: if you cannot identify any specific unsupported claim, return PASS. If there are genuine problems, return FAIL with unsupported_spans entries — use the wiki_text and reason fields to describe each issue even if you cannot point to an exact span.'}]
            continue
        if res['verdict'] == 'FAIL' and spans and not reviewed:
            # one self-review (like the FAIL-without-spans retry): models often list spans they themselves call supported
            reviewed = True
            msgs = msgs + [{'role': 'assistant', 'content': text or ''}, {'role': 'user', 'content': REVIEW}]
            continue
        return res['verdict'], ([] if res['verdict'] == 'PASS' else spans), calls


def _strip_amendment_preamble(text):
    """Keep from the first Markdown heading that is not inside a code fence (the amendment writer sometimes reasons before the page)."""
    in_fence, pos = False, 0
    for line in (text or '').splitlines(keepends=True):
        st = line.strip()
        if st.startswith('```'):
            in_fence = not in_fence
        elif not in_fence and re.match(r'^#{1,6}\s+\S', st):
            return text[pos:].strip()
        pos += len(line)
    return (text or '').strip()


def amend(tools, markdown, spans):
    verifier_result = {'verdict': 'FAIL', 'unsupported_spans': spans}
    user = f'Current wiki page:\n\n{markdown}\n\nVerifier findings and evidence:\n\n{json.dumps(verifier_result, indent=2, ensure_ascii=False)}'
    text, _ = llm.chat_tools([{'role': 'system', 'content': prompt('amend')}, {'role': 'user', 'content': user}], AMENDMENT_TOOLS, tools, role='writer', max_rounds=10, max_tokens=12000)
    out = _strip_amendment_preamble(text)
    if not out:
        raise RuntimeError('the writer returned nothing when asked to correct the page')
    return out


# ---------------------------------------------------------------- the loop
def save_version(cid, sid, markdown, origin, user=None):
    """Store a version of the source wiki and index it (chunked, embedded, word-searchable) (every version)."""
    from . import index
    with conn() as c:
        v = c.execute('SELECT coalesce(max(version),0)+1 AS v FROM library.wiki_version WHERE source_id=%s', (sid,)).fetchone()['v']
        c.execute('INSERT INTO library.wiki_version (collection_id,source_id,version,markdown,origin,created_by) VALUES (%s,%s,%s,%s,%s,%s)', (cid, sid, v, markdown, origin, user))
    index.index_wiki(cid, sid, v, markdown)
    return v


def save_check(cid, sid, version, rnd, verdict, issues, calls):
    with conn() as c:
        c.execute('INSERT INTO library.wiki_check (collection_id,source_id,version,round,verdict,issues,tool_calls) VALUES (%s,%s,%s,%s,%s,%s,%s)', (cid, sid, version, rnd, verdict, J(issues), calls))


def run(cid, sid, text, title, parts, ctx=None):
    """The state machine: write, verify, amend, verify... Returns 'approved' or 'needs_review'. Every version and every check is kept.
    A FAIL that carries no spans re-verifies the same version (no new version, no blind amendment)."""
    step = ctx.progress if ctx else (lambda *a, **k: None)
    tools = SourceTools(cid, sid)
    step('writing the page', 55)
    md = write(text, title, parts)
    if not md.strip():
        raise RuntimeError('the writer returned nothing')
    ver = save_version(cid, sid, md, 'written')
    whole = text if chunking.tokens(text + md) <= SMALL_SOURCE_TOKENS else None
    rounds = settings.get('MAX_AMENDMENT_ROUNDS')
    for rnd in range(rounds + 1):
        step('checking the page against the document' if rnd == 0 else f'checking the page again (round {rnd})', 65 + int(25 * rnd / (rounds + 1)))
        verdict, spans, calls = check(tools, md, whole)
        save_check(cid, sid, ver, rnd, verdict, spans, calls)
        if verdict == 'PASS':
            return 'approved'
        if rnd >= rounds:
            break
        if verdict == 'ERROR' or not spans:
            continue                                                                    # nothing actionable: verify the same version again
        step(f'correcting {len(spans)} problem(s)', 65 + int(25 * (rnd + 0.5) / (rounds + 1)))
        md = amend(tools, md, spans)
        ver = save_version(cid, sid, md, 'amended')
    return 'needs_review'
