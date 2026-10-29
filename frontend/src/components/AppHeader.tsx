import { useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import {
  CBadge,
  CContainer,
  CHeader,
  CHeaderNav,
  CHeaderToggler,
  CNavItem,
  CNavLink,
} from '@coreui/react'
import CIcon from '@coreui/icons-react'
import { cilBell, cilList, cilMenu } from '@coreui/icons'

import { useStateQuery } from '@/api/queries/useStateQuery'
import { AppBreadcrumb } from './AppBreadcrumb'
import { RefreshSelector } from './RefreshSelector'
import { ThemeSwitcher } from './ThemeSwitcher'
import { UserMenu } from './UserMenu'

interface AppHeaderProps {
  title: string
  onToggleSidebar: () => void
}

interface CounterLinkProps {
  to: string
  icon: string[]
  count: number
  color: string
  label: string
}

/** Icono de la cabecera con su contador, como los avisos de la demo de CoreUI. */
function CounterLink({ to, icon, count, color, label }: CounterLinkProps) {
  return (
    <CNavItem>
      <CNavLink to={to} as={Link} className="position-relative" aria-label={label} title={label}>
        <CIcon icon={icon} size="lg" />
        {count > 0 ? (
          <CBadge
            color={color}
            position="top-end"
            shape="rounded-pill"
            className="header-counter"
            aria-hidden="true"
          >
            {count}
          </CBadge>
        ) : null}
      </CNavLink>
    </CNavItem>
  )
}

function Divider() {
  return (
    <li className="nav-item py-1" aria-hidden="true">
      <div className="vr h-100 mx-2 text-body text-opacity-75" />
    </li>
  )
}

/**
 * Cabecera de dos filas de la plantilla: herramientas arriba y breadcrumb abajo.
 * La sombra aparece al hacer scroll, igual que en `AppHeader.jsx` de CoreUI.
 */
export function AppHeader({ title, onToggleSidebar }: AppHeaderProps) {
  const headerRef = useRef<HTMLDivElement>(null)
  // Misma consulta y misma clave que el cuadro de mando y el sidebar: los
  // contadores no pueden discrepar de las cifras.
  const { data } = useStateQuery()
  const waiting = data?.paused.human.length ?? 0
  const queued = (data?.queue.length ?? 0) + (data?.parked?.length ?? 0)

  useEffect(() => {
    const handleScroll = () => {
      headerRef.current?.classList.toggle('shadow-sm', document.documentElement.scrollTop > 0)
    }
    document.addEventListener('scroll', handleScroll)
    return () => document.removeEventListener('scroll', handleScroll)
  }, [])

  return (
    <CHeader position="sticky" className="mb-4 p-0" ref={headerRef}>
      <CContainer className="border-bottom px-4" fluid>
        <CHeaderToggler
          onClick={onToggleSidebar}
          style={{ marginInlineStart: '-14px' }}
          aria-label="Mostrar u ocultar el menú"
        >
          <CIcon icon={cilMenu} size="lg" />
        </CHeaderToggler>
        <CHeaderNav className="ms-auto align-items-center">
          <CounterLink
            to="/bandeja"
            icon={cilBell}
            count={waiting}
            color="danger"
            label={`Bandeja: ${waiting} ${waiting === 1 ? 'pausa esperando' : 'pausas esperando'}`}
          />
          <CounterLink
            to="/"
            icon={cilList}
            count={queued}
            color="warning"
            label={`Cola: ${queued} ${queued === 1 ? 'review esperando' : 'reviews esperando'}`}
          />
          <RefreshSelector />
        </CHeaderNav>
        <CHeaderNav className="align-items-center">
          <Divider />
          <ThemeSwitcher />
          <Divider />
          <UserMenu />
        </CHeaderNav>
      </CContainer>
      <CContainer className="px-4" fluid>
        <AppBreadcrumb title={title} />
      </CContainer>
    </CHeader>
  )
}
