import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import { useStudio } from '../context.jsx'
import { ResultGrid } from '../components/DataGrid.jsx'

export default function SqlPage() {
  const [sp] = useSearchParams()
  const { user } = useStudio()
  const [sql, setSql] = useState(sp.get('q') || '-- one read-only statement, e.g.\nSELECT * FROM schema.table LIMIT 20')
  const [res, setRes] = useState(null), [err, setErr] = useState(''), [busy, setBusy] = useState(false), [ms, setMs] = useState(null), [examples, setExamples] = useState([])
  const ta = useRef(null)
  const run = async (text = sql) => {
    setBusy(true); setErr(''); const t = performance.now()
    try { setRes(await api.post('/api/data/sql', { sql: text })); setMs(Math.round(performance.now() - t)) } catch (e) { setErr(e.message); setRes(null) }
    setBusy(false)
  }
  useEffect(() => {
    api.get('/api/data/conformance').then((c) => {
      const ex = c.checks.filter((x) => x.group === 'Query examples')
      setExamples(ex)
      const p = sp.get('pattern')
      if (p) { const m = ex.find((x) => x.id === p); if (m) { setSql(m.sql); run(m.sql) } }
    }).catch(() => {})
    if (sp.get('q')) run(sp.get('q'))
  }, [])

  return (
    <>
      <h1>SQL console</h1>
      <p className="muted">Read-only, one statement, 8 second limit, up to 500 rows. Restricted columns {user.entitlements?.restricted ? 'are readable for you' : 'are refused at the database level'}.</p>
      <div className="row" style={{ marginBottom: 8 }}>
        <select style={{ width: 'auto', maxWidth: 420 }} value="" onChange={(e) => { const m = examples.find((x) => x.id === e.target.value); if (m) { setSql(m.sql); run(m.sql) } }}>
          <option value="">Example: a query example from the model…</option>{examples.map((x) => <option key={x.id} value={x.id}>{x.id} · {x.title}</option>)}</select>
        <span className="muted small right">Ctrl+Enter runs</span></div>
      <textarea ref={ta} className="mono" rows={9} value={sql} spellCheck={false} onChange={(e) => setSql(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) run() }} style={{ fontSize: 13 }} />
      <div className="row" style={{ margin: '10px 0' }}><button className="btn primary" onClick={() => run()} disabled={busy}>{busy ? 'Running…' : 'Run'}</button>
        {res && <span className="muted small">{res.rows.length} rows{res.truncated ? ` (cut at ${res.rows.length}: the row limit)` : ''} · {ms} ms</span>}</div>
      {err && <div className="banner bad" style={{ whiteSpace: 'pre-wrap' }}>{err}</div>}
      {res && <ResultGrid columns={res.columns} rows={res.rows} />}
    </>
  )
}
