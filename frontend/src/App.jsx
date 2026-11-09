import { useCallback, useEffect, useState } from 'react'
import { Link, NavLink, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import Icon from './components/Icon.jsx'
import ErrorBoundary from './components/ErrorBoundary.jsx'
import { api } from './api.js'
import { Studio } from './context.jsx'
import Alfred from './components/Alfred.jsx'
import Entities from './pages/Entities.jsx'
import EntityPage from './pages/EntityPage.jsx'
import GraphPage from './pages/GraphPage.jsx'
import PathPage from './pages/PathPage.jsx'
import ExamplesPage from './pages/ExamplesPage.jsx'
import ActivityPage from './pages/ActivityPage.jsx'
import SettingsPage from './pages/SettingsPage.jsx'
import EmptyOntology from './components/EmptyOntology.jsx'
import ChangePassword from './components/ChangePassword.jsx'
import { Modal } from './components/ui.jsx'
import { can } from './context.jsx'
import VersionsPage from './pages/VersionsPage.jsx'
import AdminPage from './pages/AdminPage.jsx'
import SearchPage from './pages/SearchPage.jsx'
import DataPage from './pages/DataPage.jsx'
import DataTablePage from './pages/DataTablePage.jsx'
import DataRowPage from './pages/DataRowPage.jsx'
import SqlPage from './pages/SqlPage.jsx'
import CheckPage from './pages/CheckPage.jsx'
import CollectionsPage from './pages/CollectionsPage.jsx'
import SourcesPage from './pages/SourcesPage.jsx'
import SourcePage from './pages/SourcePage.jsx'
import TopicsPage from './pages/TopicsPage.jsx'
import TopicPage from './pages/TopicPage.jsx'
import { ReviewList, ConflictPage } from './pages/ReviewPage.jsx'
import HealthPage from './pages/HealthPage.jsx'
import LibraryGraphPage from './pages/LibraryGraphPage.jsx'
import GlobalSearch from './components/GlobalSearch.jsx'
import InterviewsPage from './pages/InterviewsPage.jsx'
import VocabularyPage from './pages/VocabularyPage.jsx'
import InterviewPage from './pages/InterviewPage.jsx'

function Login({ onLogin }) {
  const [u, setU] = useState('admin'), [p, setP] = useState(''), [err, setErr] = useState('')
  const go = async (e) => {
    e.preventDefault()
    try { onLogin(await api.post('/api/login', { username: u, password: p })) } catch (x) { setErr(x.message) }
  }
  return (
    <form className="card login" onSubmit={go}>
      <Mark h={56} style={{ marginBottom: 10 }} />
      <h1 className="wordmark">Curltale <span style={{ color: 'var(--accent)', fontWeight: 600 }}>AI</span></h1><p className="muted">Sign in</p>
      <div className="field"><label>Username</label><input value={u} onChange={(e) => setU(e.target.value)} autoFocus /></div>
      <div className="field"><label>Password</label><input type="password" value={p} onChange={(e) => setP(e.target.value)} /></div>
      {err && <div className="banner bad">{err}</div>}
      <button className="btn primary" style={{ width: '100%' }}>Sign in</button>
    </form>
  )
}

export default function App() {
  const [user, setUser] = useState(undefined)
  const [status, setStatus] = useState(null)
  const [version, setVersionState] = useState(() => localStorage.getItem('ss_version') || 'published')
  const [q, setQ] = useState('')
  const [pwOpen, setPwOpen] = useState(false)
  const [theme, flipTheme] = useTheme()
  const [slim, setSlim] = useState(() => { try { return localStorage.getItem('ss_slim') === '1' } catch { return false } })
  const toggleSlim = () => setSlim((s) => { try { localStorage.setItem('ss_slim', s ? '0' : '1') } catch {} return !s })
  const nav = useNavigate()
  const loc = useLocation()

  const refresh = useCallback(() => api.get('/api/status').then(setStatus).catch(() => {}), [])
  useEffect(() => {                                   // keep the counters of the menu true: after any change made here, when the tab is looked at again, and every half minute
    let t
    const soon = () => { clearTimeout(t); t = setTimeout(refresh, 400) }
    const seen = () => { if (document.visibilityState === 'visible') refresh() }
    window.addEventListener('studio:changed', soon); document.addEventListener('visibilitychange', seen); window.addEventListener('focus', seen)
    const poll = setInterval(seen, 30000)
    return () => { clearTimeout(t); clearInterval(poll); window.removeEventListener('studio:changed', soon); document.removeEventListener('visibilitychange', seen); window.removeEventListener('focus', seen) }
  }, [refresh])
  useEffect(() => { api.get('/api/me').then(setUser).catch(() => setUser(null)) }, [])
  useEffect(() => { if (user) refresh() }, [user, refresh])
  // never sit on a draft that no longer exists
  useEffect(() => { if (status && version === 'draft' && !status.draft) setVersion('published') }, [status])
  const setVersion = (v) => { setVersionState(v); try { localStorage.setItem('ss_version', v) } catch {} }

  if (user === undefined) return null
  if (!user) return <Login onLogin={setUser} />
  if (user.must_change) return <ChangePassword user={user} forced onDone={() => api.get('/api/me').then(setUser)} />
  const editable = version === 'draft' && !!status?.draft && can(user, 'modeller')
  const ctx = { user, status, refresh, version, setVersion, editable }
  const onto = ['/', '/entities', '/graph', '/path', '/examples', '/search'].some((p) => (p === '/' ? loc.pathname === '/' : loc.pathname.startsWith(p)))
  const pub = status?.versions?.find((v) => v.status === 'published')
  const empty = !!status && !status.published && version === 'published' && !['/versions', '/settings', '/admin', '/activity', '/library'].some((p) => loc.pathname.startsWith(p))

  return (
    <Studio.Provider value={ctx}>
      <div className={'shell' + (slim ? ' slim' : '')}>
        <div className="top">
          <Link to="/" className="brand">
            <Mark h={34} />
            <span>Curltale <span className="ai">AI</span></span>
          </Link>
          <GlobalSearch />
          <div className="right row">
            <button className="btn small iconbtn" title={theme === 'dark' ? 'Switch to the light theme' : 'Switch to the dark theme'} aria-label="Switch theme" onClick={flipTheme}><Icon name={theme === 'dark' ? 'sun' : 'moon'} size={16} /></button>
            <button className="btn small userchip" title="Change your password" onClick={() => setPwOpen(true)}><Icon name="user" size={15} />{user.username}<span className="muted">{user.role}</span></button>
            <button className="btn small iconbtn" title="Sign out" aria-label="Sign out" onClick={async () => { await api.post('/api/logout'); setUser(null) }}><Icon name="logout" size={16} /></button>
          </div>
        </div>
        <nav className="side" aria-label="Main">
          <h4>Ontology</h4>
          <L to="/" end icon="entities">Entities</L><L to="/graph" icon="graph">Graph</L><L to="/path" icon="path">Join paths</L><L to="/examples" icon="examples">Query examples</L><L to="/versions" icon="versions">Changes & versions</L>
          {status?.library && <><h4>Knowledge</h4><L to="/library" end icon="collections">Collections</L><L to="/library/sources" icon="sources">Sources</L><L to="/library/topics" icon="topics">Topics</L><L to="/library/graph" icon="graph">Graph</L>
            <L to="/library/review" icon="review" badge={status.library_review > 0 ? status.library_review : null}>Review</L><L to="/library/interview" icon="interview">Interview</L><L to="/library/vocabulary" icon="vocabulary">Vocabulary</L><L to="/library/health" icon="health">Health</L></>}
          <h4>Data</h4>
          <L to="/data" end icon="database">Browse</L><L to="/data/sql" icon="sql">SQL console</L><L to="/data/check" icon="check">Model check</L>
          <h4>Admin</h4>
          {user.role === 'admin' ? <><L to="/admin" icon="users">Users & access</L><L to="/activity" icon="activity">Activity</L><L to="/settings" icon="settings">Settings</L></> : <L to="/admin" icon="plug">Connect agents</L>}
        </nav>
        <button className="tongue" onClick={toggleSlim} title={slim ? 'Show the menu labels' : 'Collapse the menu'} aria-label={slim ? 'Expand the menu' : 'Collapse the menu'}><Icon name={slim ? 'chevr' : 'chevl'} size={12} /></button>
        <main className="main">
          <div className="page">
            {onto && <div className="row" style={{ marginBottom: 12, justifyContent: 'flex-end' }}><span className="small muted">Ontology version</span>
              <select style={{ width: 'auto' }} aria-label="Ontology version" value={version} onChange={(e) => setVersion(e.target.value)}>
                <option value="published">Published{pub ? ': ' + pub.label : ''}</option>
                <option value="draft" disabled={!status?.draft}>{status?.draft ? 'Draft (editing)' : 'No draft'}</option>
              </select></div>}
            {version === 'draft' && editable && <div className="banner">You are viewing the <b>draft</b>. Changes are not visible to agents until you publish it (Changes & versions).</div>}
            <ErrorBoundary key={loc.pathname}>
            {empty ? <EmptyOntology /> : <Routes>
              <Route path="/" element={<Entities />} />
              <Route path="/entities/:key" element={<EntityPage />} />
              <Route path="/graph" element={<GraphPage />} />
              <Route path="/path" element={<PathPage />} />
              <Route path="/examples" element={<ExamplesPage />} />
              <Route path="/library" element={<CollectionsPage />} />
              <Route path="/library/sources" element={<SourcesPage />} />
              <Route path="/library/sources/:sid" element={<SourcePage />} />
              <Route path="/library/topics" element={<TopicsPage />} />
              <Route path="/library/graph" element={<LibraryGraphPage />} />
              <Route path="/library/interview" element={<InterviewsPage />} />
              <Route path="/library/interview/:iid" element={<InterviewPage />} />
              <Route path="/library/review" element={<ReviewList />} />
              <Route path="/library/review/:kid" element={<ConflictPage />} />
              <Route path="/library/health" element={<HealthPage />} />
              <Route path="/library/vocabulary" element={<VocabularyPage />} />
              <Route path="/library/topics/:slug" element={<TopicPage />} />
              <Route path="/versions" element={<VersionsPage />} />
              <Route path="/admin" element={<AdminPage />} /><Route path="/admin/:tab" element={<AdminPage />} />
              <Route path="/alfred" element={<p className="muted">Alfred is open full screen.</p>} />
              <Route path="/activity" element={<ActivityPage />} />
              <Route path="/connectors" element={<Navigate to="/settings/connectors" replace />} />
              <Route path="/settings" element={<SettingsPage />} /><Route path="/settings/:tab" element={<SettingsPage />} />
              <Route path="/activity/:sid" element={<ActivityPage />} />
              <Route path="/search" element={<SearchPage />} />
              <Route path="/data" element={<DataPage />} />
              <Route path="/data/table" element={<DataTablePage />} />
              <Route path="/data/row" element={<DataRowPage />} />
              <Route path="/data/sql" element={<SqlPage />} />
              <Route path="/data/check" element={<CheckPage />} />
              <Route path="*" element={<p>Not found</p>} />
            </Routes>}
            </ErrorBoundary>
          </div>
        </main>
      </div>
      <Alfred />
      {pwOpen && <Modal title="Change your password" onClose={() => setPwOpen(false)}><ChangePassword user={user} onDone={() => { setPwOpen(false); alert('Password changed. Your other sessions were signed out.') }} onCancel={() => setPwOpen(false)} /></Modal>}
    </Studio.Provider>
  )
}

function L({ to, end, icon, badge, children }) {
  return <NavLink to={to} end={end} title={typeof children === 'string' ? children : undefined}><Icon name={icon} /><span className="lbl">{children}</span>{badge != null && <span className="pill warn" title="Waiting for your decision">{badge}</span>}</NavLink>
}

// the logo in both colours; styles.css shows the one that suits the theme
function Mark({ h, style }) {
  return <><img className="mark-dark" src="/curltale-logo-dark.png" alt="" height={h} style={{ width: 'auto', ...style }} /><img className="mark-pale" src="/curltale-logo-light.png" alt="" height={h} style={{ width: 'auto', ...style }} /></>
}

// light or dark: the person's choice (kept in this browser), else the system's
const systemDark = () => window.matchMedia?.('(prefers-color-scheme: dark)').matches
function useTheme() {
  const now = () => document.documentElement.dataset.theme || (systemDark() ? 'dark' : 'light')
  const [theme, setT] = useState(now)
  const set = (t) => {
    document.documentElement.dataset.theme = t; setT(t)
    try { localStorage.setItem('ss_theme', t) } catch {}
    document.querySelectorAll('link[rel=icon]').forEach((l) => { l.removeAttribute('media'); l.href = t === 'dark' ? '/favicon-light.png' : '/favicon-dark.png' })
  }
  return [theme, () => set(theme === 'dark' ? 'light' : 'dark')]
}
