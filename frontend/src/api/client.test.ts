import { describe, expect, it } from 'vitest'

import { reviewHref, reviewPath } from './client'

// La `review_key` del panel lleva `/` y `:` dentro: `cqm/emotivoo-v2:42:<sha>:<pipeline>`.
// Si el troceado trata el `:` como separador, la ruta apunta a otro recurso.
const KEY = 'cqm/emotivoo-v2:42:abc123:7'

describe('reviewPath', () => {
  it('codifica cada segmento y conserva los separadores de ruta', () => {
    expect(reviewPath(KEY)).toBe('/api/reviews/cqm/emotivoo-v2%3A42%3Aabc123%3A7')
  })

  it('deja intacta una clave sin barras', () => {
    expect(reviewPath('simple:1:a:2')).toBe('/api/reviews/simple%3A1%3Aa%3A2')
  })
})

describe('reviewHref', () => {
  it('no codifica la clave: la ruta del navegador usa splat', () => {
    expect(reviewHref(KEY)).toBe(`/review/${KEY}`)
  })
})
