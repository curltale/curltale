"""Build the synthetic database in the background and keep the catalog of what was built."""
import json
import threading
import traceback

from . import conformance, datastore
from .db import conn, J
from .ontology import assemble, published_id
from .synth import load

_progress = {'message': '', 'running': False}
_catalogs = {}
_lock = threading.Lock()


def progress():
    return dict(_progress)


def latest():
    with conn() as c:
        return c.execute("SELECT id,started_at,finished_at,status,scale,seed,version_id,message,stats,findings,started_by FROM data_build ORDER BY id DESC LIMIT 1").fetchone()


def last_ok():
    with conn() as c:
        return c.execute("SELECT id,started_at,finished_at,status,scale,seed,version_id,message,stats,findings,started_by FROM data_build WHERE status='ok' ORDER BY id DESC LIMIT 1").fetchone()


def catalog():
    """Catalog of the model version the database was built from (not necessarily the published one)."""
    b = last_ok()
    if not b:
        return None, None
    if b['version_id'] not in _catalogs:
        _catalogs.clear()
        _catalogs[b['version_id']] = datastore.Catalog(assemble(b['version_id']))
    return _catalogs[b['version_id']], b


def start(scale, seed, user, ai=False):
    with _lock:
        if _progress['running']:
            raise RuntimeError('a build is already running')
        _progress.update(running=True, message='starting')
    vid = published_id()
    if not vid:
        with _lock:
            _progress.update(running=False, message='')
        raise RuntimeError('there is no published ontology to build the demo database from: publish one first')
    with conn() as c:
        bid = c.execute("INSERT INTO data_build (scale,seed,version_id,started_by) VALUES (%s,%s,%s,%s) RETURNING id", (scale, seed, vid, user)).fetchone()['id']
    threading.Thread(target=_run, args=(bid, vid, scale, seed, ai), daemon=True).start()
    return bid


def _run(bid, vid, scale, seed, ai=False):
    try:
        onto = assemble(vid)
        res = load.build(onto, seed=seed, scale=scale, progress=lambda m: _progress.update(message=m), ai=ai)
        datastore.reset_pools()
        _progress.update(message='checking the model against the data')
        cat = datastore.Catalog(onto)
        findings = res.pop('findings', [])
        checks = conformance.run_all(cat, findings)
        with conn() as c:
            c.execute("UPDATE data_build SET status='ok', finished_at=now(), stats=%s, findings=%s, checks=%s, message=%s WHERE id=%s",
                      (J(res), J(findings), J(checks), f"{res['rows']} rows in {res['seconds']}s", bid))
        _catalogs.clear()
    except Exception as ex:
        traceback.print_exc()
        with conn() as c:
            c.execute("UPDATE data_build SET status='failed', finished_at=now(), message=%s WHERE id=%s", (str(ex)[:2000], bid))
    finally:
        _progress.update(running=False, message='')
