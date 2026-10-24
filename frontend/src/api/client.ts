/**
 * Cliente de la API JSON del panel.
 *
 * Todo va con `credentials: "same-origin"`: la sesion es una cookie `HttpOnly` y
 * el servidor exige `Content-Type: application/json` en los comandos por cookie
 * (`require_command_session`). Un `fetch` de otro origen no puede cumplir las dos.
 */

export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, detail: unknown) {
    super(typeof detail === 'string' ? detail : `HTTP ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }

  /** Detalle que mando FastAPI en `{"detail": …}`, ya sea texto o un objeto. */
  get detailText(): string {
    const detail = this.detail
    if (typeof detail === 'string') return detail
    if (detail && typeof detail === 'object') {
      const nested = (detail as { detail?: unknown }).detail
      if (typeof nested === 'string') return nested
      const field = (detail as { field?: unknown }).field
      if (typeof field === 'string') return `${field}: ${nested ?? ''}`.trim()
    }
    return this.message
  }
}

async function parse(response: Response): Promise<unknown> {
  if (response.status === 204) return null
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      ...(init.body === undefined ? {} : { 'Content-Type': 'application/json' }),
      ...init.headers,
    },
  })
  const body = await parse(response)
  if (!response.ok) {
    const detail = body && typeof body === 'object' ? body : response.statusText
    throw new ApiError(response.status, detail)
  }
  return body as T
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? '' : JSON.stringify(body) }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
}

/** Ruta de una review: la `review_key` lleva `/` y `:`, se codifica por segmento. */
export function reviewPath(reviewKey: string): string {
  return `/api/reviews/${reviewKey.split('/').map(encodeURIComponent).join('/')}`
}

/** Ruta del navegador para la ficha. Con splat, la clave viaja tal cual. */
export function reviewHref(reviewKey: string): string {
  return `/review/${reviewKey}`
}
