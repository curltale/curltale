import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import SafeMarkdown from '../components/SafeMarkdown.jsx'
import { Pill } from '../components/ui.jsx'

const KIND = { contradiction: ['Contradicts what the page says', 'warn'], duplicate_topic: ['Probably the same subject as another topic', 'accent'] }

function Section({ title, hint, count, children }) {
  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="row"><strong>{title}</strong><Pill tone={count ? 'warn' : ''}>{count}</Pill><span className="small muted">{hint}</span></div>
      {count ? children : <p className="muted small" style={{ margin: '8px 0 0' }}>Nothing waiting.</p>}
    </div>
  )
}

export function ReviewList() {
  const [params, setParams] = useSearchParams()
  const [cols, setCols] = useState(null), [d, setD] = useState(null), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const cid = params.get('c'), base = `/api/library/collections/${cid}`
  useEffect(() => { api.get('/api/library/collections').then((x) => { const mine = x.collections.filter((c) => c.level === 'write' || c.level === 'manage'); setCols(mine); if (!cid && mine[0]) setParams({ c: mine[0].id }, { replace: true }) }).catch((e) => setErr(e.message)) }, [])
  const load = () => cid && api.get(`${base}/inbox`).then((x) => { setD(x); setErr('') }).catch((e) => setErr(e.message))
  useEffect(() => { setD(null); load() }, [cid])
  const act = async (fn) => { setBusy(true); try { await fn(); await load() } catch (e) { setErr(e.message) } setBusy(false) }
  const manager = d?.vocabulary.can_decide
  return (
    <>
      <div className="row" style={{ marginBottom: 14 }}>
        <div><h1>Review</h1><div className="muted">What in this collection waits for a person. Everyone who can write in the collection sees the same queue and can decide on its content; the collection's manager also decides the vocabulary.</div></div>
        {cols?.length > 0 && <select className="right" style={{ width: 'auto' }} value={cid || ''} onChange={(e) => setParams({ c: e.target.value })}>{cols.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select>}
      </div>
      {cols && !cols.length && <p className="muted">You need write access to a collection (and the editor role) to review it.</p>}
      {err && <div className="banner bad">{err}</div>}
      {d && <>
        <Section title="Held changes" hint="They contradict a published page, or look like an existing topic. Readers still see the old page." count={d.held_changes.length}>
          {d.held_changes.map((k) => (
            <Link key={k.id} to={`/library/review/${k.id}?c=${cid}`} style={{ display: 'block', color: 'inherit', textDecoration: 'none', padding: '10px 0', borderTop: '1px solid var(--line)' }}>
              <div className="row"><strong>{k.title}</strong><Pill tone={KIND[k.kind][1]}>{KIND[k.kind][0]}</Pill><span className="right small muted">{new Date(k.created_at).toLocaleString()}</span></div>
              <div className="small muted" style={{ marginTop: 4 }}>{k.description}</div></Link>))}
        </Section>
        <Section title="Source pages with objections" hint="The checker still disagrees with the page after its corrections." count={d.pages.length}>
          {d.pages.map((p) => <Link key={p.id} to={`/library/sources/${p.id}?c=${cid}`} style={{ display: 'block', color: 'inherit', textDecoration: 'none', padding: '10px 0', borderTop: '1px solid var(--line)' }}><strong>{p.title || p.name}</strong></Link>)}
        </Section>
        <Section title="Suggested links to the data model" hint="The AI thinks a topic is about an entity but is not sure enough to link it." count={d.suggested_links.length}>
          {d.suggested_links.map((l) => (
            <div key={l.slug + l.ref} className="row" style={{ padding: '10px 0', borderTop: '1px solid var(--line)' }}>
              <div><Link to={`/library/topics/${l.slug}?c=${cid}`}><b>{l.title}</b></Link> {l.label} <b>{l.name}</b> <span className="small muted">({Math.round((l.confidence || 0) * 100)}%){l.reason ? ` ${l.reason}` : ''}</span></div>
              <span className="right row"><button className="btn small primary" disabled={busy} onClick={() => act(() => api.post(`${base}/topics/${l.slug}/model-links/decide`, { kind: l.kind, ref: l.ref, accept: true }))}>Accept</button>
                <button className="btn small" disabled={busy} onClick={() => act(() => api.post(`${base}/topics/${l.slug}/model-links/decide`, { kind: l.kind, ref: l.ref, accept: false }))}>Reject</button></span></div>))}
        </Section>
        <Section title="Documents that could not be read" hint="Try again, or remove them." count={d.failed_sources.length}>
          {d.failed_sources.map((f) => (
            <div key={f.id} className="row" style={{ padding: '10px 0', borderTop: '1px solid var(--line)' }}>
              <div><b>{f.name}</b><div className="small" style={{ color: 'var(--bad)' }}>{f.error}</div></div>
              <span className="right row"><button className="btn small" disabled={busy} onClick={() => act(() => api.post(`${base}/sources/${f.id}/retry`))}>Retry</button>
                <button className="btn small danger" disabled={busy} onClick={() => confirm(`Remove ${f.name}?`) && act(() => api.del(`${base}/sources/${f.id}`))}>Remove</button></span></div>))}
        </Section>
        <Section title="Kinds proposed for the vocabulary" hint={manager ? 'You manage this collection: accept or reject.' : "Only the collection's manager decides these."} count={d.vocabulary.proposals.length}>
          {d.vocabulary.proposals.map((p) => (
            <div key={p.id} className="row" style={{ padding: '10px 0', borderTop: '1px solid var(--line)' }}>
              <div><Pill>{p.kind === 'topic_type' ? 'topic type' : 'relation'}</Pill> <b>{p.name}</b> <span className="small muted">{p.reason || p.description}</span></div>
              {manager ? <span className="right row"><button className="btn small primary" disabled={busy} onClick={() => act(() => api.post(`${base}/vocabulary/proposals/${p.id}`, { approve: true }))}>Accept</button>
                <button className="btn small" disabled={busy} onClick={() => act(() => api.post(`${base}/vocabulary/proposals/${p.id}`, { approve: false }))}>Reject</button></span> : <span className="right small muted">waiting for the manager</span>}</div>))}
        </Section>
      </>}
    </>
  )
}

export function ConflictPage() {
  const { kid } = useParams()
  const [params] = useSearchParams()
  const cid = params.get('c'), nav = useNavigate()
  const [d, setD] = useState(null), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  useEffect(() => { api.get(`/api/library/collections/${cid}/conflicts/${kid}`).then(setD).catch((e) => setErr(e.message)) }, [kid, cid])
  const act = async (what, body) => { setBusy(true); try { await api.post(`/api/library/collections/${cid}/conflicts/${kid}/${what}`, body); nav(`/library/review?c=${cid}`) } catch (e) { setErr(e.message) } setBusy(false) }
  if (err) return <div className="banner bad">{err} <Link to={`/library/review?c=${cid}`}>Back</Link></div>
  if (!d) return <p className="muted">Loading…</p>
  const k = d.conflict, canWrite = d.collection.level === 'write' || d.collection.level === 'manage', open = k.status === 'open'
  return (
    <>
      <div className="small muted" style={{ marginBottom: 8 }}><Link to={`/library/review?c=${cid}`}>Review</Link> / {d.collection.name}</div>
      <h1>{d.topic.title}</h1>
      <div className="row" style={{ marginBottom: 10 }}><Pill tone={KIND[k.kind][1]}>{KIND[k.kind][0]}</Pill>{!open && <Pill tone={k.status === 'rejected' ? 'bad' : 'ok'}>{k.status} by {k.decided_by}</Pill>}{d.source && <span className="small muted">from <Link to={`/library/sources/${d.source.id}?c=${cid}`}>{d.source.name}</Link></span>}</div>
      <div className="banner">{k.description}</div>
      {d.contradicts && <div className="card" style={{ marginBottom: 14, borderColor: 'var(--warn)' }}><div className="small muted">The claim it contradicts</div><div style={{ marginTop: 4 }}><b>{d.contradicts.n}.</b> {d.contradicts.text}</div></div>}
      {d.duplicate_of && <div className="card" style={{ marginBottom: 14 }}><div className="small muted">Existing topic it resembles</div><div style={{ marginTop: 4 }}><Link to={`/library/topics/${d.duplicate_of.slug}?c=${cid}`}><b>{d.duplicate_of.title}</b></Link>: {d.duplicate_of.summary}</div></div>}
      {open && canWrite && <div className="row" style={{ marginBottom: 14 }}>
        <button className="btn primary" disabled={busy} onClick={() => act('approve')}>{k.kind === 'duplicate_topic' ? 'Keep it as a separate topic' : 'Approve the change'}</button>
        {k.kind === 'duplicate_topic' && d.duplicate_of && <button className="btn" disabled={busy} onClick={() => act('redirect', {})}>Merge into “{d.duplicate_of.title}”</button>}
        <button className="btn danger" disabled={busy} onClick={() => act('reject')}>Reject</button></div>}
      {open && !canWrite && <p className="small muted">You can read this but not decide: that needs write access and the editor role.</p>}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 18, alignItems: 'flex-start' }}>
        {k.kind === 'contradiction' && <div className="card" style={{ flex: '1 1 380px', minWidth: 0 }}><div className="small muted" style={{ marginBottom: 6 }}>Published now</div><div className="article">{d.current ? <SafeMarkdown sources={{ cid, names: d.source_names }} topics={{ cid }}>{d.current}</SafeMarkdown> : <p className="muted">Nothing published.</p>}</div></div>}
        <div className="card" style={{ flex: '1 1 380px', minWidth: 0, borderColor: 'var(--accent)' }}><div className="small muted" style={{ marginBottom: 6 }}>{k.kind === 'contradiction' ? 'As it would become' : 'The new topic'}</div><div className="article">{d.proposed && <SafeMarkdown sources={{ cid, names: d.source_names }} topics={{ cid }}>{d.proposed}</SafeMarkdown>}</div></div>
        <div className="card" style={{ flex: '1 1 300px', minWidth: 0 }}>
          <strong>New claims</strong>
          {d.new_claims.map((c) => (
            <div key={c.id} style={{ borderTop: '1px solid var(--line)', padding: '10px 0' }}>
              <div className="small"><b>{c.n}.</b> {c.text} <Pill tone={c.status === 'pending' ? 'warn' : c.status === 'active' ? 'ok' : ''}>{c.status}</Pill></div>
              {c.evidence.map((e, i) => <blockquote key={i} className="small muted" style={{ margin: '4px 0', paddingLeft: 10, borderLeft: '3px solid var(--line)' }}>{e.quote}<div>{e.source}{e.location ? ` · ${e.location}` : ''}</div></blockquote>)}
            </div>))}
          {!d.new_claims.length && <p className="small muted" style={{ marginBottom: 0 }}>None.</p>}
        </div>
      </div>
    </>
  )
}
