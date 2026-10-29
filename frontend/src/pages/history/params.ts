import { HISTORY_LIMIT, type HistoryFilters } from './useHistory'

/**
 * Filas por página.
 *
 * 25 y no 50: la tabla es densa y la cabecera con el contador tiene que seguir
 * visible mientras se compara una fila con la de arriba; a partir de 30 filas el
 * operador ya hace scroll para ver el total de lo que está mirando.
 */
export const PAGE_SIZE = 25

/** Lo que dice la URL: la pantalla no guarda filtros en estado propio. */
export interface HistoryParams {
  repo: string
  prId: number | undefined
  page: number
}

function positive(value: string | null): number | undefined {
  if (value === null || value.trim() === '') return undefined
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed > 0 ? parsed : undefined
}

export function readParams(search: URLSearchParams): HistoryParams {
  const page = positive(search.get('pagina'))
  return {
    repo: (search.get('repo') ?? '').trim(),
    prId: positive(search.get('pr_id')),
    page: page === undefined ? 1 : page,
  }
}

export function readFilters({ repo, prId }: HistoryParams): HistoryFilters {
  return { repo: repo === '' ? undefined : repo, pr_id: prId }
}

/** Escribe los filtros y la página; borra lo que no aplica para que el enlace sea limpio. */
export function writeParams({ repo, prId, page }: HistoryParams): URLSearchParams {
  const next = new URLSearchParams()
  if (repo !== '') next.set('repo', repo)
  if (prId !== undefined) next.set('pr_id', String(prId))
  if (page > 1) next.set('pagina', String(page))
  return next
}

export interface Page<T> {
  rows: T[]
  page: number
  pages: number
  total: number
  from: number
  to: number
}

/** Trozo de la lista que toca pintar, con la página acotada a lo que hay. */
export function paginate<T>(items: T[], requested: number): Page<T> {
  const total = items.length
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const page = Math.min(Math.max(1, requested), pages)
  const start = (page - 1) * PAGE_SIZE
  return {
    rows: items.slice(start, start + PAGE_SIZE),
    page,
    pages,
    total,
    from: total === 0 ? 0 : start + 1,
    to: Math.min(start + PAGE_SIZE, total),
  }
}

/** `true` si el servidor devolvió justo el tope: puede haber más y no se ven. */
export function truncated(total: number): boolean {
  return total >= HISTORY_LIMIT
}
