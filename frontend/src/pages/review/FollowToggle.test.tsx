/**
 * El check de «seguir el final» de los paneles en vivo.
 *
 * Lo que se prueba es la regla, no el `scrollTop`: jsdom no hace layout, así que
 * `scrollHeight` y `clientHeight` son 0 y cualquier aserción sobre la posición
 * sería una mentira que pasa por casualidad. Se prueba el estado del check, que es
 * lo único que el hook decide y lo que la persona ve.
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { FollowToggle } from './FollowToggle'
import { useAutoScroll } from './useAutoScroll'

function Harness() {
  const autoScroll = useAutoScroll(0)
  return <FollowToggle id="harness-follow" autoScroll={autoScroll} />
}

/** Deja el check en el estado que tendría tras bajar a mano. */
describe('check de seguir el final', () => {
  it('empieza marcado: lo normal es querer ver llegar las líneas', () => {
    render(<Harness />)
    // Sin marcar, la consola deja de seguirse sola desde el primer render y el
    // operador no vería nada sin tocar nada.
    expect(screen.getByRole('checkbox', { name: 'Seguir el final' })).toBeChecked()
  })

  it('se puede desmarcar para quedarse leyendo una línea de arriba', async () => {
    render(<Harness />)
    const user = userEvent.setup()
    const check = screen.getByRole('checkbox', { name: 'Seguir el final' })

    await user.click(check)

    expect(check).not.toBeChecked()
  })

  it('se vuelve a marcar y el panel vuelve a seguir', async () => {
    render(<Harness />)
    const user = userEvent.setup()
    const check = screen.getByRole('checkbox', { name: 'Seguir el final' })

    await user.click(check)
    await user.click(check)

    expect(check).toBeChecked()
  })
})
