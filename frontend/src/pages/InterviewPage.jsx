import { useEffect, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import SafeMarkdown from '../components/SafeMarkdown.jsx'
import { Pill } from '../components/ui.jsx'

export default function InterviewPage() {
  const { iid } = useParams()
  const [params] = useSearchParams()
  const cid = params.get('c'), base = `/api/library/collections/${cid}/interviews/${iid}`
  const [d, setD] = useState(null), [err, setErr] = useState(''), [text, setText] = useState(''), [live, setLive] = useState(null), [sending, setSending] = useState(false)
  const [editClaim, setEditClaim] = useState(null), [editDraft, setEditDraft] = useState(null), [busy, setBusy] = useState(false)
  const end = useRef()
  const load = () => api.get(base).then((x) => { setD(x); setErr('') }).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [iid])
  useEffect(() => { end.current?.scrollIntoView({ block: 'end' }) }, [d?.turns?.length, live])
  const working = d?.working_on || []
  useEffect(() => { if (!working.length) return; const t = setInterval(load, 2500); return () => clearInterval(t) }, [working.join(',')])
  useEffect(() => { if (!sending && d && d.interview.status === 'active') { const t = setTimeout(load, 4000); return () => clearTimeout(t) } }, [sending, d?.turns?.length])   // facts arrive a moment after the answer

  const send = async (e) => {
    e.preventDefault()
    const content = text.trim(); if (!content || sending) return
    setSending(true); setText(''); setLive({ user: content, agent: '' }); setErr('')
    try {
      const r = await fetch(`${base}/turns`, { method: 'POST', credentials: 'same-origin', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ content }) })
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || r.statusText) }
      const reader = r.body.getReader(), dec = new TextDecoder(); let buf = ''
      for (;;) {
        const { value, done } = await reader.read(); if (done) break
        buf += dec.decode(value, { stream: true })
        let i
        while ((i = buf.indexOf('\n\n')) >= 0) {
          const block = buf.slice(0, i); buf = buf.slice(i + 2)
          const ev = /event: (\w+)/.exec(block)?.[1], data = /data: (.*)/.exec(block)?.[1]
          if (ev === 'content' && data) setLive((l) => ({ ...l, agent: l.agent + JSON.parse(data).delta }))
          if (ev === 'error' && data) setErr(JSON.parse(data).message)
        }
      }
    } catch (x) { setErr(x.message); setText(content) }
    setSending(false); setLive(null); await load()
  }
  const act = async (fn) => { setBusy(true); try { await fn(); setErr(''); await load() } catch (x) { setErr(x.message) } setBusy(false) }

  if (!d) return err ? <div className="banner bad">{err} <Link to={`/library/interview?c=${cid}`}>Back</Link></div> : <p className="muted">Loading…</p>
  const { interview: it, claims, draft } = d, closed = it.status === 'ingesting' || it.status === 'ingested'
  return (
    <>
      <div className="small muted" style={{ marginBottom: 8 }}><Link to="/library">Collections</Link> / {d.collection.name} / <Link to={`/library/interview?c=${cid}`}>Interviews</Link></div>
      <h1 style={{ fontSize: 22 }}>{it.seed.length > 140 ? it.seed.slice(0, 140) + '…' : it.seed}</h1>
      {closed && <div className="banner ok">This interview has been added to the collection{it.source_id && <> as <Link to={`/library/sources/${it.source_id}?c=${cid}`}>a source</Link></>}; the topics are built from it in the background.</div>}
      {err && <div className="banner bad">{err}</div>}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 20, alignItems: 'flex-start' }}>
        <div style={{ flex: '3 1 480px', minWidth: 0 }}>
          <div className="card" style={{ minHeight: 320 }}>
            {!d.turns.length && !live && <p className="muted">Tell the interviewer where to start: what happens, who is involved, what makes it hard. Paste notes if you have them.</p>}
            {[...d.turns.map((t) => ({ role: t.role, content: t.content })), ...(live ? [{ role: 'user', content: live.user }, { role: 'agent', content: live.agent || '…' }] : [])].map((t, i) => (
              <div key={i} style={{ margin: '10px 0', display: 'flex', justifyContent: t.role === 'user' ? 'flex-end' : 'flex-start' }}>
                <div className={'bubble ' + (t.role === 'user' ? 'user' : 'bot')} style={{ whiteSpace: 'pre-wrap' }}>{t.content}</div></div>))}
            <div ref={end} />
          </div>
          {!closed && <form className="row" style={{ marginTop: 10, alignItems: 'flex-end' }} onSubmit={send}>
            <textarea rows={3} style={{ flex: 1 }} value={text} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) send(e) }} placeholder="Your answer… (Ctrl+Enter to send)" aria-label="Your message" />
            <button className="btn primary" disabled={sending || !text.trim()}>{sending ? 'Answering…' : 'Send'}</button></form>}
        </div>
        <aside style={{ flex: '2 1 320px', minWidth: 0 }}>
          <div className="card">
            <div className="row"><strong>What has been established</strong><Pill tone="accent">{claims.length} facts</Pill>{working.includes('extract') && <span className="small muted">updating…</span>}</div>
            <p className="small muted" style={{ margin: '4px 0 0' }}>The interviewer remembers these. Correct or remove any that are wrong.</p>
            {claims.map((c) => (
              <div key={c.id} style={{ borderTop: '1px solid var(--line)', padding: '10px 0' }}>
                {editClaim?.id === c.id ? <div>
                  <textarea rows={3} value={editClaim.text} onChange={(e) => setEditClaim({ ...editClaim, text: e.target.value })} />
                  <div className="row" style={{ marginTop: 6 }}><button className="btn small primary" disabled={busy} onClick={() => act(async () => { await api.put(`${base}/claims/${c.id}`, { text: editClaim.text }); setEditClaim(null) })}>Save</button><button className="btn small" onClick={() => setEditClaim(null)}>Cancel</button></div></div>
                  : <><div className="small">{c.category && <Pill>{c.category}</Pill>} {c.text}</div>
                    {!closed && <div className="row" style={{ marginTop: 4 }}><button className="btn small" onClick={() => setEditClaim({ id: c.id, text: c.text })}>Edit</button><button className="btn small danger" disabled={busy} onClick={() => act(() => api.del(`${base}/claims/${c.id}`))}>Remove</button></div>}</>}
              </div>))}
            {!claims.length && <p className="small muted" style={{ marginBottom: 0 }}>Nothing yet: facts appear as you answer.</p>}
            {!closed && <div style={{ marginTop: 10 }}><button className="btn" disabled={busy || !claims.length || working.includes('draft')} onClick={() => act(() => api.post(`${base}/draft`))}>{working.includes('draft') ? 'Writing the draft…' : draft ? 'Write the draft again' : 'Write the draft'}</button></div>}
          </div>
        </aside>
      </div>
      {draft && <div className="card" style={{ marginTop: 18 }}>
        <div className="row"><strong>Draft</strong><Pill>version {draft.version}</Pill><span className="small muted">{draft.origin === 'edited' ? 'edited by you' : 'written from the facts'}</span></div>
        {editDraft != null ? <>
          <textarea className="mono" style={{ width: '100%', minHeight: '45vh', fontSize: 13, marginTop: 8 }} value={editDraft} onChange={(e) => setEditDraft(e.target.value)} aria-label="Draft (Markdown)" />
          <div className="row" style={{ marginTop: 8 }}><button className="btn primary" disabled={busy || !editDraft.trim()} onClick={() => act(async () => { await api.put(`${base}/draft`, { markdown: editDraft }); setEditDraft(null) })}>Save as a new version</button><button className="btn" onClick={() => setEditDraft(null)}>Cancel</button></div></>
          : <div className="article" style={{ marginTop: 8 }}><SafeMarkdown>{draft.markdown}</SafeMarkdown></div>}
        {!closed && editDraft == null && <div className="row" style={{ marginTop: 12 }}>
          <button className="btn" onClick={() => setEditDraft(draft.markdown)}>Edit the draft</button>
          <button className="btn primary" disabled={busy} onClick={() => { if (confirm('Add this document to the collection? It is read like any other document and turned into topics.')) act(() => api.post(`${base}/finalize`)) }}>Add to the collection</button>
          <button className="btn danger right" disabled={busy} onClick={() => { if (confirm('Throw this interview away?')) act(async () => { await api.del(base); window.location.assign(`/library/interview?c=${cid}`) }) }}>Discard the interview</button></div>}
      </div>}
    </>
  )
}
