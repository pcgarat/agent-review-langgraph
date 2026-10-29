import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AutoRefreshProvider } from '@/components/AutoRefreshContext'
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

/** `GET /api/state` con el estado dado y `POST` de comandos con su `effect`. */
function stubFetch(effect: string) {
  const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'POST') {
      return Promise.resolve(jsonResponse({ review_key: '', effect, actor: 'ana' }))
    }
    return Promise.resolve(jsonResponse(PANEL_STATE))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderDashboard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SessionContext.Provider value={OPERADOR}>
          <AutoRefreshProvider>
            <ToastProvider>
              <DashboardPage />
            </ToastProvider>
          </AutoRefreshProvider>
        </SessionContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function rowWith(text: string): HTMLElement {
  const cell = screen.getByText(text)
  const row = cell.closest('tr')
  if (row === null) throw new Error(`Sin fila para ${text}`)
  return row as HTMLElement
}

describe('comandos del cuadro de mando', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(PANEL_STATE)))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('cancelar llama a su endpoint y vuelve a pedir el estado', async () => {
    const fetchMock = stubFetch('cancelled')
    renderDashboard()
    await screen.findByRole('heading', { name: 'Cola' })

    const user = userEvent.setup()
    await user.click(within(rowWith(PANEL_STATE.queue[0]!.review_key)).getByRole('button', { name: 'Cancelar' }))

    await waitFor(() => {
      const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')
      expect(posts).toHaveLength(1)
      // La clave se codifica por segmento: los `:` no pueden viajar crudos.
      expect(posts[0]?.[0]).toBe('/api/reviews/cqm/emotivoo-v2%3A41%3Aaaa111%3Aci/cancel')
    })
    expect(await screen.findByText('Review cancelada')).toBeInTheDocument()

    // La invalidación de `AFTER_COMMAND` vuelve a leer el estado: sin ella las
    // cifras seguirían enseñando la review cancelada.
    await waitFor(() => {
      const gets = fetchMock.mock.calls.filter(([, init]) => init?.method !== 'POST')
      expect(gets.length).toBeGreaterThan(1)
    })
  })

  it('olvidar pide confirmación antes de llamar a la API', async () => {
    const fetchMock = stubFetch('forgotten')
    renderDashboard()
    await screen.findByRole('heading', { name: 'Descartes' })

    const user = userEvent.setup()
    const dead = PANEL_STATE.dead[0]!
    await user.click(within(rowWith(dead.review_key)).getByRole('button', { name: 'Olvidar' }))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Olvidar esta review')).toBeInTheDocument()
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0)

    await user.click(within(dialog).getByRole('button', { name: 'Olvidar' }))
    await waitFor(() => {
      const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')
      expect(posts).toHaveLength(1)
      expect(posts[0]?.[0]).toBe('/api/reviews/cqm/emotivoo-v2%3A46%3Afff666%3Aci/forget')
    })
  })
})
