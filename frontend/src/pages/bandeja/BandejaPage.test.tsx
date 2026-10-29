import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AutoRefreshProvider } from '@/components/AutoRefreshContext'
import { ToastProvider } from '@/components/ToastProvider'
import { SessionContext, type SessionContextValue } from '@/session/session'
import { BandejaPage } from '@/pages/BandejaPage'

import { BANDEJA } from './fixture'

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

function renderBandeja(session: SessionContextValue) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SessionContext.Provider value={session}>
          <AutoRefreshProvider>
            <ToastProvider>
              <BandejaPage />
            </ToastProvider>
          </AutoRefreshProvider>
        </SessionContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** `GET /api/bandeja` con la vista dada y el POST de decisión con la respuesta indicada. */
function stubFetch(post: () => Promise<Response>) {
  const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'POST') return post()
    return Promise.resolve(jsonResponse(BANDEJA))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function posts(fetchMock: ReturnType<typeof stubFetch>) {
  return fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')
}

/** Cuerpo JSON del único POST enviado. */
function sentBody(fetchMock: ReturnType<typeof stubFetch>): unknown {
  const call = posts(fetchMock)[0]
  if (call === undefined) throw new Error('No se envió ningún POST')
  return JSON.parse(String(call[1]?.body ?? 'null'))
}

/** La tarjeta de una pausa por el título de su PR, que es único. */
function cardFor(prId: number): HTMLElement {
  const heading = screen.getByRole('heading', { name: new RegExp(`#${prId}\\b`) })
  const card = heading.closest('.card')
  if (card === null) throw new Error(`Sin tarjeta para el PR ${prId}`)
  return card as HTMLElement
}

describe('bandeja', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(BANDEJA)))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('pinta una tarjeta por pausa, con review, repo y hallazgos', async () => {
    renderBandeja(OPERADOR)

    expect(await screen.findByRole('heading', { name: /Qué se publica · cqm\/emotivoo-v2#42/ })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /Decisión pendiente · cqm\/emotivoo-v2#44/ })).toBeInTheDocument()

    const publication = cardFor(42)
    expect(within(publication).getByText('cqm/emotivoo-v2:42:abc123:panel')).toBeInTheDocument()
    expect(within(publication).getByText('Falta un null check')).toBeInTheDocument()
    expect(within(publication).getByText('Puede reventar en producción.')).toBeInTheDocument()
    expect(within(publication).getByText('src/a.py:10')).toBeInTheDocument()
    // `code_url` es un enlace solo cuando viene: el segundo hallazgo no lo trae.
    expect(within(publication).getByRole('link', { name: 'ver el código' })).toHaveAttribute(
      'href',
      'https://example.test/blob/abc123/src/a.py#L10',
    )
    expect(within(publication).getAllByRole('link', { name: 'ver el código' })).toHaveLength(1)
    // En publicación no se enseña la acción propuesta del motor `llm`.
    expect(within(publication).queryByText(/Acción propuesta/)).not.toBeInTheDocument()
    expect(within(cardFor(44)).getByText(/Acción propuesta: arreglar con aprobación/)).toBeInTheDocument()
  })

  it('responder por hallazgo envía `items` y no `batch`', async () => {
    const fetchMock = stubFetch(() =>
      Promise.resolve(jsonResponse({ review_key: '', effect: 'enqueued', actor: 'ana' })),
    )
    renderBandeja(OPERADOR)
    await screen.findByRole('heading', { name: /#42/ })

    const card = cardFor(42)
    const user = userEvent.setup()
    // El envío está bloqueado hasta que todos los pendientes tienen marca: una
    // decisión parcial publicaría el resto como si nadie los hubiera frenado.
    expect(within(card).getByRole('button', { name: 'Enviar' })).toBeDisabled()
    // Dos hallazgos, dos juegos de marcas: el primero es el de `f1`.
    await user.click(within(card).getAllByRole('button', { name: 'Publicar' })[0]!)
    // Dos botones «No publicar» (uno por hallazgo): el segundo es el de `f2`.
    const drops = within(card).getAllByRole('button', { name: 'No publicar' })
    await user.click(drops[1]!)
    expect(within(card).getByRole('button', { name: 'Enviar' })).toBeEnabled()

    await user.click(within(card).getByRole('button', { name: 'Enviar' }))

    await waitFor(() => expect(posts(fetchMock)).toHaveLength(1))
    expect(posts(fetchMock)[0]?.[0]).toBe(
      '/api/bandeja/cqm/emotivoo-v2%3A42%3Aabc123%3Apanel/decision',
    )
    expect(sentBody(fetchMock)).toEqual({
      items: [
        { finding_id: 'f1', outcome: 'approved' },
        { finding_id: 'f2', outcome: 'dropped' },
      ],
    })
    expect(await screen.findByText('Decisión enviada')).toBeInTheDocument()
  })

  it('responder por lote envía `batch` y no `items`', async () => {
    const fetchMock = stubFetch(() =>
      Promise.resolve(jsonResponse({ review_key: '', effect: 'enqueued', actor: 'ana' })),
    )
    renderBandeja(OPERADOR)
    await screen.findByRole('heading', { name: /#42/ })

    const user = userEvent.setup()
    await user.click(within(cardFor(42)).getByRole('button', { name: 'Solo bloqueantes' }))

    await waitFor(() => expect(posts(fetchMock)).toHaveLength(1))
    // El valor sale de `PublishBatch` en `teams_bot/cards.py`, no de un id inventado.
    expect(sentBody(fetchMock)).toEqual({ batch: 'blockers' })
  })

  it('las pausas que no son de publicación no ofrecen lote', async () => {
    renderBandeja(OPERADOR)
    await screen.findByRole('heading', { name: /#44/ })

    expect(within(cardFor(44)).queryByRole('button', { name: 'Solo bloqueantes' })).not.toBeInTheDocument()
    expect(within(cardFor(44)).queryByRole('button', { name: 'Publicar todos' })).not.toBeInTheDocument()
  })

  it('un 409 se lee como «ya lo decidió otra persona», con el texto del servidor', async () => {
    stubFetch(() =>
      Promise.resolve(
        jsonResponse({ detail: 'La decision ya la tomo marta' }, 409),
      ),
    )
    renderBandeja(OPERADOR)
    await screen.findByRole('heading', { name: /#42/ })

    const user = userEvent.setup()
    const card = cardFor(42)
    await user.click(within(card).getByRole('button', { name: 'Publicar todos' }))

    // El toast también es `role="alert"`: el fallo de la tarjeta se busca dentro.
    const alert = await within(card).findByRole('alert')
    expect(alert).toHaveTextContent('Ya lo decidió otra persona')
    expect(alert).toHaveTextContent('La decision ya la tomo marta')
  })

  it('un lector no ve ningún control de respuesta', async () => {
    renderBandeja(LECTOR)
    await screen.findByRole('heading', { name: /#42/ })

    expect(screen.queryByRole('button', { name: 'Enviar' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Publicar' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Publicar todos' })).not.toBeInTheDocument()
    expect(screen.getAllByText('Solo el rol operador responde.')).toHaveLength(2)
  })
})
