import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { can, useStudio } from '../context.jsx'
import { Pill } from './ui.jsx'

const upload = async (file, params) => {
  const r = await fetch('/api/admin/import/excel?' + new URLSearchParams(params), { method: 'POST', credentials: 'same-origin', headers: { 'content-type': 'application/octet-stream' }, body: file })
  const j = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : r.statusText)
  return j
}

export default function ExcelCard({ hasDraft, onCreated }) {
  const { user, status, refresh, setVersion } = useStudio()
  const admin = user.role === 'admin', canUpload = can(user, 'modeller')
  const input = useRef(null)
  const [backups, setBackups] = useState([])
  useEffect(() => { if (admin) api.get('/api/admin/backups').then(setBackups).catch(() => {}) }, [admin, status?.published])
  const [over, setOver] = useState(false), [file, setFile] = useState(null), [rep, setRep] = useState(null), [busy, setBusy] = useState(false), [err, setErr] = useState(''), [replace, setReplace] = useState(false), [done, setDone] = useState(null)
  const check = async (f) => {
    if (input.current) input.current.value = ''                     // the same file can be chosen again after editing it
    setFile(f); setRep(null); setErr(''); setDone(null)
    if (!f) return
    setBusy(true)
    try { setRep(await upload(f, { dry_run: 'true' })) } catch (e) { setErr(e.message) }
    setBusy(false)
  }
  const create = async () => {
    setBusy(true); setErr('')
    try {
      const r = await upload(file, { dry_run: 'false', replace_draft: String(replace) })
      setDone(r); setRep(null); setFile(null); if (input.current) input.current.value = ''
      await refresh(); setVersion('draft'); onCreated?.()
    } catch (e) { setErr(e.message) }
    setBusy(false)
  }
  const c = rep?.counts
  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="row"><b>Excel</b><span className="muted small">Build or edit the model in a spreadsheet: one sheet per concept.</span></div>
      <div className="row" style={{ marginTop: 10 }}>
        <a className="btn" href="/api/template.xlsx">Download blank template</a>
        {status?.published ? <a className="btn" href="/api/export.xlsx?version=published">Export published model</a> : <button className="btn" disabled>Export published model</button>}
        {hasDraft && <a className="btn" href="/api/export.xlsx?version=draft">Export draft</a>}
      </div>
      {admin && backups.length > 0 && <div className="small" style={{ marginTop: 10 }}><span className="muted">Backups (saved automatically when the ontology is emptied): </span>
        {backups.slice(0, 6).map((b) => <a key={b.name} className="btn small" style={{ marginRight: 6 }} href={'/api/admin/backups/' + b.name}>{b.name}</a>)}</div>}
      {canUpload && <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid var(--line)' }}>
        <label className={'dropzone' + (over ? ' over' : '')} onDragOver={(e) => { e.preventDefault(); setOver(true) }} onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); const f = e.dataTransfer.files?.[0]; if (f) check(f) }}>
          <input ref={input} type="file" accept=".xlsx" onChange={(e) => check(e.target.files[0])} />
          <span className="icon">📄</span>
          <span><b>{busy ? 'Checking…' : file ? file.name : 'Drop your Excel file here, or click to choose one'}</b>
            <div className="small muted">{file ? 'Drop or click to use another file' : '.xlsx, filled from the template or an export'}</div></span>
        </label>
        <p className="small muted" style={{ margin: '6px 0 0' }}>The file is checked first. Nothing changes until you confirm, and the result is a <b>draft</b> you review and publish.</p>
        {err && <div className="banner bad" style={{ marginTop: 8 }}>{err}</div>}
        {rep && <div style={{ marginTop: 10 }}>
          {c && <div className="row" style={{ gap: 6 }}>{Object.entries(c).map(([k, v]) => <Pill key={k}>{v} {k}</Pill>)}</div>}
          {rep.errors.length > 0 ? <>
            <div className="banner bad" style={{ marginTop: 8 }}><b>{rep.errors.length} problem{rep.errors.length > 1 ? 's' : ''} to fix</b> before this can become a draft.</div>
            <Problems rows={rep.errors} /></> : <div className="banner ok" style={{ marginTop: 8 }}>No problems found{rep.warnings.length ? `, ${rep.warnings.length} note${rep.warnings.length > 1 ? 's' : ''} below` : ''}.</div>}
          {rep.warnings.length > 0 && <><div className="small muted" style={{ marginTop: 8 }}>Notes (they do not block):</div><Problems rows={rep.warnings} /></>}
          {rep.errors.length === 0 && <div className="row" style={{ marginTop: 10 }}>
            {hasDraft && <label className="small"><input type="checkbox" className="switch" style={{ width: 'auto', marginRight: 6 }} checked={replace} onChange={(e) => setReplace(e.target.checked)} />Replace the existing draft (its edits are lost)</label>}
            <button className="btn primary" disabled={busy || (hasDraft && !replace)} onClick={create}>Create draft from this file</button></div>}
        </div>}
        {done && <div className="banner ok" style={{ marginTop: 8 }}>Draft created from the file{done.sql_warnings?.length ? `; ${done.sql_warnings.length} filter or check SQL statement(s) do not run on the connected database (advice, see Check)` : ''}. Review the changes, then publish.</div>}
      </div>}
    </div>
  )
}

function Problems({ rows }) {
  return <table style={{ marginTop: 6 }}><thead><tr><th style={{ width: 120 }}>Sheet</th><th style={{ width: 60 }}>Row</th><th style={{ width: 160 }}>Column</th><th>Problem</th></tr></thead><tbody>
    {rows.slice(0, 200).map((e, i) => <tr key={i}><td>{e.sheet || ''}</td><td>{e.row || ''}</td><td className="mono small">{e.column || ''}</td><td>{e.message}</td></tr>)}
    {rows.length > 200 && <tr><td colSpan="4" className="muted">…and {rows.length - 200} more.</td></tr>}</tbody></table>
}
