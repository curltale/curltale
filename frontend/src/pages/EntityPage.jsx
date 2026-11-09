import { Fragment, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, q } from '../api.js'
import { useStudio } from '../context.jsx'
import RelatedDocuments from '../components/RelatedDocuments.jsx'
import { ATTR_SCHEMA, CALC_SCHEMA, CHECK_SCHEMA, FILTER_SCHEMA, FieldsEditor, Modal, Pill, ReviewPill, SensPill, StatusPill, REL_STATUS } from '../components/ui.jsx'
import { ENTITY_SCHEMA } from './Entities.jsx'

const CARD = ['one-to-one', 'one-to-many', 'many-to-one', 'many-to-many']
const REL_SCHEMA = (names) => [
  { name: 'from', type: 'select', options: names }, { name: 'predicate' }, { name: 'to', type: 'select', options: names },
  { name: 'cardinality', type: 'select', options: CARD }, { name: 'join', help: 'a.col = b.col' },
  { name: 'status', type: 'select', options: REL_STATUS }, { name: 'status_detail' }, { name: 'sentence', type: 'area', rows: 2 }, { name: 'note', type: 'area', rows: 2 },
]
const Rule = () => <hr style={{ border: 0, borderTop: '1px solid var(--line)', margin: '14px 0' }} />

export default function EntityPage() {
  const { key } = useParams()
  const { version, editable, refresh, status } = useStudio()
  const nav = useNavigate()
  const [e, setE] = useState(null), [err, setErr] = useState(''), [tab, setTab] = useState('fields'), [edit, setEdit] = useState(null)
  const [names, setNames] = useState([]), [domains, setDomains] = useState([]), [filter, setFilter] = useState('')

  const load = () => api.get(`/api/entities/${key}?` + q({ version })).then((d) => { if (d.id && d.id !== key) { nav('/entities/' + d.id, { replace: true }); return } setE(d); setErr('') }).catch((x) => setErr(x.message))
  useEffect(() => { setE(null); load() }, [key, version])
  useEffect(() => { api.get('/api/entities?' + q({ version })).then((d) => { setNames(d.entities.map((x) => x.name)); setDomains(d.domains.map((x) => x.name)) }).catch(() => {}) }, [version])

  if (err) return <div className="banner bad">{err}</div>
  if (!e) return <p className="muted">Loading…</p>
  const done = () => { setEdit(null); load(); refresh() }
  const attrs = e.attributes.filter((a) => !filter || (a.column + a.business_name + (a.description || '')).toLowerCase().includes(filter.toLowerCase()))
  const pending = e.attributes.filter((a) => ['needs_review', 'source_needed'].includes(a.review_status)).length
  const strip = (o) => { const { column, code, ...r } = o; return r }
  const filters = e.filters || [], checks = e.checks || []
  // filters and checks are lists on the entity: edit one, save the whole list
  const saveList = async (name, list) => { await api.patch('/api/entities/' + key, { data: { [name]: list } }); done() }
  const upsertIn = (list, item, idx) => (idx === null || idx === undefined ? [...list, item] : list.map((x, i) => (i === idx ? item : x)))

  const tabs = [['fields', `Fields (${e.attributes.length})`], ['calculated', `Calculated (${e.calculated.length})`], ['filters', `Filters (${filters.length})`], ['checks', `Checks (${checks.length})`],
    ['relationships', `Relationships (${e.relationships.length})`], ['examples', `Examples (${e.examples.length})`], ['questions', 'Example questions']]

  return (
    <>
      <div className="row"><div><h1>{e.name}</h1><div className="mono muted">{e.semantic_table} · <Pill>{e.kind}</Pill> <Pill tone="accent">{e.domain}</Pill></div></div>
        <div className="right row"><Link className="btn" to={'/data/table?name=' + e.semantic_table}>Browse data</Link></div>
        {editable && <div className="row">
          <button className="btn" onClick={() => setEdit({ type: 'entity' })}>Edit entity</button>
          <button className="btn danger" onClick={async () => { if (confirm(`Delete ${e.name} and its fields?`)) { try { await api.del('/api/entities/' + key); refresh(); nav('/') } catch (x) { alert(x.message) } } }}>Delete</button></div>}
      </div>
      <p style={{ maxWidth: 820 }}>{e.definition}</p>
      <div className="grid cols-2">
        <div className="card"><dl className="kv" style={{ margin: 0 }}>
          <dt>Business key</dt><dd>{e.business_key}</dd><dt>System of record</dt><dd>{e.system_of_record}</dd><dt>Belongs to</dt><dd>{e.belongs_to || '—'}</dd>
          {(e.tmf?.reference || e.tmf?.resource) && <><dt>Standard</dt><dd>{e.tmf?.reference || e.tmf?.resource}</dd></>}<dt>Model status</dt><dd>{e.model_status || '—'}</dd>
          {Object.entries(e.custom || {}).map(([k, v]) => <Fragment key={k}><dt>{k}</dt><dd className="small" style={{ wordBreak: 'break-word' }}>{v}</dd></Fragment>)}</dl></div>
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Also called</h3><div className="row">{(e.synonyms || []).map((s) => <Pill key={s}>{s}</Pill>)}</div>
          {e.not_to_confuse_with?.length > 0 && <><h3>Not to confuse with</h3><ul style={{ margin: 0, paddingLeft: 18 }}>{e.not_to_confuse_with.map((s) => <li key={s} className="small">{s}</li>)}</ul></>}
        </div>
      </div>
      {status?.library && <RelatedDocuments entity={key} />}
      {e.lifecycle_states && <div className="card" style={{ marginTop: 14 }}><h3 style={{ marginTop: 0 }}>Lifecycle states</h3>{Object.entries(e.lifecycle_states).map(([k, v]) => <div key={k} className="small"><b>{k}</b>: {v.join(' → ')}</div>)}</div>}

      <div className="tabs">{tabs.map(([k, l]) => <button key={k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{l}</button>)}</div>

      {tab === 'fields' && <>
        <div className="row" style={{ marginBottom: 10 }}><input style={{ width: 260 }} placeholder="Filter fields…" value={filter} onChange={(x) => setFilter(x.target.value)} />
          {pending > 0 && <Pill tone="warn">{pending} pending confirmation</Pill>}
          {editable && <button className="btn primary right" onClick={() => setEdit({ type: 'new-field' })}>New field</button>}</div>
        <table><thead><tr><th>Field</th><th>Meaning</th><th>Type</th><th>Governance</th>{editable && <th />}</tr></thead><tbody>
          {attrs.map((a) => (
            <tr key={a.column}>
              <td><div className="mono">{a.column}</div><div className="small muted">{a.business_name}</div>{a.business_term && <div className="small">term: {a.business_term}</div>}</td>
              <td><div>{a.description}</div>
                {a.allowed_values && <div className="small muted">Values: {a.allowed_values.map((v) => (a.value_labels?.[v] && a.value_labels[v] !== v ? `${v} (${a.value_labels[v]})` : v)).join(', ')}</div>}
                {a.business_term_description && <div className="small muted">{a.business_term_description}</div>}
                {a.references && <div className="small">→ <Link to={'/path?' + q({ a: e.name, b: a.references })}>{a.references}</Link></div>}
                <Custom c={a.custom} /></td>
              <td><div>{a.type}</div><div className="small muted">{a.role}</div></td>
              <td><div className="row" style={{ gap: 4 }}><SensPill s={a.sensitivity} /><Pill>{a.agent_access}</Pill><ReviewPill s={a.review_status} /></div></td>
              {editable && <td style={{ whiteSpace: 'nowrap' }}><button className="btn small" onClick={() => setEdit({ type: 'field', a })}>Edit</button></td>}
            </tr>))}
        </tbody></table></>}

      {tab === 'calculated' && <>
        <p className="muted small" style={{ marginTop: 0 }}>Values computed from the data, not columns of the table. Their SQL, when given, tells agents how to compute them; the app never runs it.</p>
        {editable && <div style={{ marginBottom: 10 }}><button className="btn primary" onClick={() => setEdit({ type: 'calc', m: { role: 'calculated', type: 'decimal', sensitivity: 'internal' }, isNew: true })}>New calculated field</button></div>}
        {e.calculated.length ? <table><thead><tr><th>Calculated field</th><th>Meaning</th><th>Governance</th>{editable && <th />}</tr></thead><tbody>{e.calculated.map((m) => (
          <tr key={m.code}><td><b>{m.business_name}</b><div className="mono small muted">{m.code}</div></td>
            <td className="small">{m.description}{m.unit && <div className="muted">unit {m.unit}</div>}{m.sql && <pre className="mono small" style={{ whiteSpace: 'pre-wrap', margin: '4px 0 0' }}>{m.sql}</pre>}<Custom c={m.custom} /></td>
            <td><div className="row" style={{ gap: 4 }}><SensPill s={m.sensitivity} /><ReviewPill s={m.review_status} /></div></td>
            {editable && <td><button className="btn small" onClick={() => setEdit({ type: 'calc', m, code: m.code })}>Edit</button></td>}</tr>))}</tbody></table> : <p className="muted">No calculated fields.</p>}</>}

      {tab === 'filters' && <>
        <p className="muted small" style={{ marginTop: 0 }}>Named conditions on this entity's table: what words like <i>open</i>, <i>active</i> or <i>unpaid</i> mean. Agents get them pre-filled in the query plan.</p>
        {editable && <div style={{ marginBottom: 10 }}><button className="btn primary" onClick={() => setEdit({ type: 'filter', f: { status: 'working' }, idx: null })}>New filter</button></div>}
        {filters.length ? <table><thead><tr><th>Name</th><th>Condition</th><th>Status</th>{editable && <th />}</tr></thead><tbody>{filters.map((f, i) => (
          <tr key={f.name}><td><b>{f.name}</b>{f.synonyms?.length > 0 && <div className="small muted">also: {f.synonyms.join(', ')}</div>}</td><td><code>{f.filter}</code><div className="small muted">{f.description}{f.source ? ` · ${f.source}` : ''}</div></td>
            <td><Pill tone={f.status === 'governed' ? 'ok' : 'warn'}>{f.status}</Pill></td>{editable && <td><button className="btn small" onClick={() => setEdit({ type: 'filter', f, idx: i })}>Edit</button></td>}</tr>))}</tbody></table> : <p className="muted">No filters.</p>}</>}

      {tab === 'checks' && <>
        <p className="muted small" style={{ marginTop: 0 }}>Data quality rules about this entity. With SQL attached they run in the Model check.</p>
        {editable && <div style={{ marginBottom: 10 }}><button className="btn primary" onClick={() => setEdit({ type: 'check', c: {}, idx: null })}>New check</button></div>}
        {checks.length ? <table><thead><tr><th>Check</th><th>Rule</th>{editable && <th />}</tr></thead><tbody>{checks.map((c, i) => (
          <tr key={c.id}><td className="mono">{c.id}{c.sql ? '' : <div className="small muted">no SQL</div>}</td><td>{c.rule}{c.note && <div className="small muted">{c.note}</div>}{c.sql && <pre className="mono small" style={{ whiteSpace: 'pre-wrap', margin: '4px 0 0' }}>{c.sql}</pre>}</td>
            {editable && <td><button className="btn small" onClick={() => setEdit({ type: 'check', c, idx: i })}>Edit</button></td>}</tr>))}</tbody></table> : <p className="muted">No checks.</p>}</>}

      {tab === 'relationships' && <>
        {editable && <div style={{ marginBottom: 10 }}><button className="btn primary" onClick={() => setEdit({ type: 'rel', r: { from: e.name, status: 'confirmed', cardinality: 'many-to-one', status_detail: 'Confirmed' }, isNew: true })}>New relationship</button></div>}
        <table><thead><tr><th>Link</th><th>Join</th><th>Status</th>{editable && <th />}</tr></thead><tbody>
          {e.relationships.map((r) => {
            const out = r.from === e.name
            return (<tr key={r.id}>
              <td><div>{out ? <><b>{r.predicate}</b> → {r.to}</> : <>{r.from} → <b>{r.predicate}</b> (this)</>}</div><div className="small muted">{r.cardinality} · {r.id}</div>{r.note && <div className="small">{r.note}</div>}</td>
              <td className="mono small">{r.join}</td><td><StatusPill s={r.status} /></td>
              {editable && <td><button className="btn small" onClick={() => setEdit({ type: 'rel', r })}>Edit</button></td>}</tr>)
          })}</tbody></table></>}

      {tab === 'examples' && <>
        <p className="muted small" style={{ marginTop: 0 }}>Worked queries that apply to this entity. They are managed under <Link to="/examples">Query examples</Link>.</p>
        {e.examples.length ? <table><tbody>{e.examples.map((k) => (
          <tr key={k.id}><td className="mono small" style={{ width: 70 }}>{k.id}</td>
            <td><Link to={'/examples#' + k.id}><b>{k.title}</b></Link><div className="small muted">{(k.body || '').slice(0, 220)}</div></td></tr>))}</tbody></table> : <p className="muted">No example is attached to this entity yet.</p>}</>}
      {tab === 'questions' && <ul>{(e.example_questions || []).map((x) => <li key={x}>{x}</li>)}</ul>}

      {edit?.type === 'entity' && <Modal title={`Edit ${e.name}`} onClose={() => setEdit(null)}><FieldsEditor schema={ENTITY_SCHEMA(domains)} value={(({ attributes, calculated, relationships, examples, version: _v, id, subtypes, ...r }) => r)(e)}
        onCancel={() => setEdit(null)} onSave={async (d) => { await api.patch('/api/entities/' + key, { data: d }); done() }} /></Modal>}
      {edit?.type === 'field' && <Modal title={`Edit ${edit.a.column}`} onClose={() => setEdit(null)}><FieldsEditor schema={ATTR_SCHEMA} value={strip(edit.a)}
        onCancel={() => setEdit(null)} onSave={async (d) => { await api.put(`/api/entities/${key}/fields/${edit.a.column}`, { data: d }); done() }} />
        <Rule /><button className="btn danger small" onClick={async () => { if (confirm('Delete this field?')) { await api.del(`/api/entities/${key}/fields/${edit.a.column}`); done() } }}>Delete field</button></Modal>}
      {edit?.type === 'new-field' && <NewField entity={key} onDone={done} onCancel={() => setEdit(null)} />}
      {edit?.type === 'calc' && <CalcModal entity={key} edit={edit} onDone={done} onCancel={() => setEdit(null)} />}
      {edit?.type === 'filter' && <Modal title={edit.idx === null ? 'New filter' : `Edit ${edit.f.name}`} onClose={() => setEdit(null)}><FieldsEditor schema={FILTER_SCHEMA} value={edit.f}
        onCancel={() => setEdit(null)} onSave={(d) => saveList('filters', upsertIn(filters, d, edit.idx))} />
        {edit.idx !== null && <><Rule /><button className="btn danger small" onClick={() => saveList('filters', filters.filter((_, i) => i !== edit.idx))}>Delete filter</button></>}</Modal>}
      {edit?.type === 'check' && <Modal title={edit.idx === null ? 'New check' : `Edit ${edit.c.id}`} onClose={() => setEdit(null)} wide><FieldsEditor schema={CHECK_SCHEMA} value={edit.c}
        onCancel={() => setEdit(null)} onSave={(d) => saveList('checks', upsertIn(checks, d, edit.idx))} />
        {edit.idx !== null && <><Rule /><button className="btn danger small" onClick={() => saveList('checks', checks.filter((_, i) => i !== edit.idx))}>Delete check</button></>}</Modal>}
      {edit?.type === 'rel' && <Modal title={edit.isNew ? 'New relationship' : `Edit ${edit.r.id}`} onClose={() => setEdit(null)}><FieldsEditor schema={REL_SCHEMA(names)} value={edit.r}
        onCancel={() => setEdit(null)} onSave={async (d) => { const id = edit.isNew ? (await api.get('/api/relationships/next-id')).id : edit.r.id; await api.put('/api/relationships/' + id, { data: d }); done() }} />
        {!edit.isNew && <><Rule /><button className="btn danger small" onClick={async () => { if (confirm('Delete this relationship?')) { await api.del('/api/relationships/' + edit.r.id); done() } }}>Delete relationship</button></>}</Modal>}
    </>
  )
}

function NewField({ entity, onDone, onCancel }) {
  const [col, setCol] = useState('')
  const base = { business_name: '', type: 'text', role: 'attribute', sensitivity: 'internal', agent_access: 'full', review_status: 'needs_review' }
  return (
    <Modal title="New field" onClose={onCancel}>
      <div className="field"><label>Column name</label><input value={col} onChange={(e) => setCol(e.target.value.trim())} /></div>
      <FieldsEditor schema={ATTR_SCHEMA} value={base} saveLabel="Create" onCancel={onCancel}
        onSave={async (d) => { if (!col) throw new Error('column name is required'); await api.put(`/api/entities/${entity}/fields/${col}`, { data: d }); onDone() }} />
    </Modal>
  )
}

function CalcModal({ entity, edit, onDone, onCancel }) {
  const [code, setCode] = useState(edit.code || '')
  const del = async () => { if (confirm('Delete this calculated field?')) { await api.del(`/api/entities/${entity}/fields/${edit.code}`); onDone() } }
  return (
    <Modal title={edit.isNew ? 'New calculated field' : `Edit ${edit.m.business_name}`} onClose={onCancel} wide>
      {edit.isNew && <div className="field"><label>Code (lower_snake_case)</label><input value={code} onChange={(e) => setCode(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, '_'))} /></div>}
      <FieldsEditor schema={CALC_SCHEMA} value={(({ code: _c, ...r }) => r)(edit.m)} saveLabel={edit.isNew ? 'Create' : 'Save'} onCancel={onCancel}
        onSave={async (d) => { if (!code) throw new Error('a code is required'); await api.put(`/api/entities/${entity}/fields/${code}`, { data: { ...d, role: 'calculated' } }); onDone() }} />
      {!edit.isNew && <><Rule /><button className="btn danger small" onClick={del}>Delete calculated field</button></>}
    </Modal>
  )
}

function Custom({ c }) {
  const items = Object.entries(c || {})
  if (!items.length) return null
  return <div className="small muted" style={{ marginTop: 4 }}>{items.map(([k, v]) => <div key={k}><b>{k}</b>: {String(v).length > 160 ? String(v).slice(0, 160) + '…' : v}</div>)}</div>
}
