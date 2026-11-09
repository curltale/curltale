import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import { Pill } from '../components/ui.jsx'

export default function HealthPage() {
  const [params, setParams] = useSearchParams()
  const [cols, setCols] = useState(null), [run, setRun] = useState(undefined), [level, setLevel] = useState(null), [busy, setBusy] = useState(false), [err, setErr] = useState('')
  const cid = params.get('c')
  useEffect(() => { api.get('/api/library/collections').then((d) => { const mine = d.collections.filter((c) => c.level); setCols(mine); if (!cid && mine[0]) setParams({ c: mine[0].id }, { replace: true }) }).catch((e) => setErr(e.message)) }, [])
  useEffect(() => { if (cid) { setRun(undefined); api.get(`/api/library/collections/${cid}/health`).then((d) => { setRun(d.run); setLevel(d.collection.level) }).catch((e) => setErr(e.message)) } }, [cid])
  const check = async () => { setBusy(true); try { await api.post(`/api/library/collections/${cid}/health`); const d = await api.get(`/api/library/collections/${cid}/health`); setRun(d.run); setErr('') } catch (e) { setErr(e.message) } setBusy(false) }
  const link = (f) => f.conflict ? `/library/review/${f.conflict}?c=${cid}` : f.topic ? `/library/topics/${f.topic}?c=${cid}` : f.source ? `/library/sources/${f.source}?c=${cid}` : null
  const f = run?.findings || []
  return (
    <>
      <div className="row" style={{ marginBottom: 14 }}>
        <div><h1>Health</h1><div className="muted">What in a collection's wiki is stale, unsupported, contradictory or lonely. Checked without any AI model.</div></div>
        {cols?.length > 0 && <select className="right" style={{ width: 'auto' }} value={cid || ''} onChange={(e) => setParams({ c: e.target.value })}>{cols.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select>}
      </div>
      {err && <div className="banner bad">{err}</div>}
      {(level === 'write' || level === 'manage') && <button className="btn primary" disabled={busy} onClick={check}>{busy ? 'Checking…' : 'Check now'}</button>}
      {run === null && <p className="muted">It has not been checked yet.</p>}
      {run && <p className="small muted">Checked {new Date(run.at).toLocaleString()} by {run.by}. {f.length ? `${f.length} finding${f.length === 1 ? '' : 's'}.` : 'Nothing to report.'}</p>}
      {f.length > 0 && <div className="card">{f.map((x, i) => (
        <div key={i} className="row" style={{ padding: '10px 0', borderTop: i ? '1px solid var(--line)' : 0, alignItems: 'baseline' }}>
          <Pill tone={x.severity === 'warn' ? 'warn' : ''}>{x.kind.replace(/_/g, ' ')}</Pill><span style={{ flex: 1, minWidth: 220 }}>{x.message}</span>
          {link(x) && <Link className="btn small" to={link(x)}>Open</Link>}</div>))}</div>}
    </>
  )
}
