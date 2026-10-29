import { Link } from 'react-router-dom'
import {
  CCol,
  CDropdown,
  CDropdownItem,
  CDropdownMenu,
  CDropdownToggle,
  CRow,
  CWidgetStatsA,
} from '@coreui/react'
import CIcon from '@coreui/icons-react'
import { cilBan, cilBell, cilList, cilMediaPlay, cilOptions } from '@coreui/icons'
import type { PanelState } from '@/api/types/state'
import { dashboardFigures, type DashboardFigure } from './figures'
import { formatNumber } from './format'
import { SECTION_ID } from './sections'

/** Color e icono de cada cifra: el orden de colores es el de la demo de CoreUI. */
const LOOK: Record<DashboardFigure['key'], { color: string; icon: string[] }> = {
  queue: { color: 'primary', icon: cilList },
  active: { color: 'info', icon: cilMediaPlay },
  human: { color: 'warning', icon: cilBell },
  dead: { color: 'danger', icon: cilBan },
}

function FigureActions({ figure }: { figure: DashboardFigure }) {
  return (
    <CDropdown alignment="end">
      <CDropdownToggle
        color="transparent"
        caret={false}
        className="text-white p-0"
        aria-label={`Opciones de ${figure.label}`}
      >
        <CIcon icon={cilOptions} />
      </CDropdownToggle>
      <CDropdownMenu>
        <CDropdownItem href={`#${SECTION_ID[figure.key]}`}>Ir a la sección</CDropdownItem>
        {figure.key === 'human' ? (
          <CDropdownItem as={Link} to="/bandeja">
            Abrir la bandeja
          </CDropdownItem>
        ) : null}
      </CDropdownMenu>
    </CDropdown>
  )
}

/**
 * Las cuatro cifras de la primera pantalla (029, D1).
 *
 * Van antes que cualquier tabla porque responden a la única pregunta que se hace al
 * abrir el panel: si hay algo esperando o algo roto. Donde la demo pinta un
 * gráfico va el detalle de la cifra: el estado no trae series temporales.
 */
export function FiguresRow({ state }: { state: PanelState }) {
  return (
    <CRow className="mb-4" xs={{ gutter: 4 }}>
      {dashboardFigures(state).map((figure) => {
        const look = LOOK[figure.key]
        return (
          <CCol key={figure.key} sm={6} xl={3}>
            <CWidgetStatsA
              color={look.color}
              className="h-100"
              value={
                <span className="num" data-testid={`figure-${figure.key}`}>
                  {formatNumber(figure.value)}
                </span>
              }
              title={figure.label}
              action={<FigureActions figure={figure} />}
              chart={
                <div className="figure-foot d-flex align-items-end justify-content-between gap-3 px-3 pb-3">
                  <span className="small">{figure.detail}</span>
                  <CIcon icon={look.icon} size="3xl" className="figure-icon flex-shrink-0" />
                </div>
              }
            />
          </CCol>
        )
      })}
    </CRow>
  )
}
