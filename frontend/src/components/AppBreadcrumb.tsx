import { Link } from 'react-router-dom'
import { CBreadcrumb, CBreadcrumbItem } from '@coreui/react'

/**
 * Segunda fila de la cabecera. El título de la pantalla va aquí como migas y no
 * como `h1`: la cabecera es una barra de herramientas, no un encabezado.
 */
export function AppBreadcrumb({ title }: { title: string }) {
  return (
    <CBreadcrumb className="my-0">
      <CBreadcrumbItem>
        <Link to="/">Inicio</Link>
      </CBreadcrumbItem>
      <CBreadcrumbItem active className="text-truncate">
        {title}
      </CBreadcrumbItem>
    </CBreadcrumb>
  )
}
