/** Recorrido de nodos: derivación de las duraciones (029, bloque F). */

import type { ReviewTrace, TraceNode } from '@/api/types/review'

/** Qué marca cierra el tramo de un nodo en la línea de tiempo. */
export type SpanSource = 'previous' | 'start-unknown' | 'unknown'

export interface TimelineRow {
  node: TraceNode
  /** Milisegundos desde el cierre del nodo anterior (o el primer sello) al suyo. */
  durationMs: number | null
  source: SpanSource
}

export interface Timeline {
  rows: TimelineRow[]
  /** Del primer sello a la última actualización del hilo. */
  totalMs: number | null
}

const DURATION = new Intl.NumberFormat('es-ES', { maximumFractionDigits: 1 })

function timestamp(value: string | null): number | null {
  if (value === null || value === '') return null
  const parsed = Date.parse(value)
  return Number.isNaN(parsed) ? null : parsed
}

/**
 * Duración de cada nodo a partir de los sellos del contrato.
 *
 * El contrato no trae duración y el nombre `started_at` engaña: `domain/trace.py`
 * lo documenta y `trace._node_ts` lo implementa como el sello del checkpoint que
 * **cerró** el nodo. Por eso cada nodo dura desde el sello del anterior (el que
 * cerró el nodo previo, que es el instante en que este empezó) hasta el suyo; el
 * primero arranca en su propio sello porque no hay marca de entrada, y el último
 * cierra en `updated_at`. Un sello ausente o que retrocede deja la celda en «—»
 * en vez de inventar una cifra.
 */
export function deriveTimeline(trace: ReviewTrace): Timeline {
  const rows = trace.nodes.map((node, index) => {
    // El primero no tiene marca de entrada: su inicio es anterior al primer
    // checkpoint que guardó el hilo, así que su duración se desconoce.
    if (index === 0) {
      return { node, durationMs: null, source: 'start-unknown' as const }
    }
    const previous = trace.nodes[index - 1]
    const start = timestamp(previous?.started_at ?? null)
    const end = timestamp(node.started_at)
    if (start === null || end === null || end < start) {
      return { node, durationMs: null, source: 'unknown' as const }
    }
    return { node, durationMs: end - start, source: 'previous' as const }
  })
  const first = timestamp(trace.nodes[0]?.started_at ?? null)
  const last = timestamp(trace.updated_at)
  return {
    rows,
    totalMs: first !== null && last !== null && last >= first ? last - first : null,
  }
}

export function formatDuration(ms: number | null): string {
  if (ms === null || Number.isNaN(ms) || ms < 0) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  const seconds = ms / 1000
  if (seconds < 60) return `${DURATION.format(seconds)} s`
  let minutes = Math.floor(seconds / 60)
  let rest = Math.round(seconds % 60)
  if (rest === 60) {
    minutes += 1
    rest = 0
  }
  if (minutes < 60) return rest === 0 ? `${minutes} min` : `${minutes} min ${rest} s`
  const hours = Math.floor(minutes / 60)
  const restMinutes = minutes % 60
  return restMinutes === 0 ? `${hours} h` : `${hours} h ${restMinutes} min`
}

/** Texto del `title` de la duración: qué se pudo medir y qué no. */
export function spanTitle(source: SpanSource): string {
  if (source === 'previous') return 'Desde el cierre del nodo anterior hasta el de este'
  if (source === 'start-unknown') {
    return 'El primer nodo empezó antes del primer checkpoint: su duración se desconoce'
  }
  return 'Sin marcas de tiempo suficientes'
}
