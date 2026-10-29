import { CFormCheck } from '@coreui/react'

import type { AutoScrollControls } from './useAutoScroll'

/**
 * El check de «seguir el final» que llevan los paneles en vivo.
 *
 * Va en la cabecera de cada panel, no en un ajuste global: la consola y el
 * razonamiento se miran en momentos distintos y pueden querer estados distintos a
 * la vez. El `id` hace falta para que la etiqueta sea el área de clic del check.
 */
export function FollowToggle({
  id,
  autoScroll,
}: {
  id: string
  autoScroll: AutoScrollControls
}) {
  return (
    <CFormCheck
      id={id}
      className="small mb-0"
      label="Seguir el final"
      checked={autoScroll.enabled}
      onChange={(event) => autoScroll.setEnabled(event.target.checked)}
      title="Con el check marcado, el panel baja solo cuando llegan líneas nuevas."
    />
  )
}
