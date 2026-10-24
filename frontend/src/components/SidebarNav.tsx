import { NavLink } from 'react-router-dom'
import { CNavItem, CNavLink, CNavTitle, CSidebarNav } from '@coreui/react'
import CIcon from '@coreui/icons-react'
import { cilHistory, cilList, cilSettings, cilSpeedometer } from '@coreui/icons'

import { useSession } from '@/session/session'

const BASE_ITEMS = [
  { to: '/', label: 'Cuadro de mando', icon: cilSpeedometer },
  { to: '/bandeja', label: 'Bandeja', icon: cilList },
  { to: '/history', label: 'Histórico', icon: cilHistory },
  { to: '/config', label: 'Configuración', icon: cilSettings },
] as const

export function SidebarNav() {
  const { isOperador } = useSession()

  return (
    <CSidebarNav>
      <CNavTitle>Operación</CNavTitle>
      {BASE_ITEMS.map((item) => (
        <CNavItem key={item.to}>
          <CNavLink to={item.to} as={NavLink} end={item.to === '/'}>
            <CIcon icon={item.icon} className="nav-icon" />
            {item.label}
          </CNavLink>
        </CNavItem>
      ))}
      {isOperador ? (
        <>
          <CNavTitle>Sesión de Claude</CNavTitle>
          <CNavItem>
            <CNavLink to="/claude" as={NavLink}>
              <CIcon icon={cilSettings} className="nav-icon" />
              Token del CLI
            </CNavLink>
          </CNavItem>
        </>
      ) : null}
    </CSidebarNav>
  )
}
