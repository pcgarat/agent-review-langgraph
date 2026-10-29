import { CAlert, CButton, CCard, CCardBody } from '@coreui/react'
import { useSearchParams } from 'react-router-dom'

import { useRefetchInterval } from '@/components/AutoRefreshContext'
import { PanelSection } from '@/pages/dashboard/PanelSection'
import { explainError } from '@/pages/dashboard/errors'
import { formatNumber } from '@/pages/dashboard/format'

import { HistoryFilters } from './history/HistoryFilters'
import { HistoryPagination, HistoryTable } from './history/HistoryTable'
import { paginate, readFilters, readParams, truncated, writeParams } from './history/params'
import { useHistoryQuery } from './history/useHistory'
import './history/history.scss'

/**
 * Histórico de reviews terminadas (029, B3/B4).
 *
 * Los filtros y la página viven en la URL: el enlace `?repo=…&pr_id=…` es el que
 * publicaba el formulario HTML antiguo y se comparte tal cual. La paginación es en
 * cliente porque la API devuelve hasta 500 filas de una vez (Q5).
 */
export function HistoryPage() {
  const [search, setSearch] = useSearchParams()
  const params = readParams(search)
  const refetchInterval = useRefetchInterval()
  const { data, isPending, isError, error, refetch } = useHistoryQuery(
    readFilters(params),
    { refetchInterval },
  )

  if (isPending) {
    return <p className="text-body-secondary small mb-0">Leyendo el histórico…</p>
  }

  if (isError) {
    return (
      <CCard>
        <CCardBody>
          <p className="eyebrow mb-2">Sin datos</p>
          <CAlert color="danger" className="py-2 mb-3">
            {explainError(error, 'No se pudo leer el histórico.')}
          </CAlert>
          <CButton type="button" color="primary" variant="outline" onClick={() => void refetch()}>
            Reintentar
          </CButton>
        </CCardBody>
      </CCard>
    )
  }

  const page = paginate(data, params.page)
  const goTo = (next: number) => {
    setSearch(writeParams({ ...params, page: next }))
  }
  const hrefFor = (next: number) => {
    const bounded = Math.min(Math.max(1, next), page.pages)
    const search = writeParams({ ...params, page: bounded }).toString()
    return search === '' ? '/history' : `/history?${search}`
  }

  return (
    <PanelSection
      title="Reviews terminadas"
      hint={
        page.total === 0
          ? 'Ninguna coincide'
          : `${formatNumber(page.total)} en total · página ${page.page} de ${page.pages} · filas ${formatNumber(page.from)}–${formatNumber(page.to)}`
      }
    >
      <div className="px-3 pt-3">
        <HistoryFilters />
      </div>
      <HistoryTable page={page} />
      <div className="px-3 pb-3">
        <HistoryPagination page={page} hrefFor={hrefFor} onPage={goTo} />
      </div>
      {truncated(page.total) ? (
        <div className="px-3 pb-3">
          <CAlert color="warning" className="py-2 mb-0 small">
            Se muestran las {formatNumber(page.total)} reviews más recientes y puede haber más:
            acota con los filtros para ver las anteriores.
          </CAlert>
        </div>
      ) : null}
    </PanelSection>
  )
}
