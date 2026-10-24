/**
 * Qué enseña la ficha cuando el motor es el CLI de Claude (029, F2/F7).
 *
 * Los nombres de nodo salen del grafo de `REVIEW_ENGINE=claude`; las líneas del
 * CLI llegan como eventos JSON y se reducen a texto antes de pintarse.
 */

const CLAUDE_STEP: Record<string, string> = {
  scope: 'Preparar el diff',
  persona: 'Leer los hallazgos de Claude',
  merge: 'Juntar hallazgos',
  stamp: 'Marcar para publicar',
  hitl: 'Esperar decisión',
  publish: 'Publicar en el pull request',
  notify: 'Avisar',
}

export function stepLabel(name: string): string {
  return CLAUDE_STEP[name] ?? name
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' ? (value as Record<string, unknown>) : null
}

/**
 * La línea que manda el servidor es un JSON *string* (`_sse("line", line)` en
 * `claude_activity.py` envuelve la línea entera), así que hay que desenvolverla
 * antes de leer el evento del CLI. Un payload que ya venga como objeto se usa tal
 * cual: el canal no debe romperse por una forma distinta.
 */
function unwrap(data: string): unknown {
  let event: unknown
  try {
    event = JSON.parse(data)
  } catch {
    return null
  }
  if (typeof event === 'string') {
    try {
      return JSON.parse(event)
    } catch {
      return event
    }
  }
  return event
}

/**
 * Una línea del SSE a texto legible.
 *
 * El runner escribe eventos del CLI (mensajes con bloques `text`, `tool_use` y
 * `tool_result`, más el `result` final). Lo que no encaja con esa forma se pinta
 * tal cual: la transcripción es dato de terceros y perderla es peor que mostrarla
 * cruda (P13: siempre como texto).
 */
export function formatClaudeLine(raw: string): string {
  const event = unwrap(raw)
  const record = asRecord(event)
  // Una línea que no es un evento (texto suelto, JSON de otra forma) se pinta tal
  // cual: perder la transcripción es peor que mostrarla cruda.
  if (record === null) return typeof event === 'string' ? event : raw

  const bits: string[] = []
  const message = asRecord(record.message)
  const content = message?.content
  if (Array.isArray(content)) {
    for (const item of content) {
      const block = asRecord(item)
      if (block === null) continue
      if (block.type === 'text' && typeof block.text === 'string') {
        bits.push(block.text)
      } else if (block.type === 'tool_use') {
        bits.push(`herramienta ${typeof block.name === 'string' ? block.name : ''}`)
      } else if (block.type === 'tool_result') {
        bits.push(
          typeof block.content === 'string' ? block.content : String(block.content ?? ''),
        )
      }
    }
  }
  if (record.type === 'result' && typeof record.result === 'string' && record.result !== '') {
    bits.push(record.result)
  }
  return bits.length > 0 ? bits.join('\n') : raw
}
