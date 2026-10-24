import { CButton, CModal, CModalBody, CModalFooter, CModalHeader, CModalTitle } from '@coreui/react'
import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { queryKeys } from '@/api/queryKeys'
import { usePanelCommand } from '@/pages/dashboard/usePanelCommand'

/**
 * «Olvidar» con confirmación (ADR-0034, 029 F4/F8).
 *
 * Vive en un solo sitio porque la ficha lo ofrece desde dos estados distintos: con
 * recorrido y en un descarte sin hilo. El comando va por `usePanelCommand`, que
 * invalida estado y bandeja; la ficha tiene su propia clave y se invalida aquí.
 */
export function ForgetReviewButton({ reviewKey }: { reviewKey: string }) {
  const [open, setOpen] = useState(false)
  const mutation = usePanelCommand()
  const queryClient = useQueryClient()

  const forget = () => {
    setOpen(false)
    mutation.mutate(
      { reviewKey, command: 'forget' },
      {
        onSettled: () => {
          void queryClient.invalidateQueries({ queryKey: queryKeys.review(reviewKey) })
        },
      },
    )
  }

  return (
    <>
      <CButton
        type="button"
        color="danger"
        variant="outline"
        size="sm"
        disabled={mutation.isPending}
        onClick={() => setOpen(true)}
      >
        {mutation.isPending ? '…' : 'Olvidar'}
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
          <CButton color="danger" onClick={forget}>
            Olvidar
          </CButton>
        </CModalFooter>
      </CModal>
    </>
  )
}
