async function req(method, url, body) {
  const r = await fetch(url, { method, credentials: 'same-origin', headers: body ? { 'content-type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined })
  if (!r.ok) {
    let msg = r.statusText
    try { const j = await r.json(); msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail || j) } catch {}
    const e = new Error(msg); e.status = r.status; throw e
  }
  if (method !== 'GET') window.dispatchEvent(new Event('studio:changed'))        // something changed on the server: the menu's counters (pending items...) are asked again
  return r.json()
}
export const api = {
  get: (u) => req('GET', u), post: (u, b) => req('POST', u, b || {}), put: (u, b) => req('PUT', u, b), patch: (u, b) => req('PATCH', u, b), del: (u) => req('DELETE', u),
}
export const q = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== '')).toString()
