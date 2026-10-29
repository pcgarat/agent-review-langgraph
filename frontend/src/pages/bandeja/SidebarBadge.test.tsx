import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SidebarNav } from '@/components/SidebarNav'
import { PANEL_STATE } from '@/pages/dashboard/fixture'
import { SessionContext, type SessionContextValue } from '@/session/session'

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

function renderSidebar() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SessionContext.Provider value={OPERADOR}>
          <SidebarNav />
        </SessionContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('aviso de pendientes del sidebar', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('enseña el número de pausas humanas junto a «Bandeja»', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(PANEL_STATE)))
    renderSidebar()

    // `PANEL_STATE` tiene una pausa humana y una de fix: la de fix no espera a
    // una persona y no debe contar.
    const badge = await screen.findByTestId('sidebar-bandeja-badge')
    expect(badge).toHaveTextContent('1')
    expect(badge).toHaveAccessibleName('1 pausa esperando a una persona')
  })

  it('sin pausas no hay badge', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ ...PANEL_STATE, paused: { human: [], fix: [] } }),
    )
    vi.stubGlobal('fetch', fetchMock)
    renderSidebar()

    // Se espera a que la consulta resuelva: si el badge apareciera, estaría aquí.
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    expect(await screen.findByRole('link', { name: 'Bandeja' })).toBeInTheDocument()
    expect(screen.queryByTestId('sidebar-bandeja-badge')).not.toBeInTheDocument()
  })

  it('si la consulta falla, el sidebar sigue navegable y sin badge', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Sin Redis')))
    renderSidebar()

    expect(screen.getByRole('link', { name: 'Cuadro de mando' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Bandeja' })).toBeInTheDocument()
    expect(screen.queryByTestId('sidebar-bandeja-badge')).not.toBeInTheDocument()
  })
})
