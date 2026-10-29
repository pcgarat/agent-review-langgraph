/** Los descartes de un paso se ven cerrados y se abren al pulsar (034 CP6). */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { REVIEW } from './fixture'
import { StepsPanel } from './StepsPanel'

describe('tarjeta de pasos', () => {
  it('el descartado sale cerrado con su título y su motivo', async () => {
    const trace = {
      ...REVIEW.trace!,
      state: 'completed' as const,
      next: [],
      pause_kind: null,
      nodes: [
        {
          name: 'persona',
          index: 0,
          started_at: null,
          writes: [],
          summary: { returned: 1, contract: 0, l0: 1, kept: 0 },
          discarded: [
            {
              stage: 'l0' as const,
              id: '1',
              title: 'Las horas se calculan en minutos',
              file: 'frontend/src/pages/dashboard/format.ts',
              reason: 'la cita no aparece en el diff',
            },
          ],
        },
      ],
    }
    render(<StepsPanel trace={trace} running={[]} />)

    expect(screen.getByText('La skill devolvió 1 · 1 descartado por L0')).toBeInTheDocument()
    const toggle = screen.getByText('Ver el descartado')
    expect(toggle.closest('details')).not.toHaveAttribute('open')

    await userEvent.click(toggle)

    expect(toggle.closest('details')).toHaveAttribute('open')
    expect(screen.getByText('#1 · Las horas se calculan en minutos')).toBeVisible()
    expect(screen.getByText('L0: la cita no aparece en el diff')).toBeVisible()
  })
})
