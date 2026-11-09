import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useStudio } from '../context.jsx'
import { useNavigate, useParams } from 'react-router-dom'
import { Pill, Switch, Tabs } from '../components/ui.jsx'
import ConnectorsPage from './ConnectorsPage.jsx'
import Maintenance from './MaintenancePage.jsx'

// tab id, label, the settings group it shows (the groups are named in settings.py)
const TABS = [['general', 'General', 'General'], ['ai', 'AI', 'AI'], ['ontology', 'Ontology & data', 'Ontology & data'], ['library', 'Library', 'Library'],
  ['web', 'Web capture', 'Web capture'], ['storage', 'Storage', 'Storage'], ['limits', 'Limits', 'Limits'], ['connectors', 'Connectors', null], ['maintenance', 'Maintenance', null]]
const SOURCE = { page: ['set here', 'accent'], env: ['from .env', ''], default: ['default', ''] }

// Settings that apply on the next request. What the app needs to start, the deployment itself (proxies, ports) and the embedding model stay in .env.
export default function SettingsPage() {
  const { user } = useStudio()
  const [data, setData] = useState(null), [err, setErr] = useState(''), [saved, setSaved] = useState('')
  useEffect(() => { if (user.role === 'admin') api.get('/api/admin/settings').then(setData).catch((e) => setErr(e.message)) }, [])
  if (user.role !== 'admin') return <p className="muted">Admins only.</p>
  const done = (name) => (d) => { setData(d); setErr(''); setSaved(name); setTimeout(() => setSaved(''), 2000) }
  const save = (name, value) => api.put('/api/admin/settings', { name, value }).then(done(name)).catch((e) => setErr(e.message))
  const reset = (name) => api.del('/api/admin/settings/' + name).then(done(name)).catch((e) => setErr(e.message))
  const { tab: t } = useParams(), nav = useNavigate()
  const [find, setFind] = useState('')
  const tab = TABS.find((x) => x[0] === t) ? t : 'general'
  const mine = (g) => (data?.settings || []).filter((s) => s.group === g)
  const f = find.trim().toLowerCase()
  const hits = f ? (data?.settings || []).filter((s) => (s.label + ' ' + s.name + ' ' + (s.help || '')).toLowerCase().includes(f)) : []
  const cur = TABS.find((x) => x[0] === tab)
  const table = (list) => <table style={{ marginTop: 8 }}><tbody>{list.map((s) => <Setting key={s.name} s={s} onSave={save} onReset={reset} saved={saved === s.name} />)}</tbody></table>
  return (
    <>
      <div className="row" style={{ alignItems: 'flex-end' }}>
        <div><h1>Settings</h1>
          <p className="muted" style={{ margin: 0 }}>Changes apply on the next request, with no restart, and go to the audit log. A value set here wins over <span className="mono">.env</span>; reset it to go back.
            The database, secrets for signing, proxies, ports and (for now) the embedding model stay in <span className="mono">.env</span>.</p></div>
        <input className="right" style={{ width: 240 }} type="search" placeholder="Find a setting…" aria-label="Find a setting" value={find} onChange={(e) => setFind(e.target.value)} />
      </div>
      {data?.locked && <div className="banner" style={{ marginTop: 12 }}>Settings are locked (<span className="mono">SETTINGS_LOCKED</span>): they come from <span className="mono">.env</span> only and are shown read-only.</div>}
      {err && <div className="banner bad" style={{ marginTop: 12 }}>{err}</div>}
      {f ? <div className="card" style={{ marginTop: 14 }}><b>{hits.length} found</b>{table(hits)}</div> : <>
        <Tabs current={tab} onPick={(id) => nav('/settings/' + id)} tabs={TABS.map(([id, label, g]) => ({ id, label, badge: g && data ? mine(g).filter((s) => s.source === 'page').length || 0 : 0 }))} />
        {tab === 'connectors' ? <ConnectorsPage embedded /> : tab === 'maintenance' ? <Maintenance /> : !data ? <p className="muted">Loading…</p> : (
          <div className="card" style={{ marginTop: 14 }}>
            <b>{cur[1]}</b>{table(mine(cur[2]))}
            {tab === 'ai' && <LlmTest />}
            {tab === 'storage' && <StorageTest />}
          </div>)}
        <p className="small muted" style={{ marginTop: 8 }}>The number on a tab is how many of its settings were changed on this page.</p>
      </>}
    </>
  )
}

function LlmTest() {
  const [r, setR] = useState(null), [busy, setBusy] = useState(false)
  return (
    <div className="row" style={{ marginTop: 10 }}>
      <button className="btn small" disabled={busy} onClick={async () => { setBusy(true); setR(null); try { setR(await api.post('/api/admin/llm/test')) } catch (e) { setR({ ok: false, error: e.message }) } setBusy(false) }}>{busy ? 'Testing…' : 'Test the endpoint'}</button>
      {r && (r.ok ? <Pill tone="ok">works · {r.model} · {r.ms} ms</Pill> : <span className="small" style={{ color: 'var(--bad)' }}>failed{r.model ? ` (${r.model})` : ''}: {r.error}</span>)}
      <span className="small muted">Tests the saved values: save first.</span>
    </div>
  )
}

function StorageTest() {
  const [r, setR] = useState(null), [busy, setBusy] = useState(false)
  return (
    <div className="row" style={{ marginTop: 10 }}>
      <button className="btn small" disabled={busy} onClick={async () => { setBusy(true); setR(null); try { setR(await api.post('/api/admin/storage/test')) } catch (e) { setR({ ok: false, error: e.message }) } setBusy(false) }}>{busy ? 'Testing…' : 'Test the storage'}</button>
      {r && (r.ok ? <Pill tone="ok">{r.detail}</Pill> : <span className="small" style={{ color: 'var(--bad)' }}>failed: {r.error}</span>)}
      <span className="small muted">Tests the saved values: save first.</span>
    </div>
  )
}

function Setting({ s, onSave, onReset, saved }) {
  const [v, setV] = useState(s.type === 'secret' ? '' : s.value ?? '')
  useEffect(() => { setV(s.type === 'secret' ? '' : s.value ?? '') }, [s.value, s.is_set])
  const [label, tone] = SOURCE[s.source] || [s.source, '']
  const changed = s.type === 'secret' ? v !== '' : String(v) !== String(s.value ?? '')
  const submit = (e) => { e.preventDefault(); if (changed) onSave(s.name, s.type === 'int' || s.type === 'float' ? Number(v) : v) }
  return (
    <tr>
      <td style={{ width: '38%' }}><div>{s.label}</div><div className="small muted">{s.help}{s.min != null && ` (${s.min}–${s.max})`}</div><div className="small muted mono">{s.name}</div></td>
      <td>
        {s.type === 'enum'
          ? <select style={{ width: 200 }} disabled={!s.editable} value={s.value} onChange={(e) => onSave(s.name, e.target.value)}>{s.options.map((o) => <option key={o}>{o}</option>)}</select>
          : s.type === 'bool'
          ? <Switch disabled={!s.editable} checked={s.value} onChange={(v) => onSave(s.name, v)}>{s.value ? 'On' : 'Off'}</Switch>
          : <form className="row" onSubmit={submit}>
            <input style={{ width: s.type === 'str' || s.type === 'secret' ? 280 : 120 }} disabled={!s.editable} value={v} autoComplete="off"
              type={s.type === 'secret' ? 'password' : s.type === 'str' ? 'text' : 'number'} step={s.type === 'float' ? 'any' : 1} min={s.min ?? undefined} max={s.max ?? undefined}
              placeholder={s.type === 'secret' ? (s.is_set ? 'set: type a new key to replace it' : 'not set') : ''} onChange={(e) => setV(e.target.value)} />
            {s.editable && <button className="btn small primary" disabled={!changed}>Save</button>}
          </form>}
      </td>
      <td style={{ width: 210 }} className="small">
        <Pill tone={tone}>{label}</Pill>{saved && <span className="muted"> saved</span>}
        {s.source === 'page' && s.editable && <button className="btn small" style={{ marginLeft: 6 }} title={s.env != null ? `Back to the .env value (${s.env})` : 'Back to the default'} onClick={() => onReset(s.name)}>Reset</button>}
        {s.source === 'page' && s.env != null && <div className="muted">.env: <span className="mono">{s.env}</span></div>}
      </td>
    </tr>
  )
}
