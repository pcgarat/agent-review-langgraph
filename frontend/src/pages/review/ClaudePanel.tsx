import { CCard, CCardBody, CCardHeader } from '@coreui/react'
import { useCallback, useState } from 'react'

import type { ClaudeAvailability } from '@/api/types/review'

import { formatClaudeLine } from './claude'
import { FollowToggle } from './FollowToggle'
import { CLAUDE_SUBJECT, streamStatusText, useServerStream } from './streams'
import { useAutoScroll } from './useAutoScroll'

interface ClaudePanelProps {
  availability: ClaudeAvailability
  /** Endpoints del contrato, ya construidos con `reviewPath`. */
  eventsEndpoint: string
  downloadEndpoint: string
}

/**
 * Actividad del CLI de Claude (029, F7).
 *
 * Cada parte se ofrece solo si el servidor dice que existe: un `EventSource` contra
 * un endpoint que responde `404` reintenta para siempre, y un enlace de descarga a
 * un fichero que no está es un `404` a la vista. La transcripción es texto de
 * terceros y se pinta como texto (P13).
 */
export function ClaudePanel({ availability, eventsEndpoint, downloadEndpoint }: ClaudePanelProps) {
  const [lines, setLines] = useState<string[]>([])

  const onEvent = useCallback((_name: string, data: string) => {
    setLines((previous) => [...previous, formatClaudeLine(data)])
  }, [])

  const status = useServerStream(availability.events_available ? eventsEndpoint : null, {
    events: ['line'],
    endEvent: 'end',
    onEvent,
  })
  const autoScroll = useAutoScroll<HTMLPreElement>(lines.length)
  const { scrollRef, onScroll } = autoScroll

  return (
    <CCard className="review-card">
      <CCardHeader className="d-flex flex-wrap align-items-baseline gap-2">
        <h2 className="h6 mb-0 me-auto">Actividad del CLI</h2>
        {availability.events_available ? (
          <FollowToggle id="claude-follow" autoScroll={autoScroll} />
        ) : null}
        {availability.events_available ? (
          <span className="small text-body-secondary" role="status">
            {streamStatusText(status, CLAUDE_SUBJECT)}
          </span>
        ) : (
          <span className="small text-body-secondary">Sin transcripción todavía</span>
        )}
      </CCardHeader>
      <CCardBody>
        <pre
          className="review-console mb-2"
          data-testid="claude-events"
          ref={scrollRef}
          onScroll={onScroll}
        >
          {lines.join('\n')}
        </pre>
        {availability.download_available ? (
          <a href={downloadEndpoint} download>
            Descargar la conversación
          </a>
        ) : (
          <p className="small text-body-secondary mb-0">
            La conversación entera aparecerá aquí cuando el CLI la haya escrito.
          </p>
        )}
      </CCardBody>
    </CCard>
  )
}
