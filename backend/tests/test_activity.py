import time

from studio import access, activity, db, tools


def _wait(sid, n, tries=40):
    for _ in range(tries):
        with db.conn() as c:
            k = c.execute('SELECT count(*) AS n FROM activity_event WHERE session_id=%s', (sid,)).fetchone()['n']
        if k >= n:
            return k
        time.sleep(0.1)
    return k


def test_tool_calls_are_logged_in_their_session(client):
    access.set_caller('t', None)
    sid = activity.session('alfred', 't', None, title='what is a customer')
    activity.log('user', text='what is a customer')
    tools.call('describe', {'name': 'customer'})
    tools.call('describe', {'name': 'no_such_thing_xyz'})
    assert _wait(sid, 3) == 3
    with db.conn() as c:
        ev = c.execute('SELECT kind,name,ok,detail FROM activity_event WHERE session_id=%s ORDER BY id', (sid,)).fetchall()
        s = c.execute('SELECT source,actor,title FROM activity_session WHERE id=%s', (sid,)).fetchone()
    assert [e['kind'] for e in ev] == ['user', 'tool', 'tool'] and s['title'] == 'what is a customer'
    assert ev[1]['name'] == 'describe' and ev[1]['detail']['args'] == {'name': 'customer'} and ev[1]['detail']['chars'] > 0


def test_mcp_calls_by_one_token_share_a_session_until_idle():
    a = activity.mcp_session('tok-x')
    assert activity.mcp_session('tok-x') == a
    activity._mcp['tok-x'] = (a, time.time() - activity.IDLE_S - 1)
    assert activity.mcp_session('tok-x') != a


def test_retention_follows_each_step_not_the_session(dbmod):
    """A chat still in use keeps its recent steps and loses the ones older than ACTIVITY_DAYS."""
    from studio import config
    with db.conn() as c:
        c.execute("INSERT INTO activity_session (id,source,actor,title,started_at) VALUES ('ret-live','alfred','t','old chat', now() - interval '90 days')")
        c.execute("INSERT INTO activity_event (session_id,kind,at,detail) VALUES ('ret-live','user', now() - make_interval(days => %s), '{\"text\": \"old\"}'), "
                  "('ret-live','user', now(), '{\"text\": \"new\"}')", (config.ACTIVITY_DAYS + 5,))
        c.execute("INSERT INTO activity_session (id,source,actor,started_at,last_at) VALUES ('ret-dead','mcp','t', now() - interval '90 days', now() - interval '90 days')")
    activity.purge()
    with db.conn() as c:
        left = [r['t'] for r in c.execute("SELECT detail->>'text' AS t FROM activity_event WHERE session_id='ret-live'").fetchall()]
        gone = c.execute("SELECT count(*) AS n FROM activity_session WHERE id='ret-dead'").fetchone()['n']
        c.execute("DELETE FROM activity_session WHERE id IN ('ret-live','ret-dead')")
    assert left == ['new'] and gone == 0
