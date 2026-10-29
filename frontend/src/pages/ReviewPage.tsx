import { CAlert, CButton, CCard, CCardBody } from '@coreui/react'
import { useEffect, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { reviewPath } from '@/api/client'
import type { ReviewDetail } from '@/api/types/review'
import { useSession } from '@/session/session'
import { explainError } from '@/pages/dashboard/errors'
import { formatDateTime } from '@/pages/dashboard/format'

import { ClaudePanel } from './review/ClaudePanel'
import { ConsolePanel } from './review/ConsolePanel'
import { DecisionPanel } from './review/DecisionPanel'
import { DiscardPanel } from './review/DiscardPanel'
import { LogPanel } from './review/LogPanel'
import { ReasoningPanel } from './review/ReasoningPanel'
import { ReviewCommands } from './review/ReviewCommands'
import { StepsPanel } from './review/StepsPanel'
import { TracePanel } from './review/TracePanel'
import { useReviewDetail } from './review/useReviewDetail'
import './review/review.scss'

/**
 * Ficha de una review (029, bloque F; spec §5.4).
 *
 * Dos columnas: a la izquierda el diagrama de pasos del grafo —o los pasos del
 * CLI cuando el motor es `claude`— y a la derecha el recorrido, el log, los
 * comandos y los canales en vivo. Ese panel de la derecha se puede maximizar.
 * En móvil queda una sola columna y los pasos van primero.
 *
 * Todo lo que se pinta viene del recurso agregado (`GET /api/reviews/{key}`,
 * P8): el contrato ya dice qué canales existen y la ficha no abre un `EventSource`
 * que el servidor no puede servir.
 */
export function ReviewPage({ reviewKey }: { reviewKey: string }) {
  const { isOperador } = useSession()
  const { data, isPending, isError, error, refetch, isFetching } = useReviewDetail(reviewKey)

  // La clave se pinta siempre, incluso mientras carga: es la identidad de la
  // pantalla y un enlace publicado que no la enseña parece roto.
  const header = (
    <div className="d-flex flex-wrap align-items-baseline gap-2 mb-3">
      <p className="eyebrow mb-0 text-break">{reviewKey}</p>
      {data?.trace ? (
        <span className="small text-body-secondary">
          Estado <strong>{data.trace.state}</strong> · actualizado el{' '}
          {formatDateTime(data.trace.updated_at)}
        </span>
      ) : null}
      <CButton
        type="button"
        color="secondary"
        variant="ghost"
        size="sm"
        className="ms-auto"
        disabled={isFetching}
        onClick={() => void refetch()}
      >
        {isFetching ? 'Actualizando…' : 'Actualizar'}
      </CButton>
    </div>
  )

  if (isPending) {
    return (
      <>
        {header}
        <p className="text-body-secondary small mb-0">Leyendo la ficha de la review…</p>
      </>
    )
  }

  if (isError) {
    return (
      <>
        {header}
        <CCard>
          <CCardBody>
            <p className="eyebrow mb-2">Sin ficha</p>
            <CAlert color="danger" className="py-2 mb-3">
              {explainError(error, 'No se pudo leer la review.')}
            </CAlert>
            <div className="d-flex flex-wrap gap-2">
              <CButton type="button" color="primary" variant="outline" onClick={() => void refetch()}>
                Reintentar
              </CButton>
              <CButton type="button" color="secondary" variant="ghost" as={Link} to="/">
                Volver al panel
              </CButton>
            </div>
          </CCardBody>
        </CCard>
      </>
    )
  }

  return (
    <>
      {header}
      <ReviewBody detail={data} isOperador={isOperador} />
    </>
  )
}

function ReviewBody({ detail, isOperador }: { detail: ReviewDetail; isOperador: boolean }) {
  const { review_key: reviewKey, trace, paused, claude } = detail
  // El contrato devuelve `claude` con el motor del CLI y `available` dice si la
  // ficha de Claude aplica (motor `claude` y clave con commit): ese es el criterio
  // para enseñar los pasos del CLI en lugar del recorrido (F2). La sección se pinta
  // con solo existir el objeto; dentro, cada parte mira su propio booleano (F7).
  const claudeSteps = claude?.available === true
  // F8: sin traza y sin pausa. Hay log, pero no hay recorrido que enseñar.
  const discard = trace === null && paused === null
  const runKeys = (paused?.jobs ?? [])
    .map((job) => String(job.run_key ?? ''))
    .filter((runKey) => runKey !== '')
  const base = reviewPath(reviewKey)
  const decide =
    trace !== null && trace.pause_kind === 'human' && paused !== null && paused.kind === 'human'

  return (
    <>
      {discard ? (
        <p className="small text-body-secondary">Descartada antes de dejar recorrido</p>
      ) : trace === null ? (
        <p className="small text-body-secondary">Sin recorrido de nodos</p>
      ) : null}

      <div className="review-grid">
        <div className="review-main">
          {trace !== null && !claudeSteps ? (
            <StepsPanel trace={trace} running={detail.running} />
          ) : trace !== null ? (
            <TracePanel trace={trace} claudeSteps={claudeSteps} />
          ) : discard ? (
            <DiscardPanel reviewKey={reviewKey} isOperador={isOperador} />
          ) : (
            <CCard className="review-card">
              <CCardBody>
                <h2 className="h6">Sin recorrido de nodos</h2>
                <p className="text-body-secondary small mb-0">
                  El grafo no dejó un hilo para esta review. A la derecha queda lo que sí
                  escribió: las notas del estado y el journal del worker.
                </p>
              </CCardBody>
            </CCard>
          )}
        </div>

        <ReviewSide>
          {trace !== null && !claudeSteps ? <TracePanel trace={trace} claudeSteps={false} /> : null}
          <LogPanel notes={detail.notes} journal={detail.journal} />

          {/* La ficha sin traza ni pausa (F8) lleva sus propios comandos en la
              tarjeta del descarte, como `_forget_commands` en la UI antigua: repetir
              «Olvidar» aquí daría dos botones para la misma acción. */}
          {discard ? null : (
            <ReviewCommands reviewKey={reviewKey} trace={trace} canCommand={isOperador} />
          )}

          {decide && paused !== null ? (
            <DecisionPanel reviewKey={reviewKey} paused={paused} canDecide={isOperador} />
          ) : null}

          {paused !== null && paused.kind === 'fix' ? (
            <CCard className="review-card">
              <CCardBody>
                <h2 className="h6">Esperando la verificación de un fix</h2>
                <p className="text-body-secondary small mb-0">
                  Esta pausa es posterior a publicar. Cancelar detiene lo que aún corre;
                  los comentarios ya escritos en el pull request se quedan.
                </p>
              </CCardBody>
            </CCard>
          ) : null}

          {detail.console_available ? (
            <ConsolePanel endpoint={`${base}/console`} runKeys={runKeys} />
          ) : null}

          {detail.reasoning_available ? <ReasoningPanel endpoint={`${base}/reasoning`} /> : null}

          {/* La sección existe con el motor `claude`; dentro, cada parte se ofrece
              solo si el contrato dice que está (F7). */}
          {claude !== null ? (
            <ClaudePanel
              availability={claude}
              eventsEndpoint={`${base}/claude/events`}
              downloadEndpoint={`${base}/claude/download`}
            />
          ) : null}
        </ReviewSide>
      </div>
    </>
  )
}

/** El panel de la derecha ocupa la ventana. Escape lo devuelve a su sitio. */
function ReviewSide({ children }: { children: ReactNode }) {
  const [maximized, setMaximized] = useState(false)
  useEffect(() => {
    if (!maximized) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMaximized(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [maximized])
  return (
    <div className={maximized ? 'review-side is-max' : 'review-side'}>
      <div className="d-flex justify-content-end">
        <CButton
          type="button"
          color="secondary"
          variant="ghost"
          size="sm"
          aria-pressed={maximized}
          onClick={() => setMaximized((value) => !value)}
        >
          {maximized ? 'Restaurar' : 'Maximizar'}
        </CButton>
      </div>
      {children}
    </div>
  )
}
