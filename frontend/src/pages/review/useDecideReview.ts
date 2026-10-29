import { useMutation, useQueryClient } from '@tanstack/react-query'

import { api, reviewPath } from '@/api/client'
import { AFTER_COMMAND, queryKeys } from '@/api/queryKeys'
import { useToast } from '@/components/ToastProvider'
import {
  describeDecisionError,
  type DecisionOutcome,
} from '@/pages/bandeja/decisions'

/** Respuesta de `_run_command` (`web/main.py`): qué hizo el comando y quién. */
interface CommandResult {
  review_key: string
  effect: string
  actor: string
}

export interface DecisionItem {
  finding_id: string
  outcome: DecisionOutcome
  note: string
}

/**
 * Decidir una pausa humana desde la ficha (029, F4).
 *
 * El cuerpo es `{"items": [{finding_id, outcome, note}]}`, que es justo lo que
 * parsea `_decision_items` en `web/main.py` y valida `build_decision`: el cliente
 * no reimplementa la completitud ni los resultados permitidos. Tras decidir, la
 * ficha deja de ser verdad (la pausa se cierra), así que se invalida también su
 * propia clave y no solo la del estado.
 */
export function useDecideReview(reviewKey: string) {
  const queryClient = useQueryClient()
  const { notify } = useToast()

  return useMutation({
    mutationFn: (items: DecisionItem[]) =>
      api.post<CommandResult>(`${reviewPath(reviewKey)}/decision`, { items }),
    onSuccess: (result) => {
      notify({
        title: 'Decisión enviada',
        body: `La review se reanudará cuando el worker recoja el trabajo (${result.actor}).`,
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
      void queryClient.invalidateQueries({ queryKey: queryKeys.review(reviewKey) })
    },
  })
}
