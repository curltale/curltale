import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import GraphView from '../components/GraphView.jsx'

export default function LibraryGraphPage() {
  const [params, setParams] = useSearchParams()
  const nav = useNavigate()
  const [cols, setCols] = useState(null), [g, setG] = useState(null), [err, setErr] = useState(''), [model, setModel] = useState(false)
  const cid = params.get('c')
  useEffect(() => { api.get('/api/library/collections').then((d) => { const mine = d.collections.filter((c) => c.level); setCols(mine); if (!cid && mine[0]) setParams({ c: mine[0].id }, { replace: true }) }).catch((e) => setErr(e.message)) }, [])
  useEffect(() => { if (cid) { setG(null); api.get(`/api/library/collections/${cid}/graph${model ? '?model=true' : ''}`).then(setG).catch((e) => setErr(e.message)) } }, [cid, model])
  return (
    <>
      <div className="row" style={{ marginBottom: 10 }}>
        <div><h1>Topic graph</h1><p className="muted" style={{ margin: 0 }}>The topics of one collection and how they relate. Colours are topic types. Hover to see neighbours, click to open.</p></div>
        {cols?.length > 0 && <select className="right" style={{ width: 'auto' }} value={cid || ''} onChange={(e) => setParams({ c: e.target.value })}>{cols.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select>}
      </div>
      {err && <div className="banner bad">{err}</div>}
      <label className="small" style={{ display: 'block', marginBottom: 8 }}><input type="checkbox" className="switch" style={{ width: 'auto', marginRight: 6 }} checked={model} onChange={(e) => setModel(e.target.checked)} />Also show the data-model entities these topics are about</label>
      {g && !g.nodes.length && <p className="muted">No topics yet.</p>}
      {g && g.nodes.length > 0 && <GraphView nodes={g.nodes} edges={g.edges} onNode={(n) => nav(n.kind === 'entity' ? `/entities/${n.entity}` : `/library/topics/${n.key}?c=${cid}`)} />}
      {!g && !err && <p className="muted">Loading…</p>}
    </>
  )
}
