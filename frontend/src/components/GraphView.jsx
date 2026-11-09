import { useEffect, useMemo, useRef, useState } from 'react'
import { forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY } from 'd3-force'
import { select } from 'd3-selection'
import { zoom } from 'd3-zoom'

const PALETTE = ['#0b7a75', '#e8590c', '#2f9e44', '#ae3ec9', '#1971c2', '#d6336c', '#f08c00', '#5c7cfa', '#868e96', '#66a80f']   // led by the brand teal; no other teal, so domains stay distinct
const EDGE = { confirmed: ['#98a1ba', ''], optional: ['#d99a2b', '5 4'], unreliable: ['#e05252', '3 3'] }

export default function GraphView({ nodes, edges, highlight = [], onNode, height }) {
  const ref = useRef(null), gref = useRef(null)
  const [pos, setPos] = useState({})
  const [vb, setVb] = useState('-520 -380 1040 760')
  const [hover, setHover] = useState(null)
  const domains = useMemo(() => [...new Set(nodes.map((n) => n.domain))], [nodes])
  const color = (d) => PALETTE[domains.indexOf(d) % PALETTE.length]

  useEffect(() => {
    const ns = nodes.map((n) => ({ ...n }))
    const idx = Object.fromEntries(ns.map((n) => [n.key, n]))
    const links = edges.filter((e) => idx[e.from] && idx[e.to] && e.from !== e.to).map((e) => ({ source: e.from, target: e.to }))
    const cols = Math.ceil(Math.sqrt(domains.length))
    const sim = forceSimulation(ns).force('link', forceLink(links).id((d) => d.key).distance(70).strength(0.5)).force('charge', forceManyBody().strength(-260))
      .force('x', forceX((d) => ((domains.indexOf(d.domain) % cols) - (cols - 1) / 2) * 260).strength(0.16))
      .force('y', forceY((d) => (Math.floor(domains.indexOf(d.domain) / cols) - 1) * 230).strength(0.16)).force('collide', forceCollide(30)).stop()
    for (let i = 0; i < 320; i++) sim.tick()
    setPos(Object.fromEntries(ns.map((n) => [n.key, [n.x, n.y]])))
    const xs = ns.map((n) => n.x).sort((a, b) => a - b), ys = ns.map((n) => n.y).sort((a, b) => a - b)
    const lo = (a) => a[Math.floor(a.length * 0.02)], hi = (a) => a[Math.ceil(a.length * 0.98) - 1]  // ignore a few stray nodes
    const [x0, x1, y0, y1] = [lo(xs) - 70, hi(xs) + 70, lo(ys) - 60, hi(ys) + 60]
    setVb(`${x0} ${y0} ${x1 - x0} ${y1 - y0}`)
  }, [nodes, edges])

  useEffect(() => {
    const svg = select(ref.current)
    const z = zoom().scaleExtent([0.3, 4]).filter((ev) => ev.type !== 'wheel' || !height || ev.ctrlKey).on('zoom', (ev) => gref.current && gref.current.setAttribute('transform', ev.transform.toString()))
    svg.call(z)
    return () => svg.on('.zoom', null)
  }, [height])

  const hl = new Set(highlight)
  const near = hover ? new Set([hover, ...edges.filter((e) => e.from === hover || e.to === hover).flatMap((e) => [e.from, e.to])]) : null
  const dim = (k) => (near && !near.has(k)) || (hl.size && !hl.has(k) && !near)

  return (
    <div className="graph-wrap" style={height ? { height } : null}>
      <svg ref={ref} viewBox={vb}>
        <g ref={gref}>
          {edges.map((e, i) => {
            const a = pos[e.from], b = pos[e.to]
            if (!a || !b) return null
            const [c, d] = EDGE[e.status] || EDGE.confirmed
            const on = (hl.has(e.from) && hl.has(e.to)) || (hover && (e.from === hover || e.to === hover))
            return <line key={i} x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} stroke={c} strokeDasharray={d} strokeWidth={on ? 2.4 : 1} opacity={near && !on ? 0.08 : hl.size && !on ? 0.12 : 0.7} />
          })}
          {nodes.map((n) => {
            const p = pos[n.key]
            if (!p) return null
            return (
              <g key={n.key} transform={`translate(${p[0]},${p[1]})`} style={{ cursor: 'pointer' }} opacity={dim(n.key) ? 0.18 : 1}
                onMouseEnter={() => setHover(n.key)} onMouseLeave={() => setHover(null)} onClick={() => onNode && onNode(n)}>
                <circle r={hl.has(n.key) ? 11 : 8} fill={color(n.domain)} stroke="var(--panel)" strokeWidth="2" />
                <text y={-13} textAnchor="middle">{n.name.split(' (')[0]}</text>
              </g>
            )
          })}
        </g>
      </svg>
      <div className="legend">
        {domains.map((d) => <span key={d}><span className="dot" style={{ background: color(d) }} />{d}</span>)}
        <span style={{ width: '100%' }} className="muted">Lines: solid confirmed · amber optional · red unreliable. Drag to pan, scroll to zoom.</span>
      </div>
    </div>
  )
}
