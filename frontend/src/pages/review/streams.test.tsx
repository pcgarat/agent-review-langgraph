/**
 * La regla de reinicio de los paneles en vivo.
 *
 * El servidor repite su buffer en cada conexión. Sin reiniciar el estado, el
 * panel suma el replay a lo que ya tenía y cada línea sale dos veces. Se prueba
 * la regla del hook de forma aislada: el montaje de la ficha se prueba en
 * `ReviewPage.test.tsx`.
 */

import { act, render } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useServerStream } from './streams'

class FakeEventSource {
  static instances: FakeEventSource[] = []

  readonly url: string
  readyState = 0
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  readonly listeners = new Map<string, ((event: MessageEvent<string>) => void)[]>()

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(name: string, handler: (event: MessageEvent<string>) => void): void {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), handler])
  }

  open(): void {
    this.readyState = 1
    this.onopen?.()
  }

  /** El navegador reconecta: `CONNECTING`, sin cerrar el `EventSource`. */
  reconnect(): void {
    this.readyState = 0
    this.onerror?.()
  }

  close(): void {
    this.readyState = 2
  }
}

function mount(resetOnOpen: false | 'clean' | 'always') {
  const onReset = vi.fn()
  function Probe() {
    useServerStream('/sse', {
      events: ['line'],
      endEvent: 'end',
      onEvent: () => {},
      resetOnOpen,
      onReset,
    })
    return null
  }
  render(<Probe />)
  const source = FakeEventSource.instances[0]
  if (source === undefined) throw new Error('el hook no abrió el stream')
  return { onReset, source }
}

describe('reinicio al abrir la conexión', () => {
  beforeEach(() => {
    FakeEventSource.instances = []
    vi.stubGlobal('EventSource', FakeEventSource)
  })

  it('sin la opción no reinicia nada', () => {
    const { onReset, source } = mount(false)
    act(() => source.open())
    expect(onReset).not.toHaveBeenCalled()
  })

  it(`'clean' reinicia en una apertura limpia`, () => {
    const { onReset, source } = mount('clean')
    act(() => source.open())
    expect(onReset).toHaveBeenCalledTimes(1)
  })

  it(`'clean' NO reinicia tras un corte que el navegador reanuda`, () => {
    // Tras un corte, `EventSource` reconecta mandando `Last-Event-ID`: el
    // servidor solo emite lo nuevo y lo pintado sigue siendo válido.
    const { onReset, source } = mount('clean')
    act(() => source.open())
    act(() => source.reconnect())
    act(() => source.open())
    expect(onReset).toHaveBeenCalledTimes(1)
  })

  it(`'always' reinicia también en la reconexión del navegador`, () => {
    // El proxy de la consola no emite `id:`, así que toda conexión repite el
    // buffer entero, incluida la que el navegador reabre tras un fallo.
    const { onReset, source } = mount('always')
    act(() => source.open())
    act(() => source.reconnect())
    act(() => source.open())
    expect(onReset).toHaveBeenCalledTimes(2)
  })
})
