/**
 * Borrador del formulario de diales.
 *
 * El estado del servidor (`GET /api/config`) no se toca al editar: el borrador es
 * lo que la persona escribe y solo existe en el navegador hasta que pulsa
 * Guardar. Por eso vive en `useState` y no se deriva con un efecto del último
 * documento leído.
 */

import { ApiError } from '@/api/client'
import type { ConfigDocument } from '@/api/types/config'

import { dialKind, type DialKind } from './catalog'

/** Un control tal como lo edita la persona: texto, o `true`/`false` si es un switch. */
export type DraftValue = string | boolean

export type Draft = Record<string, DraftValue>

export interface FormState {
  /** Revisión sobre la que se edita; se sustituye por la que devuelve el servidor. */
  revision: number
  values: Draft
}

/** Lo que se pinta en un control: el override publicado o, si no hay, el inicial. */
function shownValue(document: ConfigDocument, name: string): unknown {
  // Un dial bloqueado se pinta con su inicial y no con el override publicado:
  // `apply_document` lo descarta antes de montar la review, así que enseñar el
  // override sería enseñar un número que nadie usa. Es lo que hacía `ui.py`.
  if (document.locked.includes(name)) return document.initial[name]
  const override = document.values[name]
  return override === undefined ? document.initial[name] : override
}

export function seedForm(document: ConfigDocument): FormState {
  const values: Draft = {}
  for (const [name, initial] of Object.entries(document.initial)) {
    const kind = dialKind(name, initial)
    const shown = shownValue(document, name)
    if (kind === 'bool') values[name] = shown === true
    else values[name] = shown === undefined || shown === null ? '' : String(shown)
  }
  return { revision: document.revision, values }
}

/**
 * ¿El borrador coincide con el valor inicial?
 *
 * Se compara normalizado, no como texto: `3600` y `3600.0` son el mismo número y
 * el servidor los coerciona al mismo valor. Dejar «Restaurar» activo ahí sería un
 * botón que no cambia nada.
 */
export function isPristine(kind: DialKind, value: DraftValue | undefined, initial: unknown): boolean {
  if (kind === 'bool') return (value === true) === (initial === true)
  const text = typeof value === 'string' ? value.trim() : String(value ?? '')
  if (kind === 'number') {
    if (text === '') return false
    return Number(text) === Number(initial)
  }
  return text === String(initial ?? '')
}

export function changedNames(state: FormState, document: ConfigDocument): string[] {
  return Object.entries(document.initial)
    .filter(([name, initial]) => {
      if (document.locked.includes(name)) return false
      return !isPristine(dialKind(name, initial), state.values[name], initial)
    })
    .map(([name]) => name)
}

export interface SavePayload {
  revision: number
  values: Record<string, unknown>
  /**
   * Los iniciales con los que se pintó el formulario, tal cual llegaron.
   *
   * El servidor los compara con los suyos y responde `409 {stale: true}` si un
   * despliegue cambió un default con la página abierta. Sin ellos, guardar
   * publicaría como override el valor viejo que nadie eligió.
   */
  initial: Record<string, unknown>
}

/**
 * Cuerpo de `POST /config`.
 *
 * Va el formulario **entero** menos los bloqueados, no solo los overrides:
 * `document_for` rechaza con `400 «falta en el formulario»` cualquier dial del
 * catálogo que no llegue, y el diff contra los iniciales lo hace él. Un
 * bloqueado se omite porque el navegador no envía un control `disabled` y el
 * servidor ya lo da por fijado.
 *
 * Los números viajan como texto sin recortar: un entero vacío no es `0`, y un
 * `0` publicado en silencio apagaría el presupuesto. El servidor responde `400`
 * nombrando el campo.
 */
export function savePayload(state: FormState, document: ConfigDocument): SavePayload {
  const values: Record<string, unknown> = {}
  for (const [name, initial] of Object.entries(document.initial)) {
    if (document.locked.includes(name)) continue
    const kind = dialKind(name, initial)
    const value = state.values[name]
    values[name] = kind === 'bool' ? value === true : String(value ?? '').trim()
  }
  return { revision: state.revision, values, initial: document.initial }
}

export interface SaveFailure {
  title: string
  body: string
  /** Recargar el documento es lo que desatasca el conflicto. */
  reload: boolean
}

interface ErrorDetail {
  detail?: unknown
  revision?: unknown
  stale?: unknown
  field?: unknown
}

/**
 * El `detail` de un comando del panel viene anidado dos veces.
 *
 * `HTTPException(409, {"detail": …, "revision": …})` lo serializa FastAPI como
 * `{"detail": {"detail": …, "revision": …}}`, así que `ApiError.detail` es el
 * sobre y el motivo está dentro.
 */
function nestedDetail(error: ApiError): ErrorDetail {
  const outer = error.detail
  if (outer !== null && typeof outer === 'object' && 'detail' in outer) {
    const inner = (outer as ErrorDetail).detail
    if (inner !== null && typeof inner === 'object') return inner as ErrorDetail
    if (typeof inner === 'string') return { detail: inner }
  }
  return {}
}

/**
 * Por qué no se guardó, distinguiendo los dos `409`.
 *
 * `{revision}` es una carrera: otro guardado entró antes y dice cuál manda.
 * `{stale: true}` es otra cosa: el formulario se pintó con iniciales de otra
 * versión del despliegue. Las dos se resuelven recargando, pero no son el mismo
 * suceso y un texto único haría pensar que alguien pisó el cambio.
 */
export function describeSaveFailure(error: unknown): SaveFailure {
  if (error instanceof ApiError) {
    const inner = nestedDetail(error)
    const reason = typeof inner.detail === 'string' ? inner.detail : ''

    if (error.status === 409 && inner.stale === true) {
      return {
        title: 'El formulario es de otra versión',
        body:
          `Se pintó con valores iniciales distintos de los del despliegue actual. ` +
          `Guarda de nuevo tras recargar para no publicar un valor que no elegiste.` +
          (reason === '' ? '' : ` El servidor dice: ${reason}.`),
        reload: true,
      }
    }

    if (error.status === 409 && typeof inner.revision === 'number') {
      return {
        title: 'Alguien guardó antes',
        body:
          `La revisión vigente es la ${inner.revision}; lo que tienes en pantalla se ` +
          `pintó sobre la tuya. Recarga para ver lo publicado y vuelve a aplicar tus cambios.` +
          (reason === '' ? '' : ` El servidor dice: ${reason}.`),
        reload: true,
      }
    }

    if (error.status === 409) {
      return {
        title: 'La revisión cambió',
        body: reason === '' ? 'El documento se movió mientras editabas. Recarga.' : reason,
        reload: true,
      }
    }

    if (error.status === 400) {
      const field = typeof inner.field === 'string' ? inner.field : ''
      const detail = typeof inner.detail === 'string' ? inner.detail : error.detailText
      // El servidor ya prefija el campo (`llm_x: motivo`); prefijarlo otra vez lo
      // repetiría.
      return {
        title: 'Ese dial no encaja',
        body: field === '' || detail.startsWith(field) ? detail : `${field}: ${detail}`,
        reload: false,
      }
    }

    if (error.status === 403) {
      return {
        title: 'Tu rol no puede guardar',
        body: 'Guardar los diales es del rol operador. El servidor es quien lo decide.',
        reload: false,
      }
    }

    if (error.status === 503) {
      return {
        title: 'La configuración no está disponible',
        body: 'El almacén no respondió. No se escribió nada; reintenta en un momento.',
        reload: false,
      }
    }
  }

  const message = error instanceof Error && error.message.trim() !== '' ? error.message : ''
  return {
    title: 'No se pudo guardar',
    body: message === '' ? 'No se pudo hablar con el panel.' : message,
    reload: false,
  }
}
