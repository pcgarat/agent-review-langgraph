import type { ReactNode } from 'react'
import {
  CContainer,
  CHeader,
  CHeaderNav,
  CHeaderToggler,
  CNavItem,
  CSidebar,
  CSidebarBrand,
  CSidebarFooter,
  CSidebarHeader,
  CSidebarToggler,
} from '@coreui/react'
import CIcon from '@coreui/icons-react'
import { cilMenu } from '@coreui/icons'

import { useSession } from '@/session/session'
import { RefreshSelector } from './RefreshSelector'
import { SidebarNav } from './SidebarNav'
import { UserMenu } from './UserMenu'
import { useSidebar } from './useSidebar'
import { ThemeSwitcher } from './ThemeSwitcher'
import { useColorMode } from './useColorMode'

interface AppLayoutProps {
  title: string
  children: ReactNode
}

/** Armazón del panel: sidebar, cabecera y contenido (bloque E, 029). */
export function AppLayout({ title, children }: AppLayoutProps) {
  const { sidebarVisible, setSidebarVisible, unfoldable, toggleUnfoldable } = useSidebar()
  const { session } = useSession()
  const { resolvedMode } = useColorMode()

  return (
    <>
      <CSidebar
        className="border-end"
        colorScheme={resolvedMode}
        position="fixed"
        unfoldable={unfoldable}
        visible={sidebarVisible}
        onVisibleChange={setSidebarVisible}
      >
        <CSidebarHeader className="border-bottom">
          <CSidebarBrand>
            <span className="panel-brand">agent review</span>
          </CSidebarBrand>
        </CSidebarHeader>
        <SidebarNav />
        <CSidebarFooter className="border-top d-none d-lg-flex">
          <CSidebarToggler onClick={toggleUnfoldable} />
        </CSidebarFooter>
      </CSidebar>

      <div className="wrapper d-flex flex-column min-vh-100">
        <CHeader position="sticky" className="p-0 header-slim">
          <CContainer className="border-bottom px-4" fluid>
            <CHeaderToggler onClick={() => setSidebarVisible(!sidebarVisible)}>
              <CIcon icon={cilMenu} size="lg" />
            </CHeaderToggler>
            <span className="header-title text-truncate">{title}</span>
            <CHeaderNav className="ms-auto align-items-center">
              <RefreshSelector />
              <li className="nav-item py-1">
                <div className="vr h-100 mx-2 text-body text-opacity-75" />
              </li>
              <CNavItem className="text-body-secondary small d-none d-sm-block">
                {session.user?.user ?? '—'}
                {session.user ? <span className="ms-1 opacity-75">({session.user.role})</span> : null}
              </CNavItem>
              <li className="nav-item py-1">
                <div className="vr h-100 mx-2 text-body text-opacity-75" />
              </li>
              <ThemeSwitcher />
              <li className="nav-item py-1">
                <div className="vr h-100 mx-2 text-body text-opacity-75" />
              </li>
              <UserMenu />
            </CHeaderNav>
          </CContainer>
        </CHeader>
        <div className="body flex-grow-1 px-4 py-4">
          <CContainer fluid>{children}</CContainer>
        </div>
        <footer className="footer border-top px-4 py-2 small text-body-secondary">
          Panel de operación · solo lectura salvo donde diga el botón
        </footer>
      </div>
    </>
  )
}
