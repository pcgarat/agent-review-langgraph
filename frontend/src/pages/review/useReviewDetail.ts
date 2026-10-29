import { useQuery } from '@tanstack/react-query'

import { api, reviewPath } from '@/api/client'
import { queryKeys } from '@/api/queryKeys'
import type { ReviewDetail } from '@/api/types/review'

interface ReviewDetailOptions {
  refetchInterval?: number | false
}

/**
 * La ficha entera en una petición (`GET /api/reviews/{review_key}`, 029 P8).
 *
 * El contrato ya dice si hay consola, razonamiento y actividad del CLI: la ficha
 * no pregunta por canales que el servidor no tiene, y no abre ningún SSE antes de
 * saber si el canal existe.
 */
export function useReviewDetail(reviewKey: string, options?: ReviewDetailOptions) {
  const globalInterval = options?.refetchInterval

  return useQuery({
    queryKey: queryKeys.review(reviewKey),
    queryFn: () => api.get<ReviewDetail>(reviewPath(reviewKey)),
    refetchOnWindowFocus: true,
    refetchInterval: (query) => {
      const state = query.state.data?.trace?.state
      const inProgressInterval = state === 'in_progress' || state === 'paused' ? 2000 : false

      if (globalInterval === false || globalInterval === undefined) {
        return inProgressInterval
      }
      if (inProgressInterval === false) {
        return globalInterval
      }
      return Math.min(globalInterval, inProgressInterval)
    },
  })
}
