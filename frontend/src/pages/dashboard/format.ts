/** Formato de cifras y fechas del cuadro de mando, en español. */

const NUMBER = new Intl.NumberFormat('es-ES')

const DATE_TIME = new Intl.DateTimeFormat('es-ES', {
  dateStyle: 'short',
  timeStyle: 'medium',
})

export function formatNumber(value: number): string {
  return NUMBER.format(value)
}

/** Una fecha ISO del contrato, o `—` si no hay. Nunca revienta por una fecha rara. */
export function formatDateTime(value: string | null): string {
  if (value === null || value === '') return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : DATE_TIME.format(date)
}

/**
 * Cuánto lleva un nodo en curso. Un reloj adelantado no pinta un tiempo negativo.
 * `now` es inyectable para que el test no dependa del reloj de la máquina.
 */
export function formatElapsed(startedAt: string | null, now = Date.now()): string {
  if (startedAt === null || startedAt === '') return '—'
  const started = Date.parse(startedAt)
  if (Number.isNaN(started)) return '—'
  const total = Math.max(0, Math.floor((now - started) / 1000))
  const days = Math.floor(total / 86400)
  const hours = Math.floor((total % 86400) / 60)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  if (days > 0) return `${days} d ${hours} h`
  if (hours > 0) return `${hours} h ${minutes} min`
  if (minutes > 0) return `${minutes} min ${seconds} s`
  return `${seconds} s`
}

export function plural(count: number, one: string, many: string): string {
  return `${formatNumber(count)} ${count === 1 ? one : many}`
}
