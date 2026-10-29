import CIcon from '@coreui/icons-react'
import { cilMagnifyingGlass } from '@coreui/icons'

/** Símbolo de la marca: el que queda en el sidebar estrecho y encabeza el login. */
export function Sygnet() {
  return (
    <span className="sidebar-sygnet" aria-hidden="true">
      <CIcon icon={cilMagnifyingGlass} size="lg" />
    </span>
  )
}
