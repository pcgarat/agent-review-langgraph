import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { CAlert, CButton, CCard, CCardBody, CForm, CFormInput, CFormLabel } from '@coreui/react'

import { ApiError } from '@/api/client'
import { logIn, useSession } from '@/session/session'

/**
 * Entrada al panel (029, P6). Un solo campo: el token de despliegue.
 *
 * El token viaja en el cuerpo del `POST` y el servidor contesta con una cookie
 * `HttpOnly`; no se guarda en localStorage ni en la URL, y el campo se limpia al
 * enviar.
 */
export function LoginPage() {
  const [token, setToken] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  const { refresh } = useSession()
  const navigate = useNavigate()

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!token.trim() || sending) return
    setSending(true)
    setError(null)
    try {
      await logIn(token.trim())
      setToken('')
      refresh()
      navigate('/', { replace: true })
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.status === 401
          ? 'Ese token no vale.'
          : 'No se pudo entrar. Revisa que el panel esté levantado.',
      )
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="login-shell d-flex align-items-center justify-content-center min-vh-100">
      <CCard className="login-card">
        <CCardBody className="p-4 p-md-5">
          <p className="eyebrow mb-2">Panel de operación</p>
          <h1 className="h4 mb-1">Entrar</h1>
          <p className="text-body-secondary small mb-4">
            Pega el token del despliegue (<code>PANEL_SESSION_SECRET</code>). Se guarda como
            cookie de sesión y no vuelve a mostrarse.
          </p>
          <CForm onSubmit={submit} className="d-flex flex-column gap-3">
            <div>
              <CFormLabel htmlFor="panel-token">Token</CFormLabel>
              <CFormInput
                id="panel-token"
                type="password"
                autoComplete="current-password"
                autoFocus
                value={token}
                onChange={(event) => setToken(event.target.value)}
                placeholder="••••••••••••••••"
              />
            </div>
            {error !== null ? <CAlert color="danger" className="py-2 mb-0">{error}</CAlert> : null}
            <CButton type="submit" color="primary" disabled={sending || !token.trim()}>
              {sending ? 'Entrando…' : 'Entrar'}
            </CButton>
          </CForm>
        </CCardBody>
      </CCard>
    </div>
  )
}
