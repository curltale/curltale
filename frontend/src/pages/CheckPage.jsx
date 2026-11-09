import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { can, useStudio } from '../context.jsx'
import { Pill } from '../components/ui.jsx'

const TONE = { pass: 'ok', fail: 'bad', warn: 'warn', info: '' }
const LABEL = { pass: 'pass', fail: 'fail', warn: 'attention', info: 'info' }

export default function CheckPage() {
  const { user } = useStudio()
  const [d, setD] = useState(null), [err, setErr] = useState(''), [busy, setBusy] = useState(false), [only, setOnly] = useState(''), [open, setOpen] = useState({})
  const load = (refresh) => { setBusy(true); api.get('/api/data/conformance' + (refresh ? '?refresh=true' : '')).then((x) => { setD(x); setBusy(false) }).catch((e) => { setErr(e.message); setBusy(false) }) }
  useEffect(() => load(false), [])
  const groups = useMemo(() => {
    const g = {}; (d?.checks || []).filter((c) => !only || c.status === only).forEach((c) => (g[c.group] ||= []).push(c)); return g
  }, [d, only])
  if (err) return <div className="banner bad">{err}</div>
  if (!d) return <p className="muted">Loading…</p>
  const s = d.summary
  return (
    <>
      <div className="row"><div><h1>Model check</h1><p className="muted" style={{ margin: 0 }}>Does the semantic model hold on data? Structure, relationships, data quality rules and the example queries, all run against the database.</p></div>
        {can(user, 'modeller') && <button className="btn right" onClick={() => load(true)} disabled={busy}>{busy ? 'Running…' : 'Run again'}</button>}</div>
      <div className="row" style={{ margin: '14px 0' }}>
        {['pass', 'fail', 'warn', 'info'].map((k) => <button key={k} className="btn" style={only === k ? { outline: '2px solid var(--accent)' } : null} onClick={() => setOnly(only === k ? '' : k)}><Pill tone={TONE[k]}>{s[k]}</Pill> {LABEL[k]}</button>)}
        {only && <button className="btn small" onClick={() => setOnly('')}>show all</button>}</div>
      <p className="small muted">“Attention” items are usually questions for the modellers rather than data bugs: the rule text and the physical schema do not fully line up.</p>
      {Object.entries(groups).map(([g, cs]) => (
        <section key={g}><h2>{g} <span className="muted small" style={{ fontWeight: 400 }}>{cs.length}</span></h2>
          <table><tbody>{cs.map((c) => (
            <>
              <tr key={c.id} className="click" onClick={() => setOpen({ ...open, [c.id]: !open[c.id] })}>
                <td style={{ width: 90 }}><Pill tone={TONE[c.status]}>{LABEL[c.status]}</Pill></td><td className="mono small" style={{ width: 190, wordBreak: 'break-all' }}>{c.id}</td>
                <td>{c.title}<div className="small muted">{c.detail}</div></td></tr>
              {open[c.id] && c.sql && <tr key={c.id + 's'}><td colSpan="3" style={{ background: 'var(--panel-2)' }}><pre className="mono small" style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{c.sql}</pre>
                <Link className="btn small" style={{ marginTop: 6 }} to={'/data/sql?q=' + encodeURIComponent(c.sql)}>Run in console</Link></td></tr>}
            </>))}</tbody></table></section>))}
      {!Object.keys(groups).length && <p className="muted">Nothing in this filter.</p>}
    </>
  )
}
