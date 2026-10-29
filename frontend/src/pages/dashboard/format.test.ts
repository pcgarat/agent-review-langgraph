import { describe, expect, it } from 'vitest'

import { formatElapsed } from './format'

describe('tiempo de ejecución', () => {
  const start = Date.parse('2026-10-16T10:00:00Z')

  it('lee la duración y no un tiempo negativo', () => {
    expect(formatElapsed(null, start)).toBe('—')
    expect(formatElapsed('2026-10-16T10:00:00Z', start)).toBe('0 s')
    expect(formatElapsed('2026-10-16T10:00:00Z', start + 45_000)).toBe('45 s')
    expect(formatElapsed('2026-10-16T10:00:00Z', start + 90_000)).toBe('1 min 30 s')
    expect(formatElapsed('2026-10-16T10:00:00Z', start + 3_700_000)).toBe('1 h 1 min')
    expect(formatElapsed('2026-10-16T10:00:00Z', start + 25 * 3_600_000)).toBe('1 d 1 h')
    expect(formatElapsed('2026-10-16T10:00:00Z', start - 5_000)).toBe('0 s')
  })
})
