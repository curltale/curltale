import { Fragment, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, q } from '../api.js'
import { useStudio } from '../context.jsx'
import { Pill } from '../components/ui.jsx'

// durations stay short whatever their size: 850 ms · 12.4 s · 4 m 05 s · 2 h 07 m · 3 d 4 h
const ms = (v) => {
  if (v == null) return ''
  if (v < 1000) return Math.round(v) + ' ms'
  if (v < 60000) return (v / 1000).toFixed(1) + ' s'
  const s = Math.round(v / 1000), pad = (n) => String(n).padStart(2, '0')
  if (s < 3600) return `${Math.floor(s / 60)} m ${pad(s % 60)} s`
  if (s < 86400) return `${Math.floor(s / 3600)} h ${pad(Math.floor(s % 3600 / 60))} m`
  return `${Math.floor(s / 86400)} d ${Math.floor(s % 86400 / 3600)} h`
}
const usd = (v) => (v == null || +v === 0 ? '' : '$' + (+v).toFixed(4))
const when = (t) => new Date(t).toLocaleString()
const WINDOWS = [[24, '24 hours'], [168, '7 days'], [720, '30 days']]

export default function ActivityPage() {
  const { sid } = useParams()
  return sid ? <Session sid={sid} /> : <Overview />
}

function Overview() {
  const { user } = useStudio()
  const nav = useNavigate()
  const [hours, setHours] = useState(168), [source, setSource] = useState(''), [sum, setSum] = useState(null), [rows, setRows] = useState(null), [err, setErr] = useState('')
  const load = () => {
    api.get('/api/admin/activity/summary?hours=' + hours).then(setSum).catch((e) => setErr(e.message))
    api.get('/api/admin/activity/sessions?' + q({ hours, source })).then(setRows).catch((e) => setErr(e.message))
  }
  useEffect(() => { setSum(null); setRows(null); load() }, [hours, source])
  if (user.role !== 'admin') return <p className="muted">Admins only.</p>
  return (
    <>
      <div className="row"><div><h1>Activity</h1><p className="muted" style={{ margin: 0 }}>Who asked what, which model calls and tool calls followed, how long each took. Results are summarised, never stored. Kept {sum?.retention_days ?? 30} days.</p></div>
        <div className="right row">
          <select style={{ width: 'auto' }} value={source} onChange={(e) => setSource(e.target.value)}><option value="">All sources</option><option value="alfred">Alfred</option><option value="mcp">MCP agents</option><option value="library">Library</option></select>
          <select style={{ width: 'auto' }} value={hours} onChange={(e) => setHours(+e.target.value)}>{WINDOWS.map(([h, l]) => <option key={h} value={h}>Last {l}</option>)}</select>
          <button className="btn" onClick={load}>Refresh</button></div></div>
      {err && <div className="banner bad">{err}</div>}
      {sum && <div className="grid" style={{ marginTop: 14, gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 520px), 1fr))' }}>
        <div className="card" style={{ minWidth: 0 }}><h3 style={{ marginTop: 0 }}>Tools</h3>
          <div style={{ overflowX: 'auto' }}><table><thead><tr><th>Tool</th><th style={{ textAlign: 'right' }}>Calls</th><th style={{ textAlign: 'right' }}>Errors</th><th style={{ textAlign: 'right' }}>Median</th><th style={{ textAlign: 'right' }}>p95</th><th style={{ textAlign: 'right' }}>Answer size</th></tr></thead><tbody>
            {sum.tools.map((t) => <tr key={t.name}><td className="mono">{t.name}</td><td style={{ textAlign: 'right' }}>{t.calls}</td><td style={{ textAlign: 'right' }}>{t.errors > 0 ? <Pill tone="warn">{t.errors}</Pill> : 0}</td>
              <td style={{ textAlign: 'right' }}>{ms(t.p50)}</td><td style={{ textAlign: 'right' }}>{ms(t.p95)}</td><td style={{ textAlign: 'right' }} className="small muted">{t.avg_chars ? Math.round(t.avg_chars / 100) / 10 + 'k chars' : ''}</td></tr>)}
            {!sum.tools.length && <tr><td colSpan="6" className="muted">No tool calls in this window.</td></tr>}</tbody></table></div></div>
        <div className="card" style={{ minWidth: 0 }}><h3 style={{ marginTop: 0 }}>Model calls</h3>
          <div style={{ overflowX: 'auto' }}><table><thead><tr><th>Model</th><th style={{ textAlign: 'right' }}>Calls</th><th style={{ textAlign: 'right' }}>Median</th><th style={{ textAlign: 'right' }}>First token</th><th style={{ textAlign: 'right' }}>Tokens in / out</th><th style={{ textAlign: 'right' }}>Cost</th></tr></thead><tbody>
            {sum.llm.map((t) => <tr key={t.name}><td className="mono small">{t.name}</td><td style={{ textAlign: 'right' }}>{t.calls}{t.errors > 0 && <> <Pill tone="warn">{t.errors} failed</Pill></>}</td><td style={{ textAlign: 'right' }}>{ms(t.p50)}</td><td style={{ textAlign: 'right' }}>{ms(t.ttft_p50)}</td>
              <td style={{ textAlign: 'right' }} className="small">{(+t.tokens_in || 0).toLocaleString()} / {(+t.tokens_out || 0).toLocaleString()}</td><td style={{ textAlign: 'right' }}>{usd(t.cost)}</td></tr>)}
            {!sum.llm.length && <tr><td colSpan="6" className="muted">No model calls in this window.</td></tr>}</tbody></table></div>
          <p className="small muted" style={{ marginBottom: 0 }}>{sum.sessions.map((s) => `${s.sessions} ${s.source} session${s.sessions === 1 ? '' : 's'} by ${s.actors} ${s.actors === 1 ? 'actor' : 'actors'}`).join(' · ')}</p></div></div>}

      <h2 style={{ marginTop: 22 }}>Sessions</h2>
      <p className="small muted" style={{ marginTop: -6 }}>An Alfred conversation is one session. MCP is stateless, so calls by one token with no gap longer than 30 minutes form one session.</p>
      {!rows ? <p className="muted">Loading…</p> : <table><thead><tr><th>When</th><th>Who</th><th>Questions / calls</th><th style={{ textAlign: 'right' }}>Model</th><th style={{ textAlign: 'right' }}>Tools</th><th style={{ textAlign: 'right' }}>Time in model / tools</th><th style={{ textAlign: 'right' }}>Tokens</th><th style={{ textAlign: 'right' }}>Cost</th></tr></thead><tbody>
        {rows.map((s) => (
          <tr key={s.id} className="click" onClick={() => nav('/activity/' + s.id)}>
            <td className="small">{when(s.started_at)}{new Date(s.last_at) - new Date(s.started_at) > GAP && <div className="muted">last: {when(s.last_at)}</div>}</td><td><Pill tone={s.source === 'alfred' ? 'accent' : s.source === 'library' ? 'ok' : ''}>{s.source}</Pill> {s.actor}</td>
            <td style={{ maxWidth: 360 }}>{s.title || <span className="muted">(MCP calls)</span>}{s.errors > 0 && <> <Pill tone="warn">{s.errors} error{s.errors > 1 ? 's' : ''}</Pill></>}
              {s.questions > 1 && s.last_question && s.last_question !== s.title && <div className="small muted clip">latest of {s.questions}: {s.last_question}</div>}</td>
            <td style={{ textAlign: 'right' }}>{s.llm_calls || ''}</td><td style={{ textAlign: 'right' }}>{s.tool_calls}</td><td style={{ textAlign: 'right' }} className="small nowrap">{ms(+s.llm_ms)} / {ms(+s.tool_ms)}</td>
            <td style={{ textAlign: 'right' }} className="small">{(+s.tokens_in + +s.tokens_out) ? (+s.tokens_in + +s.tokens_out).toLocaleString() : ''}</td><td style={{ textAlign: 'right' }}>{usd(s.cost)}</td></tr>))}
        {!rows.length && <tr><td colSpan="8" className="muted">No sessions in this window yet.</td></tr>}</tbody></table>}
      <Audit />
    </>
  )
}

const ACTION = { create: 'created', update: 'changed', delete: 'deleted', grant: 'granted access', revoke: 'removed access', self_grant: 'granted themselves access', upload: 'uploaded a source', delete_source: 'removed a source', setting: 'changed a setting', password: 'changed a password' }

// who changed what: collections, access, roles, settings. File names are never recorded here.
function Audit() {
  const [kind, setKind] = useState(''), [rows, setRows] = useState(null), [err, setErr] = useState('')
  useEffect(() => { setRows(null); api.get('/api/admin/audit?' + q({ kind })).then(setRows).catch((e) => setErr(e.message)) }, [kind])
  const brief = (v) => v ? Object.entries(v).filter(([, x]) => x != null).map(([k, x]) => `${k}: ${typeof x === 'object' ? JSON.stringify(x) : x}`).join(' · ') : ''
  return (
    <>
      <div className="row" style={{ marginTop: 26 }}><div><h2 style={{ margin: 0 }}>Audit trail</h2><p className="small muted" style={{ margin: 0 }}>Changes to collections, access, roles and settings. An admin who reads a collection first grants themselves access: it shows here.</p></div>
        <select className="right" style={{ width: 'auto' }} value={kind} onChange={(e) => setKind(e.target.value)}><option value="">Everything</option><option value="collection">Collections</option><option value="roles">Roles</option><option value="entitlements">Entitlements</option><option value="setting">Settings</option></select></div>
      {err && <div className="banner bad">{err}</div>}
      {!rows ? <p className="muted">Loading…</p> : <table style={{ marginTop: 10 }}><thead><tr><th>When</th><th>Who</th><th>What</th><th>Details</th></tr></thead><tbody>
        {rows.map((r) => <tr key={r.id}><td className="small nowrap">{when(r.at)}</td><td>{r.username}</td><td>{ACTION[r.action] || r.action} <span className="muted small">({r.kind})</span></td>
          <td className="small muted" style={{ maxWidth: 420 }}>{r.collection ? <b>{r.collection}</b> : r.kind === 'collection' ? <span className="mono">{String(r.key).slice(0, 8)}…</span> : r.key} {brief(r.after) || brief(r.before)}</td></tr>)}
        {!rows.length && <tr><td colSpan="4" className="muted">Nothing yet.</td></tr>}</tbody></table>}
    </>
  )
}

const GAP = 30 * 60 * 1000                               // a pause this long starts a new stretch of the timeline (as for MCP sessions)

const KIND = { step: ['step', ''], job: ['job', 'ok'], user: ['asked', 'accent'], llm: ['model', ''], tool: ['tool', ''], answer: ['answered', 'ok'], error: ['error', 'warn'] }

function Session({ sid }) {
  const [d, setD] = useState(null), [err, setErr] = useState('')
  useEffect(() => { api.get('/api/admin/activity/sessions/' + sid).then(setD).catch((e) => setErr(e.message)) }, [sid])
  if (err) return <div className="banner bad">{err} <Link to="/activity">Back</Link></div>
  if (!d) return <p className="muted">Loading…</p>
  const { session: s, events } = d, t0 = new Date(events[0]?.at || s.started_at).getTime()
  let seg = t0                                                // "+ time" counts from the start of each stretch, so a chat of several days stays readable
  return (
    <>
      <div className="row"><div><Link to="/activity" className="small">← Activity</Link><h1 style={{ marginTop: 4 }}>{s.title || 'MCP session'}</h1>
        <div className="muted small"><Pill tone={s.source === 'alfred' ? 'accent' : ''}>{s.source}</Pill> {s.actor} · {when(s.started_at)} · {events.length} events</div></div></div>
      <table style={{ marginTop: 12 }}><thead><tr><th style={{ width: 90 }} className="nowrap">+ time</th><th style={{ width: 90 }}>Step</th><th>What</th><th style={{ textAlign: 'right', width: 90 }}>Took</th><th style={{ textAlign: 'right', width: 150 }}>Tokens / cost</th></tr></thead><tbody>
        {events.map((e, i) => {
          const dt = e.detail || {}, [label, tone] = KIND[e.kind] || [e.kind, '']
          const at = new Date(e.at).getTime(), prev = i ? new Date(events[i - 1].at).getTime() : t0
          if (!i) seg = t0
          const pause = i > 0 && at - prev > GAP
          if (pause) seg = at
          return (
            <Fragment key={e.id}>
            {pause && <tr className="gap"><td colSpan="5" className="small muted">{when(e.at)} · after {ms(at - prev)} without activity</td></tr>}
            <tr>
              <td className="small muted nowrap" title={when(e.at)}>{ms(at - seg) || '0 ms'}</td>
              <td><Pill tone={e.ok ? tone : 'warn'}>{label}</Pill></td>
              <td>
                {e.kind === 'user' && <div>{dt.text}{dt.page?.view && <div className="small muted">on page: {dt.page.view}{dt.page.entity ? ' ' + dt.page.entity : ''}</div>}</div>}
                {e.kind === 'llm' && <div className="small"><span className="mono">{e.name}</span>{dt.tool_calls?.length ? <> → asked for <b>{dt.tool_calls.join(', ')}</b></> : <> → wrote the answer</>}{dt.ttft_ms != null && <span className="muted"> · first token {ms(dt.ttft_ms)}</span>}{dt.error && <div style={{ color: 'var(--warn)' }}>{dt.error}</div>}</div>}
                {e.kind === 'tool' && <div><b className="mono">{e.name}</b> <span className="small muted">{dt.rows != null ? `${dt.rows} rows · ` : ''}{dt.hits != null ? `${dt.hits} hits · ` : ''}{dt.chars != null ? `${(dt.chars / 1000).toFixed(1)}k chars` : ''}</span>
                  <pre className="mono small" style={{ whiteSpace: 'pre-wrap', margin: '2px 0 0' }}>{JSON.stringify(dt.args)}</pre>{dt.error && <div style={{ color: 'var(--warn)' }} className="small">{dt.error}</div>}
                  {e.has_result && <Response eid={e.id} truncated={dt.truncated} chars={dt.chars} />}</div>}
                {e.kind === 'answer' && <div className="small" style={{ whiteSpace: 'pre-wrap' }}>{dt.text}</div>}
                {e.kind === 'step' && <div className="small">{e.name}{dt.message ? <span className="muted"> · {dt.message}</span> : ''}</div>}
                {e.kind === 'job' && <div className="small"><span className="mono">{e.name}</span> · attempt {dt.attempt}{dt.error && <span style={{ color: 'var(--warn)' }}> · {dt.error}</span>}</div>}
                {e.kind === 'error' && <div style={{ color: 'var(--warn)' }}>{dt.error}</div>}
              </td>
              <td style={{ textAlign: 'right' }} className="nowrap">{ms(e.ms)}</td>
              <td style={{ textAlign: 'right' }} className="small">{e.tokens_in != null ? `${e.tokens_in.toLocaleString()} / ${(e.tokens_out || 0).toLocaleString()}` : ''} {usd(e.cost)}</td></tr>
            </Fragment>)
        })}</tbody></table>
    </>
  )
}

// A tool response as the caller received it: fetched when the fold is opened, scrolls inside its box, cut by the server when it was long
function Response({ eid, truncated, chars }) {
  const [text, setText] = useState(null), [err, setErr] = useState(''), [copied, setCopied] = useState(false)
  const load = (ev) => {
    if (!ev.currentTarget.open || text != null) return
    api.get(`/api/admin/activity/events/${eid}/result`).then((r) => setText(typeof r.result === 'string' ? r.result : JSON.stringify(r.result, null, 2))).catch((e) => setErr(e.message))
  }
  const copy = () => navigator.clipboard?.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500) })
  return (
    <details className="response" onToggle={load}><summary className="small muted">Response{truncated ? ` (first part of ${(chars / 1000).toFixed(1)}k chars)` : ''}</summary>
      {err ? <div className="small" style={{ color: 'var(--warn)' }}>{err}</div> : text == null ? <div className="small muted">Loading…</div> : <>
        <button className="btn small" onClick={copy}>{copied ? 'Copied' : 'Copy'}</button>
        <pre className="mono small">{text}{truncated ? '\n… cut here' : ''}</pre></>}</details>
  )
}
