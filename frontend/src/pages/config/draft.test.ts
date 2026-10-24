import { describe, expect, it } from 'vitest'

import { DIALS, OTHER_SECTION, SECTIONS, dialKind, groupDials } from './catalog'
import { changedNames, isPristine, savePayload, seedForm } from './draft'

const INITIAL: Record<string, unknown> = {
  claude_timeout_s: 3600,
  claude_max_turns: 200,
  scope_ignore: '',
  llm_max_findings: 10,
  review_tools_grep_mode: 'literal',
  review_tools_io_cache: false,
}

function document(overrides: Record<string, unknown> = {}, locked: string[] = []) {
  return {
    revision: 2,
    values: overrides,
    initial: INITIAL,
    locked,
    actor: 'ana',
    updated_at: null,
  }
}

describe('catálogo de diales', () => {
  it('el control sale del tipo del valor inicial', () => {
    expect(dialKind('review_tools_io_cache', false)).toBe('bool')
    expect(dialKind('claude_max_turns', 200)).toBe('number')
    expect(dialKind('claude_timeout_s', 3600)).toBe('number')
    expect(dialKind('scope_ignore', '')).toBe('text')
    // Un `Literal` no se distingue de un texto por su valor: lo dice el catálogo.
    expect(dialKind('review_tools_grep_mode', 'literal')).toBe('enum')
  })

  it('un dial que el catálogo no conoce se enseña en «Otros» y no se pierde', () => {
    const groups = groupDials({ ...INITIAL, dial_nuevo: 1 })
    expect(groups.map((group) => group.section)).toEqual([
      'Claude',
      'Presupuesto y lotes',
      'Bucle de lectura',
      'Política',
      OTHER_SECTION,
    ])
    expect(groups.at(-1)?.names).toEqual(['dial_nuevo'])
    // Ninguna sección vacía se pinta.
    expect(groups.some((group) => group.section === 'Verificador')).toBe(false)
  })

  it('las secciones van en el orden del servidor', () => {
    expect(groupDials(INITIAL).map((group) => group.section)).toEqual([
      'Claude',
      'Presupuesto y lotes',
      'Bucle de lectura',
      'Política',
    ])
    expect(SECTIONS[0]).toBe('Claude')
    expect(DIALS['claude_max_turns']?.bounds).toEqual({ min: 1, max: 2000, step: 1 })
  })
})

describe('borrador', () => {
  it('se siembra con los overrides publicados y el inicial para el resto', () => {
    const state = seedForm(document({ llm_max_findings: 4 }))
    expect(state.revision).toBe(2)
    expect(state.values.llm_max_findings).toBe('4')
    expect(state.values.claude_max_turns).toBe('200')
    expect(state.values.review_tools_io_cache).toBe(false)
  })

  it('un dial bloqueado se siembra con su inicial, no con el override publicado', () => {
    const state = seedForm(document({ llm_max_findings: 1 }, ['llm_max_findings']))
    expect(state.values.llm_max_findings).toBe('10')
  })

  it('compara normalizado: `3600` y `3600.0` no son un cambio', () => {
    expect(isPristine('number', '3600.0', 3600)).toBe(true)
    expect(isPristine('number', ' 3600 ', 3600)).toBe(true)
    expect(isPristine('number', '3601', 3600)).toBe(false)
    expect(isPristine('number', '', 0)).toBe(false)
    expect(isPristine('text', ' P0,P1 ', 'P0,P1')).toBe(true)
    expect(isPristine('bool', true, true)).toBe(true)
    expect(isPristine('bool', false, true)).toBe(false)
  })

  it('solo cuenta como cambio lo que se aparta del inicial y no está bloqueado', () => {
    const doc = document({ llm_max_findings: 4, claude_max_turns: 200 }, ['scope_ignore'])
    const state = { revision: 2, values: { ...seedForm(doc).values, scope_ignore: 'vendor/' } }
    expect(changedNames(state, doc)).toEqual(['llm_max_findings'])
  })

  it('el cuerpo lleva el formulario entero menos los bloqueados y el `initial` recibido', () => {
    const doc = document({ llm_max_findings: 4 }, ['scope_ignore'])
    const state = { revision: 7, values: seedForm(doc).values }
    const payload = savePayload(state, doc)

    expect(payload.revision).toBe(7)
    expect(Object.keys(payload.values)).toHaveLength(Object.keys(INITIAL).length - 1)
    expect(payload.values).not.toHaveProperty('scope_ignore')
    expect(payload.values.llm_max_findings).toBe('4')
    expect(payload.values.review_tools_io_cache).toBe(false)
    // Los números viajan como texto: el servidor coerciona y un vacío da 400, no 0.
    expect(payload.values.claude_max_turns).toBe('200')
    expect(payload.initial).toBe(doc.initial)
  })
})
