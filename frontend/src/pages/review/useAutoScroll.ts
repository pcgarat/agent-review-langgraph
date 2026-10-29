/**
 * Autoscroll de un panel que crece (029, ampliado).
 *
 * Tres paneles de la ficha reciben líneas en vivo —consola, razonamiento y
 * actividad del CLI— y los tres necesitan lo mismo: quedarse pegados al final
 * mientras llega contenido, y **dejar de hacerlo** en cuanto la persona sube a
 * leer algo. Bajar a mano otra vez vuelve a pegarlos.
 *
 * El hook no decide por su cuenta: el check es de la persona. Pero sí evita el
 * caso que molesta —un log que se mueve solo mientras intentas leer una línea de
 * arriba— porque el scroll automático se apaga en cuanto hay una separación del
 * fondo mayor que un par de líneas.
 *
 * Devuelve las piezas sueltas y no un objeto: React no deja leer durante el
 * render un objeto que contiene una `ref`, y quien consume esto tiene que pasar
 * `scrollRef` y `onScroll` por separado.
 */

import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'

/** Margen de tolerancia: un scroll que no llega al píxel no es «estoy abajo». */
const BOTTOM_SLACK_PX = 24

export interface AutoScrollControls {
  /** El check: si está marcado, el panel salta al final con cada línea nueva. */
  enabled: boolean
  setEnabled: (value: boolean) => void
}

export interface AutoScroll<T extends HTMLElement> extends AutoScrollControls {
  /** Va en el elemento con scroll: un `<pre>` o el contenedor que lo envuelve. */
  scrollRef: RefObject<T | null>
  /** `onScroll` de ese elemento: desactiva el check cuando la persona sube. */
  onScroll: () => void
}

/**
 * @param dependency cambia cuando llega contenido nuevo; es la señal para bajar.
 */
export function useAutoScroll<T extends HTMLElement = HTMLElement>(
  dependency: number | string,
): AutoScroll<T> {
  const [enabled, setEnabledState] = useState(true)
  const scrollRef = useRef<T | null>(null)
  // El scroll lo provoca el propio efecto: distinguirlo del de la persona evita
  // que el autoscroll se apague solo al bajar.
  const autoScrolling = useRef(false)

  useEffect(() => {
    if (!enabled) return
    const element = scrollRef.current
    if (element === null) return
    autoScrolling.current = true
    element.scrollTop = element.scrollHeight
    // El `scroll` que dispara esta asignación llega en el siguiente tick.
    const id = requestAnimationFrame(() => {
      autoScrolling.current = false
    })
    return () => cancelAnimationFrame(id)
  }, [dependency, enabled])

  const onScroll = useCallback(() => {
    if (autoScrolling.current) return
    const element = scrollRef.current
    if (element === null) return
    const fromBottom = element.scrollHeight - element.scrollTop - element.clientHeight
    // Subir desactiva el check: seguir bajando mientras alguien lee arriba le
    // quita la línea a media lectura.
    if (fromBottom > BOTTOM_SLACK_PX) setEnabledState(false)
  }, [])

  const setEnabled = useCallback((value: boolean) => {
    setEnabledState(value)
    // Marcar el check baja de una vez: no hay que esperar a la línea siguiente.
    if (value) {
      const element = scrollRef.current
      if (element !== null) element.scrollTop = element.scrollHeight
    }
  }, [])

  return { enabled, setEnabled, scrollRef, onScroll }
}
