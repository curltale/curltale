import time

import pytest

from studio import config, db, editing, ontology, versions


def test_publishing_runs_in_the_background_and_reports_progress(client):
    before = ontology.published_id()
    versions.create_draft('job test')
    start = versions.publish_start('tester')
    assert start['state'] == 'running' and start['version'] == ontology.draft_id()
    with pytest.raises(editing.EditError, match='already running'):
        versions.publish_start('tester')
    for _ in range(200):
        job = versions.publish_job()
        if job['state'] != 'running':
            break
        time.sleep(0.1)
    assert job['state'] == 'done' and job['result']['version'] == start['version']
    assert ontology.published_id() == start['version'] != before and ontology.draft_id() is None


def test_a_draft_with_problems_is_refused_at_once_not_in_the_background(client):
    versions.create_draft('bad draft')
    with db.conn() as c:
        c.execute("UPDATE example SET extra='{}'::jsonb WHERE id = (SELECT id FROM example WHERE version_id=%s LIMIT 1) AND version_id=%s",
                  (ontology.draft_id(),) * 2)
    try:
        with pytest.raises(editing.EditError, match='cannot publish'):
            versions.publish_start('tester')
    finally:
        versions.discard_draft()


def test_embeddings_outlive_their_version(client):
    from studio import indexer
    vid = ontology.published_id()
    with db.conn() as c:
        c.execute('DELETE FROM embedding_cache')
        # pretend one chunk was embedded, remember it, delete the chunks: the cache still has it
        c.execute("UPDATE chunk SET embedding = (SELECT ('[' || array_to_string(array_fill(0.1::float8, ARRAY[%d]), ',') || ']')::vector) WHERE version_id=%s AND id = (SELECT id FROM chunk WHERE version_id=%s LIMIT 1)" % (config.EMBEDDING_DIM, '%s', '%s'), (vid, vid))
    indexer.remember(vid)
    with db.conn() as c:
        assert c.execute('SELECT count(*) AS n FROM embedding_cache').fetchone()['n'] == 1
        c.execute('DELETE FROM chunk')
    indexer.reindex(vid, embed=False)
    with db.conn() as c:
        assert c.execute('SELECT count(*) AS n FROM chunk WHERE version_id=%s AND embedding IS NOT NULL', (vid,)).fetchone()['n'] >= 1
