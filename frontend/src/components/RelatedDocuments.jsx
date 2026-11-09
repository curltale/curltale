import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { UsagePill } from '../pages/CollectionsPage.jsx'

// Topics, in the collections the signed-in person can read, that are about a model entity. The model does not hold these links: they are asked of the collections.
export default function RelatedDocuments({ entity }) {
  const [docs, setDocs] = useState([])
  useEffect(() => { api.get(`/api/entities/${entity}/documents`).then((d) => setDocs(d.documents)).catch(() => setDocs([])) }, [entity])
  if (!docs.length) return null
  return (
    <div className="card" style={{ marginTop: 14 }}>
      <h3 style={{ marginTop: 0 }}>Related documents</h3>
      <p className="small muted" style={{ marginTop: -4 }}>Topics from the document collections you can read. Only you and others with access to a collection see its links.</p>
      {docs.map((b) => (
        <div key={b.collection_id} style={{ marginTop: 8 }}>
          <div className="row"><b>{b.collection}</b><UsagePill usage={b.usage} /></div>
          {b.topics.map((t) => (
            <div key={t.topic} className="small" style={{ marginTop: 4 }}>
              <Link to={t.link}>{t.title}</Link> <span className="muted">· {t.type} · {t.label} {t.about ? `(${t.about})` : 'this entity'}</span>
              {t.summary && <div className="muted">{t.summary}</div>}
            </div>))}
        </div>))}
    </div>
  )
}
