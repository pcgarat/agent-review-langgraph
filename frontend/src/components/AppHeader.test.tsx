import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AppHeader } from '@/components/AppHeader'
import { AutoRefreshProvider } from '@/components/AutoRefreshContext'
import { PANEL_STATE } from '@/pages/dashboard/fixture'
import { SessionContext, type SessionContextValue } from '@/session/session'

const OPERADOR: SessionContextValue = {
  session: { status: 'authenticated', user: { user: 'ana', role: 'operador' }, error: null },
  isOperador: true,
  refresh: () => {},
  signOut: async () => {},
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function renderHeader(title: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SessionContext.Provider value={OPERADOR}>
          <AutoRefreshProvider>
            <AppHeader title={title} onToggleSidebar={() => {}} />
          </AutoRefreshProvider>
        </SessionContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('cabecera del armazón', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('los contadores salen del estado y llevan a la pantalla que los resuelve', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(PANEL_STATE)))
    renderHeader('Cuadro de mando')
    // Solo `paused.human` espera a una persona; la cola suma los aparcados, como
    // la cifra del cuadro de mando.
    const bandeja = await screen.findByRole('link', { name: 'Bandeja: 1 pausa esperando' })
    expect(bandeja).toHaveAttribute('href', '/bandeja')
    const cola = screen.getByRole('link', { name: 'Cola: 4 reviews esperando' })
    expect(cola).toHaveAttribute('href', '/')
  })

  it('el título de la pantalla va en las migas y no como encabezado', () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(PANEL_STATE)))
    renderHeader('Histórico')
    expect(screen.getByRole('link', { name: 'Inicio' })).toHaveAttribute('href', '/')
    expect(screen.getByText('Histórico')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Histórico' })).not.toBeInTheDocument()
  })
})
