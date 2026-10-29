import { CBadge, CTable, CTableBody, CTableDataCell, CTableHead, CTableHeaderCell, CTableRow } from '@coreui/react'
import { Link } from 'react-router-dom'

import { reviewHref } from '@/api/client'
import type { ParkReason, ParkedItem } from '@/api/types/state'
import { plural } from '@/pages/dashboard/format'
import { PanelSection } from '@/pages/dashboard/PanelSection'
import { EmptyRow } from '@/pages/dashboard/EmptyRow'

/**
 * Qué hacer con cada motivo (030, K6).
 *
 * `session` es lo único que se resuelve desde el panel: el token se escribe en
 * `/claude` y el servidor desaparca los jobs al guardarlo. El cupo no se debe
 * poder saltar desde una pantalla, así que no lleva botón; `ceiling` se cambia
 * en `/config`, pero el job que lo agotó no se suelta al subir el dial.
 */
const REASON: Record<ParkReason, { label: string; help: string; to?: string; action?: string }> = {
  session: {
    label: 'Falta la sesión de Claude',
    help: 'El worker no puede ejecutar el CLI sin token. Se desaparcan solos al guardarlo.',
    to: '/claude',
    action: 'Escribir el token',
  },
  quota: {
    label: 'Cupo agotado',
    help: 'No se reintenta: volver a lanzarlo gastaría otra vez el límite. Hay que relanzarlo a mano cuando se renueve.',
  },
  'quota-wait': {
    label: 'Esperando cupo',
    help: 'Sale solo cuando caduca el aviso de cupo; no hay nada que hacer.',
  },
  gateway: {
    label: 'Gateway del clúster caído',
    help: 'Salen solos cuando nan-gateway vuelve a responder. Elegir Claude en /claude también los suelta.',
    to: '/claude',
    action: 'Cambiar de modelo',
  },
  'cluster-wait': {
    label: 'El clúster limita las llamadas',
    help: 'Salen solos cuando pasan unos minutos de enfriamiento; no gastan intentos.',
  },
  ceiling: {
    label: 'Techo de turnos o de tiempo',
    help: 'El CLI llegó al techo. Subirlo no suelta este job: hay que eliminarlo y relanzarlo.',
    to: '/config',
    action: 'Ver los diales',
  },
}

/**
 * Jobs apartados por el worker (030, P1).
 *
 * Van en su propia sección y no en la cola: no van a arrancar solos. Mezclarlos
 * con `reviews:jobs` haría creer que el sistema los va a coger, y el operador no
 * miraría lo que falta.
 *
 * La sección se pinta siempre, vacía incluida: una sección que aparece y
 * desaparece deja sin saber si el panel la está mirando.
 */
export function ParkedSection({ items }: { items: ParkedItem[] }) {
  return (
    <PanelSection
      title="Aparcados"
      hint={items.length === 0 ? null : plural(items.length, 'job esperando', 'jobs esperando')}
    >
      <CTable small responsive hover align="middle" className="dashboard-table mb-0">
        <CTableHead>
          <CTableRow>
            <CTableHeaderCell scope="col">Review</CTableHeaderCell>
            <CTableHeaderCell scope="col">Motivo</CTableHeaderCell>
            <CTableHeaderCell scope="col">Qué lo desatasca</CTableHeaderCell>
          </CTableRow>
        </CTableHead>
        <CTableBody>
          {items.length === 0 ? (
            <EmptyRow colSpan={3}>Ningún job está esperando al worker.</EmptyRow>
          ) : (
            items.map((item) => {
              const reason = REASON[item.reason]
              return (
                <CTableRow key={`${item.review_key}:${item.pipeline_id}`}>
                  <CTableDataCell>
                    <Link to={reviewHref(item.review_key)} className="text-break">
                      {item.review_key}
                    </Link>
                  </CTableDataCell>
                  <CTableDataCell>
                    <CBadge color={item.reason === 'session' ? 'warning' : 'secondary'}>
                      {reason.label}
                    </CBadge>
                  </CTableDataCell>
                  <CTableDataCell className="small">
                    {reason.help}
                    {reason.to !== undefined ? (
                      <Link to={reason.to} className="ms-2 text-nowrap">
                        {reason.action}
                      </Link>
                    ) : null}
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
