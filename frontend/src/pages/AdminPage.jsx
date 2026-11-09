import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useStudio } from '../context.jsx'
import { Pill, Tabs } from '../components/ui.jsx'

const LEVELS = [['confidential', 'Confidential', 'financial and other business-sensitive values'], ['personal', 'Personal', 'definitions of names, emails, mobiles'],
  ['restricted', 'Restricted', 'identity numbers, credentials and similar'], ['data', 'Can query data', 'plan_query and query_data on the database (rows follow the same switches)'],
  ['sources', 'Show sources', 'where each fact comes from in the document tools: citation markers, the documents behind a topic, links into the app. Turn it off for a customer-facing agent']]
const DEF_ENT = { confidential: true, personal: true, restricted: false, data: false, sources: true }      // tokens: structure only until granted
const DEF_USER = { ...DEF_ENT, data: true }

function Ent({ value, onChange, disabled }) {
  value = value || {}                                  // a row without entitlements shows everything unchecked instead of crashing the page
  return (
    <div className="row" style={{ gap: 10 }}>
      {LEVELS.map(([k, label, hint]) => (
        <label key={k} title={hint} style={{ display: 'flex', gap: 4, alignItems: 'center', cursor: disabled ? 'default' : 'pointer', color: k === 'restricted' && value[k] ? 'var(--bad)' : undefined, fontWeight: k === 'restricted' && value[k] ? 600 : 400 }}>
          <input type="checkbox" style={{ width: 'auto' }} checked={!!value[k]} disabled={disabled} onChange={(e) => onChange({ ...value, [k]: e.target.checked })} />{label}
        </label>))}
    </div>
  )
}

export default function AdminPage() {
  const { user, status, refresh } = useStudio()
  const admin = user.role === 'admin'
  const [tokens, setTokens] = useState([]), [users, setUsers] = useState([])
  const [tname, setTname] = useState(''), [tent, setTent] = useState(DEF_ENT), [newTok, setNewTok] = useState(''), [nu, setNu] = useState({ username: '', password: '', role: 'modeller', extra_roles: [], entitlements: DEF_USER })
  const load = () => {
    if (admin) {
      api.get('/api/admin/tokens').then((t) => setTokens(t.map((x) => ({ ...x, entitlements: { ...DEF_ENT, ...(x.entitlements || {}) } }))))
      api.get('/api/admin/users').then((u) => setUsers(u.map((x) => ({ ...x, entitlements: { ...DEF_USER, ...(x.entitlements || {}) } }))))
    }
  }
  useEffect(load, [])
  const url = location.origin + '/mcp'
  const { tab: t } = useParams(), nav = useNavigate()
  const tabs = [{ id: 'agents', label: 'Tokens & MCP' }, ...(admin ? [{ id: 'users', label: 'Users' }] : [])]
  const tab = tabs.find((x) => x.id === t) ? t : 'agents'

  return (
    <>
      <h1>{admin ? 'Users & access' : 'Connect agents'}</h1>
      {tabs.length > 1 && <Tabs current={tab} onPick={(id) => nav('/admin/' + id)} tabs={tabs} />}
      {tab === 'users' && admin && <div className="card" style={{ margin: '16px 0' }}><b>Roles</b> <span className="muted small">Roles: viewer reads · modeller edits drafts · publisher publishes · admin manages users, connectors and settings. The four-eyes rule is on the <Link to="/settings">Settings</Link> page.</span></div>}

      {tab === 'agents' && <>
      <h2>MCP server for AI agents</h2>
      <div className="card">
        <p style={{ marginTop: 0 }}>Endpoint (streamable HTTP): <code>{url}</code> — send <code>Authorization: Bearer &lt;token&gt;</code>.</p>
        <p className="small muted" style={{ margin: '4px 0' }}><b>Entitlements travel with the token.</b> The agent can call <code>get_access</code> to see what it may see; anything not granted is absent from every answer (search, entities, fields, measures, knowledge). Restricted is off unless you grant it explicitly, and every grant is logged.</p>
        <p className="small muted" style={{ margin: '4px 0' }}>Six tools: get_access, search, describe, join_path, plan_query, query_data. Read-only, published version only; a restricted attribute is never returned to a credential without that entitlement.</p>
        <pre className="mono small card" style={{ margin: '8px 0', overflow: 'auto' }}>{`claude mcp add --transport http curltale ${url} --header "Authorization: Bearer <token>"`}</pre>
        {admin ? <>
          <table><thead><tr><th>Token</th><th>May see (besides internal structure)</th><th>Last used</th><th /></tr></thead><tbody>
            {tokens.map((t) => <tr key={t.id}><td>{t.name}<div className="small muted">{new Date(t.created_at).toLocaleDateString()}</div></td>
              <td><Ent value={t.entitlements} onChange={async (e) => { if (e.restricted && !t.entitlements.restricted && !confirm(`Allow "${t.name}" to see restricted data (identity numbers, credentials and similar)?`)) return; await api.put(`/api/admin/tokens/${t.id}/entitlements`, { entitlements: e }); load() }} /></td>
              <td className="small">{t.last_used ? new Date(t.last_used).toLocaleString() : 'never'}</td>
              <td><button className="btn small danger" onClick={async () => { await api.del('/api/admin/tokens/' + t.id); load() }}>Revoke</button></td></tr>)}
            {!tokens.length && <tr><td colSpan="4" className="muted">No tokens yet.</td></tr>}</tbody></table>
          <div className="row" style={{ marginTop: 10 }}><input style={{ width: 240 }} placeholder="Token name, e.g. claude-desktop" value={tname} onChange={(e) => setTname(e.target.value)} />
            <Ent value={tent} onChange={setTent} />
            <button className="btn primary" disabled={!tname} onClick={async () => { if (tent.restricted && !confirm('This token will be able to see restricted identity data definitions. Continue?')) return; const r = await api.post('/api/admin/tokens', { name: tname, entitlements: tent }); setNewTok(r.token); setTname(''); setTent(DEF_ENT); load() }}>Create token</button></div>
          {newTok && <div className="banner ok" style={{ marginTop: 10 }}>Copy it now, it is shown once: <code>{newTok}</code></div>}
        </> : <p className="muted small">Only admins manage tokens.</p>}
      </div>
      </>}

      {tab === 'users' && admin && <><h2>Users</h2><div className="card">
        <p className="small muted" style={{ marginTop: 0 }}>A user's entitlements apply to what Alfred can see when that user chats with it. Editing the model in the UI is not affected.</p>
        <table><thead><tr><th>User</th><th>Role</th><th>Alfred may see</th></tr></thead><tbody>{users.map((u) => <tr key={u.username}><td>{u.username}</td><td>{u.username === user.username ? <Pill>{u.role}</Pill> : <select style={{ width: 120 }} value={u.role} onChange={async (e) => { try { await api.put(`/api/admin/users/${u.username}/roles`, { role: e.target.value, extra_roles: u.extra_roles }); load() } catch (x) { alert(x.message) } }}>{['viewer', 'modeller', 'publisher', 'admin'].map((r) => <option key={r}>{r}</option>)}</select>} <label className="small" title="may ingest, edit and approve documents in the collections where granted write"><input type="checkbox" style={{ width: 'auto' }} checked={u.extra_roles.includes('editor') || u.role === 'admin'} disabled={u.role === 'admin'} onChange={async (e) => { await api.put(`/api/admin/users/${u.username}/roles`, { role: u.role, extra_roles: e.target.checked ? ['editor'] : [] }); load() }} /> editor</label>{u.must_change && <> <Pill tone="warn">must change password</Pill></>}</td>
          <td><Ent value={u.entitlements} onChange={async (e) => { if (e.restricted && !u.entitlements.restricted && !confirm(`Allow Alfred to show restricted identity data definitions to ${u.username}?`)) return; await api.put(`/api/admin/users/${u.username}/entitlements`, { entitlements: e }); load() }} /></td></tr>)}</tbody></table>
        <div className="row" style={{ marginTop: 10 }}>
          <input style={{ width: 160 }} placeholder="username" value={nu.username} onChange={(e) => setNu({ ...nu, username: e.target.value })} />
          <input style={{ width: 180 }} type="password" placeholder="password (12+)" value={nu.password} onChange={(e) => setNu({ ...nu, password: e.target.value })} />
          <select style={{ width: 130 }} value={nu.role} onChange={(e) => setNu({ ...nu, role: e.target.value })} title="viewer reads · modeller edits drafts · publisher publishes · admin manages. Editor (tick) is separate: documents."><option>viewer</option><option>modeller</option><option>publisher</option><option>admin</option></select>
          <label className="small" title="may ingest, edit and approve documents"><input type="checkbox" style={{ width: 'auto' }} checked={nu.extra_roles.includes('editor')} onChange={(e) => setNu({ ...nu, extra_roles: e.target.checked ? ['editor'] : [] })} /> editor</label>
          <Ent value={nu.entitlements} onChange={(e) => setNu({ ...nu, entitlements: e })} />
          <button className="btn primary" onClick={async () => { try { await api.post('/api/admin/users', nu); setNu({ username: '', password: '', role: 'modeller', extra_roles: [], entitlements: DEF_USER }); load() } catch (e) { alert(e.message) } }}>Add / update user</button></div></div></>}

    </>
  )
}

export function Danger() {
  const [open, setOpen] = useState(false), [text, setText] = useState(''), [demo, setDemo] = useState(true), [busy, setBusy] = useState(false), [res, setRes] = useState(null), [err, setErr] = useState('')
  const { refresh, setVersion } = useStudio()
  const go = async () => {
    setBusy(true); setErr('')
    try { setRes(await api.post('/api/admin/reset-ontology', { confirm: text, remove_demo: demo })); setOpen(false); setText(''); setVersion('published'); await refresh() } catch (e) { setErr(e.message) }
    setBusy(false)
  }
  return (
    <div className="card" style={{ borderColor: 'var(--bad, #b44)', marginBottom: 16 }}>
      <div className="row"><b>Start from zero</b><span className="muted small">Empties the ontology so you can import one from scratch.</span>
        {!open && <button className="btn danger small right" onClick={() => setOpen(true)}>Empty the ontology…</button>}</div>
      {res && <div className="banner ok" style={{ marginTop: 10 }}>Done: {res.versions_deleted} version(s) deleted{res.demo_database_removed ? ', built-in demo database removed' : ''}.
        {res.backup && <> Backup of the old ontology: <a href={'/api/admin/backups/' + res.backup}>{res.backup}</a> (download it now if you want to import it again).</>}
        {' '}Next: <Link to="/versions">Versions page</Link> → upload an Excel file.</div>}
      {open && <div style={{ marginTop: 10 }}>
        <p className="small" style={{ margin: '0 0 8px' }}>This deletes <b>every version</b> of the ontology (entities, fields, relationships, examples, search index). A backup is saved as an Excel file first. <b>Kept</b>: connectors, users, tokens, the activity log.</p>
        <label className="small" style={{ display: 'block', marginBottom: 8 }}><input type="checkbox" className="switch" style={{ width: 'auto', marginRight: 6 }} checked={demo} onChange={(e) => setDemo(e.target.checked)} />Also remove the built-in demo database (it can be rebuilt from a published ontology)</label>
        <div className="row"><input style={{ width: 220 }} placeholder="Type EMPTY to confirm" value={text} onChange={(e) => setText(e.target.value)} />
          <button className="btn danger" disabled={busy || text !== 'EMPTY'} onClick={go}>{busy ? 'Working…' : 'Empty the ontology'}</button><button className="btn" onClick={() => { setOpen(false); setText('') }}>Cancel</button></div>
        {err && <div className="banner bad" style={{ marginTop: 8 }}>{err}</div>}</div>}
    </div>
  )
}

