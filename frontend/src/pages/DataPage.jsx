import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useStudio } from '../context.jsx'
import { Pill } from '../components/ui.jsx'

const ago = (t) => (t ? new Date(t).toLocaleString() : '')

export default function DataPage() {
  const { user } = useStudio()
  const [st, setSt] = useState(null), [tables, setTables] = useState(null), [err, setErr] = useState(''), [scale, setScale] = useState('medium'), [seed, setSeed] = useState(7), [filter, setFilter] = useState(''), [busy, setBusy] = useState(false), [done, setDone] = useState(null), [steps, setSteps] = useState([]), [ai, setAi] = useState(false)
  const load = async () => {
    const s = await api.get('/api/data/status'); setSt(s)
    if (s.exists) api.get('/api/data/tables').then((d) => setTables(d.tables)).catch((e) => setErr(e.message))
    return s
  }
  const polling = useRef(false)
  // one loop that follows the build until the SERVER says it is over (not tied to render cycles, and it also resumes if the page is opened mid-build)
  const watch = async () => {
    if (polling.current) return
    polling.current = true
    setBusy(true)
    let s
    try {
      for (;;) {
        s = await load()
        const m = s.progress.message
        if (m) setSteps((l) => (l[l.length - 1] === m ? l : [...l, m]))
        if (!s.progress.running) break
        await new Promise((r) => setTimeout(r, 800))
      }
      setDone(s.latest?.status === 'failed' ? { bad: true, text: s.latest.message } : { text: s.latest?.message })
    } catch (e) { setErr(e.message) }
    polling.current = false
    setBusy(false)
  }
  useEffect(() => { load().then((s) => { if (s.progress.running) watch() }).catch((e) => setErr(e.message)) }, [])
  const start = async () => {
    setErr(''); setDone(null); setSteps([]); setBusy(true)
    try { await api.post('/api/admin/data/build', { scale, seed, ai }); watch() } catch (e) { setErr(e.message); setBusy(false) }
  }

  if (!st) return <p className="muted">Loading…</p>
  const build = st.build, running = st.progress.running || busy, failed = st.latest?.status === 'failed' && (!build || st.latest.id > build.id)
  const groups = {}
  ;(tables || []).filter((t) => !filter || (t.table + t.name).toLowerCase().includes(filter.toLowerCase())).forEach((t) => (groups[t.table.split('.')[0]] ||= []).push(t))

  return (
    <>
      <div className="row"><div><h1>Synthetic database</h1>
        <p className="muted" style={{ margin: 0 }}>The database behind the model: a demo database generated from whatever model is published, or the active connector. Browse it, query it, and check that the model holds on data.</p></div></div>
      {failed && <div className="banner bad" style={{ marginTop: 12, whiteSpace: 'pre-wrap' }}>The last build failed: {st.latest.message}</div>}
      {running && <div className="banner" style={{ marginTop: 12 }}><b>Building…</b> {st.progress.message || 'starting'}{steps.length > 0 && <div className="small muted" style={{ marginTop: 4 }}>{steps.map((m, i) => <div key={i}>{i === steps.length - 1 ? '▸' : '✓'} {m}</div>)}</div>}</div>}
      {!running && done && !done.bad && <div className="banner" style={{ marginTop: 12 }}>Rebuilt: {done.text}. The tables below are the new data.</div>}
      {!running && done?.bad && <div className="banner bad" style={{ marginTop: 12 }}>The build failed: {done.text}</div>}
      {st.stale && <div className="banner" style={{ marginTop: 12 }}>The database was built from an earlier version of the model; a newer one is published. Rebuild to refresh it.</div>}

      {st.connector?.coverage && st.connector.coverage.percent < 100 && <div className="banner bad" style={{ marginTop: 12 }}>This database holds {st.connector.coverage.found} of the {st.connector.coverage.total} tables the published model describes (missing e.g. {st.connector.coverage.missing.slice(0, 4).join(', ')}). Queries on the missing tables fail. {user.role === 'admin' && <Link to="/settings/connectors">Connectors</Link>}</div>}
      {st.connector && <div className="card" style={{ marginTop: 14 }}>
        <div className="row"><b>Connected to {st.connector.name}</b><Pill tone="ok">{st.connector.kind}</Pill><span className="mono small muted">{st.connector.target}</span>
          <div className="right row"><Link className="btn small" to="/data/sql">SQL console</Link><Link className="btn small primary" to="/data/check">Model check</Link>{user.role === 'admin' && <Link className="btn small" to="/settings/connectors">Connectors</Link>}</div></div>
        <p className="small muted" style={{ margin: '8px 0 0' }}>Read-only, through this connector's account. Up to {st.connector.row_limit} rows per query. Agents {st.connector.agents_may_read_rows ? 'may' : 'may not'} read rows. Masked columns show stable hashes; restricted columns are never read.</p></div>}
      {!st.connector && <div className="card" style={{ marginTop: 14 }}>
        {build ? <div className="row"><b>Built {ago(build.finished_at)}</b><Pill>{build.stats.rows.toLocaleString()} rows</Pill><Pill>{Object.keys(build.stats.counts).length} tables</Pill><Pill>{build.scale}</Pill><span className="muted small">seed {build.seed} · {build.stats.seconds}s</span>
          <div className="right row"><Link className="btn small" to="/data/sql">SQL console</Link><Link className="btn small primary" to="/data/check">Model check</Link></div></div>
          : <b>No database yet.</b>}
        {user.role === 'admin' && <p className="small muted" style={{ margin: '6px 0 0' }}>Reading from the built-in demo database · to use your own database, see <Link to="/settings/connectors">Connectors</Link>.</p>}
        {user.role === 'admin' ? (<>
          <div className="row" style={{ marginTop: 10 }}>
            <select style={{ width: 'auto' }} value={scale} onChange={(e) => setScale(e.target.value)} disabled={running}>
              <option value="small">small</option><option value="medium">medium</option><option value="large">large</option></select>
            <input style={{ width: 90 }} type="number" value={seed} onChange={(e) => setSeed(+e.target.value)} title="Same seed, same data" disabled={running} />
            <button className="btn primary" disabled={running} onClick={start}>
              {running ? 'Building…' : build ? 'Rebuild' : 'Build database'}</button>
            <span className="muted small">Rebuilding replaces all data.</span></div>
          <label className="small" style={{ display: 'block', marginTop: 8, opacity: st.ai_available ? 1 : 0.6 }} title={st.ai_available ? '' : 'needs OPENROUTER_API_KEY'}>
            <input type="checkbox" className="switch" style={{ width: 'auto', marginRight: 6 }} disabled={!st.ai_available || running} checked={ai && st.ai_available} onChange={(e) => setAi(e.target.checked)} />
            Realistic values from the AI model: about {st.ai_calls} calls to {st.ai_available ? 'the chat model' : 'the chat model (not configured)'}, one per entity, adding a minute or two. Values are fictional; only the model's names and descriptions are sent.
            Without it, values are generated from types and column names.</label></>
        ) : !build && <p className="muted small">An admin can build it on this page.</p>}
        {err && <div className="banner bad" style={{ marginTop: 8 }}>{err}</div>}
        {build && !st.restricted_visible && <p className="small muted" style={{ margin: '8px 0 0' }}>Restricted columns (identity numbers, credentials and similar) are hidden for you. An admin can grant that entitlement per user.</p>}
      </div>}

      {tables && <>
        <div className="row" style={{ margin: '18px 0 4px' }}><h2 style={{ margin: 0 }}>Tables</h2><input style={{ width: 240 }} className="right" placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} /></div>
        {Object.entries(groups).map(([schema, ts]) => (
          <section key={schema}><h3>{schema} <span className="muted small" style={{ fontWeight: 400 }}>schema</span></h3>
            <table><thead><tr><th>Table</th><th>Entity</th><th style={{ textAlign: 'right' }}>Rows</th><th style={{ textAlign: 'right' }}>Columns</th></tr></thead><tbody>
              {ts.map((t) => <tr key={t.key} className="click"><td className="mono"><Link to={'/data/table?name=' + t.table}>{t.table}</Link></td><td>{t.name} <Pill>{t.kind}</Pill></td>
                <td style={{ textAlign: 'right' }}>{t.capped ? '≥ ' : ''}{t.rows.toLocaleString()}</td><td style={{ textAlign: 'right' }}>{t.columns}</td></tr>)}</tbody></table></section>))}
      </>}
    </>
  )
}
