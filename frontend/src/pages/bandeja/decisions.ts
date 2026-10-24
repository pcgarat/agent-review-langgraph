/** Respuesta de la bandeja: qué se puede marcar y cómo se llama cada cosa. */

import { ApiError } from '@/api/client'

import { explainError } from '@/pages/dashboard/errors'

/** `DecisionOutcome` del servidor, acotado a lo que se puede marcar a mano. */
export type DecisionOutcome = 'approved' | 'rejected' | 'noted' | 'dropped'

export interface OutcomeOption {
  outcome: DecisionOutcome
  label: string
}

/**
 * Resultados válidos de una pausa, en el mismo orden que la tarjeta de Teams y el
 * formulario HTML antiguo. La regla la fija el servidor (`outcomes_for_pause`):
 * en publicación se publica o se deja fuera; en una pausa del motor `llm`,
 * aprobar autoriza el arreglo.
 */
export function outcomesFor(pauseKind: string | null): OutcomeOption[] {
  return pauseKind === 'publication'
    ? [
        { outcome: 'approved', label: 'Publicar' },
        { outcome: 'dropped', label: 'No publicar' },
      ]
    : [
        { outcome: 'approved', label: 'Aprobar' },
        { outcome: 'rejected', label: 'Rechazar' },
        { outcome: 'noted', label: 'Anotar' },
      ]
}

/** `PublishBatch` de `teams_bot/cards.py`: son los únicos lotes que acepta el servidor. */
export type PublishBatch = 'blockers' | 'all' | 'none'

export const PUBLICATION_BATCHES: { batch: PublishBatch; label: string }[] = [
  { batch: 'blockers', label: 'Solo bloqueantes' },
  { batch: 'all', label: 'Publicar todos' },
  { batch: 'none', label: 'No publicar ninguno' },
]

/** Nombre legible de lo que el hallazgo propone hacer. */
const ACTION_LABEL: Record<string, string> = {
  escalate: 'escalar',
  fix_with_approval: 'arreglar con aprobación',
  fix_auto: 'arreglar automáticamente',
  report: 'solo informar',
  publish: 'publicar',
}

export function actionLabel(action: string): string {
  return ACTION_LABEL[action] ?? action
}

/** La ruta del contrato; la `review_key` lleva `/` y `:` y se codifica por segmento. */
export function decisionPath(reviewKey: string): string {
  return `/api/bandeja/${reviewKey.split('/').map(encodeURIComponent).join('/')}/decision`
}

/**
 * Por qué falló una respuesta.
 *
 * Un `409` no es un fallo del cliente: la pausa ya la cerró otra persona o ya no
 * existe. Se distingue del resto para no enseñar «error» donde el sistema hizo lo
 * correcto, y en los dos casos el texto es el del servidor, que es quien sabe
 * quién decidió.
 */
export interface DecisionFailure {
  title: string
  body: string
  tone: 'warning' | 'danger'
}

export function describeDecisionError(error: unknown): DecisionFailure {
  const body = explainError(error, 'No se pudo enviar la decisión.')
  if (error instanceof ApiError) {
    if (error.status === 409) {
      // Ganar la carrera no es un error del cliente: la pausa se cerró por otra
      // vía y el servidor dice quién llegó antes.
      return error.detailText.includes('ya la tomo')
        ? { title: 'Ya lo decidió otra persona', body, tone: 'warning' }
        : { title: 'La pausa ya no está abierta', body, tone: 'warning' }
    }
    if (error.status === 403) return { title: 'Tu rol no puede decidir', body, tone: 'danger' }
    if (error.status === 400) {
      return { title: 'La decisión no encaja con la pausa', body, tone: 'danger' }
    }
  }
  return { title: 'No se pudo responder', body, tone: 'danger' }
}
