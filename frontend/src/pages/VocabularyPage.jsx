import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import { Pill } from '../components/ui.jsx'

function Kinds({ title, help, kind, rows, protectedNames, canEdit, base, reload, setErr }) {
  const [adding, setAdding] = useState(false), [form, setForm] = useState({ name: '', description: '', inverse_name: '' }), [edit, setEdit] = useState(null), [merge, setMerge] = useState(null)
  const url = `${base}/${kind}`, act = async (fn) => { try { await fn(); setErr(''); reload() } catch (e) { setErr(e.message) } }
  const targets = (name) => rows.filter((r) => r.name !== name && r.status === 'approved')
  return (
    <div className="card" style={{ marginBottom: 18 }}>
      <div className="row"><div><strong>{title}</strong><div className="small muted">{help}</div></div>{canEdit && <button className="btn small right" onClick={() => setAdding(!adding)}>{adding ? 'Cancel' : 'Add'}</button>}</div>
      {adding && <form className="row" style={{ marginTop: 10 }} onSubmit={(e) => { e.preventDefault(); act(async () => { await api.post(url, form); setForm({ name: '', description: '', inverse_name: '' }); setAdding(false) }) }}>
        <input style={{ width: 180 }} placeholder="name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} autoFocus />
        <input style={{ flex: 1, minWidth: 200 }} placeholder="what it means" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
        {kind === 'relation_type' && <input style={{ width: 160 }} placeholder="inverse, e.g. part_of" value={form.inverse_name} onChange={(e) => setForm({ ...form, inverse_name: e.target.value })} />}
        <button className="btn small primary" disabled={!form.name.trim()}>Add</button></form>}
      <table style={{ marginTop: 10 }}><thead><tr><th>Name</th><th>Meaning</th>{kind === 'relation_type' && <th>Inverse</th>}<th style={{ textAlign: 'right' }}>Used</th>{canEdit && <th />}</tr></thead><tbody>
        {rows.map((r) => (
          <tr key={r.name}>
            <td><b>{r.name}</b> {r.status === 'pending' && <Pill tone="warn">proposed</Pill>}{r.status === 'rejected' && <Pill tone="bad">rejected</Pill>}</td>
            <td>{edit === r.name ? <form className="row" onSubmit={(e) => { e.preventDefault(); act(async () => { await api.patch(`${url}?name=${encodeURIComponent(r.name)}`, kind === 'topic_type' ? { description: form.description, template: form.template || '' } : { description: form.description }); setEdit(null) }) }}
                style={{ flexDirection: 'column', alignItems: 'stretch' }}>
              <textarea rows={3} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} autoFocus />
              {kind === 'topic_type' && <><span className="small muted">Page template the topic writer follows (Markdown headings with hints; empty: the default Overview / Key Facts / Related Topics / Sources)</span>
                <textarea rows={8} style={{ fontFamily: 'var(--mono, monospace)', fontSize: 12.5 }} value={form.template || ''} onChange={(e) => setForm({ ...form, template: e.target.value })} /></>}
              <div className="row"><button className="btn small primary">Save</button><button type="button" className="btn small" onClick={() => setEdit(null)}>Cancel</button></div></form>
              : <span className="small">{r.description || <span className="muted">no description</span>}{kind === 'topic_type' && r.template && <details className="small muted" style={{ marginTop: 4 }}><summary>Page template</summary><pre style={{ whiteSpace: 'pre-wrap', margin: '4px 0 0' }}>{r.template}</pre></details>}</span>}</td>
            {kind === 'relation_type' && <td className="small muted">{r.inverse_name}</td>}
            <td style={{ textAlign: 'right' }}>{r.uses}</td>
            {canEdit && <td style={{ whiteSpace: 'nowrap' }}>
              {merge === r.name ? <span className="row" style={{ gap: 4, flexWrap: 'nowrap' }}><select style={{ width: 'auto' }} defaultValue="" onChange={(e) => e.target.value && act(async () => { await api.post(`${url}/merge?name=${encodeURIComponent(r.name)}`, { into: e.target.value }); setMerge(null) })}><option value="">into…</option>{targets(r.name).map((t) => <option key={t.name}>{t.name}</option>)}</select><button className="btn small" onClick={() => setMerge(null)}>×</button></span>
                : <>
                  <button className="btn small" onClick={() => { setEdit(r.name); setForm({ ...form, description: r.description, template: r.template || '' }) }}>Edit</button>{' '}
                  {!protectedNames.includes(r.name) && (r.uses ? <button className="btn small" title="Everything of this kind becomes another kind" onClick={() => setMerge(r.name)}>Merge…</button>
                    : <button className="btn small danger" onClick={() => confirm(`Delete “${r.name}”?`) && act(() => api.del(`${url}?name=${encodeURIComponent(r.name)}`))}>Delete</button>)}</>}
            </td>}</tr>))}
      </tbody></table>
    </div>
  )
}

export default function VocabularyPage() {
  const [params, setParams] = useSearchParams()
  const [cols, setCols] = useState(null), [v, setV] = useState(null), [err, setErr] = useState('')
  const cid = params.get('c'), base = `/api/library/collections/${cid}/vocabulary`
  useEffect(() => { api.get('/api/library/collections').then((d) => { const mine = d.collections.filter((c) => c.level); setCols(mine); if (!cid && mine[0]) setParams({ c: (mine.find((c) => c.level === 'manage') || mine[0]).id }, { replace: true }) }).catch((e) => setErr(e.message)) }, [])
  const load = () => cid && api.get(base).then(setV).catch((e) => setErr(e.message))
  useEffect(() => { setV(null); load() }, [cid])
  const canEdit = cols?.find((c) => c.id === cid)?.level === 'manage'
  const pending = (v?.proposals || []).filter((p) => p.status === 'pending')
  const decide = async (id, approve) => { try { await api.post(`${base}/proposals/${id}`, { approve }); load() } catch (e) { setErr(e.message) } }
  return (
    <>
      <div className="row" style={{ marginBottom: 14 }}>
        <div><h1>Vocabulary</h1><div className="muted">The kinds of topic and of relation this collection uses. They belong to the collection alone: nothing here changes the data model or any other collection.</div></div>
        {cols?.length > 0 && <select className="right" style={{ width: 'auto' }} value={cid || ''} onChange={(e) => setParams({ c: e.target.value })}>{cols.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select>}
      </div>
      {err && <div className="banner bad">{err}</div>}
      {cols && !canEdit && cid && <p className="small muted">You can read this; changing it needs the manager role on the collection.</p>}
      {pending.length > 0 && <div className="banner" style={{ marginBottom: 14 }}><b>Proposed while reading documents</b> (until you decide, the closest existing kind is used)
        {pending.map((p) => <div key={p.id} className="row small" style={{ marginTop: 6 }}><Pill>{p.kind === 'topic_type' ? 'topic type' : 'relation'}</Pill><b>{p.name}</b><span className="muted">{p.reason || p.description}</span>
          {canEdit && <><button className="btn small primary right" onClick={() => decide(p.id, true)}>Accept</button><button className="btn small" onClick={() => decide(p.id, false)}>Reject</button></>}</div>)}</div>}
      {v && <>
        <Kinds title="Topic types" help="What kind of subject a topic is about. The model's own entities are separate: a topic is linked to those on its page." kind="topic_type" rows={v.topic_types} protectedNames={v.protected} canEdit={canEdit} base={base} reload={load} setErr={setErr} />
        <Kinds title="Relations between topics" help="How one topic relates to another. A relation can have an inverse (part of / includes)." kind="relation_type" rows={v.relation_types} protectedNames={v.protected} canEdit={canEdit} base={base} reload={load} setErr={setErr} />
      </>}
    </>
  )
}
