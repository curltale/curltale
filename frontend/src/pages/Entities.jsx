import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, q } from '../api.js'
import { useStudio } from '../context.jsx'
import { FieldsEditor, Modal, Pill } from '../components/ui.jsx'

export const ENTITY_SCHEMA = (domains) => [
  { name: 'name', required: true }, { name: 'domain', type: 'select', options: domains }, { name: 'kind', help: 'master, transaction, reference, …' },
  { name: 'definition', type: 'area' }, { name: 'synonyms', type: 'lines', rows: 5, help: 'one per line: words users type' },
  { name: 'not_to_confuse_with', type: 'lines', help: 'one per line: "Other entity: why it differs"' },
  { name: 'semantic_table', help: 'schema.table' }, { name: 'business_key' }, { name: 'system_of_record' }, { name: 'belongs_to' }, { name: 'primary_key' },
  { name: 'model_status' }, { name: 'example_questions', type: 'lines', rows: 4 },
  { name: 'tmf', label: 'Industry standard mapping (optional)', type: 'json', rows: 4, help: 'optional, e.g. {"resource": "Customer", "reference": "standard name and version"}; leave empty if you follow no standard' },
  { name: 'lifecycle_states', type: 'json', rows: 4 },
  { name: 'custom', label: 'Custom properties', type: 'json', rows: 4, help: 'your own properties: {"name": "value"}; shown in the app and given to agents' },
]

export default function Entities() {
  const { version, editable, status, refresh } = useStudio()
  const [data, setData] = useState(null)
  const [filter, setFilter] = useState('')
  const [adding, setAdding] = useState(false)
  const [err, setErr] = useState('')
  const nav = useNavigate()
  const load = () => api.get('/api/entities?' + q({ version })).then(setData).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [version, status?.draft])

  const shown = useMemo(() => {
    if (!data) return {}
    const f = filter.toLowerCase()
    const out = {}
    data.entities.filter((e) => !f || (e.name + e.key + e.table + e.definition).toLowerCase().includes(f)).forEach((e) => (out[e.domain] ||= []).push(e))
    return out
  }, [data, filter])

  if (err) return <div className="banner bad">{err}</div>
  if (!data) return <p className="muted">Loading…</p>
  const pending = data.entities.reduce((s, e) => s + e.pending, 0)

  return (
    <>
      <div className="row"><div><h1>Entities</h1>
        <p className="muted" style={{ margin: 0 }}>{data.entities.length} entities in {data.domains.length} domains · {data.entities.reduce((s, e) => s + e.attributes, 0)} attributes · {pending} attributes pending confirmation</p></div>
        <div className="right row"><input style={{ width: 240 }} placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} />
          {editable && <button className="btn primary" onClick={() => setAdding(true)}>New entity</button>}</div></div>
      {data.domains.filter((d) => shown[d.name]).map((d) => (
        <section key={d.name}>
          <h2>{d.name}</h2>
          <div className="grid cols-2">
            {shown[d.name].map((e) => (
              <Link key={e.key} to={'/entities/' + e.key} className="card" style={{ color: 'inherit', textDecoration: 'none' }}>
                <div className="row"><b>{e.name}</b><Pill>{e.kind}</Pill>{e.pending > 0 && <Pill tone="warn">{e.pending} pending</Pill>}</div>
                <div className="mono muted" style={{ margin: '2px 0 6px' }}>{e.table}</div>
                <div className="small muted" style={{ display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>{e.definition}</div>
                <div className="small" style={{ marginTop: 6 }}>{e.attributes} fields{e.calculated ? ` · ${e.calculated} calculated` : ''}{e.filters ? ` · ${e.filters} filters` : ''}</div>
              </Link>
            ))}
          </div>
        </section>
      ))}
      {adding && (
        <Modal title="New entity" onClose={() => setAdding(false)}>
          <NewEntity domains={data.domains.map((d) => d.name)} onDone={(k) => { setAdding(false); refresh(); nav('/entities/' + k) }} />
        </Modal>
      )}
    </>
  )
}

function NewEntity({ domains, onDone }) {
  const [key, setKey] = useState('')
  const base = { name: '', domain: domains[0], kind: 'master', semantic_table: '', business_key: '', system_of_record: '' }
  return (
    <>
      <div className="field"><label>Key (lower_snake_case id used in links)</label><input value={key} onChange={(e) => setKey(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, '_'))} /></div>
      <FieldsEditor schema={ENTITY_SCHEMA(domains)} value={base} saveLabel="Create" onCancel={() => onDone(null)}
        onSave={async (d) => { if (!key) throw new Error('key is required'); await api.post('/api/entities/' + key, { data: d }); onDone(key) }} />
    </>
  )
}
