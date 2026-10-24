import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SessionContext, type SessionContextValue } from '@/session/session'
import { ClaudePage } from '@/pages/ClaudePage'

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

function renderPage(session: SessionContextValue = OPERADOR) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <SessionContext.Provider value={session}>
        <ClaudePage />
      </SessionContext.Provider>
    </QueryClientProvider>,
  )
}

function stubSave(response: () => Promise<Response>) {
  const fetchMock = vi.fn(() => response())
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

async function saveToken(token = 'sk-ant-oauth') {
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('Token'), token)
  await user.click(screen.getByRole('button', { name: 'Guardar' }))
  return user
}

describe('sesión de Claude', () => {
  beforeEach(() => {
    stubSave(() => Promise.resolve(jsonResponse({ released: 2 })))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('tiene un solo campo y el botón está deshabilitado mientras está vacío', () => {
    renderPage()

    // `type="password"` no expone el rol `textbox`, así que el único campo se
    // busca por etiqueta y se comprueba que no hay un segundo `input` en la tarjeta.
    const card = screen.getByLabelText('Token').closest('.claude-card') as HTMLElement
    expect(card.querySelectorAll('input')).toHaveLength(1)
    expect(screen.getByLabelText('Token')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Guardar' })).toBeDisabled()
  })

  it('manda el token a `/api/claude/session` como JSON del mismo origen', async () => {
    const fetchMock = stubSave(() => Promise.resolve(jsonResponse({ released: 0 })))
    renderPage()

    await saveToken('sk-ant-oauth')

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/claude/session')
    expect(init.method).toBe('POST')
    // El comando por cookie exige JSON del mismo origen (`require_command_session`).
    expect(init.headers).toMatchObject({ 'Content-Type': 'application/json' })
    expect(init.credentials).toBe('same-origin')
    expect(init.body).toBe(JSON.stringify({ token: 'sk-ant-oauth' }))
  })

  it('limpia el campo tras guardar y dice cuántas reviews vuelven a la cola', async () => {
    stubSave(() => Promise.resolve(jsonResponse({ released: 2 })))
    renderPage()

    await saveToken()

    expect(await screen.findByText('2 reviews paradas vuelven a la cola.')).toBeInTheDocument()
    // El token no sobrevive al guardado: ni en el campo ni en ningún estado.
    expect(screen.getByLabelText('Token')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Guardar' })).toBeDisabled()
  })

  it('con cero reviews dice explícitamente que no había nada parado', async () => {
    stubSave(() => Promise.resolve(jsonResponse({ released: 0 })))
    renderPage()

    await saveToken()

    // Cero no es un fallo: es un dato que el operador tiene que poder leer.
    expect(
      await screen.findByText(/No había ninguna review parada por falta de sesión/),
    ).toBeInTheDocument()
  })

  it('un 403 se explica con el texto del servidor', async () => {
    stubSave(() =>
      Promise.resolve(
        jsonResponse({ detail: 'El rol lector no puede ejecutar comandos' }, 403),
      ),
    )
    renderPage()

    await saveToken()

    expect(
      await screen.findByText('El rol lector no puede ejecutar comandos'),
    ).toBeInTheDocument()
    // Un fallo conserva lo escrito para poder corregirlo; lo que no hace es
    // limpiarlo dejando al operador sin saber qué pasó.
    expect(screen.getByLabelText('Token')).toHaveValue('sk-ant-oauth')
  })

  it('un 400 de cuerpo inválido se explica con el texto del servidor', async () => {
    stubSave(() => Promise.resolve(jsonResponse({ detail: 'Falta el token' }, 400)))
    renderPage()

    await saveToken()

    expect(await screen.findByText('Falta el token')).toBeInTheDocument()
  })

  it('un 503 del fichero de sesión no se convierte en un texto genérico', async () => {
    stubSave(() =>
      Promise.resolve(jsonResponse({ detail: 'No se pudo guardar la sesion' }, 503)),
    )
    renderPage()

    await saveToken()

    expect(await screen.findByText('No se pudo guardar la sesion')).toBeInTheDocument()
  })

  it('un lector no ve el formulario', () => {
    renderPage(LECTOR)

    expect(screen.queryByLabelText('Token')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Guardar' })).not.toBeInTheDocument()
    expect(screen.getByText(/guardar el token del CLI es del rol operador/)).toBeInTheDocument()
  })
})
