import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, q } from '../api.js'
import { linkFor } from '../pages/SearchPage.jsx'
import { useStudio } from '../context.jsx'
import Icon from './Icon.jsx'

// Ctrl/Cmd+K: go to a page, or find an entity, field, query example, collection, document or topic. Everything is searched with the signed-in person's own rights.
export default function GlobalSearch() {
  const { user, status, version } = useStudio()
  const nav = useNavigate()
  const [text, setText] = useState(''), [model, setModel] = useState([]), [lib, setLib] = useState(null), [i, setI] = useState(0), [open, setOpen] = useState(false)
  const input = useRef(), box = useRef()
  const pages = useMemo(() => [
    ['Entities', '/', 'model ontology'], ['Entity graph', '/graph', 'model ontology graph'], ['Join paths', '/path', 'model join relationships'], ['Query examples', '/examples', 'model sql patterns'],
    ['Database', '/data', 'data tables browse'], ['SQL console', '/data/sql', 'data query'], ['Model check', '/data/check', 'data conformance'],
    ['Changes & versions', '/versions', 'ontology draft publish excel versions'], ['Users & access', '/admin', 'manage users tokens agents mcp'],
    ...(user.role === 'admin' ? [['Settings', '/settings', 'manage configuration ai storage'], ['Activity', '/activity', 'manage audit log'], ['Connectors', '/settings/connectors', 'settings data sources database'], ['Settings: AI', '/settings/ai', 'model key endpoint alfred reranker'], ['Settings: Library', '/settings/library', 'documents workers upload'], ['Settings: Storage', '/settings/storage', 's3 files bucket'], ['Settings: Web capture', '/settings/web', 'fetch browser urls'], ['Settings: Limits', '/settings/limits', 'rate'], ['Settings: Maintenance', '/settings/maintenance', 'search index reindex questions empty ontology reset']] : []),
    ...(status?.library ? [['Collections', '/library', 'library documents access'], ['Sources', '/library/sources', 'library upload files web'], ['Topics', '/library/topics', 'library wiki knowledge'], ['Topic graph', '/library/graph', 'library knowledge graph'],
      ['Interview', '/library/interview', 'library capture expertise'], ['Review', '/library/review', 'library held changes inbox'], ['Vocabulary', '/library/vocabulary', 'library kinds types relations'], ['Health', '/library/health', 'library check']] : []),
  ], [user.role, status?.library])
  useEffect(() => {                                           // Ctrl/Cmd+K puts the cursor here
    const k = (e) => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); input.current?.focus(); input.current?.select(); setOpen(true) } }
    window.addEventListener('keydown', k); return () => window.removeEventListener('keydown', k)
  }, [])
  useEffect(() => {
    const away = (e) => { if (box.current && !box.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', away); return () => document.removeEventListener('mousedown', away)
  }, [])
  useEffect(() => {
    if (!open || text.trim().length < 2) { setModel([]); setLib(null); return }
    const t = setTimeout(() => {
      api.get('/api/search?' + q({ q: text.trim(), version })).then((r) => setModel(r.slice(0, 6))).catch(() => setModel([]))
      if (status?.library) api.get('/api/library/find?' + q({ q: text.trim() })).then(setLib).catch(() => setLib(null))
    }, 250)
    return () => clearTimeout(t)
  }, [text, open])
  const t = text.trim().toLowerCase()
  const items = useMemo(() => {
    const out = []
    pages.filter(([name, , kw]) => !t || (name + ' ' + kw).toLowerCase().includes(t)).slice(0, t ? 5 : 8).forEach(([name, to]) => out.push({ group: 'Go to', label: name, to }))
    model.forEach((r) => out.push({ group: 'In the data model', label: r.title, sub: r.type.replaceAll('_', ' '), to: linkFor(r) }))
    ;(lib?.collections || []).forEach((c) => out.push({ group: 'Collections', label: c.name, sub: c.usage, to: `/library/sources?c=${c.id}` }))
    ;(lib?.topics || []).forEach((x) => out.push({ group: 'Topics', label: x.title, sub: `${x.type} · ${x.collection} (${x.usage})`, to: `/library/topics/${x.slug}?c=${x.collection_id}` }))
    ;(lib?.sources || []).forEach((x) => out.push({ group: 'Documents', label: x.name, sub: `${x.collection} (${x.usage})`, to: `/library/sources/${x.id}?c=${x.collection_id}` }))
    if (t.length >= 2) out.push({ group: 'Everything', label: `Show all results for “${text.trim()}”`, to: '/search?q=' + encodeURIComponent(text.trim()) })
    return out
  }, [pages, model, lib, t])
  useEffect(() => { setI(0) }, [text])
  const done = () => { setOpen(false); setText(''); input.current?.blur() }
  const go = (it) => { if (it) { done(); nav(it.to) } }
  const onKey = (e) => {
    if (e.key === 'Escape') done()
    else if (e.key === 'ArrowDown') { e.preventDefault(); setOpen(true); setI((x) => Math.min(x + 1, items.length - 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setI((x) => Math.max(x - 1, 0)) }
    else if (e.key === 'Enter') { e.preventDefault(); go(items[i]) }
  }
  let last = ''
  return (
    <div className="gsearch" ref={box}>
      <Icon name="search" size={16} />
      <input ref={input} value={text} onChange={(e) => { setText(e.target.value); setOpen(true) }} onFocus={() => setOpen(true)} onKeyDown={onKey}
        placeholder="Search entities, topics, documents, or go to a page" aria-label="Search" role="combobox" aria-expanded={open} aria-controls="gsearch-list" aria-autocomplete="list" autoComplete="off" />
      {!text && <kbd>Ctrl K</kbd>}
      {open && <div id="gsearch-list" className="gdrop" role="listbox">
        {items.map((it, n) => {
          const head = it.group !== last; last = it.group
          return (
            <div key={n}>
              {head && <div className="small muted ghead">{it.group}</div>}
              <div role="option" aria-selected={n === i} className={'gitem' + (n === i ? ' on' : '')} onMouseEnter={() => setI(n)} onClick={() => go(it)}>
                <span>{it.label}</span>{it.sub && <span className="small muted" style={{ marginLeft: 'auto' }}>{it.sub}</span>}</div>
            </div>)
        })}
        {!items.length && <div className="muted" style={{ padding: '12px 14px' }}>Nothing found.</div>}
        <div className="small muted gfoot">↑ ↓ to move · Enter to open · Esc to close</div>
      </div>}
    </div>
  )
}
