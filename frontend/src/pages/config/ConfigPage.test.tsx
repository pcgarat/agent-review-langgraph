import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AutoRefreshProvider } from '@/components/AutoRefreshContext'
import { ToastProvider } from '@/components/ToastProvider'
import { ConfigPage } from '@/pages/ConfigPage'
import { SessionContext, type SessionContextValue } from '@/session/session'

import { CONFIG_DOCUMENT } from './fixture'

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

function renderConfig(session: SessionContextValue = OPERADOR) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SessionContext.Provider value={session}>
          <AutoRefreshProvider>
            <ToastProvider>
              <ConfigPage />
            </ToastProvider>
          </AutoRefreshProvider>
        </SessionContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** `GET /api/config` con el documento dado y `POST /config` con la respuesta dada. */
function stubFetch(
  post: () => Promise<Response> = () => Promise.resolve(jsonResponse({ revision: 4 })),
  get: () => Promise<Response> = () => Promise.resolve(jsonResponse(CONFIG_DOCUMENT)),
) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST' && url === '/config') return post()
    return get()
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function posts(fetchMock: ReturnType<typeof stubFetch>) {
  return fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')
}

function sentBody(fetchMock: ReturnType<typeof stubFetch>): Record<string, unknown> {
  const call = posts(fetchMock)[0]
  if (call === undefined) throw new Error('No se envió ningún POST')
  return JSON.parse(String(call[1]?.body ?? 'null')) as Record<string, unknown>
}

/** El bloque de un dial, con su control, su ayuda y su Restaurar. */
async function dial(name: string): Promise<HTMLElement> {
  return screen.findByTestId(`dial-${name}`)
}

async function restoreButton(name: string): Promise<HTMLElement> {
  return within(await dial(name)).findByRole('button', { name: 'Restaurar' })
}

describe('configuración', () => {
  beforeEach(() => {
    stubFetch()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('agrupa los diales en las cinco secciones, con su ayuda', async () => {
    renderConfig()

    for (const section of [
      'Claude',
      'Presupuesto y lotes',
      'Verificador',
      'Bucle de lectura',
      'Política',
    ]) {
      expect(await screen.findByRole('heading', { name: section })).toBeInTheDocument()
    }

    // Un dial de cada sección, con el texto que da el catálogo.
    expect(within(await dial('claude_max_turns')).getByText(/Turnos del CLI/)).toBeInTheDocument()
    expect(within(await dial('llm_max_findings')).getByText(/Tope de hallazgos/)).toBeInTheDocument()
    expect(
      within(await dial('verifier_max_findings')).getByText(/pasa el verificador/),
    ).toBeInTheDocument()
    expect(
      within(await dial('review_tools_max_turns')).getByText(/Turnos con herramientas/),
    ).toBeInTheDocument()
    expect(within(await dial('scope_ignore')).getByText(/Patrones extra/)).toBeInTheDocument()

    // 25 diales: cada uno con su Restaurar.
    expect(screen.getAllByRole('button', { name: 'Restaurar' })).toHaveLength(
      Object.keys(CONFIG_DOCUMENT.initial).length,
    )
  })

  it('elige el control por el tipo del valor inicial, no por el nombre', async () => {
    renderConfig()

    // `bool` → interruptor.
    const notice = await dial('review_tools_truncation_notice')
    expect(within(notice).getByRole('checkbox')).toBeChecked()
    expect(within(await dial('review_tools_io_cache')).getByRole('checkbox')).not.toBeChecked()
    // Número → caja numérica; decimales donde el rango los admite.
    expect(within(await dial('llm_max_findings')).getByRole('spinbutton')).toHaveAttribute(
      'type',
      'number',
    )
    expect(within(await dial('claude_timeout_s')).getByRole('spinbutton')).toHaveAttribute(
      'step',
      'any',
    )
    expect(within(await dial('claude_max_turns')).getByRole('spinbutton')).toHaveAttribute(
      'max',
      '2000',
    )
    // `Literal` → desplegable con las opciones del catálogo.
    const mode = within(await dial('review_tools_grep_mode')).getByRole('combobox')
    expect(mode).toHaveValue('regex')
    expect(within(mode).getByRole('option', { name: 'literal' })).toBeInTheDocument()
    // Texto → caja de texto.
    expect(within(await dial('verifier_require_verified_for')).getByRole('textbox')).toHaveValue(
      'P0,P1',
    )
  })

  it('Restaurar está deshabilitado con el valor inicial y se habilita al cambiarlo', async () => {
    renderConfig()
    const user = userEvent.setup()

    const input = within(await dial('review_tools_max_turns')).getByRole('spinbutton')
    expect(input).toHaveValue(4)
    expect(await restoreButton('review_tools_max_turns')).toBeDisabled()

    await user.clear(input)
    await user.type(input, '9')
    expect(await restoreButton('review_tools_max_turns')).toBeEnabled()

    await user.click(await restoreButton('review_tools_max_turns'))
    expect(input).toHaveValue(4)
    expect(await restoreButton('review_tools_max_turns')).toBeDisabled()
  })

  it('Restaurar devuelve el override publicado a su valor inicial', async () => {
    renderConfig()
    const user = userEvent.setup()

    const block = await dial('llm_max_findings')
    const input = within(block).getByRole('spinbutton')
    expect(input).toHaveValue(4)
    // Un override publicado es un cambio: su Restaurar está activo.
    expect(within(block).getByRole('button', { name: 'Restaurar' })).toBeEnabled()

    await user.click(within(block).getByRole('button', { name: 'Restaurar' }))
    expect(input).toHaveValue(10)
    expect(within(block).getByRole('button', { name: 'Restaurar' })).toBeDisabled()
  })

  it('Guardar manda el formulario entero y `initial` tal cual llegó', async () => {
    const fetchMock = stubFetch()
    renderConfig()
    const user = userEvent.setup()

    const findings = within(await dial('llm_max_findings')).getByRole('spinbutton')
    await user.clear(findings)
    await user.type(findings, '7')
    await user.click(await screen.findByRole('button', { name: 'Guardar' }))

    await waitFor(() => expect(posts(fetchMock)).toHaveLength(1))
    const body = sentBody(fetchMock)

    expect(posts(fetchMock)[0]?.[0]).toBe('/config')
    expect(body.revision).toBe(3)
    // El diff contra los iniciales lo hace el servidor (`document_for`): se manda
    // el formulario completo, con el dial cambiado en su valor nuevo.
    const values = body.values as Record<string, unknown>
    expect(Object.keys(values)).toHaveLength(Object.keys(CONFIG_DOCUMENT.initial).length - 1)
    expect(values.llm_max_findings).toBe('7')
    expect(values.review_tools_max_turns).toBe('4')
    expect(values.review_tools_truncation_notice).toBe(true)
    // El bloqueado se omite: el navegador no lo enviaría y el servidor lo da por fijado.
    expect(values).not.toHaveProperty('llm_batch_retries')
    // Lo que no se toca conserva lo publicado: `grep_mode` sigue en su override.
    expect(values.scope_ignore).toBe('')
    expect(values.review_tools_grep_mode).toBe('regex')
    // `initial` viaja sin tocar para que el servidor detecte un formulario obsoleto.
    expect(body.initial).toEqual(CONFIG_DOCUMENT.initial)
  })

  it('tras guardar toma la revisión del servidor y deja de haber cambios pendientes', async () => {
    const fetchMock = stubFetch()
    renderConfig()
    const user = userEvent.setup()

    const findings = within(await dial('llm_max_findings')).getByRole('spinbutton')
    await user.clear(findings)
    await user.type(findings, '7')
    await user.click(await screen.findByRole('button', { name: 'Guardar' }))

    await waitFor(() => expect(posts(fetchMock)).toHaveLength(1))
    expect(await screen.findByText('Configuración publicada')).toBeInTheDocument()
    // El valor editado se queda en pantalla y la barra desaparece: ya está publicado.
    expect(within(await dial('llm_max_findings')).getByRole('spinbutton')).toHaveValue(7)
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Guardar' })).not.toBeInTheDocument(),
    )
  })

  it('un 409 de revisión nombra la revisión vigente y ofrece recargar', async () => {
    stubFetch(() =>
      Promise.resolve(
        jsonResponse(
          { detail: { detail: 'la revisión vigente es 5', revision: 5 } },
          409,
        ),
      ),
    )
    renderConfig()
    const user = userEvent.setup()

    const findings = within(await dial('llm_max_findings')).getByRole('spinbutton')
    await user.clear(findings)
    await user.type(findings, '7')
    await user.click(await screen.findByRole('button', { name: 'Guardar' }))

    // El título va en el toast y en la alerta; el botón de recargar solo en la alerta.
    const reload = await screen.findByRole('button', { name: 'Recargar el documento' })
    const alert = reload.closest('.alert') as HTMLElement
    expect(within(alert).getByText('Alguien guardó antes')).toBeInTheDocument()
    expect(within(alert).getByText(/La revisión vigente es la 5/)).toBeInTheDocument()
  })

  it('un 409 `stale` dice que el formulario es de otra versión, no que hubo carrera', async () => {
    stubFetch(() =>
      Promise.resolve(
        jsonResponse(
          {
            detail: {
              detail: 'llm_max_findings: la página tiene valores iniciales de otra versión',
              stale: true,
            },
          },
          409,
        ),
      ),
    )
    renderConfig()
    const user = userEvent.setup()

    const findings = within(await dial('llm_max_findings')).getByRole('spinbutton')
    await user.clear(findings)
    await user.type(findings, '7')
    await user.click(await screen.findByRole('button', { name: 'Guardar' }))

    const reload = await screen.findByRole('button', { name: 'Recargar el documento' })
    const alert = reload.closest('.alert') as HTMLElement
    expect(within(alert).getByText('El formulario es de otra versión')).toBeInTheDocument()
    expect(within(alert).getByText(/valores iniciales distintos/)).toBeInTheDocument()
    // No es una carrera de revisiones: no se nombra ninguna revisión vigente.
    expect(screen.queryByText(/La revisión vigente es la/)).not.toBeInTheDocument()
  })

  it('un 400 nombra el dial que no encaja', async () => {
    stubFetch(() =>
      Promise.resolve(
        jsonResponse(
          { detail: { detail: 'llm_batch_retries: se espera un entero', field: 'llm_batch_retries' } },
          400,
        ),
      ),
    )
    renderConfig()
    const user = userEvent.setup()

    const findings = within(await dial('llm_max_findings')).getByRole('spinbutton')
    await user.clear(findings)
    await user.type(findings, '7')
    await user.click(await screen.findByRole('button', { name: 'Guardar' }))

    // Toast y alerta dicen lo mismo: la alerta es la que se queda en pantalla.
    const alerts = await screen.findAllByText(/llm_batch_retries: se espera un entero/)
    expect(alerts.length).toBeGreaterThan(0)
    expect(screen.getAllByText('Ese dial no encaja').length).toBeGreaterThan(0)
  })

  it('un dial bloqueado se pinta deshabilitado, con su inicial, y lo explica', async () => {
    renderConfig()

    const block = await dial('llm_batch_retries')
    const input = within(block).getByRole('spinbutton')
    // El override publicado para un bloqueado se descarta al montar la review: se
    // enseña el del despliegue, no el 5 del documento.
    expect(input).toHaveValue(2)
    expect(input).toBeDisabled()
    expect(within(block).getByRole('button', { name: 'Restaurar' })).toBeDisabled()
    expect(within(block).getByText(/PANEL_CONFIG_LOCK lo bloquea/)).toBeInTheDocument()
    // Además del aviso de cada dial bloqueado, la cabecera explica el candado.
    expect(screen.getAllByText(/PANEL_CONFIG_LOCK/).length).toBeGreaterThan(1)
  })

  it('enseña las revisiones fijadas agrupadas por revisión', async () => {
    renderConfig()

    // Dos reviews sobre la revisión 2 se cuentan juntas; una sola dice su clave.
    expect(await screen.findByText('en uso por 2 reviews')).toBeInTheDocument()
    expect(screen.getByText(/cqm\/emotivoo-v2:43:ccc333:panel/)).toBeInTheDocument()
    expect(screen.queryByText(/No se pudo leer la foto/)).not.toBeInTheDocument()
  })

  it('con `pins` ausente dice que no se pudo leer, no que no hay ninguna', async () => {
    // `pins` omitido es lo que el servidor manda cuando no pudo leer el checkpointer.
    const withoutPins: Record<string, unknown> = { ...CONFIG_DOCUMENT }
    delete withoutPins.pins
    stubFetch(undefined, () => Promise.resolve(jsonResponse(withoutPins)))
    renderConfig()

    expect(await screen.findByText(/el checkpointer no respondió/)).toBeInTheDocument()
    expect(screen.queryByText(/Ninguna review en curso ha fijado/)).not.toBeInTheDocument()
  })

  it('con `pins` vacío dice que ninguna review ha fijado revisión', async () => {
    stubFetch(undefined, () =>
      Promise.resolve(jsonResponse({ ...CONFIG_DOCUMENT, pins: [] })),
    )
    renderConfig()

    expect(await screen.findByText(/Ninguna review en curso ha fijado/)).toBeInTheDocument()
    expect(screen.queryByText(/el checkpointer no respondió/)).not.toBeInTheDocument()
  })

  it('un lector no ve Guardar ni puede editar', async () => {
    renderConfig(LECTOR)
    await screen.findByRole('heading', { name: 'Claude' })

    expect(screen.queryByRole('button', { name: 'Guardar' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Descartar cambios' })).not.toBeInTheDocument()
    expect(screen.getByText(/guardarlos es del rol operador/)).toBeInTheDocument()
    // El formulario sí se lee, pero no se edita: un borrador sin botón de guardar
    // solo lleva a perder el trabajo.
    const input = within(await dial('llm_max_findings')).getByRole('spinbutton')
    expect(input).toHaveValue(4)
    expect(input).toBeDisabled()
  })
})
