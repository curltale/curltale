import { useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { api, q } from '../api.js'
import { useStudio } from '../context.jsx'
import { FieldsEditor, Modal } from '../components/ui.jsx'

const SCHEMA = [
  { name: 'title', help: 'the question this query answers' }, { name: 'body', type: 'area', rows: 3, help: 'a note on the path it takes or what to watch for' },
  { name: 'sql', type: 'area', rows: 8, help: 'use :name for parameters' }, { name: 'params', type: 'json', rows: 3, help: '{"customer_id": "customer"}: parameter -> entity key' },
  { name: 'sample_sql', type: 'area', rows: 3, help: 'optional: a query returning one row of parameter values to try' },
  { name: 'applies_to', type: 'lines', help: 'entity keys this is about, one per line' }, { name: 'topic', help: 'optional grouping' },
]

export default function ExamplesPage() {
  const { version, editable, refresh } = useStudio()
  const { hash } = useLocation()
  const [rows, setRows] = useState(null), [filter, setFilter] = useState(''), [edit, setEdit] = useState(null)
  const load = () => api.get('/api/examples?' + q({ version })).then(setRows)
  useEffect(() => { setRows(null); load() }, [version])
  const shown = (rows || []).filter((r) => !filter || JSON.stringify(r).toLowerCase().includes(filter.toLowerCase()))
  const done = () => { setEdit(null); load(); refresh() }
  return (
    <>
      <div className="row"><div><h1>Query examples</h1><p className="muted" style={{ margin: 0 }}>Worked queries with their SQL: patterns the model offers to agents building a query. The Model check runs each on real rows. Rules, definitions and procedures are documents: see the Library.</p></div>
        <div className="right row"><input style={{ width: 220 }} placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} />
          {editable && <button className="btn primary" onClick={() => setEdit({ isNew: true, data: { applies_to: [] } })}>New example</button>}</div></div>
      {!rows ? <p className="muted">Loading…</p> : <table style={{ marginTop: 12 }}><thead><tr><th style={{ width: 90 }}>Id</th><th>Example</th><th style={{ width: 200 }}>Applies to</th>{editable && <th />}</tr></thead><tbody>
        {shown.map((r) => (
          <tr key={r.id} style={hash === '#' + r.id ? { outline: '2px solid var(--accent)' } : null}>
            <td className="mono small muted">{r.id}</td>
            <td><b>{r.title}</b><div className="small">{r.body}</div>
              {r.sql && <pre className="mono small" style={{ whiteSpace: 'pre-wrap', margin: '6px 0 0' }}>{r.sql}</pre>}
              <Link className="btn small" style={{ marginTop: 6 }} to={'/data/sql?pattern=' + r.id}>Try on data</Link></td>
            <td className="small">{r.applies_to?.length ? r.applies_to.map((k) => <span key={k}><Link to={'/entities/' + k}>{k}</Link> </span>) : <span className="muted">everywhere</span>}</td>
            {editable && <td><button className="btn small" onClick={() => setEdit({ id: r.id, data: r })}>Edit</button></td>}</tr>))}
        {!shown.length && <tr><td colSpan="4" className="muted">{rows.length ? 'Nothing matches.' : 'No examples yet.'}</td></tr>}</tbody></table>}
      {edit && <Modal title={edit.isNew ? 'New example' : `Edit ${edit.id}`} onClose={() => setEdit(null)} wide>
        <FieldsEditor schema={SCHEMA} value={edit.data} onCancel={() => setEdit(null)}
          onSave={async (d) => { const id = edit.isNew ? 'EX' + String(Date.now()).slice(-5) : edit.id; await api.put('/api/examples/' + encodeURIComponent(id), { data: d }); done() }} />
        {!edit.isNew && <><hr style={{ border: 0, borderTop: '1px solid var(--line)', margin: '14px 0' }} /><button className="btn danger small" onClick={async () => { if (confirm('Delete this example?')) { await api.del('/api/examples/' + encodeURIComponent(edit.id)); done() } }}>Delete</button></>}
      </Modal>}
    </>
  )
}
