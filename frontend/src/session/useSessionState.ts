import { useCallback, useMemo } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { logOut, type SessionContextValue, type SessionState } from './session'
import { fetchSession } from './session'

export const SESSION_QUERY_KEY = ['session'] as const

/**
 * Estado de sesión de la aplicación, sobre la caché de TanStack Query.
 *
 * Sin `useEffect`: la pregunta «¿quién soy?» es una consulta al servidor, y la
 * caché ya sabe cuándo está pendiente, cuándo falló y cuándo hay que repetirla.
 * `retry: false` porque un `401` no mejora por insistir.
 */
export function useSessionState(): SessionContextValue {
  const queryClient = useQueryClient()

  const query = useQuery({
    queryKey: SESSION_QUERY_KEY,
    queryFn: fetchSession,
    retry: false,
    staleTime: Infinity,
    gcTime: Infinity,
  })

  const session = useMemo<SessionState>(() => {
    if (query.isPending) return { status: 'loading', user: null, error: null }
    if (query.isError) {
      return {
        status: 'anonymous',
        user: null,
        error: query.error instanceof Error ? query.error.message : 'No se pudo comprobar la sesión',
      }
    }
    return query.data === null || query.data === undefined
      ? { status: 'anonymous', user: null, error: null }
      : { status: 'authenticated', user: query.data, error: null }
  }, [query.isPending, query.isError, query.error, query.data])

  const signOut = useCallback(async () => {
    try {
      await logOut()
    } finally {
      queryClient.setQueryData(SESSION_QUERY_KEY, null)
    }
  }, [queryClient])

  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: SESSION_QUERY_KEY })
  }, [queryClient])

  return useMemo(
    () => ({
      session,
      isOperador: session.user?.role === 'operador',
      refresh,
      signOut,
    }),
    [session, refresh, signOut],
  )
}
