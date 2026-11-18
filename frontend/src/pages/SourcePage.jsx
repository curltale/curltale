import { useEffect, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import SafeMarkdown from '../components/SafeMarkdown.jsx'
import { api } from '../api.js'
import { Pill } from '../components/ui.jsx'
import { UsagePill } from './CollectionsPage.jsx'

export const WIKI = { approved: ['Page approved', 'ok'], needs_review: ['Needs review', 'warn'], skipped: ['No page (AI off)', ''], failed: ['Page failed', 'bad'], none: ['No page yet', ''] }
const VERDICT = { PASS: ['Passed', 'ok'], FAIL: ['Problems found', 'warn'], ERROR: ['Checker gave no result', 'bad'] }
const ISSUE = { UNSUPPORTED: 'not in the document', TOO_BROAD: 'broader than the document', TOO_SPECIFIC: 'more specific than the document', MISSING_QUALIFIER: 'a qualifier is missing', MISSING_FACT: 'a fact is missing',
  AMBIGUOUS_ENTITY: 'unclear which entity', UNCLEAR_RELATIONSHIP: 'unclear relationship', NUMERIC_MISMATCH: 'number differs', CONDITION_LOSS: 'a condition was lost', SOURCE_REFERENCE: 'refers to the document', OTHER: 'other' }

export default function SourcePage() {
  const { sid } = useParams()
  const [params] = useSearchParams()
  const cid = params.get('c')
  const [ver, setVer] = useState(null), [d, setD] = useState(null), [err, setErr] = useState(''), [busy, setBusy] = useState(false), [draft, setDraft] = useState(null), [waiting, setWaiting] = useState(false)
  const load = () => api.get(`/api/library/collections/${cid}/sources/${sid}/wiki${ver ? `?version=${ver}` : ''}`).then((x) => { setD(x); setErr('') }).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [sid, ver])
  const checked = d && d.checks.some((c) => c.version === d.versions[0]?.version)
  const timer = useRef()
  useEffect(() => {                                              // after an edit, wait for the check of that version
    if (!waiting) return
    if (checked) { setWaiting(false); return }
    timer.current = setTimeout(load, 3000)
    return () => clearTimeout(timer.current)
  }, [waiting, d])
  const save = async () => {
    setBusy(true)
    try { const r = await api.put(`/api/library/collections/${cid}/sources/${sid}/wiki`, { markdown: draft }); setDraft(null); setVer(null); setWaiting(r.checking); await load() } catch (e) { setErr(e.message) }
    setBusy(false)
  }
  const fileRef = useRef(), [rep, setRep] = useState({ busy: false, msg: '', bad: false })
  const reading = d && ['queued', 'processing'].includes(d.source.status)
  useEffect(() => {                                              // after a replace, follow the new version until it has been read
    if (!reading) return
    const t = setTimeout(load, 4000)
    return () => clearTimeout(t)
  }, [reading, d])
  const replace = async (f) => {
    if (!f) return
    setRep({ busy: true, msg: '', bad: false })
    try {
      const fd = new FormData(); fd.append('file', f)
      const r = await fetch(`/api/library/collections/${cid}/sources/${sid}/replace`, { method: 'POST', body: fd, credentials: 'same-origin' })
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || r.statusText) }
      setRep({ busy: false, msg: `${f.name} is the new version. It is being read; the topics it feeds are then updated in place.`, bad: false }); setVer(null); await load()
    } catch (e) { setRep({ busy: false, msg: e.message, bad: true }) }
    if (fileRef.current) fileRef.current.value = ''
  }
  const act = async (what) => { setBusy(true); try { await api.post(`/api/library/collections/${cid}/sources/${sid}/wiki/${what}`); if (what === 'approve') await load() } catch (e) { setErr(e.message) } setBusy(false) }
  if (err) return <div className="banner bad">{err} <Link to={`/library/sources?c=${cid}`}>Back</Link></div>
  if (!d) return <p className="muted">Loading…</p>
  const { source: s, collection: col } = d, canWrite = col.level === 'write' || col.level === 'manage'
  const [label, tone] = WIKI[s.wiki_status] || [s.wiki_status, '']
  const checks = d.checks.filter((c) => !d.version || c.version === d.version)
  return (
    <>
      <div className="small muted" style={{ marginBottom: 8 }}><Link to="/library">Collections</Link> / {col.name} / <Link to={`/library/sources?c=${cid}`}>Sources</Link></div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 22, alignItems: 'flex-start' }}>
        <div style={{ flex: '3 1 520px', minWidth: 0 }}>
          <div className="card" style={{ padding: '26px 30px' }}>
            <div className="row" style={{ marginBottom: 10 }}><Pill tone={tone}>{label}</Pill><UsagePill usage={col.usage} />
              {d.versions.length > 1 && <select style={{ width: 'auto' }} value={d.version || ''} onChange={(e) => setVer(+e.target.value)}>{d.versions.map((v) => <option key={v.version} value={v.version}>Version {v.version} · {v.origin}</option>)}</select>}</div>
            <h1 style={{ fontSize: 26 }}>{s.title || s.name}</h1>
            <div className="muted small" style={{ marginBottom: 14 }}>{s.name}. {col.usage_note}</div>
            {rep.msg && <div className={`banner ${rep.bad ? 'bad' : 'ok'}`}>{rep.msg}</div>}
            <div className="row" style={{ marginBottom: 14 }}>
              {canWrite && s.status === 'ready' && draft == null && <button className="btn small" onClick={() => setDraft(d.markdown || '')}>{d.markdown ? 'Edit the page' : 'Write the page by hand'}</button>}
              {d.markdown && <a className="btn small" href={`/api/library/collections/${cid}/sources/${sid}/download/page${d.version ? `?version=${d.version}` : ''}`}>Download the page</a>}
              <a className="btn small" href={`/api/library/collections/${cid}/sources/${sid}/download/original`}>Download the original file</a>
              {canWrite && s.kind === 'file' && !reading && <><button className="btn small" disabled={rep.busy} title="Upload a new version of this document: it stays the same source and its topics are updated in place" onClick={() => fileRef.current?.click()}>{rep.busy ? 'Uploading…' : 'Replace with a new version'}</button>
                <input ref={fileRef} type="file" hidden onChange={(e) => replace(e.target.files[0])} /></>}
              {reading && <span className="small muted">Reading the document…</span>}
              {waiting && <span className="small muted">Checking your version…</span>}</div>
            {draft != null ? <>
              <textarea className="mono" style={{ width: '100%', minHeight: '55vh', fontSize: 13 }} value={draft} onChange={(e) => setDraft(e.target.value)} aria-label="Page text (Markdown)" />
              <div className="row" style={{ marginTop: 8 }}><button className="btn primary" disabled={busy || !draft.trim()} onClick={save}>Save as a new version</button><button className="btn" onClick={() => setDraft(null)}>Cancel</button>
                <span className="small muted">{col.usage ? 'It is checked against the document and approved if it passes.' : ''}</span></div></>
            : d.markdown ? <div className="article"><SafeMarkdown>{d.markdown}</SafeMarkdown></div>
              : <p className="muted">{s.wiki_status === 'skipped' ? 'No page was written: AI is off for this collection or no AI endpoint is set. The passages are searchable.' : 'There is no page yet.'}</p>}
          </div>
        </div>
        <aside style={{ flex: '1 1 320px', minWidth: 0 }} aria-label="Checks">
          <div className="card">
            <strong>Checks</strong>
            {s.wiki_status === 'needs_review' && <div className="banner" style={{ marginTop: 10 }}>The checker still has objections after the corrections. Read them, then approve the page as it is, or write it again.</div>}
            {canWrite && <div className="row" style={{ marginTop: 10 }}>
              {s.wiki_status === 'needs_review' && <button className="btn small primary" disabled={busy} onClick={() => act('approve')}>Approve as it is</button>}
              {s.status === 'ready' && s.wiki_status !== 'skipped' && <button className="btn small" disabled={busy} onClick={() => act('regenerate')}>Write it again</button>}</div>}
            {!checks.length && <p className="small muted" style={{ marginBottom: 0 }}>No checks for this version.</p>}
            {checks.map((c, i) => {
              const [vl, vt] = VERDICT[c.verdict] || [c.verdict, '']
              return (
                <div key={i} style={{ borderTop: '1px solid var(--line)', padding: '12px 0 0', marginTop: 12 }}>
                  <div className="row"><span className="small">Version {c.version}, round {c.round}</span><Pill tone={vt}>{vl}</Pill><span className="small muted">{c.tool_calls} lookups</span></div>
                  {c.issues.map((x, j) => (
                    <div key={j} style={{ margin: '10px 0' }}>
                      <div className="small"><Pill tone="warn">{ISSUE[x.type] || x.type}</Pill></div>
                      {x.wiki_text && <blockquote className="small" style={{ margin: '6px 0', paddingLeft: 10, borderLeft: '3px solid var(--warn)' }}>{x.wiki_text}</blockquote>}
                      <div className="small">{x.reason}</div>
                      {(x.evidence_chunks || x.evidence || []).map((q, k) => <blockquote key={k} className="small muted" style={{ margin: '6px 0', paddingLeft: 10, borderLeft: '3px solid var(--line)' }}>{typeof q === 'string' ? q : q.text}</blockquote>)}
                      {x.suggested_fix && <div className="small muted">Suggested: {x.suggested_fix}</div>}
                    </div>))}
                </div>)
            })}
          </div>
        </aside>
      </div>
    </>
  )
}
