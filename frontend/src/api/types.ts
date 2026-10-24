/** Respuesta de `GET /api/state` (`PanelState` en `read_model.py`). */
export interface QueueItem {
  review_key: string
  repo: string
  pr_id: number
  head_sha: string
  pipeline_id: string
  attempts: number
  launch: boolean
}

export interface ActiveNode {
  node: string
  started_at: string | null
  branches: number
}

export interface ActiveReview {
  review_key: string
  nodes: ActiveNode[]
}

export interface PendingDecision {
  finding_id: string
  title: string
  severity: string
  file: string
  line: number
  why_it_matters: string
  proposed_action: string
  reason: string
  code_url?: string | null
}

export interface PausedItem {
  review_key: string
  kind: 'human' | 'fix'
  created_at: string
  state: string
  pending: PendingDecision[]
  command_hint: string
  jobs: Record<string, unknown>[]
  publication: boolean
}

export interface DeadLetterItem {
  review_key: string
  attempts: number
  reason: string
  discarded_at: string | null
  error: string
}

export interface BudgetItem {
  review_key: string
  committed: number
  reserved: number
  total: number
  remaining: number
}

export interface PanelState {
  generated_at: string
  queue: QueueItem[]
  active: ActiveReview[]
  paused: { human: PausedItem[]; fix: PausedItem[] }
  dead: DeadLetterItem[]
  budget: BudgetItem[]
}

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
  claude_available: boolean
}

export interface HistoryItem {
  review_key: string
  repo: string
  pr_id: number
  head_sha: string
  finished_at: string | null
  end: string
  findings: number
  by_severity: Record<string, number>
  files_reviewed: number
  files_skipped: number
  tokens: number
  reasoning: boolean
}

export interface SessionUser {
  user: string
  role: 'lector' | 'operador'
}

/** `GET /api/config` (027). Solo las claves que se apartan del inicial. */
export interface ConfigDocument {
  revision: number
  values: Record<string, unknown>
  initial: Record<string, unknown>
  locked: string[]
  actor: string
  updated_at: string | null
  pins?: { review_key: string; revision: number }[]
}

export interface BandejaPending {
  finding_id: string
  title: string
  severity: string
  file: string
  line: number
  why_it_matters: string
  proposed_action: string
  reason: string
  code_url?: string | null
}

export interface BandejaItem {
  review_key: string
  pause_id: string
  repo: string
  pr_id: number
  pause_kind: string | null
  created_at: string
  pending: BandejaPending[]
}

export interface BandejaView {
  teams_enabled: boolean
  items: BandejaItem[]
}
