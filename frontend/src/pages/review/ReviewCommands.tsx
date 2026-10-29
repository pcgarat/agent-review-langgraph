import { CButton, CCard, CCardBody, CCardHeader } from '@coreui/react'
import { useQueryClient } from '@tanstack/react-query'

import { queryKeys } from '@/api/queryKeys'
import type { ReviewTrace } from '@/api/types/review'
import { usePanelCommand } from '@/pages/dashboard/usePanelCommand'

import { ForgetReviewButton } from './ForgetReviewButton'

interface ReviewCommandsProps {
  reviewKey: string
  /** Sin traza no hay nada que cancelar: solo queda olvidar. */
  trace: ReviewTrace | null
  canCommand: boolean
}

/**
 * Cancelar una review con recorrido (029, F4; spec §5.4).
 *
 * Las etiquetas salen de la misma tabla que `_trace_commands` en `ui.py`: una pausa
 * humana no ofrece «Reanudar» (decidir es la acción, 022 §5.5 Q1) y una pausa de
 * fix ya publicó, así que cancelar es «lo pendiente». Un hilo completado no tiene
 * nada que cancelar y solo conserva «Olvidar».
 */
function cancelLabel(trace: ReviewTrace | null): string | null {
  if (trace === null || trace.state === 'completed') return null
  if (trace.pause_kind === 'fix') return 'Cancelar lo pendiente'
  if (trace.pause_kind === 'human') return 'Cancelar la review'
  return 'Cancelar'
}

export function ReviewCommands({ reviewKey, trace, canCommand }: ReviewCommandsProps) {
  const mutation = usePanelCommand()
  const queryClient = useQueryClient()
  const label = cancelLabel(trace)

  const cancel = () => {
    mutation.mutate(
      { reviewKey, command: 'cancel' },
      {
        // `usePanelCommand` invalida estado y bandeja; la ficha tiene su propia
        // clave, así que se invalida aquí para que el recorrido deje de mentir.
        onSettled: () => {
          void queryClient.invalidateQueries({ queryKey: queryKeys.review(reviewKey) })
        },
      },
    )
  }

  return (
    <CCard className="review-card">
      <CCardHeader>
        <h2 className="h6 mb-0">Comandos</h2>
      </CCardHeader>
      <CCardBody>
        {canCommand ? (
          <div className="d-flex flex-wrap gap-2">
            {label !== null ? (
              <CButton
                type="button"
                color="secondary"
                variant="outline"
                size="sm"
                disabled={mutation.isPending}
                onClick={cancel}
              >
                {mutation.isPending ? '…' : label}
              </CButton>
            ) : null}
            <ForgetReviewButton reviewKey={reviewKey} />
          </div>
        ) : (
          <p className="small text-body-secondary mb-0">
            Tu rol es <strong>lector</strong>: puedes leer la ficha, pero los comandos son del rol
            operador.
          </p>
        )}
      </CCardBody>
    </CCard>
  )
}
