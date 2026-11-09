import { useState } from 'react'

export const REVIEW = ['confirmed_by_team', 'by_design', 'high_confidence', 'needs_review', 'source_needed']
export const SENS = ['internal', 'confidential', 'personal', 'restricted']
export const ACCESS = ['full', 'masked', 'none']
export const REL_STATUS = ['confirmed', 'optional', 'unreliable']

/** Tab bar. tabs: [{id,label,badge}]; the page keeps the current id in its URL. */
export function Tabs({ tabs, current, onPick }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => <button key={t.id} role="tab" aria-selected={t.id === current} className={t.id === current ? 'on' : ''} onClick={() => onPick(t.id)}>{t.label}{t.badge ? <span className="tb">{t.badge}</span> : null}</button>)}
    </div>
  )
}

/** An on/off switch with its label; drawn by styles.css (input.switch). */
export function Switch({ checked, onChange, disabled, children, title, style }) {
  return <label className="sw" title={title} style={style}><input type="checkbox" className="switch" checked={!!checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} /><span>{children}</span></label>
}

export const Pill = ({ children, tone }) => <span className={'pill ' + (tone || '')}>{children}</span>

export function ReviewPill({ s }) {
  const tone = s === 'needs_review' || s === 'source_needed' ? 'warn' : s === 'confirmed_by_team' ? 'ok' : ''
  return <Pill tone={tone}>{s?.replaceAll('_', ' ')}</Pill>
}
export function StatusPill({ s }) {
  return <Pill tone={s === 'confirmed' ? 'ok' : s === 'unreliable' ? 'bad' : 'warn'}>{s?.replaceAll('_', ' ')}</Pill>
}
export function SensPill({ s }) {
  return <Pill tone={s === 'restricted' ? 'bad' : s === 'personal' || s === 'confidential' ? 'warn' : ''}>{s}</Pill>
}

export function Modal({ title, onClose, children, wide }) {
  return (
    <div className="modal-bg" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="modal" style={wide ? { width: 'min(980px,100%)' } : null}>
        <div className="row" style={{ marginBottom: 12 }}><h1 style={{ fontSize: 18, margin: 0 }}>{title}</h1><button className="btn small right" onClick={onClose}>Close</button></div>
        {children}
      </div>
    </div>
  )
}

/** Schema-driven editor. field: {name,label,type:text|area|lines|select|json,options,help}. Keys not in the schema are kept untouched. */
export function FieldsEditor({ schema, value, onSave, onCancel, saveLabel = 'Save', lockKeys = [] }) {
  const init = {}
  schema.forEach((f) => {
    const v = value[f.name]
    init[f.name] = f.type === 'lines' ? (v || []).join('\n') : f.type === 'json' ? (v === undefined ? '' : JSON.stringify(v, null, 1)) : v ?? ''
  })
  const [vals, setVals] = useState(init)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const set = (n, v) => setVals((s) => ({ ...s, [n]: v }))

  const submit = async () => {
    const out = { ...value }
    try {
      for (const f of schema) {
        const v = vals[f.name]
        if (f.type === 'lines') { const l = v.split('\n').map((x) => x.trim()).filter(Boolean); if (l.length) out[f.name] = l; else delete out[f.name] }
        else if (f.type === 'json') { if (v.trim()) out[f.name] = JSON.parse(v); else delete out[f.name] }
        else if (v === '' && !f.required) delete out[f.name]
        else out[f.name] = v
      }
    } catch (e) { setErr(`${e.message} (check the JSON fields)`); return }
    setBusy(true); setErr('')
    try { await onSave(out) } catch (e) { setErr(e.message); setBusy(false) }
  }

  return (
    <div>
      {schema.map((f) => (
        <div className="field" key={f.name}>
          <label>{f.label || f.name}{f.help && <span className="muted" style={{ fontWeight: 400 }}> — {f.help}</span>}</label>
          {f.type === 'select' ? (
            <select value={vals[f.name]} onChange={(e) => set(f.name, e.target.value)} disabled={lockKeys.includes(f.name)}>
              {!f.options.includes(vals[f.name]) && <option value={vals[f.name]}>{vals[f.name] || '—'}</option>}
              {f.options.map((o) => <option key={o}>{o}</option>)}
            </select>
          ) : f.type === 'area' || f.type === 'lines' || f.type === 'json' ? (
            <textarea className={f.type === 'json' ? 'mono' : ''} rows={f.rows || (f.type === 'json' ? 4 : 3)} value={vals[f.name]} onChange={(e) => set(f.name, e.target.value)} />
          ) : (
            <input value={vals[f.name]} onChange={(e) => set(f.name, e.target.value)} disabled={lockKeys.includes(f.name)} />
          )}
        </div>
      ))}
      {err && <div className="banner bad" style={{ whiteSpace: 'pre-wrap' }}>{err}</div>}
      <div className="row"><button className="btn primary" onClick={submit} disabled={busy}>{saveLabel}</button><button className="btn" onClick={onCancel}>Cancel</button></div>
    </div>
  )
}

export const ATTR_SCHEMA = [
  { name: 'business_name', label: 'Business name' },
  { name: 'description', type: 'area' },
  { name: 'type', help: 'text, integer, decimal, date, boolean, …' },
  { name: 'role', help: 'primary_key, foreign_key, attribute, …' },
  { name: 'sensitivity', type: 'select', options: SENS, help: 'restricted must have agent access none' },
  { name: 'agent_access', type: 'select', options: ACCESS },
  { name: 'review_status', type: 'select', options: REVIEW },
  { name: 'allowed_values', type: 'lines', help: 'one per line: the values this field can take' },
  { name: 'value_labels', type: 'json', rows: 3, help: '{"code": "Label"} optional' },
  { name: 'references', help: 'entity name this column points to' },
  { name: 'tmf_name', label: 'Industry standard name' },
  { name: 'business_term' }, { name: 'business_term_description', type: 'area', rows: 2 }, { name: 'format' }, { name: 'example' }, { name: 'unit' },
  { name: 'custom', label: 'Custom properties', type: 'json', rows: 4, help: 'your own properties: {"name": "value"}; shown in the app and given to agents' },
]

export const CALC_SCHEMA = [
  { name: 'business_name', label: 'Name' }, { name: 'description', type: 'area', help: 'what it means and how it is computed, in words' },
  { name: 'sql', type: 'area', rows: 5, help: 'optional: SQL that computes it. Context for agents; the app never runs it' },
  { name: 'type', help: 'integer, decimal, …' }, { name: 'unit' }, { name: 'sensitivity', type: 'select', options: SENS },
  { name: 'agent_access', type: 'select', options: ACCESS }, { name: 'review_status', type: 'select', options: REVIEW }, { name: 'business_term' },
  { name: 'custom', label: 'Custom properties', type: 'json', rows: 4, help: 'your own properties: {"name": "value"}; shown in the app and given to agents' },
]
export const FILTER_SCHEMA = [
  { name: 'name', help: 'the words people use: open order, unpaid invoice' }, { name: 'synonyms', type: 'lines', help: 'other ways to say it, one per line' },
  { name: 'filter', type: 'area', rows: 3, help: "SQL condition on this entity's table, columns unqualified; checked against the database when you publish" },
  { name: 'description', type: 'area', rows: 2 }, { name: 'status', type: 'select', options: ['governed', 'working'], help: 'governed = from a defined measure; working = a reading nobody has confirmed' },
  { name: 'source', help: 'where the definition comes from' },
]
export const CHECK_SCHEMA = [
  { name: 'id', help: 'e.g. DQ01' }, { name: 'rule', type: 'area', rows: 2 },
  { name: 'sql', type: 'area', rows: 5, help: 'optional: SQL returning one column n = the number of violations (must be 0); checked when you publish and run by the Model check' },
  { name: 'note', type: 'area', rows: 2 },
]
