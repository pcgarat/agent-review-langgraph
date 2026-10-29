/** Histórico de reviews terminadas (`GET /api/history`, iniciativa 021 R9). */

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
