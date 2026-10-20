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

export async function apiFetch<T = unknown>(url: string, options?: RequestInit): Promise<T> {
  const method = options?.method?.toUpperCase() ?? 'GET'
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (options?.headers) Object.assign(headers, options.headers)
  if (['POST', 'PUT', 'PATCH'].includes(method) && options?.body) {
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
