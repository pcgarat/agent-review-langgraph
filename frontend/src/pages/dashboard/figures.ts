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
 * Las cuatro cifras de «¿qué pasa ahora?» (029, D1).
 *
 * «Esperando persona» cuenta solo `paused.human`: una pausa `fix` espera a la wave
 * de corrección, no a una decisión humana, y sumarlas enseñaría trabajo que nadie
 * puede desatascar desde el panel.
 */
export function dashboardFigures(state: PanelState): DashboardFigure[] {
  const pendingCommit = state.queue.filter((item) => item.launch && !item.head_sha).length
  const human = state.paused.human
  const pendingDecisions = human.reduce((total, item) => total + item.pending.length, 0)
  const attempts = state.dead.reduce((total, item) => total + item.attempts, 0)

  return [
    {
      key: 'queue',
      label: 'En cola',
      value: state.queue.length,
      detail:
        state.queue.length === 0
          ? 'Nada esperando'
          : pendingCommit > 0
            ? `${formatNumber(pendingCommit)} sin commit todavía`
            : 'Todas con commit',
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
