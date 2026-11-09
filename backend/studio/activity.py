"""Activity log: sessions and the events in them (questions, model calls, tool calls, answers). Writes go through a queue and never slow or break a request."""
import queue
import threading
import time
import uuid
from contextvars import ContextVar

from . import config, settings
from .db import J, conn

CTX: ContextVar = ContextVar('activity', default=None)        # {'session': id, 'source': 'alfred' | 'mcp', 'actor': name}
IDLE_S = 30 * 60                                              # an MCP token idle for this long starts a new session
_q: queue.Queue = queue.Queue()
_mcp: dict = {}                                               # token name -> (session id, last seen)
_lock = threading.Lock()
_started = False


def _writer():
    while True:
        item = _q.get()
        try:
            with conn() as c:
                if item[0] == 'session':
                    _, sid, source, actor, title = item
                    c.execute('INSERT INTO activity_session (id,source,actor,title) VALUES (%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET last_at=now(), '
                              'title=coalesce(activity_session.title, EXCLUDED.title)', (sid, source, actor, title))
                else:
                    _, sid, kind, name, ms, ok, tin, tout, cost, detail = item
                    c.execute('INSERT INTO activity_event (session_id,kind,name,ms,ok,tokens_in,tokens_out,cost,detail) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                              (sid, kind, name, ms, ok, tin, tout, cost, J(detail) if detail is not None else None))
                    c.execute('UPDATE activity_session SET last_at=now() WHERE id=%s', (sid,))
        except Exception as e:                                # logging must never take the app down
            print('activity log write failed:', repr(e)[:200])


def _start():
    global _started
    with _lock:
        if not _started:
            threading.Thread(target=_writer, daemon=True).start()
            _started = True


def session(source, actor, session_id=None, title=None):
    """Open (or continue) a session and make it the current one for this task/thread. Returns its id."""
    _start()
    sid = session_id or uuid.uuid4().hex[:16]
    CTX.set({'session': sid, 'source': source, 'actor': actor})
    _q.put(('session', sid, source, actor, (title or '')[:200] or None))
    return sid


def mcp_session(actor):
    """MCP is stateless: calls by one token with gaps shorter than IDLE_S belong to one session."""
    now = time.time()
    with _lock:
        sid, seen = _mcp.get(actor, (None, 0))
        fresh = not sid or now - seen > IDLE_S
        if fresh:
            sid = uuid.uuid4().hex[:16]
        _mcp[actor] = (sid, now)
    if fresh:
        session('mcp', actor, sid)
    else:
        CTX.set({'session': sid, 'source': 'mcp', 'actor': actor})
    return sid


def log(kind, name=None, ms=None, ok=True, tokens_in=None, tokens_out=None, cost=None, **detail):
    ctx = CTX.get()
    if not ctx:
        return
    _start()
    _q.put(('event', ctx['session'], kind, name, None if ms is None else int(ms), ok, tokens_in, tokens_out, cost, detail or None))


def purge():
    """Retention by each step's own time: a chat that is still in use loses its oldest steps on schedule; a session with nothing left goes too."""
    with conn() as c:
        c.execute("DELETE FROM activity_event WHERE at < now() - make_interval(days => %s)", (settings.get('ACTIVITY_DAYS'),))
        c.execute("DELETE FROM activity_session s WHERE s.last_at < now() - make_interval(days => %s) "
                  "OR (s.started_at < now() - interval '1 hour' AND NOT EXISTS (SELECT 1 FROM activity_event e WHERE e.session_id = s.id))", (settings.get('ACTIVITY_DAYS'),))


def purge_every(seconds=3600):
    """Keep purging while the app runs (a server that stays up for months must still forget on time)."""
    def loop():
        while True:
            try:
                purge()
            except Exception as e:
                print('activity purge failed:', repr(e)[:150])
            time.sleep(seconds)
    threading.Thread(target=loop, daemon=True).start()
