/** Ficha de una review: traza y recurso agregado de la spec 029, P8. */

import type { ActiveNode, PausedItem } from './state'

export interface TraceNode {
  name: string
  index: number
  started_at: string | null
  writes: string[]
}

export interface ReviewTrace {
  review_key: string
  thread_id: string
  state: 'completed' | 'paused' | 'in_progress'
  nodes: TraceNode[]
  next: string[]
  pause_kind: 'human' | 'fix' | null
  updated_at: string | null
}

/** `GET /api/reviews/{review_key}` (spec 029, P8). */
export interface ReviewDetail {
  review_key: string
  trace: ReviewTrace | null
  paused: PausedItem | null
  notes: string[]
  journal: string[]
  console_available: boolean
  reasoning_available: boolean
  claude: ClaudeAvailability | null
  /** Nodos que el worker está ejecutando ahora. Vacío si la review no corre. */
  running: ActiveNode[]
}

/** Lo que el CLI de Claude tiene de una review (`REVIEW_ENGINE=claude`). */
export interface ClaudeAvailability {
  available: boolean
  events_available: boolean
  download_available: boolean
}
