import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import SafeMarkdown from '../components/SafeMarkdown.jsx'
import { Pill } from '../components/ui.jsx'
import { UsagePill } from './CollectionsPage.jsx'

function ModelLinks({ d, cid, slug, onChange }) {
  const [adding, setAdding] = useState(false), [ents, setEnts] = useState([]), [pick, setPick] = useState(''), [rel, setRel] = useState('about'), [err, setErr] = useState('')
  const canWrite = d.collection.level === 'write' || d.collection.level === 'manage'
  const url = `/api/library/collections/${cid}/topics/${slug}/model-links`
  const act = async (fn) => { try { await fn(); setErr(''); onChange() } catch (e) { setErr(e.message) } }
  if (!d.model_links.length && !canWrite) return null
  return (
    <div className="card" style={{ marginTop: 16 }}>
      <div className="row"><strong>In the data model</strong><span className="small muted">Which entities this topic is about. Kept in this collection: the model does not know about it.</span></div>
      {err && <div className="banner bad" style={{ marginTop: 8 }}>{err}</div>}
      <div className="row" style={{ marginTop: 8, gap: 8 }}>
        {d.model_links.map((m) => (
          <span key={m.kind + m.ref} className={'pill ' + (m.status === 'suggested' ? 'warn' : 'accent')} title={m.reason || ''}>
            {m.in_model === false ? <span title="No longer in the published model">{m.name} ⚠</span> : <Link to={m.kind === 'entity' ? `/entities/${m.ref}` : `/entities/${m.ref.split('.')[0]}`}>{m.name}</Link>} · {m.label}{m.status === 'suggested' ? ` · suggested${m.confidence != null ? ` ${Math.round(m.confidence * 100)}%` : ''}` : ''}
            {canWrite && m.status === 'suggested' && <> <button className="btn small" style={{ padding: '0 6px' }} onClick={() => act(() => api.post(url + '/decide', { kind: m.kind, ref: m.ref, accept: true }))}>Accept</button></>}
            {canWrite && <> <button aria-label={`Remove link to ${m.name}`} className="btn small" style={{ padding: '0 6px' }} onClick={() => act(() => m.status === 'suggested' ? api.post(url + '/decide', { kind: m.kind, ref: m.ref, accept: false }) : api.del(`${url}?kind=${m.kind}&link=${encodeURIComponent(m.ref)}`))}>×</button></>}
          </span>))}
        {!d.model_links.length && <span className="small muted">No links yet.</span>}
      </div>
      {canWrite && !adding && <div className="row" style={{ marginTop: 10 }}><button className="btn small" onClick={() => { setAdding(true); api.get('/api/entities').then((x) => setEnts(x.entities)).catch(() => {}) }}>Link to an entity</button>
        <button className="btn small" title="Ask the AI which entities this topic is about" onClick={() => act(async () => { await api.post(url + '/suggest'); alert('Suggestions are on their way: reload in a moment.') })}>Suggest with AI</button></div>}
      {canWrite && adding && <div className="row" style={{ marginTop: 10 }}>
        <select style={{ width: 'auto', maxWidth: 260 }} value={pick} onChange={(e) => setPick(e.target.value)}><option value="">Choose an entity…</option>{ents.map((x) => <option key={x.key} value={x.key}>{x.name}</option>)}</select>
        <select style={{ width: 'auto' }} value={rel} onChange={(e) => setRel(e.target.value)}><option value="about">is about</option><option value="instance_of">is a kind of</option><option value="governs">governs</option><option value="uses">concerns</option></select>
        <button className="btn small primary" disabled={!pick} onClick={() => act(async () => { await api.put(url, { kind: 'entity', ref: pick, relation: rel }); setAdding(false); setPick('') })}>Add</button>
        <button className="btn small" onClick={() => setAdding(false)}>Cancel</button></div>}
    </div>
  )
}

export default function TopicPage() {
  const { slug } = useParams()
  const [params] = useSearchParams()
  const cid = params.get('c')
  const nav = useNavigate()
  const [ver, setVer] = useState(null), [d, setD] = useState(null), [err, setErr] = useState(''), [hi, setHi] = useState(null), [merge, setMerge] = useState(false), [all, setAll] = useState([]), [into, setInto] = useState('')
  const reload = () => api.get(`/api/library/collections/${cid}/topics/${slug}${ver ? `?version=${ver}` : ''}`).then(setD).catch((e) => setErr(e.message))
  const refs = useRef({})
  useEffect(() => { setD(null); api.get(`/api/library/collections/${cid}/topics/${slug}${ver ? `?version=${ver}` : ''}`).then(setD).catch((e) => setErr(e.message)) }, [slug, ver, cid])
  if (err) return <div className="banner bad">{err} <Link to={`/library/topics?c=${cid}`}>Back</Link></div>
  if (!d) return <p className="muted">Loading…</p>
  const { topic: t, collection: col } = d
  const known = new Set(d.claims.map((c) => c.n))
  const cite = (n) => known.has(n)
    ? <button key={Math.random()} className="cite" style={{ border: 0, cursor: 'pointer', font: 'inherit', fontSize: 11.5, fontWeight: 700, background: hi === n ? 'var(--accent)' : 'var(--accent-soft)', color: hi === n ? 'var(--accent-ink)' : 'var(--accent)', borderRadius: 5, padding: '0 5px', margin: '0 1px' }}
        title={`Statement ${n}`} onClick={() => { setHi(n); refs.current[n]?.scrollIntoView({ behavior: 'smooth', block: 'center' }) }}>{n}</button>
    : null
  return (
    <>
      <div className="small muted" style={{ marginBottom: 8 }}><Link to="/library">Collections</Link> / {col.name} / <Link to={`/library/topics?c=${cid}`}>Topics</Link></div>
      {d.redirected_from && <div className="banner">You followed an old address (“{d.redirected_from}”): this topic took it over.</div>}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 22, alignItems: 'flex-start' }}>
        <div style={{ flex: '3 1 520px', minWidth: 0 }}>
          <div className="card" style={{ padding: '26px 30px' }}>
            <div className="row" style={{ marginBottom: 10 }}>{t.kind_of?.length > 0 && <Pill tone="ok">{t.kind_of.join(' · ')}</Pill>}<Pill tone="accent">{t.topic_type}</Pill><UsagePill usage={col.usage} />
              {d.versions.length > 1 && <select style={{ width: 'auto' }} value={d.version || ''} onChange={(e) => setVer(+e.target.value)}>{d.versions.map((v) => <option key={v.version} value={v.version}>Version {v.version} · {new Date(v.created_at).toLocaleDateString()}</option>)}</select>}
              {t.needs_revision && <Pill tone="warn">needs revision: a source it relied on is gone</Pill>}</div>
            <h1 style={{ fontSize: 26 }}>{t.title}</h1>
            <div className="muted small" style={{ marginBottom: 14 }}>{col.usage_note}{d.aliases.length > 0 && <> Also called: {d.aliases.join(', ')}.</>}</div>
            {(col.level === 'write' || col.level === 'manage') && <div className="row" style={{ marginBottom: 12 }}>
              <button className="btn small" onClick={() => { setMerge(!merge); api.get(`/api/library/collections/${cid}/topics`).then((x) => setAll(x.topics.filter((o) => o.slug !== t.slug))) }}>Merge into another topic…</button>
              {t.needs_revision && <button className="btn small" onClick={async () => { try { await api.post(`/api/library/collections/${cid}/topics/${t.slug}/revise`); alert('Revision queued: reload in a moment.') } catch (e) { setErr(e.message) } }}>Revise the page now</button>}</div>}
            {merge && <div className="row" style={{ marginBottom: 12 }}><select style={{ width: 'auto', maxWidth: 320 }} value={into} onChange={(e) => setInto(e.target.value)}><option value="">Merge into…</option>{all.map((o) => <option key={o.slug} value={o.slug}>{o.title}</option>)}</select>
              <button className="btn small primary" disabled={!into} onClick={async () => { if (!confirm(`Fold “${t.title}” into the chosen topic? Its statements, aliases and links move there and this address redirects.`)) return; try { const r = await api.post(`/api/library/collections/${cid}/topics/${t.slug}/merge`, { into }); nav(`/library/topics/${r.slug}?c=${cid}`) } catch (e) { setErr(e.message) } }}>Merge</button></div>}
            {d.markdown ? <div className="article"><SafeMarkdown cite={cite} sources={{ cid, names: Object.fromEntries(d.sources.map((s) => [s.id, s.name])) }} topics={{ cid, titles: Object.fromEntries([...d.links, ...d.backlinks].map((l) => [l.slug, l.title])) }}>{d.markdown}</SafeMarkdown></div> : <p className="muted">There is no published page yet.</p>}
          </div>
          <ModelLinks d={d} cid={cid} slug={t.slug} onChange={() => api.get(`/api/library/collections/${cid}/topics/${slug}`).then(setD)} />
          {(d.links.length > 0 || d.backlinks.length > 0) && <div className="card" style={{ marginTop: 16 }}>
            <strong>Related topics</strong>
            <div className="row" style={{ marginTop: 8, gap: 8 }}>
              {d.links.map((l) => <Link key={l.relation + l.slug} className="pill" to={`/library/topics/${l.slug}?c=${cid}`} title={l.context}>{l.title} · {l.relation.replace(/_/g, ' ')}</Link>)}
              {d.backlinks.map((l) => <Link key={'b' + l.relation + l.slug} className="pill" to={`/library/topics/${l.slug}?c=${cid}`} title={l.context}>{l.title} · {l.relation.replace(/_/g, ' ')} this</Link>)}</div></div>}
        </div>
        <aside style={{ flex: '1 1 320px', minWidth: 0 }} aria-label="Evidence">
          <div className="card">
            <div className="row"><strong>Evidence</strong><Pill tone="ok">{d.claims.length} claims</Pill></div>
            {d.sources.length > 0 && <div className="small muted" style={{ marginTop: 6 }}>From {d.sources.map((s, i) => <span key={s.id}>{i ? ', ' : ''}<Link to={`/library/sources/${s.id}?c=${cid}`}>{s.name}</Link></span>)}</div>}
            {d.claims.map((c) => (
              <div key={c.id} ref={(el) => { refs.current[c.n] = el }} style={{ borderTop: '1px solid var(--line)', padding: '12px 0', background: hi === c.n ? 'var(--accent-soft)' : undefined, margin: '0 -4px', paddingLeft: 4, paddingRight: 4 }}>
                <div className="small"><span className="cite" style={{ background: 'var(--accent-soft)', color: 'var(--accent)', borderRadius: 5, padding: '0 5px', fontWeight: 700 }}>{c.n}</span> {c.text}</div>
                {c.evidence.map((e, i) => (
                  <div key={i} style={{ marginTop: 6 }}>
                    <div className="small">{e.source_id ? <Link to={`/library/sources/${e.source_id}?c=${cid}`}><b>{e.source}</b></Link> : <b>{e.source}</b>}<span className="muted">{e.wiki_version != null ? ` · source wiki v${e.wiki_version}` : ''}{e.wiki_chunk != null ? `, passage ${e.wiki_chunk + 1}` : ''}</span></div>
                    <blockquote className="small" style={{ margin: '4px 0', paddingLeft: 10, borderLeft: '3px solid var(--line)' }}>{e.quote}</blockquote>
                    {e.passage && <details className="small muted"><summary>In the source wiki</summary><div style={{ whiteSpace: 'pre-wrap', marginTop: 4 }}>{e.passage}</div></details>}
                  </div>))}
              </div>))}
            {!d.claims.length && <p className="small muted" style={{ marginBottom: 0 }}>No supporting claims are left.</p>}
          </div>
        </aside>
      </div>
    </>
  )
}
