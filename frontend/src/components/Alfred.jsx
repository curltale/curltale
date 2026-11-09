import Icon from './Icon.jsx'
import { useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useStudio } from '../context.jsx'
import { WORDS } from './thinking.js'

const SUGGEST = ['What are the main entities in the model, by domain?', 'How do the two most important entities join?', 'Which rules must every answer respect?', 'Which measures are defined, and which are still pending?', 'Which fields are still pending confirmation?']

// Answers never load anything by themselves: an image in Markdown would be fetched without a click, so text planted in the data could make the model
// put data into an image address and send it out. Images are not rendered, raw HTML is skipped, and only http(s), mailto and in-app links survive.
const safeUrl = (url) => (/^(https?:\/\/|mailto:|\/(?![\/\\]))/i.test(url || '') && !/[\\\s]/.test(url || '') ? url : '')
const inApp = (href) => /^\/[A-Za-z0-9_-]/.test(href || '') && !/[\\]/.test(href)        // only plain app paths go to the router (no //host, no backslash tricks)

// [[entity:key]] and [[attr:key.column]] become links into the UI
const linkify = (t) => t.replace(/\[\[entity:([a-z0-9_]+)\]\]/g, (_, k) => `[${k.replaceAll('_', ' ')}](/entities/${k})`)
  .replace(/\[\[attr:([a-z0-9_]+)\.([^\]]+)\]\]/g, (_, e, c) => `[${e}.${c}](/entities/${e})`)

function pageContext(pathname, search) {
  const m = pathname.match(/^\/entities\/(.+)$/)
  if (m) return { view: 'entity', entity: m[1] }
  if (pathname.startsWith('/examples')) return { view: 'query examples' }
  if (pathname === '/path') return { view: 'join paths', query: search }
  if (pathname === '/search') return { view: 'search', query: search }
  return { view: pathname === '/' ? 'entity list' : pathname.slice(1) }
}

// what a lookup was about, in a few words (a topic, a document and where in it, a question, a name...)
function lookupDetail(a) {
  if (!a) return ''
  const part = a.query ? a.query : a.topic ? a.topic : a.name ? a.name : a.from_entity ? `${a.from_entity}${a.to_entity ? ' → ' + a.to_entity : ''}` : a.entity ? a.entity : ''
  const where = a.around != null ? `passage ${a.around}` : ''
  const doc = a.source && !a.query ? (a.source.length > 24 && /^[0-9a-f-]{36}$/.test(a.source) ? 'document' : a.source) : ''
  const text = [part, doc && !part ? doc : '', where].filter(Boolean).join(' · ')
  return text ? `: ${text}` : ''
}

export default function Alfred() {
  const { user } = useStudio()
  const [open, setOpen] = useState(false)
  const [msgs, setMsgs] = useState(() => { try { return JSON.parse(sessionStorage.getItem('alfred') || '[]') } catch { return [] } })
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const end = useRef(null)
  const inputRef = useRef(null)
  const loc = useLocation()
  const nav = useNavigate()
  const full = loc.pathname === '/alfred'                 // the whole-screen agent experience; the floating panel is the same chat, smaller
  const back = useRef('/')
  useEffect(() => { if (!full) back.current = loc.pathname + loc.search }, [loc.pathname, loc.search, full])
  useEffect(() => { try { sessionStorage.setItem('alfred', JSON.stringify(msgs.slice(-40))) } catch {} ; end.current?.scrollIntoView({ block: 'end' }) }, [msgs, open])

  // the input stays enabled while Alfred answers (a disabled input loses focus and never gets it back); focus returns when the answer is complete
  useEffect(() => { if (!busy && (open || full)) inputRef.current?.focus() }, [busy, open, full])
  useEffect(() => { if (full) inputRef.current?.focus() }, [full])

  const send = async (text) => {
    if (!text.trim() || busy) return
    const history = [...msgs, { role: 'user', content: text }]
    let conv = null
    try { conv = sessionStorage.getItem('alfred_conv'); if (!conv || !msgs.length) { conv = Math.random().toString(36).slice(2) + Date.now().toString(36); sessionStorage.setItem('alfred_conv', conv) } } catch {}
    setMsgs([...history, { role: 'assistant', content: '', tools: [] }]); setInput(''); setBusy(true)
    if (inputRef.current) inputRef.current.style.height = 'auto'
    const patch = (fn) => setMsgs((m) => { const c = [...m]; c[c.length - 1] = fn(c[c.length - 1]); return c })
    try {
      const r = await fetch('/api/alfred', { method: 'POST', credentials: 'same-origin', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ messages: history.map(({ role, content }) => ({ role, content })), page: pageContext(full ? back.current.split('?')[0] : loc.pathname, full ? (back.current.split('?')[1] ? '?' + back.current.split('?')[1] : '') : loc.search), conversation: conv }) })
      if (!r.ok) throw new Error(r.status === 401 ? 'Session expired: sign in again' : r.statusText)
      const reader = r.body.getReader(), dec = new TextDecoder()
      let buf = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        const parts = buf.split('\n\n'); buf = parts.pop()
        for (const p of parts) {
          if (!p.startsWith('data:')) continue
          const ev = JSON.parse(p.slice(5))
          if (ev.type === 'token') patch((m) => ({ ...m, content: m.content + ev.text }))
          else if (ev.type === 'tool') patch((m) => ({ ...m, tools: [...(m.tools || []), ev] }))
          else if (ev.type === 'done') patch((m) => ({ ...m, sources: ev.sources }))
          else if (ev.type === 'error') patch((m) => ({ ...m, content: m.content + (m.content ? '\n\n' : '') + '⚠️ ' + ev.message }))
        }
      }
    } catch (e) { patch((m) => ({ ...m, content: m.content + '⚠️ ' + e.message })) }
    setBusy(false)
  }

  if (!open && !full) return <button className="alfred-fab" onClick={() => setOpen(true)}>✦ Ask Alfred</button>
  return (
    <div className={full ? 'alfred full' : 'alfred'}>
      <header>✦ Alfred
        <button className="btn small iconbtn right" title="New chat" aria-label="New chat" onClick={() => setMsgs([])} disabled={busy || !msgs.length}><Icon name="newchat" size={15} /></button>
        {!full && <button className="btn small iconbtn" title="Open Alfred full screen" aria-label="Open full screen" onClick={() => { setOpen(false); nav('/alfred') }}><Icon name="maximize" size={15} /></button>}
        {full && <button className="btn small iconbtn" title="Back to the small window" aria-label="Back to the small window" onClick={() => { setOpen(true); nav(back.current) }}><Icon name="minimize" size={15} /></button>}
        <button className="btn small iconbtn" title="Close" aria-label="Close" onClick={() => { setOpen(false); if (full) nav(back.current) }}><Icon name="close" size={15} /></button></header>
      <div className="msgs">
        {!msgs.length && <div className="suggest"><p className="muted" style={{ margin: 0 }}>Ask about entities, columns, calculated fields, joins or terms. I look things up in the model before I answer.</p>
          {SUGGEST.map((s) => <button key={s} className="btn small" onClick={() => send(s)}>{s}</button>)}</div>}
        {msgs.map((m, i) => m.role === 'user' ? <div key={i} className="bubble user">{m.content}</div> : (
          <div key={i} className="bubble bot">
            {busy && i === msgs.length - 1 && !m.content && <Thinking />}
            {m.tools?.length > 0 && !(busy && i === msgs.length - 1) && <details className="lookups"><summary>{m.tools.length} lookup{m.tools.length > 1 ? 's' : ''}</summary>
              {m.tools.map((t, j) => <div key={j}>{t.name.replaceAll('_', ' ')}{lookupDetail(t.args)}</div>)}</details>}
            <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={safeUrl} components={{ img: ({ alt }) => <span className="muted small">[image not shown{alt ? `: ${alt}` : ''}]</span>, a: ({ href, children }) => inApp(href) ? (full ? <Link to={href} target="_blank">{children}</Link> : <Link to={href} onClick={() => setOpen(false)}>{children}</Link>) : <a href={href} target="_blank" rel="noreferrer">{children}</a> }}>
              {linkify(m.content || '')}
            </ReactMarkdown>
            {m.sources?.length > 0 && <div className="chips">{m.sources.slice(0, 8).map((s) => s.id.startsWith('/library/') ? <Link key={s.id} to={s.id} className="chip" target={full ? '_blank' : undefined} onClick={() => !full && setOpen(false)}>{s.title}</Link> : <span key={s.id} className="chip" title={s.id}>{s.title}</span>)}</div>}
          </div>))}
        <div ref={end} />
      </div>
      <form onSubmit={(e) => { e.preventDefault(); send(input) }}>
        <textarea ref={inputRef} rows={1} placeholder={busy ? 'Alfred is answering… you can already type your next question' : 'Ask about the model or the data… (Enter to send, Shift+Enter for a new line)'} value={input}
          onChange={(e) => { setInput(e.target.value); e.target.style.height = 'auto'; e.target.style.height = Math.min(e.target.scrollHeight, 160) + 'px' }}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); if (!busy) send(input) } }} />
        <button className="btn primary" disabled={busy || !input.trim()}>Send</button>
      </form>
    </div>
  )
}

// While Alfred works: one word at a time from the vocabulary of semantics, with breathing dots (the lookups themselves are listed, folded, once done)
function Thinking() {
  const pick = () => WORDS[Math.floor(Math.random() * WORDS.length)]
  const [w, setW] = useState(pick)
  useEffect(() => { const t = setInterval(() => setW(pick()), 1800); return () => clearInterval(t) }, [])
  return <div className="thinking"><span className="spark">✦</span><span key={w} className="word">{w}</span><span className="dots"><i>.</i><i>.</i><i>.</i></span></div>
}
