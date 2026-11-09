import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, q } from '../api.js'
import { can, useStudio } from '../context.jsx'
import { Modal, Pill } from '../components/ui.jsx'
import ExcelCard from '../components/ExcelCard.jsx'

const short = (v) => { const s = typeof v === 'string' ? v : JSON.stringify(v); return s && s.length > 140 ? s.slice(0, 140) + '…' : s }

export default function VersionsPage() {
  const { status, refresh, setVersion, user } = useStudio()
  const canPublish = can(user, 'publisher'), canEdit = can(user, 'modeller')
  const fourEyesBlocked = status?.four_eyes && (status?.draft_editors || []).includes(user.username)
  const [label, setLabel] = useState(''), [note, setNote] = useState('')
  const [diff, setDiff] = useState(null), [errs, setErrs] = useState(null), [audit, setAudit] = useState([]), [busy, setBusy] = useState(''), [msg, setMsg] = useState(null)
  const [job, setJob] = useState(null)
  const polling = useRef(false)
  const vs = status?.versions || []
  const draft = vs.find((v) => v.status === 'draft'), pub = vs.find((v) => v.status === 'published')
  useEffect(() => { api.get('/api/audit?' + q({ version: draft?.id })).then(setAudit) }, [status])
  // publishing runs in the background (embedding a whole ontology can take minutes): poll it, also when the page is opened while one is running
  const watch = async () => {
    if (polling.current) return
    polling.current = true
    try {
      for (;;) {
        const j = await api.get('/api/versions/publish/status')
        setJob(j)
        if (j.state !== 'running') {
          if (j.state === 'done') { const r = j.result; setVersion('published'); setMsg({ text: `Published. ${r.chunks} search chunks, ${r.newly_embedded} newly embedded, ${r.reused_embeddings} reused${r.missing_embeddings ? `, ${r.missing_embeddings} WITHOUT embeddings (keyword search only for those)` : ''}.` }) }
          if (j.state === 'error') setMsg({ bad: true, text: 'Publishing failed: ' + j.error })
          refresh(); break
        }
        await new Promise((r) => setTimeout(r, 1500))
      }
    } catch (e) { setMsg({ bad: true, text: e.message }) }
    polling.current = false
  }
  useEffect(() => { api.get('/api/versions/publish/status').then((j) => { if (j.state === 'running') watch() }).catch(() => {}) }, [])
  const act = async (name, fn) => { setBusy(name); setMsg(null); try { await fn() } catch (e) { setMsg({ bad: true, text: e.message }) } setBusy(''); refresh() }

  return (
    <>
      <h1>Versions & changes</h1>
      <p className="muted">Agents only see the <b>published</b> version. Edit a <b>draft</b>, review the changes, then publish: search and embeddings are rebuilt and the new version goes live atomically.</p>
      {job?.state === 'running' && <div className="banner" style={{ whiteSpace: 'pre-wrap' }}><b>Publishing…</b> {job.phase}{job.total ? ` (${job.done} of ${job.total})` : ''}.
        <div style={{ height: 6, background: 'var(--line)', borderRadius: 3, marginTop: 8 }}><div style={{ height: 6, borderRadius: 3, background: 'var(--accent)', width: (job.total ? Math.round(100 * job.done / job.total) : 5) + '%', transition: 'width .6s' }} /></div>
        <span className="small muted">Embedding a whole ontology for the first time can take several minutes. You can leave this page: the new version goes live when it is complete, and what was embedded so far is kept if something stops it.</span></div>}
      {msg && <div className={'banner ' + (msg.bad ? 'bad' : 'ok')} style={{ whiteSpace: 'pre-wrap' }}>{msg.text}</div>}

      <div className="card" style={{ marginBottom: 16 }}>
        {draft ? <>
          <div className="row"><b>Draft “{draft.label}”</b><Pill tone="warn">{draft.edits} edits</Pill><span className="muted small">{pub ? `based on ${pub.label}` : 'the first version: nothing is published yet'}</span></div>
          <div className="row" style={{ marginTop: 10 }}>
            <button className="btn" onClick={() => { setVersion('draft') }}>Open draft</button>
            {pub && <button className="btn" onClick={async () => setDiff(await api.get(`/api/versions/diff?a=${pub.id}&b=${draft.id}`))}>Review changes</button>}
            <button className="btn" onClick={() => act('validate', async () => { const r = await api.get('/api/versions/validate'); setErrs(r) })}>Check</button>
            {canPublish && <button className="btn primary" title={fourEyesBlocked ? 'Four-eyes rule: you changed this draft, so another publisher must publish it' : ''} disabled={!!busy || job?.state === 'running' || fourEyesBlocked} onClick={async () => {
              if (!confirm('Publish this draft? Agents will see it as soon as it is fully indexed.')) return
              setMsg(null)
              try { setJob(await api.post('/api/versions/publish')); watch() } catch (e) { setMsg({ bad: true, text: e.message }) }
            }}>{job?.state === 'running' ? 'Publishing…' : 'Publish'}</button>}
            {!canPublish && <span className="small muted">Publishing needs the publisher role.</span>}
            {canPublish && fourEyesBlocked && <span className="small muted">Four-eyes rule: you changed this draft, another publisher must publish it.</span>}
            {canPublish && <button className="btn danger right" onClick={() => act('discard', async () => { if (confirm('Discard the draft and all its edits?')) { await api.del('/api/versions/draft'); setVersion('published') } })}>Discard draft</button>}
          </div>
        </> : <>
          <b>No draft</b>{pub ? <p className="muted small" style={{ margin: '4px 0 10px' }}>Start a draft from the published version “{pub.label}” to make changes.</p>
            : <p className="muted small" style={{ margin: '4px 0 10px' }}>Nothing is published yet. Upload an Excel file below, or generate a draft from a database on the Connectors page.</p>}
          {pub && canEdit && <div className="row"><input style={{ width: 200 }} placeholder="Label, e.g. 0.52" value={label} onChange={(e) => setLabel(e.target.value)} />
            <input style={{ width: 320 }} placeholder="What is this version about? (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
            <button className="btn primary" disabled={!label} onClick={() => act('draft', async () => { await api.post('/api/versions/draft', { label, note }); setLabel(''); setNote(''); setVersion('draft') })}>Create draft</button></div>}
        </>}
      </div>

      <ExcelCard hasDraft={!!draft} />

      <h2>All versions</h2>
      <table><thead><tr><th>Version</th><th>Status</th><th>Created</th><th>Published</th><th>Edits</th><th>Note</th></tr></thead><tbody>
        {vs.map((v) => <tr key={v.id}><td><b>{v.label}</b></td><td><Pill tone={v.status === 'published' ? 'ok' : v.status === 'draft' ? 'warn' : ''}>{v.status}</Pill></td>
          <td className="small">{new Date(v.created_at).toLocaleString()}</td><td className="small">{v.published_at ? new Date(v.published_at).toLocaleString() : '—'}</td><td>{v.edits}</td><td className="small">{v.note}</td></tr>)}
      </tbody></table>

      <h2>{draft ? 'Edit log of the draft' : 'Recent edits'}</h2>
      <table><thead><tr><th>When</th><th>Who</th><th>Action</th><th>What</th></tr></thead><tbody>
        {audit.slice(0, 60).map((a) => <tr key={a.id}><td className="small">{new Date(a.at).toLocaleString()}</td><td>{a.username}</td><td><Pill>{a.action}</Pill></td><td className="small">{a.kind} <b>{a.key}</b></td></tr>)}
        {!audit.length && <tr><td colSpan="4" className="muted">No edits yet.</td></tr>}</tbody></table>

      {errs && <Modal title="Check" onClose={() => setErrs(null)}>
        {errs.errors.length ? <><div className="banner bad">{errs.errors.length} problem(s) block publishing:</div><ul>{errs.errors.map((e) => <li key={e} className="small">{e}</li>)}</ul></> : <div className="banner ok">No problems: this draft can be published.</div>}
        {errs.warnings?.length > 0 && <><div className="banner" style={{ marginTop: 10 }}>Advice: {errs.warnings.length} filter or check SQL statement(s) do not run on the connected database. This never blocks publishing (the model may be ahead of the database).</div>
          <ul>{errs.warnings.slice(0, 50).map((w) => <li key={w} className="small">{w}</li>)}</ul></>}</Modal>}
      {diff && <Modal wide title={`Changes: v${pub.label} → draft`} onClose={() => setDiff(null)}>
        {diff.length === 0 ? <p className="muted">No differences.</p> : diff.map((d) => (
          <div key={d.kind + d.key} style={{ borderTop: '1px solid var(--line)', padding: '8px 0' }}>
            <div className="row"><Pill tone={d.change === 'added' ? 'ok' : d.change === 'removed' ? 'bad' : 'warn'}>{d.change}</Pill><span className="small">{d.kind}</span><b>{d.key}</b>
              {d.kind === 'entity' && <Link to={'/entities/' + d.key} onClick={() => setDiff(null)} className="small">open</Link>}</div>
            {d.fields && Object.entries(d.fields).map(([f, v]) => <div key={f} className="small" style={{ marginLeft: 12 }}><span className="muted">{f}:</span> <del style={{ color: 'var(--bad)' }}>{short(v.from)}</del> → <ins style={{ color: 'var(--ok)', textDecoration: 'none' }}>{short(v.to)}</ins></div>)}
          </div>))}
      </Modal>}
    </>
  )
}
