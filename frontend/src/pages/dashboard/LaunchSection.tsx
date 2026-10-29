import { useState, type FormEvent } from 'react'
import { CAlert, CButton, CCard, CCardBody, CCol, CForm, CFormInput, CFormLabel, CRow } from '@coreui/react'
import { useMutation, useQueryClient } from '@tanstack/react-query'

import { api } from '@/api/client'
import { AFTER_COMMAND } from '@/api/queryKeys'
import { useToast } from '@/components/ToastProvider'

import { explainError } from './errors'

/** Respuesta de `POST /api/launch`; no trae `review_key` porque aún no hay SHA. */
interface LaunchResult {
  status: string
  repo: string
  pr_id: number
  pipeline_id: string
  actor: string
}

/** Lanzar una review pegando la URL del pull request (029, D3). */
export function LaunchSection() {
  const [url, setUrl] = useState('')
  const queryClient = useQueryClient()
  const { notify } = useToast()

  const launch = useMutation({
    mutationFn: (value: string) => api.post<LaunchResult>('/api/launch', { url: value }),
    onSuccess: (result) => {
      setUrl('')
      notify({
        title: 'Review encolada',
        body: `${result.repo}#${result.pr_id}: el worker leerá el commit y la identidad de la revisión nace de él.`,
      })
    },
    onError: (error) => {
      notify({ title: 'No se pudo lanzar', body: explainError(error), color: 'danger' })
    },
    onSettled: () => {
      for (const key of AFTER_COMMAND) {
        void queryClient.invalidateQueries({ queryKey: key })
      }
    },
  })

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const trimmed = url.trim()
    if (trimmed === '' || launch.isPending) return
    launch.mutate(trimmed)
  }

  return (
    <CCard className="mb-4">
      <CCardBody>
        <p className="eyebrow mb-2">Lanzar review</p>
        <p className="text-body-secondary small">
          Pega la URL del pull request. El worker lee el commit de ahora y encola la review;
          repetir el mismo commit no publica otra vez.
        </p>
        <CForm onSubmit={submit}>
          <CRow className="g-2 align-items-end">
            <CCol xs={12} md={9}>
              <CFormLabel htmlFor="launch-url" className="visually-hidden">
                URL del pull request
              </CFormLabel>
              <CFormInput
                id="launch-url"
                type="url"
                autoComplete="off"
                placeholder="https://github.com/org/repo/pull/123"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                disabled={launch.isPending}
              />
            </CCol>
            <CCol xs={12} md={3}>
              <CButton
                type="submit"
                color="primary"
                className="w-100"
                disabled={launch.isPending || url.trim() === ''}
              >
                {launch.isPending ? 'Lanzando…' : 'Lanzar'}
              </CButton>
            </CCol>
          </CRow>
        </CForm>
        {launch.isError ? (
          <CAlert color="danger" className="py-2 mt-3 mb-0 small">
            {/* El detalle ya lo dice el toast; aquí queda el último fallo a la vista. */}
            {explainError(launch.error)}
          </CAlert>
        ) : null}
      </CCardBody>
    </CCard>
  )
}
