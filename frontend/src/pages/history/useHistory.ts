import { useQuery } from '@tanstack/react-query'

import { api } from '@/api/client'
import { queryKeys } from '@/api/queryKeys'
import type { HistoryItem } from '@/api/types/history'

export interface HistoryFilters {
  repo?: string
  pr_id?: number
}

/**
 * Tope del contrato (`GET /api/history`, `limit` entre 1 y 500).
 *
 * Se pide el máximo que permite la API y se pagina en el cliente (029, Q5): el
 * servidor no cambia y el operador ve el histórico entero de una vez en lugar de
 * esperar una ida y vuelta por cada página.
 */
export const HISTORY_LIMIT = 500

/** `repo` y `pr_id` viajan solo si hay valor: un filtro vacío no es un filtro. */
export function historyPath({ repo, pr_id }: HistoryFilters): string {
  const params = new URLSearchParams()
  if (repo) params.set('repo', repo)
  if (pr_id !== undefined) params.set('pr_id', String(pr_id))
  params.set('limit', String(HISTORY_LIMIT))
  return `/api/history?${params.toString()}`
}

interface HistoryQueryOptions {
  refetchInterval?: number | false
}

export function useHistoryQuery(filters: HistoryFilters, options?: HistoryQueryOptions) {
  return useQuery({
    queryKey: queryKeys.history(filters),
    queryFn: () => api.get<HistoryItem[]>(historyPath(filters)),
    refetchInterval: options?.refetchInterval,
  })
}
