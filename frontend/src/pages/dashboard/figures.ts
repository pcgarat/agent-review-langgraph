import type { BudgetItem, PanelState } from '@/api/types/state'

import { formatNumber, plural } from './format'

export interface DashboardFigure {
  key: 'queue' | 'active' | 'human' | 'dead'
  label: string
  value: number
  detail: string
}

/** Gasto de una review: lo consumido más lo reservado sin confirmar. */
export function spentTokens(item: BudgetItem): number {
  return item.committed + item.reserved
}

/** Presupuesto por `review_key`: la tabla de en vuelo lo cruza con el gasto. */
export function budgetByReview(items: BudgetItem[]): Map<string, BudgetItem> {
  return new Map(items.map((item) => [item.review_key, item]))
}

function activeSpend(state: PanelState): string {
  const keys = new Set(state.active.map((review) => review.review_key))
  const budgets = state.budget.filter((item) => keys.has(item.review_key))
  if (budgets.length === 0) return 'Sin gasto registrado'
  const spent = budgets.reduce((total, item) => total + spentTokens(item), 0)
  const cap = budgets.reduce((max, item) => Math.max(max, item.total), 0)
  return cap > 0
    ? `Gasto ${formatNumber(spent)} de ${formatNumber(cap)} tokens`
    : `Gasto ${formatNumber(spent)} tokens`
}

/**
 * Las cuatro cifras de «¿qué pasa ahora?» (029 D1, ampliado en 030).
 *
 * «Esperando persona» cuenta solo `paused.human`: una pausa `fix` espera a la wave
 * de corrección, no a una decisión humana, y sumarlas enseñaría trabajo que nadie
 * puede desatascar desde el panel.
 *
 * «En cola» suma los aparcados: están esperando, no corriendo ni descartados. Si
 * no contaran, la primera pantalla diría «nada esperando» con un job parado
 * esperando un token (030, K4).
 */
/**
 * Detalle de la cifra de cola: distingue lo que va a arrancar de lo que está
 * apartado. Un «2 en cola» sin más esconde que ninguna de las dos va a moverse
 * sin que alguien escriba la sesión.
 */
function queueDetail(queued: number, running: number, parked: number, pendingCommit: number): string {
  if (queued === 0) return 'Nada esperando'
  const parts: string[] = []
  if (parked > 0) {
    parts.push(`${formatNumber(parked)} ${parked === 1 ? 'aparcada' : 'aparcadas'}`)
  }
  if (running > 0) {
    parts.push(
      pendingCommit > 0
        ? `${formatNumber(pendingCommit)} sin commit todavía`
        : `${formatNumber(running)} lista${running === 1 ? '' : 's'} para arrancar`,
    )
  }
  return parts.join(' · ')
}

export function dashboardFigures(state: PanelState): DashboardFigure[] {
  const parked = state.parked ?? []
  const queued = state.queue.length + parked.length
  const pendingCommit = state.queue.filter((item) => item.launch && !item.head_sha).length
  const human = state.paused.human
  const pendingDecisions = human.reduce((total, item) => total + item.pending.length, 0)
  const attempts = state.dead.reduce((total, item) => total + item.attempts, 0)

  return [
    {
      key: 'queue',
      label: 'En cola',
      value: queued,
      detail: queueDetail(queued, state.queue.length, parked.length, pendingCommit),
    },
    {
      key: 'active',
      label: 'En vuelo',
      value: state.active.length,
      detail: activeSpend(state),
    },
    {
      key: 'human',
      label: 'Esperando persona',
      value: human.length,
      detail:
        pendingDecisions === 0
          ? 'Ninguna decisión pendiente'
          : plural(pendingDecisions, 'hallazgo por decidir', 'hallazgos por decidir'),
    },
    {
      key: 'dead',
      label: 'Descartes',
      value: state.dead.length,
      detail:
        attempts === 0
          ? 'Ningún intento registrado'
          : plural(attempts, 'intento acumulado', 'intentos acumulados'),
    },
  ]
}
