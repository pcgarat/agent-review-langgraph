import { CButton, CModal, CModalBody, CModalFooter, CModalHeader, CModalTitle } from '@coreui/react'
import { useState } from 'react'

import { usePanelCommand } from './usePanelCommand'

function useCommand(reviewKey: string) {
  const mutation = usePanelCommand()
  return {
    mutation,
    // Un botón solo se bloquea por su propio comando: cancelar una fila no deja
    // toda la tabla en «…» esperando.
    busy: mutation.isPending && mutation.variables?.reviewKey === reviewKey,
  }
}

/** Cancelar: lo pendiente de una review en cola, en vuelo o pausada. */
export function CancelButton({
  reviewKey,
  label = 'Cancelar',
}: {
  reviewKey: string
  label?: string
}) {
  const { mutation, busy } = useCommand(reviewKey)
  return (
    <CButton
      type="button"
      size="sm"
      color="secondary"
      variant="outline"
      disabled={busy}
      onClick={() => mutation.mutate({ reviewKey, command: 'cancel' })}
    >
      {busy ? '…' : label}
    </CButton>
  )
}

/** Reintentar: reencola un descarte de la DLQ. */
export function RetryButton({ reviewKey }: { reviewKey: string }) {
  const { mutation, busy } = useCommand(reviewKey)
  return (
    <CButton
      type="button"
      size="sm"
      color="secondary"
      variant="outline"
      disabled={busy}
      onClick={() => mutation.mutate({ reviewKey, command: 'retry' })}
    >
      {busy ? '…' : 'Reintentar'}
    </CButton>
  )
}

/**
 * Olvidar: borra la ejecución local en el worker (ADR-0034).
 *
 * Es el único comando destructivo del cuadro de mando y no se puede deshacer desde
 * el panel, así que pide confirmación en un `CModal` antes de llamar a la API.
 */
export function ForgetButton({ reviewKey }: { reviewKey: string }) {
  const [open, setOpen] = useState(false)
  const { mutation, busy } = useCommand(reviewKey)

  return (
    <>
      <CButton
        type="button"
        size="sm"
        color="danger"
        variant="outline"
        disabled={busy}
        onClick={() => setOpen(true)}
      >
        Olvidar
      </CButton>
      <CModal visible={open} onClose={() => setOpen(false)} alignment="center">
        <CModalHeader>
          <CModalTitle>Olvidar esta review</CModalTitle>
        </CModalHeader>
        <CModalBody>
          <p className="mb-2">
            Se borrarán el hilo y el artefacto locales de <code>{reviewKey}</code>.
          </p>
          <p className="text-body-secondary small mb-0">
            Los comentarios ya publicados en el pull request no se tocan. Esta acción no se
            puede deshacer desde el panel.
          </p>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" variant="outline" onClick={() => setOpen(false)}>
            No, dejarla
          </CButton>
          <CButton
            color="danger"
            onClick={() => {
              setOpen(false)
              mutation.mutate({ reviewKey, command: 'forget' })
            }}
          >
            Olvidar
          </CButton>
        </CModalFooter>
      </CModal>
    </>
  )
}
