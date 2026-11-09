"""Taking a collection out: one zip a manager can keep, hand over, read offline or use as the source of an import elsewhere. Everything in it comes from this one
collection. What it holds: the original files (web pages as they were captured), every source page with its versions and the checks made on it, every topic as
Markdown with its statements and the evidence behind them, the links between topics and to the data model, the vocabulary, and a manifest.
What it leaves out on purpose: changes still held for review (they are unpublished), interviews (a conversation belongs to the person who held it; a finished
interview is in the zip as the source it became), and anything about other collections."""
import json
import tempfile
import zipfile
from datetime import datetime, timezone
from ..db import conn
from . import bridge, storage, vocab

FORMAT = 2                                     # 2: claims with their source wiki version and passage; pages cite [source: <id>]
MAX_ORIGINALS = 4 * 1024 ** 3


def _j(x):
    return json.dumps(x, ensure_ascii=False, indent=1, default=str)


def _front(meta):
    """Front matter in a form any YAML reader takes: one `key: <JSON value>` per line."""
    return '---\n' + '\n'.join(f'{k}: {json.dumps(v, ensure_ascii=False, default=str)}' for k, v in meta.items()) + '\n---\n\n'


README = """This is an export of one collection of the document Library.

manifest.json            what was exported, by whom, when, and what could not be included
collection.json         the collection's settings (name, usage tag, AI and agent switches)
vocabulary.json         the kinds of topic and of relation this collection used
originals/               the files as uploaded; a web page is the page as it was captured (page.html)
sources/<id>.json       what is known about each source: kind, address, size, checksum, status, title, who added it and when
pages/<id>/vN.md        the source wiki written from each source, every version, and checks.json with what the checker found
topics/<slug>.md        each topic's current page, with front matter (type, aliases, summary, links, the data-model entities it is about)
topics/<slug>.json      the same topic with every claim, its exact quote, the source, the source wiki version and passage, and the page history
topics/_history/<slug>/vN.md   earlier versions of the page
links.json              the links between topics

Topic pages cite their sources as [source: <id>]; sources/<id>.json says which document each id is. Held changes (unpublished) and interviews are not included.
"""


def build(col, user):
    """-> an open temporary file holding the zip, positioned at 0, and the manifest."""
    cid = col.id
    out = tempfile.TemporaryFile()
    missing, counts = [], {}
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as z, conn() as c:
        sources = c.execute('SELECT * FROM library.source WHERE collection_id=%s ORDER BY created_at', (cid,)).fetchall()
        total = sum(s['size'] or 0 for s in sources)
        if total > MAX_ORIGINALS:
            raise ValueError(f'the original files add up to {total // 1024 ** 3} GB, more than an export holds: remove some, or take them from the storage directly')
        for s in sources:
            sid = str(s['id'])
            z.writestr(f'sources/{sid}.json', _j({k: s[k] for k in ('id', 'name', 'kind', 'url', 'mime', 'size', 'sha256', 'status', 'wiki_status', 'title', 'stats', 'error', 'created_by', 'created_at')}))
            fname = s['storage_key'].rsplit('/', 1)[-1]
            try:
                with storage.store().open(s['storage_key']) as fh:
                    with z.open(f'originals/{sid}-{storage.clean_name(fname)}', 'w', force_zip64=True) as dst:
                        for block in iter(lambda: fh.read(1024 * 1024), b''):
                            dst.write(block)
            except Exception as e:
                missing.append({'source': sid, 'name': s['name'], 'reason': f'{type(e).__name__}'})
            for v in c.execute('SELECT version, markdown, origin, created_by, created_at FROM library.wiki_version WHERE source_id=%s AND collection_id=%s ORDER BY version', (s['id'], cid)).fetchall():
                z.writestr(f"pages/{sid}/v{v['version']}.md", _front({'source': s['name'], 'version': v['version'], 'origin': v['origin'], 'by': v['created_by'], 'at': v['created_at']}) + v['markdown'])
            checks = c.execute('SELECT version, round, verdict, issues, tool_calls, at FROM library.wiki_check WHERE source_id=%s AND collection_id=%s ORDER BY id', (s['id'], cid)).fetchall()
            if checks:
                z.writestr(f'pages/{sid}/checks.json', _j(checks))
        counts['sources'] = len(sources)
        topics = c.execute("SELECT * FROM library.topic WHERE collection_id=%s AND status='active' ORDER BY slug", (cid,)).fetchall()
        names = {t['id']: t['slug'] for t in topics}
        for t in topics:
            versions = c.execute("SELECT version, markdown, update_reason, created_at FROM library.topic_version WHERE topic_id=%s AND collection_id=%s AND status='published' ORDER BY version", (t['id'], cid)).fetchall()
            aliases = [r['alias'] for r in c.execute('SELECT alias FROM library.topic_alias WHERE topic_id=%s AND collection_id=%s ORDER BY alias', (t['id'], cid)).fetchall()]
            claims = c.execute("SELECT id, n, text, kind FROM library.claim WHERE topic_id=%s AND collection_id=%s AND status='active' ORDER BY n", (t['id'], cid)).fetchall()
            ev = c.execute("SELECT cs.claim_id, coalesce(s.name, '(deleted source)') AS source, cs.source_id, cs.quote, cs.wiki_version, cs.wiki_chunk FROM library.claim_source cs "
                           "LEFT JOIN library.source s ON s.id=cs.source_id AND s.collection_id=cs.collection_id WHERE cs.collection_id=%s AND cs.claim_id = ANY(%s) ORDER BY cs.id",
                           (cid, [x['id'] for x in claims])).fetchall()
            refs = [r for r in bridge.refs_of(t['id'], cid) if r['status'] == 'confirmed']
            links = c.execute('SELECT l.relation, o.slug FROM library.topic_link l JOIN library.topic o ON o.id=l.to_topic AND o.collection_id=l.collection_id WHERE l.from_topic=%s AND l.collection_id=%s AND o.status=\'active\' ORDER BY o.slug', (t['id'], cid)).fetchall()
            meta = {'title': t['title'], 'type': t['topic_type'], 'aliases': aliases, 'summary': t['summary'], 'links': [f"{l['relation']}: {l['slug']}" for l in links],
                    'data_model': [f"{r['relation']}: {r['kind']} {r['ref']}" for r in refs], 'version': versions[-1]['version'] if versions else None}
            z.writestr(f"topics/{t['slug']}.md", _front(meta) + (versions[-1]['markdown'] if versions else ''))
            for v in versions[:-1]:
                z.writestr(f"topics/_history/{t['slug']}/v{v['version']}.md", v['markdown'])
            z.writestr(f"topics/{t['slug']}.json", _j({**meta, 'slug': t['slug'], 'claims': [{'n': x['n'], 'text': x['text'], 'kind': x['kind'], 'evidence': [{'source': e['source'], 'source_id': str(e['source_id']) if e['source_id'] else None, 'quote': e['quote'],
                                                                                                              'wiki_version': e['wiki_version'], 'wiki_passage': e['wiki_chunk']} for e in ev if e['claim_id'] == x['id']]} for x in claims],
                                                   'versions': [{'version': v['version'], 'reason': v['update_reason'], 'at': v['created_at']} for v in versions]}))
        counts['topics'] = len(topics)
        alllinks = c.execute("SELECT l.from_topic, l.to_topic, l.relation, l.context FROM library.topic_link l WHERE l.collection_id=%s", (cid,)).fetchall()
        z.writestr('links.json', _j([{'from': names[l['from_topic']], 'relation': l['relation'], 'to': names[l['to_topic']], 'context': l['context']} for l in alllinks if l['from_topic'] in names and l['to_topic'] in names]))
        held = c.execute("SELECT count(*) AS n FROM library.topic_conflict WHERE collection_id=%s AND status='open'", (cid,)).fetchone()['n']
        z.writestr('vocabulary.json', _j(vocab.get(cid, include_rejected=True)))
        z.writestr('collection.json', _j({'name': col.name, 'slug': col.slug, 'description': col.description, 'usage': col.usage, 'allow_ai': col.allow_ai, 'agents_may_read': col.agents_may_read}))
        manifest = {'format': FORMAT, 'exported_at': datetime.now(timezone.utc).isoformat(), 'exported_by': user, 'collection': col.name, 'collection_id': cid, 'counts': counts,
                    'left_out': {'held_changes': held, 'note': 'held changes are unpublished; interviews are private conversations'}, 'files_missing_from_storage': missing}
        z.writestr('manifest.json', _j(manifest))
        z.writestr('README.txt', README)
    out.seek(0)
    return out, manifest


def stream(fh, chunk=1024 * 1024):
    try:
        for block in iter(lambda: fh.read(chunk), b''):
            yield block
    finally:
        fh.close()
