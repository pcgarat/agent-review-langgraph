import { CAlert } from '@coreui/react'

import { useSession } from '@/session/session'

import { ClaudeBackendForm } from './claude/ClaudeBackendForm'
import { ClaudeTokenForm } from './claude/ClaudeTokenForm'
import { useClaudeBackend } from './claude/useClaudeBackend'
import './claude/claude.scss'

/** El token solo hace falta si el destino es Anthropic. */
function SessionForm() {
  const query = useClaudeBackend()
  // Sin respuesta todavía no se pinta nada: con un modelo del clúster el
  // formulario aparecería y desaparecería. Si la lectura falla, se enseña.
  if (query.isPending) return null
  const needsSession = query.data?.needs_session ?? true
  if (!needsSession) return null
  return <ClaudeTokenForm />
}

/**
 * Sesión de Claude (029, S8).
 *
 * El sidebar ya esconde el enlace para un `lector` y el servidor respondería
 * `403`, pero la ruta sigue siendo accesible por URL: si alguien llega, se le
 * dice por qué no hay formulario en vez de enseñarle un campo que no puede usar.
 */
export function ClaudePage() {
  const { isOperador } = useSession()

  if (!isOperador) {
    return (
      <CAlert color="info" className="py-2 small">
        Tu rol es <strong>lector</strong>: guardar el token del CLI es del rol operador.
      </CAlert>
    )
  }

  return (
    <>
      <p className="eyebrow mb-3">
        Solo el rol operador. El modelo de la siguiente review se lee al lanzarla: cambiarlo
        no reinicia nada. Con Claude, las reviews paradas por falta de sesión vuelven a la
        cola al guardar el token. Con un modelo del clúster no hay token.
      </p>
      <ClaudeBackendForm />
      <SessionForm />
    </>
  )
}
