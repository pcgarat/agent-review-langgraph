import type { ReactNode } from 'react'
import { CCard, CCardBody, CCardHeader } from '@coreui/react'

interface PanelSectionProps {
  title: string
  /** Ancla para saltar a la sección desde su cifra. */
  id?: string
  /** Segunda línea de la cabecera: el recuento o el matiz que la tabla no dice. */
  hint?: ReactNode
  actions?: ReactNode
  children: ReactNode
}

/** Una sección del cuadro de mando: cabecera con su recuento y su tabla debajo. */
export function PanelSection({ title, id, hint, actions, children }: PanelSectionProps) {
  return (
    <CCard className="panel-section mb-4" id={id}>
      <CCardHeader className="d-flex flex-wrap align-items-center gap-2">
        <h2 className="h6 mb-0 me-auto">{title}</h2>
        {hint !== undefined && hint !== null ? (
          <span className="small text-body-secondary">{hint}</span>
        ) : null}
        {actions}
      </CCardHeader>
      <CCardBody className="p-0">{children}</CCardBody>
    </CCard>
  )
}
