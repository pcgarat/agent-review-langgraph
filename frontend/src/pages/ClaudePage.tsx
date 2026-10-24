import { CAlert } from '@coreui/react'

import { useSession } from '@/session/session'

import { ClaudeTokenForm } from './claude/ClaudeTokenForm'
import './claude/claude.scss'

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
        Solo el rol operador. Sustituir el token no cancela nada: las reviews paradas por
        falta de sesión vuelven a la cola en cuanto se guarda.
      </p>
      <ClaudeTokenForm />
    </>
  )
}
