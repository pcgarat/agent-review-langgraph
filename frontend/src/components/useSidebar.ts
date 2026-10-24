import { useCallback, useState } from 'react'

/**
 * Visibilidad del sidebar, en estado local y no en Redux.
 *
 * La plantilla de CoreUI lo guarda en un store global. Aquí solo lo usa este
 * armazón y nadie más lo lee: un store para un valor que consume un componente
 * es ceremonia. Si algún día lo necesita otra pantalla, se sube entonces.
 */
export function useSidebar() {
  const [sidebarVisible, setSidebarVisible] = useState(true)
  const [unfoldable, setUnfoldable] = useState(false)

  const toggleUnfoldable = useCallback(() => setUnfoldable((value) => !value), [])

  return { sidebarVisible, setSidebarVisible, unfoldable, toggleUnfoldable }
}
