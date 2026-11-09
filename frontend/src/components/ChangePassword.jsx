import { useState } from 'react'
import { api } from '../api.js'

export default function ChangePassword({ user, forced, onDone, onCancel }) {
  const [cur, setCur] = useState(''), [pw, setPw] = useState(''), [pw2, setPw2] = useState(''), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const go = async (e) => {
    e.preventDefault(); setErr('')
    if (pw !== pw2) { setErr('the two new passwords differ'); return }
    setBusy(true)
    try { await api.post('/api/me/password', { current: cur, new: pw }); onDone() } catch (x) { setErr(x.message); setBusy(false) }
  }
  return (
    <form className={forced ? 'card login' : ''} onSubmit={go}>
      {forced && <><h1>Choose a new password</h1><p className="muted">{user?.username}, your password must be changed before you continue: it was set by an administrator, or it is shorter than the 12 characters now required.</p></>}
      <div className="field"><label>Current password</label><input type="password" value={cur} onChange={(e) => setCur(e.target.value)} autoFocus autoComplete="current-password" /></div>
      <div className="field"><label>New password (at least 12 characters)</label><input type="password" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="new-password" /></div>
      <div className="field"><label>New password again</label><input type="password" value={pw2} onChange={(e) => setPw2(e.target.value)} autoComplete="new-password" /></div>
      <p className="small muted">Changing it signs you out everywhere else.</p>
      {err && <div className="banner bad">{err}</div>}
      <div className="row" style={{ justifyContent: 'flex-end' }}>{onCancel && <button type="button" className="btn" onClick={onCancel}>Cancel</button>}
        <button className="btn primary" disabled={busy || !cur || !pw}>{busy ? 'Saving…' : 'Change password'}</button></div>
    </form>
  )
}
