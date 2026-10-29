/**
 * Catálogo de diales del panel (027), espejo de `panel_config.catalog()`.
 *
 * `GET /api/config` trae valores, candado y revisión, pero **ni la sección ni la
 * ayuda**: las dos viven en el metadato `Dial` de `Settings`, que solo conoce
 * Python. Hasta que la API las publique, esta copia es lo que permite agrupar y
 * explicar cada control como hacía la página HTML.
 *
 * Un dial que el servidor añada y aquí falte no desaparece de la pantalla: se
 * pinta en «Otros» y sin ayuda. Ocultarlo dejaría el formulario incompleto y el
 * guardado respondería `400`.
 */

/** Orden de las secciones, el mismo de `SECTIONS` en `panel_config.py`. */
export const SECTIONS = [
  'Claude',
  'Presupuesto y lotes',
  'Verificador',
  'Bucle de lectura',
  'Política',
] as const

/** Cajón para un dial que esta versión del panel no conoce. */
export const OTHER_SECTION = 'Otros'

export interface DialBounds {
  min: number
  max: number
  /** `1` en un entero; `'any'` cuando el dial admite decimales. */
  step: number | 'any'
}

export interface DialSpec {
  section: string
  help: string
  /** Valores de un `Literal` del servidor. Sin esto, el dial es texto libre. */
  options?: readonly string[]
  /** Cotas de `_RANGES` en `panel_config.py`: solo los techos del CLI las tienen. */
  bounds?: DialBounds
}

export const DIALS: Readonly<Record<string, DialSpec>> = {
  claude_timeout_s: {
    section: 'Claude',
    help:
      'Segundos de pared del CLI y del checkout. Entre 1 y 21600. Si se agotan, el job ' +
      'se parkea (ceiling) y no se reintenta.',
    bounds: { min: 1, max: 21600, step: 'any' },
  },
  claude_max_turns: {
    section: 'Claude',
    help:
      'Turnos del CLI (--max-turns). Entre 1 y 2000. Si se agotan, el job se parkea ' +
      '(ceiling) y no se reintenta.',
    bounds: { min: 1, max: 2000, step: 1 },
  },
  max_autonomy_override: {
    section: 'Política',
    help:
      'Techo de autonomía por debajo del YAML. Vacío: manda config/autonomy.yaml. ' +
      'No puede subirlo.',
  },
  scope_ignore: {
    section: 'Política',
    help:
      'Patrones extra, separados por comas, de ficheros que el alcance ignora además ' +
      'de lockfiles, generados y vendor.',
  },
  llm_max_findings: {
    section: 'Presupuesto y lotes',
    help: 'Tope de hallazgos que una persona LLM puede emitir en la review.',
  },
  llm_budget_tokens_per_pr: {
    section: 'Presupuesto y lotes',
    help:
      'Tope de tokens de esa review, compartido por personas, fixer y verificador. ' +
      'Al llegar, lo que falte queda budget_exhausted.',
  },
  llm_batch_max_chars: {
    section: 'Presupuesto y lotes',
    help:
      'Tamaño de un lote, en caracteres de diff. Un fichero que no cabe se trocea por hunks.',
  },
  llm_batch_retries: {
    section: 'Presupuesto y lotes',
    help:
      'Reintentos de un lote si el proveedor corta, cierra vacío o cierra sin JSON. ' +
      'Agotados, el lote sale failed y la review sigue.',
  },
  llm_context_max_chars: {
    section: 'Presupuesto y lotes',
    help:
      'Tope del bloque de contexto (fichero completo y documentos del perfil) por lote. ' +
      'El diff no se recorta.',
  },
  verifier_max_findings: {
    section: 'Verificador',
    help:
      'Cuántos hallazgos de la review pasa el verificador, los más graves primero. ' +
      'El resto queda unverified.',
  },
  verifier_max_tool_calls: {
    section: 'Verificador',
    help: 'Llamadas a herramientas del verificador por hallazgo.',
  },
  verifier_max_context_bytes: {
    section: 'Verificador',
    help: 'Bytes de contexto que el verificador puede pedir por hallazgo.',
  },
  verifier_require_verified_for: {
    section: 'Verificador',
    help:
      'Severidades que exigen veredicto positivo. Si autonomy.yaml declara ' +
      'severity_gate.require_verified_for, manda el YAML.',
  },
  review_tools_roles: {
    section: 'Bucle de lectura',
    help:
      'Roles LLM que pueden leer y hacer grep mientras revisan. Vacío: una llamada ' +
      'por lote, sin herramientas.',
  },
  review_tools_max_turns: {
    section: 'Bucle de lectura',
    help: 'Turnos con herramientas por lote, sin contar el turno final forzado.',
  },
  review_tools_max_calls: {
    section: 'Bucle de lectura',
    help: 'Peticiones de herramientas por lote.',
  },
  review_tools_max_bytes: {
    section: 'Bucle de lectura',
    help: 'Bytes servidos por lote.',
  },
  review_tools_grep_mode: {
    section: 'Bucle de lectura',
    help:
      'Modo por defecto de grep: literal o regex. Un regex pedido explícito se atiende ' +
      'igual con el default literal.',
    options: ['literal', 'regex'],
  },
  review_tools_read_outline: {
    section: 'Bucle de lectura',
    help:
      'Permite mode=outline en read_file. Apagado, ese modo se sirve como full y se avisa.',
  },
  review_tools_read_state: {
    section: 'Bucle de lectura',
    help: 'Incluye en el prompt el bloque de lecturas ya hechas en la review.',
  },
  review_tools_truncation_notice: {
    section: 'Bucle de lectura',
    help: 'Al cortar un fichero, el aviso incluye el tamaño real.',
  },
  review_tools_strict_calls: {
    section: 'Bucle de lectura',
    help: 'Una petición no atendida es un error. Apagado, se descarta con un aviso.',
  },
  review_tools_calls_per_turn: {
    section: 'Bucle de lectura',
    help: 'Peticiones atendidas en un mismo turno.',
  },
  review_tools_precache_context: {
    section: 'Bucle de lectura',
    help: 'Precarga el contexto L1 en el bucle. Apagado, el modelo tiene que leerlo y gasta turnos.',
  },
  review_tools_io_cache: {
    section: 'Bucle de lectura',
    help: 'Reutiliza lecturas de la misma review. Ahorra lectura del repo, no tokens del prompt.',
  },
}

/**
 * Tipo de control, sacado del **valor inicial** y no del nombre.
 *
 * Es la misma regla que `_dial_control` en `ui.py`: un `bool` es un interruptor,
 * un número es una caja numérica y lo demás es texto. La excepción son los
 * `Literal`, que no se distinguen de un texto por su valor y necesitan su lista
 * de opciones: esos salen del catálogo.
 */
export type DialKind = 'bool' | 'number' | 'enum' | 'text'

export function dialKind(name: string, initial: unknown): DialKind {
  if (DIALS[name]?.options !== undefined) return 'enum'
  if (typeof initial === 'boolean') return 'bool'
  if (typeof initial === 'number') return 'number'
  return 'text'
}

export function dialSpec(name: string): DialSpec | undefined {
  return DIALS[name]
}

export function dialSection(name: string): string {
  return DIALS[name]?.section ?? OTHER_SECTION
}

export interface DialGroup {
  section: string
  names: string[]
}

/**
 * Agrupa los diales que el servidor publicó, en el orden de `SECTIONS`.
 *
 * Se recorre `initial`, no el catálogo: si el despliegue añade un dial, la
 * pantalla lo enseña aunque esta versión no sepa explicarlo. Una sección vacía no
 * se pinta.
 */
export function groupDials(initial: Readonly<Record<string, unknown>>): DialGroup[] {
  const groups = new Map<string, string[]>()
  for (const section of [...SECTIONS, OTHER_SECTION]) groups.set(section, [])

  for (const name of Object.keys(initial)) {
    const section = dialSection(name)
    const names = groups.get(section)
    if (names === undefined) {
      groups.set(section, [name])
    } else {
      names.push(name)
    }
  }

  return [...groups.entries()]
    .filter(([, names]) => names.length > 0)
    .map(([section, names]) => ({ section, names }))
}
