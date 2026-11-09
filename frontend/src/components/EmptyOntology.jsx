import { Link } from 'react-router-dom'
import { useStudio } from '../context.jsx'

export default function EmptyOntology() {
  const { user, status } = useStudio()
  const admin = user.role === 'admin'
  return (
    <div className="card" style={{ maxWidth: 760, margin: '40px auto' }}>
      <h1 style={{ marginTop: 0 }}>No ontology yet</h1>
      <p className="muted">Nothing is published, so there is nothing to explore, search or query. {status?.draft ? 'A draft exists: switch the version selector (top right) to Draft to look at it, then publish it.' : ''}</p>
      {admin ? <>
        <p><b>Two ways to start</b></p>
        <ol style={{ lineHeight: 1.7 }}>
          <li><b>From a spreadsheet.</b> On the <Link to="/versions">Versions page</Link>, download the blank template (or a backup of an earlier ontology), fill it in, and upload it. The file is checked first and becomes a draft.</li>
          <li><b>From a database.</b> On the <Link to="/settings/connectors">Connectors page</Link>, add the database, read its structure and generate a draft model from its tables.</li>
        </ol>
        <p>Then review the draft and press <b>Publish</b> on the Versions page: search, agents and Alfred see it from that moment.</p>
        <div className="row"><Link className="btn primary" to="/versions">Versions page</Link><Link className="btn" to="/settings/connectors">Connectors</Link></div>
      </> : <p>Ask an administrator to publish an ontology.</p>}
    </div>
  )
}
