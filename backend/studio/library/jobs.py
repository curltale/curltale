"""Background work for the Library: a queue in the database, claimed with SKIP LOCKED so any number of workers (threads in the web process, or
separate containers) can share it. A running job holds a lease that a heartbeat renews; if a worker dies the lease runs out and the job is
queued again. A failed job is retried with a growing pause, then marked failed with its error. Payloads hold ids only, never document text."""
import hashlib
import threading
import time
import traceback
import uuid
from datetime import timedelta
from .. import activity, settings
from ..db import conn, J

LEASE_S = 120
HEARTBEAT_S = 30
HANDLERS = {}


def handler(kind):
    """Register the function that runs jobs of this kind: fn(job, ctx)."""
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


class Cancelled(Exception):
    pass


class Permanent(Exception):
    """A failure that trying again cannot fix (the file is unreadable): the job fails at once."""


def enqueue(kind, collection_id, source_id=None, payload=None, user=None, max_attempts=3):
    with conn() as c:
        return c.execute('INSERT INTO library.job (kind,collection_id,source_id,payload,created_by,max_attempts) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id',
                         (kind, collection_id, source_id, J(payload or {}), user, max_attempts)).fetchone()['id']


def claim(worker):
    """The oldest due job, now marked running and leased to this worker; or None."""
    with conn() as c:
        row = c.execute("SELECT id FROM library.job WHERE status='queued' AND run_after <= now() ORDER BY run_after, id FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
        if not row:
            return None
        return c.execute("UPDATE library.job SET status='running', worker=%s, attempts=attempts+1, started_at=coalesce(started_at, now()), lease_until=now() + %s, error=NULL "
                         "WHERE id=%s RETURNING *", (worker, timedelta(seconds=LEASE_S), row['id'])).fetchone()


def reap():
    """Jobs whose worker vanished (lease expired) go back to the queue, or fail when they have used their attempts."""
    with conn() as c:
        c.execute("UPDATE library.job SET status=CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END, worker=NULL, lease_until=NULL, "
                  "error='the worker stopped before finishing', finished_at=CASE WHEN attempts >= max_attempts THEN now() END "
                  "WHERE status='running' AND lease_until < now()")


class Ctx:
    """What a handler gets: progress reporting and a cancellation check. Both renew the lease."""

    def __init__(self, job_id):
        self.id = job_id
        self.step = None

    def progress(self, step=None, pct=None, message=None):
        if step and step != self.step:
            self.step = step
            activity.log('step', step, **({'message': message} if message else {}))          # the timeline of a document in the activity log
        data = {k: v for k, v in (('step', step), ('pct', pct), ('message', message)) if v is not None}
        with conn() as c:
            r = c.execute("UPDATE library.job SET progress = progress || %s::jsonb, lease_until = now() + %s WHERE id=%s RETURNING status", (J(data), timedelta(seconds=LEASE_S), self.id)).fetchone()
        if r and r['status'] == 'cancelled':
            raise Cancelled()

    def check(self):
        self.progress()


def _heartbeat(job_id, stop):
    while not stop.wait(HEARTBEAT_S):
        try:
            with conn() as c:
                c.execute("UPDATE library.job SET lease_until = now() + %s WHERE id=%s AND status='running'", (timedelta(seconds=LEASE_S), job_id))
        except Exception:
            pass


def run_one(worker):
    """Claim and run a single job. Returns the job id handled, or None when the queue is empty."""
    job = claim(worker)
    if not job:
        return None
    stop = threading.Event()
    threading.Thread(target=_heartbeat, args=(job['id'], stop), daemon=True).start()
    ref = str(job['source_id'] or job['id'])                         # one activity session per document, so retries continue the same timeline; ids only, no file names
    activity.session('library', job['created_by'] or 'system', hashlib.sha1(ref.encode()).hexdigest()[:16], title=f"{job['kind']} {ref[:8]}")
    t0 = time.perf_counter()
    try:
        fn = HANDLERS.get(job['kind'])
        if fn is None:
            raise RuntimeError(f"no handler for job kind '{job['kind']}'")
        fn(job, Ctx(job['id']))
        _finish(job['id'], 'done')
        activity.log('job', job['kind'], ms=(time.perf_counter() - t0) * 1000, attempt=job['attempts'], collection=str(job['collection_id']))
    except Cancelled:
        _finish(job['id'], 'cancelled')
        activity.log('job', job['kind'], ms=(time.perf_counter() - t0) * 1000, ok=False, attempt=job['attempts'], collection=str(job['collection_id']), error='cancelled')
    except Exception as e:
        _fail(job, f'{type(e).__name__}: {e}'[:500], permanent=isinstance(e, Permanent))
        activity.log('job', job['kind'], ms=(time.perf_counter() - t0) * 1000, ok=False, attempt=job['attempts'], collection=str(job['collection_id']), error=f'{type(e).__name__}: {e}'[:300])
        print('job', job['id'], job['kind'], 'failed:', traceback.format_exc(limit=3), flush=True)
    finally:
        stop.set()
        activity.CTX.set(None)
    return job['id']


def _finish(job_id, status):
    with conn() as c:
        c.execute("UPDATE library.job SET status=%s, finished_at=now(), lease_until=NULL, worker=NULL WHERE id=%s AND status='running'", (status, job_id))


def _fail(job, error, permanent=False):
    retry = job['attempts'] < job['max_attempts'] and not permanent
    with conn() as c:
        c.execute("UPDATE library.job SET status=%s, error=%s, worker=NULL, lease_until=NULL, run_after=now() + %s, finished_at=CASE WHEN %s THEN NULL ELSE now() END "
                  "WHERE id=%s AND status='running'", ('queued' if retry else 'failed', error, timedelta(seconds=30 * job['attempts']), retry, job['id']))


def cancel(job_id, collection_id):
    with conn() as c:
        c.execute("UPDATE library.job SET status='cancelled', finished_at=now() WHERE id=%s AND collection_id=%s AND status IN ('queued','running')", (job_id, collection_id))


def retry(source_id, collection_id, user=None):
    """Queue the source again after a failure."""
    from . import pipeline  # noqa: F401  (registers the handlers)
    return enqueue('source.ingest', collection_id, source_id, user=user)


def status_of(source_ids):
    """Latest job per source: {source_id: {status, step, pct, message, error, attempts}}"""
    if not source_ids:
        return {}
    with conn() as c:
        rows = c.execute("SELECT DISTINCT ON (source_id) source_id, kind, status, progress, error, attempts FROM library.job WHERE source_id = ANY(%s) ORDER BY source_id, id DESC", (list(source_ids),)).fetchall()
    return {str(r['source_id']): {'kind': r['kind'], 'status': r['status'], **(r['progress'] or {}), 'error': r['error'], 'attempts': r['attempts']} for r in rows}


# ---------------------------------------------------------------- workers
_threads = []
_stop = threading.Event()


def work_loop(name, stop, idle=2.0):
    n = 0
    while not stop.is_set():
        try:
            if n % 15 == 0:
                reap()
            n += 1
            if not settings.get('LIBRARY_ENABLED'):                      # switched off: nothing is claimed, nothing is deleted
                stop.wait(idle)
                continue
            if run_one(name) is None:
                stop.wait(idle)
        except Exception as e:                                         # the database is briefly away, say: keep trying
            print('worker', name, 'error:', e, flush=True)
            stop.wait(5)


def start_workers(count, prefix=None):
    from . import pipeline  # noqa: F401
    prefix = prefix or f'{uuid.uuid4().hex[:6]}'
    for i in range(count):
        t = threading.Thread(target=work_loop, args=(f'{prefix}-{i}', _stop), daemon=True)
        t.start()
        _threads.append(t)


def stop_workers():
    _stop.set()
