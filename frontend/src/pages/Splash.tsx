import { CSpinner } from '@coreui/react'

/** Pantalla mientras se resuelve la sesión. No decide nada: solo espera. */
export function Splash({ message = 'Comprobando la sesión…' }: { message?: string }) {
  return (
    <div className="d-flex flex-column align-items-center justify-content-center min-vh-100 gap-3">
      <CSpinner color="primary" />
      <p className="text-body-secondary small mb-0">{message}</p>
    </div>
  )
}
