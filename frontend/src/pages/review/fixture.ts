import type { ReviewDetail } from '@/api/types/review'

/**
 * Ficha de fixture.
 *
 * Los tres sellos están separados por una cantidad distinta (2 s, 1 min 5 s y
 * 4 s hasta `updated_at`) para que una duración que se tome del nodo equivocado
 * no cuadre por casualidad.
 */
const REVIEW_KEY = 'cqm/emotivoo-v2:42:abc123:panel'

export const REVIEW: ReviewDetail = {
  review_key: REVIEW_KEY,
  trace: {
    review_key: REVIEW_KEY,
    thread_id: REVIEW_KEY,
    state: 'paused',
    nodes: [
      {
        name: 'scope',
        index: 0,
        started_at: '2026-10-24T11:00:00+00:00',
        writes: ['diff'],
        summary: null,
        discarded: [],
      },
      {
        name: 'persona',
        index: 1,
        started_at: '2026-10-24T11:00:02+00:00',
        writes: ['findings'],
        summary: null,
        discarded: [],
      },
      {
        name: 'merge',
        index: 2,
        started_at: '2026-10-24T11:01:07+00:00',
        writes: [],
        summary: null,
        discarded: [],
      },
    ],
    next: ['hitl'],
    pause_kind: 'human',
    updated_at: '2026-10-24T11:01:11+00:00',
  },
  paused: {
    review_key: REVIEW_KEY,
    kind: 'human',
    created_at: '2026-10-24T11:01:11+00:00',
    state: 'PAUSED',
    pending: [
      {
        finding_id: 'f1',
        title: 'Falta un null check',
        severity: 'P0',
        file: 'src/a.py',
        line: 10,
        why_it_matters: 'Puede reventar en producción.',
        proposed_action: 'publish',
        reason: 'bug',
        code_url: null,
      },
      {
        finding_id: 'f2',
        title: 'Nombre poco claro',
        severity: 'P3',
        file: 'src/b.py',
        line: 20,
        why_it_matters: 'Confunde al leerlo.',
        proposed_action: 'publish',
        reason: 'style',
        code_url: null,
      },
    ],
    command_hint: 'agent-review decide …',
    jobs: [],
    publication: true,
  },
  notes: ['Fuente estática phpstan: missing.', 'Presupuesto agotado en: persona.'],
  journal: ['worker: review recibida', 'worker: persona lanzada'],
  console_available: false,
  reasoning_available: false,
  claude: null,
  running: [],
}

/** Descarte sin recorrido (F8): ni traza ni pausa, pero con log. */
export const DISCARD: ReviewDetail = {
  review_key: REVIEW_KEY,
  trace: null,
  paused: null,
  notes: [],
  journal: ['worker: intento 2 falló con HTTP 502'],
  console_available: false,
  reasoning_available: false,
  claude: null,
  running: [],
}

/** Ficha del motor `claude` con los dos canales disponibles (F2/F7). */
export const CLAUDE_REVIEW: ReviewDetail = {
  ...REVIEW,
  claude: { available: true, events_available: true, download_available: true },
}

/** Ficha de `claude` antes de que el CLI escriba: nada que seguir ni descargar. */
export const CLAUDE_QUIET: ReviewDetail = {
  ...REVIEW,
  paused: null,
  trace: { ...REVIEW.trace!, next: [], pause_kind: null, state: 'in_progress' },
  claude: { available: true, events_available: false, download_available: false },
}
