// Thin fetch wrapper: JSON in/out, session cookie auth, FastAPI's
// `{"detail": "..."}` error shape surfaced as Error.message.
export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

export async function apiFetch<T = unknown>(url: string, options?: RequestInit): Promise<T> {
  const method = options?.method?.toUpperCase() ?? 'GET'
  const headers: Record<string, string> = {}
  if (options?.headers) Object.assign(headers, options.headers)
  if (['POST', 'PUT', 'PATCH'].includes(method) && options?.body) {
    headers['Content-Type'] = 'application/json'
  }

  const response = await fetch(url, {
    ...options,
    credentials: 'include',
    headers,
  })

  if (!response.ok) {
    let message = `HTTP ${response.status}`
    try {
      const body = await response.json()
      if (body?.detail) {
        message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
      }
    } catch {
      // body wasn't JSON — keep the generic message
    }
    throw new ApiError(message, response.status)
  }

  const contentType = response.headers.get('content-type') ?? ''
  if (response.status === 204 || !contentType.includes('application/json')) {
    return undefined as T
  }
  return response.json() as Promise<T>
}
