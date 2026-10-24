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

export function plural(count: number, one: string, many: string): string {
  return `${formatNumber(count)} ${count === 1 ? one : many}`
}
