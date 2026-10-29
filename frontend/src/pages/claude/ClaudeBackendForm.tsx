import { CAlert } from '@coreui/react'

import { explainError } from '@/pages/dashboard/errors'

import {
  CLAUDE_BACKENDS,
  type ClaudeBackendId,
  useClaudeBackend,
  useSaveClaudeBackend,
} from './useClaudeBackend'

/**
 * Tres destinos del mismo CLI. Cambiar uno no reinicia nada: la siguiente
 * review lo lee. Claude pide el token; los dos del clúster no.
 */
export function ClaudeBackendForm() {
  const query = useClaudeBackend()
  const save = useSaveClaudeBackend()
  const selected = (save.data ?? query.data)?.effective ?? 'claude'

  const choose = (choice: ClaudeBackendId) => {
    if (choice === selected || save.isPending) return
    save.mutate(choice)
  }

  return (
    <fieldset className="claude-backend mb-4">
      <legend className="form-label">Modelo</legend>
      <div className="d-flex flex-column gap-2">
        {CLAUDE_BACKENDS.map((option) => (
          <label key={option.id} className="form-check" htmlFor={`claude-backend-${option.id}`}>
            <input
              className="form-check-input"
              type="radio"
              name="claude-backend"
              id={`claude-backend-${option.id}`}
              checked={selected === option.id}
              disabled={save.isPending}
              onChange={() => choose(option.id)}
            />
            <span className="form-check-label">
              {option.label} <span className="text-body-secondary">{option.detail}</span>
            </span>
          </label>
        ))}
      </div>
      {save.isError ? (
        <CAlert color="danger" className="py-2 mt-3 mb-0" role="alert">
          {explainError(save.error, 'No se pudo guardar el modelo.')}
        </CAlert>
      ) : null}
      {query.isError ? (
        <CAlert color="danger" className="py-2 mt-3 mb-0" role="alert">
          {explainError(query.error, 'No se pudo leer el modelo.')}
        </CAlert>
      ) : null}
    </fieldset>
  )
}
