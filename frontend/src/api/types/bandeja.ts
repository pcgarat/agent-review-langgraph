/** Bandeja de decisiones (`GET /api/bandeja`, iniciativa 019). */

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
