import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/api/client'
import { AFTER_COMMAND, queryKeys } from '@/api/queryKeys'
import type { BandejaView } from '@/api/types/bandeja'
import { useToast } from '@/components/ToastProvider'

import { decisionPath, describeDecisionError, type DecisionOutcome, type PublishBatch } from './decisions'

interface BandejaQueryOptions {
  refetchInterval?: number | false
}

/** Las pausas abiertas, con la misma clave que invalida cualquier comando. */
export function useBandejaQuery(options?: BandejaQueryOptions) {
  return useQuery({
    queryKey: queryKeys.bandeja,
    queryFn: () => api.get<BandejaView>('/api/bandeja'),
    refetchInterval: options?.refetchInterval,
  })
}

export interface DecisionItem {
  finding_id: string
  outcome: DecisionOutcome
}

/** El cuerpo es `items` o `batch`, nunca los dos: el servidor responde `400` si no. */
export type DecisionBody = { items: DecisionItem[] } | { batch: PublishBatch }

/** Respuesta de `_effect` en `web/bandeja.py`. */
interface DecisionResult {
  review_key: string
  effect: string
  actor: string
}

/**
 * Responder una pausa (029, B1).
 *
 * Vale la primera respuesta: si otra persona llegó antes, el servidor responde
 * `409` y el aviso lo explica con su texto, no con un genérico. La invalidación va
 * en `onSettled` porque el fallo tampoco garantiza que el estado no haya cambiado
 * (un `409` significa justo que cambió).
 */
export function useDecide(reviewKey: string) {
  const queryClient = useQueryClient()
  const { notify } = useToast()

  return useMutation({
    mutationFn: (body: DecisionBody) => api.post<DecisionResult>(decisionPath(reviewKey), body),
    onSuccess: (result) => {
      notify({
        title: 'Decisión enviada',
        body:
          result.effect === 'enqueued'
            ? `La review se reanudará cuando el worker recoja el trabajo (${result.actor}).`
            : `El servidor respondió «${result.effect}».`,
      })
    },
    onError: (error) => {
      const { title, body, tone } = describeDecisionError(error)
      notify({ title, body, color: tone })
    },
    onSettled: () => {
      for (const key of AFTER_COMMAND) {
        void queryClient.invalidateQueries({ queryKey: key })
      }
    },
  })
}
