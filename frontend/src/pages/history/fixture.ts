import type { HistoryItem } from '@/api/types/history'

/** Una fila por cada forma del contrato que la tabla tiene que saber pintar. */
export const HISTORY: HistoryItem[] = [
  {
    review_key: 'cqm/emotivoo-v2:42:abc123:panel',
    repo: 'cqm/emotivoo-v2',
    pr_id: 42,
    head_sha: 'abc123456789',
    finished_at: '2026-10-24T11:00:00+00:00',
    end: 'completa',
    findings: 3,
    by_severity: { P0: 1, P2: 2 },
    files_reviewed: 12,
    files_skipped: 1,
    tokens: 12345,
    reasoning: true,
  },
  {
    review_key: 'cqm/emotivoo-v2:43:def456:ci',
    repo: 'otro/repo',
    pr_id: 43,
    head_sha: 'def456789012',
    finished_at: null,
    end: 'incompleta',
    findings: 0,
    by_severity: {},
    files_reviewed: 0,
    files_skipped: 4,
    tokens: 0,
    reasoning: false,
  },
]

/** Lista larga para que la paginación tenga más de una página (25 por página). */
export function manyReviews(count = 60): HistoryItem[] {
  return Array.from({ length: count }, (_, index) => ({
    ...HISTORY[0]!,
    review_key: `cqm/emotivoo-v2:${100 + index}:sha${index}:panel`,
    pr_id: 100 + index,
    finished_at: `2026-10-24T${String(10 - Math.floor(index / 60)).padStart(2, '0')}:00:00+00:00`,
  }))
}
