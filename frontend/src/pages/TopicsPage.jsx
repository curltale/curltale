import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, q as qs } from '../api.js'
import { Pill } from '../components/ui.jsx'
import { UsagePill } from './CollectionsPage.jsx'

function Proposals({ cid, items, onDone }) {
  const [err, setErr] = useState('')
  const decide = async (id, approve) => { try { await api.post(`/api/library/collections/${cid}/vocabulary/proposals/${id}`, { approve }); onDone() } catch (e) { setErr(e.message) } }
  return (
    <div className="banner" style={{ marginBottom: 14 }}>
      <b>Kinds proposed while reading documents</b> (the manager decides; until then the closest existing kind is used)
      {err && <div className="small" style={{ color: 'var(--bad)' }}>{err}</div>}
      {items.map((p) => <div key={p.id} className="row small" style={{ marginTop: 6 }}><Pill>{p.kind === 'topic_type' ? 'topic type' : 'relation'}</Pill><b>{p.name}</b><span className="muted">{p.reason || p.description}</span>
        <button className="btn small primary right" onClick={() => decide(p.id, true)}>Accept</button><button className="btn small" onClick={() => decide(p.id, false)}>Reject</button></div>)}
    </div>
  )
}

export default function TopicsPage() {
  const [params, setParams] = useSearchParams()
  const [cols, setCols] = useState(null), [data, setData] = useState(null), [vocab, setVocab] = useState(null), [text, setText] = useState(params.get('q') || ''), [err, setErr] = useState('')
  const cid = params.get('c'), type = params.get('type') || '', query = params.get('q') || ''
  useEffect(() => { api.get('/api/library/collections').then((d) => { const mine = d.collections.filter((c) => c.level); setCols(mine); if (!cid && mine[0]) setParams({ c: mine[0].id }, { replace: true }) }).catch((e) => setErr(e.message)) }, [])
  const load = () => cid && api.get(`/api/library/collections/${cid}/topics?${qs({ type, q: query })}`).then((d) => { setData(d); setErr('') }).catch((e) => setErr(e.message))
  useEffect(() => { setData(null); load() }, [cid, type, query])
  const level = cols?.find((c) => c.id === cid)?.level
  useEffect(() => { if (cid && level === 'manage') api.get(`/api/library/collections/${cid}/vocabulary`).then(setVocab).catch(() => {}) }, [cid, level, data])
  const pending = (vocab?.proposals || []).filter((p) => p.status === 'pending')
  const go = (patch) => setParams({ c: cid, ...(type && { type }), ...(query && { q: query }), ...patch })
  return (
    <>
      <div className="row" style={{ marginBottom: 14 }}>
        <div><h1>Topics</h1><div className="muted">What the documents of a collection say, organised by subject, every statement tied to its evidence.</div></div>
        {cols?.length > 0 && <select className="right" style={{ width: 'auto' }} value={cid || ''} onChange={(e) => setParams({ c: e.target.value })}>{cols.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select>}
      </div>
      {cols && !cols.length && <p className="muted">You have no access to any collection yet.</p>}
      {err && <div className="banner bad">{err}</div>}
      {pending.length > 0 && <Proposals cid={cid} items={pending} onDone={() => { load(); api.get(`/api/library/collections/${cid}/vocabulary`).then(setVocab) }} />}
      {data && <p className="small muted" style={{ marginTop: 0 }}>Collection <b>{cols?.find((c) => c.id === cid)?.name}</b> <UsagePill usage={data.collection.usage} /> · {data.collection.usage_note}</p>}
      <form className="row" style={{ marginBottom: 12 }} onSubmit={(e) => { e.preventDefault(); go({ q: text || undefined, type: undefined }) }}>
        <input className="search" style={{ flex: 1, maxWidth: 420 }} placeholder="Find a topic by name or meaning…" value={text} onChange={(e) => setText(e.target.value)} /><button className="btn">Search</button>
        {query && <button type="button" className="btn" onClick={() => { setText(''); go({ q: undefined }) }}>Clear</button>}
      </form>
      {data?.types?.length > 1 && !query && <div className="row" style={{ marginBottom: 12, gap: 6 }}>
        <button className={'pill ' + (!type ? 'accent' : '')} style={{ border: 0, cursor: 'pointer' }} onClick={() => go({ type: undefined })}>All {data.total}</button>
        {data.types.map((t) => <button key={t.name} className={'pill ' + (type === t.name ? 'accent' : '')} style={{ border: 0, cursor: 'pointer' }} onClick={() => go({ type: t.name })}>{t.name} {t.n}</button>)}</div>}
      <div className="grid cols-2">
        {(data?.topics || []).map((t) => (
          <Link key={t.slug} to={`/library/topics/${t.slug}?c=${cid}`} className="card" style={{ color: 'inherit', textDecoration: 'none' }}>
            <div className="row" style={{ gap: 8 }}><strong style={{ fontSize: 15 }}>{t.title}</strong><span className="right row" style={{ gap: 6 }}>{t.kind_of?.length > 0 && <Pill tone="ok" title="What the data model says this is">{t.kind_of.join(' · ')}</Pill>}<Pill tone="accent">{t.topic_type}</Pill></span></div>
            <p className="muted small" style={{ margin: '6px 0 0' }}>{t.summary || 'No summary.'}</p>
            {t.claims !== undefined && <div className="small muted" style={{ marginTop: 8 }}>{t.claims} statements · {t.sources} source{t.sources === 1 ? '' : 's'}{t.needs_revision && <> <Pill tone="warn">needs revision</Pill></>}</div>}
            {t.similarity != null && <div className="small muted" style={{ marginTop: 8 }}>similarity {t.similarity}</div>}
          </Link>))}
        {data && !data.topics.length && <p className="muted">{query ? 'No topic matches.' : 'No topics yet. They appear when documents have been read and their pages approved (and the collection allows AI).'}</p>}
      </div>
    </>
  )
}
