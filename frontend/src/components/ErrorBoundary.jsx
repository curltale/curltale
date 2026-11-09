import { Component } from 'react'

/** A crash in one page must not blank the whole app: show what happened and how to continue. */
export default class ErrorBoundary extends Component {
  state = { error: null }
  static getDerivedStateFromError(error) { return { error } }
  componentDidCatch(error, info) { console.error('page crashed', error, info?.componentStack) }
  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="card" style={{ maxWidth: 640 }}>
        <h1>This page ran into a problem</h1>
        <p className="muted">The rest of the app still works. If it keeps happening, send this message to the developer:</p>
        <pre className="mono small" style={{ whiteSpace: 'pre-wrap' }}>{String(this.state.error?.message || this.state.error)}</pre>
        <button className="btn primary" onClick={() => location.reload()}>Reload the page</button>
      </div>
    )
  }
}
