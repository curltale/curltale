import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import { Pill } from '../components/ui.jsx'

const STATUS = { active: ['In progress', 'accent'], draft_ready: ['Draft ready', 'warn'], ingesting: ['Being added', ''], ingested: ['Added', 'ok'] }

export default function InterviewsPage() {
  const [params, setParams] = useSearchParams()
  const nav = useNavigate()
  const [cols, setCols] = useState(null), [data, setData] = useState(null), [seed, setSeed] = useState(''), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const cid = params.get('c')
  useEffect(() => { api.get('/api/library/collections').then((d) => { const mine = d.collections.filter((c) => c.level === 'write' || c.level === 'manage'); setCols(mine); if (!cid && mine[0]) setParams({ c: mine[0].id }, { replace: true }) }).catch((e) => setErr(e.message)) }, [])
  const load = () => cid && api.get(`/api/library/collections/${cid}/interviews`).then((d) => { setData(d); setErr('') }).catch((e) => setErr(e.message))
  useEffect(() => { setData(null); load() }, [cid])
  const start = async (e) => {
    e.preventDefault(); setBusy(true)
    try { const r = await api.post(`/api/library/collections/${cid}/interviews`, { seed }); nav(`/library/interview/${r.id}?c=${cid}`) } catch (x) { setErr(x.message) }
    setBusy(false)
  }
  return (
    <>
      <div className="row" style={{ marginBottom: 14 }}>
        <div><h1>Interviews</h1><div className="muted">Put what you know into words by being asked questions, one at a time. The answers become facts, then a document, then topics in the collection.</div></div>
        {cols?.length > 0 && <select className="right" style={{ width: 'auto' }} value={cid || ''} onChange={(e) => setParams({ c: e.target.value })}>{cols.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select>}
      </div>
      {cols && !cols.length && <p className="muted">You need write access to a collection (and the editor role) to hold an interview.</p>}
      {err && <div className="banner bad">{err}</div>}
      {data && !data.collection.allow_ai && <div className="banner">This collection does not allow AI to read its documents, so an interview (which needs a model) is not available here.</div>}
      {data?.collection.allow_ai && <form className="card" onSubmit={start} style={{ marginBottom: 18 }}>
        <label className="small muted" htmlFor="seed">What do you want to document? Say it in your own words: a process, a decision, how something really works.</label>
        <textarea id="seed" rows={3} value={seed} onChange={(e) => setSeed(e.target.value)} placeholder="e.g. How we decide whether a late refund request is accepted" style={{ marginTop: 6 }} />
        <div className="row" style={{ marginTop: 8, justifyContent: 'flex-end' }}><span className="small muted">The conversation is sent to the AI model set for this collection.</span><button className="btn primary" disabled={busy || !seed.trim()}>Start the interview</button></div>
      </form>}
      {data && <div className="card">
        <strong>Interviews in this collection</strong>
        {data.interviews.map((i) => (
          <Link key={i.id} to={`/library/interview/${i.id}?c=${cid}`} style={{ display: 'block', color: 'inherit', textDecoration: 'none', padding: '12px 0', borderTop: '1px solid var(--line)' }}>
            <div className="row"><b>{i.seed.length > 110 ? i.seed.slice(0, 110) + '…' : i.seed}</b><span className="right"><Pill tone={STATUS[i.status][1]}>{STATUS[i.status][0]}</Pill></span></div>
            <div className="small muted">{i.turns} messages · {i.claims} facts · {i.created_by} · {new Date(i.created_at).toLocaleDateString()}</div></Link>))}
        {!data.interviews.length && <p className="muted" style={{ margin: '10px 0 0' }}>No interviews yet.</p>}
      </div>}
    </>
  )
}
