import { CCard, CCardBody } from '@coreui/react'

/**
 * Aviso de pantalla pendiente.
 *
 * No repite el nombre de la pantalla: la cabecera ya lo dice. Solo cuenta qué
 * falta y en qué bloque llega. En el bloque E las páginas existen para poder
 * recorrer la navegación y la sesión de verdad; no pintan datos falsos, porque
 * una cifra inventada se lee como verdad y acaba en una decisión de operación.
 */
export function PendingPane({ block }: { block: string }) {
  return (
    <CCard>
      <CCardBody>
        <p className="eyebrow mb-2">Pendiente · bloque {block}</p>
        <p className="text-body-secondary small mb-2">
          La sesión, las rutas y el armazón ya funcionan. El contenido llega en el bloque{' '}
          {block} del{' '}
          <a href="https://github.com/pcgarat/agent-review-langgraph/blob/main/docs/iniciativas/029-panel-en-react/plan.md">
            plan de la iniciativa 029
          </a>
          .
        </p>
        <p className="text-body-secondary small mb-0">
          Hasta entonces esta pantalla está vacía a propósito: preferimos un hueco visible a
          un dato de mentira.
        </p>
      </CCardBody>
    </CCard>
  )
}
