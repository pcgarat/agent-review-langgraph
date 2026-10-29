import type { ReactNode } from 'react'
import { CCard, CCardBody, CCardHeader } from '@coreui/react'

export type SectionTone = 'primary' | 'info' | 'warning' | 'danger' | 'success' | 'secondary'

interface PanelSectionProps {
  title: string
  /** Segunda línea de la cabecera: el recuento o el matiz que la tabla no dice. */
  hint?: ReactNode
  tone?: SectionTone
  actions?: ReactNode
  children: ReactNode
}

/** Una sección del cuadro de mando: cabecera con su recuento y su tabla debajo. */
export function PanelSection({
  title,
  hint,
  tone = 'primary',
  actions,
  children,
}: PanelSectionProps) {
  return (
    <CCard className={`panel-section panel-section--${tone} mb-4`}>
      <CCardHeader className="d-flex flex-wrap align-items-center gap-2">
        <h2 className="h6 mb-0 me-auto">{title}</h2>
        {hint !== undefined && hint !== null ? (
          <span className="section-hint-badge">{hint}</span>
        ) : null}
        {actions}
      </CCardHeader>
      <CCardBody className="p-0">{children}</CCardBody>
    </CCard>
  )
}
