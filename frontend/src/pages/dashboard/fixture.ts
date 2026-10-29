import type { PanelState } from '@/api/types/state'

/**
 * Estado de fixture del cuadro de mando.
 *
 * Cada colección tiene un tamaño distinto para que una cifra que se equivoque de
 * campo (`paused.human` por `paused.fix`, por ejemplo) no cuadre por casualidad.
 */
export const PANEL_STATE: PanelState = {
  generated_at: '2026-10-24T11:00:00+00:00',
  queue: [
    {
      review_key: 'cqm/emotivoo-v2:41:aaa111:ci',
      repo: 'cqm/emotivoo-v2',
      pr_id: 41,
      head_sha: 'aaa111aaa111',
      pipeline_id: 'ci',
      attempts: 0,
      launch: false,
    },
    {
      review_key: 'cqm/emotivoo-v2:42:bbb222:panel',
      repo: 'cqm/emotivoo-v2',
      pr_id: 42,
      // Un lanzamiento del panel no tiene SHA todavía: lo resuelve el worker.
      head_sha: '',
      pipeline_id: 'panel',
      attempts: 1,
      launch: true,
    },
  ],
  active: [
    {
      review_key: 'cqm/emotivoo-v2:43:ccc333:panel',
      nodes: [{ node: 'review', started_at: '2026-10-24T10:58:00+00:00', branches: 3 }],
    },
  ],
  paused: {
    human: [
      {
        review_key: 'cqm/emotivoo-v2:44:ddd444:panel',
        kind: 'human',
        created_at: '2026-10-24T10:30:00+00:00',
        state: 'PAUSED',
        pending: [
          {
            finding_id: 'f1',
            title: 'Falta un null check',
            severity: 'high',
            file: 'src/a.py',
            line: 10,
            why_it_matters: 'Puede reventar en producción.',
            proposed_action: 'apply',
            reason: 'bug',
          },
          {
            finding_id: 'f2',
            title: 'Nombre poco claro',
            severity: 'low',
            file: 'src/b.py',
            line: 20,
            why_it_matters: 'Confunde al leerlo.',
            proposed_action: 'apply',
            reason: 'style',
          },
        ],
        command_hint: 'agent-review decide …',
        jobs: [],
        publication: false,
      },
    ],
    fix: [
      {
        review_key: 'cqm/emotivoo-v2:45:eee555:panel',
        kind: 'fix',
        created_at: '2026-10-24T10:40:00+00:00',
        state: 'VERIFYING',
        pending: [],
        command_hint: '',
        jobs: [{ run_key: 'run-1' }],
        publication: false,
      },
    ],
  },
  parked: [
    {
      review_key: 'pcgarat/agent-review-langgraph:19:abc123:panel',
      repo: 'pcgarat/agent-review-langgraph',
      pr_id: 19,
      pipeline_id: 'panel',
      // Espera un token: es el caso que dejaba un lanzamiento invisible (030).
      reason: 'session',
      launch: true,
    },
    {
      review_key: 'pcgarat/agent-review-langgraph:20:def456:ci',
      repo: 'pcgarat/agent-review-langgraph',
      pr_id: 20,
      pipeline_id: 'ci',
      // Agotó el cupo: no vuelve solo, y eso lo cambia todo para el operador.
      reason: 'quota',
      launch: false,
    },
  ],
  dead: [
    {
      review_key: 'cqm/emotivoo-v2:46:fff666:ci',
      attempts: 1,
      reason: 'proveedor caído',
      discarded_at: '2026-10-24T09:00:00+00:00',
      error: 'HTTP 502',
    },
    {
      review_key: 'cqm/emotivoo-v2:47:ggg777:ci',
      attempts: 2,
      reason: 'presupuesto agotado',
      discarded_at: '2026-10-24T09:10:00+00:00',
      error: '',
    },
    {
      review_key: 'cqm/emotivoo-v2:48:hhh888:ci',
      attempts: 2,
      reason: 'timeout',
      discarded_at: null,
      error: '',
    },
  ],
  budget: [
    {
      review_key: 'cqm/emotivoo-v2:43:ccc333:panel',
      committed: 15000,
      reserved: 5000,
      total: 100000,
      remaining: 80000,
    },
  ],
}
