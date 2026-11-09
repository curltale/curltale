import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, q } from '../api.js'
import { useStudio } from '../context.jsx'
import GraphView from '../components/GraphView.jsx'

export default function GraphPage() {
  const { version, status } = useStudio()
  const [g, setG] = useState(null), [docs, setDocs] = useState(false)
  const nav = useNavigate()
  useEffect(() => { api.get('/api/graph?' + q({ version, documents: docs ? 'true' : '' })).then(setG) }, [version, docs])
  return (
    <>
      <h1>Entity graph</h1>
      <p className="muted">Hover an entity to see its neighbours; click to open it.</p>
      {status?.library && <label className="small" style={{ display: 'block', marginBottom: 8 }}><input type="checkbox" className="switch" style={{ width: 'auto', marginRight: 6 }} checked={docs} onChange={(e) => setDocs(e.target.checked)} />Show the topics of my document collections that are about these entities</label>}
      {g ? <GraphView nodes={g.nodes} edges={g.edges} onNode={(n) => nav(n.kind === 'topic' ? `/library/topics/${n.slug}?c=${n.collection}` : '/entities/' + n.key)} /> : <p className="muted">Loading…</p>}
    </>
  )
}
