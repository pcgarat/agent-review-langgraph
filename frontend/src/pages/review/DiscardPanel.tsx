import { CButton, CCard, CCardBody, CCardHeader } from '@coreui/react'
import { useQueryClient } from '@tanstack/react-query'

import { queryKeys } from '@/api/queryKeys'
import { useStateQuery } from '@/api/queries/useStateQuery'
import { formatDateTime } from '@/pages/dashboard/format'
import { usePanelCommand } from '@/pages/dashboard/usePanelCommand'

import { ForgetReviewButton } from './ForgetReviewButton'

interface DiscardPanelProps {
  reviewKey: string
  isOperador: boolean
}

/**
 * Ficha sin recorrido (029, F8): un descarte (`_discard_page`) o una review que
 * solo dejó log (`_log_page`).
 *
 * El recurso agregado no lleva la DLQ (spec §5.3), así que el motivo y los intentos
 * salen de `GET /api/state`, que ya está en caché porque el sidebar lo consulta.
 * Los comandos son los de la UI antigua: reintentar solo si la review sigue en la
 * DLQ y olvidar siempre. Con un `404` del estado la ficha sigue enseñando el log:
 * eso es un dato, no un fallo.
 */
export function DiscardPanel({ reviewKey, isOperador }: DiscardPanelProps) {
  const { data } = useStateQuery()
  const dead = data?.dead.find((item) => item.review_key === reviewKey) ?? null
  const mutation = usePanelCommand()
  const queryClient = useQueryClient()

  const retry = () => {
    mutation.mutate(
      { reviewKey, command: 'retry' },
      {
        onSettled: () => {
          void queryClient.invalidateQueries({ queryKey: queryKeys.review(reviewKey) })
        },
      },
    )
  }

  return (
    <CCard className="review-card">
      <CCardHeader>
        <h2 className="h6 mb-0">{dead === null ? 'Sin recorrido' : 'Descarte'}</h2>
      </CCardHeader>
      <CCardBody>
        {dead === null ? (
          <p className="text-body-secondary small mb-3">
            El grafo no dejó un hilo para esta review y tampoco está en la lista de
            descartes. Lo que sí escribió queda en el log.
          </p>
        ) : (
          <>
            <p className="mb-2">
              Motivo <strong>{dead.reason || '—'}</strong> · intentos {dead.attempts} ·{' '}
              {formatDateTime(dead.discarded_at)}
            </p>
            {dead.error ? <pre className="review-log mb-3">{dead.error}</pre> : null}
          </>
        )}

        {isOperador ? (
          <div className="d-flex flex-wrap gap-2">
            {dead !== null ? (
              <CButton
                type="button"
                color="secondary"
                variant="outline"
                size="sm"
                disabled={mutation.isPending}
                onClick={retry}
              >
                {mutation.isPending ? '…' : 'Reintentar'}
              </CButton>
            ) : null}
            <ForgetReviewButton reviewKey={reviewKey} />
          </div>
        ) : (
          <p className="small text-body-secondary mb-0">
            Solo el rol operador reintenta o borra.
          </p>
        )}
      </CCardBody>
    </CCard>
  )
}
