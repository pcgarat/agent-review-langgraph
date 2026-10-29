import { CCard, CCardBody, CCardHeader } from '@coreui/react'

interface LogPanelProps {
  notes: string[]
  journal: string[]
}

/**
 * Log de la review (029, F3): notas del estado final y anotaciones del worker.
 *
 * Se pinta como texto dentro de un `<pre>`: son líneas de datos de terceros y el
 * panel no interpreta HTML ajeno (P13). Mismo criterio que `_log_text` del
 * contrato: las líneas vacías no son contenido y cada bloque se separa con una
 * línea en blanco.
 */
export function LogPanel({ notes, journal }: LogPanelProps) {
  const text = [...notes, ...journal]
    .map((line) => line.trim())
    .filter((line) => line !== '')
    .join('\n\n')

  return (
    <CCard className="review-card">
      <CCardHeader className="d-flex flex-wrap align-items-baseline gap-2">
        <h2 className="h6 mb-0 me-auto">Log</h2>
        <span className="small text-body-secondary">
          {text === '' ? 'sin anotaciones' : 'notas del estado y del worker'}
        </span>
      </CCardHeader>
      <CCardBody>
        <pre className="review-log mb-0" data-testid="review-log">
          {text === '' ? 'No hay anotaciones de esta review.' : text}
        </pre>
      </CCardBody>
    </CCard>
  )
}
