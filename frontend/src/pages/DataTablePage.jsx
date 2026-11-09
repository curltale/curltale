import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api, q } from '../api.js'
import { Cell } from '../components/DataGrid.jsx'
import { Pill } from '../components/ui.jsx'

const OPS = [['contains', 'contains'], ['eq', '='], ['gt', '>'], ['lt', '<'], ['null', 'is null'], ['notnull', 'is not null']]

export default function DataTablePage() {
  const [sp, setSp] = useSearchParams()
  const name = sp.get('name'), page = +(sp.get('page') || 1), sort = sp.get('sort') || '', desc = sp.get('desc') === '1'
  const filters = useMemo(() => { try { return JSON.parse(sp.get('f') || '[]') } catch { return [] } }, [sp])
  const [d, setD] = useState(null), [err, setErr] = useState(''), [nf, setNf] = useState({ col: '', op: 'contains', value: '' })
  const nav = useNavigate()
  const set = (o) => { const n = new URLSearchParams(sp); Object.entries(o).forEach(([k, v]) => (v === '' || v === null || v === undefined ? n.delete(k) : n.set(k, v))); setSp(n) }

  useEffect(() => {
    setD(null); setErr('')
    api.get('/api/data/table?' + q({ name, page, size: 50, sort, desc: desc ? 'true' : '', filters: filters.length ? JSON.stringify(filters) : '' })).then(setD).catch((e) => setErr(e.message))
  }, [name, page, sort, desc, sp.get('f')])

  if (err) return <div className="banner bad">{err}</div>
  if (!d) return <p className="muted">Loading…</p>
  const cols = d.columns.filter((c) => !c.hidden), hidden = d.columns.filter((c) => c.hidden)
  const pages = Math.max(1, Math.ceil(d.total / d.size))
  const addFilter = () => { if (!nf.col) return; set({ f: JSON.stringify([...filters, nf]), page: '' }); setNf({ col: '', op: 'contains', value: '' }) }

  return (
    <>
      <div className="row"><div><h1 className="mono" style={{ fontSize: 20 }}>{d.table}</h1>
        <div className="muted">{d.name} · <b>{d.total.toLocaleString()}</b> rows{filters.length ? ' (filtered)' : ''}</div></div>
        <div className="right row"><Link className="btn small" to={'/entities/' + d.entity}>Definition</Link><Link className="btn small" to={'/data/sql?q=' + encodeURIComponent(`SELECT * FROM ${d.table} LIMIT 50`)}>Open in console</Link><Link className="btn small" to="/data">All tables</Link></div></div>
      {cols.some((c) => c.masked) && <div className="banner" style={{ marginTop: 10 }}>🎭 Masked columns ({cols.filter((c) => c.masked).map((c) => c.name).join(', ')}) show a stable hash, not the real value: equal values give equal hashes. They cannot be filtered.</div>}
      {hidden.length > 0 && <div className="banner" style={{ marginTop: 10 }}>🔒 {hidden.length} restricted column(s) hidden for your entitlements: {hidden.map((h) => h.name).join(', ')}</div>}

      <div className="row" style={{ margin: '12px 0' }}>
        <select style={{ width: 200 }} value={nf.col} onChange={(e) => setNf({ ...nf, col: e.target.value })}><option value="">Filter column…</option>{cols.map((c) => <option key={c.name}>{c.name}</option>)}</select>
        <select style={{ width: 120 }} value={nf.op} onChange={(e) => setNf({ ...nf, op: e.target.value })}>{OPS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
        {!['null', 'notnull'].includes(nf.op) && <input style={{ width: 200 }} value={nf.value} onChange={(e) => setNf({ ...nf, value: e.target.value })} onKeyDown={(e) => e.key === 'Enter' && addFilter()} placeholder="value" />}
        <button className="btn" onClick={addFilter} disabled={!nf.col}>Add filter</button>
        {filters.map((f, i) => <Pill key={i} tone="accent"><span style={{ cursor: 'pointer' }} onClick={() => set({ f: JSON.stringify(filters.filter((_, j) => j !== i)) , page: '' })}>{f.col} {OPS.find((o) => o[0] === f.op)[1]} {f.value} ✕</span></Pill>)}
      </div>

      <div style={{ overflow: 'auto', border: '1px solid var(--line)', borderRadius: 10, maxHeight: '66vh' }}>
        <table style={{ border: 'none', borderRadius: 0 }}><thead><tr>
          {cols.map((c) => (
            <th key={c.name} style={{ position: 'sticky', top: 0, cursor: 'pointer', whiteSpace: 'nowrap' }} title={`${c.business_name}\n${c.description}\n${c.type} · ${c.role} · ${c.sensitivity}`}
              onClick={() => set({ sort: c.name, desc: sort === c.name && !desc ? '1' : '' })}>
              {c.name}{sort === c.name ? (desc ? ' ▼' : ' ▲') : ''}{c.fk ? ' →' : ''}<div style={{ fontWeight: 400, textTransform: 'none', letterSpacing: 0 }}>{c.type}{c.role === 'primary_key' ? ' · key' : ''}{c.masked ? ' · masked' : ''}</div></th>))}
        </tr></thead><tbody>
          {d.rows.map((r, i) => <tr key={i}>{cols.map((c) => <td key={c.name} className="mono small" style={{ whiteSpace: 'nowrap' }}><Cell col={c} v={r[c.name]} table={d.table} isPk={c.name === d.pk[0] && d.pk.length === 1} /></td>)}</tr>)}
          {!d.rows.length && <tr><td colSpan={cols.length} className="muted">No rows match.</td></tr>}</tbody></table>
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        <button className="btn small" disabled={page <= 1} onClick={() => set({ page: page - 1 })}>← Prev</button><span className="small">Page {page} of {pages}</span>
        <button className="btn small" disabled={page >= pages} onClick={() => set({ page: page + 1 })}>Next →</button>
        <span className="muted small right">Hover a header for the meaning of the column. Keys and foreign keys (→) open the related row.</span></div>
    </>
  )
}
