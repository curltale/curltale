import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api, q } from '../api.js'
import { useStudio } from '../context.jsx'
import GraphView from '../components/GraphView.jsx'
import { Pill, StatusPill } from '../components/ui.jsx'

function Hops({ title, p }) {
  if (!p) return null
  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <div className="row"><b>{title}</b><Pill tone={p.all_confirmed ? 'ok' : 'warn'}>{p.all_confirmed ? 'all links confirmed' : 'uses unconfirmed links'}</Pill>{p.fan_out && <Pill tone="bad">fan-out: joins siblings via a shared parent</Pill>}<span className="muted small">cost {p.cost}</span></div>
      {p.hops.map((h, i) => (
        <div key={i} style={{ padding: '8px 0', borderTop: i ? '1px solid var(--line)' : 'none' }}>
          <div><Link to={'/entities/' + h.from}>{h.from_name}</Link> → <Link to={'/entities/' + h.to}>{h.to_name}</Link> <span className="muted">via {h.predicate} ({h.followed}, {h.cardinality})</span> <StatusPill s={h.status} /></div>
          <div className="mono small">{h.join}</div>{h.warning && <div className="small" style={{ color: 'var(--warn)' }}>{h.warning}</div>}
        </div>))}
    </div>
  )
}

export default function PathPage() {
  const { version } = useStudio()
  const [sp, setSp] = useSearchParams()
  const [a, setA] = useState(sp.get('a') || ''), [b, setB] = useState(sp.get('b') || '')
  const [res, setRes] = useState(null), [g, setG] = useState(null), [names, setNames] = useState([]), [err, setErr] = useState('')
  const nav = useNavigate()
  useEffect(() => { api.get('/api/graph?' + q({ version })).then((x) => { setG(x); setNames(x.nodes.map((n) => n.name)) }) }, [version])
  const go = async () => { setErr(''); try { const r = await api.get('/api/path?' + q({ a, b, version })); setRes(r); setSp({ a, b }) } catch (e) { setErr(e.message) } }
  useEffect(() => { if (sp.get('a') && sp.get('b')) go() }, [version])
  const hl = res?.best ? [res.from, ...res.best.hops.map((h) => h.to)] : []
  return (
    <>
      <h1>Join paths</h1><p className="muted">How two entities connect, preferring confirmed links — the same logic external agents get from <code>join_path</code>.</p>
      <datalist id="ents">{names.map((n) => <option key={n} value={n} />)}</datalist>
      <div className="row" style={{ marginBottom: 14 }}>
        <input list="ents" style={{ width: 240 }} value={a} onChange={(e) => setA(e.target.value)} /><span>→</span>
        <input list="ents" style={{ width: 240 }} value={b} onChange={(e) => setB(e.target.value)} /><button className="btn primary" onClick={go}>Find path</button>
      </div>
      {err && <div className="banner bad">{err}</div>}
      {res?.error && <div className="banner bad">{res.error}</div>}
      {res && !res.error && <>
        {res.best ? <Hops title="Preferred path" p={res.best} /> : <div className="banner">The model has no join path between these entities.</div>}
        {res.note && <div className="banner">{res.note}</div>}
        <Hops title="Confirmed-only alternative" p={res.confirmed_only} />
        <Hops title="Approximate: confirmed but via a shared parent" p={res.approximate_via_shared_parent} />
        {g && <GraphView nodes={g.nodes} edges={g.edges} highlight={hl} height={380} onNode={(n) => nav('/entities/' + n.key)} />}
      </>}
    </>
  )
}
