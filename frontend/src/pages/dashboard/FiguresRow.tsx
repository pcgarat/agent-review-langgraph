import { CCard, CCardBody, CCol, CRow } from '@coreui/react'

import type { PanelState } from '@/api/types/state'

import { dashboardFigures } from './figures'
import { formatNumber } from './format'

/**
 * Las cuatro cifras de la primera pantalla (029, D1).
 *
 * Van antes que cualquier tabla porque responden a la única pregunta que se hace al
 * abrir el panel: si hay algo esperando o algo roto. El detalle de cada una es una
 * segunda lectura, no otra cifra del mismo tamaño.
 */
export function FiguresRow({ state }: { state: PanelState }) {
  return (
    <CRow className="g-3 mb-4">
      {dashboardFigures(state).map((figure) => (
        <CCol key={figure.key} xs={6} lg={3}>
          <CCard className="figure-card h-100">
            <CCardBody className="py-3">
              <p className="eyebrow mb-1">{figure.label}</p>
              <p className="num display-6 lh-1 mb-2" data-testid={`figure-${figure.key}`}>
                {formatNumber(figure.value)}
              </p>
              <p className="small text-body-secondary mb-0">{figure.detail}</p>
            </CCardBody>
          </CCard>
        </CCol>
      ))}
    </CRow>
  )
}
