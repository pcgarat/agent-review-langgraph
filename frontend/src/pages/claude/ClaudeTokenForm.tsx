import { useState, type FormEvent } from 'react'
import { CAlert, CButton, CCard, CCardBody, CForm, CFormInput, CFormLabel } from '@coreui/react'

import { plural } from '@/pages/dashboard/format'
import { explainError } from '@/pages/dashboard/errors'

import { useSaveClaudeToken } from './useSaveClaudeToken'

const TOKEN_URL = 'https://claude.ai/settings/claude-code'

/** Qué decir de las reviews que la sesión nueva ha devuelto a la cola. */
function releasedText(released: number): string {
  if (released === 0) {
    return 'No había ninguna review parada por falta de sesión: la cola no cambia.'
  }
  return `${plural(released, 'review parada vuelve', 'reviews paradas vuelven')} a la cola.`
}

/**
 * Formulario del token de Claude (029, S8).
 *
 * Un solo campo, como el login: el token viaja en el cuerpo del `POST` y el
 * servidor no lo devuelve. El campo se limpia al guardar, así que no sobrevive
 * al envío ni en el estado de React ni en ningún almacén del navegador.
 */
export function ClaudeTokenForm() {
  const [token, setToken] = useState('')
  const save = useSaveClaudeToken()

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const value = token.trim()
    if (value === '' || save.isPending) return
    save.mutate(value, {
      onSuccess: () => {
        setToken('')
      },
    })
  }

  return (
    <CCard className="claude-card">
      <CCardBody>
        <p className="small text-body-secondary mb-4">
          El token se guarda en el fichero de sesión del CLI, en el servidor, y caduca: cuando
          vence, el runner lo retira y las reviews que dependan de él se paran hasta que pegues
          uno nuevo. Al guardarlo también vuelven a la cola las reviews que estaban esperando
          sesión. La respuesta no lo devuelve y no se queda en esta pestaña. Se genera en{' '}
          <a href={TOKEN_URL} target="_blank" rel="noopener noreferrer">
            {TOKEN_URL}
          </a>
          .
        </p>

        <CForm onSubmit={submit} className="d-flex flex-column gap-3">
          <div>
            <CFormLabel htmlFor="claude-token">Token</CFormLabel>
            <CFormInput
              id="claude-token"
              type="password"
              autoComplete="off"
              spellCheck={false}
              autoFocus
              value={token}
              onChange={(event) => {
                // Un aviso de «guardado» que sigue en pantalla mientras se pega
                // otro token se lee como si este ya estuviera guardado.
                if (save.isSuccess || save.isError) save.reset()
                setToken(event.target.value)
              }}
              placeholder="••••••••••••••••"
            />
          </div>

          {save.isError ? (
            <CAlert color="danger" className="py-2 mb-0" role="alert">
              {explainError(save.error, 'No se pudo guardar la sesión de Claude.')}
            </CAlert>
          ) : null}

          {save.isSuccess ? (
            <CAlert color="success" className="py-2 mb-0" role="status">
              <strong className="d-block">
                {save.data.released === 0
                  ? 'Token guardado'
                  : 'Token guardado y parqueo liberado'}
              </strong>
              <span className="small">{releasedText(save.data.released)}</span>
            </CAlert>
          ) : null}

          <CButton type="submit" color="primary" disabled={save.isPending || token.trim() === ''}>
            {save.isPending ? 'Guardando…' : 'Guardar'}
          </CButton>
        </CForm>
      </CCardBody>
    </CCard>
  )
}
