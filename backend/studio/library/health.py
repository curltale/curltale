"""A health check of a collection's wiki, done without any model: what is stale, unsupported, contradictory or lonely, with where to look."""
from .. import config, vectors
from ..db import conn, J
from . import topics


def run(cid, user=None):
    out = []
    add = lambda sev, kind, msg, **ref: out.append({'severity': sev, 'kind': kind, 'message': msg, **ref})
    with conn() as c:
        for r in c.execute("SELECT id, kind, description, (SELECT slug FROM library.topic t WHERE t.id=k.topic_id) AS slug FROM library.topic_conflict k WHERE collection_id=%s AND status='open' ORDER BY id", (cid,)).fetchall():
            add('warn', 'held_change', f"A change waits for a decision: {r['description']}", conflict=r['id'], topic=r['slug'])
        for r in c.execute("SELECT slug, title FROM library.topic WHERE collection_id=%s AND status='active' AND needs_revision ORDER BY title", (cid,)).fetchall():
            add('warn', 'needs_revision', f"'{r['title']}' relied on a document that was removed or replaced and has not been revised yet.", topic=r['slug'])
        # lint checks: orphan topics, topics with no published version, active claims with no source citation
        for r in c.execute("SELECT t.slug, t.title FROM library.topic t WHERE t.collection_id=%s AND t.status='active' AND NOT EXISTS (SELECT 1 FROM library.topic_source s WHERE s.topic_id=t.id) ORDER BY t.title", (cid,)).fetchall():
            add('warn', 'orphan_topic', f"'{r['title']}' has no source evidence left.", topic=r['slug'])
        for r in c.execute("SELECT t.slug, t.title FROM library.topic t WHERE t.collection_id=%s AND t.status='active' AND NOT EXISTS (SELECT 1 FROM library.topic_version v WHERE v.topic_id=t.id AND v.status='published') ORDER BY t.title", (cid,)).fetchall():
            add('warn', 'no_published_version', f"'{r['title']}' has no published page.", topic=r['slug'])
        for r in c.execute("SELECT t.slug, count(*) AS n FROM library.claim cl JOIN library.topic t ON t.id=cl.topic_id WHERE cl.collection_id=%s AND cl.status='active' AND t.status='active' "
                           "AND NOT EXISTS (SELECT 1 FROM library.claim_source cs WHERE cs.claim_id=cl.id AND cs.source_id IS NOT NULL) GROUP BY t.slug ORDER BY t.slug", (cid,)).fetchall():
            add('warn', 'uncited_claims', f"{r['n']} active claim(s) of '{r['slug']}' cite no source.", topic=r['slug'])
        for r in c.execute("SELECT id, name FROM library.source WHERE collection_id=%s AND wiki_status='needs_review' ORDER BY name", (cid,)).fetchall():
            add('warn', 'page_needs_review', f"The page written from '{r['name']}' still has objections from the checker.", source=str(r['id']))
        for r in c.execute("SELECT id, name, error FROM library.source WHERE collection_id=%s AND status='failed' ORDER BY name", (cid,)).fetchall():
            add('warn', 'source_failed', f"'{r['name']}' could not be read: {r['error']}", source=str(r['id']))
        for r in c.execute("SELECT id, name FROM library.source WHERE collection_id=%s AND wiki_status='approved' AND NOT (stats ? 'topics') ORDER BY name", (cid,)).fetchall():
            add('info', 'no_topics_yet', f"'{r['name']}' has an approved page but has not been turned into topics.", source=str(r['id']))
        for r in c.execute("SELECT t.slug, t.title FROM library.topic t WHERE t.collection_id=%s AND t.status='active' AND NOT EXISTS (SELECT 1 FROM library.topic_link l WHERE l.from_topic=t.id OR l.to_topic=t.id) ORDER BY t.title", (cid,)).fetchall():
            add('info', 'isolated_topic', f"'{r['title']}' is not linked to any other topic.", topic=r['slug'])
        for r in c.execute("SELECT f.title AS a, f.slug AS sa, t.title AS b FROM library.topic_link l JOIN library.topic f ON f.id=l.from_topic JOIN library.topic t ON t.id=l.to_topic WHERE l.collection_id=%s AND t.status<>'active' AND f.status='active'", (cid,)).fetchall():
            add('warn', 'broken_link', f"'{r['a']}' links to '{r['b']}', which is no longer a topic.", topic=r['sa'])
        # a statement whose quote is no longer on its source's page (the page was edited or written again)
        for r in c.execute("SELECT t.slug, cl.n, cs.quote, s.name, s.id AS sid, (SELECT markdown FROM library.wiki_version w WHERE w.source_id=s.id ORDER BY version DESC LIMIT 1) AS page "
                           "FROM library.claim cl JOIN library.topic t ON t.id=cl.topic_id JOIN library.claim_source cs ON cs.claim_id=cl.id JOIN library.source s ON s.id=cs.source_id AND s.collection_id=cs.collection_id "
                           "WHERE cl.collection_id=%s AND cl.status='active' AND t.status='active'", (cid,)).fetchall():
            if r['page'] is None or topics.norm(r['quote']) not in topics.norm(r['page']):
                add('warn', 'quote_missing', f"Statement {r['n']} of '{r['slug']}' quotes '{r['name']}', whose page no longer says it.", topic=r['slug'], source=str(r['sid']))
        sp = vectors.spec(config.EMBEDDING_DIM)
        try:
            for r in c.execute(f"SELECT a.slug AS sa, a.title AS ta, b.slug AS sb, b.title AS tb, 1 - (a.embedding <=> b.embedding) AS sim FROM library.topic a JOIN library.topic b ON a.collection_id=b.collection_id AND a.id<b.id "
                               "WHERE a.collection_id=%s AND a.status='active' AND b.status='active' AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL AND 1 - (a.embedding <=> b.embedding) >= 0.88 "
                               "ORDER BY sim DESC LIMIT 20", (cid,)).fetchall():
                add('info', 'similar_topics', f"'{r['ta']}' and '{r['tb']}' are {r['sim']:.0%} similar: they may be one subject (a topic can be merged into another).", topic=r['sa'], other=r['sb'])
        except Exception:
            pass
        c.execute('INSERT INTO library.health_run (collection_id, by, findings) VALUES (%s,%s,%s)', (cid, user, J(out)))
        c.execute('DELETE FROM library.health_run WHERE collection_id=%s AND id NOT IN (SELECT id FROM library.health_run WHERE collection_id=%s ORDER BY id DESC LIMIT 20)', (cid, cid))
    return out
