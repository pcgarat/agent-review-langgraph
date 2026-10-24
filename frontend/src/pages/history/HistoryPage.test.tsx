import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ToastProvider } from '@/components/ToastProvider'
import { HistoryPage } from '@/pages/HistoryPage'

import { HISTORY, manyReviews } from './fixture'
import { PAGE_SIZE } from './params'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** La URL que ve el router: los filtros viven ahí, no en el estado del componente. */
function LocationProbe() {
  const location = useLocation()
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>
}

function renderHistory(initial = '/history') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initial]}>
        <ToastProvider>
          <HistoryPage />
          <LocationProbe />
        </ToastProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function location(): string {
  return screen.getByTestId('location').textContent ?? ''
}

function requestedUrls(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls.map(([input]) => String(input))
}

describe('histórico', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(HISTORY)))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('pinta los campos reales de `ReviewSummary`', async () => {
    renderHistory()

    expect(await screen.findByRole('link', { name: HISTORY[0]!.review_key })).toBeInTheDocument()
    const row = screen.getByRole('link', { name: HISTORY[0]!.review_key }).closest('tr') as HTMLElement
    expect(within(row).getByText('completa')).toBeInTheDocument()
    expect(within(row).getByText('abc12345')).toBeInTheDocument()
    expect(within(row).getByText('3')).toBeInTheDocument()
    expect(within(row).getByText('(P0: 1, P2: 2)')).toBeInTheDocument()
    // «Revisados / sin revisar» son dos cifras tabulares separadas, no un texto.
    expect(within(row).getByText(/^12$/)).toBeInTheDocument()
    expect(within(row).getByText(/^1$/)).toBeInTheDocument()
    expect(within(row).getByText('12.345')).toBeInTheDocument()
    expect(within(row).getByText('sí')).toBeInTheDocument()

    // Una review sin `finished_at` y sin razonamiento enseña el hueco, no una fecha
    // inventada: dos celdas con guion largo, la fecha y el razonamiento.
    const second = screen.getByRole('link', { name: HISTORY[1]!.review_key }).closest('tr') as HTMLElement
    expect(within(second).getAllByText('—')).toHaveLength(2)
    expect(within(second).getByText('incompleta')).toBeInTheDocument()
  })

  it('los filtros van a la URL y a la petición', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(HISTORY))
    vi.stubGlobal('fetch', fetchMock)
    renderHistory()
    await screen.findByRole('link', { name: HISTORY[0]!.review_key })

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Repo'), 'otro/repo')
    await user.type(screen.getByLabelText('PR'), '43')
    await user.click(screen.getByRole('button', { name: 'Filtrar' }))

    await waitFor(() => expect(location()).toBe('/history?repo=otro%2Frepo&pr_id=43'))
    // El contrato se pide con los mismos filtros y el tope de la API.
    await waitFor(() =>
      expect(requestedUrls(fetchMock)).toContain('/api/history?repo=otro%2Frepo&pr_id=43&limit=500'),
    )
  })

  it('arranca leyendo los filtros de la URL compartida', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(HISTORY))
    vi.stubGlobal('fetch', fetchMock)
    renderHistory('/history?repo=cqm%2Femotivoo-v2&pr_id=42')

    expect(await screen.findByLabelText('Repo')).toHaveValue('cqm/emotivoo-v2')
    expect(screen.getByLabelText('PR')).toHaveValue(42)
    await waitFor(() =>
      expect(requestedUrls(fetchMock)).toContain(
        '/api/history?repo=cqm%2Femotivoo-v2&pr_id=42&limit=500',
      ),
    )
  })

  it('pagina en cliente: contador con el total y la página, y una página por vez', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(manyReviews(60))))
    renderHistory()

    const rows = await screen.findAllByRole('row')
    // 60 filas → 3 páginas de 25; la primera enseña 25 más la cabecera.
    expect(rows).toHaveLength(PAGE_SIZE + 1)
    expect(screen.getByText(/60 en total · página 1 de 3/)).toBeInTheDocument()
    expect(screen.getByText(/filas 1–25/)).toBeInTheDocument()

    const user = userEvent.setup()
    await user.click(screen.getByRole('link', { name: '3' }))

    await waitFor(() => expect(location()).toBe('/history?pagina=3'))
    expect(screen.getByText(/página 3 de 3/)).toBeInTheDocument()
    expect(screen.getByText(/filas 51–60/)).toBeInTheDocument()
    expect(screen.getAllByRole('row')).toHaveLength(11)
  })

  it('una página fuera de rango se acota a la última que existe', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(HISTORY)))
    renderHistory('/history?pagina=9')

    expect(await screen.findByText(/página 1 de 1/)).toBeInTheDocument()
  })

  it('sin filas enseña el hueco como dato, no como fallo', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse([])))
    renderHistory()

    expect(await screen.findByText('Ninguna review coincide con los filtros.')).toBeInTheDocument()
    expect(screen.getByText('Ninguna coincide')).toBeInTheDocument()
  })
})
