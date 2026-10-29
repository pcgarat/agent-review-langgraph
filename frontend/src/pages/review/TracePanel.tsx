import { useMemo } from 'react'
import { CBadge, CCard, CCardBody, CCardHeader } from '@coreui/react'

import type { ReviewTrace } from '@/api/types/review'
import { formatDateTime } from '@/pages/dashboard/format'

import { stepLabel } from './claude'
import { deriveTimeline, formatDuration, spanTitle } from './trace'

interface TracePanelProps {
  trace: ReviewTrace
  /** Con el motor `claude` los nombres de nodo son pasos del CLI y se traducen. */
  claudeSteps: boolean
}

/**
 * Recorrido de la review (029, F1/F2) y **elemento firma** de la pantalla.
 *
 * Es una lista ordenada con marcas de tiempo, no un gráfico de la plantilla: el
 * contrato no trae duraciones, así que cada tramo se mide entre sellos de cierre
 * (ver `deriveTimeline`). El sello de cada fila es el momento en que ese nodo
 * terminó, no en el que empezó.
 */
export function TracePanel({ trace, claudeSteps }: TracePanelProps) {
  const timeline = useMemo(() => deriveTimeline(trace), [trace])
  const label = (name: string) => (claudeSteps ? stepLabel(name) : name)

  return (
    <CCard className="review-card">
      <CCardHeader className="d-flex flex-wrap align-items-baseline gap-2">
        <h2 className="h6 mb-0 me-auto">{claudeSteps ? 'Pasos del CLI' : 'Recorrido'}</h2>
        <span className="small text-body-secondary">
          {trace.state === 'paused' ? `pausa ${trace.pause_kind ?? ''}` : trace.state}
        </span>
        {timeline.totalMs !== null ? (
          <span className="small text-body-secondary num">
            {formatDuration(timeline.totalMs)} en total
          </span>
        ) : null}
      </CCardHeader>
      <CCardBody>
        {trace.nodes.length === 0 ? (
          <p className="text-body-secondary small mb-0">
            El hilo no tiene ningún nodo cerrado todavía.
          </p>
        ) : (
          <ol className="trace-list mb-0">
            {timeline.rows.map(({ node, durationMs, source }) => (
              <li key={`${node.index}:${node.name}`} className="trace-step">
                <span className="trace-index num">{node.index}</span>
                <div className="trace-main">
                  <div className="d-flex flex-wrap align-items-baseline gap-2">
                    <strong>{label(node.name)}</strong>
                    <span
                      className="trace-duration num ms-auto"
                      title={spanTitle(source)}
                      data-testid={`duration-${node.index}`}
                    >
                      {formatDuration(durationMs)}
                    </span>
                  </div>
                  <p className="small text-body-secondary mb-1">
                    cerrado a las {formatDateTime(node.started_at)}
                    {node.writes.length > 0 ? ` · escribe ${node.writes.join(', ')}` : ''}
                  </p>
                </div>
              </li>
            ))}
          </ol>
        )}

        {trace.next.length > 0 ? (
          <p className="small mb-0 mt-3">
            <span className="text-body-secondary me-2">Siguiente</span>
            {trace.next.map((name) => (
              <CBadge key={name} color="info" className="me-1">
                {label(name)}
              </CBadge>
            ))}
          </p>
        ) : null}
        {trace.state === 'paused' && trace.pause_kind !== null ? (
          <p className="small mb-0 mt-2">
            <span className="text-body-secondary me-2">Pausa</span>
            <CBadge color={trace.pause_kind === 'human' ? 'warning' : 'secondary'}>
              {trace.pause_kind === 'human' ? 'espera una persona' : 'espera la verificación de un fix'}
            </CBadge>
          </p>
        ) : null}
      </CCardBody>
    </CCard>
  )
}
