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
})
