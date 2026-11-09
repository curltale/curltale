import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useStudio } from '../context.jsx'
import { Modal, Pill } from '../components/ui.jsx'

const USAGE = { public: ['Public', 'ok', 'May be shared with customers.'], internal: ['Internal', 'warn', "For the agent's own understanding. Do not quote it or refer to it with customers."] }
const LEVEL_TEXT = { manage: 'Manage', write: 'Write', read: 'Read' }
const LEVEL_HELP = { read: 'search and read', write: 'also ingest, edit and approve (needs the editor role)', manage: 'also grant access, change settings, delete' }

export const UsagePill = ({ usage }) => <span className={'pill ' + USAGE[usage][1]} title={USAGE[usage][2]}>{USAGE[usage][0]}</span>

function CollectionForm({ initial, admin, onSave, onClose }) {
  const [f, setF] = useState({ name: '', description: '', usage: 'internal', allow_ai: true, agents_may_read: true, manager: '', ...initial }), [err, setErr] = useState('')
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })
  return (
    <Modal title={initial ? 'Collection settings' : 'New collection'} onClose={onClose}>
      <div className="field"><label>Name</label><input value={f.name} onChange={set('name')} autoFocus /></div>
      <div className="field"><label>Description</label><textarea value={f.description} onChange={set('description')} placeholder="What belongs here and who it is for" /></div>
      <div className="field"><label>How agents should treat it</label>
        <select value={f.usage} onChange={set('usage')}><option value="internal">Internal: for the agent's own understanding, do not quote it to customers</option><option value="public">Public: may be shared with customers</option></select>
        <div className="small muted" style={{ marginTop: 4 }}>This is guidance returned with every result, not an access rule. Access is decided by the grants below.</div></div>
      <div className="field"><label>AI and agents</label>
        <label className="small" style={{ display: 'flex', gap: 6, alignItems: 'center' }}><input type="checkbox" className="switch" style={{ width: 'auto' }} checked={f.allow_ai} onChange={(e) => setF({ ...f, allow_ai: e.target.checked })} />Documents may be sent to the AI model (to write and check pages, and to read scanned pages)</label>
        <label className="small" style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 4 }}><input type="checkbox" className="switch" style={{ width: 'auto' }} checked={f.agents_may_read} onChange={(e) => setF({ ...f, agents_may_read: e.target.checked })} />Agents (Alfred and MCP clients) may read this collection</label>
        <div className="small muted" style={{ marginTop: 4 }}>Turn the first off for documents that must not leave this server: they are still stored, split and searchable, but no model ever sees them.</div></div>
      {!initial && admin && <div className="field"><label>First manager (username)</label><input value={f.manager} onChange={set('manager')} placeholder="optional; you can grant later" /></div>}
      {err && <div className="banner bad">{err}</div>}
      <div className="row" style={{ justifyContent: 'flex-end' }}><button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn primary" onClick={async () => { try { await onSave(f) } catch (e) { setErr(e.message) } }}>{initial ? 'Save' : 'Create collection'}</button></div>
    </Modal>
  )
}

function Access({ col, admin, reload }) {
  const [rows, setRows] = useState(null), [tokens, setTokens] = useState([]), [g, setG] = useState({ who: '', level: 'read' }), [err, setErr] = useState('')
  const load = () => api.get(`/api/library/collections/${col.id}/grants`).then(setRows).catch((e) => setErr(e.message))
  useEffect(() => { load(); if (admin) api.get('/api/admin/tokens').then(setTokens).catch(() => {}) }, [col.id])
  const add = async () => {
    setErr('')
    try {
      const tok = g.who.startsWith('token:') ? Number(g.who.slice(6)) : null
      await api.put(`/api/library/collections/${col.id}/grants`, tok ? { token_id: tok, level: 'read' } : { username: g.who.trim(), level: g.level })
      setG({ who: '', level: 'read' }); load()
    } catch (e) { setErr(e.message) }
  }
  return (
    <div className="card" style={{ marginTop: 22 }}>
      <div className="row"><h2 style={{ margin: 0 }}>Access to {col.name}</h2></div>
      {err && <div className="banner bad" style={{ marginTop: 10 }}>{err}</div>}
      <div style={{ overflowX: 'auto', marginTop: 10 }}><table><thead><tr><th>Who</th><th>Kind</th><th>Level</th><th>Needs role</th><th>Granted</th><th /></tr></thead><tbody>
        {(rows || []).map((r) => (
          <tr key={r.id}><td>{r.who}</td><td>{r.kind === 'user' ? 'User' : 'API token'}</td>
            <td>{r.kind === 'user' ? <select style={{ width: 110 }} value={r.level} onChange={async (e) => { try { await api.put(`/api/library/collections/${col.id}/grants`, { username: r.who, level: e.target.value }); load() } catch (x) { setErr(x.message) } }}>{Object.keys(LEVEL_TEXT).map((l) => <option key={l} value={l} title={LEVEL_HELP[l]}>{LEVEL_TEXT[l]}</option>)}</select> : <Pill>Read</Pill>}</td>
            <td className="muted small">{r.needs_role}{r.kind === 'user' && r.level !== 'read' && !r.has_role && <> <Pill tone="warn">not an editor yet</Pill></>}</td>
            <td className="muted small">{new Date(r.granted_at).toLocaleDateString()} by {r.granted_by}</td>
            <td><button className="btn small danger" onClick={async () => { if (!confirm(`Remove ${r.who}'s access to ${col.name}?`)) return; try { await api.del(`/api/library/collections/${col.id}/grants/${r.id}`); load() } catch (x) { setErr(x.message) } }}>Remove</button></td></tr>))}
        {rows && !rows.length && <tr><td colSpan="6" className="muted">Nobody yet.</td></tr>}
      </tbody></table></div>
      <div className="row" style={{ marginTop: 10 }}>
        <input style={{ width: 200 }} list="grant-who" placeholder="username or token" value={g.who} onChange={(e) => setG({ ...g, who: e.target.value })} />
        <datalist id="grant-who">{tokens.map((t) => <option key={t.id} value={`token:${t.id}`}>{t.name} (API token)</option>)}</datalist>
        <select style={{ width: 120 }} value={g.level} disabled={g.who.startsWith('token:')} onChange={(e) => setG({ ...g, level: e.target.value })}>{Object.keys(LEVEL_TEXT).map((l) => <option key={l} value={l}>{LEVEL_TEXT[l]}</option>)}</select>
        <button className="btn primary" disabled={!g.who.trim()} onClick={add}>Add access</button>
      </div>
      <div className="banner" style={{ marginTop: 12, background: 'var(--accent-soft)', color: 'var(--accent)' }}><b>Customer-facing agents:</b> give their token access to public collections only. The Public / Internal tag tells an agent how to treat a collection, but only a grant decides what it can read.</div>
    </div>
  )
}

export default function CollectionsPage() {
  const { user, status } = useStudio()
  const [data, setData] = useState(null), [sel, setSel] = useState(null), [form, setForm] = useState(null), [err, setErr] = useState('')
  const admin = user.role === 'admin'
  const load = () => api.get('/api/library/collections').then((d) => { setData(d.collections); setSel((s) => s && d.collections.find((c) => c.id === s) ? s : (d.collections.find((c) => c.level === 'manage') || {}).id || null) }).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])
  if (status && status.library === false) return <p className="muted">The Library is switched off (Settings, Library).</p>
  const cur = data?.find((c) => c.id === sel)
  return (
    <>
      <div className="row" style={{ marginBottom: 14 }}>
        <div><h1>Collections</h1><div className="muted">Each collection is isolated: its documents, topics and links are visible only to the people and tokens it is granted to.</div></div>
        {admin && <button className="btn primary right" onClick={() => setForm({ create: true })}>New collection</button>}
      </div>
      {err && <div className="banner bad">{err}</div>}
      <div className="grid cols-2">
        {(data || []).map((c) => c.level ? (
          <div key={c.id} className="card" style={{ cursor: 'pointer', borderColor: c.id === sel ? 'var(--accent)' : undefined }} onClick={() => setSel(c.id)}>
            <div className="row" style={{ gap: 8 }}><strong style={{ fontSize: 15 }}>{c.name}</strong><UsagePill usage={c.usage} /><span className="right"><Pill tone={c.level === 'manage' ? 'accent' : ''}>{LEVEL_TEXT[c.level]}</Pill></span></div>
            <p className="muted small" style={{ margin: '6px 0 0' }}>{c.description || 'No description.'}</p>
            {(!c.allow_ai || !c.agents_may_read) && <div className="row" style={{ marginTop: 6 }}>{!c.allow_ai && <Pill tone="warn">AI off</Pill>}{!c.agents_may_read && <Pill tone="warn">Agents blocked</Pill>}</div>}
            {c.level === 'manage' && <div className="row" style={{ marginTop: 8 }}><button className="btn small" onClick={(e) => { e.stopPropagation(); setForm({ edit: c }) }}>Settings</button>
              <a className="btn small" href={`/api/library/collections/${c.id}/export`} onClick={(e) => e.stopPropagation()} title="Download the whole collection as one zip: originals, pages, topics with their evidence, vocabulary">Export</a>
              <button className="btn small danger" onClick={async (e) => { e.stopPropagation(); if (!confirm(`Delete ${c.name} and everything in it? This cannot be undone.`)) return; try { await api.del(`/api/library/collections/${c.id}`); load() } catch (x) { setErr(x.message) } }}>Delete</button></div>}
          </div>) : (
          <div key={c.id} className="card" style={{ background: 'var(--panel-2)', boxShadow: 'none', borderStyle: 'dashed' }}>
            <div className="row" style={{ gap: 8 }}><strong style={{ fontSize: 15 }}>{c.name}</strong><UsagePill usage={c.usage} /><span className="right"><Pill>No access</Pill></span></div>
            <p className="muted small" style={{ margin: '6px 0 8px' }}>You see this as an admin. Its content and access list stay hidden until you grant yourself access, which is logged.</p>
            <button className="btn small" onClick={async () => { try { await api.post(`/api/library/collections/${c.id}/self-grant`, { level: 'manage' }); load() } catch (x) { setErr(x.message) } }}>Grant myself access</button>
          </div>))}
        {data && !data.length && <p className="muted">{admin ? 'No collections yet. Create one, then give a manager access.' : 'You have no access to any collection yet. Ask an administrator.'}</p>}
      </div>
      {cur?.level === 'manage' && <Access col={cur} admin={admin} />}
      {form?.create && <CollectionForm admin={admin} onClose={() => setForm(null)} onSave={async (f) => { await api.post('/api/library/collections', { ...f, manager: f.manager || null }); setForm(null); load() }} />}
      {form?.edit && <CollectionForm initial={form.edit} onClose={() => setForm(null)} onSave={async (f) => { await api.patch(`/api/library/collections/${form.edit.id}`, { name: f.name, description: f.description, usage: f.usage, allow_ai: f.allow_ai, agents_may_read: f.agents_may_read }); setForm(null); load() }} />}
    </>
  )
}
