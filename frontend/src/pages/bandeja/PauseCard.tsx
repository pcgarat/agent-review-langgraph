import { CButton, CCard, CCardBody, CCardFooter, CCardHeader } from '@coreui/react'
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { reviewHref } from '@/api/client'
import type { BandejaItem, BandejaPending } from '@/api/types/bandeja'
import { formatDateTime, plural } from '@/pages/dashboard/format'

import { ageLabel } from './age'
import {
  PUBLICATION_BATCHES,
  describeDecisionError,
  outcomesFor,
  type DecisionOutcome,
} from './decisions'
import { FindingBlock } from './FindingBlock'
import { useDecide, type DecisionItem } from './useBandeja'

/** Qué pregunta la pausa; el motor `claude` pausa para elegir qué se publica. */
function pauseTitle(item: BandejaItem): string {
  return item.pause_kind === 'publication'
    ? `Qué se publica · ${item.repo}#${item.pr_id}`
    : `Decisión pendiente · ${item.repo}#${item.pr_id}`
}

/** Una pausa con sus hallazgos y las dos formas de responder (029, B1). */
export function PauseCard({ item, isOperador }: { item: BandejaItem; isOperador: boolean }) {
  const [marks, setMarks] = useState<Record<string, DecisionOutcome>>({})
  const decide = useDecide(item.review_key)

  const mark = (finding: BandejaPending, outcome: DecisionOutcome) => {
    setMarks((previous) => ({ ...previous, [finding.finding_id]: outcome }))
  }

  // `build_decision` exige una respuesta por cada pendiente: una parcial
  // publicaría el resto como si nadie los hubiera frenado. Por eso el envío por
  // hallazgo espera a que estén todos marcados.
  const { items, complete } = useMemo(() => {
    const decided = item.pending
      .map((finding) => ({ finding_id: finding.finding_id, outcome: marks[finding.finding_id] }))
      .filter((entry): entry is DecisionItem => entry.outcome !== undefined)
    return { items: decided, complete: decided.length === item.pending.length }
  }, [item.pending, marks])

  const failure = decide.isError ? describeDecisionError(decide.error) : null
  const busy = decide.isPending

  return (
    <CCard className="pause-card mb-4">
      <CCardHeader className="d-flex flex-wrap align-items-baseline gap-2">
        <h2 className="h6 mb-0 me-auto">{pauseTitle(item)}</h2>
        <span className="small text-body-secondary">
          {plural(item.pending.length, 'hallazgo', 'hallazgos')}
        </span>
      </CCardHeader>
      <CCardBody>
        <p className="small text-body-secondary mb-3">
          <Link to={reviewHref(item.review_key)} className="text-break">
            {item.review_key}
          </Link>
          <span className="ms-2">·</span>
          <span className="ms-2">{formatDateTime(item.created_at)}</span>
          <span className="ms-2">({ageLabel(item.created_at)})</span>
        </p>

        {item.pending.map((finding) => (
          <FindingBlock
            key={finding.finding_id}
            finding={finding}
            pauseKind={item.pause_kind}
            options={outcomesFor(item.pause_kind)}
            chosen={marks[finding.finding_id]}
            canDecide={isOperador}
            busy={busy}
            onChoose={(outcome) => mark(finding, outcome)}
          />
        ))}

        {failure !== null ? (
          <p className={`decision-error small mb-0 text-${failure.tone}`} role="alert">
            <strong>{failure.title}.</strong> {failure.body}
          </p>
        ) : null}
      </CCardBody>
      {isOperador ? (
        <CCardFooter className="d-flex flex-wrap align-items-center gap-2">
          {item.pause_kind === 'publication' ? (
            <>
              <span className="small text-body-secondary me-1">Lote:</span>
              {PUBLICATION_BATCHES.map((option) => (
                <CButton
                  key={option.batch}
                  type="button"
                  size="sm"
                  color="secondary"
                  variant="outline"
                  disabled={busy}
                  onClick={() => decide.mutate({ batch: option.batch })}
                >
                  {option.label}
                </CButton>
              ))}
            </>
          ) : null}
          <CButton
            type="button"
            size="sm"
            color="primary"
            className="ms-auto"
            disabled={busy || !complete || item.pending.length === 0}
            onClick={() => decide.mutate({ items })}
          >
            {busy ? 'Enviando…' : 'Enviar'}
          </CButton>
        </CCardFooter>
      ) : (
        <CCardFooter>
          <p className="small text-body-secondary mb-0">Solo el rol operador responde.</p>
        </CCardFooter>
      )}
    </CCard>
  )
}
