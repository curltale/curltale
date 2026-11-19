// The collection a person is working in, remembered in this browser so every Knowledge page opens on it. A link that names a collection (?c=)
// still wins, and following it makes that collection the current one. Storage can be unavailable (private windows): then pages fall back to the first.
const KEY = 'curltale.collection'

export function currentCollection() {
  try { return localStorage.getItem(KEY) } catch { return null }
}

export function rememberCollection(id) {
  if (!id || id === currentCollection()) return
  try { localStorage.setItem(KEY, id) } catch { /* not remembered: nothing else depends on it */ }
  window.dispatchEvent(new CustomEvent('studio:collection', { detail: id }))
}

// The collection a page should open on: the remembered one when the person may use it there, otherwise the first.
export function pickCollection(cols) {
  const keep = currentCollection()
  return (cols.find((c) => c.id === keep) || cols[0])?.id
}
