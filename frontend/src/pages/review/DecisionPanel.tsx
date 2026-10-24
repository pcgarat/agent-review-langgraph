import { CButton, CCard, CCardBody, CCardHeader, CTable, CTableBody, CTableDataCell, CTableHead, CTableHeaderCell, CTableRow } from '@coreui/react'
import { useState } from 'react'

import type { PausedItem } from '@/api/types/state'
import { actionLabel, outcomesFor, type DecisionOutcome } from '@/pages/bandeja/decisions'
import { SeverityBadge } from '@/pages/bandeja/FindingBlock'

import { useDecideReview, type DecisionItem } from './useDecideReview'

interface DecisionPanelProps {
  reviewKey: string
  paused: PausedItem
  canDecide: boolean
}

/**
 * Formulario de decisión de una pausa humana (029, F4; 022 H4).
 *
 * Un control por pendiente y una nota por hallazgo, como el formulario HTML
 * antiguo. El resultado preseleccionado es el primero válido, así que el envío
 * cubre todos los pendientes sin obligar a tocar nada: `build_decision` rechaza
 * una decisión parcial con `409` y la completitud no se reimplementa aquí.
 *
 * El formulario no se pinta para un lector: no hay nada que pueda enviar y cinco
 * selects deshabilitados se leen como un fallo de la pantalla.
 */
export function DecisionPanel({ reviewKey, paused, canDecide }: DecisionPanelProps) {
  const decide = useDecideReview(reviewKey)
  const options = outcomesFor(paused.publication ? 'publication' : null)
  const fallback = options[0]?.outcome ?? 'approved'
  const [marks, setMarks] = useState<Record<string, DecisionOutcome>>(() =>
    Object.fromEntries(paused.pending.map((finding) => [finding.finding_id, fallback])),
  )
  const [notes, setNotes] = useState<Record<string, string>>({})

  const items: DecisionItem[] = paused.pending.map((finding) => ({
    finding_id: finding.finding_id,
    outcome: marks[finding.finding_id] ?? fallback,
    note: notes[finding.finding_id] ?? '',
  }))

  return (
    <CCard className="review-card">
      <CCardHeader className="d-flex flex-wrap align-items-baseline gap-2">
        <h2 className="h6 mb-0 me-auto">Decisión</h2>
        <span className="small text-body-secondary">
          {paused.publication ? 'qué se publica' : 'espera una persona'}
        </span>
      </CCardHeader>
      <CCardBody>
        {!canDecide ? (
          <p className="small text-body-secondary mb-0">Solo el rol operador decide.</p>
        ) : (
          <>
            <p className="small text-body-secondary">
              {paused.publication
                ? 'Publicar escribe el hallazgo en el pull request. No publicar lo deja fuera.'
                : 'Aprobar solo autoriza a intentar el arreglo: los temas protegidos y las rutas nunca-auto siguen mandando (ADR-0009).'}
            </p>

            {paused.pending.length === 0 ? (
              <p className="text-body-secondary small mb-0">
                Esta pausa no tiene hallazgos pendientes.
              </p>
            ) : (
              <CTable small responsive align="middle" className="mb-3">
                <CTableHead>
                  <CTableRow>
                    <CTableHeaderCell scope="col">Hallazgo</CTableHeaderCell>
                    <CTableHeaderCell scope="col">Resultado</CTableHeaderCell>
                    <CTableHeaderCell scope="col">Nota</CTableHeaderCell>
                  </CTableRow>
                </CTableHead>
                <CTableBody>
                  {paused.pending.map((finding) => (
                    <CTableRow key={finding.finding_id}>
                      <CTableDataCell>
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
                        </p>
                        <p className="small text-body-secondary mb-1">
                          {actionLabel(finding.proposed_action)}
                        </p>
                        {finding.why_it_matters ? (
                          <p className="small mb-0">{finding.why_it_matters}</p>
                        ) : null}
                      </CTableDataCell>
                      <CTableDataCell>
                        <select
                          className="form-select form-select-sm"
                          aria-label={`Resultado de ${finding.title}`}
                          value={marks[finding.finding_id] ?? fallback}
                          onChange={(event) =>
                            setMarks((previous) => ({
                              ...previous,
                              [finding.finding_id]: event.target.value as DecisionOutcome,
                            }))
                          }
                        >
                          {options.map((option) => (
                            <option key={option.outcome} value={option.outcome}>
                              {option.label}
                            </option>
                          ))}
                        </select>
                      </CTableDataCell>
                      <CTableDataCell>
                        <input
                          type="text"
                          className="form-control form-control-sm"
                          placeholder="nota opcional"
                          aria-label={`Nota de ${finding.title}`}
                          value={notes[finding.finding_id] ?? ''}
                          onChange={(event) =>
                            setNotes((previous) => ({
                              ...previous,
                              [finding.finding_id]: event.target.value,
                            }))
                          }
                        />
                      </CTableDataCell>
                    </CTableRow>
                  ))}
                </CTableBody>
              </CTable>
            )}

            <CButton
              type="button"
              color="primary"
              disabled={decide.isPending || items.length === 0}
              onClick={() => decide.mutate(items)}
            >
              {decide.isPending ? 'Enviando…' : 'Decidir'}
            </CButton>
          </>
        )}
      </CCardBody>
    </CCard>
  )
}
