import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useStudio } from '../context.jsx'
import { Pill } from '../components/ui.jsx'
import { Danger } from './AdminPage.jsx'

// Settings > Maintenance: the search index, questions nobody could answer, and starting the ontology from zero.
export default function Maintenance() {
  const { status } = useStudio()
  const admin = true                                   // this page is only reachable by admins (Settings)
  const [qs, setQs] = useState([]), [msg, setMsg] = useState(null), [busy, setBusy] = useState(false)
  useEffect(() => { api.get('/api/admin/questions').then(setQs).catch(() => {}) }, [])
  return (
    <>
      <h2 style={{ marginTop: 18 }}>Search index</h2>
      <div className="card">
        <div className="row">
          <Pill tone={status?.embeddings_configured ? 'ok' : 'warn'}>{status?.embeddings_configured ? 'Embeddings ready' : 'No embeddings'}</Pill>
          <Pill tone={status?.alfred_available ? 'ok' : 'warn'}>{status?.alfred_available ? 'Alfred ready' : 'Alfred off: no OpenRouter key'}</Pill>
          <span className="small muted">embeddings: {status?.embedding_model} · Alfred: {status?.chat_model}</span>
          {status?.missing_embeddings > 0 && <Pill tone="warn">{status.missing_embeddings} chunks without embedding</Pill>}
        </div>
        {(!status?.embeddings_configured || !status?.alfred_available) && <p className="small muted">Add the OpenRouter API key on the <Link to="/settings">Settings</Link> page (or <code>OPENROUTER_API_KEY</code> in .env).{!status?.embeddings_configured && ' Until then search is keyword-only.'} Without it Alfred is off; browsing, search and the MCP server still work.</p>}
        {admin && <div style={{ marginTop: 10 }}><button className="btn" disabled={busy} onClick={async () => { setBusy(true); setMsg(null); try { const r = await api.post('/api/admin/reindex'); setMsg(`Reindexed: ${r.chunks} chunks, ${r.newly_embedded} newly embedded, ${r.missing_embeddings} still without embedding.`) } catch (e) { setMsg(e.message) } setBusy(false); refresh() }}>{busy ? 'Working…' : 'Rebuild search index'}</button> {msg && <span className="small">{msg}</span>}</div>}
      </div>

      <h2>Questions nobody could answer</h2>
      <p className="muted small">Searches (from Alfred, the UI or agents) that found nothing: a backlog of missing synonyms, terms or entities.</p>
      <table><thead><tr><th>When</th><th>Question</th></tr></thead><tbody>
        {qs.map((x, i) => <tr key={i}><td className="small">{new Date(x.at).toLocaleString()}</td><td>{x.question}</td></tr>)}{!qs.length && <tr><td colSpan="2" className="muted">None yet.</td></tr>}</tbody></table>
      <h2>Danger zone</h2>
      <Danger />
    </>
  )
}
