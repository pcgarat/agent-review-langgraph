/** Antigüedad de una pausa, en lenguaje natural y en español. */

const RELATIVE = new Intl.RelativeTimeFormat('es', { numeric: 'auto' })

/**
 * Cuánto lleva esperando algo desde `createdAt`.
 *
 * La tarjeta enseña la fecha exacta al lado; esto es la lectura rápida («hace 2
 * horas») que dice si una pausa lleva demasiado tiempo sin dueño. Devuelve vacío
 * ante una fecha que no se puede leer, en vez de inventar una cifra.
 */
export function ageLabel(createdAt: string, now: number = Date.now()): string {
  const then = Date.parse(createdAt)
  if (Number.isNaN(then)) return ''
  const seconds = Math.round((then - now) / 1000)
  if (Math.abs(seconds) < 60) return RELATIVE.format(seconds, 'second')
  const minutes = Math.round(seconds / 60)
  if (Math.abs(minutes) < 60) return RELATIVE.format(minutes, 'minute')
  const hours = Math.round(minutes / 60)
  if (Math.abs(hours) < 24) return RELATIVE.format(hours, 'hour')
  const days = Math.round(hours / 24)
  if (Math.abs(days) < 30) return RELATIVE.format(days, 'day')
  const months = Math.round(days / 30)
  if (Math.abs(months) < 12) return RELATIVE.format(months, 'month')
  return RELATIVE.format(Math.round(months / 12), 'year')
}
