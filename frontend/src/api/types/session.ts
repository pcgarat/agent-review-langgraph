/** Sesión del panel (`GET /api/session`, spec 029 P6/P7). */

export type Role = 'lector' | 'operador'

export interface SessionUser {
  user: string
  role: Role
}
