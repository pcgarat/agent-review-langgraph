import type { ReactNode } from 'react'
import { CCard, CCardBody, CCardHeader } from '@coreui/react'

interface PanelSectionProps {
  title: string
  /** Segunda línea de la cabecera: el recuento o el matiz que la tabla no dice. */
  hint?: ReactNode
  actions?: ReactNode
  children: ReactNode
}

/** Una sección del cuadro de mando: cabecera con su recuento y su tabla debajo. */
export function PanelSection({ title, hint, actions, children }: PanelSectionProps) {
  return (
    <CCard className="mb-4">
      <CCardHeader className="d-flex flex-wrap align-items-center gap-2">
        <h2 className="h6 mb-0 me-auto">{title}</h2>
        {hint !== undefined ? (
          <span className="small text-body-secondary">{hint}</span>
        ) : null}
        {actions}
      </CCardHeader>
      <CCardBody className="p-0">{children}</CCardBody>
    </CCard>
  )
}
