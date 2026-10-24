import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

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

const LECTOR: SessionContextValue = {
  session: { status: 'authenticated', user: { user: 'leo', role: 'lector' }, error: null },
  isOperador: false,
  refresh: () => {},
  signOut: async () => {},
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function renderDashboard(session: SessionContextValue) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SessionContext.Provider value={session}>
          <ToastProvider>
            <DashboardPage />
          </ToastProvider>
        </SessionContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** Cifra de una tarjeta por su clave: dos cifras iguales no se confunden. */
async function figure(key: string): Promise<HTMLElement> {
  const value = await screen.findByTestId(`figure-${key}`)
  const card = value.closest('.card')
  if (card === null) throw new Error(`Sin tarjeta para ${key}`)
  return card as HTMLElement
}

describe('cuadro de mando', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(PANEL_STATE)))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('pinta las cuatro cifras de «¿qué pasa ahora?» con sus campos', async () => {
    renderDashboard(OPERADOR)

    // La cola tiene 2, uno de ellos un lanzamiento todavía sin SHA.
    const queue = await figure('queue')
    expect(within(queue).getByText('2')).toBeInTheDocument()
    expect(within(queue).getByText('1 sin commit todavía')).toBeInTheDocument()

    const active = await figure('active')
    expect(within(active).getByText('1')).toBeInTheDocument()
    expect(within(active).getByText('Gasto 20.000 de 100.000 tokens')).toBeInTheDocument()

    // Solo `paused.human`: la pausa de fix no espera a una persona.
    const human = await figure('human')
    expect(within(human).getByText('1')).toBeInTheDocument()
    expect(within(human).getByText('2 hallazgos por decidir')).toBeInTheDocument()

    const dead = await figure('dead')
    expect(within(dead).getByText('3')).toBeInTheDocument()
    expect(within(dead).getByText('5 intentos acumulados')).toBeInTheDocument()
  })

  it('separa las pausas humanas de las de fix', async () => {
    renderDashboard(OPERADOR)

    expect(await screen.findByRole('heading', { name: 'Pausas humanas' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Pausas de fix' })).toBeInTheDocument()
    expect(screen.getByText('Verificación de la wave')).toBeInTheDocument()
    // Una pausa de fix no cuenta como «esperando persona».
    expect(screen.queryByText('2 pausas')).not.toBeInTheDocument()
  })

  it('un lector no ve ningún control de comando ni el formulario de lanzar', async () => {
    renderDashboard(LECTOR)

    expect(await screen.findByRole('heading', { name: 'Cola' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Lanzar' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Cancelar' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reintentar' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Olvidar' })).not.toBeInTheDocument()
    expect(screen.getByText(/Tu rol es/)).toBeInTheDocument()
  })

  it('cada review enlaza a su ficha con la clave entera', async () => {
    renderDashboard(OPERADOR)

    // La ruta es contrato: hay enlaces publicados en PRs y en avisos de Teams (P4).
    const links = await screen.findAllByRole('link', { name: 'cqm/emotivoo-v2:41:aaa111:ci' })
    expect(links[0]).toHaveAttribute('href', '/review/cqm/emotivoo-v2:41:aaa111:ci')
  })

  it('el nodo del CLI de Claude se nombra sin inventarse el del grafo', async () => {
    const claude = {
      ...PANEL_STATE,
      active: [
        {
          review_key: 'cqm/emotivoo-v2:8:8d789e:panel',
          nodes: [{ node: 'claude', started_at: '2026-10-24T10:58:00+00:00', branches: 1 }],
        },
      ],
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(claude)))

    renderDashboard(OPERADOR)

    expect(await screen.findByText('CLI de Claude')).toBeInTheDocument()
  })
})
