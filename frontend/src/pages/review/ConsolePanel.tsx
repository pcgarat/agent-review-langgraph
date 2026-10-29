import { CCard, CCardBody, CCardHeader } from '@coreui/react'
import { useCallback, useState } from 'react'

import {
  CONSOLE_SUBJECT,
  parseJson,
  streamStatusText,
  useServerStream,
} from './streams'
import { FollowToggle } from './FollowToggle'
import { useAutoScroll } from './useAutoScroll'

interface ConsolePanelProps {
  /** Endpoint SSE de la consola: solo se monta si el contrato dice que hay canal. */
  endpoint: string
  runKeys: string[]
}

/** Una línea del SSE del runner (`_sse("line", {seq, stream, text})`). */
function lineOf(data: string): string | null {
  const event = parseJson(data)
  if (event === null || typeof event !== 'object') return null
  const text = (event as { text?: unknown }).text
  return typeof text === 'string' ? text : null
}

/**
 * Consola del sandbox en vivo (029, F5).
 *
 * El contrato dice si el canal existe (`console_available`): si dice que no, el
 * componente no se monta y no se abre un `EventSource` que solo recibiría un `404`.
 * Si aun así el `404` llega —el runner perdió su buffer efímero— el hook lo cuenta
 * como `unavailable` y la ficha sigue entera.
 */
export function ConsolePanel({ endpoint, runKeys }: ConsolePanelProps) {
  const [lines, setLines] = useState<string[]>([])

  const onEvent = useCallback((_name: string, data: string) => {
    const line = lineOf(data)
    if (line !== null) setLines((previous) => [...previous, line])
  }, [])

  const status = useServerStream(endpoint, { events: ['line'], endEvent: 'end', onEvent })
  const autoScroll = useAutoScroll<HTMLPreElement>(lines.length)
  const { scrollRef, onScroll } = autoScroll

  return (
    <CCard className="review-card">
      <CCardHeader className="d-flex flex-wrap align-items-baseline gap-2">
        <h2 className="h6 mb-0 me-auto">Consola en vivo</h2>
        <FollowToggle id="console-follow" autoScroll={autoScroll} />
        <span className="small text-body-secondary">
          {runKeys.length > 0 ? `Run keys: ${runKeys.join(', ')}` : 'Sin run keys'}
        </span>
      </CCardHeader>
      <CCardBody>
        <p className="small text-body-secondary" role="status">
          {streamStatusText(status, CONSOLE_SUBJECT)}
        </p>
        <pre
          className="review-console mb-0"
          data-testid="review-console"
          ref={scrollRef}
          onScroll={onScroll}
        >
          {lines.join('\n')}
        </pre>
      </CCardBody>
    </CCard>
  )
}
