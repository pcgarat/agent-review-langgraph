import { Link } from 'react-router-dom'
import {
  CCloseButton,
  CSidebar,
  CSidebarBrand,
  CSidebarFooter,
  CSidebarHeader,
  CSidebarToggler,
} from '@coreui/react'

import { SidebarNav } from './SidebarNav'
import { Sygnet } from './Sygnet'

interface AppSidebarProps {
  visible: boolean
  unfoldable: boolean
  onVisibleChange: (visible: boolean) => void
  onToggleUnfoldable: () => void
}

/**
 * Sidebar de la plantilla: oscuro en los dos temas, como en la demo de CoreUI.
 * El texto de la marca lleva `sidebar-brand-full` para que el modo estrecho deje
 * solo el símbolo.
 */
export function AppSidebar({ visible, unfoldable, onVisibleChange, onToggleUnfoldable }: AppSidebarProps) {
  return (
    <CSidebar
      className="border-end"
      colorScheme="dark"
      position="fixed"
      unfoldable={unfoldable}
      visible={visible}
      onVisibleChange={onVisibleChange}
    >
      <CSidebarHeader className="border-bottom">
        <CSidebarBrand as={Link} to="/" aria-label="Agent reviews, ir al cuadro de mando">
          <Sygnet />
          <span className="sidebar-brand-full">Agent reviews</span>
        </CSidebarBrand>
        <CCloseButton
          className="d-lg-none"
          dark
          aria-label="Cerrar el menú"
          onClick={() => onVisibleChange(false)}
        />
      </CSidebarHeader>
      <SidebarNav />
      <CSidebarFooter className="border-top d-none d-lg-flex">
        <CSidebarToggler aria-label="Estrechar el menú" onClick={onToggleUnfoldable} />
      </CSidebarFooter>
    </CSidebar>
  )
}
