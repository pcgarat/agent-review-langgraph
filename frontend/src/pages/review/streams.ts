/**
 * Canales SSE de la ficha (029, F5–F7).
 *
 * `EventSource` no acepta cabeceras, pero la cookie `HttpOnly` de la sesión viaja
 * sola en las peticiones del mismo origen, así que el canal se autoriza igual que
 * el resto de la API. El `EventSource` se cierra **siempre** al desmontar: el panel
 * se queda abierto días y una pestaña que no cierra sus streams deja el proceso
 * `web` esperando en un generador que ya nadie lee.
 */

import { useEffect, useRef, useState } from 'react'

/**
 * - `idle`: no hay canal que abrir.
 * - `connecting`: abierto y sin datos todavía.
 * - `live`: llegó al menos un evento.
 * - `done`: el servidor terminó el stream (evento de cierre).
 * - `closed`: se cerró sin evento de cierre y con datos: cortaron la conexión.
 * - `unavailable`: se cerró sin un solo evento; es el `404` del contrato (la
 *   consola del runner o el razonamiento ya no están) o un stream vacío.
 * - `error`: fallo transitorio mientras el navegador aún reintenta.
 */
export type StreamStatus =
  | 'idle'
  | 'connecting'
  | 'live'
  | 'done'
  | 'closed'
  | 'unavailable'
  | 'error'

/** `EventSource.CLOSED`; se compara con el número para no depender del global en tests. */
const EVENT_SOURCE_CLOSED = 2

export interface StreamOptions {
  /** Nombres de evento que el hook escucha y entrega a `onEvent`. */
  events: readonly string[]
  /** Evento con el que el servidor cierra el stream correctamente. */
  endEvent: string
  onEvent: (name: string, data: string) => void
}

interface Progress {
  url: string | null
  status: StreamStatus
}

function initial(url: string | null): StreamStatus {
  return url === null ? 'idle' : 'connecting'
}

export function useServerStream(url: string | null, options: StreamOptions): StreamStatus {
  // El estado lleva la URL a la que pertenece: al cambiar de canal el valor viejo
  // no se arrastra (y no hace falta un `setState` síncrono en el efecto, que
  // volvería a abrir el stream en cada render).
  const [progress, setProgress] = useState<Progress>({ url, status: initial(url) })
  const status = progress.url === url ? progress.status : initial(url)

  // Los manejadores se guardan en un `ref`: si fueran dependencia del efecto, cada
  // render reabriría el stream y perdería lo ya recibido.
  const optionsRef = useRef(options)
  useEffect(() => {
    optionsRef.current = options
  }, [options])

  useEffect(() => {
    if (url === null) return
    let received = false
    let ended = false
    const source = new EventSource(url)
    const update = (next: StreamStatus) => setProgress({ url, status: next })

    const listen = (name: string, handler: (data: string) => void) => {
      source.addEventListener(name, (event) => {
        const data = (event as MessageEvent<string>).data
        received = true
        update('live')
        handler(typeof data === 'string' ? data : '')
      })
    }

    for (const name of optionsRef.current.events) {
      listen(name, (data) => optionsRef.current.onEvent(name, data))
    }
    listen(optionsRef.current.endEvent, () => {
      ended = true
      update('done')
      source.close()
    })

    // `onerror` también salta cuando el servidor cierra el stream al terminar. Con
    // el `readyState` en `CLOSED` no es un fallo: llegó el cierre, o el canal no
    // existe (el `404` que el contrato documenta para consola y razonamiento).
    source.onerror = () => {
      if (ended) return
      if (source.readyState !== EVENT_SOURCE_CLOSED) {
        update('error')
        return
      }
      source.close()
      update(received ? 'closed' : 'unavailable')
    }

    return () => {
      source.close()
    }
  }, [url])

  return status
}

/** Sujeto del canal, con su artículo: «la consola», «el razonamiento». */
export interface StreamSubject {
  article: 'el' | 'la'
  noun: string
}

export const CONSOLE_SUBJECT: StreamSubject = { article: 'la', noun: 'consola' }
export const REASONING_SUBJECT: StreamSubject = { article: 'el', noun: 'razonamiento' }
export const CLAUDE_SUBJECT: StreamSubject = { article: 'la', noun: 'conversación' }

/** Texto del estado del stream, con el sujeto que corresponda. */
export function streamStatusText(status: StreamStatus, subject: StreamSubject): string {
  switch (status) {
    case 'idle':
      return ''
    case 'connecting':
      return `Conectando con ${subject.article} ${subject.noun}…`
    case 'live':
      return 'En vivo'
    case 'done':
      return 'Transmisión terminada'
    case 'closed':
      return `Se cerró la conexión con ${subject.article} ${subject.noun}.`
    case 'unavailable':
      return `No hay ${subject.noun} disponible para esta review.`
    case 'error':
      return `No se pudo seguir ${subject.article} ${subject.noun}.`
  }
}

/** JSON que no revienta: el canal es de terceros y una línea rara no rompe la ficha. */
export function parseJson(data: string): unknown {
  try {
    return JSON.parse(data)
  } catch {
    return null
  }
}
