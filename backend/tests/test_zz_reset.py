import os

import pytest

from studio import api, datastore, editing, excel_io, indexer, ontology, tools, versions


@pytest.fixture
def backup_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(versions, 'BACKUP_DIR', str(tmp_path))
    return tmp_path


def test_emptying_the_ontology_keeps_a_backup_and_leaves_the_app_standing(client, backup_dir):
    model = ontology.assemble(ontology.published_id())
    try:
        r = versions.reset_all('tester', remove_demo=False)
        assert r['versions_deleted'] >= 1 and r['backup'] and (backup_dir / r['backup']).stat().st_size > 10_000
        assert ontology.published_id() is None and ontology.draft_id() is None
        # nothing published: every tool answers with a clean error instead of crashing
        out = tools.call('search', {'query': 'customer'})
        assert 'error' in out
        with pytest.raises(datastore.DataError, match='no published ontology'):
            datastore.current_catalog()
        # the backup is the way back: reading it gives the same model
        back, rep = excel_io.read_workbook((backup_dir / r['backup']).read_bytes())
        assert not rep.errors and set(back['entities']) == set(model['entities'])
        # the first draft comes from an upload, and can be published without any previous version
        out = versions.import_excel((backup_dir / r['backup']).read_bytes(), 'tester', dry_run=False)
        assert out['created'] and not out['errors']
    finally:
        with __import__('studio.db', fromlist=['conn']).conn() as c:                     # leave the shared test database as other tests expect it
            c.execute('DELETE FROM ontology_version')
        vid = ontology.import_model(model, 'restored', publish=True)
        indexer.reindex(vid, embed=False)
        tools.invalidate()
