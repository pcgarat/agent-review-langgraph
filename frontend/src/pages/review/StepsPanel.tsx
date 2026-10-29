import { CCard, CCardBody, CCardHeader } from '@coreui/react'

import type { ReviewTrace } from '@/api/types/review'
import type { ActiveNode } from '@/api/types/state'

import { graphSteps, type StepView } from './steps'

/**
 * Pasos que tiene que dar la review, de arriba abajo.
 *
 * El texto de la derecha dice lo que está haciendo (con el spinner) o lo que
 * dejó hecho. Una pausa no gira: el worker ya no ejecuta ese nodo.
 */
export function StepsPanel({
  trace,
  running,
}: {
  trace: ReviewTrace
  running: readonly ActiveNode[]
}) {
  const steps = graphSteps(trace, running)
  return (
    <CCard className="review-card">
      <CCardHeader>
        <h2 className="h6 mb-0">Pasos</h2>
      </CCardHeader>
      <CCardBody>
        <ol className="step-list mb-0">
          {steps.map((step) => (
            <StepRow key={step.name} step={step} />
          ))}
        </ol>
      </CCardBody>
    </CCard>
  )
}

function StepRow({ step }: { step: StepView }) {
  const current = step.phase === 'running' || step.phase === 'paused'
  return (
    <li className={`step-row ${step.phase}`} data-step={step.name} aria-current={current ? 'step' : undefined}>
      <span className="step-mark" aria-hidden="true">
        {step.phase === 'done' ? '✓' : null}
        {step.phase === 'paused' ? (
          <>
            <span className="pause-bar" />
            <span className="pause-bar" />
          </>
        ) : null}
      </span>
      <span className="step-name">{step.title}</span>
      {step.caption === '' ? null : (
        <p className="step-caption">
          {step.phase === 'running' ? <span className="step-spinner" aria-hidden="true" /> : null}
          {step.caption}
        </p>
      )}
    </li>
  )
}
