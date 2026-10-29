import { describe, expect, it } from 'vitest'

import type { ReviewTrace } from '@/api/types/review'

import { REVIEW } from './fixture'
import { graphSteps } from './steps'

function trace(partial: Partial<ReviewTrace>): ReviewTrace {
  return { ...REVIEW.trace!, ...partial }
}

describe('pasos del grafo', () => {
  it('una pausa espera sin marcar el nodo como en curso', () => {
    const hitl = graphSteps(REVIEW.trace!, []).find((step) => step.name === 'hitl')
    expect(hitl).toMatchObject({
      phase: 'paused',
      title: 'Decisión',
      caption: 'Esperando una decisión humana',
    })
    expect(graphSteps(REVIEW.trace!, []).find((step) => step.name === 'scope')).toMatchObject({
      phase: 'done',
      caption: 'Diff filtrado y contexto preparado',
    })
    expect(graphSteps(REVIEW.trace!, []).find((step) => step.name === 'policy')?.phase).toBe(
      'pending',
    )
  })

  it('el nodo en curso lleva lo que está haciendo y las ramas', () => {
    const steps = graphSteps(
      trace({
        state: 'in_progress',
        nodes: [REVIEW.trace!.nodes[0]!],
        next: ['persona'],
        pause_kind: null,
      }),
      [{ node: 'persona', started_at: '2026-10-24T11:00:00Z', branches: 3 }],
    )
    expect(steps.find((step) => step.name === 'persona')).toMatchObject({
      phase: 'running',
      caption: 'Revisando el código con las personas (3 ramas)',
    })
  })

  it('una review terminada marca como no ejecutado lo que no recorrió', () => {
    const merge = graphSteps(
      trace({ state: 'completed', nodes: [REVIEW.trace!.nodes[0]!], next: [], pause_kind: null }),
      [],
    ).find((step) => step.name === 'merge')
    expect(merge).toMatchObject({ phase: 'skipped', caption: 'No se ejecutó' })
  })

  it('un paso terminado dice sus cifras y los descartes (034)', () => {
    const descarte = {
      stage: 'l0' as const,
      id: '1',
      title: 'Las horas se calculan en minutos',
      file: 'frontend/src/pages/dashboard/format.ts',
      reason: 'la cita no aparece en el diff',
    }
    const node = (name: string, index: number, summary: Record<string, number>) => ({
      name,
      index,
      started_at: null,
      writes: [],
      summary,
      discarded: name === 'persona' ? [descarte] : [],
    })
    const steps = graphSteps(
      trace({
        state: 'completed',
        next: [],
        pause_kind: null,
        nodes: [
          node('scope', 0, { files: 1, ignored: 0 }),
          node('persona', 1, { returned: 1, contract: 0, l0: 1, kept: 0 }),
          node('merge', 2, { before: 3, after: 2 }),
          node('stamp', 3, { publish: 0, other: 0 }),
          node('publish', 4, { published: 0, inline: 0 }),
        ],
      }),
      [],
    )
    const caption = (name: string) => steps.find((step) => step.name === name)?.caption
    expect(caption('scope')).toBe('1 fichero en el diff')
    expect(caption('persona')).toBe('La skill devolvió 1 · 1 descartado por L0')
    expect(caption('merge')).toBe('2 tras juntar · 1 duplicado')
    expect(caption('stamp')).toBe('0 para publicar')
    expect(caption('publish')).toBe('No se escribió en el PR')
    expect(steps.find((step) => step.name === 'persona')?.discarded).toEqual([descarte])
  })

  it('sin cifras, un paso terminado conserva el texto fijo', () => {
    const scope = graphSteps(REVIEW.trace!, []).find((step) => step.name === 'scope')
    expect(scope).toMatchObject({ caption: 'Diff filtrado y contexto preparado', discarded: [] })
  })
})
