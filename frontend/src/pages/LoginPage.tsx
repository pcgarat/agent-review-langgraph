import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  CAlert,
  CButton,
  CCard,
  CCardBody,
  CCol,
  CContainer,
  CForm,
  CFormInput,
  CFormLabel,
  CInputGroup,
  CInputGroupText,
  CRow,
} from '@coreui/react'
import CIcon from '@coreui/icons-react'
import { cilLockLocked } from '@coreui/icons'

import { ApiError } from '@/api/client'
import { logIn, useSession } from '@/session/session'
import { Sygnet } from '@/components/Sygnet'

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
    <div className="bg-body-tertiary min-vh-100 d-flex flex-row align-items-center">
      <CContainer>
        <CRow className="justify-content-center">
          <CCol md={8} lg={6} xl={5}>
            <div className="d-flex flex-column gap-4">
              <div className="d-flex align-items-center justify-content-center gap-2">
                <Sygnet />
                <span className="fs-4 fw-semibold">Agent reviews</span>
              </div>
              <CCard className="p-4">
                <CCardBody className="d-flex flex-column gap-4">
                  <div className="text-center">
                    <h1 className="h5 mb-1">Entrar</h1>
                    <p className="text-body-secondary small mb-0">
                      Pega el token del despliegue (<code>PANEL_SESSION_SECRET</code>). Se guarda
                      como cookie de sesión y no vuelve a mostrarse.
                    </p>
                  </div>
                  <CForm onSubmit={submit} className="row gy-3">
                    <CCol xs={12}>
                      <CFormLabel htmlFor="panel-token">Token</CFormLabel>
                      <CInputGroup>
                        <CInputGroupText>
                          <CIcon icon={cilLockLocked} />
                        </CInputGroupText>
                        <CFormInput
                          id="panel-token"
                          type="password"
                          autoComplete="current-password"
                          autoFocus
                          value={token}
                          onChange={(event) => setToken(event.target.value)}
                          placeholder="••••••••••••••••"
                        />
                      </CInputGroup>
                    </CCol>
                    {error !== null ? (
                      <CCol xs={12}>
                        <CAlert color="danger" className="py-2 mb-0">
                          {error}
                        </CAlert>
                      </CCol>
                    ) : null}
                    <CCol xs={12}>
                      <CButton
                        type="submit"
                        color="primary"
                        className="w-100"
                        disabled={sending || !token.trim()}
                      >
                        {sending ? 'Entrando…' : 'Entrar'}
                      </CButton>
                    </CCol>
                  </CForm>
                </CCardBody>
              </CCard>
              <div className="text-center text-body-secondary small">Panel de operación</div>
            </div>
          </CCol>
        </CRow>
      </CContainer>
    </div>
  )
}
