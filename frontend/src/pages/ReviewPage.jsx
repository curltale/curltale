import { useEffect, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import SafeMarkdown from '../components/SafeMarkdown.jsx'
import { Pill } from '../components/ui.jsx'
import { useStudio } from '../context.jsx'

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
  const [d, setD] = useState(null), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const cid = params.get('c'), base = `/api/library/collections/${cid}`
  const { collections, status } = useStudio()               // the shell's bar chooses the collection; this page reads it
  const cols = collections ? collections.filter((c) => c.level === 'write' || c.level === 'manage') : null
  const allowed = !cols || cols.some((c) => c.id === cid)          // a collection this page cannot be used on: nothing is asked for
  const load = () => cid && allowed && api.get(`${base}/inbox`).then((x) => { setD(x); setErr('') }).catch((e) => setErr(e.message))
  useEffect(() => { setD(null); load() }, [cid, allowed])
  const act = async (fn) => { setBusy(true); try { await fn(); await load(); window.dispatchEvent(new Event('studio:changed')) } catch (e) { setErr(e.message) } setBusy(false) }
  const manager = d?.vocabulary.can_decide
  const waiting = status?.library_review_by || {}
  const others = (cols || []).filter((c) => c.id !== cid && waiting[c.id] > 0)
  return (
    <>
      <div className="row" style={{ marginBottom: 14 }}>
        <div><h1>Review</h1><div className="muted">What in this collection waits for a person. Everyone who can write in the collection sees the same queue and can decide on its content; the collection's manager also decides the vocabulary.</div></div>
      </div>
      {others.length > 0 && <div className="banner" style={{ marginBottom: 14 }}>Also waiting: {others.map((c, i) => <span key={c.id}>{i ? ', ' : ''}<Link to={`/library/review?c=${c.id}`}>{c.name}</Link> ({waiting[c.id]})</span>)}</div>}
      {cols && !cols.length && <p className="muted">You need write access to a collection (and the editor role) to review it.</p>}
      {cols?.length > 0 && cid && !cols.some((c) => c.id === cid) && <div className="banner">You need write access to this collection (and the editor role) to review it. Choose another collection above.</div>}
      {err && <div className="banner bad">{err}</div>}
      {d && <>
        <Section title="Held changes" hint="They contradict a published page, or look like an existing topic. Readers still see the old page." count={d.held_changes.length}>
          {d.held_changes.map((k) => (
            <Link key={k.id} to={`/library/review/${k.id}?c=${cid}`} style={{ display: 'block', color: 'inherit', textDecoration: 'none', padding: '10px 0', borderTop: '1px solid var(--line)' }}>
              <div className="row"><strong>{k.title}</strong><Pill tone={KIND[k.kind][1]}>{KIND[k.kind][0]}</Pill>{k.approving && <Pill tone="accent">approving…</Pill>}<span className="right small muted">{new Date(k.created_at).toLocaleString()}</span></div>
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

const DONE = {
  approved: 'Approved. Readers now see the page with this change.',
  rejected: 'Rejected. The page stays as it was.',
  redirected: 'Merged into the existing topic.',
}

export function ConflictPage() {
  const { kid } = useParams()
  const [params] = useSearchParams()
  const cid = params.get('c')
  useEffect(() => { rememberCollection(cid) }, [cid])
  const [d, setD] = useState(null), [err, setErr] = useState(''), [busy, setBusy] = useState(''), [notice, setNotice] = useState('')
  const url = `/api/library/collections/${cid}/conflicts/${kid}`
  const load = () => api.get(url).then((x) => { setD(x); return x }).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [kid, cid])
  const approving = d?.conflict.status === 'open' && d?.approval?.status === 'approving'
  useEffect(() => {                                                       // the page is being written in the background: follow it until it is decided
    if (!approving) return
    const t = setInterval(() => { load().then((x) => { if (x && x.conflict.status === 'approved') setNotice(DONE.approved) }) }, 3000)
    return () => clearInterval(t)
  }, [approving])
  const act = async (what, body, label) => {
    setBusy(label); setErr(''); setNotice('')
    try {
      const r = await api.post(`${url}/${what}`, body)
      const x = await load()
      if (r?.status !== 'approving') setNotice(DONE[x?.conflict.status] || 'Done.')
    } catch (e) { setErr(e.message) }
    setBusy('')
  }
  if (!d) return err ? <div className="banner bad">{err} <Link to={`/library/review?c=${cid}`}>Back</Link></div> : <p className="muted">Loading…</p>
  const k = d.conflict, canWrite = d.collection.level === 'write' || d.collection.level === 'manage', open = k.status === 'open'
  const back = <Link to={`/library/review?c=${cid}`}>Back to review</Link>
  return (
    <>
      <div className="small muted" style={{ marginBottom: 8 }}><Link to={`/library/review?c=${cid}`}>Review</Link> / {d.collection.name}</div>
      <h1>{d.topic.title}</h1>
      <div className="row" style={{ marginBottom: 10 }}><Pill tone={KIND[k.kind][1]}>{KIND[k.kind][0]}</Pill>{!open && <Pill tone={k.status === 'rejected' ? 'bad' : 'ok'}>{k.status} by {k.decided_by}</Pill>}{d.source && <span className="small muted">from <Link to={`/library/sources/${d.source.id}?c=${cid}`}>{d.source.name}</Link></span>}</div>
      {notice && <div className="banner ok">{notice} {k.status !== 'rejected' && d.topic.status !== 'archived' && <Link to={`/library/topics/${d.topic.slug}?c=${cid}`}>Open the topic</Link>} · {back}</div>}
      {err && <div className="banner bad">{err}</div>}
      {approving && <div className="banner info">Approving: the AI model is writing the page again with this change, on top of the page readers see now. This usually takes a minute or two. You can leave this page; the change stays in the review queue, marked approving, until it is published.</div>}
      {open && d.approval?.status === 'failed' && <div className="banner bad">The last approval did not go through: {d.approval.error}. Nothing was changed; you can approve it again.</div>}
      <div className="banner">{k.description}</div>
      {open && d.page_moved_on && !approving && <div className="banner warn">The page has changed since this was held (another update was published). Approving writes the page again with the AI model, applying this change on top of the current page, so the result can differ from the preview below.</div>}
      {d.contradicts && <div className="card" style={{ marginBottom: 14, borderColor: 'var(--warn)' }}><div className="small muted">The claim it contradicts</div><div style={{ marginTop: 4 }}><b>{d.contradicts.n}.</b> {d.contradicts.text}</div></div>}
      {d.duplicate_of && <div className="card" style={{ marginBottom: 14 }}><div className="small muted">Existing topic it resembles</div><div style={{ marginTop: 4 }}><Link to={`/library/topics/${d.duplicate_of.slug}?c=${cid}`}><b>{d.duplicate_of.title}</b></Link>: {d.duplicate_of.summary}</div></div>}
      {open && canWrite && !approving && <div className="row" style={{ marginBottom: 14 }}>
        <button className="btn primary" disabled={!!busy} onClick={() => act('approve', undefined, 'approve')}>{busy === 'approve' ? 'Approving…' : k.kind === 'duplicate_topic' ? 'Keep it as a separate topic' : 'Approve the change'}</button>
        {k.kind === 'duplicate_topic' && d.duplicate_of && <button className="btn" disabled={!!busy} onClick={() => act('redirect', {}, 'redirect')}>{busy === 'redirect' ? 'Merging…' : `Merge into “${d.duplicate_of.title}”`}</button>}
        <button className="btn danger" disabled={!!busy} onClick={() => act('reject', undefined, 'reject')}>{busy === 'reject' ? 'Rejecting…' : 'Reject'}</button></div>}
      {open && !canWrite && <p className="small muted">You can read this but not decide: that needs write access and the editor role.</p>}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 18, alignItems: 'flex-start' }}>
        {k.kind === 'contradiction' && <div className="card" style={{ flex: '1 1 380px', minWidth: 0 }}><div className="small muted" style={{ marginBottom: 6 }}>{open ? 'Published now' : 'Published when this was decided'}</div><div className="article">{d.current ? <SafeMarkdown sources={{ cid, names: d.source_names }} topics={{ cid }}>{d.current}</SafeMarkdown> : <p className="muted">Nothing published.</p>}</div></div>}
        <div className="card" style={{ flex: '1 1 380px', minWidth: 0, borderColor: 'var(--accent)' }}><div className="small muted" style={{ marginBottom: 6 }}>{k.kind === 'contradiction' ? (open ? 'As it would become' : 'The change as it was reviewed') : 'The new topic'}</div><div className="article">{d.proposed && <SafeMarkdown sources={{ cid, names: d.source_names }} topics={{ cid }}>{d.proposed}</SafeMarkdown>}</div></div>
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
