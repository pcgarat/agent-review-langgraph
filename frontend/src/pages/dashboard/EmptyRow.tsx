import type { ReactNode } from 'react'

interface EmptyRowProps {
  /** Cuántas columnas tiene la tabla: la celda vacía las ocupa todas. */
  colSpan: number
  children: ReactNode
}

/**
 * Fila de «aquí no hay nada».
 *
 * Una tabla sin filas deja solo la cabecera y parece rota; el texto dentro dice que
 * el hueco es un dato («no hay descartes»), no un fallo de carga.
 */
export function EmptyRow({ colSpan, children }: EmptyRowProps) {
  return (
    <tr>
      <td colSpan={colSpan} className="text-body-secondary small py-3">
        {children}
      </td>
    </tr>
  )
}
