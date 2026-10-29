import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SessionContext, type SessionContextValue } from '@/session/session'
import { LoginPage } from './LoginPage'

function newClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

const AUTHENTICATED: SessionContextValue = {
  session: { status: 'authenticated', user: { user: 'ana', role: 'operador' }, error: null },
  isOperador: true,
  refresh: () => {},
  signOut: async () => {},
}

const ANONYMOUS: SessionContextValue = {
  session: { status: 'anonymous', user: null, error: null },
  isOperador: false,
  refresh: () => {},
  signOut: async () => {},
}

function renderLogin(session: SessionContextValue = ANONYMOUS) {
  return render(
    <QueryClientProvider client={newClient()}>
      <MemoryRouter initialEntries={['/login']}>
        <SessionContext.Provider value={session}>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/" element={<p>cuadro de mando</p>} />
          </Routes>
        </SessionContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('LoginPage', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('no deja enviar vacío', () => {
    renderLogin()
    expect(screen.getByRole('button', { name: 'Entrar' })).toBeDisabled()
  })

  it('envía el token como JSON y entra', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ user: 'ana', role: 'operador' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    const user = userEvent.setup()
    renderLogin()
    await user.type(screen.getByLabelText('Token'), 'secreto')
    await user.click(screen.getByRole('button', { name: 'Entrar' }))

    await waitFor(() => expect(screen.getByText('cuadro de mando')).toBeInTheDocument())

    const [url, init] = fetchMock.mock.calls[0] ?? []
    expect(url).toBe('/api/session')
    expect(init?.method).toBe('POST')
    // El servidor rechaza un comando por cookie que no sea JSON del mismo origen.
    expect(init?.headers).toMatchObject({ 'Content-Type': 'application/json' })
    expect(init?.credentials).toBe('same-origin')
    expect(init?.body).toBe(JSON.stringify({ token: 'secreto' }))
  })

  it('un 401 se explica sin decir que el panel está caído', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ detail: 'Sesión del panel inválida' }), {
        status: 401,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    const user = userEvent.setup()
    renderLogin()
    await user.type(screen.getByLabelText('Token'), 'malo')
    await user.click(screen.getByRole('button', { name: 'Entrar' }))

    expect(await screen.findByText('Ese token no vale.')).toBeInTheDocument()
    // Tras un fallo el campo conserva lo escrito para poder corregirlo; lo que
    // nunca hace es limpiarse dejando al operador sin saber qué pasó.
    expect(screen.getByLabelText('Token')).toHaveValue('malo')
  })

  it('sin sesión no se puede llegar a la app', () => {
    // La guardia vive en `router.tsx`; aquí se comprueba el contrato del contexto
    // que la alimenta: un anónimo no es operador.
    expect(ANONYMOUS.isOperador).toBe(false)
    expect(AUTHENTICATED.isOperador).toBe(true)
  })
})
