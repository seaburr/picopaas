// Thin fetch wrapper: JSON in/out, session cookie auth. Errors are always ApiError:
//  - message: one human-readable string (FastAPI `detail` string, or 422 array formatted as "field: message" lines)
//  - fieldErrors: per-field map (e.g. { name: 'String should have at least 1 character' }) for inline form errors
//  - status: HTTP status (0 = network failure)
export class ApiError extends Error {
  status: number
  fieldErrors: Record<string, string>
  constructor(message: string, status: number, fieldErrors: Record<string, string> = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.fieldErrors = fieldErrors
  }
}

type UnauthorizedHandler = () => void
let onUnauthorized: UnauthorizedHandler | null = null
// main.ts registers the handler (clear auth store, route to /login?next=...). Kept injectable to avoid import cycles.
export function setUnauthorizedHandler(fn: UnauthorizedHandler | null) {
  onUnauthorized = fn
}

interface ValidationItem {
  loc?: (string | number)[]
  msg?: string
}

function cleanMsg(msg: string): string {
  return msg.replace(/^Value error, /, '')
}

// FastAPI 422: detail is [{loc:['body','env',0,'key'], msg:'...', type:'...'}]. Returns text + per-field map.
export function formatDetail(detail: unknown): { message: string; fields: Record<string, string> } {
  if (typeof detail === 'string') return { message: detail, fields: {} }
  if (Array.isArray(detail)) {
    const fields: Record<string, string> = {}
    const lines: string[] = []
    for (const raw of detail as ValidationItem[]) {
      const msg = cleanMsg(String(raw?.msg ?? 'Invalid value'))
      const loc = (raw?.loc ?? []).filter((p) => p !== 'body' && p !== 'query' && p !== 'path')
      const path = loc.join('.')
      if (path) {
        if (!(path in fields)) fields[path] = msg
        const top = String(loc[0])
        if (!(top in fields)) fields[top] = msg
        lines.push(`${path}: ${msg}`)
      } else {
        lines.push(msg)
      }
    }
    return { message: lines.join('\n') || 'Invalid request.', fields }
  }
  if (detail && typeof detail === 'object') return { message: JSON.stringify(detail), fields: {} }
  return { message: '', fields: {} }
}

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const method = options?.method?.toUpperCase() ?? 'GET'
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (options?.headers) Object.assign(headers, options.headers)
  if (['POST', 'PUT', 'PATCH'].includes(method) && options?.body && !headers['Content-Type']) {
    headers['Content-Type'] = 'application/json'
  }

  let response: Response
  try {
    // cache: 'no-store' so the browser never serves a cached SPA HTML page (same URL, navigation) to an API call.
    response = await fetch(url, { cache: 'no-store', ...options, credentials: 'include', headers })
  } catch {
    throw new ApiError("Can't reach the server. Check your connection and try again.", 0)
  }

  if (!response.ok) {
    let message = `Request failed (HTTP ${response.status}).`
    let fields: Record<string, string> = {}
    try {
      const body = await response.json()
      const f = formatDetail(body?.detail)
      if (f.message) message = f.message
      fields = f.fields
    } catch {
      // body wasn't JSON: keep the generic message
    }
    // A 401 from anything but /auth/* means the session expired: send the user to log in, then back here.
    if (response.status === 401 && !url.startsWith('/auth/')) onUnauthorized?.()
    throw new ApiError(message, response.status, fields)
  }

  if (response.status === 204) return undefined as T
  const contentType = response.headers.get('content-type') ?? ''
  if (contentType.includes('application/json')) return response.json() as Promise<T>
  const text = await response.text()
  if (text === '') return undefined as T
  throw new ApiError('Unexpected response from the server (expected JSON). Reload the page and try again.', response.status)
}

// Authenticated download: fetch with the session cookie, then save the blob. Filename from Content-Disposition,
// else fallbackName. Throws ApiError (message from FastAPI `detail`) on failure.
export async function downloadFile(path: string, fallbackName: string): Promise<void> {
  let response: Response
  try {
    response = await fetch(path, { cache: 'no-store', credentials: 'include' })
  } catch {
    throw new ApiError("Can't reach the server. Check your connection and try again.", 0)
  }
  if (!response.ok) {
    let message = `Download failed (HTTP ${response.status}).`
    try {
      const f = formatDetail((await response.json())?.detail)
      if (f.message) message = f.message
    } catch {
      // not JSON
    }
    if (response.status === 401 && !path.startsWith('/auth/')) onUnauthorized?.()
    throw new ApiError(message, response.status)
  }
  const cd = response.headers.get('content-disposition') ?? ''
  const m = /filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i.exec(cd)
  let name = fallbackName
  if (m) {
    try {
      name = decodeURIComponent(m[1] ?? m[2] ?? '') || fallbackName
    } catch {
      name = m[2] ?? fallbackName
    }
  }
  const url = URL.createObjectURL(await response.blob())
  const el = document.createElement('a')
  el.href = url
  el.download = name
  document.body.appendChild(el)
  el.click()
  el.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

// ---- GET sharing + short-lived cache ----
// Concurrent identical GETs share one request (two components asking for /projects at once cost one round trip).
// Every GET result is also remembered for CACHE_TTL_MS, but only apiGetSwr reads it: plain apiFetch always hits the
// network, so polls and "refresh" buttons stay fresh. Any non-GET (success or failure) and every sign-in/sign-out drops
// the whole cache, so a list shown after a save/delete is never the old one and no account sees another's data.
const CACHE_TTL_MS = 30_000 // beyond this a cached value is not shown at all
const FRESH_MS = 8_000 // within this a cached value is used as is, with no revalidation
const cache = new Map<string, { at: number; json: string }>()
const inflight = new Map<string, Promise<string>>()
let generation = 0

const clearListeners = new Set<() => void>()

export function clearApiCache() {
  generation++
  cache.clear()
  inflight.clear()
  clearListeners.forEach((fn) => fn())
}

// Called after every cache clear (any write, sign-in/out): for small badges that should follow a write at once.
// Returns the unsubscribe function.
export function onApiCacheCleared(fn: () => void): () => void {
  clearListeners.add(fn)
  return () => clearListeners.delete(fn)
}

// Changes whenever something was written (or the account changed): lets a caller know its own earlier GET may be stale.
export const apiGeneration = () => generation

const parse = <T>(json: string): T => (json === '' ? (undefined as T) : (JSON.parse(json) as T))

// GET as a JSON string (cheap to share between callers: each gets its own parsed copy, nobody can mutate another's).
function getJson(url: string, options?: RequestInit): Promise<string> {
  const shared = !options?.signal
  const hit = shared ? inflight.get(url) : undefined
  if (hit) return hit
  const gen = generation
  const p = request<unknown>(url, options).then((v) => {
    const json = v === undefined ? '' : JSON.stringify(v)
    if (gen === generation && !url.startsWith('/auth/')) cache.set(url, { at: Date.now(), json })
    return json
  })
  if (shared) {
    inflight.set(url, p)
    const done = () => {
      if (inflight.get(url) === p) inflight.delete(url)
    }
    p.then(done, done)
  }
  return p
}

export async function apiFetch<T = unknown>(url: string, options?: RequestInit): Promise<T> {
  const method = options?.method?.toUpperCase() ?? 'GET'
  if (method === 'GET') return parse<T>(await getJson(url, options))
  try {
    return await request<T>(url, options)
  } finally {
    clearApiCache()
  }
}

// Stale-while-revalidate GET for page loads: a result under CACHE_TTL_MS old is returned at once; unless it is under
// FRESH_MS old it is also re-fetched in the background and `onFresh` gets the new value if it differs. Older or missing:
// a normal request. Not for polling, and not for anything that decides auth.
export async function apiGetSwr<T = unknown>(url: string, onFresh?: (value: T) => void): Promise<T> {
  const hit = cache.get(url)
  const age = hit ? Date.now() - hit.at : Infinity
  if (!hit || age >= CACHE_TTL_MS) return parse<T>(await getJson(url))
  if (age >= FRESH_MS) {
    const gen = generation
    getJson(url).then(
      (json) => {
        if (json !== hit.json && gen === generation) onFresh?.(parse<T>(json))
      },
      () => {}, // the cached value stays; a 401 was already handled by request()
    )
  }
  return parse<T>(hit.json)
}
