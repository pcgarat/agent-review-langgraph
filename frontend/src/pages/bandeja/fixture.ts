import type { BandejaView } from '@/api/types/bandeja'

/**
 * Bandeja de fixture.
 *
 * Dos pausas con `pause_kind` distinto: una de publicación (que admite lote) y una
 * del motor `llm` (que no). Si una tarjeta leyese el `pause_kind` de la otra, los
 * botones de lote aparecerían donde el servidor los rechaza.
 */
export const BANDEJA: BandejaView = {
  teams_enabled: true,
  items: [
    {
      review_key: 'cqm/emotivoo-v2:42:abc123:panel',
      pause_id: 'pause-1',
      repo: 'cqm/emotivoo-v2',
      pr_id: 42,
      pause_kind: 'publication',
      created_at: '2026-10-24T11:00:00+00:00',
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
          code_url: 'https://example.test/blob/abc123/src/a.py#L10',
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
    },
    {
      review_key: 'cqm/emotivoo-v2:44:ddd444:panel',
      pause_id: 'pause-2',
      repo: 'cqm/emotivoo-v2',
      pr_id: 44,
      pause_kind: null,
      created_at: '2026-10-24T10:30:00+00:00',
      pending: [
        {
          finding_id: 'f3',
          title: 'Falta un test',
          severity: 'P1',
          file: 'tests/test_a.py',
          line: 1,
          why_it_matters: 'El cambio no está cubierto.',
          proposed_action: 'fix_with_approval',
          reason: 'test',
        },
      ],
    },
  ],
}
