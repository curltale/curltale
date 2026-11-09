import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, q } from '../api.js'
import { Cell, fmt } from '../components/DataGrid.jsx'
import { Pill, SensPill } from '../components/ui.jsx'

export default function DataRowPage() {
  const [sp] = useSearchParams()
  const name = sp.get('name'), pk = sp.get('pk')
  const [d, setD] = useState(null), [err, setErr] = useState('')
  useEffect(() => { setD(null); setErr(''); api.get('/api/data/row?' + q({ name, pk })).then(setD).catch((e) => setErr(e.message)) }, [name, pk])
  if (err) return <div className="banner bad">{err}</div>
  if (!d) return <p className="muted">Loading…</p>
  const cols = d.columns.filter((c) => !c.hidden)
  return (
    <>
      <div className="row"><div><h1 style={{ fontSize: 20 }}>{d.name} <span className="mono muted" style={{ fontSize: 15 }}>{pk}</span></h1><div className="mono muted">{d.table}</div></div>
        <div className="right row"><Link className="btn small" to={'/data/table?name=' + d.table}>Whole table</Link><Link className="btn small" to={'/entities/' + d.entity}>Definition</Link></div></div>
      <div className="grid" style={{ gridTemplateColumns: 'minmax(0,2fr) minmax(260px,1fr)', marginTop: 14, alignItems: 'start' }}>
        <table><thead><tr><th>Column</th><th>Value</th></tr></thead><tbody>
          {cols.map((c) => (
            <tr key={c.name}><td><div className="mono small">{c.name}</div><div className="small muted" title={c.description}>{c.business_name}</div></td>
              <td className="mono small" style={{ wordBreak: 'break-word' }}>{c.fk ? <><Cell col={c} v={d.row[c.name]} table={d.table} /> {d.row[c.name] != null && <span className="muted"> → {c.fk.table}</span>}</> : fmt(d.row[c.name])}
                {c.sensitivity !== 'internal' && <> <SensPill s={c.sensitivity} /></>}{c.masked && <> <Pill>masked (hash)</Pill></>}</td></tr>))}</tbody></table>
        <div className="card"><h3 style={{ marginTop: 0 }}>Referenced by</h3>
          {d.referenced_by.length ? d.referenced_by.map((r) => (
            <div key={r.table + r.column} style={{ padding: '4px 0' }}><Link to={'/data/table?name=' + r.table + '&f=' + encodeURIComponent(JSON.stringify([{ col: r.column, op: 'eq', value: pk }]))}>{r.rows} × {r.name}</Link>
              <div className="mono small muted">{r.table}.{r.column}</div></div>)) : <p className="muted small">Nothing points at this row.</p>}
        </div>
      </div>
    </>
  )
}
