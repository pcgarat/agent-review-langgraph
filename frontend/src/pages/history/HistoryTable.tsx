import {
  CPagination,
  CPaginationItem,
  CTable,
  CTableBody,
  CTableDataCell,
  CTableHead,
  CTableHeaderCell,
  CTableRow,
} from '@coreui/react'
import { Link } from 'react-router-dom'

import { reviewHref } from '@/api/client'
import type { HistoryItem } from '@/api/types/history'
import { EmptyRow } from '@/pages/dashboard/EmptyRow'
import { formatDateTime, formatNumber } from '@/pages/dashboard/format'

import type { Page } from './params'

const END_LABEL: Record<string, string> = {
  completa: 'completa',
  parcial: 'parcial',
  incompleta: 'incompleta',
}

const END_COLOR: Record<string, string> = {
  completa: 'text-success',
  parcial: 'text-warning',
  incompleta: 'text-danger',
}

function severityText(bySeverity: Record<string, number>): string {
  const entries = Object.entries(bySeverity)
  return entries.length === 0
    ? ''
    : entries.map(([severity, count]) => `${severity}: ${formatNumber(count)}`).join(', ')
}

/** Tabla densa del histórico: una fila por review terminada (029, B3). */
export function HistoryTable({ page }: { page: Page<HistoryItem> }) {
  return (
    <CTable small responsive hover align="middle" className="history-table mb-0">
      <CTableHead>
        <CTableRow>
          <CTableHeaderCell scope="col">Terminada</CTableHeaderCell>
          <CTableHeaderCell scope="col">Review</CTableHeaderCell>
          <CTableHeaderCell scope="col">PR</CTableHeaderCell>
          <CTableHeaderCell scope="col">Resultado</CTableHeaderCell>
          <CTableHeaderCell scope="col" className="text-end">Hallazgos</CTableHeaderCell>
          <CTableHeaderCell scope="col" className="text-end">Revisados / sin revisar</CTableHeaderCell>
          <CTableHeaderCell scope="col" className="text-end">Tokens</CTableHeaderCell>
          <CTableHeaderCell scope="col">Razonamiento</CTableHeaderCell>
        </CTableRow>
      </CTableHead>
      <CTableBody>
        {page.rows.length === 0 ? (
          <EmptyRow colSpan={8}>Ninguna review coincide con los filtros.</EmptyRow>
        ) : (
          page.rows.map((item) => (
            <CTableRow key={item.review_key}>
              <CTableDataCell className="text-nowrap small">
                {formatDateTime(item.finished_at)}
              </CTableDataCell>
              <CTableDataCell>
                <Link to={reviewHref(item.review_key)} className="text-break">
                  {item.review_key}
                </Link>
              </CTableDataCell>
              <CTableDataCell className="text-nowrap">
                {item.repo}
                {item.repo === '' ? '' : '#'}
                {item.pr_id}{' '}
                <code className="small text-body-secondary">{item.head_sha.slice(0, 8)}</code>
              </CTableDataCell>
              <CTableDataCell className={`text-nowrap ${END_COLOR[item.end] ?? ''}`}>
                {END_LABEL[item.end] ?? item.end}
              </CTableDataCell>
              <CTableDataCell className="text-end">
                <span className="num">{formatNumber(item.findings)}</span>
                {Object.keys(item.by_severity).length > 0 ? (
                  <span className="small text-body-secondary ms-1">
                    ({severityText(item.by_severity)})
                  </span>
                ) : null}
              </CTableDataCell>
              <CTableDataCell className="text-end text-nowrap">
                <span className="num">{formatNumber(item.files_reviewed)}</span>
                {' / '}
                <span className="num text-body-secondary">{formatNumber(item.files_skipped)}</span>
              </CTableDataCell>
              <CTableDataCell className="num text-end">{formatNumber(item.tokens)}</CTableDataCell>
              <CTableDataCell className="text-center">
                {item.reasoning ? 'sí' : <span className="text-body-secondary">—</span>}
              </CTableDataCell>
            </CTableRow>
          ))
        )}
      </CTableBody>
    </CTable>
  )
}

interface PageLinksProps {
  page: Page<HistoryItem>
  /** Enlace de cada página: la página vive en la URL y se puede compartir. */
  hrefFor: (page: number) => string
  onPage: (page: number) => void
}

/** Números de página con ventana corta: 500 filas no pueden ser 20 botones. */
function pageNumbers(current: number, pages: number): number[] {
  const span = 2
  const first = Math.max(1, Math.min(current - span, pages - span * 2))
  const last = Math.min(pages, first + span * 2)
  return Array.from({ length: Math.max(0, last - first + 1) }, (_, index) => first + index)
}

/**
 * Paginación del histórico.
 *
 * Cada elemento es un `a` con su `href` y un `onClick` que evita la recarga: en un
 * `button` CoreUI descarta los props que no sean `ref`/`className`/`children` y el
 * clic se perdería.
 */
export function HistoryPagination({ page, hrefFor, onPage }: PageLinksProps) {
  if (page.pages <= 1) return null

  const item = (number: number, label: string, disabled = false) => (
    <CPaginationItem
      key={label}
      as="a"
      href={hrefFor(number)}
      active={!disabled && number === page.page}
      disabled={disabled}
      onClick={(event) => {
        event.preventDefault()
        if (!disabled) onPage(number)
      }}
    >
      {label}
    </CPaginationItem>
  )

  return (
    <CPagination aria-label="Páginas del histórico" className="mt-3">
      {item(page.page - 1, 'Anterior', page.page === 1)}
      {pageNumbers(page.page, page.pages).map((number) => item(number, String(number)))}
      {item(page.page + 1, 'Siguiente', page.page === page.pages)}
    </CPagination>
  )
}
