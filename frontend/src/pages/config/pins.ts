import type { DialPin } from '@/api/types/config'

export interface PinGroup {
  revision: number
  reviewKeys: string[]
}

/**
 * Pines agrupados por revisión, de la más alta a la más baja.
 *
 * Agrupar importa: en un día normal hay varias reviews en curso sobre la misma
 * revisión y repetir «fijó la revisión 7» seis veces convierte el aviso en ruido.
 */
export function groupPins(pins: readonly DialPin[]): PinGroup[] {
  const byRevision = new Map<number, string[]>()
  for (const pin of pins) {
    const keys = byRevision.get(pin.revision)
    if (keys === undefined) byRevision.set(pin.revision, [pin.review_key])
    else keys.push(pin.review_key)
  }
  return [...byRevision.entries()]
    .sort(([left], [right]) => right - left)
    .map(([revision, reviewKeys]) => ({ revision, reviewKeys }))
}
