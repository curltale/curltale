import { Link } from 'react-router-dom'

export const fmt = (v) => (v === null || v === undefined ? <span className="muted">null</span> : typeof v === 'boolean' ? (v ? 'true' : 'false') : Array.isArray(v) ? '{' + v.join(', ') + '}' : String(v))
const short = (s) => (s.length > 60 ? s.slice(0, 60) + '…' : s)

/** Cell with navigation: primary keys and foreign keys open the referenced row. */
export function Cell({ col, v, table, isPk }) {
  if (v === null || v === undefined) return <span className="muted">null</span>
  if (col.fk) return <Link to={`/data/row?name=${encodeURIComponent(col.fk.table)}&pk=${encodeURIComponent(v)}`} title={`Open ${col.fk.table} ${v}`}>{String(v)}</Link>
  if (isPk) return <Link to={`/data/row?name=${encodeURIComponent(table)}&pk=${encodeURIComponent(v)}`}>{String(v)}</Link>
  return <span title={String(v)}>{typeof v === 'string' ? short(v) : fmt(v)}</span>
}

/** Plain result grid for the console (no semantic columns). */
export function ResultGrid({ columns, rows }) {
  return (
    <div style={{ overflow: 'auto', maxHeight: '60vh', border: '1px solid var(--line)', borderRadius: 10 }}>
      <table style={{ border: 'none', borderRadius: 0 }}><thead><tr>{columns.map((c) => <th key={c} style={{ position: 'sticky', top: 0 }}>{c}</th>)}</tr></thead>
        <tbody>{rows.map((r, i) => <tr key={i}>{columns.map((c) => <td key={c} className="mono small" title={r[c] == null ? '' : String(r[c])}>{typeof r[c] === 'string' && r[c].length > 70 ? r[c].slice(0, 70) + '…' : fmt(r[c])}</td>)}</tr>)}
          {!rows.length && <tr><td colSpan={columns.length || 1} className="muted">No rows.</td></tr>}</tbody></table>
    </div>
  )
}
