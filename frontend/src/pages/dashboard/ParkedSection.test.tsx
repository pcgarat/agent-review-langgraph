import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { PanelState } from '@/api/types/state'
import { ToastProvider } from '@/components/ToastProvider'
import { SessionContext, type SessionContextValue } from '@/session/session'
import { DashboardPage } from '@/pages/DashboardPage'
import { PANEL_STATE } from './fixture'

const OPERADOR: SessionContextValue = {
  session: { status: 'authenticated', user: { user: 'ana', role: 'operador' }, error: null },
  isOperador: true,
  refresh: () => {},
  signOut: async () => {},
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function renderDashboard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SessionContext.Provider value={OPERADOR}>
          <ToastProvider>
            <DashboardPage />
          </ToastProvider>
        </SessionContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function section(): HTMLElement {
  const heading = screen.getByRole('heading', { name: 'Aparcados' })
  const card = heading.closest('.card')
  if (card === null) throw new Error('Sin tarjeta para Aparcados')
  return card as HTMLElement
}

function stub(state: PanelState) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(state)))
}

describe('aparcados en el cuadro de mando', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  beforeEach(() => {
    stub(PANEL_STATE)
  })

  it('pinta una fila por aparcado con su motivo', async () => {
    renderDashboard()

    const card = await screen.findByRole('heading', { name: 'Aparcados' })
    expect(card).toBeInTheDocument()
    const inSection = within(section())
    expect(inSection.getByText('2 jobs esperando')).toBeInTheDocument()
    expect(inSection.getByText('Falta la sesión de Claude')).toBeInTheDocument()
    expect(inSection.getByText('Cupo agotado')).toBeInTheDocument()
  })

  it('el que espera la sesión enlaza a /claude, que es lo que lo resuelve', async () => {
    renderDashboard()
    await screen.findByRole('heading', { name: 'Aparcados' })

    // El enlace es la acción real: sin token, el worker no ejecuta nada (030, K6).
    const action = within(section()).getByRole('link', { name: 'Escribir el token' })
    expect(action).toHaveAttribute('href', '/claude')
  })

  it('el cupo agotado no ofrece botón: no se puede saltar desde una pantalla', async () => {
    renderDashboard()
    await screen.findByRole('heading', { name: 'Aparcados' })

    const card = section()
    // Solo el de `session` lleva acción; el de `quota` se explica y se queda ahí.
    expect(within(card).getAllByRole('link', { name: /Escribir el token|Ver los diales/ })).toHaveLength(
      1,
    )
    expect(within(card).getByText(/No se reintenta/)).toBeInTheDocument()
  })

  it('sin aparcados la sección se pinta vacía, no desaparece', async () => {
    stub({ ...PANEL_STATE, parked: [] })
    renderDashboard()

    await screen.findByRole('heading', { name: 'Aparcados' })
    expect(within(section()).getByText('Ningún job está esperando al worker.')).toBeInTheDocument()
  })
})
