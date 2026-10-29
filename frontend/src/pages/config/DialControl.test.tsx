import { useState } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { DialControl } from './DialControl'
import type { DraftValue } from './draft'

/** El dial de turnos con su valor en estado, como lo tiene la página. */
function Turns({ initial, start }: { initial: number; start: string }) {
  const [value, setValue] = useState<DraftValue>(start)
  return (
    <>
      <DialControl
        name="claude_max_turns"
        initial={initial}
        value={value}
        locked={false}
        disabled={false}
        onChange={setValue}
      />
      <output data-testid="valor">{String(value)}</output>
    </>
  )
}

const valor = () => screen.getByTestId('valor').textContent

describe('DialControl: tope de turnos', () => {
  it('apagado de inicio, encenderlo propone 200', async () => {
    render(<Turns initial={0} start="0" />)
    expect(screen.queryByRole('spinbutton')).toBeNull()
    await userEvent.click(screen.getByLabelText('Limitar turnos'))
    expect(valor()).toBe('200')
    expect(screen.getByRole('spinbutton')).toHaveValue(200)
  })

  it('apagar y volver a encender recupera el último valor', async () => {
    render(<Turns initial={50} start="50" />)
    const toggle = screen.getByLabelText('Limitar turnos')
    await userEvent.click(toggle)
    expect(valor()).toBe('0')
    expect(screen.queryByRole('spinbutton')).toBeNull()
    await userEvent.click(toggle)
    expect(valor()).toBe('50')
  })

  it('escribir un 0 no desmonta el campo mientras tiene el foco', async () => {
    render(<Turns initial={50} start="50" />)
    const input = screen.getByRole('spinbutton')
    await userEvent.clear(input)
    await userEvent.type(input, '0')
    expect(valor()).toBe('0')
    expect(screen.getByRole('spinbutton')).toHaveFocus()
    await userEvent.type(screen.getByRole('spinbutton'), '7')
    // jsdom normaliza «07» a «7» en un input numérico: lo que importa es que llega.
    expect(valor()).toBe('7')
  })

  it('al salir del campo, un 0 apaga el tope', async () => {
    render(<Turns initial={50} start="50" />)
    const input = screen.getByRole('spinbutton')
    await userEvent.clear(input)
    await userEvent.type(input, '0')
    await userEvent.tab()
    expect(screen.queryByRole('spinbutton')).toBeNull()
    expect(screen.getByLabelText('Limitar turnos')).not.toBeChecked()
  })
})
