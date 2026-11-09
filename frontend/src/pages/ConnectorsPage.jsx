import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useStudio } from '../context.jsx'
import { Modal, Pill } from '../components/ui.jsx'

const BLANK = { name: '', params: { host: '', port: 5432, database: '', user: '', sslmode: 'prefer', schemas: [] }, password: '', options: { statement_timeout_ms: 15000, row_limit: 500, agents_may_read_rows: true } }
const when = (t) => (t ? new Date(t).toLocaleString() : '')

export default function ConnectorsPage({ embedded }) {
  const { user } = useStudio()
  const [data, setData] = useState(null), [err, setErr] = useState(''), [form, setForm] = useState(null), [busy, setBusy] = useState(''), [structure, setStructure] = useState(null), [result, setResult] = useState(null)
  const load = () => api.get('/api/admin/connectors').then(setData).catch((e) => setErr(e.message))
  useEffect(() => { if (user.role === 'admin') load() }, [])
  if (user.role !== 'admin') return <p className="muted">Admins only.</p>
  const act = async (id, name, fn) => { setBusy(id + name); setErr(''); try { await fn() } catch (e) { setErr(e.message) } setBusy(''); load() }

  return (
    <>
      <div className="row"><div>{embedded ? <h2 style={{ marginTop: 0 }}>Connectors</h2> : <h1>Connectors</h1>}<p className="muted" style={{ margin: 0 }}>The database the model describes. Queries from agents and the SQL console run against the active one. Passwords are stored encrypted and never shown again.</p></div>
        <div className="right"><button className="btn primary" onClick={() => setForm({ ...BLANK, kind: 'postgres', isNew: true })}>New connector</button></div></div>
      {err && <div className="banner bad" style={{ marginTop: 12, whiteSpace: 'pre-wrap' }}>{err}</div>}
      {result && <div className="banner ok" style={{ marginTop: 12 }}>
        <b>Draft created</b>: {result.entities} entities, {result.relationships} relationships. {result.problems.length > 0 && <span>{result.problems.length} problem(s) to fix before publishing. </span>}
        <Link to="/versions">Review it on the Versions page</Link>.
        {result.notes.length > 0 && <details style={{ marginTop: 6 }}><summary className="small">{result.notes.length} note(s) from the generator</summary><ul className="small" style={{ margin: '6px 0 0', paddingLeft: 18 }}>{result.notes.slice(0, 100).map((n, i) => <li key={i}>{n}</li>)}</ul></details>}</div>}

      <div className="card" style={{ marginTop: 14 }}>
        <div className="row"><b>Built-in demo database</b><Pill tone={data?.builtin_active ? 'ok' : ''}>{data?.builtin_active ? 'in use' : 'not in use'}</Pill>
          <span className="muted small">Generated from the model (Data → Database).</span>
          {!data?.builtin_active && <button className="btn small right" onClick={() => act(0, 'builtin', async () => { await api.post('/api/admin/connectors/deactivate') })}>Use it again</button>}</div></div>

      {(data?.connectors || []).map((c) => {
        const s = c.status
        return (
          <div className="card" key={c.id} style={{ marginTop: 12 }}>
            <div className="row"><b>{c.name}</b><Pill>{c.label || c.kind}</Pill>
              {c.active && <Pill tone="accent">in use</Pill>}
              {s ? (s.ok ? <Pill tone="ok">connected</Pill> : <Pill tone="warn">failed</Pill>) : <Pill>not tested</Pill>}
              <span className="mono small muted">{c.params.path ? c.params.path : `${c.params.user}@${c.params.host}:${c.params.port}${c.params.database ? '/' + c.params.database : ''}`}</span>
              <div className="right row">
                <button className="btn small" disabled={!!busy} onClick={() => act(c.id, 'test', () => api.post(`/api/admin/connectors/${c.id}/test`))}>{busy === c.id + 'test' ? 'Testing…' : 'Test'}</button>
                {!c.active && <button className="btn small primary" disabled={!!busy} onClick={() => act(c.id, 'use', async () => {
                  if (!confirm(`Use ${c.name} for data queries? Agents, Alfred and the SQL console will query it (read-only).`)) return
                  let r = await api.post(`/api/admin/connectors/${c.id}/activate`)
                  if (r.needs_confirmation) {
                    const cv = r.coverage
                    if (!confirm(`This database holds only ${cv.found} of the ${cv.total} tables the published model describes (missing e.g. ${cv.missing.slice(0, 4).join(', ')}).\n\nData queries will fail for the missing tables. Generate a model from this database (Generate draft model) if it is a different database.\n\nUse it anyway?`)) return
                    r = await api.post(`/api/admin/connectors/${c.id}/activate?force=true`)
                  }
                })}>{busy === c.id + 'use' ? 'Switching…' : 'Use for data'}</button>}
                <button className="btn small" onClick={() => setForm({ id: c.id, kind: c.kind, name: c.name, params: { ...c.params }, password: '', options: { ...c.options }, has_password: c.has_password })}>Edit</button>
                <button className="btn small danger" onClick={() => act(c.id, 'del', async () => { if (confirm(`Delete connector ${c.name}?`)) await api.del('/api/admin/connectors/' + c.id) })}>Delete</button></div></div>
            {s && (s.ok ? <div className="small" style={{ marginTop: 6 }}>{s.version} · as <b>{s.user}</b> · {s.tables_visible} readable tables · {s.ms} ms · tested {when(s.at)}
              {s.warnings?.map((w, i) => <div key={i} style={{ color: 'var(--warn)' }}>⚠ {w}</div>)}</div>
              : <div className="small" style={{ marginTop: 6, color: 'var(--warn)' }}>{s.error}</div>)}
            <div className="row" style={{ marginTop: 10 }}>
              <button className="btn" disabled={!!busy} onClick={() => act(c.id, 'read', async () => { const r = await api.post(`/api/admin/connectors/${c.id}/introspect`, { schemas: c.params.schemas?.length ? c.params.schemas : null }); setStructure({ id: c.id, name: c.name, ...r }) })}>
                {busy === c.id + 'read' ? 'Reading…' : 'Read structure'}</button>
              {c.tables_known > 0 && <><span className="small muted">{c.tables_known} tables known ({when(c.catalog_at)})</span>
                <button className="btn primary" onClick={() => act(c.id, 'pick', async () => { const r = await api.get(`/api/admin/connectors/${c.id}/structure`); setStructure({ id: c.id, name: c.name, ...r }) })}>Generate draft model…</button></>}
            </div>
          </div>)
      })}
      {data && !data.connectors.length && <p className="muted" style={{ marginTop: 14 }}>No connectors yet. Add one to point the model at a real database.</p>}

      {form && <Form form={form} kinds={data?.kinds || []} onClose={() => setForm(null)} onSaved={() => { setForm(null); load() }} />}
      {structure && <Pick structure={structure} onClose={() => setStructure(null)} onDone={(r) => { setStructure(null); setResult(r) }} hasDraftHint />}
    </>
  )
}

const DEFAULTS = { postgres: 5432, mysql: 3306, mariadb: 3306, clickhouse: 8123 }

function Form({ form, kinds, onClose, onSaved }) {
  const [f, setF] = useState(form), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const kind = kinds.find((k) => k.kind === f.kind) || { fields: ['host', 'port', 'database', 'user', 'password', 'sslmode', 'schemas'], file_based: false }
  const P = (k, v) => setF({ ...f, params: { ...f.params, [k]: v } }), O = (k, v) => setF({ ...f, options: { ...f.options, [k]: v } })
  const has = (n) => kind.fields.includes(n)
  const pickKind = (k) => setF({ ...f, kind: k, params: { ...f.params, port: DEFAULTS[k] || f.params.port } })
  const save = async () => {
    setBusy(true); setErr('')
    try {
      const body = { name: f.name, kind: f.kind, params: f.params, password: f.password || null, options: f.options }
      if (f.isNew) await api.post('/api/admin/connectors', body); else await api.put('/api/admin/connectors/' + f.id, body)
      onSaved()
    } catch (e) { setErr(e.message); setBusy(false) }
  }
  return (
    <Modal title={f.isNew ? 'New connector' : `Edit ${form.name}`} onClose={onClose}>
      <div className="row"><div className="field" style={{ flex: 1 }}><label>Kind</label>
        <select value={f.kind} disabled={!f.isNew} onChange={(e) => pickKind(e.target.value)}>{kinds.map((k) => <option key={k.kind} value={k.kind}>{k.label}</option>)}</select></div>
        <div className="field" style={{ flex: 2 }}><label>Name</label><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></div></div>
      {has('path') && <div className="field"><label>File path (inside the app's data folder, e.g. /app/data/samples/demo.duckdb)</label><input value={f.params.path || ''} onChange={(e) => P('path', e.target.value)} /></div>}
      {has('host') && <div className="row"><div className="field" style={{ flex: 3 }}><label>Host</label><input value={f.params.host || ''} onChange={(e) => P('host', e.target.value)} /></div>
        <div className="field" style={{ flex: 1 }}><label>Port</label><input value={f.params.port || ''} onChange={(e) => P('port', e.target.value)} /></div></div>}
      {has('host') && <div className="row"><div className="field" style={{ flex: 1 }}><label>Database{f.kind === 'mysql' || f.kind === 'mariadb' || f.kind === 'clickhouse' ? ' (optional)' : ''}</label><input value={f.params.database || ''} onChange={(e) => P('database', e.target.value)} /></div>
        <div className="field" style={{ flex: 1 }}><label>User</label><input value={f.params.user || ''} onChange={(e) => P('user', e.target.value)} autoComplete="off" /></div></div>}
      {has('password') && <div className="row"><div className="field" style={{ flex: 1 }}><label>Password {f.has_password && <span className="muted small">(leave empty to keep the stored one)</span>}</label><input type="password" value={f.password} autoComplete="new-password" onChange={(e) => setF({ ...f, password: e.target.value })} /></div>
        <div className="field" style={{ flex: 1 }}><label>SSL</label><select value={f.params.sslmode || 'prefer'} onChange={(e) => P('sslmode', e.target.value)}>{['disable', 'prefer', 'require', 'verify-ca', 'verify-full'].map((m) => <option key={m}>{m}</option>)}</select></div></div>}
      <div className="field"><label>{f.kind === 'mysql' || f.kind === 'mariadb' ? 'Databases' : 'Schemas'} to read (optional, one per line; empty = all)</label><textarea rows={2} value={(f.params.schemas || []).join('\n')} onChange={(e) => P('schemas', e.target.value.split('\n').map((x) => x.trim()).filter(Boolean))} /></div>
      <div className="row"><div className="field" style={{ flex: 1 }}><label>Statement timeout (ms)</label><input value={f.options.statement_timeout_ms} onChange={(e) => O('statement_timeout_ms', e.target.value)} /></div>
        <div className="field" style={{ flex: 1 }}><label>Row limit per query</label><input value={f.options.row_limit} onChange={(e) => O('row_limit', e.target.value)} /></div></div>
      <label className="small" style={{ display: 'block', marginBottom: 8 }}><input type="checkbox" className="switch" style={{ width: 'auto', marginRight: 6 }} checked={!!f.options.agents_may_read_rows} onChange={(e) => O('agents_may_read_rows', e.target.checked)} />Agents and Alfred may read rows (rows go to the language model). Untick for structure-only.</label>
      <p className="small muted">Use a <b>read-only</b> account{kind.file_based ? ' or a read-only copy of the file' : ''}. Queries are forced read-only as well, but the account is the real protection: Test tells you if it can write. The model keeps its SQL in PostgreSQL syntax and translates it for this database.</p>
      {err && <div className="banner bad">{err}</div>}
      <div className="row" style={{ justifyContent: 'flex-end' }}><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={busy} onClick={save}>{busy ? 'Saving…' : 'Save'}</button></div>
    </Modal>
  )
}

function Pick({ structure, onClose, onDone }) {
  const [sel, setSel] = useState(() => new Set(structure.tables.filter((t) => t.kind === 'table').map((t) => t.table))), [filter, setFilter] = useState(''), [replace, setReplace] = useState(false), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const shown = useMemo(() => structure.tables.filter((t) => !filter || t.table.toLowerCase().includes(filter.toLowerCase())), [filter, structure])
  const toggle = (t) => setSel((s) => { const n = new Set(s); n.has(t) ? n.delete(t) : n.add(t); return n })
  const go = async () => {
    setBusy(true); setErr('')
    try { onDone(await api.post(`/api/admin/connectors/${structure.id}/generate-draft`, { tables: [...sel], replace_draft: replace })) } catch (e) { setErr(e.message); setBusy(false) }
  }
  return (
    <Modal title={`Draft model from ${structure.name}`} onClose={onClose} wide>
      <p className="small muted" style={{ marginTop: 0 }}>{structure.tables.length} tables found. Pick the ones the model should describe. Entities, fields, keys and links are generated; everything is marked <i>needs review</i> and the result is a <b>draft</b>.</p>
      <div className="row"><input placeholder="Filter…" style={{ width: 220 }} value={filter} onChange={(e) => setFilter(e.target.value)} />
        <button className="btn small" onClick={() => setSel(new Set(shown.map((t) => t.table)))}>Select shown</button><button className="btn small" onClick={() => setSel(new Set())}>Clear</button>
        <span className="small muted right">{sel.size} selected</span></div>
      <div style={{ maxHeight: 360, overflow: 'auto', marginTop: 8 }}><table><tbody>
        {shown.map((t) => <tr key={t.table} className="click" onClick={() => toggle(t.table)}><td style={{ width: 30 }}><input type="checkbox" style={{ width: 'auto' }} checked={sel.has(t.table)} readOnly /></td>
          <td className="mono">{t.table}</td><td className="small muted">{t.kind}</td><td className="small" style={{ textAlign: 'right' }}>{t.columns} cols</td><td className="small" style={{ textAlign: 'right' }}>{t.rows != null ? `~${t.rows.toLocaleString()} rows` : ''}</td><td className="small muted">{t.fks ? `${t.fks} links` : ''}</td></tr>)}</tbody></table></div>
      {err && <div className="banner bad" style={{ marginTop: 8 }}>{err}</div>}
      <div className="row" style={{ marginTop: 12 }}><label className="small"><input type="checkbox" className="switch" style={{ width: 'auto', marginRight: 6 }} checked={replace} onChange={(e) => setReplace(e.target.checked)} />Replace the existing draft, if there is one</label>
        <div className="right row"><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={busy || !sel.size} onClick={go}>{busy ? 'Generating…' : `Generate draft (${sel.size} tables)`}</button></div></div>
    </Modal>
  )
}
