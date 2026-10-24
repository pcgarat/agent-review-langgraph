/** Estado operativo: lo que devuelve `GET /api/state` (`PanelState` en `read_model.py`). */

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
  /** `human` espera una persona; `fix` espera la wave de corrección. */
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
