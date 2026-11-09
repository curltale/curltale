import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, q } from '../api.js'
import { useStudio } from '../context.jsx'
import { Pill } from '../components/ui.jsx'

export function linkFor(r) {
  const [t, rest] = [r.id.split(':')[0], r.id.split(':').slice(1).join(':')]
  if (t === 'entity') return '/entities/' + rest
  if (t === 'attr' || t === 'facttype' || t === 'filter') return '/entities/' + rest.split('.')[0]
  if (t === 'calc' || t === 'check') return r.entity ? '/entities/' + r.entity : '/'
  if (t === 'rel') return '/path'
  if (t === 'ex') return '/examples#' + rest
  return '/'
}

export default function SearchPage() {
  const { version } = useStudio()
  const [sp] = useSearchParams()
  const query = sp.get('q') || ''
  const [res, setRes] = useState(null)
  useEffect(() => { setRes(null); if (query) api.get('/api/search?' + q({ q: query, version })).then(setRes) }, [query, version])
  return (
    <>
      <h1>Search</h1><p className="muted">“{query}” — meaning plus exact code match, as agents see it.</p>
      {!res ? <p className="muted">Searching…</p> : res.length === 0 ? <div className="banner">Nothing found. This question is logged so modellers can fill the gap.</div> :
        res.map((r) => (
          <div className="card" key={r.id} style={{ marginBottom: 10 }}>
            <div className="row"><Link to={linkFor(r)}><b>{r.title}</b></Link><Pill>{r.type.replaceAll('_', ' ')}</Pill>{r.pending_confirmation && <Pill tone="warn">pending confirmation</Pill>}
              {r.expanded && <Pill tone="accent">context</Pill>}<span className="muted small right">{r.score}</span></div>
            <div className="small muted" style={{ marginTop: 4 }}>{r.text.length > 330 ? r.text.slice(0, 330) + '…' : r.text}</div>
          </div>))}
    </>
  )
}
