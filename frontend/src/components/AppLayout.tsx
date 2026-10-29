import type { ReactNode } from 'react'
import { CContainer, CFooter } from '@coreui/react'

import { AppHeader } from './AppHeader'
import { AppSidebar } from './AppSidebar'
import { useSidebar } from './useSidebar'

interface AppLayoutProps {
  title: string
  children: ReactNode
}

/**
 * Armazón del panel (bloque E, 029): el `DefaultLayout` de la plantilla de CoreUI,
 * con su sidebar, su cabecera de dos filas, el contenido en `container-lg` y el
 * footer.
 */
export function AppLayout({ title, children }: AppLayoutProps) {
  const { sidebarVisible, setSidebarVisible, unfoldable, toggleUnfoldable } = useSidebar()

  return (
    <>
      <AppSidebar
        visible={sidebarVisible}
        unfoldable={unfoldable}
        onVisibleChange={setSidebarVisible}
        onToggleUnfoldable={toggleUnfoldable}
      />
      <div className="wrapper d-flex flex-column min-vh-100">
        <AppHeader title={title} onToggleSidebar={() => setSidebarVisible(!sidebarVisible)} />
        <div className="body flex-grow-1">
          <CContainer className="px-4" lg>
            {children}
          </CContainer>
        </div>
        <CFooter className="px-4">
          <div>
            <span className="fw-semibold">Agent reviews</span>
            <span className="ms-1 text-body-secondary">· panel de operación</span>
          </div>
          <div className="ms-auto text-body-secondary small">
            Solo lectura salvo donde diga el botón
          </div>
        </CFooter>
      </div>
    </>
  )
}
