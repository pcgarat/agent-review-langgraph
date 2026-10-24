import { describe, expect, it } from 'vitest'

import { deriveTimeline, formatDuration } from './trace'
import { REVIEW } from './fixture'

describe('duraciones del recorrido', () => {
  it('mide cada nodo desde el cierre del anterior hasta el suyo', () => {
    // Los sellos son de cierre: `persona` cierra 2 s después de `scope` y `merge`
    // 1 min 5 s después. El primero no tiene marca de entrada y queda sin duración.
    const timeline = deriveTimeline(REVIEW.trace!)
    expect(timeline.rows.map((row) => row.durationMs)).toEqual([null, 2000, 65000])
    expect(timeline.rows.map((row) => row.source)).toEqual([
      'start-unknown',
      'previous',
      'previous',
    ])
  })

  it('el total va del primer sello a la última actualización del hilo', () => {
    expect(deriveTimeline(REVIEW.trace!).totalMs).toBe(71000)
  })

  it('un sello que falta deja la duración vacía en vez de inventarla', () => {
    const timeline = deriveTimeline({
      ...REVIEW.trace!,
      nodes: [
        { name: 'scope', index: 0, started_at: null, writes: [] },
        { name: 'persona', index: 1, started_at: '2026-10-24T11:00:02+00:00', writes: [] },
      ],
    })
    // El primero no tiene sello: no hay marca de entrada que medir.
    expect(timeline.rows[0]?.durationMs).toBeNull()
    // El segundo tampoco: su nodo anterior no tiene sello del que partir.
    expect(timeline.rows[1]?.durationMs).toBeNull()
  })

  it('un sello que retrocede no produce una duración negativa', () => {
    const timeline = deriveTimeline({
      ...REVIEW.trace!,
      nodes: [
        { name: 'scope', index: 0, started_at: '2026-10-24T11:00:05+00:00', writes: [] },
        { name: 'persona', index: 1, started_at: '2026-10-24T11:00:01+00:00', writes: [] },
      ],
      updated_at: '2026-10-24T11:00:06+00:00',
    })
    expect(timeline.rows[1]?.durationMs).toBeNull()
  })

  it('formatea la duración en unidades legibles', () => {
    expect(formatDuration(null)).toBe('—')
    expect(formatDuration(450)).toBe('450 ms')
    expect(formatDuration(2000)).toBe('2 s')
    expect(formatDuration(65000)).toBe('1 min 5 s')
    expect(formatDuration(120000)).toBe('2 min')
    expect(formatDuration(3600000)).toBe('1 h')
  })
})
