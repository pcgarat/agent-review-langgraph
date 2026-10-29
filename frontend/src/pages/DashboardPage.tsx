/**
 * Cuadro de mando del panel (029, bloque D).
 *
 * Todo lo que se pinta aquí viene de `GET /api/state`; no hay estado local que
 * sincronizar. Lo que el operador cambia es el servidor, y TanStack Query se encarga
 * de que la copia en pantalla deje de mentir cuando un comando termina.
 */

import { CAlert, CButton, CCard, CCardBody } from '@coreui/react'

import { useStateQuery } from '@/api/queries/useStateQuery'
import { useRefetchInterval } from '@/components/AutoRefreshContext'
import { useSession } from '@/session/session'

import { FiguresRow } from './dashboard/FiguresRow'
import { LaunchSection } from './dashboard/LaunchSection'
import { explainError } from './dashboard/errors'
import { formatDateTime } from './dashboard/format'
import {
  ActiveSection,
  BudgetSection,
  DeadSection,
  PausedSection,
  QueueSection,
} from './dashboard/Sections'
import { ParkedSection } from './dashboard/ParkedSection'
import { SECTION_ID } from './dashboard/sections'
import './dashboard/dashboard.scss'

export function DashboardPage() {
  const refetchInterval = useRefetchInterval()
  const { data, isPending, isError, error, refetch } = useStateQuery({ refetchInterval })
  const { isOperador } = useSession()

  if (isPending) {
    return <p className="text-body-secondary small mb-0">Leyendo el estado del panel…</p>
  }

  if (isError) {
    return (
      <CCard>
        <CCardBody>
          <p className="eyebrow mb-2">Sin datos</p>
          <CAlert color="danger" className="py-2 mb-3">
            {explainError(error, 'No se pudo leer el estado del panel.')}
          </CAlert>
          <CButton type="button" color="primary" variant="outline" onClick={() => void refetch()}>
            Reintentar
          </CButton>
        </CCardBody>
      </CCard>
    )
  }

  return (
    <>
      <p className="eyebrow mb-3">Estado generado el {formatDateTime(data.generated_at)}</p>

      <FiguresRow state={data} />

      {isOperador ? (
        <LaunchSection />
      ) : (
        <CAlert color="info" className="py-2 small">
          Tu rol es <strong>lector</strong>: puedes ver el estado, pero los comandos y el
          lanzamiento son del rol operador.
        </CAlert>
      )}

      <QueueSection items={data.queue} isOperador={isOperador} />
      <ActiveSection items={data.active} budget={data.budget} isOperador={isOperador} />
      <PausedSection
        title="Pausas humanas"
        id={SECTION_ID.human}
        hint="Ninguna review espera una decisión."
        items={data.paused.human}
        isOperador={isOperador}
      />
      <PausedSection
        title="Pausas de fix"
        hint="Ninguna wave espera verificación."
        items={data.paused.fix}
        isOperador={isOperador}
      />
      <ParkedSection items={data.parked} />
      <DeadSection items={data.dead} isOperador={isOperador} />
      <BudgetSection items={data.budget} />
    </>
  )
}
