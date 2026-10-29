import { NavLink } from 'react-router-dom'
import { CBadge, CNavItem, CNavLink, CNavTitle, CSidebarNav } from '@coreui/react'
import CIcon from '@coreui/icons-react'
import { cilHistory, cilList, cilSettings, cilSpeedometer } from '@coreui/icons'

import { useStateQuery } from '@/api/queries/useStateQuery'
import { useSession } from '@/session/session'

const BASE_ITEMS = [
  { to: '/', label: 'Cuadro de mando', icon: cilSpeedometer },
  { to: '/bandeja', label: 'Bandeja', icon: cilList },
  { to: '/history', label: 'Histórico', icon: cilHistory },
  { to: '/config', label: 'Configuración', icon: cilSettings },
] as const

export function SidebarNav() {
  const { isOperador } = useSession()
  // Misma consulta y misma clave que el cuadro de mando: el aviso y las cifras
  // salen del mismo documento, no de dos lecturas que puedan discrepar. Sin
  // `fetch` propio ni clave nueva.
  const { data } = useStateQuery()
  const waiting = data?.paused.human.length ?? 0

  return (
    <CSidebarNav>
      <CNavTitle>Operación</CNavTitle>
      {BASE_ITEMS.map((item) => (
        <CNavItem key={item.to}>
          <CNavLink to={item.to} as={NavLink} end={item.to === '/'}>
            <CIcon icon={item.icon} className="nav-icon" />
            {item.label}
            {item.to === '/bandeja' && waiting > 0 ? (
              <CBadge
                color="warning"
                shape="rounded-pill"
                className="ms-auto"
                aria-label={`${waiting} ${waiting === 1 ? 'pausa esperando' : 'pausas esperando'} a una persona`}
                data-testid="sidebar-bandeja-badge"
              >
                {waiting}
              </CBadge>
            ) : null}
          </CNavLink>
        </CNavItem>
      ))}
      {isOperador ? (
        <>
          <CNavTitle>Sesión de Claude</CNavTitle>
          <CNavItem>
            <CNavLink to="/claude" as={NavLink}>
              <CIcon icon={cilSettings} className="nav-icon" />
              Modelo
            </CNavLink>
          </CNavItem>
        </>
      ) : null}
    </CSidebarNav>
  )
}
