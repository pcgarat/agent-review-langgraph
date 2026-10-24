import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { SessionContext, type SessionContextValue } from '@/session/session'
import { ToastProvider } from '@/components/ToastProvider'
import { AppRoutes } from './router'

const KEY = 'cqm/emotivoo-v2:42:abc123:7'

const OPERADOR: SessionContextValue = {
  session: { status: 'authenticated', user: { user: 'ana', role: 'operador' }, error: null },
  isOperador: true,
  refresh: () => {},
  signOut: async () => {},
}

const ANONIMO: SessionContextValue = {
  session: { status: 'anonymous', user: null, error: null },
  isOperador: false,
  refresh: () => {},
  signOut: async () => {},
}

function renderAt(path: string, session: SessionContextValue) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <SessionContext.Provider value={session}>
          <ToastProvider>
            <AppRoutes />
          </ToastProvider>
        </SessionContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('rutas', () => {
  it('la review_key llega entera: lleva barras y dos puntos', () => {
    renderAt(`/review/${KEY}`, OPERADOR)
    // Si el splat no fuese `*`, la clave quedaria cortada en el primer `/` y esta
    // asercion fallaria: es el riesgo R1 de la spec 029.
    expect(screen.getByText(KEY)).toBeInTheDocument()
  })

  it('sin sesión, cualquier ruta cae en el login', () => {
    renderAt('/', ANONIMO)
    expect(screen.getByRole('heading', { name: 'Entrar' })).toBeInTheDocument()
    expect(screen.queryByText('Cuadro de mando')).not.toBeInTheDocument()
  })

  it('con sesión, la raíz es el cuadro de mando', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            generated_at: '2026-10-24T11:00:00+00:00',
            queue: [],
            active: [],
            paused: { human: [], fix: [] },
            dead: [],
            budget: [],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )
    renderAt('/', OPERADOR)
    // El título vive en la cabecera como texto, no como encabezado de documento;
    // el sidebar lo repite como enlace. No debe haber un `h1` por pantalla.
    expect(screen.queryByRole('heading', { name: 'Cuadro de mando' })).not.toBeInTheDocument()
    // La raíz ya no es un aviso de bloque pendiente: pide el estado y pinta el
    // cuadro de mando.
    expect(await screen.findByTestId('figure-queue')).toBeInTheDocument()
    expect(screen.queryByText('Pendiente · bloque D')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Cuadro de mando' })).toBeInTheDocument()
    vi.unstubAllGlobals()
  })
})
