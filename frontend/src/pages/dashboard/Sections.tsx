import {
  CBadge,
  CProgress,
  CTable,
  CTableBody,
  CTableDataCell,
  CTableHead,
  CTableHeaderCell,
  CTableRow,
} from '@coreui/react'
import { Link } from 'react-router-dom'

import { reviewHref } from '@/api/client'
import type {
  ActiveReview,
  BudgetItem,
  DeadLetterItem,
  PausedItem,
  QueueItem,
} from '@/api/types/state'

import { CancelButton, ForgetButton, RetryButton } from './CommandButtons'
import { EmptyRow } from './EmptyRow'
import { budgetByReview, spentTokens } from './figures'
import { formatDateTime, formatNumber, plural } from './format'
import { PanelSection } from './PanelSection'

/** Nombre legible del nodo en curso; el resto se pinta tal cual lo da el worker. */
const NODE_LABEL: Record<string, string> = { claude: 'CLI de Claude' }

interface SectionProps {
  isOperador: boolean
}

function ReviewCell({ reviewKey }: { reviewKey: string }) {
  return (
    <Link to={reviewHref(reviewKey)} className="text-break">
      {reviewKey}
    </Link>
  )
}

/** Cola de `reviews:jobs`, por antigüedad: lo que va a arrancar sin que nadie haga nada. */
export function QueueSection({ items, isOperador }: SectionProps & { items: QueueItem[] }) {
  return (
    <PanelSection
      title="Cola"
      hint={items.length === 0 ? null : plural(items.length, 'review esperando', 'reviews esperando')}
    >
      <CTable small responsive hover align="middle" className="dashboard-table mb-0">
        <CTableHead>
          <CTableRow>
            <CTableHeaderCell scope="col">Review</CTableHeaderCell>
            <CTableHeaderCell scope="col">PR</CTableHeaderCell>
            <CTableHeaderCell scope="col">Commit</CTableHeaderCell>
            <CTableHeaderCell scope="col">Pipeline</CTableHeaderCell>
            <CTableHeaderCell scope="col" className="text-end">Intentos</CTableHeaderCell>
            <CTableHeaderCell scope="col" className="text-end">
              <span className="visually-hidden">Comandos</span>
            </CTableHeaderCell>
          </CTableRow>
        </CTableHead>
        <CTableBody>
          {items.length === 0 ? (
            <EmptyRow colSpan={6}>No hay reviews en cola.</EmptyRow>
          ) : (
            items.map((item) => (
              <CTableRow key={`${item.review_key}:${item.pipeline_id}`}>
                <CTableDataCell>
                  <ReviewCell reviewKey={item.review_key} />
                </CTableDataCell>
                <CTableDataCell className="text-nowrap">
                  {item.repo}
                  {item.repo === '' ? '' : '#'}
                  {item.pr_id || ''}
                </CTableDataCell>
                <CTableDataCell>
                  {item.head_sha ? (
                    <code>{item.head_sha.slice(0, 12)}</code>
                  ) : (
                    <span className="text-body-secondary">pendiente</span>
                  )}
                </CTableDataCell>
                <CTableDataCell>{item.pipeline_id || '—'}</CTableDataCell>
                <CTableDataCell className="num text-end">{formatNumber(item.attempts)}</CTableDataCell>
                <CTableDataCell className="text-end">
                  {isOperador ? <CancelButton reviewKey={item.review_key} /> : null}
                </CTableDataCell>
              </CTableRow>
            ))
          )}
        </CTableBody>
      </CTable>
    </PanelSection>
  )
}

/** Reviews con algo en vuelo: el nodo actual y lo gastado hasta ahora. */
export function ActiveSection({
  items,
  budget,
  isOperador,
}: SectionProps & { items: ActiveReview[]; budget: BudgetItem[] }) {
  const budgets = budgetByReview(budget)
  return (
    <PanelSection
      title="En vuelo"
      hint={items.length === 0 ? null : plural(items.length, 'review corriendo', 'reviews corriendo')}
    >
      <CTable small responsive hover align="middle" className="dashboard-table mb-0">
        <CTableHead>
          <CTableRow>
            <CTableHeaderCell scope="col">Review</CTableHeaderCell>
            <CTableHeaderCell scope="col">Nodo en curso</CTableHeaderCell>
            <CTableHeaderCell scope="col">Gasto</CTableHeaderCell>
            <CTableHeaderCell scope="col" className="text-end">
              <span className="visually-hidden">Comandos</span>
            </CTableHeaderCell>
          </CTableRow>
        </CTableHead>
        <CTableBody>
          {items.length === 0 ? (
            <EmptyRow colSpan={4}>No hay ninguna review en vuelo.</EmptyRow>
          ) : (
            items.map((review) => {
              const spend = budgets.get(review.review_key)
              return (
                <CTableRow key={review.review_key}>
                  <CTableDataCell>
                    <ReviewCell reviewKey={review.review_key} />
                  </CTableDataCell>
                  <CTableDataCell>
                    {review.nodes.length === 0 ? (
                      <span className="text-body-secondary">—</span>
                    ) : (
                      <ul className="list-unstyled mb-0">
                        {review.nodes.map((node) => (
                          <li key={node.node}>
                            <strong>{NODE_LABEL[node.node] ?? node.node}</strong>
                            <span className="text-body-secondary small ms-2">
                              desde {formatDateTime(node.started_at)}
                              {node.branches > 1 ? ` · ${node.branches} ramas` : ''}
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </CTableDataCell>
                  <CTableDataCell className="num text-nowrap">
                    {spend === undefined ? (
                      <span className="text-body-secondary">—</span>
                    ) : (
                      <>
                        {formatNumber(spentTokens(spend))}
                        <span className="text-body-secondary small">
                          {' / '}
                          {formatNumber(spend.total)}
                        </span>
                      </>
                    )}
                  </CTableDataCell>
                  <CTableDataCell className="text-end">
                    {isOperador ? <CancelButton reviewKey={review.review_key} /> : null}
                  </CTableDataCell>
                </CTableRow>
              )
            })
          )}
        </CTableBody>
      </CTable>
    </PanelSection>
  )
}

function pausedDetail(item: PausedItem) {
  if (item.kind === 'fix') {
    const runKeys = item.jobs.map((job) => String(job.run_key ?? '')).filter(Boolean)
    return (
      <span className="small">
        Verificación de la wave
        {runKeys.length > 0 ? (
          <span className="text-body-secondary ms-2">{runKeys.join(', ')}</span>
        ) : null}
      </span>
    )
  }
  return (
    <span className="small">
      {item.pending.length === 0 ? (
        <span className="text-body-secondary">Sin hallazgos pendientes</span>
      ) : (
        plural(item.pending.length, 'hallazgo por decidir', 'hallazgos por decidir')
      )}
      {item.publication ? (
        <CBadge color="info" className="ms-2">
          publica
        </CBadge>
      ) : null}
    </span>
  )
}

/**
 * Una pausa con su acción.
 *
 * Las de `human` esperan a una persona; las de `fix` esperan a la wave de
 * corrección. Se pintan en secciones distintas porque la acción que desatasca cada
 * una no es la misma y mezclarlas haría creer que las dos están bloqueadas.
 */
export function PausedSection({
  title,
  hint,
  items,
  isOperador,
}: SectionProps & { title: string; hint: string; items: PausedItem[] }) {
  return (
    <PanelSection
      title={title}
      hint={items.length === 0 ? null : plural(items.length, 'pausa', 'pausas')}
    >
      <CTable small responsive hover align="middle" className="dashboard-table mb-0">
        <CTableHead>
          <CTableRow>
            <CTableHeaderCell scope="col">Review</CTableHeaderCell>
            <CTableHeaderCell scope="col">Estado</CTableHeaderCell>
            <CTableHeaderCell scope="col">Desde</CTableHeaderCell>
            <CTableHeaderCell scope="col">Detalle</CTableHeaderCell>
            <CTableHeaderCell scope="col" className="text-end">
              <span className="visually-hidden">Comandos</span>
            </CTableHeaderCell>
          </CTableRow>
        </CTableHead>
        <CTableBody>
          {items.length === 0 ? (
            <EmptyRow colSpan={5}>{hint}</EmptyRow>
          ) : (
            items.map((item) => (
              <CTableRow key={`${item.review_key}:${item.kind}`}>
                <CTableDataCell>
                  <ReviewCell reviewKey={item.review_key} />
                </CTableDataCell>
                <CTableDataCell>
                  <code>{item.state}</code>
                </CTableDataCell>
                <CTableDataCell className="text-nowrap small">
                  {formatDateTime(item.created_at)}
                </CTableDataCell>
                <CTableDataCell>{pausedDetail(item)}</CTableDataCell>
                <CTableDataCell className="text-end">
                  {isOperador ? <CancelButton reviewKey={item.review_key} /> : null}
                </CTableDataCell>
              </CTableRow>
            ))
          )}
        </CTableBody>
      </CTable>
    </PanelSection>
  )
}

/** Descartes de la DLQ: reencolables (`retry`) o descartables (`forget`). */
export function DeadSection({ items, isOperador }: SectionProps & { items: DeadLetterItem[] }) {
  return (
    <PanelSection
      title="Descartes"
      hint={items.length === 0 ? null : plural(items.length, 'review descartada', 'reviews descartadas')}
    >
      <CTable small responsive hover align="middle" className="dashboard-table mb-0">
        <CTableHead>
          <CTableRow>
            <CTableHeaderCell scope="col">Review</CTableHeaderCell>
            <CTableHeaderCell scope="col" className="text-end">Intentos</CTableHeaderCell>
            <CTableHeaderCell scope="col">Motivo</CTableHeaderCell>
            <CTableHeaderCell scope="col">Descartada</CTableHeaderCell>
            <CTableHeaderCell scope="col" className="text-end">
              <span className="visually-hidden">Comandos</span>
            </CTableHeaderCell>
          </CTableRow>
        </CTableHead>
        <CTableBody>
          {items.length === 0 ? (
            <EmptyRow colSpan={5}>No hay descartes.</EmptyRow>
          ) : (
            items.map((item) => (
              <CTableRow key={item.review_key}>
                <CTableDataCell>
                  <ReviewCell reviewKey={item.review_key} />
                </CTableDataCell>
                <CTableDataCell className="num text-end">
                  {formatNumber(item.attempts)}
                </CTableDataCell>
                <CTableDataCell className="small">
                  {item.reason || '—'}
                  {item.error ? (
                    <span className="d-block text-body-secondary text-truncate">{item.error}</span>
                  ) : null}
                </CTableDataCell>
                <CTableDataCell className="text-nowrap small">
                  {formatDateTime(item.discarded_at)}
                </CTableDataCell>
                <CTableDataCell className="text-end text-nowrap">
                  {isOperador ? (
                    <span className="d-inline-flex gap-2">
                      <RetryButton reviewKey={item.review_key} />
                      <ForgetButton reviewKey={item.review_key} />
                    </span>
                  ) : null}
                </CTableDataCell>
              </CTableRow>
            ))
          )}
        </CTableBody>
      </CTable>
    </PanelSection>
  )
}

/**
 * Gasto por review.
 *
 * El total es el techo configurado (`llm_budget_tokens_per_pr`) y `remaining` ya lo
 * calcula el servidor: el cliente no resta presupuestos por su cuenta, para que dos
 * pantallas no den dos cifras distintas.
 */
export function BudgetSection({ items }: { items: BudgetItem[] }) {
  return (
    <PanelSection
      title="Presupuesto"
      hint={items.length === 0 ? null : plural(items.length, 'review con gasto', 'reviews con gasto')}
    >
      <CTable small responsive hover align="middle" className="dashboard-table mb-0">
        <CTableHead>
          <CTableRow>
            <CTableHeaderCell scope="col">Review</CTableHeaderCell>
            <CTableHeaderCell scope="col" className="text-end">Gastado</CTableHeaderCell>
            <CTableHeaderCell scope="col" className="text-end">Reservado</CTableHeaderCell>
            <CTableHeaderCell scope="col" className="text-end">Total</CTableHeaderCell>
            <CTableHeaderCell scope="col" className="text-end">Restante</CTableHeaderCell>
            <CTableHeaderCell scope="col" style={{ width: '18%' }}>
              <span className="visually-hidden">Consumo</span>
            </CTableHeaderCell>
          </CTableRow>
        </CTableHead>
        <CTableBody>
          {items.length === 0 ? (
            <EmptyRow colSpan={6}>Ninguna review ha gastado tokens todavía.</EmptyRow>
          ) : (
            items.map((item) => {
              const spent = spentTokens(item)
              const percent = item.total > 0 ? Math.min(100, (spent / item.total) * 100) : 0
              return (
                <CTableRow key={item.review_key}>
                  <CTableDataCell>
                    <ReviewCell reviewKey={item.review_key} />
                  </CTableDataCell>
                  <CTableDataCell className="num text-end">
                    {formatNumber(item.committed)}
                  </CTableDataCell>
                  <CTableDataCell className="num text-end">
                    {formatNumber(item.reserved)}
                  </CTableDataCell>
                  <CTableDataCell className="num text-end">
                    {formatNumber(item.total)}
                  </CTableDataCell>
                  <CTableDataCell className="num text-end">
                    {formatNumber(item.remaining)}
                  </CTableDataCell>
                  <CTableDataCell>
                    <CProgress
                      thin
                      color={percent >= 90 ? 'danger' : percent >= 70 ? 'warning' : 'success'}
                      value={percent}
                      aria-label={`${formatNumber(spent)} de ${formatNumber(item.total)} tokens`}
                    />
                  </CTableDataCell>
                </CTableRow>
              )
            })
          )}
        </CTableBody>
      </CTable>
    </PanelSection>
  )
}
