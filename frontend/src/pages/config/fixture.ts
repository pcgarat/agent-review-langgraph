import type { ConfigDocument } from '@/api/types/config'

/**
 * Documento de fixture: una revisión publicada con dos overrides y un dial
 * bloqueado por `PANEL_CONFIG_LOCK`.
 *
 * Los tipos están mezclados a propósito (float, entero, `Literal`, booleano,
 * texto) para que un control que se equivoque de campo no cuadre por casualidad.
 */
export const CONFIG_DOCUMENT: ConfigDocument = {
  revision: 3,
  values: {
    llm_max_findings: 4,
    review_tools_grep_mode: 'regex',
    // Un override sobre un dial bloqueado: el servidor lo descarta al montar la
    // review, así que la pantalla tiene que enseñar el inicial y no este 5.
    llm_batch_retries: 5,
  },
  initial: {
    claude_timeout_s: 3600,
    claude_max_turns: 200,
    max_autonomy_override: '',
    scope_ignore: '',
    llm_max_findings: 10,
    llm_budget_tokens_per_pr: 2000000,
    llm_batch_max_chars: 20000,
    llm_batch_retries: 2,
    llm_context_max_chars: 40000,
    verifier_max_findings: 5,
    verifier_max_tool_calls: 8,
    verifier_max_context_bytes: 60000,
    verifier_require_verified_for: 'P0,P1',
    review_tools_roles: '',
    review_tools_max_turns: 4,
    review_tools_max_calls: 8,
    review_tools_max_bytes: 60000,
    review_tools_grep_mode: 'literal',
    review_tools_read_outline: false,
    review_tools_read_state: false,
    review_tools_truncation_notice: true,
    review_tools_strict_calls: false,
    review_tools_calls_per_turn: 3,
    review_tools_precache_context: true,
    review_tools_io_cache: false,
  },
  locked: ['llm_batch_retries'],
  actor: 'ana',
  updated_at: '2026-10-24T10:20:00+00:00',
  pins: [
    { review_key: 'cqm/emotivoo-v2:41:aaa111:ci', revision: 2 },
    { review_key: 'cqm/emotivoo-v2:42:bbb222:panel', revision: 2 },
    { review_key: 'cqm/emotivoo-v2:43:ccc333:panel', revision: 3 },
  ],
}
