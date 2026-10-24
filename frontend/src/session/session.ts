/**
 * Sesión del panel (029, P6/P7).
 *
 * La cookie es `HttpOnly`: el navegador no puede leerla, así que el rol se
 * pregunta a `GET /api/session` y no se duplica en ningún almacén local.
 *
 * Mientras S1–S3 no existan en el servidor, `GET /api/session` responde `401` y
 * la aplicación se queda en `/login`: es el comportamiento correcto de una SPA
 * sin sesión, no un fallo que haya que esconder.
 */

import { createContext, useContext } from 'react'

import { api, ApiError } from '@/api/client'
import type { SessionUser } from '@/api/types'

export type Role = 'lector' | 'operador'

export interface SessionState {
  status: 'loading' | 'anonymous' | 'authenticated'
  user: SessionUser | null
  error: string | null
}

export const ANONYMOUS: SessionState = { status: 'anonymous', user: null, error: null }

export async function fetchSession(): Promise<SessionUser | null> {
  try {
    return await api.get<SessionUser>('/api/session')
  } catch (error) {
    // `401` es la respuesta normal de quien no ha entrado; cualquier otra cosa
    // (503, red) también deja la app en el login, pero con el motivo visible.
    if (error instanceof ApiError && error.status === 401) return null
    throw error
  }
}

export async function logIn(token: string): Promise<SessionUser> {
  return api.post<SessionUser>('/api/session', { token })
}

export async function logOut(): Promise<void> {
  await api.del<void>('/api/session')
}

export interface SessionContextValue {
  session: SessionState
  isOperador: boolean
  refresh: () => void
  signOut: () => Promise<void>
}

export const SessionContext = createContext<SessionContextValue | null>(null)

export function useSession(): SessionContextValue {
  const value = useContext(SessionContext)
  if (value === null) {
    throw new Error('useSession fuera de <SessionProvider>')
  }
  return value
}
