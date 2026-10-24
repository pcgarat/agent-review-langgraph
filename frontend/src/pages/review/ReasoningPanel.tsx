import { CCard, CCardBody, CCardHeader } from '@coreui/react'
import { useCallback, useState } from 'react'

import { parseJson, REASONING_SUBJECT, streamStatusText, useServerStream } from './streams'

interface ReasoningPanelProps {
  /** Endpoint SSE del razonamiento: solo se monta si el contrato dice que hay. */
  endpoint: string
}

interface ReasoningBlock {
  key: string
  role: string
  batch: number
  total: number
  files: string[]
  text: string
  /** Resumen del cierre (`end`) o el motivo del fallo (`fallo`). */
  footer: string | null
  failed: boolean
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' ? (value as Record<string, unknown>) : null
}

function textOf(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function filesOf(meta: unknown): string[] {
  const record = asRecord(meta)
  const files = record?.files
  return Array.isArray(files) ? files.map((file) => String(file)) : []
}

/**
 * Razonamiento de los revisores, lote a lote y en vivo (029, F6; 021 §5.3).
 *
 * Los eventos son `start`/`reasoning`/`end`/`fallo` y se agrupan por `role` y
 * `batch`, igual que el script antiguo: cada lote es un bloque con su cabecera y su
 * texto acumulado. El texto se pinta como nodos de React, nunca como HTML (P13).
 */
export function ReasoningPanel({ endpoint }: ReasoningPanelProps) {
  const [blocks, setBlocks] = useState<ReasoningBlock[]>([])

  const onEvent = useCallback((name: string, data: string) => {
    const event = asRecord(parseJson(data))
    if (event === null) return
    const role = textOf(event.role)
    const batch = typeof event.batch === 'number' ? event.batch : 0
    const total = typeof event.total === 'number' ? event.total : 0
    const key = `${role}/${batch}`

    const upsert = (update: (block: ReasoningBlock) => ReasoningBlock) => {
      setBlocks((previous) => {
        const index = previous.findIndex((block) => block.key === key)
        if (index === -1) {
          const fresh: ReasoningBlock = {
            key,
            role,
            batch,
            total,
            files: filesOf(event.meta),
            text: '',
            footer: null,
            failed: false,
          }
          return [...previous, update(fresh)]
        }
        return previous.map((block, position) =>
          position === index ? update(block) : block,
        )
      })
    }

    if (name === 'start') {
      upsert((block) => ({ ...block, files: filesOf(event.meta), footer: null }))
      return
    }
    if (name === 'reasoning') {
      const chunk = textOf(event.text)
      upsert((block) => ({ ...block, text: block.text + chunk }))
      return
    }
    if (name === 'end') {
      const meta = asRecord(event.meta) ?? {}
      const findings = typeof meta.findings === 'number' ? meta.findings : 0
      const tokens = typeof meta.reasoning_tokens === 'number' ? meta.reasoning_tokens : 0
      upsert((block) => ({
        ...block,
        footer: `${findings} hallazgos · ${tokens} tokens de razonamiento`,
      }))
      return
    }
    if (name === 'fallo') {
      const reason = textOf(event.text) || 'el lote falló'
      upsert((block) => ({ ...block, failed: true, footer: reason }))
    }
  }, [])

  const status = useServerStream(endpoint, {
    events: ['start', 'reasoning', 'end', 'fallo'],
    endEvent: 'fin',
    onEvent,
  })

  return (
    <CCard className="review-card">
      <CCardHeader className="d-flex flex-wrap align-items-baseline gap-2">
        <h2 className="h6 mb-0 me-auto">Razonamiento</h2>
        <span className="small text-body-secondary" role="status">
          {streamStatusText(status, REASONING_SUBJECT)}
        </span>
      </CCardHeader>
      <CCardBody>
        {blocks.length === 0 ? (
          <p className="text-body-secondary small mb-0">
            Todavía no hay razonamiento de los revisores.
          </p>
        ) : (
          blocks.map((block) => (
            <section key={block.key} className={block.failed ? 'reasoning failed' : 'reasoning'}>
              <p className="small mb-1">
                <strong>
                  {block.role} · lote {block.batch}
                  {block.total > 0 ? `/${block.total}` : ''}
                </strong>
                {block.files.length > 0 ? (
                  <span className="text-body-secondary ms-2">{block.files.join(', ')}</span>
                ) : null}
              </p>
              <pre className="review-reasoning mb-1">{block.text}</pre>
              {block.footer !== null ? (
                <p className={`small mb-2 ${block.failed ? 'text-danger' : 'text-body-secondary'}`}>
                  {block.footer}
                </p>
              ) : null}
            </section>
          ))
        )}
      </CCardBody>
    </CCard>
  )
}
