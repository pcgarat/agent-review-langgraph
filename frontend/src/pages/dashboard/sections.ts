import type { DashboardFigure } from './figures'

/** Ancla de la sección que detalla cada cifra; el menú de la cifra salta a ella. */
export const SECTION_ID: Record<DashboardFigure['key'], string> = {
  queue: 'seccion-cola',
  active: 'seccion-en-vuelo',
  human: 'seccion-pausas-humanas',
  dead: 'seccion-descartes',
}
