import { CBadge, CButton } from '@coreui/react'

import type { BandejaPending } from '@/api/types/bandeja'

import { actionLabel, type DecisionOutcome, type OutcomeOption } from './decisions'

const SEVERITY_COLOR: Record<string, string> = {
  P0: 'danger',
  P1: 'warning',
  P2: 'info',
  P3: 'secondary',
}

/** La severidad es un dato del contrato (`P0`…`P3`), no una etiqueta inventada. */
export function SeverityBadge({ severity }: { severity: string }) {
  return (
    <CBadge color={SEVERITY_COLOR[severity] ?? 'secondary'} className="me-2">
      {severity}
    </CBadge>
  )
}

interface FindingBlockProps {
  finding: BandejaPending
  pauseKind: string | null
  options: OutcomeOption[]
  chosen: DecisionOutcome | undefined
  /** El rol decide si el control existe; `busy` solo lo apaga mientras viaja. */
  canDecide: boolean
  busy: boolean
  onChoose: (outcome: DecisionOutcome) => void
}

/**
 * Un hallazgo pendiente: qué se encontró, por qué importa y qué se puede marcar.
 *
 * El texto es dato de terceros (lo escribió un modelo sobre un diff ajeno): se
 * pinta como nodos de React, nunca como HTML (029, P13).
 */
export function FindingBlock({
  finding,
  pauseKind,
  options,
  chosen,
  canDecide,
  busy,
  onChoose,
}: FindingBlockProps) {
  return (
    <div className="finding mb-3">
      <p className="mb-1">
        <SeverityBadge severity={finding.severity} />
        <strong>{finding.title}</strong>
      </p>
      <p className="small text-body-secondary mb-1">
        <code>
          {finding.file}:{finding.line}
        </code>
        {finding.code_url ? (
          <a
            href={finding.code_url}
            className="ms-2"
            target="_blank"
            rel="noreferrer noopener"
          >
            ver el código
          </a>
        ) : null}
        {finding.reason ? <span className="ms-2">· {finding.reason}</span> : null}
      </p>
      {finding.why_it_matters ? <p className="small mb-1">{finding.why_it_matters}</p> : null}
      {pauseKind !== 'publication' && finding.proposed_action ? (
        <p className="small text-body-secondary mb-1">
          Acción propuesta: {actionLabel(finding.proposed_action)}
        </p>
      ) : null}
      {canDecide ? (
        <div className="d-flex flex-wrap gap-2 mt-2">
          {options.map((option) => (
            <CButton
              key={option.outcome}
              type="button"
              size="sm"
              color={chosen === option.outcome ? 'primary' : 'secondary'}
              variant={chosen === option.outcome ? undefined : 'outline'}
              aria-pressed={chosen === option.outcome}
              disabled={busy}
              onClick={() => onChoose(option.outcome)}
            >
              {option.label}
            </CButton>
          ))}
        </div>
      ) : null}
    </div>
  )
}
