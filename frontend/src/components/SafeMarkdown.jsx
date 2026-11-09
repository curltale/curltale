import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Link } from 'react-router-dom'

// Markdown that came from a model or a document is untrusted: no raw HTML, no images (they would load from anywhere), only http(s) and mailto links, opened in a new tab.
const safeUrl = (url) => (/^(https?:\/\/|mailto:|\/library\/)/i.test(url || '') && !/[\\\s]/.test(url || '') ? url : '')
// cite: optional function (n) => element. `[c3]` markers in the text become citation chips (they go through inline code, which renders no HTML of its own).
// source: optional function (id) => element. Topic pages cite their sources as `[source: <id>]` (usually inside backticks): chips too.
// A zero-width space follows each placeholder so that adjacent citations ([c1][c4]) do not become a double-backtick code fence.
const SOURCE_REF = /`?\[source:\s*([0-9a-f-]{36})\]`?/gi
// topics: optional { cid, titles } - `[[topic-slug]]` links between topic pages (the topic writer emits them) open that topic in the app.
// sources: optional { cid, names } - the simple way to get source chips: each `[source: <id>]` becomes a link to that source, named when known.
export function sourceChips({ cid, names }) {
  return (id) => <Link key={Math.random()} to={`/library/sources/${id}?c=${cid}`} className="cite" title={names?.[id] ? `Source: ${names[id]}` : 'Source'}
    style={{ fontSize: 11.5, fontWeight: 600, background: 'var(--accent-soft)', color: 'var(--accent)', borderRadius: 5, padding: '0 6px', margin: '0 1px', textDecoration: 'none', whiteSpace: 'nowrap' }}>{names?.[id] || 'source'}</Link>
}

export default function SafeMarkdown({ children, cite, source, sources, topics }) {
  if (!source && sources) source = sourceChips(sources)
  let text = children || ''
  if (topics) text = text.replace(/\[\[([a-z0-9][a-z0-9-]{0,118})\]\]/g, (_, slug) => `[${(topics.titles || {})[slug] || slug.replace(/-/g, ' ')}](/library/topics/${slug}?c=${topics.cid})`)
  if (cite) text = text.replace(/\[c(\d+)\]/g, (_, n) => `\`\u27e6c${n}\u27e7\`\u200b`)
  if (source) text = text.replace(SOURCE_REF, (_, id) => `\`\u27e6s:${id.toLowerCase()}\u27e7\`\u200b`)

  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={safeUrl}
      components={{ code: ({ children: c, className }) => { const m = cite && /^\u27e6c(\d+)\u27e7$/.exec(String(c)); if (m) return cite(+m[1]); const sm = source && /^\u27e6s:([0-9a-f-]{36})\u27e7$/.exec(String(c)); return sm ? source(sm[1]) : <code className={className}>{c}</code> }, img: ({ alt }) => <span className="muted small">[image not shown{alt ? `: ${alt}` : ''}]</span>, a: ({ href, children: c }) => !href ? <span>{c}</span> : href.startsWith('/library/') ? <Link to={href}>{c}</Link> : <a href={href} target="_blank" rel="noreferrer noopener">{c}</a> }}>
      {text}
    </ReactMarkdown>
  )
}
