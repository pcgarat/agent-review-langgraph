/** Diagrama de los pasos del grafo, en el orden en que una review los recorre. */

import type { DiscardedFinding, ReviewTrace } from '@/api/types/review'
import type { ActiveNode } from '@/api/types/state'

export type StepPhase = 'running' | 'paused' | 'done' | 'pending' | 'skipped'

interface StepSpec {
  name: string
  title: string
  doing: string
  done: string
}

/** Ningún nodo se salta: la decisión de no hacer nada vive dentro del paso. */
const GRAPH_STEPS: readonly StepSpec[] = [
  {
    name: 'scope',
    title: 'Alcance',
    doing: 'Leyendo el diff y preparando el contexto',
    done: 'Diff filtrado y contexto preparado',
  },
  {
    name: 'persona',
    title: 'Personas',
    doing: 'Revisando el código con las personas',
    done: 'Revisión de las personas terminada',
  },
  {
    name: 'merge',
    title: 'Fusión',
    doing: 'Uniendo los hallazgos duplicados',
    done: 'Hallazgos unidos',
  },
  {
    name: 'verify',
    title: 'Verificación',
    doing: 'Contrastando los candidatos',
    done: 'Candidatos contrastados',
  },
  {
    name: 'policy',
    title: 'Política',
    doing: 'Aplicando la política',
    done: 'Política aplicada',
  },
  {
    name: 'hitl',
    title: 'Decisión',
    doing: 'Esperando una decisión humana',
    done: 'Decisión humana registrada',
  },
  {
    name: 'fix_dispatch',
    title: 'Arreglos',
    doing: 'Proponiendo arreglos',
    done: 'Arreglos propuestos',
  },
  {
    name: 'publish',
    title: 'Publicación',
    doing: 'Publicando la review en el PR',
    done: 'Review publicada en el PR',
  },
  {
    name: 'fix_wait',
    title: 'Espera',
    doing: 'Esperando la verificación de los arreglos',
    done: 'Verificación de los arreglos recogida',
  },
  {
    name: 'publish_fix',
    title: 'Seguimiento',
    doing: 'Publicando el resultado de los arreglos',
    done: 'Resultado de los arreglos publicado',
  },
  {
    name: 'notify_fix',
    title: 'Aviso del fix',
    doing: 'Avisando del resultado de los arreglos',
    done: 'Aviso de arreglos enviado',
  },
  {
    name: 'notify',
    title: 'Aviso',
    doing: 'Avisando de la review en el canal',
    done: 'Aviso de la review enviado',
  },
]

const BY_NAME = new Map(GRAPH_STEPS.map((step) => [step.name, step]))

export interface StepView {
  name: string
  title: string
  phase: StepPhase
  caption: string
  /** Hallazgos que el paso tiró, con su motivo (034). */
  discarded: DiscardedFinding[]
}

/**
 * Estado de cada paso a partir del recorrido y de los nodos en curso.
 *
 * La pausa gana al «en curso»: en un interrupt el worker ya no ejecuta el nodo,
 * aunque el checkpoint lo deje en `next`. Sin marca de progreso, `next` es el
 * nodo vivo de una review que sigue `in_progress`.
 */
export function graphSteps(trace: ReviewTrace, running: readonly ActiveNode[]): StepView[] {
  const names = GRAPH_STEPS.map((step) => step.name)
  for (const node of trace.nodes) {
    if (!names.includes(node.name)) names.push(node.name)
  }
  for (const name of [...trace.next, ...running.map((node) => node.node)]) {
    if (!names.includes(name)) names.push(name)
  }
  const completed = new Set(trace.nodes.map((node) => node.name))
  const active = new Set(running.map((node) => node.node))
  const pending = new Set(trace.next)
  const useNext = trace.state === 'in_progress' && active.size === 0
  // Un nodo que corrió dos veces (reanudación) cuenta por su última pasada.
  const lastRun = new Map(trace.nodes.map((node) => [node.name, node]))
  return names.map((name) => {
    const phase = phaseOf(trace.state, name, completed, active, pending, useNext)
    const spec = BY_NAME.get(name)
    const branches = running
      .filter((node) => node.node === name)
      .reduce((sum, node) => sum + node.branches, 0)
    const node = lastRun.get(name)
    const counted = phase === 'done' && node?.summary ? summaryCaption(name, node.summary) : null
    return {
      name,
      title: spec?.title ?? name,
      phase,
      caption: counted ?? caption(spec, name, phase, branches),
      discarded: phase === 'done' ? (node?.discarded ?? []) : [],
    }
  })
}

function count(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`
}

/** Texto con las cifras del paso (034 §4.1), o `null` si no sabe leerlas. */
function summaryCaption(name: string, summary: Record<string, number>): string | null {
  const n = (key: string) => summary[key] ?? 0
  switch (name) {
    case 'scope': {
      const parts = [`${count(n('files'), 'fichero', 'ficheros')} en el diff`]
      if (n('ignored') > 0) parts.push(count(n('ignored'), 'ignorado', 'ignorados'))
      return parts.join(' · ')
    }
    case 'persona': {
      if (n('returned') === 0) return 'La skill no devolvió hallazgos'
      const parts = [`La skill devolvió ${n('returned')}`]
      if (n('contract') > 0) {
        parts.push(`${count(n('contract'), 'descartado', 'descartados')} por contrato`)
      }
      if (n('l0') > 0) parts.push(`${count(n('l0'), 'descartado', 'descartados')} por L0`)
      if (n('kept') > 0) parts.push(`${n('kept')} ${n('kept') === 1 ? 'entra' : 'entran'}`)
      return parts.join(' · ')
    }
    case 'merge': {
      const parts = [`${n('after')} tras juntar`]
      const duplicates = n('before') - n('after')
      if (duplicates > 0) parts.push(count(duplicates, 'duplicado', 'duplicados'))
      return parts.join(' · ')
    }
    case 'stamp':
      return `${n('publish')} para publicar`
    case 'publish':
      if (n('published') === 0) return 'No se escribió en el PR'
      return n('inline') > 0 ? `Comentario en el PR · ${n('inline')} en línea` : 'Comentario en el PR'
    default:
      return null
  }
}

function phaseOf(
  state: ReviewTrace['state'],
  name: string,
  completed: ReadonlySet<string>,
  active: ReadonlySet<string>,
  pending: ReadonlySet<string>,
  useNext: boolean,
): StepPhase {
  if (state === 'paused' && pending.has(name)) return 'paused'
  if (state !== 'paused' && (active.has(name) || (useNext && pending.has(name)))) return 'running'
  if (completed.has(name)) return 'done'
  if (state === 'completed') return 'skipped'
  return 'pending'
}

function caption(
  spec: StepSpec | undefined,
  name: string,
  phase: StepPhase,
  branches: number,
): string {
  if (phase === 'running') {
    const text = spec?.doing ?? `Ejecutando ${name}`
    return branches > 1 ? `${text} (${branches} ramas)` : text
  }
  if (phase === 'paused') return spec?.doing ?? `En pausa en ${name}`
  if (phase === 'done') return spec?.done ?? `Paso ${name} terminado`
  if (phase === 'skipped') return 'No se ejecutó'
  return ''
}
