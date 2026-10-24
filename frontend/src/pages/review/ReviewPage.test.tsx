import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ReviewDetail } from '@/api/types/review'
import type { PanelState } from '@/api/types/state'
import { ToastProvider } from '@/components/ToastProvider'
import { SessionContext, type SessionContextValue } from '@/session/session'
import { PANEL_STATE } from '@/pages/dashboard/fixture'
import { ReviewPage } from '@/pages/ReviewPage'

import { CLAUDE_QUIET, CLAUDE_REVIEW, DISCARD, REVIEW } from './fixture'

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

/** `EventSource` de mentira: guarda las instancias para poder empujarles eventos. */
class FakeEventSource {
  static instances: FakeEventSource[] = []

  readonly url: string
  readyState = 0
  onerror: (() => void) | null = null
  readonly listeners = new Map<string, ((event: MessageEvent<string>) => void)[]>()

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(name: string, handler: (event: MessageEvent<string>) => void): void {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), handler])
  }

  close(): void {
    this.readyState = 2
  }

  emit(name: string, data: unknown): void {
    this.readyState = 1
    const payload = typeof data === 'string' ? data : JSON.stringify(data)
    for (const handler of this.listeners.get(name) ?? []) {
      handler({ data: payload } as MessageEvent<string>)
    }
  }

  /** El navegador se rinde: `CLOSED`, sin evento de cierre del servidor. */
  fail(): void {
    this.readyState = 2
    this.onerror?.()
  }
}

function renderReview(session: SessionContextValue = OPERADOR) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SessionContext.Provider value={session}>
          <ToastProvider>
            <ReviewPage reviewKey={REVIEW.review_key} />
          </ToastProvider>
        </SessionContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** `GET` de la ficha (y del estado, para el descarte) y `POST` de comandos. */
function stubFetch(detail: ReviewDetail, state: PanelState = PANEL_STATE) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST') {
      return Promise.resolve(jsonResponse({ review_key: detail.review_key, effect: 'decided', actor: 'ana' }))
    }
    if (url === '/api/state') return Promise.resolve(jsonResponse(state))
    return Promise.resolve(jsonResponse(detail))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** El SSE de la ficha, si se abrió: `/api/reviews/<clave>/console`, `/reasoning`… */
function stream(suffix: string): FakeEventSource | undefined {
  return FakeEventSource.instances.find((source) => source.url.endsWith(suffix))
}

describe('ficha de review', () => {
  beforeEach(() => {
    FakeEventSource.instances = []
    vi.stubGlobal('EventSource', FakeEventSource)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('pinta el recorrido con la duración derivada de cada nodo', async () => {
    stubFetch(REVIEW)
    renderReview()

    expect(await screen.findByRole('heading', { name: 'Recorrido' })).toBeInTheDocument()
    // Los sellos son de cierre: el primer nodo no tiene marca de entrada y queda
    // sin duración; `persona` cierra 2 s después de `scope` y `merge` 1 min 5 s
    // después de `persona`.
    expect(screen.getByTestId('duration-0')).toHaveTextContent('—')
    expect(screen.getByTestId('duration-1')).toHaveTextContent('2 s')
    expect(screen.getByTestId('duration-2')).toHaveTextContent('1 min 5 s')
    expect(screen.getByText('1 min 11 s en total')).toBeInTheDocument()

    // Punto de parada: el siguiente nodo y el tipo de pausa.
    expect(screen.getByText('hitl')).toBeInTheDocument()
    expect(screen.getByText('espera una persona')).toBeInTheDocument()
  })

  it('con el motor claude pinta los pasos traducidos en vez del recorrido', async () => {
    stubFetch(CLAUDE_REVIEW)
    renderReview()

    expect(await screen.findByRole('heading', { name: 'Pasos del CLI' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Recorrido' })).not.toBeInTheDocument()
    expect(screen.getByText('Preparar el diff')).toBeInTheDocument()
    expect(screen.getByText('Leer los hallazgos de Claude')).toBeInTheDocument()
  })

  it('el log es texto plano: las notas y el journal, sin HTML interpretado', async () => {
    stubFetch({
      ...REVIEW,
      notes: ['Fuente estática phpstan: missing.'],
      journal: ['<b>no es negrita</b>'],
    })
    renderReview()

    const log = await screen.findByTestId('review-log')
    expect(log.textContent).toContain('Fuente estática phpstan: missing.')
    // El marcado del journal se pinta tal cual: ni nodos hijos ni negrita.
    expect(log.textContent).toContain('<b>no es negrita</b>')
    expect(log.children).toHaveLength(0)
    expect(within(log).queryByText('no es negrita')).not.toBeInTheDocument()
  })

  it('el formulario de decisión manda `items` con los campos del contrato', async () => {
    const fetchMock = stubFetch(REVIEW)
    renderReview()
    await screen.findByRole('heading', { name: 'Decisión' })

    const user = userEvent.setup()
    // Cada pendiente con su control: el primero se deja en «Publicar» por defecto.
    const second = screen.getByLabelText('Resultado de Nombre poco claro')
    await user.selectOptions(second, 'dropped')
    await user.type(screen.getByLabelText('Nota de Falta un null check'), 'lo arreglo yo')
    await user.click(screen.getByRole('button', { name: 'Decidir' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
      expect(call).toBeDefined()
      expect(call?.[0]).toBe(
        '/api/reviews/cqm/emotivoo-v2%3A42%3Aabc123%3Apanel/decision',
      )
      expect(JSON.parse(String(call?.[1]?.body))).toEqual({
        items: [
          { finding_id: 'f1', outcome: 'approved', note: 'lo arreglo yo' },
          { finding_id: 'f2', outcome: 'dropped', note: '' },
        ],
      })
    })
  })

  it('un descarte sin recorrido pinta el log y no rompe la ficha', async () => {
    const state: PanelState = {
      ...PANEL_STATE,
      dead: [
        ...PANEL_STATE.dead,
        {
          review_key: DISCARD.review_key,
          attempts: 2,
          reason: 'proveedor caído',
          discarded_at: '2026-10-24T09:00:00+00:00',
          error: 'HTTP 502',
        },
      ],
    }
    stubFetch(DISCARD, state)
    renderReview()

    expect(await screen.findByRole('heading', { name: 'Descarte' })).toBeInTheDocument()
    expect(screen.getByText(/intentos/)).toHaveTextContent('intentos 2')
    expect(screen.getByTestId('review-log')).toHaveTextContent('worker: intento 2 falló con HTTP 502')
    expect(screen.queryByRole('heading', { name: 'Recorrido' })).not.toBeInTheDocument()
    // Sin traza no hay formulario de decisión ni recorrido que cancelar.
    expect(screen.queryByRole('heading', { name: 'Decisión' })).not.toBeInTheDocument()
  })

  it('no abre ningún SSE cuando el contrato dice que no hay canales', async () => {
    stubFetch(REVIEW)
    renderReview()

    await screen.findByRole('heading', { name: 'Recorrido' })
    expect(screen.queryByRole('heading', { name: 'Consola en vivo' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Razonamiento' })).not.toBeInTheDocument()
    expect(FakeEventSource.instances).toHaveLength(0)
  })

  it('abre la consola si `console_available` y pinta sus líneas', async () => {
    stubFetch({ ...REVIEW, console_available: true })
    renderReview()

    await screen.findByRole('heading', { name: 'Consola en vivo' })
    const console_ = stream('/console')
    expect(console_).toBeDefined()
    expect(console_?.url).toBe(
      '/api/reviews/cqm/emotivoo-v2%3A42%3Aabc123%3Apanel/console',
    )

    console_?.emit('line', { seq: 1, stream: 'stdout', text: 'npm test' })
    expect(await screen.findByTestId('review-console')).toHaveTextContent('npm test')

    // El servidor cierra al terminar la verificación: no es un fallo.
    console_?.emit('end', {})
    expect(await screen.findByText('Transmisión terminada')).toBeInTheDocument()
    expect(console_?.readyState).toBe(2)
  })

  it('un `404` del SSE se cuenta en pantalla sin romper la ficha', async () => {
    stubFetch({ ...REVIEW, console_available: true })
    renderReview()

    await screen.findByRole('heading', { name: 'Consola en vivo' })
    const console_ = stream('/console')
    // El runner perdió el buffer: el `EventSource` se rinde sin recibir nada.
    console_?.fail()

    expect(
      await screen.findByText('No hay consola disponible para esta review.'),
    ).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Recorrido' })).toBeInTheDocument()
  })

  it('un corte con datos se lee como conexión cerrada, no como error', async () => {
    stubFetch({ ...REVIEW, console_available: true })
    renderReview()

    await screen.findByRole('heading', { name: 'Consola en vivo' })
    const console_ = stream('/console')
    console_?.emit('line', { seq: 1, stream: 'stdout', text: 'npm test' })
    await screen.findByText('npm test')

    console_?.fail()
    expect(await screen.findByText('Se cerró la conexión con la consola.')).toBeInTheDocument()
  })

  it('un stream terminado se cierra sin pintar error', async () => {
    stubFetch({ ...REVIEW, reasoning_available: true })
    renderReview()

    await screen.findByRole('heading', { name: 'Razonamiento' })
    const reasoning = stream('/reasoning')
    expect(reasoning).toBeDefined()
    reasoning?.emit('start', { kind: 'start', role: 'persona', batch: 1, total: 2, meta: { files: ['a.py'] } })
    reasoning?.emit('reasoning', { kind: 'reasoning', role: 'persona', batch: 1, total: 2, text: 'pienso' })
    expect(await screen.findByText('pienso')).toBeInTheDocument()

    reasoning?.emit('fin', {})
    expect(await screen.findByText('Transmisión terminada')).toBeInTheDocument()
    expect(reasoning?.readyState).toBe(2)
  })

  it('la actividad de Claude solo ofrece lo que existe y no abre un SSE vacío', async () => {
    stubFetch(CLAUDE_QUIET)
    renderReview()

    await screen.findByRole('heading', { name: 'Actividad del CLI' })
    expect(screen.queryByRole('link', { name: 'Descargar la conversación' })).not.toBeInTheDocument()
    expect(stream('/claude/events')).toBeUndefined()
  })

  it('con la actividad disponible sigue el SSE y ofrece la descarga', async () => {
    stubFetch(CLAUDE_REVIEW)
    renderReview()

    await screen.findByRole('heading', { name: 'Actividad del CLI' })
    expect(screen.getByRole('link', { name: 'Descargar la conversación' })).toHaveAttribute(
      'href',
      '/api/reviews/cqm/emotivoo-v2%3A42%3Aabc123%3Apanel/claude/download',
    )
    const events = stream('/claude/events')
    expect(events).toBeDefined()
    // El servidor manda `data:` con la línea entera como JSON *string* (`_sse("line",
    // line)`), o sea doble codificación: aquí se prueba esa forma, no el evento ya
    // desenvuelto.
    const cliLine = JSON.stringify({
      type: 'assistant',
      message: { content: [{ type: 'text', text: 'leyendo el diff' }] },
    })
    events?.emit('line', JSON.stringify(cliLine))
    expect(await screen.findByTestId('claude-events')).toHaveTextContent('leyendo el diff')
  })

  it('una línea suelta del CLI se pinta tal cual', async () => {
    stubFetch(CLAUDE_REVIEW)
    renderReview()

    await screen.findByRole('heading', { name: 'Actividad del CLI' })
    const events = stream('/claude/events')
    events?.emit('line', 'texto suelto sin JSON')
    expect(await screen.findByTestId('claude-events')).toHaveTextContent('texto suelto sin JSON')
  })

  it('un lector no ve el botón de decidir', async () => {
    stubFetch(REVIEW)
    renderReview({ ...OPERADOR, isOperador: false, session: { status: 'authenticated', user: { user: 'leo', role: 'lector' }, error: null } })

    await screen.findByRole('heading', { name: 'Decisión' })
    expect(screen.queryByRole('button', { name: 'Decidir' })).not.toBeInTheDocument()
    expect(screen.getByText('Solo el rol operador decide.')).toBeInTheDocument()
  })

  it('cierra el `EventSource` al desmontar la ficha', async () => {
    stubFetch({ ...REVIEW, console_available: true, reasoning_available: true })
    const { unmount } = renderReview()

    await screen.findByRole('heading', { name: 'Consola en vivo' })
    const sources = [...FakeEventSource.instances]
    expect(sources.length).toBeGreaterThan(0)
    // `CLOSED` es el único estado que suelta la conexión: un panel que se deja
    // abierto días no puede quedarse con streams de pantallas que ya no existen.
    expect(sources.every((source) => source.readyState !== 2)).toBe(true)

    unmount()
    expect(sources.every((source) => source.readyState === 2)).toBe(true)
  })
})
