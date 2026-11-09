import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import { Modal, Pill } from '../components/ui.jsx'
import { UsagePill } from './CollectionsPage.jsx'
import { WIKI } from './SourcePage.jsx'

const STATUS = { queued: ['Waiting', ''], processing: ['Working', 'accent'], ready: ['Ready', 'ok'], failed: ['Failed', 'bad'] }
const size = (n) => n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB'

function Passages({ cid, sid }) {
  const [d, setD] = useState(null), [off, setOff] = useState(0), [err, setErr] = useState('')
  useEffect(() => { api.get(`/api/library/collections/${cid}/sources/${sid}/passages?offset=${off}&limit=10`).then(setD).catch((e) => setErr(e.message)) }, [off])
  if (err) return <div className="banner bad">{err}</div>
  if (!d) return <p className="muted small">Loading…</p>
  return (
    <div style={{ marginTop: 8 }}>
      {d.passages.map((p) => (
        <div key={p.idx} style={{ borderLeft: '3px solid var(--line)', padding: '2px 0 2px 10px', margin: '8px 0' }}>
          <div className="small muted">#{p.idx + 1}{p.location ? ` · ${p.location}` : ''}{p.heading_path?.length ? ` · ${p.heading_path.join(' › ')}` : ''} · ~{p.tokens} tokens{!p.embedded && ' · not embedded'}</div>
          <div className="small" style={{ whiteSpace: 'pre-wrap' }}>{p.text.length > 600 ? p.text.slice(0, 600) + '…' : p.text}</div></div>))}
      <div className="row small"><button className="btn small" disabled={off === 0} onClick={() => setOff(off - 10)}>Previous</button><button className="btn small" disabled={off + 10 >= d.total} onClick={() => setOff(off + 10)}>Next</button><span className="muted">{off + 1}–{Math.min(off + 10, d.total)} of {d.total}</span></div>
    </div>
  )
}

function Row({ s, cid, canWrite, onChange }) {
  const [open, setOpen] = useState(false)
  const [label, tone] = STATUS[s.status] || [s.status, '']
  const busy = s.status === 'queued' || s.status === 'processing'
  const topicsRunning = s.status === 'ready' && s.job?.kind === 'source.topics' && (s.job.status === 'queued' || s.job.status === 'running')      // the topic wiki is built after the page is approved, in its own background job
  return (
    <div style={{ padding: '14px 0', borderTop: '1px solid var(--line)' }}>
      <div className="row" style={{ alignItems: 'baseline' }}>
        <strong>{s.kind === 'url' ? <a href={s.url} target="_blank" rel="noreferrer noopener">{s.name}</a> : s.name}</strong><span className="muted small">{s.size ? size(s.size) + ' · ' : ''}{s.kind === 'url' ? 'web page · ' : ''}by {s.created_by} · {new Date(s.created_at).toLocaleString()}</span>
        <span className="right row" style={{ gap: 6 }}>{s.status === 'ready' && s.wiki_status !== 'none' && <Pill tone={(WIKI[s.wiki_status] || [])[1]}>{(WIKI[s.wiki_status] || [s.wiki_status])[0]}</Pill>}<Pill tone={tone}>{label}</Pill></span>
      </div>
      {topicsRunning && <div className="small muted" style={{ marginTop: 4 }}>Building topics from this document… <span className="muted">{s.job.step || (s.job.status === 'queued' ? 'waiting for a worker' : '')}</span></div>}
      {busy && <>
        <div className="small muted" style={{ marginTop: 4 }}>{s.job?.step || (s.status === 'queued' ? 'Waiting for a worker' : 'Starting')}{s.job?.message ? ` · ${s.job.message}` : ''}</div>
        <div role="progressbar" aria-label="Progress" aria-valuenow={s.job?.pct || 0} aria-valuemin="0" aria-valuemax="100" style={{ height: 6, borderRadius: 3, background: 'var(--line)', overflow: 'hidden', marginTop: 6 }}>
          <i style={{ display: 'block', height: '100%', width: `${s.job?.pct || 4}%`, background: 'var(--accent)', transition: 'width .3s' }} /></div></>}
      {s.status === 'ready' && s.stats?.passages > 0 && <div className="small muted" style={{ marginTop: 2 }}>
        {s.title && s.title !== s.name ? `${s.title} · ` : ''}{s.stats.pages ? `${s.stats.pages} page${s.stats.pages === 1 ? '' : 's'} · ` : ''}{s.stats.passages} passages{s.stats.ocr_pages ? ` · ${s.stats.ocr_pages} read by OCR` : ''}{s.stats.embedded < s.stats.passages ? ' · keyword search only' : ''}
        {' '}<button className="btn small" onClick={() => setOpen(!open)}>{open ? 'Hide passages' : 'Show passages'}</button>
        {s.wiki_status !== 'skipped' && s.wiki_status !== 'none' && <Link className="btn small" style={{ marginLeft: 6 }} to={`/library/sources/${s.id}?c=${cid}`}>Read the page</Link>}</div>}
      {(s.stats?.notes || []).map((n, i) => <div key={i} className="small" style={{ color: 'var(--warn)', marginTop: 2 }}>{n}</div>)}
      {open && <Passages cid={cid} sid={s.id} />}
      {s.status === 'failed' && <div className="small" style={{ marginTop: 4, color: 'var(--bad)' }}>{s.error || s.job?.error}</div>}
      {canWrite && <div className="row" style={{ marginTop: 6 }}>
        {s.status === 'failed' && <button className="btn small" onClick={() => onChange('retry', s)}>Retry</button>}
        {s.kind === 'url' && s.status !== 'processing' && s.status !== 'queued' && <button className="btn small" title="Capture the address again" onClick={() => onChange('refresh', s)}>Refresh</button>}
        <button className="btn small danger" disabled={s.status === 'processing'} onClick={() => onChange('delete', s)}>Remove</button></div>}
    </div>
  )
}

function Search({ cid, usage }) {
  const [q, setQ] = useState(''), [r, setR] = useState(null), [err, setErr] = useState('')
  useEffect(() => { setR(null) }, [cid])
  return (
    <div className="card" style={{ marginBottom: 18 }}>
      <form className="row" onSubmit={async (e) => { e.preventDefault(); if (!q.trim()) return; try { setErr(''); setR(await api.get(`/api/library/collections/${cid}/search?q=${encodeURIComponent(q)}`)) } catch (x) { setErr(x.message) } }}>
        <input style={{ flex: 1, minWidth: 220 }} placeholder="Search this collection by meaning or words…" value={q} onChange={(e) => setQ(e.target.value)} /><button className="btn primary">Search</button></form>
      {err && <div className="banner bad" style={{ marginTop: 8 }}>{err}</div>}
      {r && <div style={{ marginTop: 8 }}>
        <div className="small muted">{r.passages.length ? `${r.passages.length} passages.` : 'Nothing matched.'} {r.usage_note}</div>
        {r.passages.map((p) => (
          <div key={p.passage} style={{ borderLeft: '3px solid var(--accent)', padding: '2px 0 2px 10px', margin: '10px 0' }}>
            <div className="small"><b>{p.source}</b><span className="muted">{p.location ? ` · ${p.location}` : ''}{p.section ? ` · ${p.section}` : ''}{p.similarity != null ? ` · similarity ${p.similarity}` : ''} · found by {p.found_by.join(' and ')}</span></div>
            <div className="small" style={{ whiteSpace: 'pre-wrap' }}>{p.text.length > 500 ? p.text.slice(0, 500) + '…' : p.text}</div></div>))}
      </div>}
    </div>
  )
}

export default function SourcesPage() {
  const [params, setParams] = useSearchParams()
  const [cols, setCols] = useState(null), [data, setData] = useState(null), [err, setErr] = useState(''), [over, setOver] = useState(false), [sending, setSending] = useState(0)
  const input = useRef(), [adding, setAdding] = useState(false), [url, setUrl] = useState('')
  const cid = params.get('c')
  useEffect(() => { api.get('/api/library/collections').then((d) => { const mine = d.collections.filter((c) => c.level); setCols(mine); if (!cid && mine[0]) setParams({ c: mine[0].id }, { replace: true }) }).catch((e) => setErr(e.message)) }, [])
  const load = () => cid && api.get(`/api/library/collections/${cid}/sources`).then((d) => { setData(d); setErr('') }).catch((e) => setErr(e.message))
  useEffect(() => { setData(null); load() }, [cid])
  const active = data?.sources.some((s) => s.status === 'queued' || s.status === 'processing' || (s.job?.kind === 'source.topics' && (s.job.status === 'queued' || s.job.status === 'running')))
  useEffect(() => { if (!active) return; const t = setInterval(load, 2500); return () => clearInterval(t) }, [active, cid])
  const canWrite = data?.collection.level === 'write' || data?.collection.level === 'manage'
  const send = async (files) => {
    for (const f of files) {
      setSending((n) => n + 1)
      try { const fd = new FormData(); fd.append('file', f); const r = await fetch(`/api/library/collections/${cid}/sources`, { method: 'POST', body: fd, credentials: 'same-origin' }); if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(`${f.name}: ${j.detail || r.statusText}`) } } catch (e) { setErr(e.message) }
      setSending((n) => n - 1)
    }
    window.dispatchEvent(new Event('studio:changed'))
    load()
  }
  const act = async (what, s) => {
    try {
      if (what === 'refresh') await api.post(`/api/library/collections/${cid}/sources/${s.id}/refresh`)
      else if (what === 'delete') { if (!confirm(`Remove ${s.name}?`)) return; await api.del(`/api/library/collections/${cid}/sources/${s.id}`) } else await api.post(`/api/library/collections/${cid}/sources/${s.id}/retry`)
      load()
    } catch (e) { setErr(e.message) }
  }
  return (
    <>
      <div className="row" style={{ marginBottom: 14 }}>
        <div><h1>Sources</h1><div className="muted">The original files of a collection. Each is checked, then processed in the background.</div></div>
 {canWrite && <button className="btn right" onClick={() => setAdding(true)}>Add a web page</button>}
        {cols?.length > 0 && <select style={{ width: 'auto' }} value={cid || ''} onChange={(e) => setParams({ c: e.target.value })}>{cols.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select>}
      </div>
      {cols && !cols.length && <p className="muted">You have no access to any collection yet.</p>}
      {err && <div className="banner bad">{err}</div>}
      {data && <p className="small muted" style={{ marginTop: 0 }}>Collection <b>{data.collection.name}</b> <UsagePill usage={data.collection.usage} /> · your access: {data.collection.level}</p>}
      {canWrite && (
        <div className={'dropzone' + (over ? ' over' : '')} style={{ marginBottom: 18 }} role="button" tabIndex={0}
          onClick={() => input.current.click()} onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && input.current.click()}
          onDragOver={(e) => { e.preventDefault(); setOver(true) }} onDragLeave={() => setOver(false)} onDrop={(e) => { e.preventDefault(); setOver(false); send([...e.dataTransfer.files]) }}>
          <svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true"><path d="M14 20V6M8 12l6-6 6 6M5 22h18" fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>
          <div><strong>{sending ? `Uploading ${sending}…` : 'Drop files here'}</strong>{!sending && <> or click to browse</>}<div className="muted small">PDF, Word, PowerPoint, Excel, CSV, text, HTML, images.</div></div>
          <input ref={input} type="file" multiple onChange={(e) => { send([...e.target.files]); e.target.value = '' }} />
        </div>)}
      {adding && <Modal title="Add a web page" onClose={() => setAdding(false)}>
        <form onSubmit={async (e) => { e.preventDefault(); try { await api.post(`/api/library/collections/${cid}/sources/url`, { url }); setUrl(''); setAdding(false); setErr(''); load() } catch (x) { setErr(x.message) } }}>
          <div className="field"><label>Web address</label><input autoFocus placeholder="https://…" value={url} onChange={(e) => setUrl(e.target.value)} /></div>
          <p className="small muted">Only public addresses. The page is fetched on the server and kept as a source; you can refresh it later.</p>
          {err && <div className="banner bad">{err}</div>}
          <div className="row" style={{ justifyContent: 'flex-end' }}><button type="button" className="btn" onClick={() => setAdding(false)}>Cancel</button><button className="btn primary" disabled={!url.trim()}>Add</button></div></form></Modal>}
      {data && <Search cid={cid} usage={data.collection.usage} />}
      {data && <div className="card">
        <strong>In this collection</strong> <span className="muted small">{data.sources.length} source{data.sources.length === 1 ? '' : 's'}</span>
        {data.sources.map((s) => <Row key={s.id} s={s} cid={cid} canWrite={canWrite} onChange={act} />)}
        {!data.sources.length && <p className="muted" style={{ margin: '10px 0 0' }}>{canWrite ? 'Nothing yet. Drop a file above.' : 'Nothing yet.'}</p>}
      </div>}
    </>
  )
}
